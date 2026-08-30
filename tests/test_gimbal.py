"""Vòng điều khiển gimbal — bộ test quan trọng nhất về an toàn của cả dự án.

Trọng tâm không phải "gimbal có quay không" mà là "gimbal có **dừng** không", ở
mọi ngả hỏng hóc nghĩ ra được.
"""

import asyncio

import pytest

from c12ctl.protocol.types import MAX_SPEED_DPS
from c12ctl.services.gimbal import (
    ZERO_REPEATS,
    ControlState,
    GimbalController,
    NotArmed,
)
from c12ctl.services.telemetry import TelemetryService
from c12ctl.sim.c12_sim import C12Simulator
from c12ctl.transport.udp_link import UdpLink

FAST_TICK = 0.02
FAST_WATCHDOG = 0.15


@pytest.fixture
async def rig():
    """Simulator + link + controller, nhịp nhanh để test không lê thê."""
    sim = C12Simulator(seed=11)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    ctrl = GimbalController(link, max_speed=30.0,
                            tick=FAST_TICK, watchdog=FAST_WATCHDOG)
    await ctrl.start()
    ctrl.sim, ctrl.link_ = sim, link
    yield ctrl
    await ctrl.close()
    await link.close()
    await sim.close()


async def drive(ctrl, yaw=20.0, pitch=0.0, seconds=0.3):
    """Giữ tốc độ có nhịp tim, đúng như trình duyệt sẽ làm."""
    ctrl.set_speed(yaw, pitch)
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        ctrl.heartbeat()


# --------------------------------------------------------------------------
# Cổng ARM
# --------------------------------------------------------------------------


async def test_speed_rejected_before_arm(rig):
    with pytest.raises(NotArmed, match="chưa ARM"):
        rig.set_speed(20, 0)
    await asyncio.sleep(0.1)
    assert rig.sim.state.yaw_speed == 0, "gói không được rời khỏi backend"
    assert rig.stats.rejected == 1


async def test_speed_accepted_after_arm(rig):
    rig.arm()
    await drive(rig, 20, 0, 0.2)
    assert rig.sim.state.yaw_speed != 0
    assert rig.sim.state.yaw > 0


async def test_arm_starts_from_zero_state(rig):
    """ARM không được kế thừa tốc độ cũ còn sót lại."""
    rig.arm()
    rig.set_speed(25, 25)
    rig.stop_all("test")
    rig.arm()
    assert rig.state.as_dict() == {"yaw": 0.0, "pitch": 0.0}


# --------------------------------------------------------------------------
# Năm ngả dừng khẩn
# --------------------------------------------------------------------------


async def test_stop_zeroes_a_moving_gimbal(rig):
    """Ngả 1 & 2: nút STOP / phím Space-Esc."""
    rig.arm()
    await drive(rig, 25, 0, 0.2)
    assert rig.sim.state.yaw_speed != 0

    rig.stop_all("nút STOP")
    await asyncio.sleep(0.15)
    assert rig.sim.state.yaw_speed == 0
    assert rig.sim.state.pitch_speed == 0
    assert not rig.armed, "STOP phải disarm luôn"


async def test_watchdog_stops_when_updates_dry_up(rig):
    """Ngả 4: không nhận cập nhật nào trong 500 ms (ở đây rút ngắn để test)."""
    rig.arm()
    rig.set_speed(25, 0)
    await asyncio.sleep(0.08)
    assert rig.sim.state.yaw_speed != 0

    await asyncio.sleep(FAST_WATCHDOG + 0.15)      # im lặng, không heartbeat
    assert rig.stats.watchdog_trips >= 1
    assert rig.sim.state.yaw_speed == 0
    assert not rig.armed
    assert "watchdog" in rig.stats.last_stop_reason


async def test_heartbeat_prevents_watchdog_trip(rig):
    """Giữ phím lâu không được bị watchdog cắt oan — đó là vai trò của nhịp tim."""
    rig.arm()
    await drive(rig, 25, 0, FAST_WATCHDOG * 4)
    assert rig.stats.watchdog_trips == 0
    assert rig.armed
    assert rig.sim.state.yaw_speed != 0


async def test_close_stops_gimbal(rig):
    """Ngả 5: tiến trình thoát."""
    rig.arm()
    await drive(rig, 25, 0, 0.15)
    assert rig.sim.state.yaw_speed != 0

    await rig.close()
    await asyncio.sleep(0.1)
    assert rig.sim.state.yaw_speed == 0


async def test_loop_error_triggers_stop(rig):
    """Ngả 5: exception thoát ra khỏi vòng cũng phải dừng gimbal."""
    rig.arm()
    await drive(rig, 25, 0, 0.1)

    def boom():
        raise RuntimeError("hỏng giả lập")

    rig._apply_soft_limits = lambda y, p: boom()
    await asyncio.sleep(0.2)

    assert rig.stats.stops >= 1
    assert "lỗi vòng điều khiển" in rig.stats.last_stop_reason
    assert not rig.armed
    await asyncio.sleep(0.1)
    assert rig.sim.state.yaw_speed == 0


