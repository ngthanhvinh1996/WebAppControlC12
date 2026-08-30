"""Tầng video: bus, nguồn, colormap, cầu MJPEG."""

import asyncio
import time

import numpy as np
import pytest

from c12ctl.video import colormap as cmap
from c12ctl.video.bus import Frame, FrameBus, Rate
from c12ctl.video.manager import SPECS, THERMAL, VISIBLE, VideoManager
from c12ctl.video.mjpeg import BOUNDARY, MjpegStream
from c12ctl.video.source import CaptureSource, SyntheticSource, gst_pipeline


def img(h=8, w=8, c=3):
    return np.full((h, w, c) if c else (h, w), 128, np.uint8)


# --------------------------------------------------------------------------
# FrameBus — kỷ luật "không hàng đợi"
# --------------------------------------------------------------------------


async def test_bus_latest_wins_no_queueing():
    """Đây là bất biến quan trọng nhất của cả tầng video.

    Consumer chậm phải NHẢY CÓC tới khung mới nhất, không lần lượt qua từng khung
    đã lỡ. Queue tích khung là tích độ trễ.
    """
    bus = FrameBus()
    bus.bind_loop(asyncio.get_running_loop())
    for i in range(100):
        bus.publish(img())
    frame = await bus.next_frame(after_seq=0)
    assert frame.seq == 100, "phải nhảy thẳng tới khung 100, không trả khung 1"


async def test_bus_blocks_until_new_frame():
    bus = FrameBus()
    bus.bind_loop(asyncio.get_running_loop())
    bus.publish(img())
    task = asyncio.create_task(bus.next_frame(after_seq=1))
    await asyncio.sleep(0.02)
    assert not task.done(), "không được trả khung cũ"
    bus.publish(img())
    assert (await asyncio.wait_for(task, 1)).seq == 2


async def test_bus_returns_immediately_when_newer_exists():
    bus = FrameBus()
    bus.bind_loop(asyncio.get_running_loop())
    bus.publish(img())
    started = time.monotonic()
    frame = await bus.next_frame(after_seq=0)
    assert frame.seq == 1
    assert time.monotonic() - started < 0.05


async def test_bus_timeout_returns_none_and_cleans_up():
    bus = FrameBus()
    bus.bind_loop(asyncio.get_running_loop())
    assert await bus.next_frame(timeout=0.05) is None
    assert bus._waiters == [], "waiter timeout phải được dọn"


async def test_bus_wakes_from_another_thread():
    """Source chạy trên thread, consumer trên asyncio — đây là chỗ dễ sai nhất."""
    import threading

    bus = FrameBus()
    bus.bind_loop(asyncio.get_running_loop())
    task = asyncio.create_task(bus.next_frame(after_seq=0))
    await asyncio.sleep(0.02)
    threading.Thread(target=lambda: bus.publish(img()), daemon=True).start()
    assert (await asyncio.wait_for(task, 2)).seq == 1


async def test_bus_multiple_waiters_all_woken():
    bus = FrameBus()
    bus.bind_loop(asyncio.get_running_loop())
    tasks = [asyncio.create_task(bus.next_frame(after_seq=0)) for _ in range(5)]
    await asyncio.sleep(0.02)
    bus.publish(img())
    assert all(f.seq == 1 for f in await asyncio.gather(*tasks))


def test_frame_age_grows():
    f = Frame(img(), 1, time.monotonic() - 0.1)
    assert 80 < f.age_ms < 200


def test_rate_tracks_frequency():
    r = Rate(alpha=0.5)
    now = 0.0
    for _ in range(20):
        now += 0.05
        r.tick(now)
    assert 18 < r.value < 22


# --------------------------------------------------------------------------
# Nguồn tổng hợp
# --------------------------------------------------------------------------


async def test_synthetic_visible_shape_and_motion():
    src = SyntheticSource("v", 320, 240, 50, kind="visible")
    src.start(asyncio.get_running_loop())
    try:
        first = await asyncio.wait_for(src.bus.next_frame(), 2)
        assert first.image.shape == (240, 320, 3)
        later = await asyncio.wait_for(src.bus.next_frame(after_seq=first.seq + 3), 2)
        assert not np.array_equal(first.image, later.image), "khung phải thay đổi"
    finally:
        src.stop()


async def test_synthetic_thermal_is_single_channel():
    """Luồng nhiệt của C12 thật là ảnh xám — nguồn tổng hợp phải giống."""
    src = SyntheticSource("t", 384, 288, 50, kind="thermal")
    src.start(asyncio.get_running_loop())
    try:
        frame = await asyncio.wait_for(src.bus.next_frame(), 2)
        assert frame.image.shape == (288, 384)
        assert frame.image.ndim == 2
    finally:
        src.stop()


async def test_synthetic_respects_target_fps():
    src = SyntheticSource("v", 64, 48, 25, kind="visible")
    src.start(asyncio.get_running_loop())
    try:
        await asyncio.sleep(0.8)
        assert 15 <= src.stats.frames <= 30, src.stats.frames
    finally:
        src.stop()


