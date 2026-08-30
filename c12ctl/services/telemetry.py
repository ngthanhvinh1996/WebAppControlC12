"""Tư thế gimbal thời gian thực qua ``GAA`` → ``GAC``.

``skydroid-c12-protocol.md`` kết luận "không có telemetry" và gắn nhãn
``[VERIFIED]``. Đó là **âm tính giả**: luồng đẩy mặc định tắt, và chưa ai gửi
``GAA`` để bật. Bytecode RCSDK cho thấy ``GAA`` bật push 0–100 Hz, sau đó camera
tự đẩy gói ``GAC`` chứa yaw/pitch/roll.

Đây là thứ mở ra điều khiển vòng kín: HUD tư thế, giới hạn mềm theo góc thật,
xác minh ``goto`` bằng số đo, preset vị trí đáng tin.

Ràng buộc của hãng: ``GAA`` **chỉ hiệu lực sau khi camera đã ra hình**. Nên gửi
lặp cho tới khi thấy gói ``GAC`` đầu tiên, đừng gửi một phát rồi kết luận là
không hỗ trợ.
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
"""Khoảng gửi lại GAA khi chưa thấy gói tư thế nào."""

STALE_AFTER = 1.0
"""Quá khoảng này không có GAC thì coi tư thế là cũ, không dùng để chặn nữa."""


class TelemetryService:
    """Bật luồng đẩy tư thế và giữ giá trị mới nhất."""

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

    # -------------------------------------------------------------- vòng đời

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
        """Gửi ``GAA`` cho tới khi thấy tư thế, rồi gửi lại nếu luồng đứt."""
        while True:
            if not self.fresh:
                self.link.send("telemetry.push_attitude", self.rate_hz)
                if not self.enabled:
                    log.info("gửi GAA %d Hz (camera phải đã ra hình mới nhận)",
                             self.rate_hz)
            await asyncio.sleep(self.rearm_interval)

    # ------------------------------------------------------------------ nhận

    def _on_frame(self, frame: Frame) -> None:
        if frame.cmd3 != ATTITUDE_CMD:
            return
        try:
            attitude = Attitude.from_data(frame.data)
        except ValueError:
            log.warning("gói GAC méo: %r", frame.data)
            return

        first = not self.enabled
        self.attitude = attitude
        self.updated_at = time.monotonic()
        self.packets += 1
        self.enabled = True
        if first:
            log.info("có tư thế: %s", attitude)

        for callback in list(self._subscribers):
            try:
                callback(attitude)
            except Exception:  # pragma: no cover
                log.exception("subscriber telemetry lỗi")

    def subscribe(self, callback: Callable[[Attitude], None]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(callback)

    # ------------------------------------------------------------------ trạng thái

    @property
    def age(self) -> float | None:
        """Giây kể từ gói tư thế cuối. ``None`` nếu chưa có gói nào."""
        if not self.updated_at:
            return None
        return time.monotonic() - self.updated_at

    @property
    def fresh(self) -> bool:
        """Tư thế còn dùng được để ra quyết định an toàn không?"""
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
