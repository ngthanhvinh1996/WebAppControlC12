"""The HTTP API — centred on the risk gate enforced on the server."""

import httpx
import pytest
from httpx import ASGITransport

from c12ctl.sim.c12_sim import C12Simulator
from c12ctl.transport.udp_link import UdpLink
from c12ctl.web.app import Session, create_app


@pytest.fixture
async def client():
    sim = C12Simulator(seed=99)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    session = Session()
    app = create_app(link, session)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        c.sim, c.link, c.session = sim, link, session
        yield c
    await link.close()
    await sim.close()


# --------------------------------------------------------------------------
# The risk gate
# --------------------------------------------------------------------------


async def test_physical_command_refused_until_armed(client):
    r = await client.post("/api/cmd/gimbal.yaw_speed", json={"args": [20]})
    assert r.status_code == 403
    assert "ARM" in r.json()["detail"]
    assert client.sim.state.yaw_speed == 0, "no packet may leave the backend"


async def test_physical_command_allowed_after_arm(client):
    await client.post("/api/arm")
    r = await client.post("/api/cmd/gimbal.yaw_speed", json={"args": [20]})
    assert r.status_code == 200
    # 20 °/s → raw 40 = 0x28; the checksum of "#TPUG2wGSY28" is 69.
    assert r.json()["frame"] == "#TPUG2wGSY2869"


async def test_reversible_command_needs_no_arm(client):
    r = await client.post("/api/cmd/camera.snap")
    assert r.status_code == 200
    assert r.json()["frame"] == "#TPUD2wCAP013E"


async def test_safe_read_needs_no_arm(client):
    r = await client.post("/api/cmd/read.model")
    assert r.status_code == 200
    assert r.json()["value"] == "0C"


async def test_unknown_command_is_404(client):
    r = await client.post("/api/cmd/camera.set_ip")
    assert r.status_code == 404
    assert "allowlist" in r.json()["detail"].lower()


@pytest.mark.parametrize(
    "name",
    ["camera.reboot", "camera.factory_reset", "camera.set_gateway",
     "network.set_ip", "gimbal.calibrate", "camera.set_video_config"],
)
async def test_dangerous_names_have_no_route(client, name):
    """There is no endpoint for the 🔴 group, not even under the right name."""
    assert (await client.post("/api/cmd/" + name)).status_code == 404


async def test_stop_disarms_and_sends_zero(client):
    await client.post("/api/arm")
    assert (await client.get("/api/health")).json()["armed"] is True

    r = await client.post("/api/stop")
    assert r.json() == {"stopped": True, "armed": False}
    assert (await client.get("/api/health")).json()["armed"] is False

    # After STOP, motion commands must be refused again.
    assert (await client.post("/api/cmd/gimbal.yaw_speed",
                              json={"args": [20]})).status_code == 403


async def test_stop_actually_zeroes_a_moving_gimbal(client):
    import asyncio

    await client.post("/api/arm")
    await client.post("/api/cmd/gimbal.yaw_speed", json={"args": [30]})
    await asyncio.sleep(0.05)
    assert client.sim.state.yaw_speed != 0

    await client.post("/api/stop")
    await asyncio.sleep(0.05)
    assert client.sim.state.yaw_speed == 0


async def test_disarm_also_stops(client):
    await client.post("/api/arm")
    assert (await client.post("/api/disarm")).json()["armed"] is False


# --------------------------------------------------------------------------
# Registry and diagnostics
# --------------------------------------------------------------------------


async def test_registry_endpoint_exposes_risk_and_confidence(client):
    body = (await client.get("/api/commands")).json()
    names = {c["name"] for c in body["commands"]}
    assert "read.version" in names
    assert "gimbal.yaw_speed" in names
    assert all(c["risk"] != "DANGEROUS" for c in body["commands"])
    assert "IPV" in body["forbidden"] and "GTW" in body["forbidden"]


