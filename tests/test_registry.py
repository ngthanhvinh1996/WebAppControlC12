"""Bất biến của registry.

Đây là bộ test quan trọng nhất về mặt an toàn: nó khẳng định rằng không có lệnh
nguy hiểm nào lọt được vào allowlist, kể cả do sửa nhầm sau này.
"""

import pytest

from c12ctl.protocol import registry as reg
from c12ctl.protocol.registry import (
    COMMANDS,
    FORBIDDEN,
    FORBIDDEN_PTZ_DATA,
    STOP_FRAMES,
    CommandNotAllowed,
)
from c12ctl.protocol.codec import parse
from c12ctl.protocol.types import AKey, Palette, RiskLevel, Resolution


def test_registry_is_sane():
    reg.assert_registry_sane()


def test_no_dangerous_command_in_registry():
    assert [c.name for c in COMMANDS.values() if c.risk is RiskLevel.DANGEROUS] == []


def test_no_write_to_forbidden_command_word():
    """IPV/GTW/VOM/IQE/RST/RTF/GAR/ZMC/FCC không được có lệnh ghi nào."""
    offenders = [
        c.name for c in COMMANDS.values() if c.rw == "w" and c.cmd3 in FORBIDDEN
    ]
    assert offenders == []


def test_forbidden_words_are_still_readable():
    """Đọc IPV/GTW/VOM/IQE an toàn và có ích — chỉ ghi mới nguy hiểm."""
    for name in ("read.ip_address", "read.gateway", "read.video_config",
                 "read.image_quality"):
        assert COMMANDS[name].rw == "r"
        assert COMMANDS[name].risk is RiskLevel.SAFE


def test_unknown_command_is_rejected():
    with pytest.raises(CommandNotAllowed, match="(?i)allowlist"):
        reg.get("camera.set_ip")


def test_every_frame_parses_and_verifies():
    """Mọi lệnh không tham số phải dựng ra khung hợp lệ."""
    for cmd in COMMANDS.values():
        if cmd.encode is None:
            f = parse(cmd.frame())
            assert f.cmd3 == cmd.cmd3
            assert f.dest == cmd.dest.value
            assert f.rw == cmd.rw


def test_read_commands_all_expect_reply():
    for cmd in reg.read_commands():
        assert cmd.rw == "r"
        assert cmd.expect_reply, "%s là lệnh đọc mà không chờ phản hồi" % cmd.name


# --------------------------------------------------------------------------
# Khung sinh ra phải khớp literal đã kiểm chứng
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expected",
    [
        ("read.version", "#TPUD2rVER0051"),
        ("read.model", "#TPUD2rMOD0044"),
        ("read.zoom", "#TPUD2rDZM004F"),
        ("read.palette", "#TPUD2rIMG0041"),
        ("read.resolution", "#TPUD2rVID0047"),
        ("read.sdcard", "#TPUD2rSDC013F"),
        ("camera.snap", "#TPUD2wCAP013E"),
        ("camera.record_start", "#TPUD2wREC0144"),
        ("camera.record_stop", "#TPUD2wREC0043"),
        ("camera.zoom_in", "#TPUD2wDZM0A65"),
        ("camera.zoom_out", "#TPUD2wDZM0B66"),
    ],
)
def test_no_arg_frames_match_verified_literals(name, expected):
    assert COMMANDS[name].frame() == expected


