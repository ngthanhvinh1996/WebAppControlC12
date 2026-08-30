"""Ô "khung mới nhất" nối thread bắt hình với vòng lặp asyncio.

Nguyên tắc quan trọng nhất của cả tầng video: **không hàng đợi**. Queue tích khung
là tích độ trễ, mà độ trễ là thứ duy nhất đáng quan tâm ở một app điều khiển. Nếu
consumer chậm hơn source, khung cũ bị **vứt** chứ không xếp hàng — người điều khiển
gimbal cần khung *mới nhất*, không cần khung nào cũng phải xem.

Source chạy trên thread (``cv2.VideoCapture.read`` là blocking), consumer chạy trên
asyncio. Bàn giao qua ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Frame:
    """Một khung ảnh kèm thời điểm bắt được."""

    image: np.ndarray
    seq: int
    captured_at: float
    """``time.monotonic()`` lúc khung rời khỏi decoder — gốc để đo độ trễ pipeline."""

    @property
    def shape(self) -> tuple[int, ...]:
        return self.image.shape

    @property
    def age_ms(self) -> float:
        return (time.monotonic() - self.captured_at) * 1000


class Rate:
    """Đếm nhịp trung bình trượt. Rẻ, không cấp phát."""

    def __init__(self, alpha: float = 0.1) -> None:
        self.alpha = alpha
        self.value = 0.0
        self._last: float | None = None

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self._last is not None:
            dt = now - self._last
            if dt > 0:
                inst = 1.0 / dt
                self.value = inst if not self.value else (
                    self.alpha * inst + (1 - self.alpha) * self.value
                )
        self._last = now

    def reset(self) -> None:
        self.value = 0.0
        self._last = None


class Average:
    """Trung bình trượt cho các đại lượng đo bằng ms."""

    def __init__(self, alpha: float = 0.1) -> None:
        self.alpha = alpha
        self.value = 0.0

    def add(self, sample: float) -> None:
        self.value = sample if not self.value else (
            self.alpha * sample + (1 - self.alpha) * self.value
        )


@dataclass
class BusStats:
    published: int = 0
    fps: Rate = field(default_factory=Rate)

    def as_dict(self) -> dict:
        return {"published": self.published, "fps": round(self.fps.value, 1)}


class FrameBus:
    """Một ô chứa đúng một khung — khung mới đè khung cũ."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: list[asyncio.Future] = []
        self.stats = BusStats()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Gắn vòng lặp sẽ nhận thông báo. Gọi từ phía asyncio trước khi source chạy."""
        self._loop = loop

    # ------------------------------------------------------------ phía thread

    def publish(self, image: np.ndarray, captured_at: float | None = None) -> Frame:
        """Đăng một khung mới. Gọi từ thread bắt hình."""
        now = time.monotonic()
        with self._lock:
            seq = self.stats.published + 1
            frame = Frame(image=image, seq=seq,
                          captured_at=now if captured_at is None else captured_at)
            self._latest = frame
            self.stats.published = seq
            waiters, self._waiters = self._waiters, []
        self.stats.fps.tick(now)

        if waiters:
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._wake, waiters, frame)
        return frame

    @staticmethod
    def _wake(waiters: list[asyncio.Future], frame: Frame) -> None:
        for fut in waiters:
            if not fut.done():
                fut.set_result(frame)

    # ----------------------------------------------------------- phía asyncio

    @property
    def latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    async def next_frame(self, after_seq: int = 0,
                         timeout: float | None = None) -> Frame | None:
        """Chờ khung mới hơn ``after_seq``.

        Trả ngay nếu đã có khung mới hơn — consumer chậm sẽ **nhảy cóc** tới khung
        hiện tại thay vì lần lượt qua từng khung đã lỡ. Trả ``None`` khi timeout.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._latest is not None and self._latest.seq > after_seq:
                return self._latest
            if self._loop is None:
                self._loop = loop
            fut: asyncio.Future = loop.create_future()
            self._waiters.append(fut)

        try:
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            with self._lock:
                if fut in self._waiters:
                    self._waiters.remove(fut)
            return None
        except asyncio.CancelledError:
            with self._lock:
                if fut in self._waiters:
                    self._waiters.remove(fut)
            raise

    def close(self) -> None:
        with self._lock:
            waiters, self._waiters = self._waiters, []
        for fut in waiters:
            if not fut.done():
                fut.cancel()