async def test_diagnostics_sweep_separates_alive_from_silent(client):
    body = (await client.post(
        "/api/diagnostics/sweep?timeout=0.1&save=false"
    )).json()
    assert body["total"] == body["alive"] + body["silent"]
    assert body["alive"] > 0 and body["silent"] > 0

    by_name = {r["name"]: r for r in body["probes"]}
    assert by_name["read.version"]["alive"] is True
    assert by_name["read.ranging"]["alive"] is False
    assert by_name["read.version"]["frame"] == "#TPUD2rVER0051"
    assert by_name["read.version"]["raw"] is not None
    assert "C13" in by_name["read.ranging"]["note"]


async def test_diagnostics_sweep_saves_findings(client, tmp_path):
    """The Diagnostics page writes the capability map to a file, not just to screen."""
    import json

    path = tmp_path / "findings.jsonl"
    client._transport.app.state.findings_path = str(path)
    body = (await client.post("/api/diagnostics/sweep?timeout=0.1")).json()
    assert body["saved_to"] == str(path)
    records = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(records) == body["total"]


async def test_preflight_endpoint_returns_checks(client):
    body = (await client.get("/api/diagnostics/preflight")).json()
    assert "ok" in body and body["checks"]
    assert all({"name", "status", "detail", "fix"} <= set(c) for c in body["checks"])


async def test_health_reports_link_stats(client):
    await client.post("/api/cmd/read.model")
    body = (await client.get("/api/health")).json()
    assert body["link"]["tx"] >= 1 and body["link"]["rx"] >= 1
    assert body["dry_run"] is False


# --------------------------------------------------------------------------
# Dry-run
# --------------------------------------------------------------------------


async def test_dry_run_sends_nothing_to_the_wire():
    sim = C12Simulator(seed=1)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0,
                   dry_run=True, min_tx_gap=0.001)
    await link.start()
    app = create_app(link, Session())
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.post("/api/cmd/camera.snap")
            assert r.json()["frame"] == "#TPUD2wCAP013E"
            assert (await c.get("/api/health")).json()["dry_run"] is True
        import asyncio

        await asyncio.sleep(0.05)
        assert sim.rx_count == 0
    finally:
        await link.close()
        await sim.close()


# --------------------------------------------------------------------------
# Video (pha 2)
# --------------------------------------------------------------------------


@pytest.fixture
async def vclient():
    """A client with synthetic video already running."""
    from c12ctl.video.manager import VideoManager

    sim = C12Simulator(seed=5)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    video = VideoManager.synthetic()
    video.start()
    app = create_app(link, Session(), video=video)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        c.video = video
        yield c
    await video.close()
    await link.close()
    await sim.close()


async def test_video_stats_lists_both_streams(vclient):
    b = (await vclient.get("/api/video")).json()
    assert b["enabled"] is True
    assert set(b["streams"]) == {"visible", "thermal"}
    assert "ironbow" in b["colormaps"]


async def test_snapshot_returns_jpeg_bytes(vclient):
    r = await vclient.get("/video/visible/snapshot.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8") and r.content.endswith(b"\xff\xd9")


async def test_snapshot_geometry_matches_spec(vclient):
    import cv2
    import numpy as np

    r = await vclient.get("/video/visible/snapshot.jpg")
    frame = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert frame.shape[:2] == (720, 1280)

    r = await vclient.get("/video/thermal/snapshot.jpg")
    frame = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert frame.shape[:2] == (576, 768), "thermal 384×288 upscaled ×2"


@pytest.fixture
async def live_url(vclient):
    """A real uvicorn server on a random port.

    Do NOT use ASGITransport for streaming endpoints: it awaits the ASGI app to
    COMPLETION before collecting the body, so an infinite MJPEG stream hangs
    forever — an httpx limitation, not an endpoint bug. Video streams have to be
    tested through a real socket.
    """
    import asyncio

    import uvicorn

    app = vclient._transport.app
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield "http://127.0.0.1:%d" % port
    server.should_exit = True
    await asyncio.wait_for(task, 10)


