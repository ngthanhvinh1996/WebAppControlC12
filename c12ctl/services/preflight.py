"""Connectivity diagnostics, run before talking to the camera.

When the C12 "does not respond", the cause is almost always below the protocol
layer: the cable is not plugged in, the host has no address on the camera's
subnet, or UDP port 5000 is taken by another app. Guessing at the command layer
while the fault is at the link layer is the most expensive way to spend an hour.

This module answers "why can't I control it" at a glance, and every failed check
carries **the exact command to fix it**, not just a failure flag.

No root required. The one thing that does need privileges — assigning a static
IP — is *printed* for the operator to run, never run automatically: changing
someone else's network configuration is their call.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CAMERA_IP = "192.168.144.108"
CAMERA_SUBNET = ipaddress.ip_network("192.168.144.0/24")
SUGGESTED_HOST_IP = "192.168.144.20/24"
RTSP_PORTS = {554: "visible (stream=1)", 555: "thermal (stream=2)"}
CONTROL_PORT = 5000

OK, FAIL, WARN, SKIP = "ok", "fail", "warn", "skip"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""

    @property
    def passed(self) -> bool:
        return self.status in (OK, SKIP, WARN)

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "fix": self.fix}


@dataclass
class Preflight:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    def as_dict(self) -> dict:
        return {"ok": self.ok, "checks": [c.as_dict() for c in self.checks]}


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------


def _interfaces() -> dict[str, dict]:
    """List interfaces: carrier, operstate, IPv4 addresses."""
    out: dict[str, dict] = {}
    net = Path("/sys/class/net")
    if not net.is_dir():  # pragma: no cover - not Linux
        return out
    for iface in sorted(p.name for p in net.iterdir()):
        if iface == "lo" or iface.startswith(("docker", "veth", "br-")):
            continue
        info = {"carrier": None, "operstate": None, "addrs": []}
        for key in ("carrier", "operstate"):
            try:
                info[key] = (net / iface / key).read_text().strip()
            except OSError:
                pass
        out[iface] = info

    for iface, addr in _ipv4_addresses():
        if iface in out:
            out[iface]["addrs"].append(addr)
    return out


def _ipv4_addresses() -> list[tuple[str, str]]:
    """(interface, CIDR) for every IPv4 address, via ``ip -4 addr``."""
    try:
        raw = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return []
    found = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet":
            found.append((parts[1], parts[3]))
    return found


def check_host_ip() -> Check:
    """The host needs an address on the camera's subnet. The C12 runs **no**
    DHCP server."""
    for iface, cidr in _ipv4_addresses():
        try:
            addr = ipaddress.ip_interface(cidr)
        except ValueError:  # pragma: no cover
            continue
        if addr.ip in CAMERA_SUBNET:
            return Check("Host IP on the camera subnet", OK,
                         "%s has %s" % (iface, cidr))

    # Prefer an interface that already has a carrier, but still suggest a wired
    # one that is down — the operator is about to plug into it, and printing the
    # real name is more useful than "<iface>".
    wired = [
        name for name in _interfaces()
        if not name.startswith(("wlp", "wlan", "tailscale", "tun", "wg"))
    ]
    live = [n for n in wired if _interfaces()[n].get("carrier") == "1"]
    target = (live or wired or ["<iface>"])[0]
    return Check(
        "Host IP on the camera subnet", FAIL,
        "No interface holds an address in %s. The camera runs no DHCP server, "
        "so the host must set a static IP itself." % CAMERA_SUBNET,
        "sudo ip addr add %s dev %s && sudo ip link set %s up"
        % (SUGGESTED_HOST_IP, target, target),
    )


def check_link() -> Check:
    """Is any Ethernet port carrying a link (carrier=1)?"""
    ifaces = _interfaces()
    wired = {
        name: info for name, info in ifaces.items()
        if not name.startswith(("wlp", "wlan", "tailscale", "tun", "wg"))
    }
    up = [name for name, info in wired.items() if info.get("carrier") == "1"]
    if up:
        return Check("Ethernet cable", OK, "carrier=1 on " + ", ".join(up))
    if not wired:
        return Check("Ethernet cable", WARN, "no wired interface found")
    listing = ", ".join(
        "%s(carrier=%s)" % (n, i.get("carrier")) for n, i in wired.items()
    )
    return Check(
        "Ethernet cable", FAIL,
        "No wired port has a link: " + listing,
        "Plug the cable from the C12 into an Ethernet port. Note the Skydroid "
        "cable has only 4 conductors (TX±/RX±) so the link will negotiate at "
        "100 Mb/s — that is normal. RJ45 does NOT carry power: the camera needs "
        "7.2–72 V through the JST-2P connector.",
    )


def check_ping(host: str = CAMERA_IP, timeout: float = 1.0) -> Check:
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(max(1, timeout))), host],
            capture_output=True, text=True, timeout=timeout + 3,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return Check("Ping the camera", WARN, "could not run ping: %s" % exc)

    if result.returncode == 0:
        line = next((l for l in result.stdout.splitlines() if "time=" in l), "")
        return Check("Ping the camera", OK, line.strip() or host + " answered")
    return Check(
        "Ping the camera", FAIL,
        "%s does not answer ICMP." % host,
        "Check the cable and the host IP first. If both are right and it is "
        "still silent, the camera's IP may have been changed — scan the subnet "
        "with: sudo nmap -sn 192.168.144.0/24",
    )


def check_arp(host: str = CAMERA_IP) -> Check:
    """ARP proves layer-2 reachability even when ICMP is blocked."""
    try:
        lines = Path("/proc/net/arp").read_text().splitlines()[1:]
    except OSError:  # pragma: no cover - not Linux
        return Check("ARP", SKIP, "/proc/net/arp is not readable")
    for line in lines:
        parts = line.split()
        if parts and parts[0] == host:
            mac = parts[3] if len(parts) > 3 else "?"
            if mac != "00:00:00:00:00:00":
                return Check("ARP", OK, "%s at %s" % (host, mac))
    return Check(
        "ARP", WARN,
        "No ARP entry for %s yet — normal if no packet has been sent." % host,
    )


async def check_tcp_port(host: str, port: int, label: str,
                         timeout: float = 2.0) -> Check:
    name = "RTSP %d — %s" % (port, label)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except asyncio.TimeoutError:
        return Check(name, FAIL, "timed out after %.0fs" % timeout,
                     "Is the camera producing video yet? The RTSP streams only "
                     "open once the camera has finished booting.")
    except OSError as exc:
        return Check(name, FAIL, str(exc))
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:  # pragma: no cover
        pass
    return Check(name, OK, "port open")


def check_control_port(port: int = CONTROL_PORT) -> Check:
    """The local UDP port — the number one operational failure here."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as exc:
        return Check(
            "UDP port %d free" % port, FAIL,
            "Could not bind: %s" % exc,
            "This port is often taken by a companion app or a ground station. "
            "Find the culprit with: ss -lunp | grep %d — then close it, or run "
            "the web app with --local-port 0." % port,
        )
    finally:
        sock.close()
    return Check("UDP port %d free" % port, OK, "bind succeeded")


# --------------------------------------------------------------------------


async def run(host: str = CAMERA_IP, *, control_port: int = CONTROL_PORT,
              check_rtsp: bool = True) -> Preflight:
    """Run every check, ordered from the lowest layer upward."""
    pre = Preflight()
    pre.checks.append(check_link())
    pre.checks.append(check_host_ip())
    pre.checks.append(check_control_port(control_port))
    pre.checks.append(check_ping(host))
    pre.checks.append(check_arp(host))
    if check_rtsp:
        results = await asyncio.gather(
            *(check_tcp_port(host, p, label) for p, label in RTSP_PORTS.items())
        )
        pre.checks.extend(results)
    return pre


def render(pre: Preflight) -> str:
    """Terminal rendering."""
    glyph = {OK: "  ok  ", FAIL: " FAIL ", WARN: " warn ", SKIP: " skip "}
    lines = []
    for c in pre.checks:
        lines.append("[%s] %s" % (glyph.get(c.status, "  ??  "), c.name))
        if c.detail:
            lines.append("        " + c.detail)
        if c.fix:
            for fixline in c.fix.split("\n"):
                lines.append("        → " + fixline)
    return "\n".join(lines)
