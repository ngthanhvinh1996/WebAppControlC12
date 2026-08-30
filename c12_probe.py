#!/usr/bin/env python3
"""
c12_probe.py - do lenh Topotek UDP cua Skydroid C12
Usage:
    ./c12_probe.py read              # enumerate tat ca lenh read (an toan)
    ./c12_probe.py sweep TAR         # quet data 00..0A cho 1 command word
    ./c12_probe.py send '#TPUG2wPTZ05'   # gui 1 lenh tuy y (tu them checksum)
"""
import socket, sys, time

IP, PORT = "192.168.144.108", 5000

# tat ca command word doc duoc tu strings apk, gui toi dich D (camera)
READ_CMDS_D = ["VER", "HWV", "MOD", "SDC", "REC", "IMG", "VID", "DZM",
               "IQE", "VOM", "GTW", "IPV", "EXT", "SLR",
               "TAR", "TAS", "TDI", "TGM", "TIB", "TIC", "TSM", "TTR"]


def frame(body: str) -> str:
    """Them checksum: tong byte ASCII & 0xFF, 2 ky tu hex hoa."""
    return body + f"{sum(body.encode()) & 0xFF:02X}"


def send(body: str, timeout: float = 0.6):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    s.bind(("", 0))
    pkt = frame(body)
    s.sendto(pkt.encode(), (IP, PORT))
    try:
        data, _ = s.recvfrom(512)
        return pkt, data.decode(errors="replace")
    except socket.timeout:
        return pkt, None
    finally:
        s.close()


def do_read():
    print(f"{'TX':<20} {'RX'}")
    print("-" * 60)
    for c in READ_CMDS_D:
        tx, rx = send(f"#TPUD2r{c}00")
        print(f"{tx:<20} {rx if rx else '(no reply)'}")
        time.sleep(0.15)


def do_sweep(cmd: str):
    """Quet data 00..0A. Nhin man hinh ffplay stream=2 de thay thay doi."""
    print(f"Sweep {cmd}: xem cua so ffplay rtsp://{IP}:555/stream=2")
    for v in range(0x0B):
        tx, rx = send(f"#TPUD2w{cmd}{v:02X}")
        print(f"  {tx:<20} -> {rx if rx else '(no reply)'}")
        input("    Enter de sang gia tri tiep theo...")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
    elif sys.argv[1] == "read":
        do_read()
    elif sys.argv[1] == "sweep":
        do_sweep(sys.argv[2])
    elif sys.argv[1] == "send":
        print(send(sys.argv[2]))
