"""Command registry — the single source of truth.

Every command is declared once, here. From this table we generate, all at once:

* the **allowlist** in the service layer (not in the registry = cannot be sent),
* the Diagnostics and Camera pages of the UI,
* the test suite.

Allowlist, not blocklist: an unknown command is **blocked** by default. For a
device whose only way in is Ethernet, with no UART and no reset button, that is
the only acceptable default direction.

:data:`RiskLevel.DANGEROUS` never appears in ``COMMANDS``. :data:`FORBIDDEN`
lists those commands for defense in depth and to document why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .codec import HEADER, HEADER_LOWER, build
from .types import (
    AKey,
    Confidence,
    Dest,
    Palette,
    Resolution,
    RiskLevel,
    SDCardStatus,
    angle_to_raw,
    parse_u8,
    s16_hex,
    speed_to_raw,
    u8_hex,
    DEFAULT_GOTO_SPEED,
)

DEFAULT_TIMEOUT = 1.0


@dataclass(frozen=True)
class Command:
    """A command that is allowed to be sent."""

    name: str
    """Stable identifier, shaped ``group.action``. This is the REST API key."""

    dest: Dest
    rw: str
    cmd3: str
    risk: RiskLevel
    confidence: Confidence

    doc: str = ""
    encode: Callable[..., str] | None = None
    """Parameters → data string. ``None`` means the command takes no parameters."""

    decode: Callable[[str], object] | None = None
    """Reply data string → Python value."""

    data: str = ""
    """Fixed data, used when ``encode`` is ``None``."""

    timeout: float = DEFAULT_TIMEOUT
    """Declared per command: ``setVideoConfig`` needs 4000 ms, many times more
    than anything else."""

    header: str = HEADER
    """``EXT`` uses the lowercase ``#tp`` header — exactly as in the bytecode."""

    expect_reply: bool = False
    """Read commands wait for a reply; most write commands do not."""

    source: str = ""
    """Where this constant came from, so it can be traced when the two source
    documents disagree."""

    def frame(self, *args, **kwargs) -> str:
        """Build the complete frame including its checksum."""
        if self.encode is None:
            if args or kwargs:
                raise TypeError("%s takes no parameters" % self.name)
            data = self.data
        else:
            data = self.encode(*args, **kwargs)
        return build(
            dest=self.dest.value,
            rw=self.rw,
            cmd3=self.cmd3,
            data=data,
            header=self.header,
        )


# --------------------------------------------------------------------------
# Reply decoders
# --------------------------------------------------------------------------


def _decode_sdcard(data: str) -> SDCardStatus:
    """``SDC`` returns total plus free capacity. The exact format is not yet
    verified against hardware — phase 1 records the raw string to settle it."""
    half = len(data) // 2
    try:
        return SDCardStatus(int(data[:half], 16), int(data[half:], 16))
    except ValueError:
        return SDCardStatus(0, 0)


def _decode_palette(data: str) -> str:
    try:
        return Palette(data.upper()).name
    except ValueError:
        return "UNKNOWN(%s)" % data


def _decode_resolution(data: str) -> str:
    try:
        return Resolution(data.zfill(2)).name
    except ValueError:
        return "UNKNOWN(%s)" % data


def _decode_bool(data: str) -> bool:
    return data.strip("0") != ""


# --------------------------------------------------------------------------
# Parameter encoders
# --------------------------------------------------------------------------


def _enc_palette(value) -> str:
    return value.value if isinstance(value, Palette) else Palette[str(value).upper()].value


def _enc_resolution(value) -> str:
    return (
        value.value
        if isinstance(value, Resolution)
        else Resolution[str(value).upper()].value
    )


def _enc_akey(value) -> str:
    return value.value if isinstance(value, AKey) else AKey[str(value).upper()].value


def _enc_speed(deg_per_s: float) -> str:
    return u8_hex(speed_to_raw(deg_per_s))


def _enc_speed_pair(yaw_dps: float, pitch_dps: float) -> str:
    return u8_hex(speed_to_raw(yaw_dps)) + u8_hex(speed_to_raw(pitch_dps))


def _enc_angle(deg: float, speed: int = DEFAULT_GOTO_SPEED) -> str:
    return s16_hex(angle_to_raw(deg)) + u8_hex(speed)