@pytest.mark.parametrize(
    "name, args, expected",
    [
        ("camera.palette", (Palette.WHITE_HOT,), "#TPUD2wIMG0147"),
        ("camera.palette", ("black_hot",), "#TPUD2wIMG0B58"),
        ("camera.resolution", (Resolution.R_1080P,), "#TPUD2wVID014D"),
        ("camera.resolution", ("r_720p",), "#TPUD2wVID004C"),
        ("gimbal.akey", (AKey.CENTER,), "#TPUG2wPTZ056F"),
        ("gimbal.akey", ("up",), "#TPUG2wPTZ016B"),
        ("gimbal.yaw_speed", (0,), "#TPUG2wGSY005F"),
        ("gimbal.pitch_speed", (0,), "#TPUG2wGSP0056"),
        ("gimbal.goto_yaw", (30,), "#TPUG6wGAY0BB8103E"),
        ("gimbal.goto_pitch", (-90,), "#TPUG6wGAPDCD8104C"),
        ("telemetry.push_attitude", (10,), "#TPUG2wGAA0A46"),
        ("telemetry.push_attitude", (0,), "#TPUG2wGAA0035"),
    ],
)
def test_encoded_frames_match_verified_literals(name, args, expected):
    assert COMMANDS[name].frame(*args) == expected


# --------------------------------------------------------------------------
# PTZ — dải nguy hiểm
# --------------------------------------------------------------------------


def test_akey_never_touches_calibration_range():
    """PTZ 0C/0D khởi động hiệu chuẩn gimbal. Enum không được chạm tới."""
    assert {k.value for k in AKey}.isdisjoint(FORBIDDEN_PTZ_DATA)


def test_calibration_values_are_in_forbidden_set():
    assert {"0C", "0D"} <= FORBIDDEN_PTZ_DATA


def test_akey_rejects_raw_hex():
    """Chỉ nhận tên trong enum — không cho lách bằng chuỗi hex thô."""
    with pytest.raises(KeyError):
        COMMANDS["gimbal.akey"].frame("0C")


# --------------------------------------------------------------------------
# Dừng khẩn
# --------------------------------------------------------------------------


def test_stop_frames_are_precomputed_and_valid():
    """Dựng sẵn để không phải tra registry đúng lúc đang cần dừng gấp."""
    assert STOP_FRAMES == ("#TPUG2wGSY005F", "#TPUG2wGSP0056")
    for f in STOP_FRAMES:
        parse(f)


# --------------------------------------------------------------------------
# Phân loại rủi ro
# --------------------------------------------------------------------------


def test_gimbal_motion_is_physical():
    for name in ("gimbal.yaw_speed", "gimbal.pitch_speed", "gimbal.speed",
                 "gimbal.goto_yaw", "gimbal.goto_pitch", "gimbal.goto", "gimbal.akey"):
        assert COMMANDS[name].risk is RiskLevel.PHYSICAL, name


def test_camera_writes_are_reversible():
    for name in ("camera.snap", "camera.record_start", "camera.zoom_in",
                 "camera.palette", "camera.resolution"):
        assert COMMANDS[name].risk is RiskLevel.REVERSIBLE, name


def test_telemetry_enable_is_not_physical():
    """GAA bật luồng đẩy, không gây chuyển động."""
    assert COMMANDS["telemetry.push_attitude"].risk is RiskLevel.REVERSIBLE


def test_all_reads_are_safe():
    for cmd in COMMANDS.values():
        if cmd.rw == "r":
            assert cmd.risk is RiskLevel.SAFE, cmd.name


def test_palette_command_is_img_not_tar():
    """Hồi quy cho sai lầm nguy hiểm nhất trong skydroid-c12-protocol.md.

    TAR là khử nhiễu không gian. Nếu ai đó đổi palette sang TAR, test này chết.
    """
    assert COMMANDS["camera.palette"].cmd3 == "IMG"
    assert COMMANDS["camera.thermal_spatial_nr"].cmd3 == "TAR"
    assert COMMANDS["read.palette"].cmd3 == "IMG"


def test_zoom_targets_camera_not_lens_motor():
    """C12 là zoom số qua DZM (đích D). ZMC/FCC là của model có ống kính cơ."""
    cmd = COMMANDS["camera.zoom_in"]
    assert cmd.cmd3 == "DZM"
    assert cmd.dest.value == "D"
    assert "ZMC" in FORBIDDEN and "FCC" in FORBIDDEN
