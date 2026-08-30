"""Vòng điều khiển gimbal — 20 Hz, watchdog, và năm ngả dừng khẩn.

Trình duyệt **không** tick 20 Hz. Nó chỉ báo khi trạng thái *đổi* (nhấn/nhả phím,
cần analog di chuyển); nhịp nằm ở đây, nơi độ trễ ổn định và không phụ thuộc tab
có đang được vẽ hay không.

Vì sao vẫn gửi lại 20 Hz dù có thể thừa: hai tài liệu nguồn mâu thuẫn về việc
gimbal có tự dừng khi ngừng nhận gói hay không, và không phân xử được từ tài liệu.
**Keepalive 20 Hz là tập cha của cả hai hành vi** — nếu gimbal tự dừng, keepalive
giữ nó chạy; nếu nó giữ lệnh, keepalive vô hại và watchdog vẫn là lớp an toàn cần có.
:class:`~c12ctl.sim.c12_sim.C12Simulator` chạy được cả hai kiểu và có test cho cả hai.

Mọi đường dẫn tới dừng đều đi qua đúng một hàm: :meth:`GimbalController.stop_all`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from ..protocol.registry import STOP_FRAMES
from ..protocol.types import MAX_SPEED_DPS, clamp_speed
from ..transport.udp_link import UdpLink
from .telemetry import TelemetryService

log = logging.getLogger("c12ctl.gimbal")

TICK = 0.05
"""Chu kỳ vòng điều khiển, giây. 20 Hz."""

WATCHDOG = 0.5
"""Không nhận cập nhật nào trong khoảng này thì tự dừng."""

ZERO_REPEATS = 3
"""Số lần gửi tốc độ 0 khi dừng. Gói UDP có thể mất — một lần là không đủ."""

DEFAULT_MAX_SPEED = 10.0
"""°/s. Thấp có chủ ý cho lần chạy đầu; chỉ nâng sau khi đã thấy vòng dừng đúng."""

SOFT_LIMIT_DEG = 85.0
"""Chặn trước biên cơ khí ±90° khi có tư thế thật từ GAC."""


@dataclass
class ControlState:
    yaw: float = 0.0
    pitch: float = 0.0
    updated_at: float = field(default_factory=time.monotonic)

    @property
    def moving(self) -> bool:
        return bool(self.yaw or self.pitch)

    def as_dict(self) -> dict:
        return {"yaw": self.yaw, "pitch": self.pitch}


@dataclass
class GimbalStats:
    ticks: int = 0
    packets: int = 0
    stops: int = 0
    watchdog_trips: int = 0
    limit_trips: int = 0
    rejected: int = 0
    last_stop_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "ticks": self.ticks, "packets": self.packets, "stops": self.stops,
            "watchdog_trips": self.watchdog_trips, "limit_trips": self.limit_trips,
            "rejected": self.rejected, "last_stop_reason": self.last_stop_reason,
        }


class NotArmed(PermissionError):
    """Lệnh gây chuyển động khi phiên chưa ARM."""


class GimbalController:
    """Giữ nhịp 20 Hz, cưỡng chế giới hạn, và dừng khi có bất kỳ nghi ngờ nào."""

    def __init__(
        self,
        link: UdpLink,
        *,
        max_speed: float = DEFAULT_MAX_SPEED,
        telemetry: TelemetryService | None = None,
        tick: float = TICK,
        watchdog: float = WATCHDOG,
        use_gsm: bool = False,
        soft_limit: float = SOFT_LIMIT_DEG,
    ) -> None:
        self.link = link
        self.max_speed = min(max_speed, MAX_SPEED_DPS)
        self.telemetry = telemetry
        self.tick = tick
        self.watchdog = watchdog
        self.soft_limit = soft_limit

        # GSM gộp yaw+pitch vào một gói, giảm nửa lưu lượng ở 20 Hz — nhưng cần
        # firmware gimbal ≥ 0.5. KHÔNG tự thăm dò được: GSM là lệnh ghi, không có
        # phản hồi, nên cách duy nhất để biết nó có tác dụng là ra lệnh chuyển
        # động thật rồi xem gimbal có nhúc nhích không. Thăm dò bằng cách làm
        # gimbal quay là đánh đổi sai. Mặc định dùng hai gói rời — luôn chạy.
        self.use_gsm = use_gsm

        self.armed = False
        self.state = ControlState()
        self.stats = GimbalStats()
        self._task: asyncio.Task | None = None
        self._last_sent: tuple[float, float] | None = None
        self._zeros_left = 0

    # -------------------------------------------------------------- vòng đời

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="gimbal-control")
        log.info("vòng điều khiển chạy: %.0f Hz, trần %.1f °/s, watchdog %.0f ms%s",
                 1 / self.tick, self.max_speed, self.watchdog * 1000,
                 ", GSM" if self.use_gsm else "")

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # Vòng đã chết vì lỗi và đã tự dừng gimbal ở chỗ đó rồi. close()
                # là đường dọn dẹp — nó tuyệt đối không được ném thêm, nếu không
                # phần dọn dẹp phía sau sẽ không chạy.
                log.debug("vòng điều khiển kết thúc do lỗi", exc_info=True)
        # Ngả thứ năm: thoát tiến trình cũng phải để lại gimbal đứng yên.
        self._emit_stop_frames()
        self.armed = False

    # ------------------------------------------------------------- arm / stop

    def arm(self) -> None:
        self.armed = True
        self.state = ControlState()
        self._touch()
        log.warning("ARM — lệnh gây chuyển động đã mở")

    def stop_all(self, reason: str = "yêu cầu dừng") -> None:
        """Đường dừng khẩn duy nhất. Mọi ngả khác đều gọi vào đây.

        Đồng bộ và không ném lỗi: nó phải chạy được từ handler tín hiệu, từ
        ``finally``, và từ trong chính vòng điều khiển đang lỗi.
        """
        self.state = ControlState()
        self._touch()
        self.armed = False
        self._zeros_left = 0
        self._last_sent = (0.0, 0.0)
        self.stats.stops += 1
        self.stats.last_stop_reason = reason
        self._emit_stop_frames()
        log.warning("STOP (%s) — tốc độ 0 ×%d, disarm", reason, ZERO_REPEATS)

    def _emit_stop_frames(self) -> None:
        """Khung dựng sẵn, hàng ưu tiên, gửi nhiều lần. Không tra registry lúc này."""
        for _ in range(ZERO_REPEATS):
            for frame in STOP_FRAMES:
                with contextlib.suppress(Exception):
                    self.link.send_frame(frame, priority=True)
                    self.stats.packets += 1

    # ------------------------------------------------------------------ lệnh

    def set_speed(self, yaw: float, pitch: float) -> ControlState:
        """Đặt tốc độ mong muốn. Ném :class:`NotArmed` nếu phiên chưa ARM."""
        if not self.armed:
            self.stats.rejected += 1
            raise NotArmed(
                "gimbal chưa ARM. Gọi arm trước, và kiểm tra không gian quanh "
                "gimbal đã trống — dây cáp có thể bị quấn."
            )
        self.state = ControlState(
            yaw=self._clamp(yaw), pitch=self._clamp(pitch),
        )
        return self.state

    def heartbeat(self) -> None:
        """Làm mới watchdog mà không đổi trạng thái.

        Cần thiết vì trình duyệt chỉ gửi khi trạng thái *đổi*: giữ phím 3 giây là
        3 giây không có message nào, và watchdog 500 ms sẽ cắt oan. Client gửi
        nhịp tim trong lúc còn chuyển động.
        """
        self._touch()

    def _touch(self) -> None:
        self.state.updated_at = time.monotonic()

    def _clamp(self, value: float) -> float:
        value = max(-self.max_speed, min(self.max_speed, float(value)))
        return clamp_speed(value)

    def set_max_speed(self, value: float) -> float:
        self.max_speed = max(0.5, min(float(value), MAX_SPEED_DPS))
        self.state = ControlState(
            yaw=self._clamp(self.state.yaw), pitch=self._clamp(self.state.pitch)
        )
        return self.max_speed

    # ------------------------------------------------------------ giới hạn mềm

    def _apply_soft_limits(self, yaw: float, pitch: float) -> tuple[float, float]:
        """Chặn trước biên cơ khí, chỉ khi tư thế còn tươi.

        Tư thế cũ thì **không** chặn: chặn dựa trên số đo quá hạn còn nguy hiểm
        hơn không chặn, vì nó cho cảm giác an toàn giả.
        """
        tel = self.telemetry
        if tel is None or not tel.fresh or tel.attitude is None:
            return yaw, pitch

        att = tel.attitude
        limited = False
        if pitch and abs(att.pitch) >= self.soft_limit:
            if (pitch > 0) == (att.pitch > 0):
                pitch, limited = 0.0, True
        if yaw and abs(att.yaw) >= self.soft_limit:
            if (yaw > 0) == (att.yaw > 0):
                yaw, limited = 0.0, True
        if limited:
            self.stats.limit_trips += 1
            log.warning("giới hạn mềm: yaw=%.1f pitch=%.1f đã chạm ±%.0f°",
                        att.yaw, att.pitch, self.soft_limit)
        return yaw, pitch

    # -------------------------------------------------------------- vòng lặp

    async def _loop(self) -> None:
        next_at = time.monotonic()
        try:
            while True:
                next_at += self.tick
                delay = next_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_at = time.monotonic()
                self._step()
        except asyncio.CancelledError:
            self._emit_stop_frames()
            raise
        except Exception:
            # Ngả thứ năm: exception thoát ra khỏi vòng cũng phải dừng gimbal.
            log.exception("vòng điều khiển lỗi — dừng gimbal")
            self.stop_all("lỗi vòng điều khiển")
            raise

    def _step(self) -> None:
        self.stats.ticks += 1
        now = time.monotonic()

        if not self.armed:
            self._flush_zeros()
            return

        if now - self.state.updated_at > self.watchdog:
            self.stats.watchdog_trips += 1
            self.stop_all("watchdog: không có cập nhật trong %.0f ms"
                          % (self.watchdog * 1000))
            return

        yaw, pitch = self._apply_soft_limits(self.state.yaw, self.state.pitch)

        if yaw or pitch:
            self._send_speed(yaw, pitch)
            self._last_sent = (yaw, pitch)
            self._zeros_left = ZERO_REPEATS
            return

        self._flush_zeros()

    def _flush_zeros(self) -> None:
        """Sau khi về 0, gửi thêm vài gói 0 rồi im — không spam khi đứng yên."""
        if self._last_sent not in (None, (0.0, 0.0)) and self._zeros_left <= 0:
            self._zeros_left = ZERO_REPEATS
        if self._zeros_left > 0:
            self._send_speed(0.0, 0.0)
            self._zeros_left -= 1
            self._last_sent = (0.0, 0.0)

    def _send_speed(self, yaw: float, pitch: float) -> None:
        if self.use_gsm:
            self.link.send("gimbal.speed", yaw, pitch, priority=True)
            self.stats.packets += 1
        else:
            self.link.send("gimbal.yaw_speed", yaw, priority=True)
            self.link.send("gimbal.pitch_speed", pitch, priority=True)
            self.stats.packets += 2

    # ------------------------------------------------------------- trạng thái

    def as_dict(self) -> dict:
        return {
            "armed": self.armed,
            "state": self.state.as_dict(),
            "max_speed": self.max_speed,
            "speed_ceiling": MAX_SPEED_DPS,
            "tick_hz": round(1 / self.tick, 1),
            "watchdog_ms": round(self.watchdog * 1000),
            "use_gsm": self.use_gsm,
            "soft_limit_deg": self.soft_limit,
            "running": self._task is not None and not self._task.done(),
            "stats": self.stats.as_dict(),
            "telemetry": self.telemetry.as_dict() if self.telemetry else None,
        }
