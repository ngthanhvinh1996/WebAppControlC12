"""Phase 3 — camera writes must be confirmable by a read.

The focus is not "was the packet sent" (phase 0 covers that) but **"what does the
read-back see"**. C12 write commands have no reply, so the three situations below
look identical from the sending side and must be told apart by reading back:

* the command took effect → ``ok=True``
* the packet arrived but the firmware ignored it → ``ok=False``
* there is no way to know (silent read, no card inserted) → ``ok=None``

The three simulator subclasses at the top of this file model exactly those three
situations.
"""

import asyncio

import pytest

from c12ctl.protocol.registry import COMMANDS, CommandNotAllowed
from c12ctl.services.camera import DEAD_AFTER, WRITES, CameraService
from c12ctl.sim.c12_sim import C12Simulator
from c12ctl.transport.udp_link import UdpLink


class QuietRead(C12Simulator):
    """A camera that answers everything except a few reads — firmware missing them."""

    def __init__(self, *a, quiet=(), **kw):
        super().__init__(*a, **kw)
        self.quiet = set(quiet)

    def _handle_read(self, cmd, data):
        if cmd in self.quiet:
            return None
        return super()._handle_read(cmd, data)


class DeafWrite(C12Simulator):
    """The write packet arrives but has no effect — the worst case."""

    def __init__(self, *a, ignore=(), **kw):
        super().__init__(*a, **kw)
        self.ignore = set(ignore)

    def handle(self, frame):
        if frame.rw == "w" and frame.cmd3 in self.ignore:
            return None
        return super().handle(frame)


class SlowRecord(C12Simulator):
    """REC really does take effect, but the read-back only sees it a few beats later.

    A real camera has to open a file on the card before the state changes —
    reading back once and concluding would be wrong.
    """

    lag = 2

    def _handle_read(self, cmd, data):
        if cmd == "REC" and self.state.recording and self.lag > 0:
            self.lag -= 1
            return self._reply("REC", "00")
        return super()._handle_read(cmd, data)


async def _rig(sim_obj):
    await sim_obj.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim_obj.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    svc = CameraService(link, interval=0.05, timeout=0.15, settle=0.02)
    return sim_obj, link, svc


@pytest.fixture
async def rig():
    """Simulator + link + service, with no background poll loop (tests drive it)."""
    sim, link, svc = await _rig(C12Simulator(seed=7))

    class Rig:
        pass

    r = Rig()
    r.sim, r.link, r.svc = sim, link, svc
    yield r
    await svc.close()
    await link.close()
    await sim.close()


# --------------------------------------------------------------------------
# The verification table
# --------------------------------------------------------------------------


def test_every_camera_write_is_verifiable():
    """The phase 3 invariant: no camera write escapes the verification table.

    Add a write command to the registry and forget to declare how it is read
    back, and this test breaks — which is the point.
    """
    writes = {n for n, c in COMMANDS.items()
              if n.startswith("camera.") and c.rw == "w"}
    assert writes == set(WRITES), "a camera write has no declared verification"


def test_verify_table_points_at_real_read_commands():
    for name, spec in WRITES.items():
        assert spec.read in COMMANDS, "%s points at a missing read command" % name
        assert COMMANDS[spec.read].rw == "r"
        assert COMMANDS[spec.read].expect_reply is True


# --------------------------------------------------------------------------
# State cache
# --------------------------------------------------------------------------


async def test_poll_fills_cache_from_camera(rig):
    await rig.svc.poll_once(force=True)
    f = rig.svc.fields
    assert f["model"].value == "0C"
    assert f["recording"].value is False
    assert f["zoom"].value == 0
    assert f["palette"].value == "WHITE_HOT"
    assert f["resolution"].value == "R_1080P"
    assert f["sdcard"].value.present is True
    assert f["thermal_brightness"].value == 50
    assert all(x.supported is True for x in f.values())