async def test_source_stop_is_idempotent():
    src = SyntheticSource("v", 64, 48, 25)
    src.start(asyncio.get_running_loop())
    src.stop()
    src.stop()
    assert not src.stats.running


async def test_source_survives_double_start():
    src = SyntheticSource("v", 64, 48, 25)
    src.start(asyncio.get_running_loop())
    src.start(asyncio.get_running_loop())
    try:
        assert await asyncio.wait_for(src.bus.next_frame(), 2) is not None
    finally:
        src.stop()


# --------------------------------------------------------------------------
# Nguồn thật
# --------------------------------------------------------------------------


def test_gst_pipeline_has_the_anti_latency_flags():
    """Thiếu drop=true max-buffers=1 thì độ trễ tăng dần tới mức vô dụng."""
    p = gst_pipeline("rtsp://host:554/stream=1")
    assert "drop=true" in p and "max-buffers=1" in p
    assert "rtph265depay" in p and "h265parse" in p, "codec là H.265, không phải H.264"
    assert "avdec_h265" in p


def test_gst_pipeline_decoder_is_swappable():
    """Rubik Pi 3 / QCS6490 dùng v4l2h265dec để decode phần cứng."""
    p = gst_pipeline("rtsp://h:554/s", decoder="v4l2h265dec")
    assert "v4l2h265dec" in p and "avdec_h265" not in p


def test_capture_source_picks_backend_from_uri():
    assert not CaptureSource("v", "rtsp://h:554/s").uses_gstreamer
    assert CaptureSource("v", gst_pipeline("rtsp://h:554/s")).uses_gstreamer


async def test_capture_source_reports_failure_without_hanging():
    """Nguồn hỏng phải báo lỗi rồi thoát, không treo thread mãi."""
    src = CaptureSource("bad", "rtsp://127.0.0.1:1/nope", reconnect=False)
    src.start(asyncio.get_running_loop())
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            if not src.stats.running:
                break
        assert src.stats.errors >= 1
        assert src.stats.last_error
    finally:
        src.stop()


# --------------------------------------------------------------------------
# Colormap
# --------------------------------------------------------------------------


def test_colormap_gray_to_bgr():
    out = cmap.apply(img(c=None), "ironbow")
    assert out.shape == (8, 8, 3)


def test_white_hot_is_identity_gray():
    gray = np.arange(256, dtype=np.uint8).reshape(16, 16)
    out = cmap.apply(gray, "white_hot")
    assert np.array_equal(out[:, :, 0], gray)


def test_black_hot_inverts():
    gray = np.arange(256, dtype=np.uint8).reshape(16, 16)
    out = cmap.apply(gray, "black_hot")
    assert np.array_equal(out[:, :, 0], 255 - gray)


def test_unknown_colormap_falls_back_instead_of_raising():
    """Giữa luồng video, hiển thị ảnh xám còn hơn ném lỗi."""
    out = cmap.apply(img(c=None), "không-tồn-tại")
    assert out.shape == (8, 8, 3)


def test_colormap_accepts_colour_input():
    assert cmap.apply(img(), "rainbow").shape == (8, 8, 3)


def test_upscale_uses_nearest_and_keeps_values():
    """INTER_NEAREST giữ pixel sắc nét — hợp nguồn 384×288."""
    src = np.array([[0, 255], [255, 0]], np.uint8)
    out = cmap.upscale(src, 4, 4)
    assert out.shape == (4, 4)
    assert set(np.unique(out)) <= {0, 255}, "nội suy mượt sẽ tạo giá trị trung gian"


def test_available_lists_palettes():
    assert "ironbow" in cmap.available() and "black_hot" in cmap.available()


# --------------------------------------------------------------------------
# MJPEG
# --------------------------------------------------------------------------


@pytest.fixture
async def stream():
    src = SyntheticSource("t", 64, 48, 50, kind="thermal")
    src.start(asyncio.get_running_loop())
    st = MjpegStream(src, quality=70, colormap="ironbow")
    yield st
    await st.close()
    src.stop()


async def test_snapshot_returns_jpeg(stream):
    data = await stream.snapshot()
    assert data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")


async def test_multipart_parts_are_well_formed(stream):
    gen = stream.frames()
    chunk = await asyncio.wait_for(gen.__anext__(), 3)
    assert chunk.startswith(b"--" + BOUNDARY.encode())
    assert b"Content-Type: image/jpeg" in chunk
    assert b"Content-Length: " in chunk
    assert b"\xff\xd8" in chunk
    await gen.aclose()


async def test_encoder_only_runs_while_clients_watch(stream):
    """Không ai xem thì không encode — 720p30 cho tab không ai nhìn là lãng phí."""
    assert stream._encoder is None
    gen = stream.frames()
    await asyncio.wait_for(gen.__anext__(), 3)
    assert stream._encoder is not None and stream.stats.clients == 1

    await gen.aclose()
    await asyncio.sleep(0.05)
    assert stream.stats.clients == 0
    assert stream._encoder is None, "encoder phải ngủ khi client cuối rời đi"


