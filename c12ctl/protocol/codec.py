"""Topotek command frame for the Skydroid C12.

    #TP U D 2 w REC 01 44
    │   │ │ │ │  │   │  └── checksum: sum(byte) & 0xFF, 2 uppercase hex chars
    │   │ │ │ │  │   └───── data, character count given by the length field
    │   │ │ │ │  └───────── command word, 3 chars
    │   │ │ │ └──────────── 'r' = read, 'w' = write
    │   │ │ └────────────── length: data character count, 1 hex char
    │   │ └──────────────── destination address: D = camera, G = gimbal
    │   └────────────────── source address: U = host
    └────────────────────── header, normally "#TP"

Two details that break code if missed, both taken from the RCSDK bytecode:

* Packets written to the socket **end with CRLF**. `c12_probe.py` omits it, and
  that may be why some read commands never answered there.
* The `EXT` command uses a **lowercase** header ``#tp``. The checksum is computed
  over that exact lowercase text, so never ``.upper()`` the body anywhere in
  this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEADER = "#TP"
HEADER_LOWER = "#tp"

#: Packet terminator when writing to the socket
#: (SkydroidGimbalControlCore.sendCmdData).
TERMINATOR = b"\r\n"

#: Total frame length excluding data: header(3) + src(1) + dest(1) + len(1)
#: + rw(1) + cmd3(3) + crc(2).
_OVERHEAD = 12

_FRAME_START = re.compile(r"#[Tt][Pp]")


class FrameError(ValueError):
    """Malformed frame, bad checksum, or a length field that does not match."""


def checksum(body: str) -> str:
    """Sum of the body's UTF-8 bytes, low 8 bits, as 2 uppercase hex chars."""
    return "%02X" % (sum(body.encode("utf-8")) & 0xFF)


def seal(body: str) -> str:
    """Append the checksum to a frame body."""
    return body + checksum(body)


def build(
    dest: str,
    rw: str,
    cmd3: str,
    data: str = "",
    src: str = "U",
    header: str = HEADER,
) -> str:
    """Build a complete frame including its checksum.

    >>> build("D", "w", "CAP", "01")
    '#TPUD2wCAP013E'
    >>> build("D", "w", "EXT", "0110", header=HEADER_LOWER)
    '#tpUD4wEXT0110FE'
    """
    if len(dest) != 1 or len(src) != 1:
        raise FrameError("src/dest must be exactly 1 character")
    if rw not in ("r", "w"):
        raise FrameError("rw must be 'r' or 'w', got %r" % rw)
    if len(cmd3) != 3:
        raise FrameError("command word must be 3 characters, got %r" % cmd3)
    n = len(data)
    if not 0 <= n <= 15:
        raise FrameError("data is %d characters; the length field only holds 0..15" % n)
    return seal(f"{header}{src}{dest}{n:X}{rw}{cmd3}{data}")


@dataclass(frozen=True)
class Frame:
    """A frame split into its fields."""

    raw: str
    header: str
    src: str
    dest: str
    length: int
    rw: str
    cmd3: str
    data: str
    crc: str

    @property
    def body(self) -> str:
        return self.raw[:-2]

    def __str__(self) -> str:  # pragma: no cover - debugging convenience
        return self.raw


def parse(text: str, *, verify: bool = True) -> Frame:
    """Split one frame. Raises :class:`FrameError` on bad format or checksum.

    >>> parse("#TPUD2wCAP013E").cmd3
    'CAP'
    """
    text = text.strip()
    if len(text) < _OVERHEAD:
        raise FrameError("frame too short: %r" % text)
    if not _FRAME_START.match(text):
        raise FrameError("missing #TP header: %r" % text)

    header, src, dest = text[:3], text[3], text[4]
    try:
        length = int(text[5], 16)
    except ValueError:
        raise FrameError("length field is not hex: %r" % text[5]) from None
    rw, cmd3 = text[6], text[7:10]
    data = text[10 : 10 + length]
    crc = text[10 + length : 12 + length]

    if len(data) != length:
        raise FrameError(
            "length says %d data characters but only %d are present: %r"
            % (length, len(data), text)
        )
    if len(crc) != 2:
        raise FrameError("missing checksum: %r" % text)

    frame = Frame(
        raw=text[: 12 + length],
        header=header,
        src=src,
        dest=dest,
        length=length,
        rw=rw,
        cmd3=cmd3,
        data=data,
        crc=crc,
    )
    if verify:
        expected = checksum(frame.body)
        if expected != crc.upper():
            raise FrameError(
                "bad checksum for %r: computed %s, received %s"
                % (frame.raw, expected, crc)
            )
    return frame


def split_frames(text: str, *, verify: bool = True) -> list[Frame]:
    """Split out every valid frame in a buffer.

    Does not rely on a delimiter: it locates the header, then cuts exactly
    ``12 + length`` characters. The camera packs several frames into one UDP
    datagram and does not always put CRLF between them — this handles both.

    A corrupt frame is skipped rather than poisoning the whole buffer: one
    mangled packet should not cost us the attitude frame that follows it.
    """
    out: list[Frame] = []
    pos = 0
    while True:
        m = _FRAME_START.search(text, pos)
        if m is None:
            return out
        start = m.start()
        try:
            frame = parse(text[start:], verify=verify)
        except FrameError:
            pos = start + 1
            continue
        out.append(frame)
        pos = start + len(frame.raw)


def to_wire(frame: str) -> bytes:
    """Frame string → bytes for the socket, CRLF included."""
    return frame.encode("utf-8") + TERMINATOR
