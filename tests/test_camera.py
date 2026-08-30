"""Pha 3 — lệnh ghi camera phải xác nhận được bằng một lệnh đọc.

Trọng tâm không phải "gói có được gửi không" (pha 0 đã lo) mà **"đọc lại thì
thấy gì"**. Lệnh ghi của C12 không có phản hồi, nên ba tình huống dưới đây trông
giống hệt nhau ở phía gửi và phải được phân biệt bằng bước đọc lại:

* lệnh có tác dụng → ``ok=True``
* lệnh tới nơi nhưng firmware bỏ qua → ``ok=False``
* không có cách nào biết (lệnh đọc im lặng, chưa cắm thẻ) → ``ok=None``

Ba sim con ở đầu file mô hình hoá đúng ba tình huống đó.
"""

import asyncio

import pytest

from c12ctl.protocol.registry import COMMANDS, CommandNotAllowed
from c12ctl.services.camera import DEAD_AFTER, WRITES, CameraService
from c12ctl.sim.c12_sim import C12Simulator
from c12ctl.transport.udp_link import UdpLink


class QuietRead(C12Simulator):
    """Camera trả lời mọi thứ trừ vài lệnh đọc — firmware thiếu lệnh đó."""

    def __init__(self, *a, quiet=(), **kw):
        super().__init__(*a, **kw)
        self.quiet = set(quiet)

    def _handle_read(self, cmd, data):
        if cmd in self.quiet:
            return None
        return super()._handle_read(cmd, data)


class DeafWrite(C12Simulator):
    """Gói ghi tới nơi nhưng không có tác dụng gì — kịch bản tệ nhất."""

    def __init__(self, *a, ignore=(), **kw):
        super().__init__(*a, **kw)
        self.ignore = set(ignore)

    def handle(self, frame):
        if frame.rw == "w" and frame.cmd3 in self.ignore:
            return None
        return super().handle(frame)


