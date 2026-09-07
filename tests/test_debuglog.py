"""The debug log — the file that has to be readable by someone who was not there.

Two properties are load-bearing and are what these tests hold onto:

* it must contain enough to diagnose a run **remotely**, and
* it must be cheap enough to leave on, or nobody will have it when it matters.
"""

import logging

import httpx
import pytest
from httpx import ASGITransport

from c12ctl.debuglog import DebugLog, FIRST_N, REPEAT_EVERY
from c12ctl.sim.c12_sim import C12Simulator
from c12ctl.transport.udp_link import UdpLink
from c12ctl.web.app import Session, create_app


@pytest.fixture
async def dbg(tmp_path):
    d = DebugLog(tmp_path / "debug.log", snapshot_s=0).install()
    yield d
    await d.close()


def read(d: DebugLog) -> str:
    for handler in [d.handler] if d.handler else []:
        handler.flush()
    return d.path.read_text()


# --------------------------------------------------------------------------
# What was running
# --------------------------------------------------------------------------


def test_header_answers_what_was_running(dbg):
    dbg.header(host="192.168.144.108")
    text = read(dbg)
    # The questions a remote reader asks first, in order.
    for want in ("env app:", "env host:", "env board:", "env python:",
                 "env packages:", "env net:", "env argv:"):
        assert want in text, want
    assert "c12ctl 0" in text, "the version has to be in the file, not implied"
    assert "192.168.144.108" in text, "which camera it was talking to"


def test_header_records_every_option(dbg):
    import argparse

    args = argparse.Namespace(video="live", decoder="v4l2h265dec", use_gsm=True)
    dbg.header(args)
    text = read(dbg)
    # "It does not work" is very often answered by one of these three lines.
    assert "cfg video" in text and "'live'" in text
    assert "cfg decoder" in text and "v4l2h265dec" in text
    assert "cfg use_gsm" in text and "True" in text


def test_install_does_not_make_the_console_noisier(tmp_path):
    """A debug log nobody can bear to leave on is not a debug log.

    The root gate has to open to DEBUG for the file's sake; the handler that was
    already there must not start printing DEBUG because of it.
    """
    root = logging.getLogger()
    console = logging.StreamHandler()
    root.addHandler(console)
    root.setLevel(logging.INFO)
    d = DebugLog(tmp_path / "d.log", snapshot_s=0).install()
    try:
        assert root.level == logging.DEBUG, "the file must see DEBUG records"
        assert console.level == logging.INFO, "the console must not"
    finally:
        root.removeHandler(console)
        root.removeHandler(d.handler)
        d.handler.close()


# --------------------------------------------------------------------------
# What the machine did
# --------------------------------------------------------------------------


def test_snapshot_reports_every_source(dbg):
    dbg.add_source("thing", lambda: "a=1 b=2")
    dbg.snapshot()
    dbg.snapshot()
    text = read(dbg)
    assert "snap[1] thing a=1 b=2" in text
    assert "snap[2] thing a=1 b=2" in text, "a single reading proves nothing"
    assert "snap[1] proc rss=" in text, "CPU and memory are the board's story"


def test_a_broken_source_does_not_stop_the_snapshot(dbg):
    def boom():
        raise RuntimeError("gone")

    dbg.add_source("broken", boom)
    dbg.add_source("fine", lambda: "ok")
    dbg.snapshot()
    text = read(dbg)
    assert "broken unavailable: gone" in text
    assert "snap[1] fine ok" in text


def test_cpu_is_not_reported_when_the_interval_is_meaningless(dbg):
    """Two snapshots microseconds apart divide by nearly zero."""
    dbg.snapshot()
    dbg.snapshot()
    assert "cpu=n/a" in read(dbg)


# --------------------------------------------------------------------------
# What the link did
# --------------------------------------------------------------------------


def packet(direction, cmd3, raw, data="00"):
    return {"dir": direction, "cmd3": cmd3, "raw": raw, "data": data}


def test_speed_spam_is_counted_not_printed(dbg):
    for i in range(200):
        dbg._on_packet(packet("tx", "GSY", "#TPUG2wGSY%02X" % (i % 40)))
    text = read(dbg)
    assert text.count("GSY") == FIRST_N, "the shape is worth keeping, the flood is not"
    dbg.snapshot()
    assert "tx/GSY=200" in read(dbg), "but the count must survive"


def test_an_identical_repeat_is_folded(dbg):
    """The camera cache reads the same register every second, forever."""
    for _ in range(FIRST_N + REPEAT_EVERY + 1):
        dbg._on_packet(packet("tx", "DZM", "#TPUD2rDZM004F"))
    text = read(dbg)
    assert text.count("#TPUD2rDZM004F") == FIRST_N + 1, "printed again, not every time"
    assert "(×%d identical)" % REPEAT_EVERY in text, "and it says how many it stood for"


def test_a_reply_that_changes_is_always_printed(dbg):
    """The whole point of keeping the poll: the read whose answer moved."""
    for _ in range(20):
        dbg._on_packet(packet("rx", "DZM", "#TPDU2rDZM004F", data="00"))
    dbg._on_packet(packet("rx", "DZM", "#TPDU2rDZM0150", data="01"))
    assert "#TPDU2rDZM0150" in read(dbg)


def test_debug_packets_keeps_everything(tmp_path):
    d = DebugLog(tmp_path / "all.log", snapshot_s=0, packets=True).install()
    try:
        for i in range(50):
            d._on_packet(packet("tx", "GSY", "#TPUG2wGSY%02X" % i))
        assert d.path.read_text().count("GSY") == 50
    finally:
        logging.getLogger().removeHandler(d.handler)
        d.handler.close()