def _enc_angle_pair(
    yaw_deg: float, pitch_deg: float, speed: int = DEFAULT_GOTO_SPEED
) -> str:
    return _enc_angle(yaw_deg, speed) + _enc_angle(pitch_deg, speed)


def _enc_percent(value: int) -> str:
    """Thermal image parameter, 0–100 scale."""
    return u8_hex(max(0, min(100, int(value))))


def _enc_rate_hz(value: int) -> str:
    return u8_hex(max(0, min(100, int(value))))


# --------------------------------------------------------------------------
# Read commands — 🟢 SAFE
# --------------------------------------------------------------------------

_READ_SPECS: list[tuple[str, str, str, Callable | None]] = [
    ("version", "VER", "Camera firmware version", None),
    ("hardware_version", "HWV", "Hardware version", None),
    ("model", "MOD", "Camera model", None),
    ("recording", "REC", "Whether recording is in progress", _decode_bool),
    ("palette", "IMG", "Current thermal palette", _decode_palette),
    ("resolution", "VID", "Recording resolution", _decode_resolution),
    ("zoom", "DZM", "Current digital zoom level", parse_u8),
    ("thermal_spatial_nr", "TAR", "Spatial noise reduction, 0–100", parse_u8),
    ("thermal_shutter", "TAS", "Shutter period, 5–100", parse_u8),
    ("thermal_detail", "TDI", "Detail enhancement, 0–100", parse_u8),
    ("thermal_gamma", "TGM", "Gamma, 0–100", parse_u8),
    ("thermal_brightness", "TIB", "Brightness, 0–100", parse_u8),
    ("thermal_contrast", "TIC", "Contrast, 0–100", parse_u8),
    ("thermal_scene", "TSM", "Scene mode — the C12 may not support it", parse_u8),
    ("thermal_temporal_nr", "TTR", "Temporal noise reduction, 0–100", parse_u8),
    ("ranging", "SLR", "Laser rangefinder — bytecode says C13/C14 only", None),
    ("ext_config", "EXT", "LED / OSD / calibration configuration", None),
    ("video_config", "VOM", "Stream parameters: flip, fps, GOP, bitrate", None),
    ("image_quality", "IQE", "Image quality tuning", None),
    ("ip_address", "IPV", "Camera IP address — READ ONLY", None),
    ("gateway", "GTW", "Camera gateway — READ ONLY", None),
]


def _read_commands() -> dict[str, Command]:
    out: dict[str, Command] = {}
    for name, cmd3, doc, decode in _READ_SPECS:
        out["read." + name] = Command(
            name="read." + name,
            dest=Dest.CAMERA,
            rw="r",
            cmd3=cmd3,
            data="00",
            risk=RiskLevel.SAFE,
            confidence=Confidence.BYTECODE,
            doc=doc,
            decode=decode,
            expect_reply=True,
            source="PHAN_TICH_SDK_C12.md §4",
        )
    # SDC appears with both data 00 and 01 in the APK; the bytecode uses 01 for
    # capacity.
    out["read.sdcard"] = Command(
        name="read.sdcard",
        dest=Dest.CAMERA,
        rw="r",
        cmd3="SDC",
        data="01",
        risk=RiskLevel.SAFE,
        confidence=Confidence.BYTECODE,
        doc="SD card capacity; both values zero means no card is inserted",
        decode=_decode_sdcard,
        expect_reply=True,
        source="PHAN_TICH_SDK_C12.md §4.2",
    )
    out["read.sdcard_alt"] = Command(
        name="read.sdcard_alt",
        dest=Dest.CAMERA,
        rw="r",
        cmd3="SDC",
        data="00",
        risk=RiskLevel.SAFE,
        confidence=Confidence.APK_STRING,
        doc="SDC data=00 variant seen in the APK — phase 1 determines how it "
            "differs from data=01",
        expect_reply=True,
        source="skydroid-c12-protocol.md §8.2",
    )
    return out


# --------------------------------------------------------------------------
# Camera write commands — 🟡 REVERSIBLE
# --------------------------------------------------------------------------


