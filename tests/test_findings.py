"""Bản đồ năng lực và preflight."""

import json

import pytest

from c12ctl.protocol.registry import COMMANDS, read_commands
from c12ctl.services import findings, preflight
from c12ctl.sim.c12_sim import C12Simulator
from c12ctl.transport.udp_link import UdpLink


@pytest.fixture
async def link():
    sim = C12Simulator(seed=42)
    await sim.start("127.0.0.1", 0)
    lk = UdpLink("127.0.0.1", sim.port, local_port=0, min_tx_gap=0.001)
    await lk.start()
    lk.sim = sim
    yield lk
    await lk.close()
    await sim.close()


# --------------------------------------------------------------------------
# Sweep
# --------------------------------------------------------------------------


async def test_sweep_covers_every_safe_command(link):
    report = await findings.sweep(link, timeout=0.1)
    assert {p.name for p in report.probes} == {c.name for c in read_commands()}


async def test_sweep_separates_alive_from_silent(link):
    report = await findings.sweep(link, timeout=0.1)
    assert report.alive and report.silent
    assert len(report.alive) + len(report.silent) == len(report.probes)

    by_name = {p.name: p for p in report.probes}
    assert by_name["read.version"].alive
    assert by_name["read.model"].alive
    assert not by_name["read.ranging"].alive


async def test_sweep_records_raw_data_not_just_decoded(link):
    """Format thô là thứ pha 1 cần để chốt các format chưa xác minh."""
    report = await findings.sweep(link, timeout=0.1)
    sd = next(p for p in report.probes if p.name == "read.sdcard")
    assert sd.raw is not None
    assert len(sd.raw) <= 15, "data không thể dài quá 15 — trường length 1 hex"
    assert sd.value["present"] is True


async def test_sweep_measures_latency_for_live_commands(link):
    report = await findings.sweep(link, timeout=0.1)
    for p in report.alive:
        assert p.latency_ms is not None and p.latency_ms >= 0
    for p in report.silent:
        assert p.latency_ms is None


async def test_sweep_is_read_only(link):
    """Quét không được đổi bất kỳ trạng thái nào của camera."""
    st = link.sim.state
    before = (st.zoom, st.palette, st.resolution, st.recording,
              dict(st.thermal), st.yaw, st.pitch, st.gaa_rate)
    await findings.sweep(link, timeout=0.1)
    after = (st.zoom, st.palette, st.resolution, st.recording,
             dict(st.thermal), st.yaw, st.pitch, st.gaa_rate)
    assert before == after


async def test_sweep_only_sends_read_frames(link):
    seen = []
    original = link._transport.sendto
    link._transport.sendto = lambda d, a: (
        seen.append(d.decode(errors="replace").strip()), original(d, a)
    )[1]
    await findings.sweep(link, timeout=0.1)
    assert seen, "không gửi gói nào?"
    assert all(f[6] == "r" for f in seen), "quét gửi lệnh GHI: %r" % [
        f for f in seen if f[6] != "r"
    ]


# --------------------------------------------------------------------------
# Kỳ vọng và bất ngờ
# --------------------------------------------------------------------------


async def test_predictions_reference_only_real_commands():
    unknown = set(findings.PREDICTIONS) - set(COMMANDS)
    assert unknown == set(), "PREDICTIONS trỏ tới lệnh không có: %s" % unknown


async def test_no_surprises_against_simulator(link):
    """Simulator dựng theo đúng kỳ vọng của tài liệu, nên không được có bất ngờ.

    Nếu test này đỏ thì hoặc simulator sai, hoặc PREDICTIONS sai — cả hai đều
    đáng biết trước khi cắm phần cứng thật.
    """
    report = await findings.sweep(link, timeout=0.1)
    assert report.surprises == [], [
        (p.name, p.alive) for p in report.surprises
    ]