async def test_cache_keeps_raw_data_for_unknown_formats(rig):
    """The raw ``SDC`` format is unsettled — the cache must keep the original string."""
    await rig.svc.poll_once(force=True)
    sd = rig.svc.fields["sdcard"]
    assert sd.raw and len(sd.raw) == 12
    assert sd.as_dict()["value"]["total_mb"] == rig.sim.state.sd_total_mb


async def test_static_fields_are_read_once(rig):
    await rig.svc.poll_once(force=True)
    assert rig.svc.fields["model"].replies == 1
    for _ in range(3):
        await asyncio.sleep(0.06)
        await rig.svc.poll_once()
    assert rig.svc.fields["model"].replies == 1, "the model never changes, do not re-ask"
    assert rig.svc.fields["recording"].replies > 1


async def test_silent_field_backs_off_instead_of_jamming_the_poll():
    """A nonexistent read must be spaced out: each probe costs a full timeout."""
    sim, link, svc = await _rig(QuietRead(seed=3, quiet={"VID"}))
    svc.dead_after, svc.dead_retry = 2, 30.0
    try:
        for _ in range(svc.dead_after):
            await svc.poll_once(force=True)
        vid = svc.fields["resolution"]
        assert vid.supported is False
        assert vid.silent_streak == svc.dead_after

        checked = vid.checked_at
        await asyncio.sleep(0.06)
        await svc.poll_once()
        assert vid.checked_at == checked, "a dead field is still probed every cycle"
        assert svc.fields["palette"].checked_at > checked, "live fields must still poll"
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_poll_rests_while_the_link_is_saturated(rig):
    """At 20 Hz the priority queue owns the send slots — the poll must yield."""
    await rig.svc.poll_once(force=True)
    checked = rig.svc.fields["palette"].checked_at

    rig.svc._busy = lambda: True
    assert await rig.svc.poll_once() == []
    assert rig.svc.fields["palette"].checked_at == checked
    assert rig.svc.stats.skipped == 1

    # "Refresh" is an explicit operator request — it must still go through.
    await rig.svc.poll_once(force=True)
    assert rig.svc.fields["palette"].checked_at > checked


async def test_silence_under_saturation_is_not_evidence():
    """Silence while the link is saturated is about traffic, not about firmware.

    Without separating the two, a few seconds of gimbal motion would mark the
    whole camera state table "unsupported" — and silence it for 30 seconds under
    the back-off rule.
    """
    sim, link, svc = await _rig(QuietRead(seed=29, quiet={"IMG"}))
    svc._busy = lambda: True
    try:
        for _ in range(DEAD_AFTER + 2):
            await svc.poll_once(force=True)
        pal = svc.fields["palette"]
        assert pal.silent_streak == 0
        assert pal.supported is None, "still undecided, not 'unsupported'"
        assert svc.stats.silent >= DEAD_AFTER
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_unverifiable_write_names_the_busy_link():
    """Failing to verify while the gimbal turns must state the real reason."""
    sim, link, svc = await _rig(QuietRead(seed=37, quiet={"IMG"}))
    svc._busy = lambda: True
    try:
        r = await svc.apply("palette", "SEPIA")
        assert r.ok is None
        assert "gimbal" in r.note and "read.palette" in r.note
        assert sim.state.palette == "03", "the command is still sent"
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_background_loop_refreshes_state(rig):
    await rig.svc.start()
    rig.link.send("camera.record_start")
    for _ in range(40):
        await asyncio.sleep(0.05)
        if rig.svc.fields["recording"].value is True:
            break
    assert rig.svc.fields["recording"].value is True


# --------------------------------------------------------------------------
# Direct verification
# --------------------------------------------------------------------------


async def test_record_start_confirmed_by_reading_back(rig):
    r = await rig.svc.apply("record_start")
    assert r.ok is True
    assert r.kind == "direct" and r.read == "read.recording"
    assert r.actual is True and r.expected == "True"
    assert rig.sim.state.recording is True
    assert rig.svc.stats.verified == 1


async def test_record_stop_confirmed(rig):
    await rig.svc.apply("record_start")
    r = await rig.svc.apply("record_stop")
    assert r.ok is True and r.actual is False
    assert rig.sim.state.recording is False


