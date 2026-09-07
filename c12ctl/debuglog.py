"""One log file that answers the questions a reader who was **not there** will ask.

The camera is on a bench somewhere else. When something goes wrong — the gimbal
does not move, the image freezes, a command is refused — the only thing that can
travel back is a file. So this module writes the file that makes a remote
diagnosis possible without a second run:

* **What was running** — version, git commit, host, SoC, Python, OpenCV backends,
  the full command line and every resolved option. Half of all "it does not work"
  reports are answered by this block alone (wrong ``--video``, wrong decoder, an
  old checkout).
* **What the link did** — every packet except the 20 Hz speed spam, which is
  counted instead of printed. The first three of *every* command word are always
  printed, spam included, so its shape on the wire is on record.
* **What the machine did** — a periodic snapshot of link, gimbal, telemetry,
  video, camera, CPU, RSS and SoC temperature. A single reading proves nothing;
  a trend explains a stall, a thermal throttle, a stream that quietly died.
* **What the operator did** — the browser posts its own events here (arm, mode
  switch, every direction pressed, JS errors, WebSocket drops). Without this half
  the log shows a gimbal that stopped and no reason why, because the reason
  happened in a tab.

Nothing here changes behaviour: no command is sent, no state is touched. It can
be turned off entirely with ``--no-debug-log``.

Reading one back::

    grep 'c12ctl.ui'     debug.log    # what the operator did
    grep 'snap\\[.*video' debug.log    # the video pipeline over time
    grep -E 'MARK|WARN|ERROR' debug.log
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from . import __version__

log = logging.getLogger("c12ctl.debug")
uilog = logging.getLogger("c12ctl.ui")

DEFAULT_PATH = "logs/debug/c12ctl.log"
DEFAULT_MAX_MB = 8.0
DEFAULT_BACKUPS = 3
DEFAULT_SNAPSHOT = 10.0

FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)-16s %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

HIGH_RATE = frozenset({"GSY", "GSP", "GSM", "GAC"})
"""Command words that repeat 10–20 times a second. Counted, not printed."""

FIRST_N = 3
"""Print this many of every command word before the suppression rules apply —
the shape of a packet is worth more than its 400th repetition."""

REPEAT_EVERY = 60
"""An unchanged packet still prints after this many identical repeats, so a
link that is quiet because nothing changed cannot be mistaken for a dead one."""

QUIET_PATHS = ("/api/video", "/api/camera", "/api/session", "/api/gimbal",
               "/api/debug", "/video/")
"""The UI polls these once or twice a second. A successful poll is noise; a
failing one is not, so only 2xx/3xx on these paths are dropped."""


# --------------------------------------------------------------------------
# What was running
# --------------------------------------------------------------------------


def _cmd(*argv: str, timeout: float = 1.5) -> str:
    """Best effort: a missing tool or a non-repo directory is not an error here."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _read(path: str, limit: int = 400) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit).strip().strip("\x00")
    except OSError:
        return ""


def _os_release() -> str:
    for line in _read("/etc/os-release", 2000).splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return ""


def _board() -> str:
    """The device tree model — this is what says 'Rubik Pi 3' instead of guessing.

    Which matters: the hardware decoder, the thermal budget and half the video
    advice differ between the dev laptop and the board.
    """
    for path in ("/proc/device-tree/model",
                 "/sys/firmware/devicetree/base/model",
                 "/sys/devices/virtual/dmi/id/product_name"):
        value = _read(path, 200)
        if value:
            return value
    return ""


def _mem_total_mb() -> float:
    for line in _read("/proc/meminfo", 400).splitlines():
        if line.startswith("MemTotal:"):
            with contextlib.suppress(ValueError, IndexError):
                return round(int(line.split()[1]) / 1024, 0)
    return 0.0


