"""The gimbal control loop — the project's most safety-critical test module.

The focus is not "does the gimbal move" but "does the gimbal **stop**", down
every failure path we could think of.
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
    """Simulator + link + controller, fast cadence so the tests do not drag."""
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
    """Hold a speed with heartbeats, exactly as the browser does."""
    ctrl.set_speed(yaw, pitch)
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        ctrl.heartbeat()


# --------------------------------------------------------------------------
# The ARM gate
# --------------------------------------------------------------------------


async def test_speed_rejected_before_arm(rig):
    with pytest.raises(NotArmed, match="not armed"):
        rig.set_speed(20, 0)
    await asyncio.sleep(0.1)
    assert rig.sim.state.yaw_speed == 0, "no packet may leave the backend"
    assert rig.stats.rejected == 1


async def test_speed_accepted_after_arm(rig):
    rig.arm()
    await drive(rig, 20, 0, 0.2)
    assert rig.sim.state.yaw_speed != 0
    assert rig.sim.state.yaw > 0


async def test_arm_starts_from_zero_state(rig):
    """ARM must not inherit a leftover speed."""
    rig.arm()
    rig.set_speed(25, 25)
    rig.stop_all("test")
    rig.arm()
    assert rig.state.as_dict() == {"yaw": 0.0, "pitch": 0.0}


# --------------------------------------------------------------------------
# The five emergency-stop paths
# --------------------------------------------------------------------------


async def test_stop_zeroes_a_moving_gimbal(rig):
    """Paths 1 & 2: the STOP button / the Space-Esc keys."""
    rig.arm()
    await drive(rig, 25, 0, 0.2)
    assert rig.sim.state.yaw_speed != 0

    rig.stop_all("STOP button")
    await asyncio.sleep(0.15)
    assert rig.sim.state.yaw_speed == 0
    assert rig.sim.state.pitch_speed == 0
    assert not rig.armed, "STOP must disarm as well"


async def test_watchdog_stops_when_updates_dry_up(rig):
    """Path 4: no update within 500 ms (shortened here for the test)."""
    rig.arm()
    rig.set_speed(25, 0)
    await asyncio.sleep(0.08)
    assert rig.sim.state.yaw_speed != 0

    await asyncio.sleep(FAST_WATCHDOG + 0.15)      # silence, no heartbeat
    assert rig.stats.watchdog_trips >= 1
    assert rig.sim.state.yaw_speed == 0
    assert not rig.armed
    assert "watchdog" in rig.stats.last_stop_reason


async def test_heartbeat_prevents_watchdog_trip(rig):
    """Holding a key must not trip the watchdog — that is what the heartbeat is for."""
    rig.arm()
    await drive(rig, 25, 0, FAST_WATCHDOG * 4)
    assert rig.stats.watchdog_trips == 0
    assert rig.armed
    assert rig.sim.state.yaw_speed != 0


async def test_close_stops_gimbal(rig):
    """Path 5: process exit."""
    rig.arm()
    await drive(rig, 25, 0, 0.15)
    assert rig.sim.state.yaw_speed != 0

    await rig.close()
    await asyncio.sleep(0.1)
    assert rig.sim.state.yaw_speed == 0


async def test_loop_error_triggers_stop(rig):
    """Path 5: an exception escaping the loop must stop the gimbal too."""
    rig.arm()
    await drive(rig, 25, 0, 0.1)

    def boom():
        raise RuntimeError("simulated failure")

    rig._apply_soft_limits = lambda y, p: boom()
    await asyncio.sleep(0.2)

    assert rig.stats.stops >= 1
    assert "control loop error" in rig.stats.last_stop_reason
    assert not rig.armed
    await asyncio.sleep(0.1)
    assert rig.sim.state.yaw_speed == 0


async def test_stop_sends_zero_more_than_once(rig):
    """UDP packets can be lost — a single zero packet is not enough."""
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
        rig.stop_all("repeated")
    assert not rig.armed
    assert rig.sim.state.yaw_speed == 0


async def test_stop_survives_broken_link(rig):
    """stop_all must work even with a broken link — it is called from finally."""
    def broken(*a, **k):
        raise OSError("socket is closed")

    rig.link.send_frame = broken
    rig.stop_all("broken link")        # must not raise
    assert not rig.armed


# --------------------------------------------------------------------------
# Cadence and keepalive
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
    """The one disagreement the documents cannot settle.

    protocol.md says the gimbal stops itself after a few tens of ms; the bytecode
    says it holds the command until a new one arrives. The 20 Hz loop must be
    correct under BOTH — which is why we keepalive.
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
        assert sim.state.yaw > 1, "hold_speed=%s: the gimbal must turn" % hold_speed

        ctrl.stop_all("test")
        await asyncio.sleep(0.15)
        assert sim.state.yaw_speed == 0, "hold_speed=%s: it must stop" % hold_speed
    finally:
        await ctrl.close()
        await link.close()
        await sim.close()


