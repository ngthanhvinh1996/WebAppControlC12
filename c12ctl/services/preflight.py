"""Chẩn đoán kết nối trước khi nói chuyện với camera.

Khi C12 "không phản hồi" thì nguyên nhân gần như luôn nằm ở tầng dưới giao thức:
cáp chưa cắm, host chưa có IP cùng dải, hoặc cổng UDP 5000 bị app khác chiếm.
Đoán mò ở tầng lệnh trong khi lỗi nằm ở tầng link là cách tốn thời gian nhất.

Module này trả lời "tại sao không điều khiển được" trong một cái liếc, và mỗi
kiểm tra hỏng đều kèm **lệnh cụ thể để sửa** chứ không chỉ báo hỏng.

Không cần root. Việc duy nhất cần quyền — gán IP tĩnh — được *in ra* cho người
dùng chạy, không tự chạy: đổi cấu hình mạng của máy người khác là việc của họ.
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
RTSP_PORTS = {554: "khả kiến (stream=1)", 555: "nhiệt (stream=2)"}
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
# Kiểm tra riêng lẻ
# --------------------------------------------------------------------------


def _interfaces() -> dict[str, dict]:
    """Liệt kê interface: carrier, operstate, địa chỉ IPv4."""
    out: dict[str, dict] = {}
    net = Path("/sys/class/net")
    if not net.is_dir():  # pragma: no cover - không phải Linux
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
    """(interface, CIDR) cho mọi địa chỉ IPv4, qua ``ip -4 addr``."""
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
    """Host phải có IP cùng dải với camera. C12 **không** có DHCP server."""
    for iface, cidr in _ipv4_addresses():
        try:
            addr = ipaddress.ip_interface(cidr)
        except ValueError:  # pragma: no cover
            continue
        if addr.ip in CAMERA_SUBNET:
            return Check("IP host cùng dải camera", OK,
                         "%s có %s" % (iface, cidr))

    # Ưu tiên cổng đang có tín hiệu, nhưng vẫn gợi ý cổng có dây đang down —
    # người dùng sắp cắm cáp vào nó, và in tên thật hữu ích hơn "<iface>".
    wired = [
        name for name in _interfaces()
        if not name.startswith(("wlp", "wlan", "tailscale", "tun", "wg"))
    ]
    live = [n for n in wired if _interfaces()[n].get("carrier") == "1"]
    target = (live or wired or ["<iface>"])[0]
    return Check(
        "IP host cùng dải camera", FAIL,
        "Không interface nào có địa chỉ trong %s. Camera không chạy DHCP server "
        "nên host phải tự gán IP tĩnh." % CAMERA_SUBNET,
        "sudo ip addr add %s dev %s && sudo ip link set %s up"
        % (SUGGESTED_HOST_IP, target, target),
    )


def check_link() -> Check:
    """Có cổng Ethernet nào cắm cáp không (carrier=1)?"""
    ifaces = _interfaces()
    wired = {
        name: info for name, info in ifaces.items()
        if not name.startswith(("wlp", "wlan", "tailscale", "tun", "wg"))
    }
    up = [name for name, info in wired.items() if info.get("carrier") == "1"]
    if up:
        return Check("Cáp Ethernet", OK, "carrier=1 trên " + ", ".join(up))
    if not wired:
        return Check("Cáp Ethernet", WARN, "không thấy interface có dây nào")
    listing = ", ".join(
        "%s(carrier=%s)" % (n, i.get("carrier")) for n, i in wired.items()
    )
    return Check(
        "Cáp Ethernet", FAIL,
        "Không cổng có dây nào đang có tín hiệu: " + listing,
        "Cắm cáp từ C12 vào cổng Ethernet. Lưu ý cáp Skydroid chỉ có 4 lõi "
        "(TX±/RX±) nên link sẽ ở 100 Mb/s — đó là bình thường. RJ45 KHÔNG cấp "
        "nguồn: camera cần 7.2–72 V qua JST-2P.",
    )


def check_ping(host: str = CAMERA_IP, timeout: float = 1.0) -> Check:
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(max(1, timeout))), host],
            capture_output=True, text=True, timeout=timeout + 3,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return Check("Ping camera", WARN, "không chạy được ping: %s" % exc)

    if result.returncode == 0:
        line = next((l for l in result.stdout.splitlines() if "time=" in l), "")
        return Check("Ping camera", OK, line.strip() or host + " trả lời")
    return Check(
        "Ping camera", FAIL,
        "%s không trả lời ICMP." % host,
        "Kiểm tra cáp và IP host trước. Nếu cả hai đã đúng mà vẫn im, camera có "
        "thể đã bị đổi IP — quét dải bằng: sudo nmap -sn 192.168.144.0/24",
    )


def check_arp(host: str = CAMERA_IP) -> Check:
    """ARP chứng minh tới được tầng 2 kể cả khi ICMP bị chặn."""
    try:
        lines = Path("/proc/net/arp").read_text().splitlines()[1:]
    except OSError:  # pragma: no cover - không phải Linux
        return Check("ARP", SKIP, "/proc/net/arp không đọc được")
    for line in lines:
        parts = line.split()
        if parts and parts[0] == host:
            mac = parts[3] if len(parts) > 3 else "?"
            if mac != "00:00:00:00:00:00":
                return Check("ARP", OK, "%s ở %s" % (host, mac))
    return Check(
        "ARP", WARN,
        "Chưa có ARP entry cho %s — bình thường nếu chưa gửi gói nào." % host,
    )


async def check_tcp_port(host: str, port: int, label: str,
                         timeout: float = 2.0) -> Check:
    name = "RTSP %d — %s" % (port, label)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except asyncio.TimeoutError:
        return Check(name, FAIL, "timeout sau %.0fs" % timeout,
                     "Camera có thể đã ra hình chưa? Luồng RTSP chỉ mở sau khi "
                     "camera khởi động xong.")
    except OSError as exc:
        return Check(name, FAIL, str(exc))
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:  # pragma: no cover
        pass
    return Check(name, OK, "cổng mở")


def check_control_port(port: int = CONTROL_PORT) -> Check:
    """Cổng UDP local — lỗi vận hành số một của hệ này."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as exc:
        return Check(
            "Cổng UDP %d rảnh" % port, FAIL,
            "Không bind được: %s" % exc,
            "Cổng này hay bị app trợ lý hoặc ground station chiếm. Tìm thủ phạm "
            "bằng: ss -lunp | grep %d — rồi tắt nó, hoặc chạy web app với "
            "--local-port 0." % port,
        )
    finally:
        sock.close()
    return Check("Cổng UDP %d rảnh" % port, OK, "bind được")


# --------------------------------------------------------------------------


async def run(host: str = CAMERA_IP, *, control_port: int = CONTROL_PORT,
              check_rtsp: bool = True) -> Preflight:
    """Chạy toàn bộ kiểm tra theo thứ tự từ tầng dưới lên tầng trên."""
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
    """Bản in cho terminal."""
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