def _write_commands() -> dict[str, Command]:
    C, W = Dest.CAMERA, "w"
    B, R = Confidence.BYTECODE, RiskLevel.REVERSIBLE
    src = "PHAN_TICH_SDK_C12.md §4"

    cmds = [
        Command("camera.snap", C, W, "CAP", R, B, "Take one photo to the SD card",
                data="01", source=src),
        Command("camera.record_start", C, W, "REC", R, B, "Start recording",
                data="01", source=src),
        Command("camera.record_stop", C, W, "REC", R, B, "Stop recording",
                data="00", source=src),
        Command("camera.zoom_in", C, W, "DZM", R, B,
                "Digital zoom in one step. Use this instead of setZoomRatios: the "
                "bytecode hard-caps the absolute setter at 0–4 while the real "
                "range is 0–67",
                data="0A", source=src),
        Command("camera.zoom_out", C, W, "DZM", R, B, "Digital zoom out one step",
                data="0B", source=src),
        Command("camera.palette", C, W, "IMG", R, B,
                "Set the thermal palette. It is IMG, not TAR — TAR is spatial "
                "noise reduction, and sweeping it would wreck the sensor config",
                encode=_enc_palette, source=src + ".4"),
        Command("camera.resolution", C, W, "VID", R, B, "Set the recording resolution",
                encode=_enc_resolution, source=src + ".3"),
    ]

    thermal = [
        ("thermal_spatial_nr", "TAR", "Spatial noise reduction, 0–100"),
        ("thermal_shutter", "TAS", "Shutter period, 5–100"),
        ("thermal_detail", "TDI", "Detail enhancement, 0–100"),
        ("thermal_gamma", "TGM", "Gamma, 0–100"),
        ("thermal_brightness", "TIB", "Brightness, 0–100"),
        ("thermal_contrast", "TIC", "Contrast, 0–100"),
        ("thermal_temporal_nr", "TTR", "Temporal noise reduction, 0–100"),
    ]
    for suffix, cmd3, doc in thermal:
        cmds.append(
            Command("camera." + suffix, C, W, cmd3, R, B, doc,
                    encode=_enc_percent, source=src + ".4")
        )

    return {c.name: c for c in cmds}


# --------------------------------------------------------------------------
# Telemetry — 🟡 REVERSIBLE (enables a push stream, causes no motion)
# --------------------------------------------------------------------------


def _telemetry_commands() -> dict[str, Command]:
    src = "PHAN_TICH_SDK_C12.md §5.4"
    cmds = [
        Command(
            "telemetry.push_attitude", Dest.GIMBAL, "w", "GAA",
            RiskLevel.REVERSIBLE, Confidence.BYTECODE,
            "Make the camera push GAC frames at N Hz (0 = off). Only takes "
            "effect AFTER the camera is producing video — send it a few times "
            "at startup. Because it defaults to off, "
            "skydroid-c12-protocol.md wrongly concluded there is no telemetry",
            encode=_enc_rate_hz, source=src,
        ),
    ]
    return {c.name: c for c in cmds}


# --------------------------------------------------------------------------
# Gimbal — 🟠 PHYSICAL
# --------------------------------------------------------------------------


def _gimbal_commands() -> dict[str, Command]:
    G, W, P, B = Dest.GIMBAL, "w", RiskLevel.PHYSICAL, Confidence.BYTECODE
    src = "PHAN_TICH_SDK_C12.md §5"

    cmds = [
        Command("gimbal.yaw_speed", G, W, "GSY", P, B,
                "Yaw speed, °/s. Negative = left. Range ±63.5, step 0.5",
                encode=_enc_speed, source=src + ".2"),
        Command("gimbal.pitch_speed", G, W, "GSP", P, B,
                "Pitch speed, °/s. Negative = down. Range ±63.5, step 0.5",
                encode=_enc_speed, source=src + ".2"),
        Command("gimbal.speed", G, W, "GSM", P, B,
                "Yaw and pitch speed in one packet — half the traffic at 20 Hz. "
                "Needs gimbal firmware >= 0.5; probe at startup and fall back to "
                "gimbal.yaw_speed + gimbal.pitch_speed if unsupported",
                encode=_enc_speed_pair, source=src + ".2"),
        Command("gimbal.goto_yaw", G, W, "GAY", P, B,
                "Absolute yaw, degrees. Clamped to ±90.00",
                encode=_enc_angle, source=src + ".3"),
        Command("gimbal.goto_pitch", G, W, "GAP", P, B,
                "Absolute pitch, degrees. Clamped to ±90.00",
                encode=_enc_angle, source=src + ".3"),
        Command("gimbal.goto", G, W, "GAM", P, B,
                "Absolute yaw and pitch in one packet. DO NOT use before "
                "goto_yaw and goto_pitch have confirmed the angle unit against "
                "GAC — a wrong unit on two axes is twice the consequence",
                encode=_enc_angle_pair, source=src + ".3"),
        Command("gimbal.akey", G, W, "PTZ", P, B,
                "One-key command: UP DOWN LEFT RIGHT CENTER. Only 01–05 are "
                "allowed — 0C/0D start a gimbal calibration",
                encode=_enc_akey, source=src + ".1"),
    ]
    return {c.name: c for c in cmds}


