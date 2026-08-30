"""Trạng thái camera và lệnh ghi **có xác nhận** — pha 3.

Quy tắc của pha này chỉ có một câu: *mọi lệnh ghi phải xác nhận được bằng một
lệnh đọc tương ứng*. Lý do là ràng buộc của chính giao thức — lệnh ghi của C12
**không có phản hồi**, nên "đã gửi" và "đã có tác dụng" là hai chuyện khác hẳn
nhau. Gói có thể mất (UDP), firmware có thể không hỗ trợ command word đó, hoặc
tham số nằm ngoài dải và bị bỏ qua im lặng. Nếu UI chỉ hiển thị thứ *ta vừa gửi*
thì nó đang nói dối người dùng trong cả ba trường hợp.

Vì vậy :meth:`CameraService.apply` không trả về "đã gửi". Nó trả về **đã đọc lại
và thấy gì**, ở một trong ba mức:

``direct``
    Có lệnh đọc trả đúng giá trị vừa ghi. ``REC``, ``IMG``, ``VID``, nhóm nhiệt.
    Đây là mức duy nhất chứng minh được lệnh có tác dụng.

``relative``
    Không đặt được giá trị tuyệt đối, chỉ tăng/giảm một nấc — ``DZM``. Xác nhận
    bằng cách đọc trước, ghi, đọc lại, rồi so *chiều* thay đổi.

``indirect``
    Không có lệnh đọc nào tương ứng — ``CAP`` (chụp ảnh). Bằng chứng gián tiếp
    duy nhất là dung lượng thẻ giảm. Yếu, và được đánh dấu là yếu.

Kết quả xác nhận là **ba trạng thái**, không phải hai: ``ok=None`` nghĩa là
*không xác nhận được* (lệnh đọc im lặng, chưa cắm thẻ). Gộp nó vào "thất bại" sẽ
làm người dùng đi sửa nhầm chỗ.

Bộ nhớ đệm trạng thái poll ở nhịp thấp và **tự bỏ những trường im lặng**: trên
phần cứng thật khá nhiều lệnh đọc sẽ không tồn tại, mà mỗi lần dò một lệnh chết
là một lần chờ hết timeout. Ba lần im liên tiếp thì giãn ra 30 giây một lần —
vẫn thử lại, nhưng không làm nghẽn vòng poll.

Một ngoại lệ quan trọng cho phép suy luận đó: **im lặng lúc gimbal đang quay
không phải bằng chứng**. Vòng điều khiển 20 Hz chiếm hàng ưu tiên của
``udp_link``, nên lệnh đọc kẹt tới hết timeout dù firmware hoàn toàn hỗ trợ nó.
Vòng poll nghỉ trong lúc đó (:func:`CameraService` nhận một predicate ``busy``),
và im lặng khi bận không được tính vào chuỗi im lặng.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from dataclasses import dataclass
from typing import Callable

from ..protocol import registry as reg
from ..protocol.codec import Frame
from ..protocol.registry import CommandNotAllowed
from ..protocol.types import Palette, Resolution, SDCardStatus
from ..transport.udp_link import UdpLink

log = logging.getLogger("c12ctl.camera")

POLL_INTERVAL = 1.0
"""Nhịp vòng poll, giây. Trường nào tới hạn thì đọc, không phải đọc tất cả."""

READ_TIMEOUT = 0.3
"""Ngắn hơn mặc định 1 s của registry: một vòng poll chạm nhiều trường, mà lệnh
không hỗ trợ thì im lặng cho tới hết timeout."""

SETTLE = 0.15
"""Chờ trước khi đọc lại. Camera cần vài chục ms để lệnh có tác dụng thật."""

CONFIRM_ATTEMPTS = 3
"""Số lần đọc lại trước khi kết luận. Gói UDP có thể mất — một lần là không đủ."""

DEAD_AFTER = 3
"""Số lần im lặng liên tiếp để coi một trường là không được hỗ trợ."""

DEAD_RETRY = 30.0
"""Trường đã coi là chết thì bao lâu thử lại một lần."""

SLOW_EVERY = 10.0
"""Tham số nhiệt gần như không tự đổi — đọc thưa, đỡ tốn lưu lượng."""


# --------------------------------------------------------------------------
# Xác nhận
# --------------------------------------------------------------------------

DIRECT = "direct"
RELATIVE = "relative"
INDIRECT = "indirect"


@dataclass(frozen=True)
class Expectation:
    """Điều lệnh đọc phải thấy sau khi ghi."""

    describe: str
    matches: Callable[[object], bool]

    note: str = ""
    """Luôn kèm theo kết quả — cách đọc con số này, kể cả khi khớp."""

    if_mismatch: str = ""
    """Chỉ kèm khi **không** khớp: vì sao chưa chắc đã là lệnh trượt."""


@dataclass(frozen=True)
class Verify:
    """Cách xác nhận một lệnh ghi bằng một lệnh đọc."""

    read: str
    """Tên lệnh đọc dùng để xác nhận."""

    kind: str
    expect: Callable[[tuple, object], Expectation]
    """``(args, giá trị đọc trước khi ghi) → kỳ vọng``."""

    needs_before: bool = False
    """Có phải đọc giá trị cũ trước khi ghi không. Chỉ ``relative``/``indirect``."""

    doc: str = ""


def _eq(value: object, note: str = "") -> Expectation:
    return Expectation(describe=str(value), matches=lambda a: a == value, note=note)


def _as_palette(value) -> Palette:
    return value if isinstance(value, Palette) else Palette[str(value).upper()]


def _as_resolution(value) -> Resolution:
    return value if isinstance(value, Resolution) else Resolution[str(value).upper()]


def _expect_palette(args: tuple, before: object) -> Expectation:
    return _eq(_as_palette(args[0]).name)


def _expect_resolution(args: tuple, before: object) -> Expectation:
    return _eq(_as_resolution(args[0]).name)


def _expect_percent(args: tuple, before: object) -> Expectation:
    return _eq(max(0, min(100, int(args[0]))))


def _expect_zoom_in(args: tuple, before: object) -> Expectation:
    level = int(before)
    return Expectation(
        describe="> %d" % level,
        matches=lambda a: int(a) > level,
        # Trần zoom thật chưa xác minh (bytecode gợi ý 0–67). Nếu không đổi thì
        # rất có thể đã chạm trần chứ không phải lệnh trượt — nhưng ta CHƯA biết
        # trần ở đâu nên không được tự nhận là thành công.
        if_mismatch="không đổi có thể là đã chạm trần zoom — dải thật chưa xác minh",
    )


def _expect_zoom_out(args: tuple, before: object) -> Expectation:
    level = int(before)
    if level <= 0:
        # Đáy thì biết chắc là 0, khác với trần. Không đổi ở đây là ĐÚNG.
        return _eq(0, note="đã ở đáy zoom, không đổi là đúng")
    return Expectation(describe="< %d" % level, matches=lambda a: int(a) < level)


def _expect_snap(args: tuple, before: object) -> Expectation:
    card: SDCardStatus = before
    if not getattr(card, "present", False):
        raise Unverifiable(
            "thẻ nhớ báo 0/0 — chưa cắm thẻ thì không có bằng chứng nào cho CAP"
        )
    free = card.free_mb
    return Expectation(
        describe="free_mb < %d" % free,
        matches=lambda a: getattr(a, "free_mb", free) < free,
        note="bằng chứng GIÁN TIẾP: CAP không có lệnh đọc tương ứng, chỉ suy ra "
             "từ dung lượng thẻ giảm",
        if_mismatch="ảnh nhỏ hơn đơn vị báo cáo của thẻ có thể không làm free_mb "
                    "đổi — không khớp ở đây là bằng chứng yếu, chưa phải kết luận",
    )


class Unverifiable(RuntimeError):
    """Không thể dựng kỳ vọng: thiếu dữ liệu nền, không phải lệnh sai."""


#: Lệnh ghi camera nào xác nhận được bằng lệnh đọc nào. **Đây là allowlist thứ
#: hai**: :meth:`CameraService.apply` chỉ chạy lệnh có mặt ở đây, nên một lệnh
#: ghi không xác nhận được thì không đi qua đường này.
WRITES: dict[str, Verify] = {
    "camera.record_start": Verify(
        "read.recording", DIRECT, lambda a, b: _eq(True),
        doc="REC=01 rồi đọc REC lại phải thấy đang ghi",
    ),
    "camera.record_stop": Verify(
        "read.recording", DIRECT, lambda a, b: _eq(False),
        doc="REC=00 rồi đọc REC lại phải thấy đã dừng",
    ),
    "camera.palette": Verify(
        "read.palette", DIRECT, _expect_palette,
        doc="IMG là palette — đọc lại IMG phải trả đúng giá trị vừa đặt",
    ),
    "camera.resolution": Verify(
        "read.resolution", DIRECT, _expect_resolution,
        doc="VID đọc lại phải trả đúng độ phân giải vừa đặt",
    ),
    "camera.zoom_in": Verify(
        "read.zoom", RELATIVE, _expect_zoom_in, needs_before=True,
        doc="DZM chỉ tăng/giảm một nấc — đọc DZM trước và sau, so chiều",
    ),
    "camera.zoom_out": Verify(
        "read.zoom", RELATIVE, _expect_zoom_out, needs_before=True,
        doc="DZM một nấc xuống; ở đáy (0) thì không đổi là đúng",
    ),
    "camera.snap": Verify(
        "read.sdcard", INDIRECT, _expect_snap, needs_before=True,
        doc="CAP không có lệnh đọc tương ứng — chỉ suy ra từ dung lượng thẻ giảm",
    ),
}

for _suffix in ("spatial_nr", "shutter", "detail", "gamma",
                "brightness", "contrast", "temporal_nr"):
    WRITES["camera.thermal_" + _suffix] = Verify(
        "read.thermal_" + _suffix, DIRECT, _expect_percent,
        doc="Tham số nhiệt 0–100, đọc lại phải trả đúng giá trị đã clamp",
    )
del _suffix


# --------------------------------------------------------------------------
# Bộ nhớ đệm trạng thái
# --------------------------------------------------------------------------

#: ``(tên trường, lệnh đọc, chu kỳ giây)``. ``0`` = mỗi vòng poll; ``inf`` = chỉ
#: đọc một lần, vì model và phiên bản firmware không đổi trong lúc chạy.
POLL_SPECS: tuple[tuple[str, str, float], ...] = (
    ("model", "read.model", math.inf),
    ("version", "read.version", math.inf),
    ("hardware_version", "read.hardware_version", math.inf),
    ("recording", "read.recording", 0.0),
    ("zoom", "read.zoom", 0.0),
    ("palette", "read.palette", 0.0),
    ("resolution", "read.resolution", 0.0),
    ("sdcard", "read.sdcard", 5.0),
    ("thermal_spatial_nr", "read.thermal_spatial_nr", SLOW_EVERY),
    ("thermal_shutter", "read.thermal_shutter", SLOW_EVERY),
    ("thermal_detail", "read.thermal_detail", SLOW_EVERY),
    ("thermal_gamma", "read.thermal_gamma", SLOW_EVERY),
    ("thermal_brightness", "read.thermal_brightness", SLOW_EVERY),
    ("thermal_contrast", "read.thermal_contrast", SLOW_EVERY),
    ("thermal_temporal_nr", "read.thermal_temporal_nr", SLOW_EVERY),
)


@dataclass
class Field:
    """Một giá trị camera trong bộ nhớ đệm, kèm tuổi và lịch sử im lặng."""

    name: str
    read: str
    every: float

    value: object = None
    raw: str | None = None
    updated_at: float = 0.0
    checked_at: float = 0.0
    replies: int = 0
    silent_streak: int = 0
    dead_after: int = DEAD_AFTER
    """Ngưỡng của service sở hữu trường này, không phải hằng số module."""

    @property
    def supported(self) -> bool | None:
        """``None`` = chưa đủ dữ liệu để kết luận."""
        if self.replies:
            return True
        return False if self.silent_streak >= self.dead_after else None

    @property
    def age(self) -> float | None:
        return None if not self.updated_at else time.monotonic() - self.updated_at

    def as_dict(self) -> dict:
        age = self.age
        return {
            "name": self.name,
            "read": self.read,
            "value": _jsonable(self.value),
            "raw": self.raw,
            "supported": self.supported,
            "silent_streak": self.silent_streak,
            "age_ms": round(age * 1000) if age is not None else None,
        }


@dataclass
class CameraStats:
    reads: int = 0
    silent: int = 0
    skipped: int = 0
    """Vòng poll bỏ qua vì link đang bận lưu lượng ưu tiên."""

    applies: int = 0
    verified: int = 0
    mismatched: int = 0
    unverified: int = 0

    def as_dict(self) -> dict:
        return {
            "reads": self.reads, "silent": self.silent, "skipped": self.skipped,
            "applies": self.applies, "verified": self.verified,
            "mismatched": self.mismatched, "unverified": self.unverified,
        }


@dataclass
class ApplyResult:
    """Kết quả một lệnh ghi *đã đọc lại*."""

    action: str
    command: str
    frame: str
    kind: str
    read: str
    expected: str
    before: object = None
    actual: object = None
    ok: bool | None = None
    attempts: int = 0
    elapsed_ms: float = 0.0
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "action": self.action, "command": self.command, "frame": self.frame,
            "kind": self.kind, "read": self.read, "expected": self.expected,
            "before": _jsonable(self.before), "actual": _jsonable(self.actual),
            "ok": self.ok, "attempts": self.attempts,
            "elapsed_ms": round(self.elapsed_ms, 1), "note": self.note,
        }


# --------------------------------------------------------------------------


class CameraService:
    """Đệm trạng thái camera, và chạy lệnh ghi kèm bước đọc lại xác nhận."""

    def __init__(
        self,
        link: UdpLink,
        *,
        interval: float = POLL_INTERVAL,
        timeout: float = READ_TIMEOUT,
        settle: float = SETTLE,
        attempts: int = CONFIRM_ATTEMPTS,
        dead_after: int = DEAD_AFTER,
        dead_retry: float = DEAD_RETRY,
        busy: Callable[[], bool] | None = None,
    ) -> None:
        self.link = link
        # Gimbal đang quay thì hàng ưu tiên chiếm gần hết khe gửi (20 Hz × 2 gói,
        # cách nhau tối thiểu 15 ms) và lệnh đọc sẽ kẹt tới hết timeout. Im lặng
        # lúc đó nói về lưu lượng, KHÔNG nói về việc firmware có hỗ trợ lệnh hay
        # không — nên vòng poll nghỉ, và im lặng không bị tính là bằng chứng.
        self._busy = busy or (lambda: False)
        self.interval = max(0.05, interval)
        self.timeout = timeout
        self.settle = settle
        self.attempts = attempts
        self.dead_after = dead_after
        self.dead_retry = dead_retry

        self.fields: dict[str, Field] = {
            name: Field(name=name, read=read, every=every, dead_after=dead_after)
            for name, read, every in POLL_SPECS
        }
        self.stats = CameraStats()
        self.last_apply: ApplyResult | None = None

        self._task: asyncio.Task | None = None
        # Hai người cùng chờ một command word thì udp_link chỉ trao gói cho
        # người đầu tiên — người kia timeout và ghi nhầm một lần "im lặng".
        # Vòng poll và bước đọc-lại của apply dùng chung khoá này để không bao
        # giờ chồng nhau.
        self._read_lock = asyncio.Lock()

    # -------------------------------------------------------------- vòng đời

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop(), name="camera-poll")
        log.info("đệm trạng thái camera: poll %.1f Hz, timeout đọc %.0f ms",
                 1 / self.interval, self.timeout * 1000)

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - poll hỏng không được giết app
                log.exception("vòng poll camera lỗi — thử lại ở nhịp sau")
            await asyncio.sleep(self.interval)

    # ------------------------------------------------------------------ đọc

    async def poll_once(self, *, force: bool = False) -> list[Field]:
        """Đọc những trường tới hạn. ``force`` đọc tất cả, kể cả trường đã chết.

        Nghỉ hẳn một vòng khi link đang bận lưu lượng ưu tiên — ``force`` (người
        dùng bấm "Đọc lại") vẫn đi qua, vì đó là yêu cầu tường minh.
        """
        if not force and self._busy():
            self.stats.skipped += 1
            return []
        now = time.monotonic()
        done = []
        for f in self.fields.values():
            if force or self._due(f, now):
                done.append(await self._read_field(f))
        return done

    def _due(self, f: Field, now: float) -> bool:
        if not f.checked_at:
            return True
        return now - f.checked_at >= self._gap(f)

    def _gap(self, f: Field) -> float:
        """Bao lâu nữa mới đọc lại trường này."""
        if f.replies == 0 and f.silent_streak >= self.dead_after:
            # Im lặng liên tục = C12 không hỗ trợ lệnh đọc này. Mỗi lần thử lại
            # là một lần chờ hết timeout, nên giãn ra thay vì bỏ hẳn.
            return self.dead_retry
        if math.isinf(f.every):
            # Trường tĩnh: đọc được rồi thì thôi, chưa được thì thử lại đều.
            return math.inf if f.replies else self.interval
        return f.every

    async def _read_field(self, f: Field) -> Field:
        f.dead_after = self.dead_after      # ngưỡng đổi lúc chạy vẫn phải theo kịp
        cmd = reg.get(f.read)
        raw: list[str] = []

        def capture(frame: Frame, _cmd3=cmd.cmd3, _raw=raw) -> None:
            if frame.cmd3 == _cmd3:
                _raw.append(frame.data)

        async with self._read_lock:
            self.link.subscribe(capture)
            try:
                value = await self.link.request(cmd, timeout=self.timeout)
            finally:
                self.link.unsubscribe(capture)

        f.checked_at = time.monotonic()
        self.stats.reads += 1
        if value is None:
            self.stats.silent += 1
            if self._busy():
                # Im lặng lúc link bão hoà không nói lên điều gì về firmware.
                return f
            f.silent_streak += 1
            if f.silent_streak == self.dead_after:
                log.info("%s im lặng %d lần — giãn xuống %.0f s/lần",
                         f.read, f.silent_streak, self.dead_retry)
        else:
            f.value = value
            f.raw = raw[-1] if raw else None
            f.updated_at = f.checked_at
            f.replies += 1
            f.silent_streak = 0
        return f

    def field_for(self, read_name: str) -> Field | None:
        for f in self.fields.values():
            if f.read == read_name:
                return f
        return None

    # ------------------------------------------------------------------ ghi

    async def apply(self, action: str, *args) -> ApplyResult:
        """Gửi một lệnh ghi camera rồi **đọc lại để xác nhận**.

        :raises CommandNotAllowed: lệnh không nằm trong :data:`WRITES`. Lệnh ghi
            không xác nhận được thì không đi qua đường này.
        """
        name = action if action.startswith("camera.") else "camera." + action
        spec = WRITES.get(name)
        if spec is None:
            raise CommandNotAllowed(
                "%r không phải lệnh ghi camera xác nhận được. Chỉ những lệnh có "
                "một lệnh đọc tương ứng mới chạy được ở đây: %s"
                % (action, ", ".join(sorted(WRITES)))
            )
        cmd = reg.get(name)
        read_field = self.field_for(spec.read)
        if read_field is None:  # pragma: no cover - mọi mục WRITES đều có trong POLL_SPECS
            read_field = self.fields[spec.read] = Field(
                name=spec.read, read=spec.read, every=math.inf
            )
        started = time.monotonic()

        before = None
        if spec.needs_before:
            f = await self._read_field(read_field)
            before = f.value

        # Dựng kỳ vọng TRƯỚC khi gửi. Tham số sai (palette lạ, thiếu dữ liệu nền)
        # phải hỏng ở đây, lúc chưa có gói nào rời khỏi backend.
        note = ""
        try:
            expectation = spec.expect(tuple(args), before)
        except Unverifiable as exc:
            expectation = None
            note = str(exc)
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            raise ValueError("tham số không hợp lệ cho %s: %s" % (name, exc)) from None

        frame = self.link.send(cmd, *args)
        self.stats.applies += 1

        result = ApplyResult(
            action=name.split(".", 1)[1], command=name, frame=frame,
            kind=spec.kind, read=spec.read,
            expected=expectation.describe if expectation else "—",
            before=before, note=note,
        )

        if expectation is None:
            # Không dựng được kỳ vọng (chưa cắm thẻ). Vẫn đọc lại một lần để
            # trả về trạng thái tươi, nhưng không kết luận gì.
            replies = read_field.replies
            f = await self._read_field(read_field)
            result.actual = f.value if f.replies > replies else None
            result.attempts = 1
            result.ok = None
        else:
            await self._confirm(result, expectation, read_field)
            _add_note(result, expectation.note)
            if result.ok is False:
                _add_note(result, expectation.if_mismatch)

        result.elapsed_ms = (time.monotonic() - started) * 1000
        self.last_apply = result

        if result.ok is True:
            self.stats.verified += 1
        elif result.ok is False:
            self.stats.mismatched += 1
            log.warning("%s: đọc lại thấy %r, kỳ vọng %s",
                        name, result.actual, result.expected)
        else:
            self.stats.unverified += 1
            log.warning("%s: không xác nhận được — %s", name, result.note or "lệnh đọc im lặng")
        return result

    async def _confirm(self, result: ApplyResult, expectation: Expectation,
                       f: Field) -> None:
        """Đọc lại tới khi khớp, hoặc hết lượt. Gói UDP mất thì lượt sau bù.

        Mốc so sánh là ``f.replies``, **không phải** ``f.value``: đệm cố ý giữ
        giá trị cũ kèm tuổi khi lệnh đọc im lặng, nên so với ``f.value`` sẽ biến
        một lần im lặng thành "camera vẫn báo giá trị cũ" — tức là kết luận
        ``ok=False`` từ số liệu chưa hề được đọc lại. Đúng loại nói dối mà cả cơ
        chế xác nhận này sinh ra để chặn.
        """
        replies_before = f.replies
        for attempt in range(1, self.attempts + 1):
            await asyncio.sleep(self.settle)
            await self._read_field(f)
            result.attempts = attempt
            answered = f.replies > replies_before
            result.actual = f.value if answered else None
            if not answered:
                continue
            if expectation.matches(f.value):
                result.ok = True
                return
        if f.replies == replies_before:
            result.ok = None
            result.note = result.note or (
                "%s không phản hồi trong lúc gimbal đang quay — hàng ưu tiên "
                "chiếm hết khe gửi. Thử lại khi gimbal đứng yên." % f.read
                if self._busy() else
                "%s không phản hồi — không có cách nào xác nhận lệnh này trên "
                "phần cứng hiện tại" % f.read
            )
        else:
            result.ok = False

    # ------------------------------------------------------------- trạng thái

    def as_dict(self) -> dict:
        return {
            "interval": self.interval,
            "running": self._task is not None and not self._task.done(),
            "fields": {name: f.as_dict() for name, f in self.fields.items()},
            "stats": self.stats.as_dict(),
            "last_apply": self.last_apply.as_dict() if self.last_apply else None,
            "actions": [
                {
                    "action": name.split(".", 1)[1],
                    "command": name,
                    "read": v.read,
                    "kind": v.kind,
                    "doc": reg.COMMANDS[name].doc,
                    "verify_doc": v.doc,
                    "takes_args": reg.COMMANDS[name].encode is not None,
                }
                for name, v in sorted(WRITES.items())
            ],
            "options": {
                "palette": [p.name for p in Palette],
                "resolution": [r.name for r in Resolution],
            },
        }


def _add_note(result: ApplyResult, text: str) -> None:
    if text and text not in result.note:
        result.note = (result.note + " · " + text) if result.note else text


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return str(value)
