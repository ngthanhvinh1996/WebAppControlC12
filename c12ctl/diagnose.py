"""Quy trình pha 1 trong một lệnh: preflight → quét lệnh đọc → xuất kết quả.

    python -m c12ctl.diagnose                          # camera thật
    python -m c12ctl.diagnose --host 127.0.0.1 --port 15000 --local-port 0
    python -m c12ctl.diagnose --skip-preflight -o findings.jsonl -m CAPABILITIES.md

Chỉ gửi lệnh ``2r``. Read-only, an toàn tuyệt đối — không đổi bất kỳ trạng thái
nào của camera.

Preflight chạy trước và **chặn** nếu tầng link hỏng: quét lệnh trong khi cáp chưa
cắm chỉ tạo ra 22 dòng timeout vô nghĩa và làm người đọc tưởng camera hỏng.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .protocol import registry as reg
from .services import findings, preflight
from .transport.udp_link import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    PortBusyError,
    UdpLink,
)

log = logging.getLogger("c12ctl.diagnose")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m c12ctl.diagnose",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--local-port", type=int, default=DEFAULT_PORT,
                    help="cổng UDP local; dùng 0 nếu 5000 đang bị chiếm")
    ap.add_argument("--timeout", type=float, default=0.4,
                    help="chờ mỗi lệnh, giây (mặc định 0.4)")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="quét N lần; lệnh chỉ cần trả lời 1 lần là coi như sống. "
                         "Dùng khi nghi ngờ mất gói")
    ap.add_argument("-o", "--out", default="logs/findings.jsonl",
                    help="file JSONL, nối thêm chứ không đè")
    ap.add_argument("-m", "--markdown", default=None, metavar="PATH",
                    help="xuất thêm bảng markdown")
    ap.add_argument("--packet-log", default=None, metavar="PATH",
                    help="ghi mọi gói TX/RX ra JSONL")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="bỏ qua kiểm tra mạng (dùng khi trỏ vào simulator)")
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


async def _run(args) -> int:
    reg.assert_registry_sane()

    if not args.skip_preflight:
        print("── Preflight " + "─" * 52)
        pre = await preflight.run(args.host, control_port=args.local_port or 0)
        print(preflight.render(pre))
        print()
        if pre.blocking:
            print("Dừng: %d kiểm tra hỏng ở tầng dưới giao thức."
                  % len(pre.blocking))
            print("Sửa theo gợi ý bên trên rồi chạy lại. Quét lệnh lúc này chỉ "
                  "sinh ra một loạt timeout vô nghĩa.")
            print("\nMuốn quét bất chấp: thêm --skip-preflight")
            return 2
        if args.preflight_only:
            return 0
    elif args.preflight_only:
        print("--preflight-only và --skip-preflight loại trừ nhau.", file=sys.stderr)
        return 1

    try:
        link = UdpLink(args.host, args.port, args.local_port,
                       log_path=args.packet_log)
        await link.start()
    except PortBusyError as exc:
        print("\n%s" % exc, file=sys.stderr)
        return 2

    try:
        print("── Quét lệnh đọc " + "─" * 48)
        print("%d lệnh 🟢 SAFE, timeout %.1fs. Read-only — không đổi trạng thái nào.\n"
              % (len(reg.read_commands()), args.timeout))

        report = await findings.sweep(link, timeout=args.timeout)

        for extra in range(1, max(1, args.repeat)):
            log.info("lượt quét lại %d/%d cho các lệnh im lặng",
                     extra + 1, args.repeat)
            silent = [reg.COMMANDS[p.name] for p in report.silent]
            if not silent:
                break
            again = await findings.sweep(link, timeout=args.timeout,
                                         commands=silent)
            revived = {p.name: p for p in again.probes if p.alive}
            if revived:
                log.warning("%d lệnh sống ở lượt sau — nghi mất gói: %s",
                            len(revived), ", ".join(revived))
                report.probes = [revived.get(p.name, p) for p in report.probes]

        print(findings.render_text(report))
    finally:
        await link.close()

    out = findings.append_jsonl(report, args.out)
    print("\nĐã ghi %d bản ghi vào %s" % (len(report.probes), out))

    if args.markdown:
        path = Path(args.markdown)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(findings.render_markdown(report), encoding="utf-8")
        print("Bảng markdown: %s" % path)

    if report.surprises:
        print("\n⚠️  %d lệnh khác kỳ vọng — xem phần đầu bảng markdown:"
              % len(report.surprises))
        for p in report.surprises:
            print("   %s (%s): %s" % (p.name, p.cmd3, p.note))

    # Điều kiện qua pha 1: đọc được VER, MOD, SDC.
    required = {"read.version", "read.model", "read.sdcard"}
    got = {p.name for p in report.alive}
    missing = sorted(required - got)
    print()
    if missing:
        print("Chưa đạt điều kiện qua pha 1 — còn thiếu: %s" % ", ".join(missing))
        return 1
    print("Đạt điều kiện qua pha 1: đọc được VER, MOD, SDC.")
    return 0


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    try:
        sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