async def test_stop_sends_zero_more_than_once(rig):
    """Gói UDP có thể mất — một gói 0 là không đủ."""
    sent = []
    rig.link.subscribe(lambda f: None)
    original = rig.link.send_frame
    rig.link.send_frame = lambda f, priority=False: (
        sent.append(f), original(f, priority=priority))[1]

    rig.arm()
    rig.stop_all("test")
    assert sent.count("#TPUG2wGSY005F") >= ZERO_REPEATS
    assert sent.count("#TPUG2wGSP0056") >= ZERO_REPEATS


async def test_stop_is_safe_to_call_repeatedly(rig):
    rig.arm()
    for _ in range(5):
        rig.stop_all("lặp")
    assert not rig.armed
    assert rig.sim.state.yaw_speed == 0


async def test_stop_survives_broken_link(rig):
    """stop_all phải chạy được cả khi link đã hỏng — nó bị gọi từ finally."""
    def broken(*a, **k):
        raise OSError("socket đã đóng")

    rig.link.send_frame = broken
    rig.stop_all("link hỏng")          # không được ném
    assert not rig.armed


# --------------------------------------------------------------------------
# Nhịp và keepalive
# --------------------------------------------------------------------------


async def test_loop_refreshes_at_target_rate(rig):
    rig.arm()
    before = rig.stats.packets
    await drive(rig, 20, 0, 0.4)
    sent = rig.stats.packets - before
    expected = 0.4 / FAST_TICK
    assert 0.5 * expected <= sent / 2 <= 1.5 * expected, sent


@pytest.mark.parametrize("hold_speed", [False, True])
async def test_keepalive_correct_under_both_gimbal_behaviours(hold_speed):
    """Mâu thuẫn duy nhất không phân xử được từ tài liệu.

    protocol.md nói gimbal tự dừng sau vài chục ms; bytecode nói nó giữ lệnh tới
    khi có lệnh mới. Vòng 20 Hz phải đúng ở CẢ HAI — đó là lý do chọn keepalive.
    """
    sim = C12Simulator(hold_speed=hold_speed, seed=3)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    ctrl = GimbalController(link, max_speed=30.0, tick=FAST_TICK,
                            watchdog=FAST_WATCHDOG)
    await ctrl.start()
    try:
        ctrl.arm()
        await drive(ctrl, 25, 0, 0.4)
        assert sim.state.yaw > 1, "hold_speed=%s: gimbal phải quay" % hold_speed

        ctrl.stop_all("test")
        await asyncio.sleep(0.15)
        assert sim.state.yaw_speed == 0, "hold_speed=%s: phải dừng" % hold_speed
    finally:
        await ctrl.close()
        await link.close()
        await sim.close()


async def test_idle_does_not_spam_packets(rig):
    """Đứng yên thì im — gửi vài gói 0 rồi thôi, không bắn 20 Hz mãi."""
    rig.arm()
    await asyncio.sleep(0.05)
    before = rig.stats.packets
    for _ in range(10):
        await asyncio.sleep(FAST_TICK)
        rig.heartbeat()
    assert rig.stats.packets - before <= 2 * ZERO_REPEATS + 2


async def test_zero_is_sent_after_release(rig):
    """Nhả phím: phải có gói 0 thật sự đi ra, không chỉ ngừng gửi."""
    rig.arm()
    await drive(rig, 25, 0, 0.15)
    rig.set_speed(0, 0)
    await asyncio.sleep(0.15)
    assert rig.sim.state.yaw_speed == 0


async def test_gsm_mode_halves_packet_count():
    sim = C12Simulator(seed=4)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    ctrl = GimbalController(link, max_speed=30.0, tick=FAST_TICK,
                            watchdog=FAST_WATCHDOG, use_gsm=True)
    await ctrl.start()
    try:
        ctrl.arm()
        before = ctrl.stats.packets
        await drive(ctrl, 20, 10, 0.3)
        ticks = 0.3 / FAST_TICK
        assert ctrl.stats.packets - before <= ticks * 1.5, "GSM phải là 1 gói/tick"
        assert sim.state.yaw_speed != 0 and sim.state.pitch_speed != 0
    finally:
        await ctrl.close()
        await link.close()
        await sim.close()


async def test_gsm_on_old_firmware_leaves_gimbal_still():
    """Firmware < 0.5 bỏ qua GSM. Đây là lý do mặc định KHÔNG bật GSM."""
    sim = C12Simulator(supports_gsm=False, seed=4)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    ctrl = GimbalController(link, max_speed=30.0, tick=FAST_TICK,
                            watchdog=FAST_WATCHDOG, use_gsm=True)
    await ctrl.start()
    try:
        ctrl.arm()
        await drive(ctrl, 20, 0, 0.2)
        assert sim.state.yaw_speed == 0
    finally:
        await ctrl.close()
        await link.close()
        await sim.close()


# --------------------------------------------------------------------------
# Giới hạn tốc độ
# --------------------------------------------------------------------------


async def test_speed_clamped_to_max(rig):
    rig.arm()
    state = rig.set_speed(999, -999)
    assert state.yaw == 30.0 and state.pitch == -30.0


