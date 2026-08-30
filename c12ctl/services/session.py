"""Synchronised session recording: video + commands + attitude — phase 6.

The question this exists to answer is the one that keeps coming up while
reverse-engineering a device: *what did we send just before the camera did that?*
Answering it from three separate places — a packet log here, a video file there,
attitude numbers scrolling past in a terminal — means aligning clocks by hand,
which is exactly the kind of work that hides the interesting five seconds.

So everything lands in one directory on **one clock**. ``time.monotonic()`` is the
ordering key throughout, because it is already the timebase of
:class:`~c12ctl.video.bus.Frame.captured_at`, of the link journal, and of the
telemetry service. Wall-clock time is recorded alongside it for human reference
only; it is never used for ordering, since it can jump.

    logs/sessions/<id>/
      meta.json        what was recorded, and with what configuration
      events.jsonl     one line per event, in monotonic order
      visible.mjpeg    JPEG frames, concatenated
      thermal.mjpeg

Frames are stored as concatenated JPEGs rather than as a real container: it needs
no codec, survives a truncated file (a crash mid-recording still leaves every
frame before it readable), and every frame's byte offset is in ``events.jsonl``
so extracting frame N stays a seek rather than a scan.

Two limits are enforced rather than advertised. Recording holds a **viewer** on
the MJPEG stream, so it reuses the existing encode instead of adding one — but a
disk fills up regardless, and this is meant to run on a Pi during a flight test.
The recorder stops itself on a byte cap and on a duration cap, and says which one
tripped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..protocol.types import Attitude
from ..transport.udp_link import UdpLink

log = logging.getLogger("c12ctl.session")

DEFAULT_ROOT = "logs/sessions"

FRAME_FPS = 5.0
"""Recorded frames per second per stream. Far below the source rate on purpose:
30 fps × ~24 KB is 720 KB/s per stream, which fills a card faster than it earns
its keep for protocol work."""

MAX_BYTES = 512 * 1024 * 1024
"""Stop before the disk does."""

MAX_SECONDS = 3600.0
"""A recording nobody remembered to stop must still end."""

FLUSH_EVERY = 50
"""Events between flushes. A crash loses at most this many lines."""


@dataclass
class RecorderStats:
    events: int = 0
    packets: int = 0
    attitudes: int = 0
    frames: dict = field(default_factory=dict)
    bytes_written: int = 0

    def as_dict(self) -> dict:
        return {
            "events": self.events, "packets": self.packets,
            "attitudes": self.attitudes, "frames": dict(self.frames),
            "bytes_written": self.bytes_written,
        }


class SessionRecorder:
    """Records one session at a time into its own directory."""

    def __init__(
        self,
        link: UdpLink,
        *,
        root: str | Path = DEFAULT_ROOT,
        video=None,
        telemetry=None,
        frame_fps: float = FRAME_FPS,
        max_bytes: int = MAX_BYTES,
        max_seconds: float = MAX_SECONDS,
    ) -> None:
        self.link = link
        self.root = Path(root)
        self.video = video
        self.telemetry = telemetry
        self.frame_fps = max(0.1, frame_fps)
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds

        self.session_id: str | None = None
        self.path: Path | None = None
        self.note = ""
        self.started_at = 0.0
        self.started_mono = 0.0
        self.stop_reason = ""
        self.stats = RecorderStats()

        self._events = None
        self._streams: dict[str, object] = {}
        self._tasks: list[asyncio.Task] = []
        self._since_flush = 0
        self._stopping = False

    # ---------------------------------------------------------------- state

    @property
    def recording(self) -> bool:
        return self.session_id is not None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_mono if self.recording else 0.0

    # ------------------------------------------------------------- lifecycle

    async def start(self, note: str = "") -> dict:
        """Begin a recording. Raises :class:`RuntimeError` if one is already running."""
        if self.recording:
            raise RuntimeError(
                "a recording is already running (%s) — stop it first" % self.session_id
            )

        self.session_id = time.strftime("%Y%m%dT%H%M%S")
        self.path = self.root / self.session_id
        self.path.mkdir(parents=True, exist_ok=True)
        self.note = note
        self.started_at = time.time()
        self.started_mono = time.monotonic()
        self.stop_reason = ""
        self.stats = RecorderStats()
        self._since_flush = 0
        self._stopping = False

        self._events = (self.path / "events.jsonl").open("w", encoding="utf-8")
        (self.path / "meta.json").write_text(
            json.dumps(self._meta(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

        self.link.add_journal_sink(self._on_packet)
        if self.telemetry is not None:
            self.telemetry.subscribe(self._on_attitude)

        if self.video is not None:
            for name, stream in self.video.streams.items():
                path = self.path / ("%s.mjpeg" % name)
                self._streams[name] = path.open("wb")
                self.stats.frames[name] = 0
                self._tasks.append(asyncio.create_task(
                    self._record_stream(name, stream), name="record-%s" % name
                ))

        self._tasks.append(asyncio.create_task(self._watchdog(), name="record-watchdog"))
        self._emit("marker", text=note or "recording started")
        log.warning("session recording started: %s (%s)", self.session_id, self.path)
        return self.as_dict()

    async def stop(self, reason: str = "requested") -> dict:
        """End the recording and return its summary. Safe to call when idle."""
        if not self.recording or self._stopping:
            return self.as_dict()
        self._stopping = True

        self.link.remove_journal_sink(self._on_packet)
        if self.telemetry is not None:
            self.telemetry.unsubscribe(self._on_attitude)

        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        self.stop_reason = reason
        self._emit("marker", text="recording stopped: " + reason)

        for fh in self._streams.values():
            fh.close()
        self._streams.clear()
        if self._events is not None:
            self._events.close()
            self._events = None

        # Clear the active state BEFORE summarising. Summarising first would
        # stamp `recording: true` into a finished session's meta.json, where it
        # would stay wrong forever.
        elapsed = round(self.elapsed, 1)
        sid, path = self.session_id, self.path
        self.session_id = None

        summary = self.as_dict() | {
            "id": sid, "path": str(path), "elapsed_s": elapsed,
            "stopped_at": time.time(),
        }
        (path / "meta.json").write_text(
            json.dumps({**self._meta(), "id": sid, "summary": summary},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.warning("session recording stopped (%s): %d events, %.1f MB",
                    reason, self.stats.events, self.stats.bytes_written / 1e6)

        self._stopping = False
        return summary

    async def close(self) -> None:
        if self.recording:
            await self.stop("shutting down")

    # ------------------------------------------------------------- recording

    def _meta(self) -> dict:
        streams = {}
        if self.video is not None:
            streams = {n: s.describe() for n, s in self.video.streams.items()}
        return {
            "id": self.session_id,
            "note": self.note,
            "started_at": self.started_at,
            "started_mono": self.started_mono,
            "host": "%s:%d" % self.link.addr,
            "dry_run": self.link.dry_run,
            "platform": platform.platform(),
            "frame_fps": self.frame_fps,
            "max_bytes": self.max_bytes,
            "max_seconds": self.max_seconds,
            "streams": streams,
        }

    def _emit(self, kind: str, **fields) -> None:
        if self._events is None:
            return
        record = {"mono": time.monotonic(), "t": time.time(), "kind": kind, **fields}
        record["at"] = round(record["mono"] - self.started_mono, 4)
        self._events.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.stats.events += 1
        self._since_flush += 1
        if self._since_flush >= FLUSH_EVERY:
            self._events.flush()
            self._since_flush = 0

    def _on_packet(self, record: dict) -> None:
        """Called by the link for every TX/RX packet, including malformed ones."""
        self.stats.packets += 1
        self._emit("packet", **{k: v for k, v in record.items() if k not in ("t", "mono")})

    def _on_attitude(self, attitude: Attitude) -> None:
        self.stats.attitudes += 1
        self._emit("attitude", **attitude.as_dict())

    async def _record_stream(self, name: str, stream) -> None:
        """Hold a viewer on one stream and write the frames it already encodes."""
        fh = self._streams[name]
        period = 1.0 / self.frame_fps
        seq = 0
        next_at = 0.0
        try:
            async with stream.viewer():
                while True:
                    got = await stream.next_jpeg(after=seq, timeout=2.0)
                    if got is None:
                        continue
                    payload, seq = got
                    now = time.monotonic()
                    if now < next_at:
                        continue
                    next_at = now + period

                    offset = fh.tell()
                    fh.write(payload)
                    self.stats.frames[name] = self.stats.frames.get(name, 0) + 1
                    self.stats.bytes_written += len(payload)
                    self._emit("frame", stream=name, seq=seq,
                               offset=offset, length=len(payload))
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - a recording fault must not kill the app
            log.exception("recording stream %s failed", name)

    async def _watchdog(self) -> None:
        """Enforce the byte and duration caps. A full disk on a Pi mid-test is a
        worse outcome than a recording that ends early and says so."""
        while True:
            await asyncio.sleep(0.5)
            if self.stats.bytes_written >= self.max_bytes:
                asyncio.create_task(self.stop("byte cap reached (%d MB)"
                                              % (self.max_bytes // 1_000_000)))
                return
            if self.elapsed >= self.max_seconds:
                asyncio.create_task(self.stop("duration cap reached (%.0f s)"
                                              % self.max_seconds))
                return

    # ------------------------------------------------------------------ view

    def as_dict(self) -> dict:
        return {
            "recording": self.recording,
            "id": self.session_id,
            "path": str(self.path) if self.path else None,
            "note": self.note,
            "started_at": self.started_at or None,
            "elapsed_s": round(self.elapsed, 1),
            "stop_reason": self.stop_reason,
            "frame_fps": self.frame_fps,
            "max_bytes": self.max_bytes,
            "max_seconds": self.max_seconds,
            "stats": self.stats.as_dict(),
        }

    def list_sessions(self) -> list[dict]:
        """Every recording on disk, newest first."""
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(self.root.iterdir(), reverse=True):
            if not (path / "meta.json").is_file():
                continue
            try:
                meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):  # pragma: no cover - a half-written meta
                continue
            out.append({
                "id": meta.get("id") or path.name,
                "note": meta.get("note", ""),
                "started_at": meta.get("started_at"),
                "summary": meta.get("summary"),
                "size_mb": round(
                    sum(f.stat().st_size for f in path.iterdir() if f.is_file()) / 1e6, 2
                ),
            })
        return out


# --------------------------------------------------------------------------


class SessionReader:
    """Reads a recording back: summary, timeline, and individual frames."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not (self.path / "meta.json").is_file():
            raise FileNotFoundError("no recording at %s" % self.path)
        self.meta = json.loads((self.path / "meta.json").read_text(encoding="utf-8"))

    def events(self):
        """Yield every event in recorded (monotonic) order."""
        events = self.path / "events.jsonl"
        if not events.is_file():
            return
        with events.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:  # pragma: no cover - truncated final line
                    continue

    def frame(self, stream: str, index: int) -> bytes | None:
        """Extract one frame by its index within that stream.

        A seek, not a scan: the byte offset is in the event log.
        """
        entries = [e for e in self.events()
                   if e.get("kind") == "frame" and e.get("stream") == stream]
        if not -len(entries) <= index < len(entries):
            return None
        entry = entries[index]
        path = self.path / ("%s.mjpeg" % stream)
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            fh.seek(entry["offset"])
            return fh.read(entry["length"])

    def summary(self) -> dict:
        """What happened, condensed — the point of recording in the first place."""
        kinds: dict[str, int] = {}
        commands: dict[str, dict[str, int]] = {}
        frames: dict[str, int] = {}
        markers = []
        yaw = pitch = roll = None
        first = last = None

        for event in self.events():
            kind = event.get("kind", "?")
            kinds[kind] = kinds.get(kind, 0) + 1
            at = event.get("at")
            if at is not None:
                first = at if first is None else min(first, at)
                last = at if last is None else max(last, at)

            if kind == "packet":
                cmd3 = event.get("cmd3") or "?"
                bucket = commands.setdefault(cmd3, {"tx": 0, "rx": 0})
                direction = event.get("dir", "")
                if direction in bucket:
                    bucket[direction] += 1
            elif kind == "frame":
                name = event.get("stream", "?")
                frames[name] = frames.get(name, 0) + 1
            elif kind == "attitude":
                yaw = _extend(yaw, event.get("yaw"))
                pitch = _extend(pitch, event.get("pitch"))
                roll = _extend(roll, event.get("roll"))
            elif kind == "marker":
                markers.append({"at": at, "text": event.get("text", "")})

        return {
            "id": self.meta.get("id"),
            "note": self.meta.get("note", ""),
            "started_at": self.meta.get("started_at"),
            "duration_s": round((last or 0) - (first or 0), 2),
            "kinds": kinds,
            "commands": dict(sorted(commands.items())),
            "frames": frames,
            "attitude_range": {"yaw": yaw, "pitch": pitch, "roll": roll},
            "markers": markers,
        }


def _extend(span, value):
    if value is None:
        return span
    if span is None:
        return [value, value]
    return [min(span[0], value), max(span[1], value)]