async def test_mjpeg_stream_emits_multipart(live_url):
    from c12ctl.video.mjpeg import BOUNDARY

    async with httpx.AsyncClient(base_url=live_url, timeout=15) as c:
        async with c.stream("GET", "/video/thermal") as r:
            assert r.status_code == 200
            assert BOUNDARY in r.headers["content-type"]
            chunks = b""
            async for chunk in r.aiter_bytes():
                chunks += chunk
                if chunks.count(b"--" + BOUNDARY.encode()) >= 2:
                    break

    assert b"Content-Type: image/jpeg" in chunks
    assert b"Content-Length: " in chunks
    assert b"\xff\xd8" in chunks


async def test_mjpeg_encoder_sleeps_after_client_leaves(live_url, vclient):
    """Disconnecting mid-stream: the server encoder must sleep, not run forever."""
    import asyncio

    from c12ctl.video.mjpeg import BOUNDARY

    stream = vclient.video.get("visible")
    async with httpx.AsyncClient(base_url=live_url, timeout=15) as c:
        async with c.stream("GET", "/video/visible") as r:
            async for chunk in r.aiter_bytes():
                if b"--" + BOUNDARY.encode() in chunk:
                    break
            assert stream.stats.clients == 1

    for _ in range(200):
        await asyncio.sleep(0.02)
        if stream.stats.clients == 0:
            break
    assert stream.stats.clients == 0
    assert stream._encoder is None, "the encoder must sleep when nobody is watching"


async def test_two_clients_share_one_encode(live_url, vclient):
    """Two real HTTP connections, one encode per frame."""
    import asyncio

    from c12ctl.video.mjpeg import BOUNDARY

    stream = vclient.video.get("thermal")

    async def watch(c):
        async with c.stream("GET", "/video/thermal") as r:
            async for chunk in r.aiter_bytes():
                if b"--" + BOUNDARY.encode() in chunk:
                    return

    async with httpx.AsyncClient(base_url=live_url, timeout=15) as c1, \
               httpx.AsyncClient(base_url=live_url, timeout=15) as c2:
        await asyncio.gather(watch(c1), watch(c2))

    assert stream.stats.encoded >= 1


async def test_unknown_stream_is_404(vclient):
    r = await vclient.get("/video/infrared/snapshot.jpg")
    assert r.status_code == 404
    assert "visible" in r.json()["detail"]


async def test_colormap_can_be_set_and_cleared(vclient):
    r = await vclient.post("/api/video/thermal/colormap", json={"colormap": "rainbow"})
    assert r.json()["colormap"] == "rainbow"
    assert vclient.video.get("thermal").colormap == "rainbow"

    r = await vclient.post("/api/video/thermal/colormap", json={"colormap": None})
    assert r.json()["colormap"] is None


async def test_bad_colormap_rejected_with_options(vclient):
    r = await vclient.post("/api/video/thermal/colormap", json={"colormap": "xyz"})
    assert r.status_code == 400
    assert "ironbow" in r.json()["detail"]


async def test_video_routes_503_when_disabled(client):
    """The app runs without video — phase 1 still works as before."""
    assert (await client.get("/api/video")).json()["enabled"] is False
    assert (await client.get("/video/visible/snapshot.jpg")).status_code == 503


# --------------------------------------------------------------------------
# Gimbal control over WebSocket (phase 5)
# --------------------------------------------------------------------------


