"""Integration test: UdpLink talking to C12Simulator over a real socket.

It runs on loopback so it is still fast, but it goes through exactly the
encoding / socket / frame-splitting / demux path that real hardware will take.
"""

import asyncio

import pytest

from c12ctl.protocol.registry import COMMANDS, CommandNotAllowed
from c12ctl.protocol.types import Attitude, Palette, RiskLevel
from c12ctl.sim.c12_sim import SPEED_HOLD_TIMEOUT, C12Simulator
from c12ctl.transport.udp_link import PortBusyError, UdpLink


@pytest.fixture
async def sim():
    s = C12Simulator(seed=1234)
    await s.start("127.0.0.1", 0)
    yield s
    await s.close()


@pytest.fixture
async def link(sim):
    lk = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await lk.start()
    yield lk
    await lk.close()


# --------------------------------------------------------------------------
# The read path
# --------------------------------------------------------------------------


async def test_read_version(link):
    assert await link.request("read.version") is not None


async def test_read_model(link):
    assert await link.request("read.model") == "0C"


async def test_read_recording_state_roundtrip(link, sim):
    assert await link.request("read.recording") is False
    link.send("camera.record_start")
    await asyncio.sleep(0.05)
    assert sim.state.recording is True
    assert await link.request("read.recording") is True


async def test_read_zoom_reflects_writes(link, sim):
    for _ in range(3):
        link.send("camera.zoom_in")
    await asyncio.sleep(0.05)
    assert await link.request("read.zoom") == 3
    link.send("camera.zoom_out")
    await asyncio.sleep(0.05)
    assert await link.request("read.zoom") == 2


async def test_read_palette_decodes_to_name(link):
    link.send("camera.palette", Palette.BLACK_HOT)
    await asyncio.sleep(0.05)
    assert await link.request("read.palette") == "BLACK_HOT"


async def test_read_sdcard_present(link):
    sd = await link.request("read.sdcard")
    assert sd.present
    assert sd.total_mb > sd.free_mb > 0


async def test_unsupported_read_times_out_and_returns_none(link):
    """An unsupported camera command stays SILENT. That silence is the data phase 1
    needs."""
    assert await link.request("read.ranging", timeout=0.1) is None
    assert link.stats.timeouts == 1


async def test_diagnostics_sweep_reports_which_commands_are_alive(link):
    """The capability map — which commands are alive and which stay silent."""
    alive, silent = [], []
    for cmd in COMMANDS.values():
        if cmd.risk is not RiskLevel.SAFE:
            continue
        result = await link.request(cmd, timeout=0.1)
        (alive if result is not None else silent).append(cmd.name)

    assert "read.version" in alive
    assert "read.zoom" in alive
    assert "read.ranging" in silent      # the bytecode says C13/C14 only
    assert alive and silent


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------


async def test_link_rejects_unknown_command(link):
    with pytest.raises(CommandNotAllowed):
        await link.request("camera.set_ip")


async def test_link_rejects_command_not_from_registry(link):
    """You cannot forge a Command and slip it through the link — the allowlist has
    no exceptions."""
    from dataclasses import replace

    forged = replace(COMMANDS["camera.snap"], cmd3="RST", data="01")
    with pytest.raises(CommandNotAllowed):
        link.send(forged)


# --------------------------------------------------------------------------
# Gimbal
# --------------------------------------------------------------------------


async def test_speed_command_moves_simulated_gimbal(link, sim):
    link.send("gimbal.yaw_speed", 20, priority=True)
    await asyncio.sleep(0.1)
    assert sim.state.yaw > 0


async def test_gimbal_auto_stops_without_keepalive(sim, link):
    """The simulator's default behaviour: stop when packets stop arriving."""
    link.send("gimbal.yaw_speed", 20, priority=True)
    await asyncio.sleep(SPEED_HOLD_TIMEOUT + 0.1)
    assert sim.state.yaw_speed == 0


