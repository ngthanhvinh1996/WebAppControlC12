"""Registry lệnh — nguồn sự thật duy nhất.

Mỗi lệnh khai báo một lần ở đây. Từ bảng này sinh ra đồng thời:

* **allowlist** ở tầng service (không có trong registry = không gửi được),
* UI trang Diagnostics và Camera,
* bộ test.

Nguyên tắc allowlist chứ không phải blocklist: lệnh chưa biết mặc định **bị chặn**.
Với thiết bị chỉ có một đường vào là Ethernet, không UART và không nút reset, đó là
hướng mặc định duy nhất chấp nhận được.

Mức :data:`RiskLevel.DANGEROUS` không bao giờ xuất hiện trong ``COMMANDS``.
:data:`FORBIDDEN` liệt kê chúng để phòng thủ theo chiều sâu và để tài liệu hoá lý do.
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
    """Một lệnh được phép gửi."""

    name: str
    """Định danh ổn định, dạng ``nhóm.hành_động``. Đây là khoá của REST API."""

    dest: Dest
    rw: str
    cmd3: str
    risk: RiskLevel
    confidence: Confidence

    doc: str = ""
    encode: Callable[..., str] | None = None
    """Tham số → chuỗi data. ``None`` nghĩa là lệnh không nhận tham số."""

    decode: Callable[[str], object] | None = None
    """Chuỗi data của phản hồi → giá trị Python."""

    data: str = ""
    """Data cố định, dùng khi ``encode`` là ``None``."""

    timeout: float = DEFAULT_TIMEOUT
    """Khai báo per-command: ``setVideoConfig`` cần 4000 ms, gấp nhiều lần lệnh khác."""

    header: str = HEADER
    """``EXT`` dùng header chữ thường ``#tp`` — đúng như trong bytecode."""

    expect_reply: bool = False
    """Lệnh đọc thì chờ phản hồi; phần lớn lệnh ghi thì không."""

    source: str = ""
    """Nơi rút ra hằng số này, để truy vết khi hai tài liệu mâu thuẫn."""

    def frame(self, *args, **kwargs) -> str:
        """Dựng khung hoàn chỉnh kèm checksum."""
        if self.encode is None:
            if args or kwargs:
                raise TypeError("%s không nhận tham số" % self.name)
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
# Bộ giải mã phản hồi
# --------------------------------------------------------------------------


def _decode_sdcard(data: str) -> SDCardStatus:
    """``SDC`` trả dung lượng tổng + còn lại. Format chính xác chưa xác minh
    trên phần cứng — pha 1 ghi lại chuỗi thô để chốt."""
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
# Bộ mã hoá tham số
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
    """Tham số ảnh nhiệt, thang 0–100."""
    return u8_hex(max(0, min(100, int(value))))


def _enc_rate_hz(value: int) -> str:
    return u8_hex(max(0, min(100, int(value))))


# --------------------------------------------------------------------------
# Lệnh đọc — 🟢 SAFE
# --------------------------------------------------------------------------

