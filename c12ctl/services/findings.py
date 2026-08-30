"""Bản đồ năng lực: lệnh nào C12 thực sự trả lời.

Toàn bộ lệnh ``2r`` là read-only nên quét cả nhóm là **an toàn tuyệt đối**. Lệnh
camera không hỗ trợ sẽ **im lặng** — và chính sự im lặng đó là dữ liệu: nó phân
xử hộ ta những chỗ hai tài liệu nguồn mâu thuẫn, mà không cần gửi lệnh ghi nào.

Kết quả ghi ra JSONL (nối thêm, không đè) để so được giữa các lần chạy, kèm một
bảng markdown sinh lại được để dán thẳng vào tài liệu.

:data:`PREDICTIONS` là phần đáng giá nhất: mỗi lệnh đi kèm điều nó sẽ *chứng minh*
nếu trả lời, và điều nó chứng minh nếu im lặng. Nhờ vậy báo cáo không chỉ nói
"lệnh này im" mà nói "im lặng ở đây xác nhận SLR chỉ có trên C13/C14".
"""

from __future__ import annotations

import json
import platform
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..protocol.registry import Command, read_commands
from ..transport.udp_link import UdpLink


@dataclass(frozen=True)
class Prediction:
    """Điều một kết quả sẽ chứng minh."""

    if_alive: str = ""
    if_silent: str = ""
    expected: bool | None = None
    """Kỳ vọng theo tài liệu. ``None`` = không có kỳ vọng, chỉ đi thu thập."""


#: Ánh xạ lệnh → ý nghĩa của kết quả. Chỉ liệt kê những lệnh mà kết quả thực sự
#: nói lên điều gì đó; phần còn lại chỉ thu thập format phản hồi.
PREDICTIONS: dict[str, Prediction] = {
    "read.palette": Prediction(
        if_alive="XÁC NHẬN palette nằm ở IMG. skydroid-c12-protocol.md đoán TAR — "
                 "sweep TAR sẽ phá cấu hình khử nhiễu chứ không đổi màu.",
        if_silent="IMG không phản hồi. Kiểm tra lại trước khi ghi; có thể phải "
                  "tô màu phía client bằng cv2.applyColorMap.",
        expected=True,
    ),
    "read.thermal_spatial_nr": Prediction(
        if_alive="TAR sống và là tham số khử nhiễu 0–100, KHÔNG phải palette. "
                 "Ghi lại giá trị gốc trước khi động vào.",
        if_silent="TAR không phản hồi trên C12.",
        expected=True,
    ),
    "read.zoom": Prediction(
        if_alive="XÁC NHẬN zoom nằm ở DZM (đích D). protocol.md đoán ZMC (đích M) "
                 "— đó là lệnh của model có ống kính cơ.",
        if_silent="DZM không phản hồi — thử lại trước khi kết luận.",
        expected=True,
    ),
    "read.ranging": Prediction(
        if_alive="BẤT NGỜ: C12 có laser đo xa. Bytecode ghi SLR chỉ có ở C13/C14.",
        if_silent="Xác nhận SLR chỉ có trên C13/C14, đúng như bytecode ghi.",
        expected=False,
    ),
    "read.thermal_scene": Prediction(
        if_alive="BẤT NGỜ: C12 có scene mode. protocol.md ngờ đây là tính năng C13.",
        if_silent="Xác nhận nghi ngờ của protocol.md: TSM là của C13, không phải C12.",
        expected=False,
    ),
    "read.version": Prediction(
        if_alive="Link lệnh hoạt động. Ghi lại chuỗi phiên bản.",
        if_silent="Link hỏng ở tầng dưới — chạy preflight trước khi đi tiếp.",
        expected=True,
    ),
    "read.model": Prediction(
        if_alive="Xác nhận đang nói chuyện với đúng model.",
        if_silent="Không đọc được model.",
        expected=True,
    ),
    "read.sdcard": Prediction(
        if_alive="Ghi lại FORMAT THÔ. Trường length chỉ 1 ký tự hex nên data tối "
                 "đa 15 ký tự — điều đó loại mọi giả thuyết 2×32-bit.",
        if_silent="SDC data=01 không phản hồi; thử read.sdcard_alt (data=00).",
        expected=True,
    ),
    "read.sdcard_alt": Prediction(
        if_alive="SDC data=00 là một sub-query khác. So format với read.sdcard.",
        if_silent="Chỉ data=01 được hỗ trợ.",
    ),
    "read.ext_config": Prediction(
        if_alive="EXT sống. Lưu ý lệnh GHI dùng header chữ thường #tp.",
        if_silent="EXT không có trên C12.",
    ),
    "read.ip_address": Prediction(
        if_alive="Ghi lại IP hiện tại. CHỈ ĐỌC — ghi vào IPV là mất camera.",
        if_silent="IPV không phản hồi.",
    ),
    "read.gateway": Prediction(
        if_alive="Ghi lại gateway hiện tại. CHỈ ĐỌC.",
        if_silent="GTW không phản hồi.",
    ),
    "read.video_config": Prediction(
        if_alive="Ghi lại format VOM. Cần cho quy trình get→sửa 1 field→set sau này.",
        if_silent="VOM không phản hồi.",
    ),
    "read.resolution": Prediction(
        if_alive="VID sống. Đối chiếu giá trị với độ phân giải thật của luồng RTSP.",
        if_silent="VID không phản hồi.",
        expected=True,
    ),
}


@dataclass
class Probe:
    """Một lần dò một lệnh."""

    name: str
    cmd3: str
    frame: str
    alive: bool
    raw: str | None
    value: object
    latency_ms: float | None
    doc: str = ""
    note: str = ""
    surprise: bool = False