async def test_encoder_stops_when_client_disconnects_midstream(stream):
    """Đóng tab giữa chừng: generator bị huỷ, encoder vẫn phải dọn."""
    gen = stream.frames()
    await asyncio.wait_for(gen.__anext__(), 3)
    task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await gen.aclose()
    await asyncio.sleep(0.05)
    assert stream.stats.clients == 0


async def test_single_encode_shared_by_all_clients(stream):
    """Ba client, một lần encode mỗi khung — không nhân CPU theo số người xem."""
    gens = [stream.frames() for _ in range(3)]
    for g in gens:
        await asyncio.wait_for(g.__anext__(), 3)
    assert stream.stats.clients == 3

    before = stream.stats.encoded
    await asyncio.sleep(0.3)
    produced = stream.stats.encoded - before

    payloads = await asyncio.gather(*(asyncio.wait_for(g.__anext__(), 3) for g in gens))
    assert len({bytes(p) for p in payloads}) == 1, "mọi client phải nhận cùng bytes"
    assert produced < 3 * 30, "encode nhân theo số client rồi"

    for g in gens:
        await g.aclose()


async def test_stats_measure_latency_and_encode_time(stream):
    gen = stream.frames()
    for _ in range(5):
        await asyncio.wait_for(gen.__anext__(), 3)
    s = stream.stats.as_dict()
    assert s["encoded"] >= 1
    assert s["encode_ms"] > 0
    assert s["latency_ms"] >= 0
    assert s["jpeg_kb"] > 0
    await gen.aclose()


async def test_max_fps_throttles_encoding():
    src = SyntheticSource("v", 64, 48, 60, kind="visible")
    src.start(asyncio.get_running_loop())
    st = MjpegStream(src, quality=60, max_fps=10)
    try:
        gen = st.frames()
        await asyncio.wait_for(gen.__anext__(), 3)
        await asyncio.sleep(0.6)
        assert st.stats.encoded <= 12, st.stats.encoded
        await gen.aclose()
    finally:
        await st.close()
        src.stop()


async def test_scale_enlarges_output():
    src = SyntheticSource("t", 32, 24, 50, kind="thermal")
    src.start(asyncio.get_running_loop())
    st = MjpegStream(src, scale=2.0, colormap="ironbow")
    try:
        import cv2

        data = await st.snapshot()
        decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        assert decoded.shape[:2] == (48, 64)
    finally:
        await st.close()
        src.stop()


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------


async def test_manager_synthetic_runs_both_streams():
    mgr = VideoManager.synthetic()
    mgr.start()
    try:
        for name in (VISIBLE, THERMAL):
            data = await asyncio.wait_for(mgr.get(name).snapshot(), 5)
            assert data.startswith(b"\xff\xd8")
    finally:
        await mgr.close()


async def test_streams_are_independent_not_frame_synced():
    """30 fps và 25 fps chạy độc lập — không đồng bộ khung giữa hai luồng."""
    mgr = VideoManager.synthetic()
    mgr.start()
    try:
        await asyncio.sleep(1.0)
        v = mgr.sources[VISIBLE].stats.frames
        t = mgr.sources[THERMAL].stats.frames
        assert v > 0 and t > 0
        assert v != t, "hai luồng lệch nhịp thì số khung không thể bằng nhau"
    finally:
        await mgr.close()


def test_specs_match_measured_camera_geometry():
    assert SPECS[VISIBLE].width == 1280 and SPECS[VISIBLE].height == 720
    assert SPECS[VISIBLE].fps == 30.0 and SPECS[VISIBLE].rtsp_port == 554
    assert SPECS[THERMAL].width == 384 and SPECS[THERMAL].height == 288
    assert SPECS[THERMAL].fps == 25.0 and SPECS[THERMAL].rtsp_port == 555
    assert SPECS[THERMAL].grayscale and not SPECS[VISIBLE].grayscale


def test_live_manager_builds_correct_uris():
    mgr = VideoManager.live("10.0.0.5")
    assert mgr.sources[VISIBLE].uri == "rtsp://10.0.0.5:554/stream=1"
    assert mgr.sources[THERMAL].uri == "rtsp://10.0.0.5:555/stream=2"
    assert not mgr.sources[VISIBLE].uses_gstreamer, "mặc định dùng FFMPEG của cv2"


def test_live_manager_with_hardware_decoder():
    mgr = VideoManager.live("10.0.0.5", decoder="v4l2h265dec")
    uri = mgr.sources[VISIBLE].uri
    assert "v4l2h265dec" in uri and "drop=true" in uri
    assert mgr.sources[VISIBLE].uses_gstreamer


def test_live_defaults_to_camera_side_palette():
    """C12 tự tô màu qua IMG — colormap server chỉ là dự phòng."""
    assert VideoManager.live("10.0.0.5").streams[THERMAL].colormap is None
    assert VideoManager.live("10.0.0.5", colormap="ironbow"
                             ).streams[THERMAL].colormap == "ironbow"


def test_manager_unknown_stream_lists_options():
    with pytest.raises(KeyError, match="visible"):
        VideoManager.synthetic().get("infrared")
