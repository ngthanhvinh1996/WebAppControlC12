"""A "latest frame" slot connecting the capture thread to the asyncio loop.

The most important rule in the whole video layer: **no queue**. A queue
accumulates frames, and accumulated frames are accumulated latency — and latency
is the only thing that matters in a control application. If the consumer is
slower than the source, old frames are **dropped** rather than queued: someone
flying a gimbal needs the *latest* frame, not every frame.

The source runs on a thread (``cv2.VideoCapture.read`` is blocking), the consumer
runs on asyncio. Hand-off happens through ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Frame:
    """One image frame plus the moment it was captured."""

    image: np.ndarray
    seq: int
    captured_at: float
    """``time.monotonic()`` when the frame left the decoder — the reference point
    for measuring pipeline latency."""

    @property
    def shape(self) -> tuple[int, ...]:
        return self.image.shape

    @property
    def age_ms(self) -> float:
        return (time.monotonic() - self.captured_at) * 1000


class Rate:
    """Rolling average rate counter. Cheap, allocation-free."""

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
    """Rolling average for quantities measured in milliseconds."""

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
    """A slot holding exactly one frame — a new frame overwrites the old one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: Frame | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: list[asyncio.Future] = []
        self.stats = BusStats()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach the loop to notify. Call from the asyncio side before the
        source starts."""
        self._loop = loop

    # ------------------------------------------------------------ thread side

    def publish(self, image: np.ndarray, captured_at: float | None = None) -> Frame:
        """Publish a new frame. Called from the capture thread."""
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

    # ----------------------------------------------------------- asyncio side

    @property
    def latest(self) -> Frame | None:
        with self._lock:
            return self._latest

    async def next_frame(self, after_seq: int = 0,
                         timeout: float | None = None) -> Frame | None:
        """Wait for a frame newer than ``after_seq``.

        Returns immediately if a newer frame already exists — a slow consumer
        **skips ahead** to the current frame instead of walking through every
        frame it missed. Returns ``None`` on timeout.
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