@pytest.fixture
async def gclient():
    """The app with the gimbal control loop and telemetry, on a real server.

    WebSockets are like MJPEG here: ASGITransport cannot emulate them, so this
    has to go through a socket.
    """
    import asyncio

    import uvicorn

    from c12ctl.services.gimbal import GimbalController
    from c12ctl.services.telemetry import TelemetryService

    sim = C12Simulator(seed=21)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    tel = TelemetryService(link, rate_hz=20, rearm_interval=0.2)
    await tel.start()
    ctrl = GimbalController(link, max_speed=30.0, telemetry=tel,
                            tick=0.02, watchdog=0.3)
    await ctrl.start()

    app = create_app(link, Session(), gimbal=ctrl, telemetry=tel)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]

    class Rig:
        pass

    rig = Rig()
    rig.sim, rig.link, rig.ctrl, rig.tel = sim, link, ctrl, tel
    rig.base = "http://127.0.0.1:%d" % port
    rig.ws_url = "ws://127.0.0.1:%d/ws/control" % port
    yield rig

    server.should_exit = True
    await asyncio.wait_for(task, 10)
    await ctrl.close()
    await tel.close()
    await link.close()
    await sim.close()


async def _ws(url):
    import websockets

    return await websockets.connect(url)


async def test_ws_requires_arm_before_moving(gclient):
    import json

    async with await _ws(gclient.ws_url) as ws:
        await ws.send(json.dumps({"type": "state", "yaw": 20, "pitch": 0}))
        for _ in range(40):
            msg = json.loads(await ws.recv())
            if msg["type"] == "rejected":
                break
        assert msg["type"] == "rejected"
        assert "ARM" in msg["detail"]
    assert gclient.sim.state.yaw_speed == 0


async def test_ws_arm_then_move(gclient):
    import asyncio
    import json

    async with await _ws(gclient.ws_url) as ws:
        await ws.send(json.dumps({"type": "arm"}))
        await ws.send(json.dumps({"type": "state", "yaw": 25, "pitch": 0}))
        for _ in range(30):
            await asyncio.sleep(0.03)
            await ws.send(json.dumps({"type": "ping"}))
            if gclient.sim.state.yaw_speed:
                break
        assert gclient.sim.state.yaw_speed != 0
        assert gclient.sim.state.yaw > 0


async def test_ws_close_stops_a_moving_gimbal(gclient):
    """Phase 5 exit criterion: close the tab mid-motion → it must stop."""
    import asyncio
    import json

    ws = await _ws(gclient.ws_url)
    await ws.send(json.dumps({"type": "arm"}))
    await ws.send(json.dumps({"type": "state", "yaw": 25, "pitch": 0}))
    for _ in range(30):
        await asyncio.sleep(0.03)
        await ws.send(json.dumps({"type": "ping"}))
        if gclient.sim.state.yaw_speed:
            break
    assert gclient.sim.state.yaw_speed != 0

    await ws.close()

    for _ in range(60):
        await asyncio.sleep(0.03)
        if gclient.sim.state.yaw_speed == 0:
            break
    assert gclient.sim.state.yaw_speed == 0
    assert not gclient.ctrl.armed
    assert "WebSocket" in gclient.ctrl.stats.last_stop_reason


async def test_ws_silence_trips_watchdog(gclient):
    """The connection is up but the client is mute — the watchdog must still cut in."""
    import asyncio
    import json

    async with await _ws(gclient.ws_url) as ws:
        await ws.send(json.dumps({"type": "arm"}))
        await ws.send(json.dumps({"type": "state", "yaw": 25, "pitch": 0}))
        await asyncio.sleep(0.1)
        assert gclient.sim.state.yaw_speed != 0

        await asyncio.sleep(0.6)                    # silence, no ping
        assert gclient.ctrl.stats.watchdog_trips >= 1
        assert gclient.sim.state.yaw_speed == 0


async def test_ws_stop_message_disarms(gclient):
    import asyncio
    import json

    async with await _ws(gclient.ws_url) as ws:
        await ws.send(json.dumps({"type": "arm"}))
        await ws.send(json.dumps({"type": "state", "yaw": 25, "pitch": 0}))
        await asyncio.sleep(0.1)
        await ws.send(json.dumps({"type": "stop"}))
        await asyncio.sleep(0.15)
        assert not gclient.ctrl.armed
        assert gclient.sim.state.yaw_speed == 0


