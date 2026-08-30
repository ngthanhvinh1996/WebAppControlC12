"""Nguồn video: tổng hợp (không cần camera) và bắt thật (RTSP).

Cả hai đứng sau cùng một interface và đẩy vào cùng một :class:`FrameBus`, nên mọi
tầng phía trên — colormap, MJPEG, số đo — không biết và không cần biết khung đến
từ đâu. Đó là điều cho phép làm xong toàn bộ pha 2 trước khi có phần cứng.

Về decode H.265: máy dev hiện **thiếu** ``avdec_h265``, ``h265parse``, ``x265enc``
(chưa cài ``gst-plugins-bad``/``gst-libav``). Nhưng ``cv2`` được build kèm cả
FFMPEG (libavcodec 58) lẫn GStreamer, nên :class:`CaptureSource` vẫn decode được
RTSP H.265 qua đường FFMPEG mà không cần cài thêm gì. Trên Rubik Pi 3 thì truyền
vào một pipeline GStreamer có ``v4l2h265dec`` để dùng decode phần cứng — cùng một
class, chỉ khác chuỗi URI.
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
    """Nguồn chạy trên thread riêng, đẩy khung vào một :class:`FrameBus`."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.bus = FrameBus()
        self.stats = SourceStats()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -------------------------------------------------------------- vòng đời

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
        log.info("nguồn %s: chạy", self.name)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
        self.stats.running = False
        self.bus.close()
        log.info("nguồn %s: dừng sau %d khung", self.name, self.stats.frames)

    def _run_guarded(self) -> None:
        try:
            self._run()
        except Exception as exc:  # pragma: no cover - thread không được chết lặng lẽ
            log.exception("nguồn %s chết: %s", self.name, exc)
            self.stats.last_error = str(exc)
        finally:
            self.stats.running = False

    def _run(self) -> None:  # pragma: no cover - lớp con cài đặt
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "kind": type(self).__name__}


# --------------------------------------------------------------------------
# Nguồn tổng hợp
# --------------------------------------------------------------------------


class SyntheticSource(VideoSource):
    """Sinh khung trong tiến trình — không cần camera, không cần GStreamer.

    Nội dung cố ý làm cho **đo được độ trễ bằng mắt**: có đồng hồ, số thứ tự khung,
    và một vạch quét chạy ngang. So vạch trên trình duyệt với vạch ở nguồn là thấy
    ngay độ trễ end-to-end mà không cần dụng cụ gì.

    :param kind: ``"visible"`` cho ảnh màu 3 kênh, ``"thermal"`` cho ảnh xám 1 kênh
        — đúng như C12 thật: luồng nhiệt đã bị nén 8-bit, dữ liệu radiometric mất rồi.
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

    # ----------------------------------------------------------------- nội dung

    @staticmethod
    def _make_bars(w: int, h: int) -> np.ndarray:
        """Dải màu kiểu SMPTE, tính sẵn một lần."""
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
        """Nền xám có gradient nhẹ, để colormap có cái mà tô."""
        ramp = np.linspace(40, 90, w, dtype=np.float32)[None, :]
        vign = np.linspace(-12, 12, h, dtype=np.float32)[:, None]
        return np.clip(ramp + vign, 0, 255).astype(np.uint8)

    def _render(self, n: int) -> np.ndarray:
        img = self._background.copy()
        t = time.time()
        phase = (n / max(self.fps, 1)) % 1.0

        if self.kind == "thermal":
            # Vài đốm nóng chuyển động — colormap sẽ làm chúng nổi bật.
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

        # Vạch quét — mốc để so độ trễ bằng mắt.
        cv2.line(img, (bar, 0), (bar, self.height), fg, 2)

        scale = max(0.4, self.width / 1600)
        cv2.putText(img, "%s  #%06d" % (self.name, n), (10, int(28 * scale) + 6),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, fg, max(1, int(2 * scale)))
        cv2.putText(img, time.strftime("%H:%M:%S", time.localtime(t))
                    + ".%03d" % int(t % 1 * 1000),
                    (10, int(60 * scale) + 12),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, fg, max(1, int(2 * scale)))
        return img

    # -------------------------------------------------------------------- chạy

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
            if delay < -period:          # tụt quá xa thì bỏ nhịp, đừng dồn
                next_at = time.monotonic()
            elif delay > 0:
                self._stop.wait(delay)

    def describe(self) -> dict:
        return {**super().describe(), "kind_detail": self.kind,
                "width": self.width, "height": self.height, "fps": self.fps,
                "uri": "synthetic:%s" % self.kind}


# --------------------------------------------------------------------------
# Nguồn thật
# --------------------------------------------------------------------------


class CaptureSource(VideoSource):
    """Bắt hình bằng ``cv2.VideoCapture`` — RTSP qua FFMPEG, hoặc pipeline GStreamer.

    :param uri: URL RTSP (dùng backend FFMPEG) hoặc chuỗi pipeline GStreamer kết
        thúc bằng ``appsink``. Tự nhận biết qua sự xuất hiện của ``appsink``.
    :param grayscale: chuyển sang 1 kênh sau khi decode — hợp với luồng nhiệt.
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
            # Buffer 1 khung: thiếu dòng này độ trễ tăng dần cho tới khi vô dụng.
            # Tương đương drop=true max-buffers=1 của appsink.
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except cv2.error:  # pragma: no cover - backend không hỗ trợ
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
                    log.info("nguồn %s: mở %s", self.name, self.uri)
                    cap = self._open()
                    if not cap.isOpened():
                        self.stats.errors += 1
                        self.stats.last_error = "không mở được nguồn"
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
                    self.stats.last_error = "read() trả về rỗng"
                    if failures >= self.MAX_READ_FAILURES:
                        log.warning("nguồn %s: %d lần đọc hỏng, mở lại",
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
# Pipeline tham chiếu
# --------------------------------------------------------------------------

def gst_pipeline(uri: str, decoder: str = "avdec_h265", latency: int = 200) -> str:
    """Pipeline GStreamer cho RTSP H.265 → BGR appsink.

    ``drop=true max-buffers=1`` **không phải chi tiết vụn**: thiếu nó appsink tích
    khung và độ trễ tăng dần cho tới khi không dùng được.

    Decoder chọn qua tham số, không hard-code — cùng một codebase chạy trên máy dev
    (``avdec_h265``, phần mềm) và trên Rubik Pi 3 / QCS6490 (``v4l2h265dec``, NVDEC
    tương đương của Qualcomm).
    """
    return (
        "rtspsrc location=%s latency=%d protocols=tcp "
        "! rtph265depay ! h265parse ! %s "
        "! videoconvert ! video/x-raw,format=BGR "
        "! appsink drop=true max-buffers=1 sync=false"
        % (uri, latency, decoder)
    )