async def test_idle_does_not_spam_packets(rig):
    """Standing still means quiet — a few zero packets, then nothing, not 20 Hz forever."""
    rig.arm()
    await asyncio.sleep(0.05)
    before = rig.stats.packets
    for _ in range(10):
        await asyncio.sleep(FAST_TICK)
        rig.heartbeat()
    assert rig.stats.packets - before <= 2 * ZERO_REPEATS + 2


async def test_zero_is_sent_after_release(rig):
    """Key released: a real zero packet must go out, not merely a stop in sending."""
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
        assert ctrl.stats.packets - before <= ticks * 1.5, "GSM must be 1 packet per tick"
        assert sim.state.yaw_speed != 0 and sim.state.pitch_speed != 0
    finally:
        await ctrl.close()
        await link.close()
        await sim.close()


async def test_gsm_on_old_firmware_leaves_gimbal_still():
    """Firmware < 0.5 ignores GSM. This is why GSM is NOT on by default."""
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
# Speed limits
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

    assert DEFAULT_MAX_SPEED == 10.0, "the first run must be deliberately slow"


async def test_speed_is_quantised_to_wire_resolution(rig):
    """Wire speed comes in 0.5 °/s steps — the UI should not promise more."""
    rig.arm()
    assert rig.set_speed(3.7, 0).yaw == 3.5


# --------------------------------------------------------------------------
# Soft limits driven by telemetry
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
    rig_tel.sim.state.yaw = 60.0            # already past the 45° soft limit
    await asyncio.sleep(0.1)

    rig_tel.arm()
    await drive(rig_tel, 25, 0, 0.2)        # keep pushing in the positive direction
    assert rig_tel.stats.limit_trips >= 1
    assert rig_tel.sim.state.yaw_speed == 0


async def test_soft_limit_still_allows_retreat(rig_tel):
    """Reaching the edge must still allow retreating — otherwise it is stuck."""
    for _ in range(60):
        await asyncio.sleep(0.02)
        if rig_tel.tel.fresh:
            break
    rig_tel.sim.state.yaw = 60.0
    await asyncio.sleep(0.1)

    rig_tel.arm()
    await drive(rig_tel, -25, 0, 0.2)       # drive back the other way
    assert rig_tel.sim.state.yaw_speed < 0


async def test_stale_telemetry_does_not_gate_motion(rig_tel):
    """A stale attitude must NOT gate — false safety is worse than no gating."""
    for _ in range(60):
        await asyncio.sleep(0.02)
        if rig_tel.tel.fresh:
            break
    rig_tel.sim.state.yaw = 60.0
    await asyncio.sleep(0.1)

    # Kill telemetry FOR REAL: stop the GAA re-arm loop and turn the push off
    # at the camera. Just tampering with updated_at is useless — the next GAC
    # frame would refresh it immediately.
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
    """Control still works without telemetry; only the protective layer is lost."""
    assert rig.telemetry is None
    rig.arm()
    await drive(rig, 25, 0, 0.15)
    assert rig.sim.state.yaw_speed != 0
    assert rig.stats.limit_trips == 0


# --------------------------------------------------------------------------
# State reporting
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