async def test_palette_confirmed_and_lands_on_img(rig):
    r = await rig.svc.apply("palette", "IRONBOW")
    assert r.ok is True
    assert r.frame == "#TPUD2wIMG044A", "the exact palette frame from PLAN §1.4"
    assert r.actual == "IRONBOW"
    assert rig.sim.state.palette == "04"


async def test_resolution_confirmed(rig):
    r = await rig.svc.apply("resolution", "R_4K")
    assert r.ok is True and r.actual == "R_4K"
    assert rig.sim.state.resolution == "03"


async def test_thermal_value_is_clamped_and_expectation_follows(rig):
    """The expectation must be the **clamped** value, not the number typed in."""
    r = await rig.svc.apply("thermal_brightness", 150)
    assert r.expected == "100" and r.actual == 100 and r.ok is True
    assert rig.sim.state.thermal["TIB"] == 100


async def test_thermal_roundtrip_all_seven(rig):
    for suffix in ("spatial_nr", "shutter", "detail", "gamma",
                   "brightness", "contrast", "temporal_nr"):
        r = await rig.svc.apply("thermal_" + suffix, 42)
        assert r.ok is True, suffix
        assert r.actual == 42, suffix


# --------------------------------------------------------------------------
# Relative and indirect verification
# --------------------------------------------------------------------------


async def test_zoom_in_compares_before_and_after(rig):
    r = await rig.svc.apply("zoom_in")
    assert r.kind == "relative"
    assert r.before == 0 and r.actual == 1 and r.ok is True
    assert rig.sim.state.zoom == 1


async def test_zoom_out_at_floor_counts_as_confirmed(rig):
    """The zoom floor is known to be 0 — no change there is correct, not a failure."""
    r = await rig.svc.apply("zoom_out")
    assert r.before == 0 and r.actual == 0
    assert r.ok is True
    assert "floor" in r.note


async def test_zoom_out_after_zoom_in(rig):
    await rig.svc.apply("zoom_in")
    await rig.svc.apply("zoom_in")
    r = await rig.svc.apply("zoom_out")
    assert r.before == 2 and r.actual == 1 and r.ok is True


async def test_snap_is_confirmed_only_indirectly(rig):
    r = await rig.svc.apply("snap")
    assert r.kind == "indirect", "CAP has no corresponding read — say so plainly"
    assert r.read == "read.sdcard" and r.ok is True
    assert r.actual.free_mb < r.before.free_mb
    assert rig.sim.state.photos == 1


async def test_snap_without_a_card_is_unverifiable_not_failed():
    """No card inserted: the command still goes out, but counts as unconfirmed."""
    sim, link, svc = await _rig(C12Simulator(seed=11))
    sim.state.sd_total_mb = sim.state.sd_free_mb = 0
    try:
        r = await svc.apply("snap")
        assert r.ok is None, "unverifiable is not the same as failed"
        assert "card" in r.note
        assert r.frame == "#TPUD2wCAP013E", "the command must still be sent"
        assert svc.stats.unverified == 1 and svc.stats.mismatched == 0
    finally:
        await svc.close()
        await link.close()
        await sim.close()


# --------------------------------------------------------------------------
# The three confirmation states
# --------------------------------------------------------------------------


