"""Video sources: synthetic (no camera needed) and live capture (RTSP).

Both sit behind the same interface and publish into the same :class:`FrameBus`,
so every layer above — colormap, MJPEG, metrics — neither knows nor needs to know
where a frame came from. That is what made it possible to finish all of phase 2
before any hardware existed.

On H.265 decoding: this dev machine is **missing** ``avdec_h265``, ``h265parse``
and ``x265enc`` (``gst-plugins-bad``/``gst-libav`` are not installed). But ``cv2``
is built with both FFMPEG (libavcodec 58) and GStreamer, so :class:`CaptureSource`
still decodes RTSP H.265 through the FFMPEG path with nothing extra installed. On
the Rubik Pi 3, pass a GStreamer pipeline containing ``v4l2h265dec`` to get
hardware decoding — same class, different URI string.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from .bus import Average, FrameBus

log = logging.getLogger("c12ctl.video")


@dataclass
class SourceStats:
    started_at: float = 0.0
    frames: int = 0
    errors: int = 0
    reconnects: int = 0
    read_ms: Average = field(default_factory=Average)
    running: bool = False
    last_error: str = ""

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "errors": self.errors,
            "reconnects": self.reconnects,
            "read_ms": round(self.read_ms.value, 1),
            "running": self.running,
            "last_error": self.last_error,
            "uptime_s": round(time.monotonic() - self.started_at, 1)
            if self.started_at else 0.0,
        }


class VideoSource:
    """A source running on its own thread, publishing into a :class:`FrameBus`."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.bus = FrameBus()
        self.stats = SourceStats()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---------------------------------------------------------------- lifecycle

    def start(self, loop=None) -> None:
        if self._thread is not None:
            return
        if loop is not None:
            self.bus.bind_loop(loop)
        self._stop.clear()
        self.stats.started_at = time.monotonic()
        self.stats.running = True
        self._thread = threading.Thread(
            target=self._run_guarded, name="video-%s" % self.name, daemon=True
        )
        self._thread.start()
        log.info("source %s: running", self.name)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
        self.stats.running = False
        self.bus.close()
        log.info("source %s: stopped after %d frames", self.name, self.stats.frames)

    def _run_guarded(self) -> None:
        try:
            self._run()
        except Exception as exc:  # pragma: no cover - the thread must not die silently
            log.exception("source %s died: %s", self.name, exc)
            self.stats.last_error = str(exc)
        finally:
            self.stats.running = False

    def _run(self) -> None:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "kind": type(self).__name__}


# --------------------------------------------------------------------------
# Synthetic source
# --------------------------------------------------------------------------


class SyntheticSource(VideoSource):
    """Generates frames in-process — no camera, no GStreamer needed.

    The content is deliberately built so that **latency can be measured by eye**:
    it carries a clock, a frame counter, and a sweeping vertical bar. Comparing
    the bar in the browser against the bar at the source shows the end-to-end
    latency with no instruments at all.

    :param kind: ``"visible"`` for a 3-channel color image, ``"thermal"`` for a
        1-channel grayscale one — matching the real C12: the thermal stream is
        already 8-bit compressed, the radiometric data is long gone.
    """

    def __init__(self, name: str, width: int, height: int, fps: float,
                 kind: str = "visible") -> None:
        super().__init__(name)
        self.width = width
        self.height = height
        self.fps = fps
        self.kind = kind
        self._background = (
            self._make_bars(width, height) if kind == "visible"
            else self._make_thermal_bg(width, height)
        )

    # ------------------------------------------------------------------ content

    @staticmethod
    def _make_bars(w: int, h: int) -> np.ndarray:
        """SMPTE-style color bars, computed once."""
        colours = [
            (192, 192, 192), (0, 192, 192), (192, 192, 0), (0, 192, 0),
            (192, 0, 192), (0, 0, 192), (192, 0, 0), (32, 32, 32),
        ]
        img = np.zeros((h, w, 3), np.uint8)
        step = max(1, w // len(colours))
        for i, colour in enumerate(colours):
            img[:, i * step:(i + 1) * step] = colour
        img[int(h * 0.72):] = 24
        return img

    @staticmethod
    def _make_thermal_bg(w: int, h: int) -> np.ndarray:
        """A gently graded gray background, so the colormap has something to work on."""
        ramp = np.linspace(40, 90, w, dtype=np.float32)[None, :]
        vign = np.linspace(-12, 12, h, dtype=np.float32)[:, None]
        return np.clip(ramp + vign, 0, 255).astype(np.uint8)

    def _render(self, n: int) -> np.ndarray:
        img = self._background.copy()
        t = time.time()
        phase = (n / max(self.fps, 1)) % 1.0

        if self.kind == "thermal":
            # A few moving hot spots — the colormap will make them stand out.
            yy, xx = np.mgrid[0:self.height, 0:self.width].astype(np.float32)
            heat = np.zeros((self.height, self.width), np.float32)
            for k, (rx, ry, amp, sigma) in enumerate(
                [(0.30, 0.40, 150, 26), (0.65, 0.60, 120, 18), (0.50, 0.28, 90, 12)]
            ):
                ang = 2 * np.pi * (phase + k / 3)
                cx = (rx + 0.16 * np.cos(ang)) * self.width
                cy = (ry + 0.16 * np.sin(ang)) * self.height
                heat += amp * np.exp(
                    -(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2))
                )
            img = np.clip(img.astype(np.float32) + heat, 0, 255).astype(np.uint8)
            bar, fg = int(self.width * phase), 255
        else:
            bar, fg = int(self.width * phase), (255, 255, 255)

        # The sweep bar — the visual reference for comparing latency.
        cv2.line(img, (bar, 0), (bar, self.height), fg, 2)

        scale = max(0.4, self.width / 1600)
        cv2.putText(img, "%s  #%06d" % (self.name, n), (10, int(28 * scale) + 6),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, fg, max(1, int(2 * scale)))
        cv2.putText(img, time.strftime("%H:%M:%S", time.localtime(t))
                    + ".%03d" % int(t % 1 * 1000),
                    (10, int(60 * scale) + 12),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, fg, max(1, int(2 * scale)))
        return img

    # --------------------------------------------------------------------- run

    def _run(self) -> None:
        period = 1.0 / self.fps
        next_at = time.monotonic()
        n = 0
        while not self._stop.is_set():
            started = time.monotonic()
            image = self._render(n)
            self.bus.publish(image)
            self.stats.frames = n = n + 1
            self.stats.read_ms.add((time.monotonic() - started) * 1000)

            next_at += period
            delay = next_at - time.monotonic()
            if delay < -period:          # too far behind: skip ahead, do not pile up
                next_at = time.monotonic()
            elif delay > 0:
                self._stop.wait(delay)

    def describe(self) -> dict:
        return {**super().describe(), "kind_detail": self.kind,
                "width": self.width, "height": self.height, "fps": self.fps,
                "uri": "synthetic:%s" % self.kind}


