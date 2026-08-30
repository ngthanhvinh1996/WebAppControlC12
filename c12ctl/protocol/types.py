"""Data types and parameter encoders for the C12 protocol.

Every constant here comes from the ``rcsdk-v1.9.2.aar`` bytecode (see
PLAN_WEBAPP_C12.md §1), not from APK strings. In two places the sources
disagree and the bytecode wins:

* Gimbal speed: ``raw = deg_per_s / 0.5``, clamped to ±127 → real range
  **±63.5 °/s**. The table in ``skydroid-c12-protocol.md`` is off by exactly 2×.
* The thermal palette lives on command word ``IMG``, **not** ``TAR``
  (``TAR`` is spatial noise reduction on a 0–100 scale).
"""

from __future__ import annotations

import enum

# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


class Dest(str, enum.Enum):
    """Destination address in a command frame."""

    CAMERA = "D"
    GIMBAL = "G"


class RiskLevel(enum.IntEnum):
    """Risk tier, enforced in the service layer — see PLAN_WEBAPP_C12.md §3.1."""

    SAFE = 0
    """Read-only. Always allowed."""

    REVERSIBLE = 1
    """Changes state but is easy to undo. Allowed; show the current value."""

    PHYSICAL = 2
    """Causes mechanical motion. Only while the session is armed and the
    watchdog is alive."""

    DANGEROUS = 3
    """Can lose the device permanently. MUST NOT appear in the registry."""


class Confidence(str, enum.Enum):
    """Where a registry entry came from — for the UI, and to know what to verify."""

    BYTECODE = "bytecode"
    """Decompiled from rcsdk-v1.9.2.aar. Gives the formula, not just the result."""

    APK_STRING = "apk-string"
    """Only seen as a string in the APK. The meaning is inferred."""

    HYPOTHESIS = "hypothesis"
    """A guess. Verify before trusting it."""


# --------------------------------------------------------------------------
# Parameter enums
# --------------------------------------------------------------------------


class Palette(str, enum.Enum):
    """Thermal pseudo-color, command word ``IMG``.

    Exactly 11 entries — matching the 11 palettes counted in the APK resources.
    Value ``02`` is left unused in the bytecode.
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
    """Recording resolution, command word ``VID``.

    The value is the **on-the-wire data** (2 characters), not the ordinal of the
    SDK's ``Resolution`` enum. ``VID`` has a length field of 2, so 720P goes out
    as ``00`` rather than ``0`` — keeping the wire form here means nothing else
    has to remember to pad, the same way :class:`Palette` works.
    """

    R_720P = "00"
    R_1080P = "01"
    R_2K = "02"
    R_4K = "03"


class AKey(str, enum.Enum):
    """One-key gimbal commands, command word ``PTZ``.

    Only 01–05 are declared. The bytecode shows ``0A``/``0B`` switch the mount
    mode, ``06``–``08`` switch the control mode, and ``0C``/``0D`` **start a
    gimbal calibration** — none of which may reach the registry.
    """

    UP = "01"
    DOWN = "02"
    LEFT = "03"
    RIGHT = "04"
    CENTER = "05"


# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------

SPEED_SCALE = 0.5
"""°/s per raw step (bytecode: the constant 0.5f)."""

SPEED_RAW_LIMIT = 127
MAX_SPEED_DPS = SPEED_RAW_LIMIT * SPEED_SCALE  # 63.5

ANGLE_SCALE = 100
"""Raw steps per degree (bytecode: multiply by 100)."""

ANGLE_RAW_LIMIT = 9000
MAX_ANGLE_DEG = ANGLE_RAW_LIMIT / ANGLE_SCALE  # 90.0

DEFAULT_GOTO_SPEED = 0x10
"""Speed suffix on the absolute-angle commands (GAY/GAP/GAM)."""


# --------------------------------------------------------------------------
# Numeric encoding
# --------------------------------------------------------------------------


def u8_hex(value: int) -> str:
    """int → 2 uppercase hex chars, two's complement for negatives."""
    return "%02X" % (int(value) & 0xFF)


def s16_hex(value: int) -> str:
    """int16 two's complement → 4 uppercase hex chars
    (SkydroidGimbalControlCore.short2Hex)."""
    return "%04X" % (int(value) & 0xFFFF)


def parse_s16(text: str) -> int:
    """4 hex chars → signed int16."""
    raw = int(text, 16)
    return raw - 0x10000 if raw >= 0x8000 else raw


def parse_u8(text: str) -> int:
    return int(text, 16)


def speed_to_raw(deg_per_s: float) -> int:
    """°/s → signed speed byte, clamped to ±127.

    >>> speed_to_raw(25)
    50
    >>> speed_to_raw(99)      # out of range, clamped
    127
    """
    return max(
        -SPEED_RAW_LIMIT, min(SPEED_RAW_LIMIT, int(float(deg_per_s) / SPEED_SCALE))
    )


def raw_to_speed(raw: int) -> float:
    """Signed speed byte → °/s."""
    return raw * SPEED_SCALE


def clamp_speed(deg_per_s: float) -> float:
    """°/s → °/s quantized and clamped exactly as the device will read it."""
    return raw_to_speed(speed_to_raw(deg_per_s))


def angle_to_raw(deg: float) -> int:
    """Degrees → raw int16, clamped to ±9000.

    >>> angle_to_raw(30)
    3000
    """
    return max(-ANGLE_RAW_LIMIT, min(ANGLE_RAW_LIMIT, int(float(deg) * ANGLE_SCALE)))


def raw_to_angle(raw: int) -> float:
    """Raw int16 → degrees."""
    return raw / ANGLE_SCALE


def clamp_angle(deg: float) -> float:
    return raw_to_angle(angle_to_raw(deg))


# --------------------------------------------------------------------------
# Returned structures
# --------------------------------------------------------------------------


class Attitude:
    """Gimbal attitude, decoded from a pushed ``GAC`` frame."""

    __slots__ = ("yaw", "pitch", "roll")

    def __init__(self, yaw: float, pitch: float, roll: float) -> None:
        self.yaw = yaw
        self.pitch = pitch
        self.roll = roll

    @classmethod
    def from_data(cls, data: str) -> "Attitude":
        """``GAC`` carries 3 consecutive int16 hex values; divide by 100 for degrees."""
        if len(data) < 12:
            raise ValueError("a GAC frame needs 12 data characters, got %d" % len(data))
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

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return "Attitude(yaw=%.2f, pitch=%.2f, roll=%.2f)" % (
            self.yaw,
            self.pitch,
            self.roll,
        )


class SDCardStatus:
    """Return value of ``SDC``. Both fields zero means no card is inserted."""

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

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return "SDCardStatus(total_mb=%d, free_mb=%d)" % (self.total_mb, self.free_mb)
