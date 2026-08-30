"""Hai luồng của C12, cấu hình sẵn theo thông số đo được.

|            | Khả kiến      | Nhiệt          |
|------------|---------------|----------------|
| RTSP       | `:554/stream=1` | `:555/stream=2` |
| Codec      | HEVC Main     | HEVC Main      |
| Kích thước | 1280×720      | 384×288        |
| Nhịp       | 30 fps        | 25 fps         |

Hai luồng **lệch nhịp** (30 vs 25) nên chạy hoàn toàn độc lập: mỗi luồng một thread,
một bus, một encoder. Không có chỗ nào đồng bộ khung giữa chúng — cố đồng bộ hai
nguồn lệch nhịp chỉ tạo ra giật hoặc độ trễ.

Luồng nhiệt để ``scale=2.0``: 384×288 hiển thị nguyên cỡ thì quá nhỏ, và phóng to
ở server bằng ``INTER_NEAREST`` rẻ hơn nhiều so với để trình duyệt nội suy mượt.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .mjpeg import MjpegStream
from .source import CaptureSource, SyntheticSource, VideoSource, gst_pipeline

log = logging.getLogger("c12ctl.video")

CAMERA_IP = "192.168.144.108"

VISIBLE = "visible"
THERMAL = "thermal"


@dataclass(frozen=True)
class StreamSpec:
    """Thông số một luồng, đo bằng ffprobe trên C12 thật."""

    name: str
    label: str
    rtsp_port: int
    rtsp_path: str
    width: int
    height: int
    fps: float
    grayscale: bool
    quality: int
    scale: float

    def uri(self, host: str) -> str:
        return "rtsp://%s:%d/%s" % (host, self.rtsp_port, self.rtsp_path)


SPECS: dict[str, StreamSpec] = {
    VISIBLE: StreamSpec(
        name=VISIBLE, label="Khả kiến", rtsp_port=554, rtsp_path="stream=1",
        width=1280, height=720, fps=30.0, grayscale=False, quality=80, scale=1.0,
    ),
    THERMAL: StreamSpec(
        name=THERMAL, label="Nhiệt", rtsp_port=555, rtsp_path="stream=2",
        width=384, height=288, fps=25.0, grayscale=True, quality=85, scale=2.0,
    ),
}


class VideoManager:
    """Vòng đời của cả hai luồng."""

    def __init__(self) -> None:
        self.streams: dict[str, MjpegStream] = {}
        self.sources: dict[str, VideoSource] = {}
        self._started = False

    # ---------------------------------------------------------------- dựng

    @classmethod
    def synthetic(cls, *, colormap: str | None = "ironbow",
                  max_fps: float | None = None) -> "VideoManager":
        """Hai luồng tổng hợp — không cần camera, không cần GStreamer."""
        mgr = cls()
        for spec in SPECS.values():
            source = SyntheticSource(
                spec.name, spec.width, spec.height, spec.fps,
                kind="thermal" if spec.grayscale else "visible",
            )
            mgr._add(spec, source, colormap=colormap, max_fps=max_fps)
        return mgr

    @classmethod
    def live(cls, host: str = CAMERA_IP, *, decoder: str | None = None,
             colormap: str | None = None, latency: int = 200,
             max_fps: float | None = None) -> "VideoManager":
        """Hai luồng RTSP thật.

        :param decoder: đặt tên phần tử GStreamer (``avdec_h265`` trên máy dev,
            ``v4l2h265dec`` trên Rubik Pi 3) để đi đường GStreamer. Bỏ trống thì
            dùng backend FFMPEG của cv2 — chạy được ngay cả khi chưa cài
            ``gst-plugins-bad``/``gst-libav``.
        :param colormap: tô màu phía server. Mặc định ``None`` vì C12 tự tô được
            qua lệnh ``IMG`` — chỉ bật khi pha 1 cho thấy ``IMG`` không phản hồi.
        """
        mgr = cls()
        for spec in SPECS.values():
            uri = spec.uri(host)
            if decoder:
                uri = gst_pipeline(uri, decoder=decoder, latency=latency)
            source = CaptureSource(spec.name, uri, grayscale=spec.grayscale)
            mgr._add(spec, source,
                     colormap=colormap if spec.grayscale else None,
                     max_fps=max_fps)
        return mgr

    def _add(self, spec: StreamSpec, source: VideoSource,
             *, colormap: str | None, max_fps: float | None) -> None:
        self.sources[spec.name] = source
        self.streams[spec.name] = MjpegStream(
            source,
            quality=spec.quality,
            max_fps=max_fps,
            colormap=colormap if spec.grayscale else None,
            scale=spec.scale,
            label=spec.label,
        )

    # ------------------------------------------------------------- vòng đời

    def start(self) -> None:
        if self._started:
            return
        loop = asyncio.get_event_loop()
        for source in self.sources.values():
            source.bus.bind_loop(loop)
            source.start(loop)
        self._started = True

    async def close(self) -> None:
        for stream in self.streams.values():
            await stream.close()
        for source in self.sources.values():
            source.stop()
        self._started = False

    # ---------------------------------------------------------------- truy cập

    def get(self, name: str) -> MjpegStream:
        try:
            return self.streams[name]
        except KeyError:
            raise KeyError(
                "luồng %r không có. Có: %s" % (name, ", ".join(self.streams))
            ) from None

    def stats(self) -> dict:
        return {"streams": {n: s.describe() for n, s in self.streams.items()}}
