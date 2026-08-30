"""The C12's two streams, preconfigured from measured parameters.

|            | Visible         | Thermal         |
|------------|-----------------|-----------------|
| RTSP       | `:554/stream=1` | `:555/stream=2` |
| Codec      | HEVC Main       | HEVC Main       |
| Size       | 1280×720        | 384×288         |
| Rate       | 30 fps          | 25 fps          |

The two streams run at **different rates** (30 vs 25) so they are kept fully
independent: one thread, one bus and one encoder each. Nothing synchronizes
frames between them — trying to sync two sources with different rates only
produces judder or latency.

The thermal stream uses ``scale=2.0``: 384×288 shown at native size is too small,
and upscaling server-side with ``INTER_NEAREST`` is far cheaper than letting the
browser interpolate smoothly.
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
    """One stream's parameters, measured with ffprobe against a real C12."""

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
        name=VISIBLE, label="Visible", rtsp_port=554, rtsp_path="stream=1",
        width=1280, height=720, fps=30.0, grayscale=False, quality=80, scale=1.0,
    ),
    THERMAL: StreamSpec(
        name=THERMAL, label="Thermal", rtsp_port=555, rtsp_path="stream=2",
        width=384, height=288, fps=25.0, grayscale=True, quality=85, scale=2.0,
    ),
}


class VideoManager:
    """Lifecycle of both streams."""

    def __init__(self) -> None:
        self.streams: dict[str, MjpegStream] = {}
        self.sources: dict[str, VideoSource] = {}
        self._started = False

    # -------------------------------------------------------------- building

    @classmethod
    def synthetic(cls, *, colormap: str | None = "ironbow",
                  max_fps: float | None = None) -> "VideoManager":
        """Two synthetic streams — no camera, no GStreamer needed."""
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
        """Two real RTSP streams.

        :param decoder: name a GStreamer element (``avdec_h265`` on the dev
            machine, ``v4l2h265dec`` on the Rubik Pi 3) to take the GStreamer
            path. Leave it empty to use cv2's FFMPEG backend — which works even
            without ``gst-plugins-bad``/``gst-libav`` installed.
        :param colormap: colorize server-side. Defaults to ``None`` because the
            C12 colorizes on its own via ``IMG`` — only enable it if phase 1
            shows that ``IMG`` does not answer.
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

    # ------------------------------------------------------------- lifecycle

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

    # ----------------------------------------------------------------- access

    def get(self, name: str) -> MjpegStream:
        try:
            return self.streams[name]
        except KeyError:
            raise KeyError(
                "no stream named %r. Available: %s" % (name, ", ".join(self.streams))
            ) from None

    def stats(self) -> dict:
        return {"streams": {n: s.describe() for n, s in self.streams.items()}}