async def test_max_speed_cannot_exceed_hardware_ceiling(rig):
    assert rig.set_max_speed(1000) == MAX_SPEED_DPS


async def test_lowering_max_speed_reclamps_current_state(rig):
    rig.arm()
    rig.set_speed(30, 30)
    rig.set_max_speed(5)
    assert rig.state.yaw == 5.0 and rig.state.pitch == 5.0


async def test_default_max_speed_is_conservative():
    from c12ctl.services.gimbal import DEFAULT_MAX_SPEED

    assert DEFAULT_MAX_SPEED == 10.0, "lần chạy đầu phải chậm có chủ ý"


async def test_speed_is_quantised_to_wire_resolution(rig):
    """Tốc độ trên dây là bội của 0.5 °/s — UI không nên hứa hẹn hơn thế."""
    rig.arm()
    assert rig.set_speed(3.7, 0).yaw == 3.5


# --------------------------------------------------------------------------
# Giới hạn mềm từ telemetry
# --------------------------------------------------------------------------


@pytest.fixture
async def rig_tel():
    sim = C12Simulator(seed=12)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    tel = TelemetryService(link, rate_hz=40, rearm_interval=0.2, stale_after=0.2)
    await tel.start()
    ctrl = GimbalController(link, max_speed=30.0, telemetry=tel,
                            tick=FAST_TICK, watchdog=FAST_WATCHDOG,
                            soft_limit=45.0)
    await ctrl.start()
    ctrl.sim, ctrl.tel = sim, tel
    yield ctrl
    await ctrl.close()
    await tel.close()
    await link.close()
    await sim.close()


async def test_telemetry_arrives_after_gaa(rig_tel):
    for _ in range(60):
        await asyncio.sleep(0.02)
        if rig_tel.tel.enabled:
            break
    assert rig_tel.tel.enabled and rig_tel.tel.fresh
    assert rig_tel.tel.attitude is not None


async def test_soft_limit_blocks_motion_past_the_edge(rig_tel):
    for _ in range(60):
        await asyncio.sleep(0.02)
        if rig_tel.tel.fresh:
            break
    rig_tel.sim.state.yaw = 60.0            # đã vượt soft limit 45°
    await asyncio.sleep(0.1)

    rig_tel.arm()
    await drive(rig_tel, 25, 0, 0.2)        # đẩy tiếp về phía dương
    assert rig_tel.stats.limit_trips >= 1
    assert rig_tel.sim.state.yaw_speed == 0


async def test_soft_limit_still_allows_retreat(rig_tel):
    """Chạm biên vẫn phải quay ngược ra được — nếu không thì kẹt cứng."""
    for _ in range(60):
        await asyncio.sleep(0.02)
        if rig_tel.tel.fresh:
            break
    rig_tel.sim.state.yaw = 60.0
    await asyncio.sleep(0.1)

    rig_tel.arm()
    await drive(rig_tel, -25, 0, 0.2)       # đi ngược lại
    assert rig_tel.sim.state.yaw_speed < 0


async def test_stale_telemetry_does_not_gate_motion(rig_tel):
    """Tư thế quá hạn thì KHÔNG chặn — an toàn giả còn nguy hơn không chặn."""
    for _ in range(60):
        await asyncio.sleep(0.02)
        if rig_tel.tel.fresh:
            break
    rig_tel.sim.state.yaw = 60.0
    await asyncio.sleep(0.1)

    # Làm telemetry CHẾT THẬT: dừng vòng gửi lại GAA và tắt push ở camera.
    # Chỉ vặn updated_at là vô ích — gói GAC kế tiếp sẽ làm mới nó ngay.
    rig_tel.tel._arm_task.cancel()
    rig_tel.sim.state.gaa_rate = 0
    for _ in range(60):
        await asyncio.sleep(0.02)
        if not rig_tel.tel.fresh:
            break
    assert not rig_tel.tel.fresh
    rig_tel.arm()
    trips = rig_tel.stats.limit_trips
    await drive(rig_tel, 25, 0, 0.15)
    assert rig_tel.stats.limit_trips == trips
    assert rig_tel.sim.state.yaw_speed != 0


async def test_no_telemetry_means_no_soft_limits(rig):
    """Không có telemetry vẫn điều khiển được, chỉ là mất lớp bảo vệ."""
    assert rig.telemetry is None
    rig.arm()
    await drive(rig, 25, 0, 0.15)
    assert rig.sim.state.yaw_speed != 0
    assert rig.stats.limit_trips == 0


# --------------------------------------------------------------------------
# Báo cáo trạng thái
# --------------------------------------------------------------------------


async def test_status_dict_is_complete(rig):
    d = rig.as_dict()
    assert d["armed"] is False and d["running"] is True
    assert d["tick_hz"] == pytest.approx(1 / FAST_TICK, rel=0.01)
    assert d["speed_ceiling"] == MAX_SPEED_DPS
    assert "stats" in d and "watchdog_trips" in d["stats"]


def test_control_state_moving_flag():
    assert not ControlState().moving
    assert ControlState(yaw=1).moving
    assert ControlState(pitch=-1).moving