# --------------------------------------------------------------------------
# Cheap enough to leave on
# --------------------------------------------------------------------------


async def test_rotation_bounds_the_disk(tmp_path):
    d = DebugLog(tmp_path / "r.log", max_mb=0.01, backups=2, snapshot_s=0).install()
    try:
        for i in range(4000):
            d._on_packet(packet("tx", "CAP%d" % i, "#TPUD2wCAP01 " + "x" * 60))
        assert len(d.files()) == 3, "the live file plus its two backups"
        assert d.size() < 0.01 * 1024 * 1024 * 4, "and nothing beyond the cap"
    finally:
        await d.close()


async def test_the_bundle_reads_oldest_first(tmp_path):
    d = DebugLog(tmp_path / "b.log", max_mb=0.01, backups=2, snapshot_s=0).install()
    try:
        for i in range(4000):
            d._on_packet(packet("tx", "C%d" % i, "#seq%05d " % i + "x" * 60))
        text = b"".join(d.bundle()).decode()
        first = text.index("b.log.2")
        assert first < text.index("b.log.1") < text.index("===== b.log ====")
        seqs = [int(line.split("#seq")[1][:5])
                for line in text.splitlines() if "#seq" in line]
        assert seqs == sorted(seqs), "a bundle out of order is worse than no bundle"
    finally:
        await d.close()


def test_tail_returns_the_end(dbg):
    for i in range(500):
        logging.getLogger("c12ctl.debug").warning("line %d", i)
    tail = dbg.tail(10).splitlines()
    assert len(tail) == 10
    assert "line 499" in tail[-1]


# --------------------------------------------------------------------------
# What the operator did
# --------------------------------------------------------------------------


def test_mark_plants_a_timestamp_and_a_snapshot(dbg):
    dbg.add_source("thing", lambda: "x=1")
    dbg.mark("gimbal did not turn left")
    text = read(dbg)
    assert "MARK: gimbal did not turn left" in text
    assert "snap[1] thing x=1" in text, "a mark with no state around it says little"


def test_a_mark_with_no_note_is_still_a_mark(dbg):
    dbg.mark("")
    assert "MARK: no note" in read(dbg)


# --------------------------------------------------------------------------
# The endpoints
# --------------------------------------------------------------------------


@pytest.fixture
async def client(tmp_path):
    sim = C12Simulator(seed=7)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    d = DebugLog(tmp_path / "web.log", snapshot_s=0).install()
    d.attach_link(link)
    app = create_app(link, Session(), debug=d)
    async with httpx.AsyncClient(transport=ASGITransport(app=app),
                                 base_url="http://t") as c:
        c.dbg = d
        yield c
    await d.close()
    await link.close()
    await sim.close()


@pytest.fixture
async def blind_client():
    """The app with the debug log switched off — every endpoint must say so."""
    sim = C12Simulator(seed=8)
    await sim.start("127.0.0.1", 0)
    link = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await link.start()
    app = create_app(link, Session())
    async with httpx.AsyncClient(transport=ASGITransport(app=app),
                                 base_url="http://t") as c:
        yield c
    await link.close()
    await sim.close()


async def test_status_describes_the_file(client):
    b = (await client.get("/api/debug")).json()
    assert b["enabled"] and b["path"].endswith("web.log")
    assert b["files"] == ["web.log"] and b["size_bytes"] >= 0


async def test_browser_events_reach_the_file(client):
    r = await client.post("/api/debug/log", json={"entries": [
        {"event": "boot", "ms": 12, "detail": {"ua": "Firefox/1"}},
        {"event": "cmd", "ms": 900, "detail": {"yaw": 8, "pitch": 0}},
        {"event": "ws-close", "level": "warn", "ms": 1200, "detail": {"code": 1006}},
    ]})
    assert r.status_code == 200 and r.json() == {"logged": 3}
    text = client.dbg.path.read_text()
    assert "[+0.01s] boot" in text and "Firefox/1" in text
    assert '"yaw": 8' in text
    assert "WARNING c12ctl.ui" in text and "1006" in text


async def test_a_flood_from_the_browser_is_capped(client):
    r = await client.post("/api/debug/log", json={
        "entries": [{"event": "spam", "ms": i} for i in range(300)]})
    assert r.json() == {"logged": 100}


async def test_mark_endpoint_writes_the_note(client):
    r = await client.post("/api/debug/mark",
                          json={"note": "gimbal khong quay khi bam nut trai"})
    assert r.status_code == 200
    assert "MARK: gimbal khong quay khi bam nut trai" in client.dbg.path.read_text()


async def test_tail_and_download_return_the_file(client):
    client.dbg.header()
    r = await client.get("/api/debug/tail?lines=5")
    assert r.status_code == 200 and len(r.text.splitlines()) == 5
    r = await client.get("/api/debug/download")
    assert r.status_code == 200
    assert "env app:" in r.text
    assert "attachment" in r.headers["content-disposition"]


async def test_packets_reach_the_log_through_the_link(client):
    await client.post("/api/cmd/read.model")
    text = client.dbg.path.read_text()
    assert "TX     #TPUD2rMOD" in text
    assert "RX     #TPDU2rMOD" in text, "the answer matters as much as the question"


async def test_everything_says_so_when_the_log_is_off(blind_client):
    assert (await blind_client.get("/api/debug")).json() == {"enabled": False}
    for method, path in (("post", "/api/debug/log"), ("post", "/api/debug/mark"),
                         ("get", "/api/debug/tail"), ("get", "/api/debug/download")):
        r = await getattr(blind_client, method)(
            path, **({"json": {"entries": []}} if path.endswith("log") else {}))
        assert r.status_code == 503, path
        assert "--no-debug-log" in r.json()["detail"]