@dataclass
class Report:
    started_at: float
    host: str
    probes: list[Probe] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def alive(self) -> list[Probe]:
        return [p for p in self.probes if p.alive]

    @property
    def silent(self) -> list[Probe]:
        return [p for p in self.probes if not p.alive]

    @property
    def surprises(self) -> list[Probe]:
        return [p for p in self.probes if p.surprise]

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "host": self.host,
            "meta": self.meta,
            "total": len(self.probes),
            "alive": len(self.alive),
            "silent": len(self.silent),
            "surprises": len(self.surprises),
            "probes": [asdict(p) for p in self.probes],
        }


async def sweep(link: UdpLink, *, timeout: float = 0.4,
                commands: list[Command] | None = None) -> Report:
    """Quét toàn bộ lệnh 🟢 SAFE. Không đổi bất kỳ trạng thái nào."""
    report = Report(
        started_at=time.time(),
        host="%s:%d" % link.addr,
        meta={"dry_run": link.dry_run, "platform": platform.platform(),
              "timeout": timeout},
    )
    for cmd in commands if commands is not None else read_commands():
        started = time.monotonic()
        raw_holder: list[str] = []

        def capture(frame, _c=cmd, _h=raw_holder):
            if frame.cmd3 == _c.cmd3:
                _h.append(frame.data)

        link.subscribe(capture)
        try:
            value = await link.request(cmd, timeout=timeout)
        finally:
            link.unsubscribe(capture)

        elapsed = (time.monotonic() - started) * 1000
        alive = value is not None
        pred = PREDICTIONS.get(cmd.name)
        note = ""
        surprise = False
        if pred is not None:
            note = pred.if_alive if alive else pred.if_silent
            surprise = pred.expected is not None and pred.expected != alive

        report.probes.append(
            Probe(
                name=cmd.name,
                cmd3=cmd.cmd3,
                frame=cmd.frame(),
                alive=alive,
                raw=raw_holder[-1] if raw_holder else None,
                value=_jsonable(value),
                latency_ms=round(elapsed, 1) if alive else None,
                doc=cmd.doc,
                note=note,
                surprise=surprise,
            )
        )
    return report


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return str(value)


def append_jsonl(report: Report, path: str | Path) -> Path:
    """Nối một dòng cho mỗi lần dò. Không đè — so được giữa các lần chạy."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    run_id = "%s-%d" % (time.strftime("%Y%m%dT%H%M%S"), int(report.started_at % 1 * 1000))
    with path.open("a", encoding="utf-8") as fh:
        for probe in report.probes:
            record = {"run": run_id, "t": report.started_at,
                      "host": report.host, **asdict(probe)}
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def render_markdown(report: Report) -> str:
    """Bảng markdown dán thẳng được vào tài liệu."""
    lines = [
        "# Bản đồ năng lực C12",
        "",
        "Sinh tự động bởi `c12ctl.services.findings`. Chỉ dùng lệnh `2r` — read-only.",
        "",
        "- Thiết bị: `%s`" % report.host,
        "- Thời điểm: %s" % time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(report.started_at)),
        "- Kết quả: **%d/%d lệnh trả lời**, %d im lặng"
        % (len(report.alive), len(report.probes), len(report.silent)),
        "",
    ]

    if report.surprises:
        lines += ["## ⚠️ Khác với kỳ vọng", ""]
        for p in report.surprises:
            lines.append("- **%s** (`%s`) — %s. %s"
                         % (p.name, p.cmd3,
                            "trả lời dù không kỳ vọng" if p.alive
                            else "im lặng dù kỳ vọng có",
                            p.note))
        lines.append("")

    lines += [
        "## Lệnh trả lời", "",
        "| Lệnh | Gói | Data thô | Giải mã | ms |",
        "|---|---|---|---|---|",
    ]
    for p in report.alive:
        lines.append("| `%s` | `%s` | `%s` | %s | %s |"
                     % (p.name, p.frame, p.raw if p.raw is not None else "—",
                        _fmt(p.value), p.latency_ms))

    lines += ["", "## Lệnh im lặng", "",
              "Không phản hồi = C12 không hỗ trợ. Đây là dữ liệu, không phải lỗi.",
              "", "| Lệnh | Gói | Nghĩa là |", "|---|---|---|"]
    for p in report.silent:
        lines.append("| `%s` | `%s` | %s |" % (p.name, p.frame, p.note or "—"))

    notes = [p for p in report.alive if p.note]
    if notes:
        lines += ["", "## Ghi chú theo lệnh", ""]
        for p in notes:
            lines.append("- **%s** — %s" % (p.name, p.note))

    return "\n".join(lines) + "\n"


def render_text(report: Report) -> str:
    """Bản in cho terminal."""
    width = max((len(p.name) for p in report.probes), default=20)
    lines = []
    for p in report.probes:
        mark = "!" if p.surprise else " "
        if p.alive:
            lines.append("%s ok     %-*s %-18s %s"
                         % (mark, width, p.name, p.frame, _fmt(p.value)))
        else:
            lines.append("%s silent %-*s %-18s —" % (mark, width, p.name, p.frame))
    lines.append("")
    lines.append("%d/%d trả lời, %d im lặng, %d khác kỳ vọng"
                 % (len(report.alive), len(report.probes),
                    len(report.silent), len(report.surprises)))
    return "\n".join(lines)


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, dict):
        return ", ".join("%s=%s" % kv for kv in value.items())
    return str(value)
