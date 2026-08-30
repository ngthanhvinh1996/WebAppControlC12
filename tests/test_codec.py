"""Golden cases for the codec: 43 literals taken verbatim from both source documents.

The two sources disagree about *meaning* but agree perfectly on framing and
checksums — so all 43 literals work as verification cases regardless of how we
end up interpreting them.
"""

import pytest

from c12ctl.protocol.codec import (
    FrameError,
    HEADER_LOWER,
    build,
    checksum,
    parse,
    seal,
    split_frames,
    to_wire,
)

# --------------------------------------------------------------------------
# Verified literals
# --------------------------------------------------------------------------

# From PHAN_TICH_SDK_C12.md — decompiled rcsdk-v1.9.2.aar bytecode
BYTECODE_LITERALS = [
    "#TPUD2wCAP013E", "#TPUD2wREC0144", "#TPUD2wREC0043", "#TPUD2rVER0051",
    "#TPUD2wDZM0A65", "#TPUD2wDZM0B66", "#TPUD2wDZM0054", "#TPUD2wDZM0458",
    "#TPUD2rDZM004F", "#TPUD2rSDC013F", "#TPUD2wVID004C", "#TPUD2wVID014D",
    "#TPUD2wVID024E", "#TPUD2wVID034F", "#TPUD2rVID0047", "#TPUD2wIMG0147",
    "#TPUD2wIMG0B58", "#TPUD2rIMG0041", "#TPUD2wRST0062", "#TPUD2wRTF0156",
    "#TPUD2rEXT0055", "#TPUG2wPTZ056F", "#TPUG2wPTZ016B", "#TPUG2wPTZ026C",
    "#TPUG2wPTZ036D", "#TPUG2wPTZ046E", "#TPUG2wGSY005F", "#TPUG2wGSP0056",
    "#TPUG2wGAA0A46", "#TPUG2wGAA0035", "#TPUG6wGAY0BB8103E", "#TPUG6wGAPDCD8104C",
]

# From skydroid-c12-protocol.md — strings extracted from the APK
APK_LITERALS = [
    "#TPUD2wRST0163", "#TPUG2wPTZ006A", "#TPUG2wGSY6368", "#TPUG2wGSY9D7C",
    "#TPUM2wZMC015D", "#TPUM2wFCC013F", "#TPUD2wTAR0050", "#TPUD2wTAR0A61",
    "#TPUG6wGAY0000631A", "#TPUG6wGAY01C26330", "#TPUG6wGAY11946329",
]

ALL_LITERALS = BYTECODE_LITERALS + APK_LITERALS


def test_golden_set_is_complete():
    """43 literals — the number stated in PLAN_WEBAPP_C12.md."""
    assert len(ALL_LITERALS) == 43
    assert len(set(ALL_LITERALS)) == 43, "there is a duplicate literal"


@pytest.mark.parametrize("literal", ALL_LITERALS)
def test_checksum_matches(literal):
    body, crc = literal[:-2], literal[-2:]
    assert checksum(body) == crc


@pytest.mark.parametrize("literal", ALL_LITERALS)
def test_seal_reproduces_literal(literal):
    assert seal(literal[:-2]) == literal


@pytest.mark.parametrize("literal", ALL_LITERALS)
def test_roundtrip_parse_then_build(literal):
    """Parsing then rebuilding must reproduce the original string exactly."""
    f = parse(literal)
    assert build(f.dest, f.rw, f.cmd3, f.data, src=f.src, header=f.header) == literal


@pytest.mark.parametrize("literal", ALL_LITERALS)
def test_length_field_matches_data(literal):
    f = parse(literal)
    assert f.length == len(f.data)


# --------------------------------------------------------------------------
# Special cases
# --------------------------------------------------------------------------


def test_lowercase_header_for_ext():
    """EXT uses a lowercase header, and the checksum covers that lowercase text.

    This is a real trap: anywhere that accidentally .upper()s the body produces a
    wrong checksum.
    """
    frame = build("D", "w", "EXT", "0110", header=HEADER_LOWER)
    assert frame == "#tpUD4wEXT0110FE"
    assert parse(frame).header == "#tp"
    # Same content with an uppercase header yields a different checksum — proof
    # the two are not interchangeable.
    assert build("D", "w", "EXT", "0110") != frame