async def test_ws_pushes_status_with_attitude(gclient):
    import json

    async with await _ws(gclient.ws_url) as ws:
        for _ in range(80):
            msg = json.loads(await ws.recv())
            if msg["type"] == "status" and msg.get("telemetry", {}).get("attitude"):
                break
        assert msg["type"] == "status"
        assert "armed" in msg and "stats" in msg
        assert msg["telemetry"]["attitude"] is not None


async def test_http_stop_endpoint_stops_controller(gclient):
    import asyncio
    import json

    async with httpx.AsyncClient(base_url=gclient.base) as c:
        async with await _ws(gclient.ws_url) as ws:
            await ws.send(json.dumps({"type": "arm"}))
            await ws.send(json.dumps({"type": "state", "yaw": 25, "pitch": 0}))
            await asyncio.sleep(0.1)
            assert gclient.sim.state.yaw_speed != 0

            r = await c.post("/api/stop")
            assert r.json()["stopped"] is True
            await asyncio.sleep(0.15)
            assert gclient.sim.state.yaw_speed == 0


async def test_max_speed_endpoint_clamps(gclient):
    async with httpx.AsyncClient(base_url=gclient.base) as c:
        assert (await c.post("/api/gimbal/max-speed",
                             json={"max_speed": 999})).json()["max_speed"] == 63.5
        assert (await c.post("/api/gimbal/max-speed",
                             json={"max_speed": 5})).json()["max_speed"] == 5.0


async def test_gimbal_status_endpoint(gclient):
    async with httpx.AsyncClient(base_url=gclient.base) as c:
        b = (await c.get("/api/gimbal")).json()
        assert b["enabled"] and b["running"]
        assert b["tick_hz"] == 50.0
        assert b["telemetry"] is not None


async def test_gimbal_routes_503_when_disabled(client):
    """The app still runs without a gimbal — phases 1–2 work as before."""
    assert (await client.get("/api/gimbal")).json()["enabled"] is False
    assert (await client.post("/api/gimbal/max-speed",
                              json={"max_speed": 5})).status_code == 503


# --------------------------------------------------------------------------
# Camera (phase 3) — the API returns what was READ BACK, not what was sent
# --------------------------------------------------------------------------


@pytest.fixture
async def cclient():
    from c12ctl.services.camera import CameraService

    sim = C12Simulator(seed=31)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    camera = CameraService(link, interval=0.05, timeout=0.15, settle=0.02)
    app = create_app(link, Session(), camera=camera)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        c.sim, c.camera = sim, camera
        yield c
    await camera.close()
    await link.close()
    await sim.close()


async def test_camera_state_endpoint_reports_cached_values(cclient):
    b = (await cclient.post("/api/camera/refresh")).json()
    assert b["fields"]["palette"]["value"] == "WHITE_HOT"
    assert b["fields"]["sdcard"]["value"]["present"] is True
    assert (await cclient.get("/api/camera")).json()["enabled"] is True


async def test_camera_apply_returns_the_readback(cclient):
    r = await cclient.post("/api/camera/palette", json={"args": ["IRONBOW"]})
    assert r.status_code == 200
    b = r.json()
    assert b["ok"] is True and b["actual"] == "IRONBOW" and b["kind"] == "direct"
    assert cclient.sim.state.palette == "04"


async def test_camera_apply_without_args(cclient):
    b = (await cclient.post("/api/camera/record_start")).json()
    assert b["ok"] is True and b["actual"] is True


async def test_camera_apply_unknown_action_is_404(cclient):
    r = await cclient.post("/api/camera/set_ip")
    assert r.status_code == 404
    assert "allowlist" in r.json()["detail"].lower()


async def test_camera_apply_bad_argument_is_400(cclient):
    r = await cclient.post("/api/camera/palette", json={"args": ["NEON"]})
    assert r.status_code == 400
    assert cclient.sim.state.palette == "01", "no packet may leave the backend"


# --------------------------------------------------------------------------
# Session recording (phase 6)
# --------------------------------------------------------------------------


