"""Mã hoá tham số — chỗ hai tài liệu nguồn mâu thuẫn nhau nhiều nhất."""

import pytest

from c12ctl.protocol.codec import build
from c12ctl.protocol.types import (
    Attitude,
    MAX_ANGLE_DEG,
    MAX_SPEED_DPS,
    Palette,
    SDCardStatus,
    angle_to_raw,
    clamp_angle,
    clamp_speed,
    parse_s16,
    raw_to_angle,
    raw_to_speed,
    s16_hex,
    speed_to_raw,
    u8_hex,
)

# --------------------------------------------------------------------------
# Tốc độ — bytecode nói raw = °/s ÷ 0.5, clamp ±127
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dps, raw",
    [(0, 0), (0.5, 1), (25, 50), (-25, -50), (63.5, 127), (-63.5, -127)],
)
def test_speed_encoding(dps, raw):
    assert speed_to_raw(dps) == raw
    assert raw_to_speed(raw) == pytest.approx(dps)


@pytest.mark.parametrize("dps", [99, 200, 1e9])
def test_speed_clamps_high(dps):
    assert speed_to_raw(dps) == 127


@pytest.mark.parametrize("dps", [-99, -200, -1e9])
def test_speed_clamps_low(dps):
    assert speed_to_raw(dps) == -127


def test_max_speed_is_63_5():
    """Dải thật ±63.5 °/s, không phải ±99 như bảng trong protocol.md."""
    assert MAX_SPEED_DPS == 63.5


def test_speed_frames_match_protocol_md_bytes_but_not_its_labels():
    """Hai tài liệu khớp nhau về BYTE, lệch nhau về NHÃN đúng hệ số 2.

    protocol.md gọi #TPUG2wGSY3264 là "speed +50"; theo công thức bytecode nó là
    +25 °/s. Nếu UI tin nhãn của protocol.md thì mọi cảm nhận về tay lái sẽ sai gấp đôi.
    """
    def yaw(dps):
        return build("G", "w", "GSY", u8_hex(speed_to_raw(dps)))

    assert yaw(25) == "#TPUG2wGSY3264"      # protocol.md ghi nhãn "+50"
    assert yaw(-25) == "#TPUG2wGSYCE87"     # protocol.md ghi nhãn "−50"
    assert yaw(0) == "#TPUG2wGSY005F"       # dừng — hai nguồn đồng ý
    assert yaw(63.5) == "#TPUG2wGSY7F7C"    # tối đa thật, protocol.md không có


@pytest.mark.parametrize("dps", [0, 3.7, -12.4, 63.5, -63.5, 100, -100])
def test_speed_roundtrip_is_clamped_quantised_value(dps):
    """decode(encode(x)) == clamp(quantise(x)) — bước 0.5 nên phải khớp tuyệt đối."""
    assert raw_to_speed(speed_to_raw(dps)) == clamp_speed(dps)


def test_speed_quantises_to_half_degree_steps():
    assert clamp_speed(3.7) == 3.5
    assert clamp_speed(-3.7) == -3.5


# --------------------------------------------------------------------------
# Góc — bytecode nói ×100, clamp ±9000
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "deg, raw", [(0, 0), (30, 3000), (-45, -4500), (90, 9000), (-90, -9000)]
)
def test_angle_encoding(deg, raw):
    assert angle_to_raw(deg) == raw
    assert raw_to_angle(raw) == pytest.approx(deg)


@pytest.mark.parametrize("deg", [91, 180, 1e9])
def test_angle_clamps(deg):
    assert angle_to_raw(deg) == 9000
    assert angle_to_raw(-deg) == -9000


def test_max_angle_is_90():
    assert MAX_ANGLE_DEG == 90.0


def test_goto_frames_match_bytecode_examples():
    """PHAN_TICH_SDK_C12.md §5.3 nêu hai ví dụ — dựng lại phải khớp."""
    assert build("G", "w", "GAY", s16_hex(angle_to_raw(30)) + "10") == "#TPUG6wGAY0BB8103E"
    assert build("G", "w", "GAP", s16_hex(angle_to_raw(-90)) + "10") == "#TPUG6wGAPDCD8104C"


@pytest.mark.parametrize("deg", [0, 12.34, -56.78, 90, -90, 200, -200])
def test_angle_roundtrip(deg):
    assert raw_to_angle(angle_to_raw(deg)) == clamp_angle(deg)


# --------------------------------------------------------------------------
# Hex có dấu
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, -1, 127, -127, 3000, -4500, 9000, -9000])
def test_s16_roundtrip(value):
    assert parse_s16(s16_hex(value)) == value


def test_s16_negative_is_twos_complement():
    assert s16_hex(-4500) == "EE6C"
    assert s16_hex(-9000) == "DCD8"


def test_u8_negative_is_twos_complement():
    assert u8_hex(-50) == "CE"
    assert u8_hex(-127) == "81"


# --------------------------------------------------------------------------
# Palette — 11 mục, IMG chứ không phải TAR
# --------------------------------------------------------------------------


def test_exactly_eleven_palettes():
    """protocol.md đếm được đúng 11 palette trong resource APK; bảng IMG của
    bytecode cũng có đúng 11. Hai nguồn độc lập, cùng con số."""
    assert len(Palette) == 11


def test_palette_value_02_is_absent():
    assert "02" not in {p.value for p in Palette}


@pytest.mark.parametrize(
    "palette, frame",
    [
        (Palette.WHITE_HOT, "#TPUD2wIMG0147"),
        (Palette.BLACK_HOT, "#TPUD2wIMG0B58"),
        (Palette.GLORY_HOT, "#TPUD2wIMG0C59"),
    ],
)
def test_palette_frames(palette, frame):
    assert build("D", "w", "IMG", palette.value) == frame


# --------------------------------------------------------------------------
# Giải mã telemetry
# --------------------------------------------------------------------------


def test_attitude_from_gac_payload():
    """GAC chở 3 int16 hex liên tiếp, chia 100 ra độ."""
    data = s16_hex(3000) + s16_hex(-4500) + s16_hex(0)
    att = Attitude.from_data(data)
    assert att.yaw == pytest.approx(30.0)
    assert att.pitch == pytest.approx(-45.0)
    assert att.roll == pytest.approx(0.0)


def test_attitude_rejects_short_payload():
    with pytest.raises(ValueError, match="12 ký tự"):
        Attitude.from_data("0BB8")


def test_sdcard_absent_when_both_zero():
    assert not SDCardStatus(0, 0).present
    assert SDCardStatus(32000, 15000).present