def _local_ip(host: str) -> str:
    """Which of our addresses would reach the camera — no packet is sent.

    A connected UDP socket only picks a route, and the answer is the one fact
    that separates "the camera is off" from "this machine is on the wrong
    subnet", which is the single most common failure on a fresh board.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 9))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def _opencv() -> str:
    try:
        import cv2
    except Exception:  # noqa: BLE001 - a missing cv2 is itself the finding
        return "not importable"
    parts = ["cv2 " + cv2.__version__]
    with contextlib.suppress(Exception):
        info = cv2.getBuildInformation()
        for key in ("FFMPEG", "GStreamer"):
            for line in info.splitlines():
                if line.strip().startswith(key + ":"):
                    parts.append(line.strip().replace(" " * 4, " "))
                    break
    return " · ".join(parts)


def _version(module: str) -> str:
    try:
        return __import__(module).__version__
    except Exception:  # noqa: BLE001
        return "?"


def environment(host: str = "") -> dict[str, str]:
    """Everything about this machine that changes how the app behaves."""
    now = datetime.now().astimezone()
    git = _cmd("git", "rev-parse", "--short", "HEAD")
    branch = _cmd("git", "rev-parse", "--abbrev-ref", "HEAD")
    dirty = "dirty" if _cmd("git", "status", "--porcelain") else "clean"
    uname = platform.uname()
    return {
        "when": "%s (UTC%s)" % (now.strftime("%Y-%m-%d %H:%M:%S %Z"),
                                now.strftime("%z")),
        "app": "c12ctl %s · git %s on %s (%s)" % (
            __version__, git or "unknown", branch or "?", dirty),
        "cwd": os.getcwd(),
        "host": "%s · %s %s %s" % (uname.node, uname.system, uname.release,
                                   uname.machine),
        "board": "%s · %s · %d cpu · %.0f MB RAM" % (
            _board() or "unknown board", _os_release() or "unknown os",
            os.cpu_count() or 0, _mem_total_mb()),
        "python": "%s · %s" % (sys.version.split()[0], sys.executable),
        "packages": "fastapi %s · uvicorn %s · numpy %s · %s" % (
            _version("fastapi"), _version("uvicorn"), _version("numpy"),
            _opencv()),
        "net": "local %s → camera %s" % (_local_ip(host or "8.8.8.8") or "?",
                                         host or "?"),
        "argv": " ".join([Path(sys.executable).name, "-m", "c12ctl.web.app"]
                         + sys.argv[1:]),
    }


# --------------------------------------------------------------------------
# What the machine did
# --------------------------------------------------------------------------


class _Proc:
    """CPU, memory and SoC temperature, sampled between snapshots.

    On the board this is not a nicety: software H.265 decode saturates the CPU
    and the QCS6490 throttles, and both look exactly like "the video is laggy"
    from the browser.
    """

    def __init__(self) -> None:
        self._cpu_t = time.monotonic()
        self._cpu_ticks = self._ticks()
        self._hz = float(os.sysconf("SC_CLK_TCK")) if hasattr(os, "sysconf") else 100.0

    @staticmethod
    def _ticks() -> float:
        fields = _read("/proc/self/stat", 1000).rsplit(")", 1)[-1].split()
        with contextlib.suppress(ValueError, IndexError):
            return float(fields[11]) + float(fields[12])   # utime + stime
        return 0.0

    @staticmethod
    def _rss_mb() -> float:
        for line in _read("/proc/self/status", 3000).splitlines():
            if line.startswith("VmRSS:"):
                with contextlib.suppress(ValueError, IndexError):
                    return round(int(line.split()[1]) / 1024, 1)
        return 0.0

    @staticmethod
    def _temp_c() -> float:
        best = 0.0
        for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            raw = _read(str(zone / "temp"), 32)
            with contextlib.suppress(ValueError):
                best = max(best, int(raw) / 1000.0)
        return round(best, 1)

    MIN_SPAN = 0.5
    """Two snapshots close together divide a tick count by nearly zero and
    report 1000 % CPU. Say nothing instead, and keep the old baseline so the
    next one still measures a real interval."""

    def line(self) -> str:
        now, ticks = time.monotonic(), self._ticks()
        span = now - self._cpu_t
        if span >= self.MIN_SPAN:
            cpu = "%.0f%%" % ((ticks - self._cpu_ticks) / self._hz / span * 100.0)
            self._cpu_t, self._cpu_ticks = now, ticks
        else:
            cpu = "n/a"
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
        temp = self._temp_c()
        return "rss=%.1fMB cpu=%s load=%.2f threads=%d%s" % (
            self._rss_mb(), cpu, load, threading.active_count(),
            " temp=%.1fC" % temp if temp else "")


def _fmt_link(d: dict, addr: tuple) -> str:
    age = d.get("last_rx_at")
    return "%s:%s tx=%d rx=%d bad=%d timeouts=%d last_rx=%s" % (
        addr[0], addr[1], d["tx"], d["rx"], d["rx_bad"], d["timeouts"],
        "%.1fs ago" % (time.monotonic() - age) if age else "never")


def _fmt_gimbal(d: dict) -> str:
    s, st = d["state"], d["stats"]
    return ("armed=%s cmd=(%.1f,%.1f) max=%.1f ticks=%d pkts=%d stops=%d "
            "watchdog=%d limit=%d refused=%d gsm=%s running=%s last_stop=%r" % (
                d["armed"], s["yaw"], s["pitch"], d["max_speed"], st["ticks"],
                st["packets"], st["stops"], st["watchdog_trips"],
                st["limit_trips"], st["rejected"], d["use_gsm"], d["running"],
                st["last_stop_reason"]))


def _fmt_telemetry(d: dict) -> str:
    att = d.get("attitude")
    return "on=%s fresh=%s packets=%d age=%sms att=%s" % (
        d["enabled"], d["fresh"], d["packets"], d["age_ms"],
        "(%.1f,%.1f,%.1f)" % (att["yaw"], att["pitch"], att["roll"])
        if att else "none")


def _fmt_stream(name: str, d: dict) -> str:
    src, out, bus = d["source_stats"], d["stream_stats"], d["bus"]
    return ("%s in=%s fps frames=%d out=%s fps lat=%sms enc=%sms jpeg=%sKB "
            "clients=%d errors=%d reconnects=%d up=%ss%s" % (
                name, bus.get("fps"), src["frames"], out["out_fps"],
                out["latency_ms"], out["encode_ms"], out["jpeg_kb"],
                out["clients"], src["errors"], src["reconnects"],
                src["uptime_s"],
                " last_error=%r" % src["last_error"] if src["last_error"] else ""))


def _fmt_camera(d: dict) -> str:
    st = d["stats"]
    dead = [n for n, f in d["fields"].items() if f.get("supported") is False]
    return "running=%s reads=%d silent=%d skipped=%d applies=%d ok=%d bad=%d unverified=%d%s" % (
        d["running"], st["reads"], st["silent"], st["skipped"], st["applies"],
        st["verified"], st["mismatched"], st["unverified"],
        " unsupported=%s" % ",".join(sorted(dead)) if dead else "")


# --------------------------------------------------------------------------


class _AccessFilter(logging.Filter):
    """Drop successful polls from uvicorn's access log, keep everything else.

    The UI polls four endpoints every second: an hour of that is 14 000 lines
    that say nothing the snapshot does not say better. A *failing* poll is the
    opposite — that is the line worth having.
    """

    def __init__(self) -> None:
        super().__init__()
        self.dropped = 0

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path, status = str(args[2]), args[4]
        with contextlib.suppress(TypeError, ValueError):
            if int(status) < 400 and path.startswith(QUIET_PATHS):
                self.dropped += 1
                return False
        return True


class DebugLog:
    """The debug file, its rotation, and everything that writes into it."""

    def __init__(
        self,
        path: str | os.PathLike = DEFAULT_PATH,
        *,
        max_mb: float = DEFAULT_MAX_MB,
        backups: int = DEFAULT_BACKUPS,
        snapshot_s: float = DEFAULT_SNAPSHOT,
        packets: bool = False,
        level: int = logging.DEBUG,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.backups = backups
        self.snapshot_s = snapshot_s
        self.packets = packets
        self.level = level

        self.handler: logging.Handler | None = None
        self.started_at = time.time()
        self._sources: dict[str, Callable[[], str]] = {}
        self._task: asyncio.Task | None = None
        self._proc = _Proc()
        self._seen: Counter = Counter()
        self._quiet: Counter = Counter()
        self._repeat: Counter = Counter()
        self._last_raw: dict[tuple, str] = {}
        self._access = _AccessFilter()
        self._seq = 0
        self._link = None

    # ---------------------------------------------------------------- wiring

    def install(self) -> "DebugLog":
        """Attach the file to the root logger without making the console noisier."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            self.path, maxBytes=self.max_bytes, backupCount=self.backups,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
        handler.setLevel(self.level)

        root = logging.getLogger()
        # The root gate has to open to DEBUG or the file would never see a DEBUG
        # record — but a handler left at NOTSET inherits that new level, so the
        # console would start printing DEBUG too. Pin the existing handlers to
        # the level the console was configured with *before* lowering the root.
        console = root.level or logging.INFO
        for existing in root.handlers:
            if existing.level == logging.NOTSET:
                existing.setLevel(console)
        root.setLevel(min(self.level, console))
        root.addHandler(handler)
        self.handler = handler
        return self

    def attach_logger(self, name: str) -> None:
        """Also file a logger that does not propagate to the root.

        uvicorn installs its own handlers with ``propagate=False``, so without
        this the debug log would be missing the HTTP side entirely — including
        the tracebacks of a request that failed.
        """
        if self.handler is None:
            return
        lg = logging.getLogger(name)
        lg.addHandler(self.handler)
        if lg.level == logging.NOTSET or lg.level > self.level:
            lg.setLevel(self.level)
        if name.endswith(".access"):
            lg.addFilter(self._access)

    def attach_link(self, link) -> None:
        """Trace packets, minus the 20 Hz spam."""
        self._link = link
        link.add_journal_sink(self._on_packet)

    def watch(self, *, link=None, gimbal=None, telemetry=None, video=None,
              camera=None) -> None:
        """Register the subsystems the periodic snapshot reports on."""
        if link is not None:
            self.add_source("link", lambda: _fmt_link(link.stats.as_dict(), link.addr))
        if gimbal is not None:
            self.add_source("gimbal", lambda: _fmt_gimbal(gimbal.as_dict()))
        if telemetry is not None:
            self.add_source("telemetry",
                            lambda: _fmt_telemetry(telemetry.as_dict()))
        if video is not None:
            self.add_source("video", lambda: " | ".join(
                _fmt_stream(n, s) for n, s in video.stats()["streams"].items()))
        if camera is not None:
            self.add_source("camera", lambda: _fmt_camera(camera.as_dict()))

    def add_source(self, name: str, fn: Callable[[], str]) -> None:
        self._sources[name] = fn

    # ---------------------------------------------------------------- header

    def header(self, args=None, host: str = "") -> None:
        """The block that answers "what was actually running".

        Detail goes in at DEBUG on purpose. The file records DEBUG, so it keeps
        everything; the console keeps its one line and only shows the rest under
        ``-v``. That rule holds for the snapshots and the packet trace too — a
        debug log nobody can bear to leave on is not a debug log.
        """
        log.debug("=" * 72)
        log.debug("C12 ground station debug log — send this file when reporting")
        for key, value in environment(host).items():
            log.debug("env %-8s %s", key + ":", value)
        if args is not None:
            for key, value in sorted(vars(args).items()):
                log.debug("cfg %-18s %r", key, value)
        log.debug("=" * 72)
        log.info("debug log → %s (%.0f MB × %d, snapshot %.0fs, packets %s). "
                 "Send this file when reporting a problem.",
                 self.path, self.max_bytes / 1024 / 1024, self.backups,
                 self.snapshot_s, "all" if self.packets else "filtered")

    # --------------------------------------------------------------- packets

    def _on_packet(self, record: dict) -> None:
        """A journal sink. Runs on the event loop for every TX and RX.

        Two things are suppressed, and neither loses information:

        * the 20 Hz speed traffic, after the first few — it is counted instead;
        * an **identical repeat**. The camera state cache reads the same four
          registers once a second forever; printing all of it buries the one
          line that matters, which is the read whose answer *changed*. A repeat
          still prints once a minute, so the log never goes silent on a link
          that is still working.
        """
        cmd3 = record.get("cmd3") or "????"
        key = (record["dir"], cmd3)
        raw = record["raw"]
        self._seen[key] += 1

        if not self.packets and self._seen[key] > FIRST_N:
            if cmd3 in HIGH_RATE:
                self._quiet[key] += 1
                return
            if raw == self._last_raw.get(key) and self._repeat[key] < REPEAT_EVERY:
                self._repeat[key] += 1
                self._quiet[key] += 1
                return

        repeats, self._repeat[key] = self._repeat[key], 0
        self._last_raw[key] = raw
        log.debug("%-6s %s%s%s", record["dir"].upper(), raw,
                  "" if record.get("data") is None else "  data=%s" % record["data"],
                  "  (×%d identical)" % repeats if repeats else "")

    def _packet_line(self) -> str:
        seen = " ".join("%s/%s=%d" % (d, c, n)
                        for (d, c), n in sorted(self._seen.items()))
        quiet = sum(self._quiet.values())
        return "%s%s%s" % (
            seen or "none",
            " · %d repeated lines suppressed" % quiet if quiet else "",
            " · %d polls suppressed" % self._access.dropped
            if self._access.dropped else "")

    # -------------------------------------------------------------- snapshot

    async def start(self) -> None:
        if self.snapshot_s > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="debug-snapshot")

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.snapshot_s)
                self.snapshot()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - a broken snapshot must not kill the app
            log.exception("snapshot loop failed — no more snapshots")

    def snapshot(self) -> int:
        """One numbered group of lines. Grep one subsystem and read it as a trend."""
        self._seq += 1
        log.debug("snap[%d] proc %s", self._seq, self._proc.line())
        log.debug("snap[%d] packets %s", self._seq, self._packet_line())
        for name, fn in self._sources.items():
            try:
                log.debug("snap[%d] %s %s", self._seq, name, fn())
            except Exception as exc:  # noqa: BLE001 - report, never raise
                log.warning("snap[%d] %s unavailable: %s", self._seq, name, exc)
        return self._seq

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._link is not None:
            self._link.remove_journal_sink(self._on_packet)
            self._link = None
        self.snapshot()
        log.info("debug log closed after %.0f s", time.time() - self.started_at)
        if self.handler is not None:
            logging.getLogger().removeHandler(self.handler)
            self.handler.close()
            self.handler = None

    # ------------------------------------------------------------- operator

    def mark(self, note: str = "") -> str:
        """A line the operator plants at the moment something looked wrong.

        Worth more than it looks: it turns "somewhere in this hour" into an exact
        timestamp to read around, from someone who was watching the gimbal and
        not the log.
        """
        text = (note or "no note").strip()[:200]
        log.warning("======== MARK: %s ========", text)
        self.snapshot()
        return text

    # "info" from the browser lands at DEBUG: a click is file-worthy, not
    # console-worthy. Only what the operator needs to see while flying — a
    # refused command, a JS error — is allowed past.
    LEVELS = {"debug": logging.DEBUG, "info": logging.DEBUG,
              "warn": logging.WARNING, "warning": logging.WARNING,
              "error": logging.ERROR}

    def ui(self, entries: list) -> int:
        """Browser events. The other half of the story — see the module docstring."""
        for entry in entries:
            level = self.LEVELS.get(str(getattr(entry, "level", "info")).lower(),
                                    logging.INFO)
            detail = getattr(entry, "detail", None)
            if not isinstance(detail, str):
                with contextlib.suppress(TypeError, ValueError):
                    detail = json.dumps(detail, ensure_ascii=False, default=str)
            text = "" if detail in (None, "null", "") else str(detail)[:600]
            ms = getattr(entry, "ms", None)
            uilog.log(level, "%s%s %s",
                      "[+%.2fs] " % (ms / 1000.0) if isinstance(ms, (int, float))
                      else "",
                      str(getattr(entry, "event", "?"))[:80], text)
        return len(entries)

    # ------------------------------------------------------------- readback

    def files(self) -> list[Path]:
        """Rotated parts oldest first, then the live file — reading order."""
        parts = [self.path.with_name(self.path.name + ".%d" % i)
                 for i in range(self.backups, 0, -1)]
        return [p for p in parts + [self.path] if p.is_file()]

    def size(self) -> int:
        return sum(p.stat().st_size for p in self.files())

    def bundle(self) -> Iterator[bytes]:
        """Every part in reading order, as one download."""
        for part in self.files():
            yield ("\n===== %s =====\n" % part.name).encode()
            with part.open("rb") as fh:
                while chunk := fh.read(64 * 1024):
                    yield chunk

    def tail(self, lines: int = 200) -> str:
        """The end of the live file, for reading in the browser on the board."""
        if not self.path.is_file():
            return ""
        want = max(1, min(int(lines), 5000))
        with self.path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - 256 * 1024))
            text = fh.read().decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-want:])

    def as_dict(self) -> dict:
        files = self.files()
        return {
            "enabled": True,
            "path": str(self.path),
            "size_bytes": self.size(),
            "files": [p.name for p in files],
            "max_mb": round(self.max_bytes / 1024 / 1024, 1),
            "backups": self.backups,
            "snapshot_s": self.snapshot_s,
            "packets": "all" if self.packets else "filtered",
            "snapshots": self._seq,
            "started_at": self.started_at,
            "uptime_s": round(time.time() - self.started_at, 1),
        }
