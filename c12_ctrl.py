#!/usr/bin/env python3
"""
Điều khiển gimbal-camera Skydroid C12 từ Linux (Rubik Pi 3) qua UDP.

Không cần RCSDK Android: toàn bộ giao thức là chuỗi ASCII "#TP..." gửi qua
UDP tới camera. Xem PHAN_TICH_SDK_C12.md để biết bảng lệnh đầy đủ.

  python3 c12_ctrl.py zoom-in
  python3 c12_ctrl.py yaw 20
  python3 c12_ctrl.py goto 30 -45
"""

import argparse
import socket
import sys

DEFAULT_HOST = "192.168.144.108"
DEFAULT_PORT = 5000
DEFAULT_LOCAL_PORT = 5000

# TopCameraCore.genSendControlCmd: tổng byte UTF-8 của thân lệnh, & 0xFF, hex hoa
def checksum(body):
    return body + "%02X" % (sum(body.encode("utf-8")) & 0xFF)


def s16_hex(value):
    """int16 bù 2 -> 4 ký tự hex hoa (SkydroidGimbalControlCore.short2Hex)."""
    return "%04X" % (int(value) & 0xFFFF)


def u8_hex(value):
    return "%02X" % (int(value) & 0xFF)


RESOLUTIONS = {"720p": "0", "1080p": "1", "2k": "2", "4k": "3"}

PALETTES = {
    "WHITE_HOT": "01", "SEPIA": "03", "IRONBOW": "04", "RAINBOW": "05",
    "NIGHT": "06", "AURORA": "07", "RED_HOT": "08", "JUNGLE": "09",
    "MEDICAL": "0A", "BLACK_HOT": "0B", "GLORY_HOT": "0C",
}

AKEYS = {"top": "01", "down": "02", "left": "03", "right": "04", "center": "05"}


