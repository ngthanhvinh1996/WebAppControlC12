"""Camera state and **verified** write commands — phase 3.

The rule for this phase fits in one sentence: *every write must be confirmable by
a corresponding read*. The reason is a property of the protocol itself — C12
write commands have **no reply**, so "sent" and "took effect" are two very
different things. The packet may be lost (UDP), the firmware may not support that
command word, or the parameter may be out of range and silently ignored. If the
UI displays what we *just sent*, it is lying to the operator in all three cases.

So :meth:`CameraService.apply` does not return "sent". It returns **what the
read-back saw**, at one of three levels:

``direct``
    A read command returns the exact value just written. ``REC``, ``IMG``,
    ``VID``, and the thermal group. This is the only level that proves the
    command took effect.

``relative``
    No absolute setter exists, only step up/down — ``DZM``. Confirmed by reading
    before, writing, reading again, and comparing the *direction* of the change.

``indirect``
    No corresponding read command at all — ``CAP`` (take a photo). The only
    indirect evidence is the SD card's free space dropping. Weak, and labelled
    as weak.

The verification result has **three** states, not two: ``ok=None`` means *could
not be verified* (the read is silent, no card inserted). Folding that into
"failed" would send the operator off to fix the wrong thing.

The state cache polls at a low rate and **drops fields that stay silent**: on
real hardware a fair number of read commands will not exist, and every probe of a
dead command is a full timeout spent waiting. Three consecutive silences and the
field is backed off to once every 30 seconds — still retried, but no longer
clogging the poll loop.

One important exception to that inference: **silence while the gimbal is turning
is not evidence**. The 20 Hz control loop owns the priority queue in
``udp_link``, so a read will sit there until it times out even though the
firmware supports it perfectly well. The poll loop rests during that time (
:class:`CameraService` takes a ``busy`` predicate), and silence while busy is not
counted toward the silent streak.
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
"""Poll loop period, in seconds. Whatever is due gets read, not everything."""

READ_TIMEOUT = 0.3
"""Shorter than the registry's 1 s default: one poll cycle touches many fields,
and an unsupported command stays silent for the whole timeout."""

SETTLE = 0.15
"""Wait before reading back. The camera needs tens of milliseconds for a command
to actually take effect."""

CONFIRM_ATTEMPTS = 3
"""How many read-backs before concluding. UDP packets can be lost — one is not
enough."""

DEAD_AFTER = 3
"""Consecutive silences before a field is treated as unsupported."""

DEAD_RETRY = 30.0
"""How often to retry a field that has been written off as dead."""

SLOW_EVERY = 10.0
"""Thermal parameters barely ever change on their own — read them rarely to save
traffic."""


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

DIRECT = "direct"
RELATIVE = "relative"
INDIRECT = "indirect"


@dataclass(frozen=True)
class Expectation:
    """What the read command must see after the write."""

    describe: str
    matches: Callable[[object], bool]

    note: str = ""
    """Always attached to the result — how to read this number, even on a match."""

    if_mismatch: str = ""
    """Attached only when it does **not** match: why that may not mean the
    command missed."""


@dataclass(frozen=True)
class Verify:
    """How to confirm one write command with one read command."""

    read: str
    """Name of the read command used to confirm."""

    kind: str
    expect: Callable[[tuple, object], Expectation]
    """``(args, value read before the write) → expectation``."""

    needs_before: bool = False
    """Whether the old value must be read first. Only ``relative``/``indirect``."""

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
        # The real zoom ceiling is unverified (the bytecode hints at 0–67). If
        # nothing changed it is quite likely we hit the ceiling rather than the
        # command missing — but since we do NOT yet know where the ceiling is,
        # we must not claim success.
        if_mismatch="no change may mean the zoom ceiling was reached — the real "
                    "range is still unverified",
    )


def _expect_zoom_out(args: tuple, before: object) -> Expectation:
    level = int(before)
    if level <= 0:
        # The floor is known to be 0, unlike the ceiling. No change here is
        # CORRECT.
        return _eq(0, note="already at the zoom floor, so no change is correct")
    return Expectation(describe="< %d" % level, matches=lambda a: int(a) < level)


def _expect_snap(args: tuple, before: object) -> Expectation:
    card: SDCardStatus = before
    if not getattr(card, "present", False):
        raise Unverifiable(
            "the SD card reports 0/0 — with no card inserted there is no "
            "evidence available for CAP"
        )
    free = card.free_mb
    return Expectation(
        describe="free_mb < %d" % free,
        matches=lambda a: getattr(a, "free_mb", free) < free,
        note="INDIRECT evidence: CAP has no corresponding read command, this is "
             "inferred purely from the card's free space dropping",
        if_mismatch="a photo smaller than the card's reporting unit may not move "
                    "free_mb — a mismatch here is weak evidence, not a verdict",
    )


class Unverifiable(RuntimeError):
    """Cannot build an expectation: missing baseline data, not a bad command."""


#: Which camera write is confirmed by which read. **This is a second allowlist**:
#: :meth:`CameraService.apply` only runs commands listed here, so a write that
#: cannot be confirmed cannot go through this path.
WRITES: dict[str, Verify] = {
    "camera.record_start": Verify(
        "read.recording", DIRECT, lambda a, b: _eq(True),
        doc="REC=01, then reading REC back must show recording in progress",
    ),
    "camera.record_stop": Verify(
        "read.recording", DIRECT, lambda a, b: _eq(False),
        doc="REC=00, then reading REC back must show recording stopped",
    ),
    "camera.palette": Verify(
        "read.palette", DIRECT, _expect_palette,
        doc="IMG is the palette — reading IMG back must return the value just set",
    ),
    "camera.resolution": Verify(
        "read.resolution", DIRECT, _expect_resolution,
        doc="Reading VID back must return the resolution just set",
    ),
    "camera.zoom_in": Verify(
        "read.zoom", RELATIVE, _expect_zoom_in, needs_before=True,
        doc="DZM only steps up/down — read DZM before and after, compare direction",
    ),
    "camera.zoom_out": Verify(
        "read.zoom", RELATIVE, _expect_zoom_out, needs_before=True,
        doc="DZM one step down; at the floor (0) no change is correct",
    ),
    "camera.snap": Verify(
        "read.sdcard", INDIRECT, _expect_snap, needs_before=True,
        doc="CAP has no corresponding read — only inferred from free space dropping",
    ),
}

for _suffix in ("spatial_nr", "shutter", "detail", "gamma",
                "brightness", "contrast", "temporal_nr"):
    WRITES["camera.thermal_" + _suffix] = Verify(
        "read.thermal_" + _suffix, DIRECT, _expect_percent,
        doc="Thermal parameter 0–100; reading back must return the clamped value",
    )
del _suffix


# --------------------------------------------------------------------------
# State cache
# --------------------------------------------------------------------------

#: ``(field name, read command, period in seconds)``. ``0`` = every poll cycle;
#: ``inf`` = read once, since the model and firmware version do not change while
#: running.
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
    """One cached camera value, with its age and its history of silence."""

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
    """The owning service's threshold, not the module constant."""

    @property
    def supported(self) -> bool | None:
        """``None`` = not enough data to conclude."""
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
    """Poll cycles skipped because the link was busy with priority traffic."""

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
    """The result of a write command *after reading it back*."""

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
    """Caches camera state and runs write commands with a read-back step."""

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
        # While the gimbal is turning, the priority queue takes nearly every send
        # slot (20 Hz × 2 packets, 15 ms apart) and reads sit until they time
        # out. Silence then says something about traffic, NOT about whether the
        # firmware supports the command — so the poll loop rests and that
        # silence is not counted as evidence.
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
        # When two callers wait on the same command word, udp_link hands the
        # frame to the first one only — the other times out and records a false
        # "silence". The poll loop and apply's read-back share this lock so they
        # never overlap.
        self._read_lock = asyncio.Lock()

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._poll_loop(), name="camera-poll")
        log.info("camera state cache: poll %.1f Hz, read timeout %.0f ms",
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
            except Exception:  # pragma: no cover - a poll failure must not kill the app
                log.exception("camera poll loop failed — retrying next cycle")
            await asyncio.sleep(self.interval)

    # ------------------------------------------------------------------ reading

    async def poll_once(self, *, force: bool = False) -> list[Field]:
        """Read whatever is due. ``force`` reads everything, dead fields included.

        Rests for a whole cycle while the link is busy with priority traffic —
        ``force`` (the operator pressing "refresh") still goes through, because
        that is an explicit request.
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
        """How long before this field is read again."""
        if f.replies == 0 and f.silent_streak >= self.dead_after:
            # Continuous silence = the C12 does not support this read. Every
            # retry costs a full timeout, so space them out instead of dropping
            # the field entirely.
            return self.dead_retry
        if math.isinf(f.every):
            # Static field: once it has answered we are done; until then, keep
            # retrying at the normal cadence.
            return math.inf if f.replies else self.interval
        return f.every

    async def _read_field(self, f: Field) -> Field:
        f.dead_after = self.dead_after      # a threshold changed at runtime must apply
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
                # Silence while the link is saturated says nothing about firmware.
                return f
            f.silent_streak += 1
            if f.silent_streak == self.dead_after:
                log.info("%s silent %d times — backing off to once every %.0f s",
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

    # ------------------------------------------------------------------ writing

    async def apply(self, action: str, *args) -> ApplyResult:
        """Send a camera write command and then **read it back to confirm**.

        :raises CommandNotAllowed: the command is not in :data:`WRITES`. A write
            that cannot be confirmed does not go through this path.
        """
        name = action if action.startswith("camera.") else "camera." + action
        spec = WRITES.get(name)
        if spec is None:
            raise CommandNotAllowed(
                "%r is not a verifiable camera write. Only commands that have a "
                "corresponding read can run here: %s"
                % (action, ", ".join(sorted(WRITES)))
            )
        cmd = reg.get(name)
        read_field = self.field_for(spec.read)
        if read_field is None:  # pragma: no cover - every WRITES entry is in POLL_SPECS
            read_field = self.fields[spec.read] = Field(
                name=spec.read, read=spec.read, every=math.inf
            )
        started = time.monotonic()

        before = None
        if spec.needs_before:
            f = await self._read_field(read_field)
            before = f.value

        # Build the expectation BEFORE sending. A bad parameter (unknown palette,
        # missing baseline) must fail here, while no packet has left the backend.
        note = ""
        try:
            expectation = spec.expect(tuple(args), before)
        except Unverifiable as exc:
            expectation = None
            note = str(exc)
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            raise ValueError("invalid parameter for %s: %s" % (name, exc)) from None

        frame = self.link.send(cmd, *args)
        self.stats.applies += 1

        result = ApplyResult(
            action=name.split(".", 1)[1], command=name, frame=frame,
            kind=spec.kind, read=spec.read,
            expected=expectation.describe if expectation else "—",
            before=before, note=note,
        )

        if expectation is None:
            # No expectation could be built (no card inserted). Still read back
            # once to return fresh state, but conclude nothing.
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
            log.warning("%s: read-back saw %r, expected %s",
                        name, result.actual, result.expected)
        else:
            self.stats.unverified += 1
            log.warning("%s: could not verify — %s",
                        name, result.note or "the read command was silent")
        return result

    async def _confirm(self, result: ApplyResult, expectation: Expectation,
                       f: Field) -> None:
        """Read back until it matches, or until attempts run out. A lost UDP
        packet is covered by the next attempt.

        The baseline is ``f.replies``, **not** ``f.value``: the cache
        deliberately keeps the old value with its age when a read goes silent,
        so comparing against ``f.value`` would turn one silence into "the camera
        still reports the old value" — an ``ok=False`` verdict drawn from data
        that was never actually read back. Exactly the kind of lie this whole
        verification mechanism exists to prevent.
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
                "%s did not answer while the gimbal was turning — the priority "
                "queue owns the send slots. Retry with the gimbal at rest." % f.read
                if self._busy() else
                "%s did not answer — there is no way to confirm this command on "
                "the current hardware" % f.read
            )
        else:
            result.ok = False

    # ------------------------------------------------------------------- state

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
