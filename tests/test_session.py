"""Phase 6 — synchronised session recording.

What matters here is not "did a file appear" but **can the three streams be
aligned afterwards**: packets, attitude and video frames all carry the same
monotonic clock, so a recording can answer "what did we send just before the
camera did that".
"""

import asyncio
import json

import pytest

from c12ctl.services.session import SessionReader, SessionRecorder
from c12ctl.services.telemetry import TelemetryService
from c12ctl.sim.c12_sim import C12Simulator
from c12ctl.transport.udp_link import UdpLink
from c12ctl.video.manager import VideoManager


@pytest.fixture
async def rig(tmp_path):
    """Simulator + link + telemetry + synthetic video + recorder."""
    sim = C12Simulator(seed=61)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    tel = TelemetryService(link, rate_hz=25, rearm_interval=0.2)
    await tel.start()
    video = VideoManager.synthetic()
    video.start()
    rec = SessionRecorder(link, root=tmp_path / "sessions", video=video,
                          telemetry=tel, frame_fps=20.0)

    class Rig:
        pass

    r = Rig()
    r.sim, r.link, r.tel, r.video, r.rec, r.root = sim, link, tel, video, rec, tmp_path
    yield r

    await rec.close()
    await video.close()
    await tel.close()
    await link.close()
    await sim.close()


async def _record(rig, seconds=0.6, note="test"):
    await rig.rec.start(note)
    rig.link.send("camera.record_start")
    await asyncio.sleep(seconds)
    await rig.link.request("read.model", timeout=0.2)
    return await rig.rec.stop("test finished")


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


async def test_start_creates_a_session_directory(rig):
    info = await rig.rec.start("first run")
    try:
        path = rig.rec.path
        assert path.is_dir()
        assert (path / "meta.json").is_file()
        assert (path / "events.jsonl").is_file()
        assert info["recording"] is True and info["note"] == "first run"
    finally:
        await rig.rec.stop()


async def test_stop_writes_the_summary_into_meta(rig):
    summary = await _record(rig)
    assert summary["recording"] is False
    meta = json.loads((rig.rec.root / summary["id"] / "meta.json").read_text())
    assert meta["summary"]["stats"]["events"] > 0
    assert meta["note"] == "test"


async def test_second_start_is_refused_while_recording(rig):
    await rig.rec.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            await rig.rec.start()
    finally:
        await rig.rec.stop()


async def test_stop_is_safe_when_idle(rig):
    assert (await rig.rec.stop())["recording"] is False


async def test_close_stops_an_active_recording(rig):
    await rig.rec.start()
    await asyncio.sleep(0.1)
    await rig.rec.close()
    assert not rig.rec.recording


# --------------------------------------------------------------------------
# What gets captured
# --------------------------------------------------------------------------


async def test_packets_attitude_and_frames_all_land(rig):
    summary = await _record(rig)
    kinds = SessionReader(rig.rec.root / summary["id"]).summary()["kinds"]
    assert kinds.get("packet", 0) > 0, "no command traffic recorded"
    assert kinds.get("attitude", 0) > 0, "no attitude recorded"
    assert kinds.get("frame", 0) > 0, "no video recorded"
    assert kinds.get("marker", 0) >= 2, "start and stop markers"


async def test_both_directions_of_traffic_are_recorded(rig):
    """A packet log that only shows what we sent cannot explain a reply."""
    summary = await _record(rig)
    events = list(SessionReader(rig.rec.root / summary["id"]).events())
    dirs = {e.get("dir") for e in events if e["kind"] == "packet"}
    assert "tx" in dirs and "rx" in dirs


async def test_commands_are_broken_down_by_word_and_direction(rig):
    summary_path = rig.rec.root / (await _record(rig))["id"]
    commands = SessionReader(summary_path).summary()["commands"]
    assert commands["REC"]["tx"] >= 1, "the record_start we sent"
    assert commands["MOD"]["tx"] >= 1 and commands["MOD"]["rx"] >= 1, "read and reply"
    assert commands["GAC"]["rx"] > 0, "pushed attitude frames"


async def test_every_event_shares_one_monotonic_clock(rig):
    """Alignment is the whole point: events must be ordered and stamped alike."""
    summary = await _record(rig)
    events = list(SessionReader(rig.rec.root / summary["id"]).events())
    assert len(events) > 5

    monos = [e["mono"] for e in events]
    assert monos == sorted(monos), "events are out of monotonic order"
    assert all("at" in e for e in events), "every event needs an offset from start"
    assert all(e["at"] >= 0 for e in events)
    # Different kinds, one timebase — otherwise they cannot be lined up.
    by_kind = {e["kind"] for e in events}
    assert {"packet", "attitude", "frame"} <= by_kind


async def test_attitude_range_is_reported(rig):
    await rig.rec.start()
    rig.sim.state.yaw = 12.0
    await asyncio.sleep(0.3)
    rig.sim.state.yaw = -8.0
    await asyncio.sleep(0.3)
    summary = await rig.rec.stop()

    span = SessionReader(rig.rec.root / summary["id"]).summary()["attitude_range"]["yaw"]
    assert span is not None
    assert span[0] < 0 < span[1], "the recording should span both sides of zero"


