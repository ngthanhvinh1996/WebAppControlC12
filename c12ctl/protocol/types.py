"""Kiểu dữ liệu và bộ mã hoá tham số cho giao thức C12.

Mọi hằng số ở đây lấy từ bytecode ``rcsdk-v1.9.2.aar`` (xem PLAN_WEBAPP_C12.md §1),
không lấy từ chuỗi APK. Hai chỗ hai nguồn mâu thuẫn và bytecode thắng:

* Tốc độ gimbal: ``raw = deg_per_s / 0.5``, clamp ±127 → dải thật **±63.5 °/s**.
  Bảng trong ``skydroid-c12-protocol.md`` gán nhãn lệch đúng hệ số 2.
* Palette nhiệt nằm ở command word ``IMG``, **không phải** ``TAR``
  (``TAR`` là khử nhiễu không gian, thang 0–100).
"""

from __future__ import annotations

import enum

# --------------------------------------------------------------------------
# Phân loại
# --------------------------------------------------------------------------


class Dest(str, enum.Enum):
    """Địa chỉ đích trong khung lệnh."""

    CAMERA = "D"
    GIMBAL = "G"


class RiskLevel(enum.IntEnum):
    """Mức rủi ro, cưỡng chế ở tầng service — xem PLAN_WEBAPP_C12.md §3.1."""

    SAFE = 0
    """Read-only. Luôn cho phép."""

    REVERSIBLE = 1
    """Đổi trạng thái nhưng khôi phục dễ. Cho phép, hiển thị giá trị hiện tại."""

    PHYSICAL = 2
    """Gây chuyển động cơ khí. Chỉ khi phiên đang ARM và watchdog còn sống."""

    DANGEROUS = 3
    """Có thể mất kết nối vĩnh viễn. KHÔNG được có mặt trong registry."""


class Confidence(str, enum.Enum):
    """Nguồn của một mục registry — để UI hiển thị và để biết cái gì cần verify."""

    BYTECODE = "bytecode"
    """Dịch ngược từ rcsdk-v1.9.2.aar. Biết cả công thức, không chỉ kết quả."""

    APK_STRING = "apk-string"
    """Chỉ thấy chuỗi trong APK. Ngữ nghĩa là suy luận."""

    HYPOTHESIS = "hypothesis"
    """Phỏng đoán. Phải verify trước khi tin."""


# --------------------------------------------------------------------------
# Enum tham số
# --------------------------------------------------------------------------


class Palette(str, enum.Enum):
    """Pseudo-color ảnh nhiệt, command word ``IMG``.

    Đúng 11 mục — khớp con số 11 palette đếm được trong resource APK. Giá trị
    ``02`` bị bỏ trống trong bytecode.
    """

    WHITE_HOT = "01"
    SEPIA = "03"
    IRONBOW = "04"
    RAINBOW = "05"
    NIGHT = "06"
    AURORA = "07"
    RED_HOT = "08"
    JUNGLE = "09"
    MEDICAL = "0A"
    BLACK_HOT = "0B"
    GLORY_HOT = "0C"


class Resolution(str, enum.Enum):
    """Độ phân giải ghi hình, command word ``VID``.

    Giá trị là **data trên dây** (2 ký tự), không phải ordinal của enum
    ``Resolution`` trong SDK. Trường length của ``VID`` là 2, nên 720P đi dây là
    ``00`` chứ không phải ``0`` — giữ nguyên dạng dây ở đây để không có chỗ nào
    phải nhớ pad thêm, giống cách :class:`Palette` làm.
    """

    R_720P = "00"
    R_1080P = "01"
    R_2K = "02"
    R_4K = "03"


class AKey(str, enum.Enum):
    """Lệnh một phím cho gimbal, command word ``PTZ``.

    Chỉ khai báo 01–05. Bytecode cho biết ``0A``/``0B`` đổi chế độ lắp,
    ``06``–``08`` đổi chế độ điều khiển, và ``0C``/``0D`` **khởi động hiệu chuẩn
    gimbal** — không mục nào trong số đó được phép lọt vào registry.
    """

    UP = "01"
    DOWN = "02"
    LEFT = "03"
    RIGHT = "04"
    CENTER = "05"


# --------------------------------------------------------------------------
# Giới hạn
# --------------------------------------------------------------------------

SPEED_SCALE = 0.5
"""°/s trên mỗi bậc raw (bytecode: hằng số 0.5f)."""

SPEED_RAW_LIMIT = 127
MAX_SPEED_DPS = SPEED_RAW_LIMIT * SPEED_SCALE  # 63.5