@pytest.fixture
async def sclient(tmp_path):
    from c12ctl.services.session import SessionRecorder

    sim = C12Simulator(seed=71)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    rec = SessionRecorder(link, root=tmp_path / "sessions")
    app = create_app(link, Session(), recorder=rec)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as c:
        c.sim, c.rec = sim, rec
        yield c
    await rec.close()
    await link.close()
    await sim.close()


async def test_session_start_stop_round_trip(sclient):
    import asyncio

    started = (await sclient.post("/api/session/start",
                                  json={"note": "round trip"})).json()
    assert started["recording"] is True
    assert (await sclient.get("/api/session")).json()["recording"] is True

    await sclient.post("/api/cmd/camera.snap")
    await asyncio.sleep(0.1)

    stopped = (await sclient.post("/api/session/stop")).json()
    assert stopped["recording"] is False
    assert stopped["stats"]["packets"] > 0

    body = (await sclient.get("/api/session")).json()
    assert body["recording"] is False
    assert [s["id"] for s in body["sessions"]] == [started["id"]]


async def test_session_summary_endpoint(sclient):
    import asyncio

    sid = (await sclient.post("/api/session/start")).json()["id"]
    await sclient.post("/api/cmd/read.model")
    await asyncio.sleep(0.1)
    await sclient.post("/api/session/stop")

    body = (await sclient.get("/api/session/" + sid)).json()
    assert body["kinds"]["packet"] > 0
    assert body["commands"]["MOD"]["tx"] >= 1


async def test_second_start_is_409(sclient):
    await sclient.post("/api/session/start")
    try:
        r = await sclient.post("/api/session/start")
        assert r.status_code == 409
        assert "already running" in r.json()["detail"]
    finally:
        await sclient.post("/api/session/stop")


async def test_unknown_session_is_404(sclient):
    assert (await sclient.get("/api/session/20990101T000000")).status_code == 404


@pytest.mark.parametrize(
    "sid",
    [
        "%2e%2e",                       # ".."
        "%2e%2e%2f%2e%2e",              # "../.."
        "%2e%2e%2fetc%2fpasswd",        # "../etc/passwd"
        "%2fetc%2fpasswd",              # "/etc/passwd"
        "x" * 65,                       # longer than any real id
        "a b",                          # a space
    ],
)
async def test_session_id_cannot_escape_the_recording_root(sclient, sid):
    """The id arrives from a URL and is used to build a filesystem path.

    Traversal attempts are percent-encoded on purpose: a bare ``../..`` is
    normalised away by the HTTP client before it is ever sent, so testing that
    form would test httpx rather than this app.

    Two layers may reject: the router never matches an id containing a slash
    (404), and this app's own guard rejects the rest (400). What must never
    happen is a 200 serving something from outside the recording root.
    """
    r = await sclient.get("/api/session/" + sid)
    assert r.status_code in (400, 404), sid


@pytest.mark.parametrize("sid", ["%2e%2e", "%2e", "x" * 65, "a b", "a$b"])
async def test_malformed_session_id_is_refused_by_the_guard(sclient, sid):
    """The ids that do reach the handler must be refused by name, not by luck."""
    r = await sclient.get("/api/session/" + sid)
    assert r.status_code == 400, sid
    assert "malformed" in r.json()["detail"]


async def test_session_routes_503_when_disabled(client):
    """The app runs without recording — earlier phases work as before."""
    assert (await client.get("/api/session")).json()["enabled"] is False
    assert (await client.post("/api/session/start")).status_code == 503
    assert (await client.get("/api/session/whatever")).status_code == 503


async def test_camera_routes_503_when_disabled(client):
    """The app runs without the camera cache — phases 1–2 work as before."""
    assert (await client.get("/api/camera")).json()["enabled"] is False
    assert (await client.post("/api/camera/refresh")).status_code == 503
    assert (await client.post("/api/camera/snap")).status_code == 503