# --------------------------------------------------------------------------
# Video storage
# --------------------------------------------------------------------------


async def test_frames_are_retrievable_by_index(rig):
    summary = await _record(rig)
    reader = SessionReader(rig.rec.root / summary["id"])

    data = reader.frame("visible", 0)
    assert data is not None
    assert data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"), "a whole JPEG"

    last = reader.frame("visible", -1)
    assert last is not None and last.startswith(b"\xff\xd8")


async def test_frame_index_out_of_range_returns_none(rig):
    summary = await _record(rig)
    assert SessionReader(rig.rec.root / summary["id"]).frame("visible", 9999) is None
    assert SessionReader(rig.rec.root / summary["id"]).frame("nope", 0) is None


async def test_frame_offsets_point_at_real_jpeg_boundaries(rig):
    """Offsets must be exact — the file is concatenated JPEGs, not a container."""
    summary = await _record(rig)
    path = rig.rec.root / summary["id"]
    reader = SessionReader(path)
    entries = [e for e in reader.events()
               if e["kind"] == "frame" and e["stream"] == "thermal"]
    assert entries

    raw = (path / "thermal.mjpeg").read_bytes()
    for entry in entries:
        chunk = raw[entry["offset"]:entry["offset"] + entry["length"]]
        assert chunk.startswith(b"\xff\xd8") and chunk.endswith(b"\xff\xd9")
    assert entries[-1]["offset"] + entries[-1]["length"] == len(raw), "no gaps"


async def test_frame_rate_is_capped_below_the_source(rig):
    """30 fps × 24 KB per stream would fill a card long before it earned its keep."""
    rig.rec.frame_fps = 5.0
    await rig.rec.start()
    await asyncio.sleep(1.0)
    summary = await rig.rec.stop()
    got = summary["stats"]["frames"]["visible"]
    assert got <= 9, "recorded %d frames in ~1 s at a 5 fps cap" % got


async def test_recording_does_not_add_an_encode_when_someone_is_watching(rig):
    """Recording holds a viewer, so it reuses the single shared encode."""
    stream = rig.video.get("visible")
    async with stream.viewer():
        await asyncio.sleep(0.2)
        before = stream.stats.encoded
        await rig.rec.start()
        await asyncio.sleep(0.4)
        during = stream.stats.encoded
        await rig.rec.stop()
    # Frames encoded while recording must stay in the same ballpark as the
    # stream's own rate — not doubled by a second encoder.
    assert during - before < 0.4 * stream.source.fps * 1.6


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------


async def test_byte_cap_stops_the_recording_itself(rig):
    """A full disk on a Pi mid-test is worse than a recording that ends early."""
    rig.rec.max_bytes = 40_000
    await rig.rec.start()
    for _ in range(100):
        await asyncio.sleep(0.05)
        if not rig.rec.recording:
            break
    assert not rig.rec.recording
    assert "byte cap" in rig.rec.stop_reason


async def test_duration_cap_stops_the_recording_itself(rig):
    rig.rec.max_seconds = 0.3
    await rig.rec.start()
    for _ in range(60):
        await asyncio.sleep(0.05)
        if not rig.rec.recording:
            break
    assert not rig.rec.recording
    assert "duration cap" in rig.rec.stop_reason


# --------------------------------------------------------------------------
# Listing and reading back
# --------------------------------------------------------------------------


async def test_sessions_are_listed_newest_first(rig):
    first = await _record(rig, 0.2, note="one")
    await asyncio.sleep(1.05)          # ids have one-second resolution
    second = await _record(rig, 0.2, note="two")

    listed = rig.rec.list_sessions()
    assert [s["id"] for s in listed][:2] == [second["id"], first["id"]]
    assert listed[0]["note"] == "two"
    assert listed[0]["size_mb"] >= 0
    assert listed[0]["summary"]["stats"]["events"] > 0


async def test_reader_rejects_a_directory_that_is_not_a_session(rig, tmp_path):
    with pytest.raises(FileNotFoundError):
        SessionReader(tmp_path / "nowhere")


async def test_recorder_works_without_video_or_telemetry(rig):
    """Phase 1 layout — link only — must still record command traffic."""
    bare = SessionRecorder(rig.link, root=rig.root / "bare")
    await bare.start("link only")
    rig.link.send("camera.snap")
    await asyncio.sleep(0.15)
    summary = await bare.stop()

    kinds = SessionReader(bare.root / summary["id"]).summary()["kinds"]
    assert kinds.get("packet", 0) > 0
    assert "frame" not in kinds and "attitude" not in kinds


async def test_markers_bracket_the_recording(rig):
    summary = await _record(rig, note="bracketed")
    markers = SessionReader(rig.rec.root / summary["id"]).summary()["markers"]
    assert markers[0]["text"] == "bracketed"
    assert "test finished" in markers[-1]["text"]
