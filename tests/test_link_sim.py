"""Integration test: UdpLink nói chuyện với C12Simulator qua socket thật.

Chạy trên loopback nên vẫn nhanh, nhưng đi qua đúng đường mã hoá / socket /
tách khung / demux mà phần cứng thật sẽ đi.
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
# Đường đọc
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
    """Lệnh camera không hỗ trợ thì IM LẶNG. Đó chính là dữ liệu pha 1 cần."""
    assert await link.request("read.ranging", timeout=0.1) is None
    assert link.stats.timeouts == 1


async def test_diagnostics_sweep_reports_which_commands_are_alive(link):
    """Bản đồ năng lực — lệnh nào sống, lệnh nào im lặng."""
    alive, silent = [], []
    for cmd in COMMANDS.values():
        if cmd.risk is not RiskLevel.SAFE:
            continue
        result = await link.request(cmd, timeout=0.1)
        (alive if result is not None else silent).append(cmd.name)

    assert "read.version" in alive
    assert "read.zoom" in alive
    assert "read.ranging" in silent      # bytecode ghi chỉ C13/C14
    assert alive and silent


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------


async def test_link_rejects_unknown_command(link):
    with pytest.raises(CommandNotAllowed):
        await link.request("camera.set_ip")


async def test_link_rejects_command_not_from_registry(link):
    """Không dựng được Command rồi tuồn qua link — allowlist không có ngoại lệ."""
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
    """Hành vi mặc định của simulator: dừng khi ngừng nhận gói."""
    link.send("gimbal.yaw_speed", 20, priority=True)
    await asyncio.sleep(SPEED_HOLD_TIMEOUT + 0.1)
    assert sim.state.yaw_speed == 0


async def test_keepalive_keeps_it_moving():
    """Vòng 20 Hz là tập cha của cả hai hành vi — nó đúng ở cả hai chế độ."""
    for hold in (False, True):
        s = C12Simulator(hold_speed=hold, seed=1)
        await s.start("127.0.0.1", 0)
        lk = UdpLink("127.0.0.1", s.port, local_port=0, min_tx_gap=0.001)
        await lk.start()
        try:
            for _ in range(10):                       # 10 tick × 50 ms
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
        assert s.state.yaw_speed == 0, "GSM phải bị bỏ qua khi firmware < 0.5"
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
# Telemetry — thứ mà protocol.md kết luận nhầm là không tồn tại
# --------------------------------------------------------------------------


async def test_attitude_push_is_off_by_default(link, sim):
    """Chính vì mặc định tắt mà quan sát 'không có telemetry' cho âm tính giả."""
    seen = []
    link.subscribe(lambda f: seen.append(f) if f.cmd3 == "GAC" else None)
    await asyncio.sleep(0.2)
    assert seen == []


async def test_attitude_push_after_gaa(link, sim):
    seen: list = []
    link.subscribe(lambda f: seen.append(f) if f.cmd3 == "GAC" else None)
    link.send("telemetry.push_attitude", 20)
    await asyncio.sleep(0.3)
    assert len(seen) >= 3, "phải nhận được gói GAC sau khi bật GAA"

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
    """Ràng buộc của hãng: GAA chỉ hiệu lực sau khi camera đã ra hình."""
    sim.state.has_video = False
    link.send("telemetry.push_attitude", 20)
    await asyncio.sleep(0.1)
    assert sim.state.gaa_rate == 0


# --------------------------------------------------------------------------
# Bền bỉ
# --------------------------------------------------------------------------


async def test_survives_packet_loss():
    """Mất 30% gói: có thể timeout, nhưng không được treo hay ném exception."""
    s = C12Simulator(chaos_loss=0.3, seed=7)
    await s.start("127.0.0.1", 0)
    lk = UdpLink("127.0.0.1", s.port, local_port=0, min_tx_gap=0.001)
    await lk.start()
    try:
        results = [await lk.request("read.model", timeout=0.15) for _ in range(20)]
        assert any(r is not None for r in results), "mất hết gói là bất thường"
        assert any(r is None for r in results), "chaos-loss 0.3 mà không mất gói nào?"
        assert all(r in (None, "0C") for r in results)
    finally:
        await lk.close()
        await s.close()


async def test_survives_garbage_prefix():
    """Byte rác trước phản hồi không được làm mất khung hợp lệ đi sau nó."""
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
        transport.sendto(b"#TPUD2wCAP0100\r\n")   # checksum sai
        await asyncio.sleep(0.05)
        transport.close()
        assert s.bad_checksum == 1
        assert s.rx_count == 0
    finally:
        await s.close()


# --------------------------------------------------------------------------
# Hàng ưu tiên và dry-run
# --------------------------------------------------------------------------


async def test_priority_queue_jumps_ahead(sim):
    """Gói tốc độ gimbal không được xếp sau một loạt lệnh đọc."""
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
        assert gsy < max(vers), "lệnh ưu tiên phải vượt lên trước hàng thường"
    finally:
        await lk.close()


async def test_dry_run_opens_no_socket_and_sends_nothing(sim):
    lk = UdpLink("127.0.0.1", sim.port, local_port=0, dry_run=True, min_tx_gap=0.001)
    await lk.start()
    try:
        lk.send("camera.snap")
        await asyncio.sleep(0.05)
        assert lk.stats.tx == 1
        assert sim.rx_count == 0, "dry-run không được chạm tới mạng"
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
    """Cổng 5000 bị chiếm là lỗi vận hành số một — thông báo phải chỉ được cách sửa."""
    busy = UdpLink("127.0.0.1", sim.port, local_port=sim.port)
    with pytest.raises(PortBusyError, match="ground station"):
        await busy.start()