class C12:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 local_port=DEFAULT_LOCAL_PORT, timeout=1.0, verbose=True):
        self.addr = (host, port)
        self.verbose = verbose
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("", local_port))
        except OSError as exc:
            # Cổng 5000 hay bị app trợ lý/ground station chiếm
            print("Không bind được cổng %d: %s" % (local_port, exc), file=sys.stderr)
            print("Hãy tắt app trợ lý/ground station rồi thử lại.", file=sys.stderr)
            raise
        self.sock.settimeout(timeout)

    def send(self, body, expect=None):
        """Gửi một lệnh; nếu `expect` là mã lệnh 3 ký tự thì chờ và trả về DATA."""
        frame = checksum(body)
        if self.verbose:
            print("TX: %s" % frame)
        self.sock.sendto((frame + "\r\n").encode("utf-8"), self.addr)
        if expect is None:
            return None
        return self._read_reply(expect)

    def _read_reply(self, cmd3):
        """Đọc gói trả về, tách phần DATA của gói có CMD3 khớp."""
        try:
            data, _ = self.sock.recvfrom(2048)
        except socket.timeout:
            return None
        text = data.decode("utf-8", errors="replace")
        if self.verbose:
            print("RX: %s" % text.strip())
        for line in text.replace("\r", "\n").split("\n"):
            idx = line.find(cmd3)
            if line.startswith("#") and idx > 0:
                # ...<CMD3><DATA><CRC2>  -> bỏ 2 ký tự checksum cuối
                return line[idx + 3:-2]
        return None

    def close(self):
        self.sock.close()

    # ---------------- camera (#TPUD) ----------------
    def zoom_in(self):        return self.send("#TPUD2wDZM0A")
    def zoom_out(self):       return self.send("#TPUD2wDZM0B")
    def zoom_get(self):
        raw = self.send("#TPUD2rDZM00", expect="DZM")
        return int(raw, 16) if raw else None

    def zoom_set(self, level):
        # SDK chặn cứng 0..4; dải thật là 0..67 nên ưu tiên zoom_in/zoom_out
        if not 0 <= level <= 4:
            raise ValueError("setZoomRatios chỉ nhận 0..4 (dùng zoom-in/zoom-out)")
        return self.send("#TPUD2wDZM0%d" % level)

    def take_picture(self):   return self.send("#TPUD2wCAP01")
    def record_start(self):   return self.send("#TPUD2wREC01")
    def record_stop(self):    return self.send("#TPUD2wREC00")
    def record_state(self):
        raw = self.send("#TPUD2rREC00", expect="REC")
        return None if raw is None else raw != "00"

    def set_resolution(self, name):
        return self.send("#TPUD2wVID0" + RESOLUTIONS[name.lower()])

    def get_resolution(self):
        return self.send("#TPUD2rVID00", expect="VID")

    def set_palette(self, name):
        return self.send("#TPUD2wIMG" + PALETTES[name.upper()])

    def get_palette(self):
        return self.send("#TPUD2rIMG00", expect="IMG")

    def get_version(self):
        return self.send("#TPUD2rVER00", expect="VER")

    def get_model(self):
        return self.send("#TPUD2rMOD00", expect="MOD")

    def get_sdcard(self):
        return self.send("#TPUD2rSDC01", expect="SDC")

    def get_video_config(self):
        return self.send("#TPUD2rVOM00", expect="VOM")

    def set_video_config(self, hflip, vflip, frame_rate, gop, bitrate):
        body = ("#TPUDBwVOM"
                + ("1" if hflip else "0")
                + ("1" if vflip else "0")
                + u8_hex(frame_rate)
                + u8_hex(gop)
                + "%04X" % (int(bitrate) & 0xFFFF)
                + "1")
        return self.send(body)

    def reboot(self):         return self.send("#TPUD2wRST00")

    # ---------------- gimbal (#TPUG) ----------------
    def akey(self, name):     return self.send("#TPUG2wPTZ" + AKEYS[name.lower()])

    def yaw_speed(self, dps):
        return self.send("#TPUG2wGSY" + u8_hex(self._speed_raw(dps)))

    def pitch_speed(self, dps):
        return self.send("#TPUG2wGSP" + u8_hex(self._speed_raw(dps)))

    def yaw_pitch_speed(self, yaw_dps, pitch_dps):
        return self.send("#TPUG4wGSM"
                         + u8_hex(self._speed_raw(yaw_dps))
                         + u8_hex(self._speed_raw(pitch_dps)))

    @staticmethod
    def _speed_raw(dps):
        # hằng số từ bytecode: chia 0.5, clamp ±127  ->  ±63.5 °/s
        return max(-127, min(127, int(float(dps) / 0.5)))

    def goto_yaw(self, angle):
        return self.send("#TPUG6wGAY" + s16_hex(self._angle_raw(angle)) + "10")

    def goto_pitch(self, angle):
        return self.send("#TPUG6wGAP" + s16_hex(self._angle_raw(angle)) + "10")

    def goto_yaw_pitch(self, yaw, pitch):
        return self.send("#TPUGCwGAM"
                         + s16_hex(self._angle_raw(yaw)) + "10"
                         + s16_hex(self._angle_raw(pitch)) + "10")

    @staticmethod
    def _angle_raw(angle):
        return max(-9000, min(9000, int(float(angle) * 100)))

    def push_attitude(self, rate_hz):
        return self.send("#TPUG2wGAA" + u8_hex(rate_hz))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--local-port", type=int, default=DEFAULT_LOCAL_PORT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("zoom-in", "zoom-out", "zoom-get", "snap", "rec-start",
                 "rec-stop", "rec-state", "version", "model", "sdcard",
                 "vom-get", "res-get", "palette-get", "center", "reboot"):
        sub.add_parser(name)

    p = sub.add_parser("zoom-set"); p.add_argument("level", type=int)
    p = sub.add_parser("res");      p.add_argument("value", choices=sorted(RESOLUTIONS))
    p = sub.add_parser("palette");  p.add_argument("value", choices=sorted(PALETTES))
    p = sub.add_parser("akey");     p.add_argument("value", choices=sorted(AKEYS))
    p = sub.add_parser("yaw");      p.add_argument("dps", type=float)
    p = sub.add_parser("pitch");    p.add_argument("dps", type=float)
    p = sub.add_parser("goto")
    p.add_argument("yaw", type=float); p.add_argument("pitch", type=float)
    p = sub.add_parser("attitude"); p.add_argument("rate", type=int)

    args = ap.parse_args()
    cam = C12(args.host, args.port, args.local_port)
    try:
        c = args.cmd
        if   c == "zoom-in":     cam.zoom_in()
        elif c == "zoom-out":    cam.zoom_out()
        elif c == "zoom-get":    print("zoom =", cam.zoom_get())
        elif c == "zoom-set":    cam.zoom_set(args.level)
        elif c == "snap":        cam.take_picture()
        elif c == "rec-start":   cam.record_start()
        elif c == "rec-stop":    cam.record_stop()
        elif c == "rec-state":   print("recording =", cam.record_state())
        elif c == "res":         cam.set_resolution(args.value)
        elif c == "res-get":     print("resolution =", cam.get_resolution())
        elif c == "palette":     cam.set_palette(args.value)
        elif c == "palette-get": print("palette =", cam.get_palette())
        elif c == "version":     print("version =", cam.get_version())
        elif c == "model":       print("model =", cam.get_model())
        elif c == "sdcard":      print("sdcard =", cam.get_sdcard())
        elif c == "vom-get":     print("videoconfig =", cam.get_video_config())
        elif c == "akey":        cam.akey(args.value)
        elif c == "center":      cam.akey("center")
        elif c == "yaw":         cam.yaw_speed(args.dps)
        elif c == "pitch":       cam.pitch_speed(args.dps)
        elif c == "goto":        cam.goto_yaw_pitch(args.yaw, args.pitch)
        elif c == "attitude":    cam.push_attitude(args.rate)
        elif c == "reboot":      cam.reboot()
    finally:
        cam.close()


if __name__ == "__main__":
    main()
