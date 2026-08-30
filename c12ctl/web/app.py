"""Web app: registry → API → UI, với **cổng rủi ro cưỡng chế ở server**.

Lệnh 🟠 PHYSICAL bị từ chối ở tầng service khi phiên chưa ARM, không phụ thuộc
frontend có gọi hay không. Đó là bất biến có từ pha 0 và vẫn đúng tới giờ.

Bốn nhóm endpoint, mỗi nhóm bật tắt độc lập được:

* ``/api/diagnostics/*`` — preflight và bản đồ năng lực (pha 1)
* ``/video/*`` — MJPEG hai luồng (pha 2)
* ``/api/camera/*`` — lệnh ghi camera **kèm bước đọc lại xác nhận** (pha 3)
* ``/ws/control`` + ``/api/gimbal`` — vòng 20 Hz và telemetry (pha 4–5)

    python -m c12ctl.web.app --dry-run
    python -m c12ctl.web.app --host 127.0.0.1 --port 5000   # trỏ vào simulator
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..protocol import registry as reg
from ..protocol.registry import COMMANDS, CommandNotAllowed
from ..services import findings, preflight
from ..services.camera import CameraService
from ..services.gimbal import GimbalController, NotArmed
from ..services.telemetry import TelemetryService
from ..protocol.types import RiskLevel
from ..transport.udp_link import DEFAULT_HOST, DEFAULT_PORT, UdpLink
from ..video import colormap as cmap
from ..video.manager import VideoManager
from ..video.mjpeg import CONTENT_TYPE

log = logging.getLogger("c12ctl.web")

STATIC_DIR = Path(__file__).parent / "static"


class CommandRequest(BaseModel):
    """Body của ``POST /api/cmd/{name}``.

    Tham số đi trong một model chứ không phải một ``list`` trần: FastAPI hiểu
    ``list`` trần là form data và đòi ``python-multipart``.
    """

    args: list = Field(default_factory=list)


class ColormapRequest(BaseModel):
    colormap: str | None = None


class MaxSpeedRequest(BaseModel):
    max_speed: float


class CameraRequest(BaseModel):
    """Body của ``POST /api/camera/{action}``."""

    args: list = Field(default_factory=list)


class Session:
    """Trạng thái phiên. Ở pha 0 chỉ có cờ ARM; pha 5 sẽ gắn watchdog vào đây."""

    def __init__(self, max_speed: float = 10.0) -> None:
        self.armed = False
        self.max_speed = max_speed

    def check(self, cmd) -> None:
        """Cổng rủi ro. Ném :class:`PermissionError` nếu không được phép."""
        if cmd.risk is RiskLevel.DANGEROUS:  # pragma: no cover - registry đã chặn
            raise PermissionError("%s ở mức DANGEROUS" % cmd.name)
        if cmd.risk is RiskLevel.PHYSICAL and not self.armed:
            raise PermissionError(
                "%s gây chuyển động cơ khí và phiên chưa ARM. "
                "POST /api/arm trước, và đảm bảo không gian quanh gimbal trống."
                % cmd.name
            )


def create_app(link: UdpLink, session: Session,
               findings_path: str | None = None,
               video: VideoManager | None = None,
               gimbal: GimbalController | None = None,
               telemetry: TelemetryService | None = None,
               camera: CameraService | None = None) -> FastAPI:
    app = FastAPI(title="C12 Ground Station", version="0.6.0")
    app.state.link = link
    app.state.session = session
    app.state.findings_path = findings_path
    app.state.video = video
    app.state.gimbal = gimbal
    app.state.telemetry = telemetry
    app.state.camera = camera

    # ---------------------------------------------------------------- registry

    @app.get("/api/commands")
    async def list_commands():
        """Registry lộ ra cho UI. Frontend không tự bịa lệnh nào ngoài danh sách này."""
        return {
            "commands": [
                {
                    "name": c.name,
                    "cmd3": c.cmd3,
                    "dest": c.dest.value,
                    "rw": c.rw,
                    "risk": c.risk.name,
                    "confidence": c.confidence.value,
                    "doc": c.doc,
                    "source": c.source,
                    "takes_args": c.encode is not None,
                }
                for c in sorted(COMMANDS.values(), key=lambda c: c.name)
            ],
            "forbidden": reg.FORBIDDEN,
        }

    # ------------------------------------------------------------------ health

    @app.get("/api/health")
    async def health():
        return {
            "target": {"host": link.addr[0], "port": link.addr[1]},
            "dry_run": link.dry_run,
            "armed": session.armed,
            "max_speed_dps": session.max_speed,
            "link": link.stats.as_dict(),
        }

    # ------------------------------------------------------------- diagnostics

    @app.post("/api/diagnostics/sweep")
    async def sweep(timeout: float = 0.4, save: bool = True):
        """Gọi toàn bộ lệnh 🟢 SAFE, ghi lệnh nào trả lời.

        Đây là bản đồ năng lực của pha 1: lệnh camera không hỗ trợ sẽ **im lặng**,
        và im lặng chính là dữ liệu. Không rủi ro — toàn bộ nhóm này là read-only.
        """
        report = await findings.sweep(link, timeout=timeout)
        body = report.as_dict()
        if save and app.state.findings_path:
            body["saved_to"] = str(
                findings.append_jsonl(report, app.state.findings_path)
            )
        return body

    @app.get("/api/diagnostics/preflight")
    async def preflight_check():
        """Chẩn đoán tầng dưới giao thức: cáp, IP, cổng, ping, RTSP."""
        pre = await preflight.run(
            link.addr[0], control_port=link.local_port, check_rtsp=True
        )
        return pre.as_dict()

    # --------------------------------------------------------------- arm/stop

    @app.post("/api/arm")
    async def arm():
        session.armed = True
        if app.state.gimbal is not None:
            app.state.gimbal.arm()
        log.warning("phiên ARM — lệnh gây chuyển động đã được mở")
        return {"armed": True}

    @app.post("/api/disarm")
    async def disarm():
        await stop()
        return {"armed": False}

    @app.post("/api/stop")
    async def stop(reason: str = "nút STOP"):
        """Dừng khẩn. Một đường duy nhất, gọi được từ mọi ngả."""
        session.armed = False
        if app.state.gimbal is not None:
            app.state.gimbal.stop_all(reason)
        else:
            for _ in range(3):
                for frame in reg.STOP_FRAMES:
                    link.send_frame(frame, priority=True)
        log.warning("STOP (%s)", reason)
        return {"stopped": True, "armed": False}

    # ---------------------------------------------------------------- gimbal

    def _gimbal() -> GimbalController:
        if app.state.gimbal is None:
            raise HTTPException(status_code=503, detail="điều khiển gimbal chưa bật")
        return app.state.gimbal

    @app.get("/api/gimbal")
    async def gimbal_status():
        if app.state.gimbal is None:
            return {"enabled": False}
        return {"enabled": True, **app.state.gimbal.as_dict()}

    @app.post("/api/gimbal/max-speed")
    async def set_max_speed(body: MaxSpeedRequest):
        value = _gimbal().set_max_speed(body.max_speed)
        session.max_speed = value
        return {"max_speed": value}

    @app.websocket("/ws/control")
    async def ws_control(ws: WebSocket):
        """Trình duyệt báo trạng thái; backend giữ nhịp 20 Hz.

        Đóng kết nối — dù chủ động hay do rớt mạng, đóng tab, browser crash —
        là ngả thứ ba của đường dừng khẩn.
        """
        await ws.accept()
        ctrl = app.state.gimbal
        if ctrl is None:
            await ws.send_json({"type": "error", "detail": "gimbal chưa bật"})
            await ws.close()
            return

        pusher = asyncio.create_task(_push_status(ws, ctrl))
        try:
            while True:
                msg = await ws.receive_json()
                kind = msg.get("type")

                if kind == "state":
                    try:
                        state = ctrl.set_speed(
                            float(msg.get("yaw", 0)), float(msg.get("pitch", 0))
                        )
                    except NotArmed as exc:
                        await ws.send_json({"type": "rejected", "detail": str(exc)})
                        continue
                    except (TypeError, ValueError) as exc:
                        await ws.send_json({"type": "error", "detail": str(exc)})
                        continue
                    await ws.send_json({"type": "state", **state.as_dict()})

                elif kind == "ping":
                    # Nhịp tim: làm mới watchdog mà không đổi trạng thái. Cần vì
                    # giữ phím 3 giây là 3 giây không có message state nào.
                    ctrl.heartbeat()

                elif kind == "arm":
                    session.armed = True
                    ctrl.arm()
                    await ws.send_json({"type": "armed", "armed": True})

                elif kind in ("disarm", "stop"):
                    session.armed = False
                    ctrl.stop_all("yêu cầu qua WS" if kind == "stop" else "disarm")
                    await ws.send_json({"type": "armed", "armed": False})

                else:
                    await ws.send_json({"type": "error",
                                        "detail": "loại message lạ: %r" % kind})
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - lỗi gì cũng phải dừng gimbal
            log.warning("WS điều khiển lỗi: %s", exc)
        finally:
            pusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pusher
            session.armed = False
            ctrl.stop_all("WebSocket đóng")

    async def _push_status(ws: WebSocket, ctrl: GimbalController) -> None:
        """Đẩy trạng thái + tư thế lên UI ở nhịp thấp, tách khỏi vòng 20 Hz."""
        try:
            while True:
                await asyncio.sleep(0.1)
                await ws.send_json({"type": "status", **ctrl.as_dict()})
        except (asyncio.CancelledError, WebSocketDisconnect):
            raise
        except Exception:  # pragma: no cover - client đã đi
            return

    # ---------------------------------------------------------------- camera

    def _camera() -> CameraService:
        if app.state.camera is None:
            raise HTTPException(status_code=503, detail="đệm trạng thái camera chưa bật")
        return app.state.camera

    @app.get("/api/camera")
    async def camera_state():
        """Giá trị camera **đang thật sự dùng**, không phải thứ UI vừa gửi."""
        if app.state.camera is None:
            return {"enabled": False}
        return {"enabled": True, **app.state.camera.as_dict()}

    @app.post("/api/camera/refresh")
    async def camera_refresh(force: bool = False):
        """Đọc ngay thay vì chờ tới nhịp poll. ``force`` đọc cả trường đã im lặng."""
        svc = _camera()
        await svc.poll_once(force=force)
        return svc.as_dict()

    @app.post("/api/camera/{action}")
    async def camera_apply(action: str, body: CameraRequest | None = None):
        """Ghi rồi **đọc lại**. Trả về thứ camera trả lời, kèm ba trạng thái xác nhận.

        ``ok`` là ``true`` / ``false`` / ``null``: null nghĩa là *không xác nhận
        được* (lệnh đọc im lặng, chưa cắm thẻ), khác hẳn với "sai".
        """
        svc = _camera()
        args = list(body.args) if body is not None else []
        try:
            cmd = reg.get("camera." + action)
        except CommandNotAllowed as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        try:
            session.check(cmd)
        except PermissionError as exc:  # pragma: no cover - nhóm camera là REVERSIBLE
            raise HTTPException(status_code=403, detail=str(exc)) from None
        try:
            result = await svc.apply(action, *args)
        except CommandNotAllowed as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except (TypeError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return result.as_dict()

    # ----------------------------------------------------------------- lệnh

    @app.post("/api/cmd/{name}")
    async def run_command(name: str, body: CommandRequest | None = None):
        try:
            cmd = reg.get(name)
        except CommandNotAllowed as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        try:
            session.check(cmd)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None

        args = list(body.args) if body is not None else []
        try:
            if cmd.expect_reply:
                value = await link.request(cmd, *args)
                return {"name": name, "frame": cmd.frame(*args),
                        "value": _jsonable(value), "alive": value is not None}
            frame = link.send(cmd, *args)
            return {"name": name, "frame": frame, "sent": True}
        except (TypeError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    # ----------------------------------------------------------------- video

    def _video() -> VideoManager:
        if app.state.video is None:
            raise HTTPException(status_code=503, detail="video chưa bật")
        return app.state.video

    @app.get("/video/{name}")
    async def video_stream(name: str):
        """MJPEG. Encode một lần cho mọi client; encoder ngủ khi hết người xem."""
        try:
            stream = _video().get(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return StreamingResponse(
            stream.frames(),
            media_type=CONTENT_TYPE,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                     "Pragma": "no-cache", "Connection": "close"},
        )

    @app.get("/video/{name}/snapshot.jpg")
    async def video_snapshot(name: str):
        try:
            stream = _video().get(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        data = await stream.snapshot()
        if data is None:
            raise HTTPException(status_code=503, detail="chưa có khung nào")
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/video")
    async def video_stats():
        if app.state.video is None:
            return {"enabled": False, "streams": {}}
        return {"enabled": True, "colormaps": cmap.available(),
                **_video().stats()}

    @app.post("/api/video/{name}/colormap")
    async def set_colormap(name: str, body: ColormapRequest):
        """Tô màu phía server — DỰ PHÒNG. C12 tự tô được qua lệnh IMG."""
        try:
            stream = _video().get(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        value = body.colormap
        if value not in (None, "") and value not in cmap.available():
            raise HTTPException(
                status_code=400,
                detail="colormap %r không có. Có: %s"
                       % (value, ", ".join(cmap.available())),
            )
        stream.colormap = value or None
        return {"name": name, "colormap": stream.colormap}

    # ------------------------------------------------------------------ tĩnh

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def index():
            return FileResponse(STATIC_DIR / "index.html")

    return app


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return str(value)


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST, help="IP camera")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--local-port", type=int, default=DEFAULT_PORT,
                    help="cổng UDP local. Dùng 0 nếu cổng 5000 đang bị chiếm")
    ap.add_argument("--bind", default="127.0.0.1", help="địa chỉ nghe HTTP")
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--dry-run", action="store_true",
                    help="in gói ra log thay vì gửi UDP")
    ap.add_argument("--max-speed", type=float, default=10.0,
                    help="giới hạn tốc độ gimbal °/s. Mặc định thấp có chủ ý — "
                         "chỉ nâng sau khi đã thấy vòng điều khiển dừng đúng")
    ap.add_argument("--packet-log", default=None, metavar="PATH",
                    help="ghi mọi gói TX/RX ra file JSONL")
    ap.add_argument("--findings", default="logs/findings.jsonl", metavar="PATH",
                    help="nơi trang Diagnostics ghi bản đồ năng lực")
    ap.add_argument("--video", choices=("live", "synthetic", "off"),
                    default="live",
                    help="nguồn video. 'synthetic' không cần camera — dùng để "
                         "phát triển và đo pipeline khi chưa có phần cứng")
    ap.add_argument("--decoder", default=None, metavar="ELEMENT",
                    help="phần tử GStreamer để decode: avdec_h265 (máy dev) hoặc "
                         "v4l2h265dec (Rubik Pi 3, decode phần cứng). Bỏ trống "
                         "thì dùng backend FFMPEG của cv2")
    ap.add_argument("--colormap", default=None, metavar="NAME",
                    help="tô màu ảnh nhiệt phía server. DỰ PHÒNG — C12 tự tô "
                         "được qua lệnh IMG")
    ap.add_argument("--max-video-fps", type=float, default=None, metavar="FPS",
                    help="chặn trần nhịp encode MJPEG")
    ap.add_argument("--no-gimbal", action="store_true",
                    help="không bật vòng điều khiển gimbal")
    ap.add_argument("--no-telemetry", action="store_true",
                    help="không bật luồng đẩy tư thế (GAA/GAC)")
    ap.add_argument("--no-camera", action="store_true",
                    help="không bật đệm trạng thái camera (tab Camera sẽ tắt)")
    ap.add_argument("--camera-interval", type=float, default=1.0, metavar="SEC",
                    help="nhịp vòng poll trạng thái camera")
    ap.add_argument("--use-gsm", action="store_true",
                    help="gộp yaw+pitch vào một gói GSM — nửa lưu lượng ở 20 Hz, "
                         "nhưng cần firmware gimbal >= 0.5. Không tự thăm dò được "
                         "vì GSM không có phản hồi")
    ap.add_argument("--telemetry-hz", type=int, default=10, metavar="HZ",
                    help="nhịp đẩy tư thế")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


async def _run(args) -> None:
    import uvicorn

    reg.assert_registry_sane()

    link = UdpLink(
        args.host, args.port, args.local_port,
        dry_run=args.dry_run, log_path=args.packet_log,
    )
    await link.start()
    session = Session(max_speed=args.max_speed)

    video = None
    if args.video == "synthetic":
        video = VideoManager.synthetic(
            colormap=args.colormap or "ironbow", max_fps=args.max_video_fps
        )
    elif args.video == "live":
        video = VideoManager.live(
            args.host, decoder=args.decoder, colormap=args.colormap,
            max_fps=args.max_video_fps,
        )
    if video is not None:
        video.start()

    telemetry = None
    if not args.no_telemetry:
        telemetry = TelemetryService(link, rate_hz=args.telemetry_hz)
        await telemetry.start()

    gimbal = None
    if not args.no_gimbal:
        gimbal = GimbalController(
            link, max_speed=args.max_speed, telemetry=telemetry,
            use_gsm=args.use_gsm,
        )
        await gimbal.start()

    camera = None
    if not args.no_camera:
        # Dry-run không có ai trả lời: mọi lệnh đọc sẽ im lặng và toàn bộ trường
        # bị đánh dấu không hỗ trợ sau 3 vòng. Đúng như thiết kế, nhưng vô ích —
        # nên không bật đệm ở chế độ đó.
        if args.dry_run:
            log.info("--dry-run: bỏ qua đệm trạng thái camera (không ai trả lời)")
        else:
            camera = CameraService(
                link, interval=args.camera_interval,
                # Gimbal đang quay thì lệnh đọc kẹt sau hàng ưu tiên và sẽ
                # timeout. Nghỉ poll thay vì thu về một bản đồ năng lực sai.
                busy=lambda: gimbal is not None and gimbal.state.moving,
            )
            await camera.start()

    app = create_app(link, session, findings_path=args.findings, video=video,
                     gimbal=gimbal, telemetry=telemetry, camera=camera)

    config = uvicorn.Config(app, host=args.bind, port=args.http_port,
                            log_level="debug" if args.verbose else "info")
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def _shutdown() -> None:
        # Ngả thứ năm của đường dừng khẩn: tín hiệu hệ thống.
        if gimbal is not None:
            gimbal.stop_all("tín hiệu tắt")
        else:
            for frame in reg.STOP_FRAMES:
                link.send_frame(frame, priority=True)
        server.should_exit = True
        stopping.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown)

    try:
        await server.serve()
    finally:
        if gimbal is not None:
            await gimbal.close()
        else:
            for frame in reg.STOP_FRAMES:
                link.send_frame(frame, priority=True)
        if camera is not None:
            await camera.close()
        if telemetry is not None:
            await telemetry.close()
        await asyncio.sleep(0.1)
        await link.close()
        if video is not None:
            await video.close()


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