async def test_ignored_write_is_reported_as_mismatch():
    """The packet arrives, the firmware ignores it. This is where "sent" lies."""
    sim, link, svc = await _rig(DeafWrite(seed=13, ignore={"IMG"}))
    try:
        r = await svc.apply("palette", "RAINBOW")
        assert r.ok is False
        assert r.expected == "RAINBOW" and r.actual == "WHITE_HOT"
        assert r.attempts == svc.attempts, "it must retry fully before concluding"
        assert svc.stats.mismatched == 1 and svc.stats.verified == 0
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_silent_read_makes_the_write_unverifiable():
    """``IMG`` writes fine but reads silent → no conclusion, and say so."""
    sim, link, svc = await _rig(QuietRead(seed=17, quiet={"IMG"}))
    try:
        r = await svc.apply("palette", "NIGHT")
        assert r.ok is None
        assert "read.palette" in r.note
        assert sim.state.palette == "06", "the command still reached the camera"
        assert svc.stats.unverified == 1
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_stale_cache_never_becomes_a_verdict():
    """The camera goes silent AFTER the cache holds a value — the old value is the trap.

    The cache deliberately keeps the old value with its age. If the confirmation
    step compared against it, one silence would turn into "the camera still
    reports the old value" = ``ok=False``, a verdict drawn from data that was
    never read back.
    """
    sim, link, svc = await _rig(QuietRead(seed=41))
    try:
        await svc.poll_once(force=True)
        assert svc.fields["palette"].value == "WHITE_HOT"

        sim.quiet.add("IMG")                       # the camera goes mute from here
        r = await svc.apply("palette", "IRONBOW")
        assert r.ok is None, "silence is not the camera reporting the old value"
        assert r.actual is None, "do not present the cached value as freshly read"
        assert "read.palette" in r.note
        assert svc.stats.mismatched == 0 and svc.stats.unverified == 1
        assert svc.fields["palette"].value == "WHITE_HOT", "the cache keeps the old value"
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_confirm_retries_before_giving_up():
    """The camera needs a few beats to change state — one read is not enough."""
    sim, link, svc = await _rig(SlowRecord(seed=19))
    try:
        r = await svc.apply("record_start")
        assert r.ok is True
        assert r.attempts >= 2, "this must be the result of the second read-back or later"
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_lost_packets_still_converge():
    """30% chaos: repeated read-backs make up for the lost packets."""
    sim, link, svc = await _rig(C12Simulator(seed=23, chaos_loss=0.3))
    svc.attempts = 6
    try:
        ok = False
        for _ in range(5):
            r = await svc.apply("record_start")
            if r.ok is True:
                ok = True
                break
        assert ok, "6 read-backs × 5 attempts and still nothing confirmed"
    finally:
        await svc.close()
        await link.close()
        await sim.close()


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["gimbal.yaw_speed", "read.version", "telemetry.push_attitude",
             "camera.set_ip", "reboot"],
)
async def test_apply_refuses_anything_but_verifiable_camera_writes(rig, name):
    with pytest.raises(CommandNotAllowed):
        await rig.svc.apply(name, 1)
    assert rig.sim.state.yaw_speed == 0


async def test_bad_argument_sends_nothing(rig):
    """The expectation is built before sending: a bad parameter leaves no packet."""
    before = rig.sim.rx_count
    with pytest.raises(ValueError):
        await rig.svc.apply("palette", "NEON")
    await asyncio.sleep(0.05)
    assert rig.sim.rx_count == before
    assert rig.sim.state.palette == "01"


async def test_missing_argument_is_rejected(rig):
    with pytest.raises(ValueError):
        await rig.svc.apply("thermal_gamma")


# --------------------------------------------------------------------------
# State reporting
# --------------------------------------------------------------------------


async def test_as_dict_is_json_friendly(rig):
    import json

    await rig.svc.poll_once(force=True)
    await rig.svc.apply("zoom_in")
    body = rig.svc.as_dict()
    json.dumps(body)                       # must not raise

    assert body["fields"]["sdcard"]["value"]["present"] is True
    assert body["last_apply"]["ok"] is True
    assert {a["action"] for a in body["actions"]} == {
        n.split(".", 1)[1] for n in WRITES
    }
    assert len(body["options"]["palette"]) == 11
    assert body["options"]["resolution"][0] == "R_720P"


async def test_field_reports_age_and_support(rig):
    await rig.svc.poll_once(force=True)
    d = rig.svc.fields["palette"].as_dict()
    assert d["supported"] is True and d["age_ms"] is not None
    assert d["read"] == "read.palette" and d["raw"] == "01"


def test_dead_after_default_is_small_enough_to_matter():
    """The default is a decision, not an arbitrary number."""
    assert 2 <= DEAD_AFTER <= 5
