"""Web app: registry → API → UI, with the **risk gate enforced on the server**.

🟠 PHYSICAL commands are refused in the service layer while the session is not
armed, regardless of whether the frontend ever calls them. That invariant dates
from phase 0 and still holds.

Four endpoint groups, each independently switchable:

* ``/api/diagnostics/*`` — preflight and the capability map (phase 1)
* ``/video/*`` — two MJPEG streams (phase 2)
* ``/api/camera/*`` — camera writes **with a read-back confirmation** (phase 3)
* ``/ws/control`` + ``/api/gimbal`` — the 20 Hz loop and telemetry (phases 4–5)

    python -m c12ctl.web.app --dry-run
    python -m c12ctl.web.app --host 127.0.0.1 --port 5000   # point at the simulator
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
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
from ..services.session import SessionReader, SessionRecorder
from ..services.telemetry import TelemetryService
from ..protocol.types import RiskLevel
from ..transport.udp_link import DEFAULT_HOST, DEFAULT_PORT, UdpLink
from ..video import colormap as cmap
from ..video.manager import VideoManager
from ..video.mjpeg import CONTENT_TYPE

log = logging.getLogger("c12ctl.web")

STATIC_DIR = Path(__file__).parent / "static"


class CommandRequest(BaseModel):
    """Body of ``POST /api/cmd/{name}``.

    Parameters travel inside a model rather than a bare ``list``: FastAPI reads a
    bare ``list`` as form data and then demands ``python-multipart``.
    """

    args: list = Field(default_factory=list)


class ColormapRequest(BaseModel):
    colormap: str | None = None


class MaxSpeedRequest(BaseModel):
    max_speed: float


class CameraRequest(BaseModel):
    """Body of ``POST /api/camera/{action}``."""

    args: list = Field(default_factory=list)


class SessionRequest(BaseModel):
    note: str = ""


class Session:
    """Session state. In phase 0 this was only the ARM flag; phase 5 attached the
    watchdog to it."""

    def __init__(self, max_speed: float = 10.0) -> None:
        self.armed = False
        self.max_speed = max_speed

    def check(self, cmd) -> None:
        """The risk gate. Raises :class:`PermissionError` when not allowed."""
        if cmd.risk is RiskLevel.DANGEROUS:  # pragma: no cover - registry blocks this
            raise PermissionError("%s is DANGEROUS" % cmd.name)
        if cmd.risk is RiskLevel.PHYSICAL and not self.armed:
            raise PermissionError(
                "%s causes mechanical motion and the session is not armed. "
                "ARM it with POST /api/arm first, and make sure the space around "
                "the gimbal is clear." % cmd.name
            )


def create_app(link: UdpLink, session: Session,
               findings_path: str | None = None,
               video: VideoManager | None = None,
               gimbal: GimbalController | None = None,
               telemetry: TelemetryService | None = None,
               camera: CameraService | None = None,
               recorder: SessionRecorder | None = None) -> FastAPI:
    app = FastAPI(title="C12 Ground Station", version="0.7.0")
    app.state.link = link
    app.state.session = session
    app.state.findings_path = findings_path
    app.state.video = video
    app.state.gimbal = gimbal
    app.state.telemetry = telemetry
    app.state.camera = camera
    app.state.recorder = recorder

    # ---------------------------------------------------------------- registry

    @app.get("/api/commands")
    async def list_commands():
        """The registry exposed to the UI. The frontend invents no command
        outside this list."""
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
        """Call every 🟢 SAFE command and record which ones answer.

        This is phase 1's capability map: a command the camera does not support
        stays **silent**, and that silence is the data. No risk — the whole group
        is read-only.
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
        """Diagnose the layers below the protocol: cable, IP, port, ping, RTSP."""
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
        log.warning("session ARMED — motion commands are now open")
        return {"armed": True}

    @app.post("/api/disarm")
    async def disarm():
        await stop()
        return {"armed": False}

    @app.post("/api/stop")
    async def stop(reason: str = "STOP button"):
        """Emergency stop. One single path, callable from anywhere."""
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
            raise HTTPException(status_code=503, detail="gimbal control is disabled")
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
        """The browser reports state; the backend keeps the 20 Hz cadence.

        Closing the connection — deliberately, or through a dropped network, a
        closed tab, or a browser crash — is path three of the emergency stop.
        """
        await ws.accept()
        ctrl = app.state.gimbal
        if ctrl is None:
            await ws.send_json({"type": "error", "detail": "gimbal is disabled"})
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
                    # Heartbeat: refresh the watchdog without changing state.
                    # Needed because holding a key for 3 seconds means 3 seconds
                    # with no state message at all.
                    ctrl.heartbeat()

                elif kind == "arm":
                    session.armed = True
                    ctrl.arm()
                    await ws.send_json({"type": "armed", "armed": True})

                elif kind in ("disarm", "stop"):
                    session.armed = False
                    ctrl.stop_all("requested over WebSocket" if kind == "stop"
                                  else "disarm")
                    await ws.send_json({"type": "armed", "armed": False})

                else:
                    await ws.send_json({"type": "error",
                                        "detail": "unknown message type: %r" % kind})
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - any error must stop the gimbal
            log.warning("control WebSocket failed: %s", exc)
        finally:
            pusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pusher
            session.armed = False
            ctrl.stop_all("WebSocket closed")

    async def _push_status(ws: WebSocket, ctrl: GimbalController) -> None:
        """Push state and attitude to the UI at a low rate, separate from the
        20 Hz loop."""
        try:
            while True:
                await asyncio.sleep(0.1)
                await ws.send_json({"type": "status", **ctrl.as_dict()})
        except (asyncio.CancelledError, WebSocketDisconnect):
            raise
        except Exception:  # pragma: no cover - the client is already gone
            return

    # ---------------------------------------------------------------- camera

    def _camera() -> CameraService:
        if app.state.camera is None:
            raise HTTPException(status_code=503,
                                detail="the camera state cache is disabled")
        return app.state.camera

    @app.get("/api/camera")
    async def camera_state():
        """The values the camera is **actually using**, not what the UI just sent."""
        if app.state.camera is None:
            return {"enabled": False}
        return {"enabled": True, **app.state.camera.as_dict()}

    @app.post("/api/camera/refresh")
    async def camera_refresh(force: bool = False):
        """Read now instead of waiting for the poll cycle. ``force`` also reads
        fields that have gone silent."""
        svc = _camera()
        await svc.poll_once(force=force)
        return svc.as_dict()

    @app.post("/api/camera/{action}")
    async def camera_apply(action: str, body: CameraRequest | None = None):
        """Write, then **read back**. Returns what the camera answered, with a
        three-state confirmation.

        ``ok`` is ``true`` / ``false`` / ``null``: null means *could not be
        verified* (silent read, no card inserted), which is very different from
        "wrong".
        """
        svc = _camera()
        args = list(body.args) if body is not None else []
        try:
            cmd = reg.get("camera." + action)
        except CommandNotAllowed as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        try:
            session.check(cmd)
        except PermissionError as exc:  # pragma: no cover - camera group is REVERSIBLE
            raise HTTPException(status_code=403, detail=str(exc)) from None
        try:
            result = await svc.apply(action, *args)
        except CommandNotAllowed as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except (TypeError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return result.as_dict()

    # --------------------------------------------------------------- sessions

    def _recorder() -> SessionRecorder:
        if app.state.recorder is None:
            raise HTTPException(status_code=503, detail="session recording is disabled")
        return app.state.recorder

    def _session_path(sid: str) -> Path:
        """Resolve a session id to a directory, refusing anything that escapes.

        The id reaches us from a URL, so it is untrusted input used to build a
        filesystem path — the one place in this app where that happens.
        """
        rec = _recorder()
        if not sid or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", sid) or sid in (".", ".."):
            raise HTTPException(status_code=400, detail="malformed session id")
        root = rec.root.resolve()
        path = (root / sid).resolve()
        if path != root and root not in path.parents:
            raise HTTPException(status_code=400, detail="malformed session id")
        if not (path / "meta.json").is_file():
            raise HTTPException(status_code=404, detail="no recording named %r" % sid)
        return path

    @app.get("/api/session")
    async def session_status():
        if app.state.recorder is None:
            return {"enabled": False, "recording": False, "sessions": []}
        rec = app.state.recorder
        return {"enabled": True, **rec.as_dict(), "sessions": rec.list_sessions()}

    @app.post("/api/session/start")
    async def session_start(body: SessionRequest | None = None):
        rec = _recorder()
        try:
            return await rec.start(body.note if body else "")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.post("/api/session/stop")
    async def session_stop():
        return await _recorder().stop("requested")

    @app.get("/api/session/{sid}")
    async def session_summary(sid: str):
        return SessionReader(_session_path(sid)).summary()

    @app.get("/api/session/{sid}/frame/{stream}/{index}.jpg")
    async def session_frame(sid: str, stream: str, index: int):
        data = SessionReader(_session_path(sid)).frame(stream, index)
        if data is None:
            raise HTTPException(status_code=404, detail="no such frame")
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    # --------------------------------------------------------------- commands

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
            raise HTTPException(status_code=503, detail="video is disabled")
        return app.state.video

    @app.get("/video/{name}")
    async def video_stream(name: str):
        """MJPEG. Encoded once for all clients; the encoder sleeps with no viewers."""
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
            raise HTTPException(status_code=503, detail="no frame available yet")
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
        """Server-side colorization — a FALLBACK. The C12 can colorize itself via IMG."""
        try:
            stream = _video().get(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        value = body.colormap
        if value not in (None, "") and value not in cmap.available():
            raise HTTPException(
                status_code=400,
                detail="no colormap named %r. Available: %s"
                       % (value, ", ".join(cmap.available())),
            )
        stream.colormap = value or None
        return {"name": name, "colormap": stream.colormap}

    # ----------------------------------------------------------------- static

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
    ap.add_argument("--host", default=DEFAULT_HOST, help="camera IP")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--local-port", type=int, default=DEFAULT_PORT,
                    help="local UDP port. Use 0 if port 5000 is already taken")
    ap.add_argument("--bind", default="127.0.0.1", help="HTTP listen address")
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--dry-run", action="store_true",
                    help="log packets instead of sending them over UDP")
    ap.add_argument("--max-speed", type=float, default=10.0,
                    help="gimbal speed limit in °/s. The low default is "
                         "deliberate — raise it only after watching the control "
                         "loop stop correctly")
    ap.add_argument("--packet-log", default=None, metavar="PATH",
                    help="write every TX/RX packet to a JSONL file")
    ap.add_argument("--findings", default="logs/findings.jsonl", metavar="PATH",
                    help="where the Diagnostics page writes the capability map")
    ap.add_argument("--video", choices=("live", "synthetic", "off"),
                    default="live",
                    help="video source. 'synthetic' needs no camera — use it to "
                         "develop and measure the pipeline before the hardware "
                         "exists")
    ap.add_argument("--decoder", default=None, metavar="ELEMENT",
                    help="GStreamer element to decode with: avdec_h265 (dev "
                         "machine) or v4l2h265dec (Rubik Pi 3, hardware). Leave "
                         "empty to use cv2's FFMPEG backend")
    ap.add_argument("--colormap", default=None, metavar="NAME",
                    help="colorize thermal server-side. FALLBACK — the C12 can "
                         "colorize itself via the IMG command")
    ap.add_argument("--max-video-fps", type=float, default=None, metavar="FPS",
                    help="cap the MJPEG encode rate")
    ap.add_argument("--no-gimbal", action="store_true",
                    help="do not start the gimbal control loop")
    ap.add_argument("--no-telemetry", action="store_true",
                    help="do not enable the attitude push stream (GAA/GAC)")
    ap.add_argument("--no-camera", action="store_true",
                    help="do not start the camera state cache (disables the "
                         "Camera tab)")
    ap.add_argument("--camera-interval", type=float, default=1.0, metavar="SEC",
                    help="camera state poll period")
    ap.add_argument("--no-record", action="store_true",
                    help="do not offer session recording")
    ap.add_argument("--session-dir", default="logs/sessions", metavar="PATH",
                    help="where session recordings are written")
    ap.add_argument("--record-fps", type=float, default=5.0, metavar="FPS",
                    help="recorded frames per second per stream. Low on purpose: "
                         "the source rate would fill a card long before it earned "
                         "its keep for protocol work")
    ap.add_argument("--record-max-mb", type=float, default=512.0, metavar="MB",
                    help="stop recording at this size, before the disk does")
    ap.add_argument("--record-max-seconds", type=float, default=3600.0, metavar="SEC",
                    help="stop a recording nobody remembered to stop")
    ap.add_argument("--use-gsm", action="store_true",
                    help="pack yaw and pitch into one GSM packet — half the "
                         "traffic at 20 Hz, but it needs gimbal firmware >= 0.5. "
                         "Cannot be probed, since GSM has no reply")
    ap.add_argument("--telemetry-hz", type=int, default=10, metavar="HZ",
                    help="attitude push rate")
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
        # In dry-run nobody answers: every read would go silent and all fields
        # would be marked unsupported after three cycles. Correct by design, but
        # useless — so the cache is not started in that mode.
        if args.dry_run:
            log.info("--dry-run: skipping the camera state cache (nobody answers)")
        else:
            camera = CameraService(
                link, interval=args.camera_interval,
                # While the gimbal is turning, reads queue behind the priority
                # traffic and time out. Rest instead of collecting a false
                # capability map.
                busy=lambda: gimbal is not None and gimbal.state.moving,
            )
            await camera.start()

    recorder = None
    if not args.no_record:
        recorder = SessionRecorder(
            link, root=args.session_dir, video=video, telemetry=telemetry,
            frame_fps=args.record_fps,
            max_bytes=int(args.record_max_mb * 1024 * 1024),
            max_seconds=args.record_max_seconds,
        )

    app = create_app(link, session, findings_path=args.findings, video=video,
                     gimbal=gimbal, telemetry=telemetry, camera=camera,
                     recorder=recorder)

    config = uvicorn.Config(app, host=args.bind, port=args.http_port,
                            log_level="debug" if args.verbose else "info")
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def _shutdown() -> None:
        # Path five of the emergency stop: an OS signal.
        if gimbal is not None:
            gimbal.stop_all("shutdown signal")
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
        # Close the recording before the sources it is reading from go away, so
        # a session survives Ctrl-C with its meta.json summary intact.
        if recorder is not None:
            await recorder.close()
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