# --------------------------------------------------------------------------
# Live source
# --------------------------------------------------------------------------


class CaptureSource(VideoSource):
    """Capture through ``cv2.VideoCapture`` — RTSP via FFMPEG, or a GStreamer pipeline.

    :param uri: an RTSP URL (uses the FFMPEG backend) or a GStreamer pipeline
        string ending in ``appsink``. Detected by the presence of ``appsink``.
    :param grayscale: convert to a single channel after decoding — right for the
        thermal stream.
    """

    RECONNECT_DELAY = 2.0
    MAX_READ_FAILURES = 30

    def __init__(self, name: str, uri: str, *, grayscale: bool = False,
                 reconnect: bool = True) -> None:
        super().__init__(name)
        self.uri = uri
        self.grayscale = grayscale
        self.reconnect = reconnect

    @property
    def uses_gstreamer(self) -> bool:
        return "appsink" in self.uri

    def _open(self) -> cv2.VideoCapture:
        if self.uses_gstreamer:
            cap = cv2.VideoCapture(self.uri, cv2.CAP_GSTREAMER)
        else:
            cap = cv2.VideoCapture(self.uri, cv2.CAP_FFMPEG)
            # One-frame buffer: without this, latency grows until it is useless.
            # The equivalent of appsink's drop=true max-buffers=1.
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except cv2.error:  # pragma: no cover - backend does not support it
                pass
        return cap

    def _run(self) -> None:
        cap: cv2.VideoCapture | None = None
        failures = 0
        try:
            while not self._stop.is_set():
                if cap is None or not cap.isOpened():
                    if cap is not None:
                        cap.release()
                        self.stats.reconnects += 1
                    log.info("source %s: opening %s", self.name, self.uri)
                    cap = self._open()
                    if not cap.isOpened():
                        self.stats.errors += 1
                        self.stats.last_error = "could not open the source"
                        if not self.reconnect:
                            return
                        self._stop.wait(self.RECONNECT_DELAY)
                        continue
                    failures = 0

                started = time.monotonic()
                ok, image = cap.read()
                if not ok or image is None:
                    failures += 1
                    self.stats.errors += 1
                    self.stats.last_error = "read() returned nothing"
                    if failures >= self.MAX_READ_FAILURES:
                        log.warning("source %s: %d failed reads, reopening",
                                    self.name, failures)
                        cap.release()
                        cap = None
                        if not self.reconnect:
                            return
                    continue

                failures = 0
                if self.grayscale and image.ndim == 3:
                    image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                self.bus.publish(image)
                self.stats.frames += 1
                self.stats.read_ms.add((time.monotonic() - started) * 1000)
        finally:
            if cap is not None:
                cap.release()

    def describe(self) -> dict:
        return {**super().describe(), "uri": self.uri,
                "backend": "gstreamer" if self.uses_gstreamer else "ffmpeg",
                "grayscale": self.grayscale}


# --------------------------------------------------------------------------
# Reference pipeline
# --------------------------------------------------------------------------

def gst_pipeline(uri: str, decoder: str = "avdec_h265", latency: int = 200) -> str:
    """GStreamer pipeline for RTSP H.265 → BGR appsink.

    ``drop=true max-buffers=1`` is **not a minor detail**: without it the appsink
    accumulates frames and latency grows until the stream is unusable.

    The decoder is a parameter rather than hard-coded — the same codebase runs on
    the dev machine (``avdec_h265``, software) and on the Rubik Pi 3 / QCS6490
    (``v4l2h265dec``, Qualcomm's hardware decoder).
    """
    return (
        "rtspsrc location=%s latency=%d protocols=tcp "
        "! rtph265depay ! h265parse ! %s "
        "! videoconvert ! video/x-raw,format=BGR "
        "! appsink drop=true max-buffers=1 sync=false"
        % (uri, latency, decoder)
    )
