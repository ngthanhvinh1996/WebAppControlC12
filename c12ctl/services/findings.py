"""Capability map: which commands the C12 actually answers.

Every ``2r`` command is read-only, so sweeping the whole group is **completely
safe**. A command the camera does not support stays **silent** — and that silence
is data: it settles the places where the two source documents disagree, without
sending a single write.

Results go to JSONL (appended, never overwritten) so runs can be compared, plus a
regenerable markdown table to paste straight into the documentation.

:data:`PREDICTIONS` is the valuable part: each command carries what it *proves*
if it answers, and what it proves if it stays silent. That way the report does
not merely say "this one was silent" but "silence here confirms SLR is C13/C14
only".
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
    """What a result will prove."""

    if_alive: str = ""
    if_silent: str = ""
    expected: bool | None = None
    """What the documentation expects. ``None`` = no expectation, just collecting."""


#: Command → meaning of the result. Only commands whose result actually says
#: something are listed; the rest are here to collect the reply format.
PREDICTIONS: dict[str, Prediction] = {
    "read.palette": Prediction(
        if_alive="CONFIRMS the palette lives on IMG. skydroid-c12-protocol.md "
                 "guessed TAR — sweeping TAR would wreck the noise-reduction "
                 "config instead of changing colors.",
        if_silent="IMG does not answer. Check again before writing; we may have "
                  "to colorize client-side with cv2.applyColorMap.",
        expected=True,
    ),
    "read.thermal_spatial_nr": Prediction(
        if_alive="TAR is alive and is a 0–100 noise-reduction parameter, NOT the "
                 "palette. Record the original value before touching it.",
        if_silent="TAR does not answer on the C12.",
        expected=True,
    ),
    "read.zoom": Prediction(
        if_alive="CONFIRMS zoom lives on DZM (destination D). protocol.md guessed "
                 "ZMC (destination M) — that is the mechanical-lens command.",
        if_silent="DZM does not answer — retry before concluding.",
        expected=True,
    ),
    "read.ranging": Prediction(
        if_alive="SURPRISE: the C12 has a laser rangefinder. The bytecode says "
                 "SLR is C13/C14 only.",
        if_silent="Confirms SLR is C13/C14 only, exactly as the bytecode says.",
        expected=False,
    ),
    "read.thermal_scene": Prediction(
        if_alive="SURPRISE: the C12 has scene modes. protocol.md suspected this "
                 "was a C13 feature.",
        if_silent="Confirms protocol.md's suspicion: TSM belongs to the C13, not "
                  "the C12.",
        expected=False,
    ),
    "read.version": Prediction(
        if_alive="The command link works. Record the version string.",
        if_silent="The link is broken lower down — run preflight before going on.",
        expected=True,
    ),
    "read.model": Prediction(
        if_alive="Confirms we are talking to the model we think we are.",
        if_silent="Could not read the model.",
        expected=True,
    ),
    "read.sdcard": Prediction(
        if_alive="Record the RAW FORMAT. The length field is a single hex "
                 "character, so data is at most 15 characters — which rules out "
                 "every 2×32-bit hypothesis.",
        if_silent="SDC data=01 does not answer; try read.sdcard_alt (data=00).",
        expected=True,
    ),
    "read.sdcard_alt": Prediction(
        if_alive="SDC data=00 is a different sub-query. Compare its format with "
                 "read.sdcard.",
        if_silent="Only data=01 is supported.",
    ),
    "read.ext_config": Prediction(
        if_alive="EXT is alive. Remember the WRITE command uses the lowercase "
                 "#tp header.",
        if_silent="EXT is not present on the C12.",
    ),
    "read.ip_address": Prediction(
        if_alive="Record the current IP. READ ONLY — writing IPV loses the camera.",
        if_silent="IPV does not answer.",
    ),
    "read.gateway": Prediction(
        if_alive="Record the current gateway. READ ONLY.",
        if_silent="GTW does not answer.",
    ),
    "read.video_config": Prediction(
        if_alive="Record the VOM format. Needed for a later get→edit one "
                 "field→set workflow.",
        if_silent="VOM does not answer.",
    ),
    "read.resolution": Prediction(
        if_alive="VID is alive. Cross-check the value against the real RTSP "
                 "stream resolution.",
        if_silent="VID does not answer.",
        expected=True,
    ),
}


@dataclass
class Probe:
    """One probe of one command."""

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
    """Sweep every 🟢 SAFE command. Changes no state whatsoever."""
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
    """Append one line per probe. Never overwrites — runs stay comparable."""
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
    """A markdown table that can be pasted straight into the documentation."""
    lines = [
        "# C12 capability map",
        "",
        "Generated by `c12ctl.services.findings`. Uses `2r` commands only — read-only.",
        "",
        "- Device: `%s`" % report.host,
        "- Time: %s" % time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(report.started_at)),
        "- Result: **%d/%d commands answered**, %d silent"
        % (len(report.alive), len(report.probes), len(report.silent)),
        "",
    ]

    if report.surprises:
        lines += ["## ⚠️ Different from expectation", ""]
        for p in report.surprises:
            lines.append("- **%s** (`%s`) — %s. %s"
                         % (p.name, p.cmd3,
                            "answered although not expected to" if p.alive
                            else "silent although expected to answer",
                            p.note))
        lines.append("")

    lines += [
        "## Commands that answered", "",
        "| Command | Frame | Raw data | Decoded | ms |",
        "|---|---|---|---|---|",
    ]
    for p in report.alive:
        lines.append("| `%s` | `%s` | `%s` | %s | %s |"
                     % (p.name, p.frame, p.raw if p.raw is not None else "—",
                        _fmt(p.value), p.latency_ms))

    lines += ["", "## Commands that stayed silent", "",
              "No reply = the C12 does not support it. That is data, not a failure.",
              "", "| Command | Frame | Which means |", "|---|---|---|"]
    for p in report.silent:
        lines.append("| `%s` | `%s` | %s |" % (p.name, p.frame, p.note or "—"))

    notes = [p for p in report.alive if p.note]
    if notes:
        lines += ["", "## Per-command notes", ""]
        for p in notes:
            lines.append("- **%s** — %s" % (p.name, p.note))

    return "\n".join(lines) + "\n"


def render_text(report: Report) -> str:
    """Terminal rendering."""
    width = max((len(p.name) for p in report.probes), default=20)
    lines = []
    for p in report.probes:
        mark = "!" if p.surprise else " "
        if p.alive:
            lines.append("%s answered %-*s %-18s %s"
                         % (mark, width, p.name, p.frame, _fmt(p.value)))
        else:
            lines.append("%s silent   %-*s %-18s —" % (mark, width, p.name, p.frame))
    lines.append("")
    lines.append("%d/%d answered, %d silent, %d different from expectation"
                 % (len(report.alive), len(report.probes),
                    len(report.silent), len(report.surprises)))
    return "\n".join(lines)


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, dict):
        return ", ".join("%s=%s" % kv for kv in value.items())
    return str(value)