async def test_surprise_flagged_when_expectation_breaks(link):
    """Cho SLR trả lời — trái kỳ vọng — và báo cáo phải đánh dấu bất ngờ."""
    link.sim.state.__dict__.setdefault("_", None)
    original = link.sim._handle_read

    def patched(cmd, data):
        if cmd == "SLR":
            return link.sim._reply("SLR", "0042")
        return original(cmd, data)

    link.sim._handle_read = patched
    report = await findings.sweep(
        link, timeout=0.1, commands=[COMMANDS["read.ranging"]]
    )
    assert report.surprises
    assert "BẤT NGỜ" in report.surprises[0].note


async def test_alive_and_silent_both_carry_notes(link):
    report = await findings.sweep(link, timeout=0.1)
    palette = next(p for p in report.probes if p.name == "read.palette")
    ranging = next(p for p in report.probes if p.name == "read.ranging")
    assert "IMG" in palette.note
    assert "C13" in ranging.note


# --------------------------------------------------------------------------
# Xuất kết quả
# --------------------------------------------------------------------------


async def test_jsonl_appends_across_runs(link, tmp_path):
    path = tmp_path / "findings.jsonl"
    r1 = await findings.sweep(link, timeout=0.1)
    findings.append_jsonl(r1, path)
    n1 = len(path.read_text().splitlines())

    r2 = await findings.sweep(link, timeout=0.1)
    findings.append_jsonl(r2, path)
    lines = path.read_text().splitlines()
    assert len(lines) == 2 * n1, "phải nối thêm chứ không đè"

    records = [json.loads(l) for l in lines]
    assert len({r["run"] for r in records}) == 2
    assert all("alive" in r and "frame" in r for r in records)


async def test_markdown_report_is_useful(link):
    md = findings.render_markdown(await findings.sweep(link, timeout=0.1))
    assert "# Bản đồ năng lực C12" in md
    assert "## Lệnh trả lời" in md
    assert "## Lệnh im lặng" in md
    assert "read.version" in md
    assert "#TPUD2rVER0051" in md
    assert "C13" in md, "lý do một lệnh im lặng phải có trong báo cáo"


async def test_text_report_marks_surprises(link):
    text = findings.render_text(await findings.sweep(link, timeout=0.1))
    assert "trả lời" in text and "im lặng" in text


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


def test_control_port_check_detects_busy_port():
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    port = sock.getsockname()[1]
    try:
        check = preflight.check_control_port(port)
        assert check.status == preflight.FAIL
        assert "ss -lunp" in check.fix, "phải chỉ được cách tìm thủ phạm"
    finally:
        sock.close()

    assert preflight.check_control_port(port).status == preflight.OK


def test_host_ip_check_explains_static_ip_when_absent():
    check = preflight.check_host_ip()
    if check.status == preflight.FAIL:
        assert "ip addr add" in check.fix
        assert "192.168.144" in check.fix
        assert "DHCP" in check.detail


def test_link_check_mentions_power_and_cable_facts():
    check = preflight.check_link()
    if check.status == preflight.FAIL:
        assert "JST-2P" in check.fix, "RJ45 không cấp nguồn — phải nói rõ"
        assert "100 Mb/s" in check.fix


async def test_tcp_port_check_reports_open_and_closed():
    import asyncio

    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        ok = await preflight.check_tcp_port("127.0.0.1", port, "test")
        assert ok.status == preflight.OK
    finally:
        server.close()
        await server.wait_closed()

    closed = await preflight.check_tcp_port("127.0.0.1", port, "test", timeout=0.5)
    assert closed.status == preflight.FAIL


async def test_preflight_runs_all_checks_without_root():
    pre = await preflight.run("127.0.0.1", control_port=0, check_rtsp=False)
    assert len(pre.checks) == 5
    assert all(c.name for c in pre.checks)
    text = preflight.render(pre)
    assert text and "\n" in text


async def test_preflight_blocking_lists_only_failures():
    pre = await preflight.run("127.0.0.1", control_port=0, check_rtsp=False)
    assert all(c.status == preflight.FAIL for c in pre.blocking)