async def test_keepalive_keeps_it_moving():
    """The 20 Hz loop is a superset of both behaviours — correct in either mode."""
    for hold in (False, True):
        s = C12Simulator(hold_speed=hold, seed=1)
        await s.start("127.0.0.1", 0)
        lk = UdpLink("127.0.0.1", s.port, local_port=0, min_tx_gap=0.001)
        await lk.start()
        try:
            for _ in range(10):                       # 10 ticks × 50 ms
                lk.send("gimbal.yaw_speed", 20, priority=True)
                await asyncio.sleep(0.05)
            assert s.state.yaw > 5, "hold_speed=%s" % hold
            lk.send("gimbal.yaw_speed", 0, priority=True)
            await asyncio.sleep(0.05)
            assert s.state.yaw_speed == 0, "hold_speed=%s" % hold
        finally:
            await lk.close()
            await s.close()


async def test_goto_reaches_target(link, sim):
    link.send("gimbal.goto_yaw", 30)
    for _ in range(100):
        await asyncio.sleep(0.02)
        if sim.state.goto_yaw is None:
            break
    assert sim.state.yaw == pytest.approx(30.0, abs=0.5)


async def test_gsm_falls_silent_on_old_firmware():
    s = C12Simulator(supports_gsm=False, seed=1)
    await s.start("127.0.0.1", 0)
    lk = UdpLink("127.0.0.1", s.port, local_port=0, min_tx_gap=0.001)
    await lk.start()
    try:
        lk.send("gimbal.speed", 20, 10, priority=True)
        await asyncio.sleep(0.08)
        assert s.state.yaw_speed == 0, "GSM must be ignored on firmware < 0.5"
    finally:
        await lk.close()
        await s.close()


async def test_akey_center_returns_to_zero(link, sim):
    sim.state.yaw, sim.state.pitch = 40.0, -20.0
    link.send("gimbal.akey", "center")
    for _ in range(100):
        await asyncio.sleep(0.02)
        if sim.state.goto_yaw is None and sim.state.goto_pitch is None:
            break
    assert sim.state.yaw == pytest.approx(0, abs=0.5)
    assert sim.state.pitch == pytest.approx(0, abs=0.5)


# --------------------------------------------------------------------------
# Telemetry — the thing protocol.md wrongly concluded does not exist
# --------------------------------------------------------------------------


async def test_attitude_push_is_off_by_default(link, sim):
    """Being off by default is exactly why "no telemetry" was a false negative."""
    seen = []
    link.subscribe(lambda f: seen.append(f) if f.cmd3 == "GAC" else None)
    await asyncio.sleep(0.2)
    assert seen == []


async def test_attitude_push_after_gaa(link, sim):
    seen: list = []
    link.subscribe(lambda f: seen.append(f) if f.cmd3 == "GAC" else None)
    link.send("telemetry.push_attitude", 20)
    await asyncio.sleep(0.3)
    assert len(seen) >= 3, "GAC frames must arrive once GAA is enabled"

    att = Attitude.from_data(seen[-1].data)
    assert att.yaw == pytest.approx(sim.state.yaw, abs=0.05)


async def test_attitude_tracks_motion(link, sim):
    seen: list = []
    link.subscribe(lambda f: seen.append(Attitude.from_data(f.data))
                   if f.cmd3 == "GAC" else None)
    link.send("telemetry.push_attitude", 20)
    await asyncio.sleep(0.05)
    for _ in range(6):
        link.send("gimbal.yaw_speed", 30, priority=True)
        await asyncio.sleep(0.05)
    link.send("gimbal.yaw_speed", 0, priority=True)
    await asyncio.sleep(0.05)
    assert seen[-1].yaw > seen[0].yaw + 1


async def test_gaa_ignored_before_video(sim, link):
    """Vendor constraint: GAA only takes effect once the camera has video."""
    sim.state.has_video = False
    link.send("telemetry.push_attitude", 20)
    await asyncio.sleep(0.1)
    assert sim.state.gaa_rate == 0