ANGLE_SCALE = 100
"""Bậc raw trên mỗi độ (bytecode: nhân 100)."""

ANGLE_RAW_LIMIT = 9000
MAX_ANGLE_DEG = ANGLE_RAW_LIMIT / ANGLE_SCALE  # 90.0

DEFAULT_GOTO_SPEED = 0x10
"""Hậu tố tốc độ của lệnh góc tuyệt đối (GAY/GAP/GAM)."""


# --------------------------------------------------------------------------
# Mã hoá số
# --------------------------------------------------------------------------


def u8_hex(value: int) -> str:
    """int → 2 ký tự hex hoa, bù 2 cho số âm."""
    return "%02X" % (int(value) & 0xFF)


def s16_hex(value: int) -> str:
    """int16 bù 2 → 4 ký tự hex hoa (SkydroidGimbalControlCore.short2Hex)."""
    return "%04X" % (int(value) & 0xFFFF)


def parse_s16(text: str) -> int:
    """4 ký tự hex → int16 có dấu."""
    raw = int(text, 16)
    return raw - 0x10000 if raw >= 0x8000 else raw


def parse_u8(text: str) -> int:
    return int(text, 16)


def speed_to_raw(deg_per_s: float) -> int:
    """°/s → byte tốc độ có dấu, clamp ở ±127.

    >>> speed_to_raw(25)
    50
    >>> speed_to_raw(99)      # vượt dải, bị clamp
    127
    """
    return max(
        -SPEED_RAW_LIMIT, min(SPEED_RAW_LIMIT, int(float(deg_per_s) / SPEED_SCALE))
    )


def raw_to_speed(raw: int) -> float:
    """Byte tốc độ có dấu → °/s."""
    return raw * SPEED_SCALE


def clamp_speed(deg_per_s: float) -> float:
    """°/s → °/s đã lượng tử hoá và clamp đúng như thiết bị sẽ hiểu."""
    return raw_to_speed(speed_to_raw(deg_per_s))


def angle_to_raw(deg: float) -> int:
    """Độ → int16 raw, clamp ở ±9000.

    >>> angle_to_raw(30)
    3000
    """
    return max(-ANGLE_RAW_LIMIT, min(ANGLE_RAW_LIMIT, int(float(deg) * ANGLE_SCALE)))


def raw_to_angle(raw: int) -> float:
    """int16 raw → độ."""
    return raw / ANGLE_SCALE


def clamp_angle(deg: float) -> float:
    return raw_to_angle(angle_to_raw(deg))


# --------------------------------------------------------------------------
# Cấu trúc trả về
# --------------------------------------------------------------------------


class Attitude:
    """Tư thế gimbal, giải mã từ gói ``GAC`` tự đẩy."""

    __slots__ = ("yaw", "pitch", "roll")

    def __init__(self, yaw: float, pitch: float, roll: float) -> None:
        self.yaw = yaw
        self.pitch = pitch
        self.roll = roll

    @classmethod
    def from_data(cls, data: str) -> "Attitude":
        """``GAC`` chở 3 int16 hex liên tiếp, chia 100 ra độ."""
        if len(data) < 12:
            raise ValueError("gói GAC cần 12 ký tự data, nhận %d" % len(data))
        return cls(
            yaw=raw_to_angle(parse_s16(data[0:4])),
            pitch=raw_to_angle(parse_s16(data[4:8])),
            roll=raw_to_angle(parse_s16(data[8:12])),
        )

    def as_dict(self) -> dict:
        return {"yaw": self.yaw, "pitch": self.pitch, "roll": self.roll}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Attitude):
            return NotImplemented
        return self.as_dict() == other.as_dict()

    def __repr__(self) -> str:  # pragma: no cover - tiện debug
        return "Attitude(yaw=%.2f, pitch=%.2f, roll=%.2f)" % (
            self.yaw,
            self.pitch,
            self.roll,
        )


class SDCardStatus:
    """Trả về của ``SDC``. Cả hai bằng 0 nghĩa là chưa cắm thẻ."""

    __slots__ = ("total_mb", "free_mb")

    def __init__(self, total_mb: int, free_mb: int) -> None:
        self.total_mb = total_mb
        self.free_mb = free_mb

    @property
    def present(self) -> bool:
        return not (self.total_mb == 0 and self.free_mb == 0)

    def as_dict(self) -> dict:
        return {
            "total_mb": self.total_mb,
            "free_mb": self.free_mb,
            "present": self.present,
        }

    def __repr__(self) -> str:  # pragma: no cover - tiện debug
        return "SDCardStatus(total_mb=%d, free_mb=%d)" % (self.total_mb, self.free_mb)