_READ_SPECS: list[tuple[str, str, str, Callable | None]] = [
    ("version", "VER", "Phiên bản firmware camera", None),
    ("hardware_version", "HWV", "Phiên bản phần cứng", None),
    ("model", "MOD", "Model camera", None),
    ("recording", "REC", "Đang ghi hình hay không", _decode_bool),
    ("palette", "IMG", "Palette nhiệt hiện tại", _decode_palette),
    ("resolution", "VID", "Độ phân giải ghi hình", _decode_resolution),
    ("zoom", "DZM", "Hệ số zoom số hiện tại", parse_u8),
    ("thermal_spatial_nr", "TAR", "Khử nhiễu không gian, 0–100", parse_u8),
    ("thermal_shutter", "TAS", "Chu kỳ shutter, 5–100", parse_u8),
    ("thermal_detail", "TDI", "Tăng cường chi tiết, 0–100", parse_u8),
    ("thermal_gamma", "TGM", "Gamma, 0–100", parse_u8),
    ("thermal_brightness", "TIB", "Độ sáng, 0–100", parse_u8),
    ("thermal_contrast", "TIC", "Tương phản, 0–100", parse_u8),
    ("thermal_scene", "TSM", "Scene mode — có thể C12 không hỗ trợ", parse_u8),
    ("thermal_temporal_nr", "TTR", "Khử nhiễu thời gian, 0–100", parse_u8),
    ("ranging", "SLR", "Laser đo xa — bytecode ghi chỉ C13/C14", None),
    ("ext_config", "EXT", "Cấu hình LED / OSD / hiệu chuẩn", None),
    ("video_config", "VOM", "Tham số luồng: flip, fps, GOP, bitrate", None),
    ("image_quality", "IQE", "Hiệu chỉnh hình ảnh", None),
    ("ip_address", "IPV", "Địa chỉ IP camera — CHỈ ĐỌC", None),
    ("gateway", "GTW", "Gateway camera — CHỈ ĐỌC", None),
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
    # SDC xuất hiện với cả data 00 lẫn 01 trong APK; bytecode dùng 01 cho dung lượng.
    out["read.sdcard"] = Command(
        name="read.sdcard",
        dest=Dest.CAMERA,
        rw="r",
        cmd3="SDC",
        data="01",
        risk=RiskLevel.SAFE,
        confidence=Confidence.BYTECODE,
        doc="Dung lượng thẻ nhớ; cả hai giá trị = 0 nghĩa là chưa cắm thẻ",
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
        doc="Biến thể SDC data=00 thấy trong APK — pha 1 xác định nó khác gì data=01",
        expect_reply=True,
        source="skydroid-c12-protocol.md §8.2",
    )
    return out


# --------------------------------------------------------------------------
# Lệnh ghi camera — 🟡 REVERSIBLE
# --------------------------------------------------------------------------


def _write_commands() -> dict[str, Command]:
    C, W = Dest.CAMERA, "w"
    B, R = Confidence.BYTECODE, RiskLevel.REVERSIBLE
    src = "PHAN_TICH_SDK_C12.md §4"

    cmds = [
        Command("camera.snap", C, W, "CAP", R, B, "Chụp một ảnh vào thẻ nhớ",
                data="01", source=src),
        Command("camera.record_start", C, W, "REC", R, B, "Bắt đầu ghi hình",
                data="01", source=src),
        Command("camera.record_stop", C, W, "REC", R, B, "Dừng ghi hình",
                data="00", source=src),
        Command("camera.zoom_in", C, W, "DZM", R, B,
                "Zoom số vào một nấc. Dùng cái này thay setZoomRatios: bytecode "
                "chặn cứng set tuyệt đối ở 0–4 trong khi dải thật là 0–67",
                data="0A", source=src),
        Command("camera.zoom_out", C, W, "DZM", R, B, "Zoom số ra một nấc",
                data="0B", source=src),
        Command("camera.palette", C, W, "IMG", R, B,
                "Đặt palette nhiệt. LÀ IMG, không phải TAR — TAR là khử nhiễu "
                "không gian và sweep nó sẽ phá cấu hình cảm biến",
                encode=_enc_palette, source=src + ".4"),
        Command("camera.resolution", C, W, "VID", R, B, "Đặt độ phân giải ghi hình",
                encode=_enc_resolution, source=src + ".3"),
    ]

    thermal = [
        ("thermal_spatial_nr", "TAR", "Khử nhiễu không gian, 0–100"),
        ("thermal_shutter", "TAS", "Chu kỳ shutter, 5–100"),
        ("thermal_detail", "TDI", "Tăng cường chi tiết, 0–100"),
        ("thermal_gamma", "TGM", "Gamma, 0–100"),
        ("thermal_brightness", "TIB", "Độ sáng, 0–100"),
        ("thermal_contrast", "TIC", "Tương phản, 0–100"),
        ("thermal_temporal_nr", "TTR", "Khử nhiễu thời gian, 0–100"),
    ]
    for suffix, cmd3, doc in thermal:
        cmds.append(
            Command("camera." + suffix, C, W, cmd3, R, B, doc,
                    encode=_enc_percent, source=src + ".4")
        )

    return {c.name: c for c in cmds}


# --------------------------------------------------------------------------
# Telemetry — 🟡 REVERSIBLE (bật luồng đẩy, không gây chuyển động)
# --------------------------------------------------------------------------


def _telemetry_commands() -> dict[str, Command]:
    src = "PHAN_TICH_SDK_C12.md §5.4"
    cmds = [
        Command(
            "telemetry.push_attitude", Dest.GIMBAL, "w", "GAA",
            RiskLevel.REVERSIBLE, Confidence.BYTECODE,
            "Bật camera tự đẩy gói GAC ở N Hz (0 = tắt). Chỉ hiệu lực SAU KHI "
            "camera đã ra hình — gửi lặp vài lần lúc khởi động. Chính vì mặc "
            "định tắt mà skydroid-c12-protocol.md kết luận nhầm là không có telemetry",
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
                "Tốc độ yaw, °/s. Âm = trái. Dải ±63.5, bước 0.5",
                encode=_enc_speed, source=src + ".2"),
        Command("gimbal.pitch_speed", G, W, "GSP", P, B,
                "Tốc độ pitch, °/s. Âm = xuống. Dải ±63.5, bước 0.5",
                encode=_enc_speed, source=src + ".2"),
        Command("gimbal.speed", G, W, "GSM", P, B,
                "Tốc độ yaw+pitch trong một gói — nửa lưu lượng ở 20 Hz. "
                "Cần firmware gimbal ≥ 0.5; thăm dò lúc khởi động rồi lùi về "
                "gimbal.yaw_speed + gimbal.pitch_speed nếu không hỗ trợ",
                encode=_enc_speed_pair, source=src + ".2"),
        Command("gimbal.goto_yaw", G, W, "GAY", P, B,
                "Yaw tuyệt đối, độ. Clamp ±90.00",
                encode=_enc_angle, source=src + ".3"),
        Command("gimbal.goto_pitch", G, W, "GAP", P, B,
                "Pitch tuyệt đối, độ. Clamp ±90.00",
                encode=_enc_angle, source=src + ".3"),
        Command("gimbal.goto", G, W, "GAM", P, B,
                "Yaw+pitch tuyệt đối một gói. KHÔNG dùng trước khi goto_yaw và "
                "goto_pitch đã xác minh xong đơn vị góc bằng GAC — sai đơn vị "
                "trên hai trục là hậu quả nhân đôi",
                encode=_enc_angle_pair, source=src + ".3"),
        Command("gimbal.akey", G, W, "PTZ", P, B,
                "Lệnh một phím: UP DOWN LEFT RIGHT CENTER. Chỉ 01–05 được phép — "
                "0C/0D khởi động hiệu chuẩn gimbal",
                encode=_enc_akey, source=src + ".1"),
    ]
    return {c.name: c for c in cmds}


# --------------------------------------------------------------------------
# Bảng tổng
# --------------------------------------------------------------------------

COMMANDS: dict[str, Command] = {
    **_read_commands(),
    **_write_commands(),
    **_telemetry_commands(),
    **_gimbal_commands(),
}


#: Lệnh dừng khẩn, dựng sẵn để không bao giờ phụ thuộc vào registry lookup lúc
#: đang cần dừng gấp.
STOP_FRAMES: tuple[str, ...] = (
    COMMANDS["gimbal.yaw_speed"].frame(0),
    COMMANDS["gimbal.pitch_speed"].frame(0),
)


#: 🔴 DANGEROUS — không có mục nào trong :data:`COMMANDS`.
#: Giữ danh sách để phòng thủ theo chiều sâu và để ghi lại lý do.
FORBIDDEN: dict[str, str] = {
    "IPV": "Đổi IP camera. Sai là mất thiết bị vĩnh viễn: không UART, không nút "
           "reset, chỉ còn cách quét lại cả dải mạng và cầu may. Đọc thì an toàn.",
    "GTW": "Đổi gateway. Cùng hậu quả như IPV. Đọc thì an toàn.",
    "VOM": "Đổi cấu hình luồng video. Có thể làm hỏng RTSP — mất luôn cả video "
           "lẫn khả năng chẩn đoán. Đọc thì an toàn.",
    "IQE": "Đổi cấu hình encode. Cùng rủi ro như VOM. Đọc thì an toàn.",
    "RST": "Reboot camera. Muốn reboot thì ngắt nguồn.",
    "RTF": "Khôi phục mặc định nhà máy.",
    "GAR": "Góc roll. Bytecode ghi 'không khuyến nghị'.",
    "TIM": "Đặt thời gian, format chưa xác minh.",
    "FCC": "Motor lấy nét — của model có ống kính cơ, không phải C12.",
    "ZMC": "Motor zoom quang — của model có ống kính cơ. C12 dùng DZM.",
}

#: Data của ``PTZ`` không bao giờ được phép gửi, kể cả nếu ai đó thêm nhầm vào enum.
FORBIDDEN_PTZ_DATA: frozenset[str] = frozenset(
    {"00", "06", "07", "08", "09", "0A", "0B", "0C", "0D",
     "0E", "0F", "10", "11", "12", "13", "14"}
)


class CommandNotAllowed(PermissionError):
    """Lệnh không có trong allowlist, hoặc bị chặn vì mức rủi ro."""


def get(name: str) -> Command:
    """Tra một lệnh. Ném :class:`CommandNotAllowed` nếu không có trong registry."""
    try:
        return COMMANDS[name]
    except KeyError:
        raise CommandNotAllowed(
            "%r không có trong registry. Allowlist chứ không phải blocklist: "
            "lệnh chưa khai báo thì không gửi được." % name
        ) from None


def by_risk(level: RiskLevel) -> list[Command]:
    return sorted(
        (c for c in COMMANDS.values() if c.risk == level), key=lambda c: c.name
    )


def read_commands() -> list[Command]:
    """Toàn bộ lệnh đọc, dùng cho trang Diagnostics."""
    return by_risk(RiskLevel.SAFE)


def assert_registry_sane() -> None:
    """Bất biến của registry. Gọi lúc khởi động — thà chết sớm còn hơn gửi nhầm."""
    for name, cmd in COMMANDS.items():
        if cmd.name != name:
            raise AssertionError("khoá %r lệch với Command.name %r" % (name, cmd.name))
        if cmd.risk is RiskLevel.DANGEROUS:
            raise AssertionError("%s ở mức DANGEROUS mà vẫn nằm trong registry" % name)
        if cmd.rw == "w" and cmd.cmd3 in FORBIDDEN:
            raise AssertionError(
                "%s ghi vào %s, nằm trong danh sách cấm: %s"
                % (name, cmd.cmd3, FORBIDDEN[cmd.cmd3])
            )
        if cmd.encode is None and cmd.rw == "w" and not cmd.data:
            raise AssertionError("%s là lệnh ghi nhưng không có data lẫn encode" % name)
    for key in AKey:
        if key.value in FORBIDDEN_PTZ_DATA:
            raise AssertionError("AKey.%s = %s nằm trong dải PTZ bị cấm" % (key.name, key.value))