class SlowRecord(C12Simulator):
    """REC có tác dụng thật, nhưng đọc lại chỉ thấy sau vài nhịp.

    Camera thật phải mở file trên thẻ trước khi trạng thái đổi — đọc lại một lần
    rồi kết luận là sai.
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
    """Simulator + link + service, chưa chạy vòng poll nền (test tự gọi)."""
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
# Bảng xác nhận
# --------------------------------------------------------------------------


def test_every_camera_write_is_verifiable():
    """Bất biến của pha 3: không lệnh ghi camera nào lọt ra ngoài bảng xác nhận.

    Thêm một lệnh ghi vào registry mà quên khai báo cách đọc lại thì test này
    hỏng — đó là mục đích của nó.
    """
    writes = {n for n, c in COMMANDS.items()
              if n.startswith("camera.") and c.rw == "w"}
    assert writes == set(WRITES), "lệnh ghi camera chưa khai báo cách xác nhận"


def test_verify_table_points_at_real_read_commands():
    for name, spec in WRITES.items():
        assert spec.read in COMMANDS, "%s trỏ vào lệnh đọc không có" % name
        assert COMMANDS[spec.read].rw == "r"
        assert COMMANDS[spec.read].expect_reply is True


# --------------------------------------------------------------------------
# Đệm trạng thái
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
    """Format thô của ``SDC`` chưa chốt — đệm phải giữ lại chuỗi gốc."""
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
    assert rig.svc.fields["model"].replies == 1, "model không đổi thì đừng hỏi lại"
    assert rig.svc.fields["recording"].replies > 1


async def test_silent_field_backs_off_instead_of_jamming_the_poll():
    """Lệnh đọc không tồn tại phải bị giãn ra: mỗi lần dò là một lần chờ timeout."""
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
        assert vid.checked_at == checked, "trường đã chết vẫn bị dò mỗi vòng"
        assert svc.fields["palette"].checked_at > checked, "trường sống vẫn phải dò"
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_poll_rests_while_the_link_is_saturated(rig):
    """Gimbal quay 20 Hz thì hàng ưu tiên chiếm khe gửi — poll phải nhường."""
    await rig.svc.poll_once(force=True)
    checked = rig.svc.fields["palette"].checked_at

    rig.svc._busy = lambda: True
    assert await rig.svc.poll_once() == []
    assert rig.svc.fields["palette"].checked_at == checked
    assert rig.svc.stats.skipped == 1

    # "Đọc lại" là yêu cầu tường minh của người dùng — vẫn phải đi qua.
    await rig.svc.poll_once(force=True)
    assert rig.svc.fields["palette"].checked_at > checked


async def test_silence_under_saturation_is_not_evidence():
    """Im lặng lúc link bão hoà nói về lưu lượng, không về firmware.

    Không tách hai chuyện này ra thì gimbal quay vài giây là cả bảng trạng thái
    camera bị đánh dấu "không hỗ trợ" — và im 30 giây theo cơ chế giãn.
    """
    sim, link, svc = await _rig(QuietRead(seed=29, quiet={"IMG"}))
    svc._busy = lambda: True
    try:
        for _ in range(DEAD_AFTER + 2):
            await svc.poll_once(force=True)
        pal = svc.fields["palette"]
        assert pal.silent_streak == 0
        assert pal.supported is None, "chưa kết luận được, không phải 'không hỗ trợ'"
        assert svc.stats.silent >= DEAD_AFTER
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_unverifiable_write_names_the_busy_link():
    """Không xác nhận được lúc gimbal đang quay thì phải nói đúng lý do."""
    sim, link, svc = await _rig(QuietRead(seed=37, quiet={"IMG"}))
    svc._busy = lambda: True
    try:
        r = await svc.apply("palette", "SEPIA")
        assert r.ok is None
        assert "gimbal" in r.note and "read.palette" in r.note
        assert sim.state.palette == "03", "lệnh vẫn được gửi"
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
# Xác nhận trực tiếp
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
    assert r.frame == "#TPUD2wIMG044A", "đúng khung palette trong PLAN §1.4"
    assert r.actual == "IRONBOW"
    assert rig.sim.state.palette == "04"


async def test_resolution_confirmed(rig):
    r = await rig.svc.apply("resolution", "R_4K")
    assert r.ok is True and r.actual == "R_4K"
    assert rig.sim.state.resolution == "03"


async def test_thermal_value_is_clamped_and_expectation_follows(rig):
    """Kỳ vọng phải là giá trị **đã clamp**, không phải số người dùng gõ vào."""
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
# Xác nhận tương đối và gián tiếp
# --------------------------------------------------------------------------


async def test_zoom_in_compares_before_and_after(rig):
    r = await rig.svc.apply("zoom_in")
    assert r.kind == "relative"
    assert r.before == 0 and r.actual == 1 and r.ok is True
    assert rig.sim.state.zoom == 1


async def test_zoom_out_at_floor_counts_as_confirmed(rig):
    """Đáy zoom biết chắc là 0 — không đổi ở đó là đúng, không phải lỗi."""
    r = await rig.svc.apply("zoom_out")
    assert r.before == 0 and r.actual == 0
    assert r.ok is True
    assert "đáy" in r.expected or "đáy" in r.note


async def test_zoom_out_after_zoom_in(rig):
    await rig.svc.apply("zoom_in")
    await rig.svc.apply("zoom_in")
    r = await rig.svc.apply("zoom_out")
    assert r.before == 2 and r.actual == 1 and r.ok is True


async def test_snap_is_confirmed_only_indirectly(rig):
    r = await rig.svc.apply("snap")
    assert r.kind == "indirect", "CAP không có lệnh đọc tương ứng — phải nói rõ"
    assert r.read == "read.sdcard" and r.ok is True
    assert r.actual.free_mb < r.before.free_mb
    assert rig.sim.state.photos == 1


async def test_snap_without_a_card_is_unverifiable_not_failed():
    """Chưa cắm thẻ: lệnh vẫn gửi, nhưng không được nhận là đã chụp."""
    sim, link, svc = await _rig(C12Simulator(seed=11))
    sim.state.sd_total_mb = sim.state.sd_free_mb = 0
    try:
        r = await svc.apply("snap")
        assert r.ok is None, "không xác nhận được ≠ thất bại"
        assert "thẻ" in r.note
        assert r.frame == "#TPUD2wCAP013E", "lệnh vẫn phải được gửi"
        assert svc.stats.unverified == 1 and svc.stats.mismatched == 0
    finally:
        await svc.close()
        await link.close()
        await sim.close()


# --------------------------------------------------------------------------
# Ba trạng thái xác nhận
# --------------------------------------------------------------------------


async def test_ignored_write_is_reported_as_mismatch():
    """Gói tới nơi, firmware bỏ qua. Đây là ca mà "đã gửi" nói dối."""
    sim, link, svc = await _rig(DeafWrite(seed=13, ignore={"IMG"}))
    try:
        r = await svc.apply("palette", "RAINBOW")
        assert r.ok is False
        assert r.expected == "RAINBOW" and r.actual == "WHITE_HOT"
        assert r.attempts == svc.attempts, "phải thử lại đủ số lần trước khi kết luận"
        assert svc.stats.mismatched == 1 and svc.stats.verified == 0
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_silent_read_makes_the_write_unverifiable():
    """``IMG`` ghi được nhưng đọc im lặng → không kết luận được, phải nói rõ."""
    sim, link, svc = await _rig(QuietRead(seed=17, quiet={"IMG"}))
    try:
        r = await svc.apply("palette", "NIGHT")
        assert r.ok is None
        assert "read.palette" in r.note
        assert sim.state.palette == "06", "lệnh vẫn tới camera"
        assert svc.stats.unverified == 1
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_stale_cache_never_becomes_a_verdict():
    """Camera im lặng SAU KHI đệm đã có giá trị — giá trị cũ là cái bẫy.

    Đệm cố ý giữ giá trị cũ kèm tuổi. Nếu bước xác nhận so với giá trị đó thì
    một lần im lặng biến thành "camera vẫn báo giá trị cũ" = ``ok=False``, kết
    luận rút ra từ số liệu chưa hề được đọc lại.
    """
    sim, link, svc = await _rig(QuietRead(seed=41))
    try:
        await svc.poll_once(force=True)
        assert svc.fields["palette"].value == "WHITE_HOT"

        sim.quiet.add("IMG")                       # từ đây camera câm
        r = await svc.apply("palette", "IRONBOW")
        assert r.ok is None, "im lặng ≠ camera báo giá trị cũ"
        assert r.actual is None, "không được trưng giá trị đệm ra như vừa đọc"
        assert "read.palette" in r.note
        assert svc.stats.mismatched == 0 and svc.stats.unverified == 1
        assert svc.fields["palette"].value == "WHITE_HOT", "đệm vẫn giữ giá trị cũ"
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_confirm_retries_before_giving_up():
    """Camera cần vài nhịp mới đổi trạng thái — một lần đọc là chưa đủ."""
    sim, link, svc = await _rig(SlowRecord(seed=19))
    try:
        r = await svc.apply("record_start")
        assert r.ok is True
        assert r.attempts >= 2, "phải là kết quả của lần đọc lại thứ hai trở đi"
    finally:
        await svc.close()
        await link.close()
        await sim.close()


async def test_lost_packets_still_converge():
    """Chaos 30%: đọc lại nhiều lần bù được gói mất."""
    sim, link, svc = await _rig(C12Simulator(seed=23, chaos_loss=0.3))
    svc.attempts = 6
    try:
        ok = False
        for _ in range(5):
            r = await svc.apply("record_start")
            if r.ok is True:
                ok = True
                break
        assert ok, "6 lần đọc lại × 5 lần thử vẫn không xác nhận nổi"
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
    """Kỳ vọng dựng trước khi gửi: tham số sai thì không gói nào rời backend."""
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
# Báo cáo trạng thái
# --------------------------------------------------------------------------


async def test_as_dict_is_json_friendly(rig):
    import json

    await rig.svc.poll_once(force=True)
    await rig.svc.apply("zoom_in")
    body = rig.svc.as_dict()
    json.dumps(body)                       # không được ném

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
    """Giá trị mặc định là một quyết định, không phải số ngẫu nhiên."""
    assert 2 <= DEAD_AFTER <= 5
