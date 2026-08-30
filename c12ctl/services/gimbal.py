"""Gimbal control loop — 20 Hz, watchdog, and five paths to an emergency stop.

The browser does **not** tick at 20 Hz. It only reports when state *changes*
(key down/up, stick moved); the cadence lives here, where latency is steady and
does not depend on whether the tab is being painted.

Why we resend at 20 Hz even though it may be redundant: the two source documents
disagree on whether the gimbal stops on its own when packets stop arriving, and
the documents cannot settle it. **A 20 Hz keepalive is a superset of both
behaviours** — if the gimbal stops on its own, the keepalive keeps it moving; if
it holds the last command, the keepalive is harmless and the watchdog is still
the safety layer we need. :class:`~c12ctl.sim.c12_sim.C12Simulator` can run
either way and both are covered by tests.

Every path to a stop goes through exactly one function:
:meth:`GimbalController.stop_all`.
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
"""Control loop period, in seconds. 20 Hz."""

WATCHDOG = 0.5
"""No update within this window and we stop on our own."""

ZERO_REPEATS = 3
"""How many times to send zero speed when stopping. UDP packets can be lost —
once is not enough."""

DEFAULT_MAX_SPEED = 10.0
"""°/s. Deliberately low for the first run; raise only after seeing the stop
path work."""

SOFT_LIMIT_DEG = 85.0
"""Stop short of the ±90° mechanical limit when a real attitude is available."""


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
    """A motion command while the session is not armed."""


class GimbalController:
    """Keeps the 20 Hz cadence, enforces the limits, and stops on any doubt."""

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

        # GSM packs yaw and pitch into one packet, halving the traffic at 20 Hz —
        # but it needs gimbal firmware >= 0.5. It CANNOT be probed: GSM is a
        # write command with no reply, so the only way to know whether it works
        # is to command real motion and watch whether the gimbal moves. Probing
        # by making the gimbal turn is the wrong trade. Default to two separate
        # packets — those always work.
        self.use_gsm = use_gsm

        self.armed = False
        self.state = ControlState()
        self.stats = GimbalStats()
        self._task: asyncio.Task | None = None
        self._last_sent: tuple[float, float] | None = None
        self._zeros_left = 0

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="gimbal-control")
        log.info("control loop running: %.0f Hz, ceiling %.1f °/s, watchdog %.0f ms%s",
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
                # The loop already died on an error and stopped the gimbal there.
                # close() is the cleanup path — it must never raise on top of
                # that, or the cleanup that follows will not run.
                log.debug("control loop ended with an error", exc_info=True)
        # Path five: process exit must also leave the gimbal still.
        self._emit_stop_frames()
        self.armed = False

    # --------------------------------------------------------------- arm / stop

    def arm(self) -> None:
        self.armed = True
        self.state = ControlState()
        self._touch()
        log.warning("ARM — motion commands are now open")

    def stop_all(self, reason: str = "stop requested") -> None:
        """The single emergency-stop path. Every other route calls into this.

        Synchronous and never raises: it has to run from a signal handler, from
        a ``finally``, and from inside the control loop that is failing.
        """
        self.state = ControlState()
        self._touch()
        self.armed = False
        self._zeros_left = 0
        self._last_sent = (0.0, 0.0)
        self.stats.stops += 1
        self.stats.last_stop_reason = reason
        self._emit_stop_frames()
        log.warning("STOP (%s) — zero speed ×%d, disarmed", reason, ZERO_REPEATS)

    def _emit_stop_frames(self) -> None:
        """Prebuilt frames, priority queue, sent repeatedly. No registry lookup here."""
        for _ in range(ZERO_REPEATS):
            for frame in STOP_FRAMES:
                with contextlib.suppress(Exception):
                    self.link.send_frame(frame, priority=True)
                    self.stats.packets += 1

    # ---------------------------------------------------------------- commands

    def set_speed(self, yaw: float, pitch: float) -> ControlState:
        """Set the desired speed. Raises :class:`NotArmed` if the session is not armed."""
        if not self.armed:
            self.stats.rejected += 1
            raise NotArmed(
                "the gimbal is not armed. ARM first, and check that the space "
                "around the gimbal is clear — cables can get wound up."
            )
        self.state = ControlState(
            yaw=self._clamp(yaw), pitch=self._clamp(pitch),
        )
        return self.state

    def heartbeat(self) -> None:
        """Refresh the watchdog without changing state.

        Needed because the browser only sends on *change*: holding a key for 3
        seconds is 3 seconds with no message, and the 500 ms watchdog would cut
        in wrongly. The client sends a heartbeat while motion continues.
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

    # -------------------------------------------------------------- soft limits

    def _apply_soft_limits(self, yaw: float, pitch: float) -> tuple[float, float]:
        """Stop short of the mechanical limit, but only while the attitude is fresh.

        A stale attitude is **not** used to gate: gating on an expired reading is
        more dangerous than not gating at all, because it creates a false sense
        of safety.
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
            log.warning("soft limit: yaw=%.1f pitch=%.1f reached ±%.0f°",
                        att.yaw, att.pitch, self.soft_limit)
        return yaw, pitch

    # -------------------------------------------------------------------- loop

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
            # Path five: an exception escaping the loop must stop the gimbal too.
            log.exception("control loop failed — stopping the gimbal")
            self.stop_all("control loop error")
            raise

    def _step(self) -> None:
        self.stats.ticks += 1
        now = time.monotonic()

        if not self.armed:
            self._flush_zeros()
            return

        if now - self.state.updated_at > self.watchdog:
            self.stats.watchdog_trips += 1
            self.stop_all("watchdog: no update within %.0f ms"
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
        """After returning to zero, send a few more zero packets, then go quiet —
        no spamming while standing still."""
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

    # ------------------------------------------------------------------- state

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
