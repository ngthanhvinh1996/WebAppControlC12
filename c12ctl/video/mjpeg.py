"""MJPEG bridge: frame → JPEG → ``multipart/x-mixed-replace``.

Two decisions shape this module:

**Encode once for all clients.** The encoder is a single task; clients only read
already-encoded ``bytes``. Encoding per connection multiplies CPU cost by the
number of viewers and buys nothing.

**Only encode while someone is watching.** With no open stream the encoder task
sleeps. On the Rubik Pi 3, encoding 720p30 for a tab nobody is looking at is real
waste.

MJPEG was chosen for phase 2 because it **certainly works in every browser**,
while H.265 over WebRTC depends on the browser version and on hardware decoding.
Measure first, optimize later — see PLAN_WEBAPP_C12.md §4.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import colormap as cmap
from .bus import Average, Rate
from .source import VideoSource

log = logging.getLogger("c12ctl.video")

BOUNDARY = "c12frame"
CONTENT_TYPE = "multipart/x-mixed-replace; boundary=%s" % BOUNDARY


@dataclass
class StreamStats:
    clients: int = 0
    encoded: int = 0
    skipped: int = 0
    bytes_out: int = 0
    encode_ms: Average = field(default_factory=Average)
    latency_ms: Average = field(default_factory=Average)
    out_fps: Rate = field(default_factory=Rate)
    jpeg_kb: float = 0.0

    def as_dict(self) -> dict:
        return {
            "clients": self.clients,
            "encoded": self.encoded,
            "skipped": self.skipped,
            "mb_out": round(self.bytes_out / 1e6, 2),
            "encode_ms": round(self.encode_ms.value, 1),
            "latency_ms": round(self.latency_ms.value, 1),
            "out_fps": round(self.out_fps.value, 1),
            "jpeg_kb": round(self.jpeg_kb, 1),
        }


class MjpegStream:
    """One MJPEG stream built on top of a :class:`VideoSource`."""

    def __init__(
        self,
        source: VideoSource,
        *,
        quality: int = 80,
        max_fps: float | None = None,
        colormap: str | None = None,
        scale: float = 1.0,
        label: str = "",
    ) -> None:
        self.source = source
        self.quality = quality
        self.max_fps = max_fps
        self.colormap = colormap
        self.scale = scale
        self.label = label or source.name
        self.stats = StreamStats()

        self._jpeg: bytes | None = None
        self._jpeg_seq = 0
        self._encoder: asyncio.Task | None = None
        self._waiters: list[asyncio.Future] = []
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.source.name

    # -------------------------------------------------------------- encoder

    async def _encode_loop(self) -> None:
        last_seq = 0
        min_period = 1.0 / self.max_fps if self.max_fps else 0.0
        last_emit = 0.0
        try:
            while True:
                frame = await self.source.bus.next_frame(after_seq=last_seq,
                                                         timeout=2.0)
                if frame is None:
                    continue
                self.stats.skipped += max(0, frame.seq - last_seq - 1)
                last_seq = frame.seq

                now = time.monotonic()
                if min_period and (now - last_emit) < min_period:
                    continue
                last_emit = now

                started = time.monotonic()
                # Encoding is pure CPU work and holds the GIL — push it to a
                # thread so it cannot block the asyncio loop that runs the gimbal
                # watchdog.
                payload = await asyncio.to_thread(self._render, frame.image)
                elapsed = (time.monotonic() - started) * 1000

                self.stats.encoded += 1
                self.stats.encode_ms.add(elapsed)
                self.stats.latency_ms.add(frame.age_ms)
                self.stats.out_fps.tick()
                self.stats.jpeg_kb = len(payload) / 1024

                self._jpeg = payload
                self._jpeg_seq += 1
                waiters, self._waiters = self._waiters, []
                for fut in waiters:
                    if not fut.done():
                        fut.set_result((payload, self._jpeg_seq))
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - never let the encoder die silently
            log.exception("encoder %s died", self.name)
            raise

    def _render(self, image: np.ndarray) -> bytes:
        """Runs on a thread: colormap → scale → JPEG."""
        if self.colormap:
            image = cmap.apply(image, self.colormap)
        elif image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)

        if self.scale and self.scale != 1.0:
            h, w = image.shape[:2]
            image = cmap.upscale(image, int(w * self.scale), int(h * self.scale))

        ok, buf = cv2.imencode(".jpg", image,
                               [cv2.IMWRITE_JPEG_QUALITY, int(self.quality)])
        if not ok:  # pragma: no cover - imencode failing would be very strange
            raise RuntimeError("cv2.imencode failed")
        return buf.tobytes()

    # --------------------------------------------------------------- client

    async def _acquire(self) -> None:
        async with self._lock:
            self.stats.clients += 1
            if self._encoder is None or self._encoder.done():
                self._encoder = asyncio.create_task(
                    self._encode_loop(), name="mjpeg-%s" % self.name
                )
                log.info("stream %s: encoder started (first client)", self.name)

    async def _release(self) -> None:
        async with self._lock:
            self.stats.clients = max(0, self.stats.clients - 1)
            if self.stats.clients == 0 and self._encoder is not None:
                self._encoder.cancel()
                self._encoder = None
                self.stats.out_fps.reset()
                log.info("stream %s: encoder asleep (no clients left)", self.name)

    @contextlib.asynccontextmanager
    async def viewer(self):
        """Count as a viewer for as long as the block runs.

        Non-HTTP consumers — the session recorder — need this: the encoder only
        runs while someone is watching, so a recorder that merely called
        :meth:`next_jpeg` would wait forever whenever no browser had the stream
        open. Holding a viewer also means recording costs **zero extra encoding**
        when somebody is already watching: one encode still serves everyone.
        """
        await self._acquire()
        try:
            yield self
        finally:
            await self._release()

    async def next_jpeg(self, after: int = 0,
                        timeout: float = 5.0) -> tuple[bytes, int] | None:
        if self._jpeg is not None and self._jpeg_seq > after:
            return self._jpeg, self._jpeg_seq
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters.append(fut)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            if fut in self._waiters:
                self._waiters.remove(fut)
            return None
        except asyncio.CancelledError:
            if fut in self._waiters:
                self._waiters.remove(fut)
            raise

    async def frames(self):
        """Yield multipart parts. Used by ``StreamingResponse``."""
        await self._acquire()
        seq = 0
        try:
            while True:
                got = await self.next_jpeg(after=seq)
                if got is None:
                    continue
                payload, seq = got
                self.stats.bytes_out += len(payload)
                yield (
                    b"--" + BOUNDARY.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n"
                    + payload + b"\r\n"
                )
        finally:
            # Runs even when the client closes the tab mid-stream — otherwise the
            # encoder would keep running for a viewer that no longer exists.
            await self._release()

    async def snapshot(self, timeout: float = 3.0) -> bytes | None:
        """A single JPEG frame. Does not need a running encoder."""
        frame = self.source.bus.latest
        if frame is None:
            frame = await self.source.bus.next_frame(timeout=timeout)
        if frame is None:
            return None
        return await asyncio.to_thread(self._render, frame.image)

    async def close(self) -> None:
        async with self._lock:
            if self._encoder is not None:
                self._encoder.cancel()
                self._encoder = None
        for fut in self._waiters:
            if not fut.done():
                fut.cancel()
        self._waiters.clear()

    def describe(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "quality": self.quality,
            "max_fps": self.max_fps,
            "colormap": self.colormap,
            "scale": self.scale,
            "source": self.source.describe(),
            "source_stats": self.source.stats.as_dict(),
            "bus": self.source.bus.stats.as_dict(),
            "stream_stats": self.stats.as_dict(),
        }