# --------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------


async def test_survives_packet_loss():
    """30% packet loss: timeouts are fine, but it must not hang or raise."""
    s = C12Simulator(chaos_loss=0.3, seed=7)
    await s.start("127.0.0.1", 0)
    lk = UdpLink("127.0.0.1", s.port, local_port=0, min_tx_gap=0.001)
    await lk.start()
    try:
        results = [await lk.request("read.model", timeout=0.15) for _ in range(20)]
        assert any(r is not None for r in results), "losing every packet is abnormal"
        assert any(r is None for r in results), "chaos-loss 0.3 and nothing was lost?"
        assert all(r in (None, "0C") for r in results)
    finally:
        await lk.close()
        await s.close()


async def test_survives_garbage_prefix():
    """Junk bytes before a reply must not cost us the valid frame behind them."""
    s = C12Simulator(chaos_garbage=1.0, seed=3)
    await s.start("127.0.0.1", 0)
    lk = UdpLink("127.0.0.1", s.port, local_port=0, min_tx_gap=0.001)
    await lk.start()
    try:
        assert await lk.request("read.model") == "0C"
    finally:
        await lk.close()
        await s.close()


async def test_simulator_rejects_bad_checksum():
    s = C12Simulator(seed=1)
    await s.start("127.0.0.1", 0)
    try:
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=("127.0.0.1", s.port)
        )
        transport.sendto(b"#TPUD2wCAP0100\r\n")   # bad checksum
        await asyncio.sleep(0.05)
        transport.close()
        assert s.bad_checksum == 1
        assert s.rx_count == 0
    finally:
        await s.close()


# --------------------------------------------------------------------------
# The priority queue and dry-run
# --------------------------------------------------------------------------


async def test_priority_queue_jumps_ahead(sim):
    """A gimbal speed packet must not queue behind a batch of read commands."""
    lk = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.02)
    await lk.start()
    order: list[str] = []
    try:
        for _ in range(5):
            lk.send("read.version")
        lk.send("gimbal.yaw_speed", 20, priority=True)

        original = lk._transport.sendto
        lk._transport.sendto = lambda data, addr: (          # type: ignore[method-assign]
            order.append(data.decode(errors="replace").strip()), original(data, addr)
        )[1]
        await asyncio.sleep(0.2)
        gsy = next(i for i, f in enumerate(order) if "GSY" in f)
        vers = [i for i, f in enumerate(order) if "VER" in f]
        assert gsy < max(vers), "a priority command must jump ahead of the normal queue"
    finally:
        await lk.close()


async def test_dry_run_opens_no_socket_and_sends_nothing(sim):
    lk = UdpLink("127.0.0.1", sim.port, local_port=0, dry_run=True, min_tx_gap=0.001)
    await lk.start()
    try:
        lk.send("camera.snap")
        await asyncio.sleep(0.05)
        assert lk.stats.tx == 1
        assert sim.rx_count == 0, "dry-run must never touch the network"
    finally:
        await lk.close()


async def test_journal_records_both_directions(tmp_path, sim):
    import json

    path = tmp_path / "packets.jsonl"
    lk = UdpLink("127.0.0.1", sim.port, local_port=0,
                 log_path=path, min_tx_gap=0.001)
    await lk.start()
    try:
        await lk.request("read.model")
    finally:
        await lk.close()

    records = [json.loads(line) for line in path.read_text().splitlines()]
    dirs = [r["dir"] for r in records]
    assert "tx" in dirs and "rx" in dirs
    assert any(r.get("cmd3") == "MOD" for r in records)
    assert all("mono" in r and "t" in r for r in records)


async def test_port_busy_gives_actionable_error(sim):
    """A busy port 5000 is the number one operational failure — the message must
    say how to fix it."""
    busy = UdpLink("127.0.0.1", sim.port, local_port=sim.port)
    with pytest.raises(PortBusyError, match="ground station"):
        await busy.start()
