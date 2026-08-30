"""Khung lệnh Topotek cho Skydroid C12.

    #TP U D 2 w REC 01 44
    │   │ │ │ │  │   │  └── checksum: sum(byte) & 0xFF, 2 hex hoa
    │   │ │ │ │  │   └───── data, số ký tự = trường length
    │   │ │ │ │  └───────── command word, 3 ký tự
    │   │ │ │ └──────────── 'r' = read, 'w' = write
    │   │ │ └────────────── length: số ký tự của data, 1 ký tự hex
    │   │ └──────────────── địa chỉ đích: D = camera, G = gimbal
    │   └────────────────── địa chỉ nguồn: U = host
    └────────────────────── header, thường "#TP"

Hai chi tiết dễ làm hỏng code, đều lấy từ bytecode RCSDK:

* Gói ghi xuống socket **kết thúc bằng CRLF**. `c12_probe.py` không gửi phần này và
  đó có thể là lý do một số lệnh đọc không phản hồi.
* Lệnh `EXT` dùng header **chữ thường** ``#tp``. Checksum tính trên đúng chữ thường đó,
  nên tuyệt đối không ``.upper()`` phần thân ở bất cứ đâu trong module này.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEADER = "#TP"
HEADER_LOWER = "#tp"

#: Byte kết thúc gói khi ghi xuống socket (SkydroidGimbalControlCore.sendCmdData).
TERMINATOR = b"\r\n"

#: Tổng độ dài khung không tính data: header(3) + src(1) + dest(1) + len(1)
#: + rw(1) + cmd3(3) + crc(2).
_OVERHEAD = 12

_FRAME_START = re.compile(r"#[Tt][Pp]")


class FrameError(ValueError):
    """Khung lệnh sai định dạng, sai checksum, hoặc sai trường length."""


def checksum(body: str) -> str:
    """Tổng byte UTF-8 của thân lệnh, lấy 8 bit thấp, in hex hoa 2 ký tự."""
    return "%02X" % (sum(body.encode("utf-8")) & 0xFF)


def seal(body: str) -> str:
    """Gắn checksum vào thân lệnh."""
    return body + checksum(body)


def build(
    dest: str,
    rw: str,
    cmd3: str,
    data: str = "",
    src: str = "U",
    header: str = HEADER,
) -> str:
    """Dựng một khung hoàn chỉnh kèm checksum.

    >>> build("D", "w", "CAP", "01")
    '#TPUD2wCAP013E'
    >>> build("D", "w", "EXT", "0110", header=HEADER_LOWER)
    '#tpUD4wEXT0110FE'
    """
    if len(dest) != 1 or len(src) != 1:
        raise FrameError("src/dest phải là 1 ký tự")
    if rw not in ("r", "w"):
        raise FrameError("rw phải là 'r' hoặc 'w', nhận %r" % rw)
    if len(cmd3) != 3:
        raise FrameError("command word phải là 3 ký tự, nhận %r" % cmd3)
    n = len(data)
    if not 0 <= n <= 15:
        raise FrameError("data dài %d ký tự, trường length chỉ chứa được 0..15" % n)
    return seal(f"{header}{src}{dest}{n:X}{rw}{cmd3}{data}")


@dataclass(frozen=True)
class Frame:
    """Một khung đã tách trường."""

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

    def __str__(self) -> str:  # pragma: no cover - tiện debug
        return self.raw


def parse(text: str, *, verify: bool = True) -> Frame:
    """Tách một khung. Ném :class:`FrameError` nếu sai định dạng hoặc checksum.

    >>> parse("#TPUD2wCAP013E").cmd3
    'CAP'
    """
    text = text.strip()
    if len(text) < _OVERHEAD:
        raise FrameError("khung quá ngắn: %r" % text)
    if not _FRAME_START.match(text):
        raise FrameError("thiếu header #TP: %r" % text)

    header, src, dest = text[:3], text[3], text[4]
    try:
        length = int(text[5], 16)
    except ValueError:
        raise FrameError("trường length không phải hex: %r" % text[5]) from None
    rw, cmd3 = text[6], text[7:10]
    data = text[10 : 10 + length]
    crc = text[10 + length : 12 + length]

    if len(data) != length:
        raise FrameError(
            "length nói %d ký tự data nhưng chỉ có %d: %r" % (length, len(data), text)
        )
    if len(crc) != 2:
        raise FrameError("thiếu checksum: %r" % text)

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
                "checksum sai cho %r: tính được %s, nhận %s" % (frame.raw, expected, crc)
            )
    return frame


def split_frames(text: str, *, verify: bool = True) -> list[Frame]:
    """Tách mọi khung hợp lệ trong một buffer.

    Không dựa vào dấu phân cách: định vị bằng header rồi cắt đúng ``12 + length``
    ký tự. Camera gộp nhiều khung trong một gói UDP, và không phải khi nào cũng
    chèn CRLF giữa chúng — cách này chịu được cả hai kiểu.

    Khung hỏng bị bỏ qua thay vì làm hỏng cả buffer, vì một gói méo không nên
    khiến ta mất luôn gói tư thế đi kèm sau nó.
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
    """Chuỗi khung → byte gửi lên socket, kèm CRLF."""
    return frame.encode("utf-8") + TERMINATOR