# --------------------------------------------------------------------------
# Master table
# --------------------------------------------------------------------------

COMMANDS: dict[str, Command] = {
    **_read_commands(),
    **_write_commands(),
    **_telemetry_commands(),
    **_gimbal_commands(),
}


#: Emergency stop frames, prebuilt so that stopping never depends on a registry
#: lookup at the moment it is needed.
STOP_FRAMES: tuple[str, ...] = (
    COMMANDS["gimbal.yaw_speed"].frame(0),
    COMMANDS["gimbal.pitch_speed"].frame(0),
)


#: 🔴 DANGEROUS — none of these appear in :data:`COMMANDS`.
#: The list is kept for defense in depth and to record the reasoning.
FORBIDDEN: dict[str, str] = {
    "IPV": "Changes the camera IP. Getting it wrong loses the device for good: "
           "no UART, no reset button, nothing left but rescanning the subnet "
           "and hoping. Reading it is safe.",
    "GTW": "Changes the gateway. Same consequence as IPV. Reading it is safe.",
    "VOM": "Changes the video stream configuration. Can break RTSP — losing "
           "both the video and the ability to diagnose. Reading it is safe.",
    "IQE": "Changes the encoder configuration. Same risk as VOM. Reading is safe.",
    "RST": "Reboots the camera. If you want a reboot, cut the power.",
    "RTF": "Factory reset.",
    "GAR": "Roll angle. The bytecode marks it 'not recommended'.",
    "TIM": "Sets the time; the format is unverified.",
    "FCC": "Focus motor — for models with a mechanical lens, not the C12.",
    "ZMC": "Optical zoom motor — mechanical-lens models. The C12 uses DZM.",
}

#: ``PTZ`` data values that must never be sent, even if someone adds them to the
#: enum by mistake.
FORBIDDEN_PTZ_DATA: frozenset[str] = frozenset(
    {"00", "06", "07", "08", "09", "0A", "0B", "0C", "0D",
     "0E", "0F", "10", "11", "12", "13", "14"}
)


class CommandNotAllowed(PermissionError):
    """The command is not in the allowlist, or is blocked by its risk level."""


def get(name: str) -> Command:
    """Look up a command. Raises :class:`CommandNotAllowed` if it is not declared."""
    try:
        return COMMANDS[name]
    except KeyError:
        raise CommandNotAllowed(
            "%r is not in the registry. Allowlist, not blocklist: an undeclared "
            "command cannot be sent." % name
        ) from None


def by_risk(level: RiskLevel) -> list[Command]:
    return sorted(
        (c for c in COMMANDS.values() if c.risk == level), key=lambda c: c.name
    )


def read_commands() -> list[Command]:
    """Every read command, used by the Diagnostics page."""
    return by_risk(RiskLevel.SAFE)


def assert_registry_sane() -> None:
    """Registry invariants. Call at startup — better to die early than misfire."""
    for name, cmd in COMMANDS.items():
        if cmd.name != name:
            raise AssertionError("key %r disagrees with Command.name %r" % (name, cmd.name))
        if cmd.risk is RiskLevel.DANGEROUS:
            raise AssertionError("%s is DANGEROUS yet present in the registry" % name)
        if cmd.rw == "w" and cmd.cmd3 in FORBIDDEN:
            raise AssertionError(
                "%s writes to %s, which is on the forbidden list: %s"
                % (name, cmd.cmd3, FORBIDDEN[cmd.cmd3])
            )
        if cmd.encode is None and cmd.rw == "w" and not cmd.data:
            raise AssertionError("%s is a write command with neither data nor encode" % name)
    for key in AKey:
        if key.value in FORBIDDEN_PTZ_DATA:
            raise AssertionError("AKey.%s = %s is in the forbidden PTZ range"
                                 % (key.name, key.value))
