"""Parameter encoding — where the two source documents disagree the most."""

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
# Speed — the bytecode says raw = °/s ÷ 0.5, clamped to ±127
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
    """The real range is ±63.5 °/s, not the ±99 in protocol.md's table."""
    assert MAX_SPEED_DPS == 63.5


def test_speed_frames_match_protocol_md_bytes_but_not_its_labels():
    """The two documents agree on the BYTES and differ on the LABELS by exactly 2×.

    protocol.md calls #TPUG2wGSY3264 "speed +50"; by the bytecode formula it is
    +25 °/s. If the UI trusted protocol.md's labels, every bit of stick feel
    would be off by a factor of two.
    """
    def yaw(dps):
        return build("G", "w", "GSY", u8_hex(speed_to_raw(dps)))

    assert yaw(25) == "#TPUG2wGSY3264"      # protocol.md labels this "+50"
    assert yaw(-25) == "#TPUG2wGSYCE87"     # protocol.md labels this "−50"
    assert yaw(0) == "#TPUG2wGSY005F"       # stop — both sources agree
    assert yaw(63.5) == "#TPUG2wGSY7F7C"    # the real maximum, absent from protocol.md


@pytest.mark.parametrize("dps", [0, 3.7, -12.4, 63.5, -63.5, 100, -100])
def test_speed_roundtrip_is_clamped_quantised_value(dps):
    """decode(encode(x)) == clamp(quantise(x)) — 0.5 steps, so it must match exactly."""
    assert raw_to_speed(speed_to_raw(dps)) == clamp_speed(dps)


def test_speed_quantises_to_half_degree_steps():
    assert clamp_speed(3.7) == 3.5
    assert clamp_speed(-3.7) == -3.5


# --------------------------------------------------------------------------
# Angles — the bytecode says ×100, clamped to ±9000
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
    """PHAN_TICH_SDK_C12.md §5.3 gives two examples — rebuilding must match them."""
    assert build("G", "w", "GAY", s16_hex(angle_to_raw(30)) + "10") == "#TPUG6wGAY0BB8103E"
    assert build("G", "w", "GAP", s16_hex(angle_to_raw(-90)) + "10") == "#TPUG6wGAPDCD8104C"


@pytest.mark.parametrize("deg", [0, 12.34, -56.78, 90, -90, 200, -200])
def test_angle_roundtrip(deg):
    assert raw_to_angle(angle_to_raw(deg)) == clamp_angle(deg)


# --------------------------------------------------------------------------
# Signed hex
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
# Palette — 11 entries, on IMG rather than TAR
# --------------------------------------------------------------------------


def test_exactly_eleven_palettes():
    """protocol.md counts exactly 11 palettes in the APK resources; the
    bytecode's IMG table also has exactly 11. Two independent sources, same
    number."""
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
# Telemetry decoding
# --------------------------------------------------------------------------


def test_attitude_from_gac_payload():
    """GAC carries 3 consecutive int16 hex values; divide by 100 for degrees."""
    data = s16_hex(3000) + s16_hex(-4500) + s16_hex(0)
    att = Attitude.from_data(data)
    assert att.yaw == pytest.approx(30.0)
    assert att.pitch == pytest.approx(-45.0)
    assert att.roll == pytest.approx(0.0)


def test_attitude_rejects_short_payload():
    with pytest.raises(ValueError, match="12 data characters"):
        Attitude.from_data("0BB8")


def test_sdcard_absent_when_both_zero():
    assert not SDCardStatus(0, 0).present
    assert SDCardStatus(32000, 15000).present
