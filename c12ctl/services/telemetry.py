"""Real-time gimbal attitude via ``GAA`` → ``GAC``.

``skydroid-c12-protocol.md`` concludes "no telemetry" and marks it
``[VERIFIED]``. That is a **false negative**: the push stream is off by default
and nobody had sent ``GAA`` to turn it on. The RCSDK bytecode shows ``GAA``
enables a 0–100 Hz push, after which the camera sends ``GAC`` frames carrying
yaw/pitch/roll.

This is what makes closed-loop control possible: an attitude HUD, soft limits
based on the real angle, ``goto`` verified against a measurement, and position
presets you can trust.

Vendor constraint: ``GAA`` **only takes effect once the camera is producing
video**. So keep sending it until the first ``GAC`` arrives; do not send it once
and conclude the feature is missing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Callable

from ..protocol.codec import Frame
from ..protocol.types import Attitude
from ..transport.udp_link import UdpLink

log = logging.getLogger("c12ctl.telemetry")

ATTITUDE_CMD = "GAC"
DEFAULT_RATE_HZ = 10
REARM_INTERVAL = 1.0
"""How often to resend GAA while no attitude frame has been seen."""

STALE_AFTER = 1.0
"""With no GAC for this long the attitude is stale and no longer used to gate."""


class TelemetryService:
    """Enable the attitude push stream and hold the latest value."""

    def __init__(self, link: UdpLink, *, rate_hz: int = DEFAULT_RATE_HZ,
                 rearm_interval: float = REARM_INTERVAL,
                 stale_after: float = STALE_AFTER) -> None:
        self.link = link
        self.rate_hz = rate_hz
        self.rearm_interval = rearm_interval
        self.stale_after = stale_after

        self.attitude: Attitude | None = None
        self.updated_at: float = 0.0
        self.packets = 0
        self.enabled = False
        self._arm_task: asyncio.Task | None = None
        self._subscribers: list[Callable[[Attitude], None]] = []

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self.link.subscribe(self._on_frame)
        self._arm_task = asyncio.create_task(self._arm_loop(), name="telemetry-arm")

    async def close(self) -> None:
        self.link.unsubscribe(self._on_frame)
        if self._arm_task is not None:
            self._arm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._arm_task
            self._arm_task = None
        if self.enabled:
            with contextlib.suppress(Exception):
                self.link.send("telemetry.push_attitude", 0)
            self.enabled = False

    async def _arm_loop(self) -> None:
        """Send ``GAA`` until attitude appears, then resend if the stream drops."""
        while True:
            if not self.fresh:
                self.link.send("telemetry.push_attitude", self.rate_hz)
                if not self.enabled:
                    log.info("sent GAA %d Hz (the camera must already have video)",
                             self.rate_hz)
            await asyncio.sleep(self.rearm_interval)

    # --------------------------------------------------------------- receiving

    def _on_frame(self, frame: Frame) -> None:
        if frame.cmd3 != ATTITUDE_CMD:
            return
        try:
            attitude = Attitude.from_data(frame.data)
        except ValueError:
            log.warning("malformed GAC frame: %r", frame.data)
            return

        first = not self.enabled
        self.attitude = attitude
        self.updated_at = time.monotonic()
        self.packets += 1
        self.enabled = True
        if first:
            log.info("attitude acquired: %s", attitude)

        for callback in list(self._subscribers):
            try:
                callback(attitude)
            except Exception:  # pragma: no cover
                log.exception("telemetry subscriber failed")

    def subscribe(self, callback: Callable[[Attitude], None]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(callback)

    # ------------------------------------------------------------------- state

    @property
    def age(self) -> float | None:
        """Seconds since the last attitude frame. ``None`` if none has arrived."""
        if not self.updated_at:
            return None
        return time.monotonic() - self.updated_at

    @property
    def fresh(self) -> bool:
        """Is the attitude still usable for safety decisions?"""
        age = self.age
        return age is not None and age < self.stale_after

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "fresh": self.fresh,
            "packets": self.packets,
            "rate_hz": self.rate_hz,
            "age_ms": round(self.age * 1000, 1) if self.age is not None else None,
            "attitude": self.attitude.as_dict() if self.attitude else None,
        }