def test_hex_length_field_above_nine():
    """The length field is one hex character: 11 → 'B', 12 → 'C', 15 → 'F'."""
    assert build("D", "w", "VOM", "0" * 11)[5] == "B"
    assert build("D", "w", "IQE", "0" * 12)[5] == "C"
    assert build("D", "w", "TIM", "0" * 15)[5] == "F"


def test_data_longer_than_fifteen_rejected():
    with pytest.raises(FrameError, match="length"):
        build("D", "w", "VOM", "0" * 16)


def test_parse_camera_reply_direction():
    """The camera replies with src/dest swapped: #TPDU..."""
    reply = seal("#TPDU2rDZM0A")
    f = parse(reply)
    assert (f.src, f.dest) == ("D", "U")
    assert f.cmd3 == "DZM"
    assert f.data == "0A"


def test_bad_checksum_rejected():
    with pytest.raises(FrameError, match="bad checksum"):
        parse("#TPUD2wCAP0100")


def test_bad_checksum_accepted_when_verify_off():
    assert parse("#TPUD2wCAP0100", verify=False).cmd3 == "CAP"


def test_truncated_frame_rejected():
    with pytest.raises(FrameError):
        parse("#TPUD2wCAP")


def test_missing_header_rejected():
    with pytest.raises(FrameError, match="header"):
        parse("XXUD2wCAP013E")


def test_length_longer_than_payload_rejected():
    """The length field says 6 characters but only 2 are there → reject rather
    than reading past the end."""
    with pytest.raises(FrameError, match="length"):
        parse("#TPUD6wCAP013E")


def test_invalid_rw_rejected():
    with pytest.raises(FrameError, match="rw"):
        build("D", "x", "CAP", "01")


def test_bad_cmd3_rejected():
    with pytest.raises(FrameError, match="command word"):
        build("D", "w", "CAPS", "01")


# --------------------------------------------------------------------------
# split_frames — the camera packs several frames into one UDP datagram
# --------------------------------------------------------------------------


def test_split_crlf_separated():
    buf = "#TPUD2wCAP013E\r\n#TPUD2rVER0051\r\n"
    assert [f.cmd3 for f in split_frames(buf)] == ["CAP", "VER"]


def test_split_without_separator():
    """Does not rely on CRLF: it cuts exactly 12 + length characters."""
    buf = "#TPUD2wCAP013E" + "#TPUG6wGAY0BB8103E" + "#TPUD2rVER0051"
    assert [f.cmd3 for f in split_frames(buf)] == ["CAP", "GAY", "VER"]


def test_split_skips_garbage_between_frames():
    buf = "junk#TPUD2wCAP013E\x00\xffjunk#TPUD2rVER0051"
    assert [f.cmd3 for f in split_frames(buf)] == ["CAP", "VER"]


def test_split_skips_bad_checksum_but_keeps_the_rest():
    """One mangled packet must not cost us the attitude frame right after it."""
    buf = "#TPUD2wCAP0100" + "#TPUG6wGAY0BB8103E"
    assert [f.cmd3 for f in split_frames(buf)] == ["GAY"]


def test_split_empty_buffer():
    assert split_frames("") == []


def test_split_partial_trailing_frame_ignored():
    buf = "#TPUD2wCAP013E#TPUD2rVE"
    assert [f.cmd3 for f in split_frames(buf)] == ["CAP"]


def test_split_lowercase_header_found():
    buf = "#tpUD4wEXT0110FE#TPUD2rVER0051"
    assert [f.cmd3 for f in split_frames(buf)] == ["EXT", "VER"]


# --------------------------------------------------------------------------
# Wire
# --------------------------------------------------------------------------


def test_wire_appends_crlf():
    """The bytecode appends CRLF when writing to the socket; c12_probe.py omits it."""
    assert to_wire("#TPUD2rVER0051") == b"#TPUD2rVER0051\r\n"
