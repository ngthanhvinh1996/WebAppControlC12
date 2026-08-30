"""Phase 1 in a single command: preflight → sweep the read commands → export.

    python -m c12ctl.diagnose                          # real camera
    python -m c12ctl.diagnose --host 127.0.0.1 --port 15000 --local-port 0
    python -m c12ctl.diagnose --skip-preflight -o findings.jsonl -m CAPABILITIES.md

Sends only ``2r`` commands. Read-only and completely safe — it changes no camera
state at all.

Preflight runs first and **blocks** if the link layer is broken: sweeping
commands with the cable unplugged produces 22 meaningless timeouts and leaves the
reader thinking the camera is dead.
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
                    help="local UDP port; use 0 if 5000 is already taken")
    ap.add_argument("--timeout", type=float, default=0.4,
                    help="wait per command, in seconds (default 0.4)")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="sweep N times; a command answering once counts as "
                         "alive. Use this when packet loss is suspected")
    ap.add_argument("-o", "--out", default="logs/findings.jsonl",
                    help="JSONL file, appended rather than overwritten")
    ap.add_argument("-m", "--markdown", default=None, metavar="PATH",
                    help="also write a markdown table")
    ap.add_argument("--packet-log", default=None, metavar="PATH",
                    help="write every TX/RX packet to JSONL")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="skip the network checks (for pointing at the simulator)")
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
            print("Stopping: %d check(s) failed below the protocol layer."
                  % len(pre.blocking))
            print("Fix them using the hints above, then run again. Sweeping now "
                  "would only produce a wall of meaningless timeouts.")
            print("\nTo sweep anyway: add --skip-preflight")
            return 2
        if args.preflight_only:
            return 0
    elif args.preflight_only:
        print("--preflight-only and --skip-preflight are mutually exclusive.",
              file=sys.stderr)
        return 1

    try:
        link = UdpLink(args.host, args.port, args.local_port,
                       log_path=args.packet_log)
        await link.start()
    except PortBusyError as exc:
        print("\n%s" % exc, file=sys.stderr)
        return 2

    try:
        print("── Read command sweep " + "─" * 43)
        print("%d 🟢 SAFE commands, timeout %.1fs. Read-only — no state is changed.\n"
              % (len(reg.read_commands()), args.timeout))

        report = await findings.sweep(link, timeout=args.timeout)

        for extra in range(1, max(1, args.repeat)):
            log.info("re-sweep pass %d/%d for the silent commands",
                     extra + 1, args.repeat)
            silent = [reg.COMMANDS[p.name] for p in report.silent]
            if not silent:
                break
            again = await findings.sweep(link, timeout=args.timeout,
                                         commands=silent)
            revived = {p.name: p for p in again.probes if p.alive}
            if revived:
                log.warning("%d command(s) answered on a later pass — packet loss "
                            "suspected: %s", len(revived), ", ".join(revived))
                report.probes = [revived.get(p.name, p) for p in report.probes]

        print(findings.render_text(report))
    finally:
        await link.close()

    out = findings.append_jsonl(report, args.out)
    print("\nWrote %d records to %s" % (len(report.probes), out))

    if args.markdown:
        path = Path(args.markdown)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(findings.render_markdown(report), encoding="utf-8")
        print("Markdown table: %s" % path)

    if report.surprises:
        print("\n⚠️  %d command(s) differ from expectation — see the top of the "
              "markdown table:" % len(report.surprises))
        for p in report.surprises:
            print("   %s (%s): %s" % (p.name, p.cmd3, p.note))

    # Phase 1 exit criterion: VER, MOD and SDC can be read.
    required = {"read.version", "read.model", "read.sdcard"}
    got = {p.name for p in report.alive}
    missing = sorted(required - got)
    print()
    if missing:
        print("Phase 1 exit criterion not met — still missing: %s"
              % ", ".join(missing))
        return 1
    print("Phase 1 exit criterion met: VER, MOD and SDC all read back.")
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
