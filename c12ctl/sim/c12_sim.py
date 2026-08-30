"""Camera C12 giả lập trên UDP.

Không phải đồ chơi: đây là điều kiện cần để pha 0 tồn tại (máy dev chưa nối được
camera), và về lâu dài là cách duy nhất test các đường an toàn mà không phải rút
cáp thật hàng chục lần.

Simulator mô hình hoá:

* bảng lệnh đọc/ghi theo đúng bytecode,
* gimbal tích phân tốc độ ở 50 Hz, có giới hạn cơ khí,
* luồng đẩy ``GAC`` khi được bật bằng ``GAA``,
* **cả hai hành vi keepalive** — xem ``--hold-speed``.

Điểm cuối cùng quan trọng: hai tài liệu nguồn mâu thuẫn về việc gimbal có tự dừng
khi ngừng nhận gói hay không, và ta không phân xử được từ tài liệu. Nên simulator
chạy được cả hai kiểu, và vòng điều khiển phải đúng trong cả hai.

    python -m c12ctl.sim.c12_sim --port 5000
    python -m c12ctl.sim.c12_sim --chaos-loss 0.3 --hold-speed
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import time

from ..protocol.codec import FrameError, build, parse, split_frames, to_wire
from ..protocol.types import (
    Attitude,
    ANGLE_RAW_LIMIT,
    parse_s16,
    parse_u8,
    raw_to_angle,
    raw_to_speed,
    s16_hex,
    u8_hex,
)

log = logging.getLogger("c12ctl.sim")

TICK = 0.02
"""Chu kỳ mô phỏng, giây. 50 Hz — mịn hơn nhịp điều khiển 20 Hz."""

SPEED_HOLD_TIMEOUT = 0.15
"""Không nhận gói tốc độ mới trong khoảng này thì gimbal tự dừng
(chỉ áp dụng khi ``hold_speed`` là False)."""

YAW_LIMIT_DEG = 90.0
PITCH_LIMIT_DEG = 90.0


class CameraState:
    """Trạng thái camera giả lập."""

    def __init__(self) -> None:
        self.version = "1.9.2"
        self.model = "C12"
        self.hardware_version = "0100"
        self.recording = False
        self.zoom = 0
        self.zoom_max = 67
        self.palette = "01"          # WHITE_HOT
        self.resolution = "01"       # 1080P
        self.sd_total_mb = 30436
        self.sd_free_mb = 18122
        self.photos = 0
        self.photo_mb = 2
        """CAP không có lệnh đọc tương ứng. Bằng chứng gián tiếp duy nhất là
        dung lượng thẻ giảm, nên simulator phải mô hình hoá cả chỗ đó — nếu
        không thì đường xác nhận của pha 3 không test được."""
        # TSM (scene mode) cố ý KHÔNG có mặt: skydroid-c12-protocol.md §5 ngờ đây
        # là tính năng của C13 chứ không phải C12, và registry cũng không có lệnh
        # ghi cho nó. Simulator mô hình hoá kỳ vọng đó bằng cách im lặng — nếu
        # phần cứng thật trả lời TSM, findings sẽ đánh dấu BẤT NGỜ và ta biết
        # ngay là kỳ vọng sai.
        self.thermal = {
            "TAR": 50, "TAS": 30, "TDI": 50, "TGM": 50,
            "TIB": 50, "TIC": 50, "TTR": 50,
        }

        self.yaw = 0.0
        self.pitch = 0.0
        self.roll = 0.0
        self.yaw_speed = 0.0
        self.pitch_speed = 0.0
        self.goto_yaw: float | None = None
        self.goto_pitch: float | None = None
        self.last_speed_cmd = 0.0

        self.gaa_rate = 0
        self.has_video = True
        """GAA chỉ hiệu lực sau khi camera ra hình — mô phỏng luôn ràng buộc đó."""


class C12Simulator:
    def __init__(
        self,
        *,
        hold_speed: bool = False,
        supports_gsm: bool = True,
        chaos_loss: float = 0.0,
        chaos_delay: float = 0.0,
        chaos_garbage: float = 0.0,
        seed: int | None = None,
    ) -> None:
        self.state = CameraState()
        self.hold_speed = hold_speed
        self.supports_gsm = supports_gsm
        self.chaos_loss = chaos_loss
        self.chaos_delay = chaos_delay
        self.chaos_garbage = chaos_garbage
        self.rng = random.Random(seed)

        self.transport: asyncio.DatagramTransport | None = None
        self.rx_count = 0
        self.tx_count = 0
        self.dropped = 0
        self.bad_checksum = 0
        self.unknown: list[str] = []
        self._peers: set[tuple[str, int]] = set()
        self._tasks: list[asyncio.Task] = []
        self._last_gac = 0.0

    # ----------------------------------------------------------------- socket

    async def start(self, host: str = "127.0.0.1", port: int = 5000) -> None:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: _SimProtocol(self), local_addr=(host, port)
        )
        self._tasks.append(asyncio.create_task(self._tick_loop(), name="sim-tick"))
        log.info(
            "C12 giả lập trên %s:%d (hold_speed=%s, GSM=%s)",
            host, port, self.hold_speed, self.supports_gsm,
        )

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    @property
    def port(self) -> int:
        return self.transport.get_extra_info("sockname")[1]

    # ------------------------------------------------------------------ nhận

    def on_datagram(self, data: bytes, addr) -> None:
        self._peers.add(addr)
        text = data.decode("utf-8", errors="replace")

        if self.chaos_loss and self.rng.random() < self.chaos_loss:
            self.dropped += 1
            log.debug("chaos: bỏ gói đến %r", text.strip())
            return

        frames = split_frames(text)
        if not frames:
            # Phân biệt "rác thật" với "checksum sai" để chaos test đọc được.
            try:
                parse(text, verify=False)
                self.bad_checksum += 1
            except FrameError:
                pass
            log.debug("gói không tách được khung: %r", text.strip())
            return

        for frame in frames:
            self.rx_count += 1
            reply = self.handle(frame)
            if reply is not None:
                self._send(reply, addr)

    def handle(self, frame) -> str | None:
        """Xử lý một khung, trả về khung phản hồi hoặc ``None``."""
        st = self.state
        cmd, data, rw = frame.cmd3, frame.data, frame.rw

        if rw == "r":
            return self._handle_read(cmd, data)

        # ---- camera ----
        if cmd == "CAP":
            if st.sd_total_mb:
                st.photos += 1
                st.sd_free_mb = max(0, st.sd_free_mb - st.photo_mb)
            log.info("sim: chụp ảnh (%d tấm, còn %d MB)", st.photos, st.sd_free_mb)
            return None
        if cmd == "REC":
            st.recording = data == "01" if data in ("00", "01") else not st.recording
            log.info("sim: ghi hình = %s", st.recording)
            return None
        if cmd == "DZM":
            if data == "0A":
                st.zoom = min(st.zoom_max, st.zoom + 1)
            elif data == "0B":
                st.zoom = max(0, st.zoom - 1)
            else:
                st.zoom = min(st.zoom_max, parse_u8(data))
            return None
        if cmd == "IMG":
            st.palette = data.upper()
            log.info("sim: palette = %s", st.palette)
            return None
        if cmd == "VID":
            st.resolution = data
            return None
        if cmd in st.thermal:
            st.thermal[cmd] = parse_u8(data)
            return None

        # ---- gimbal ----
        if cmd == "GSY":
            st.yaw_speed = raw_to_speed(_s8(data))
            st.goto_yaw = None
            st.last_speed_cmd = time.monotonic()
            return None
        if cmd == "GSP":
            st.pitch_speed = raw_to_speed(_s8(data))
            st.goto_pitch = None
            st.last_speed_cmd = time.monotonic()
            return None
        if cmd == "GSM":
            if not self.supports_gsm:
                log.debug("sim: GSM không hỗ trợ (firmware gimbal < 0.5)")
                return None
            st.yaw_speed = raw_to_speed(_s8(data[0:2]))
            st.pitch_speed = raw_to_speed(_s8(data[2:4]))
            st.goto_yaw = st.goto_pitch = None
            st.last_speed_cmd = time.monotonic()
            return None
        if cmd == "GAY":
            st.goto_yaw = raw_to_angle(parse_s16(data[0:4]))
            st.yaw_speed = 0.0
            return None
        if cmd == "GAP":
            st.goto_pitch = raw_to_angle(parse_s16(data[0:4]))
            st.pitch_speed = 0.0
            return None
        if cmd == "GAM":
            st.goto_yaw = raw_to_angle(parse_s16(data[0:4]))
            st.goto_pitch = raw_to_angle(parse_s16(data[6:10]))
            st.yaw_speed = st.pitch_speed = 0.0
            return None
        if cmd == "PTZ":
            self._handle_ptz(data)
            return None
        if cmd == "GAA":
            rate = parse_u8(data)
            if not st.has_video and rate:
                log.info("sim: bỏ qua GAA — camera chưa ra hình")
                return None
            st.gaa_rate = rate
            log.info("sim: đẩy tư thế %d Hz", rate)
            return None

        self.unknown.append(frame.raw)
        log.info("sim: lệnh không hỗ trợ %s — im lặng (đúng như phần cứng thật)", cmd)
        return None

    def _handle_ptz(self, data: str) -> None:
        st = self.state
        if data == "01":
            st.goto_pitch = PITCH_LIMIT_DEG
        elif data == "02":
            st.goto_pitch = -PITCH_LIMIT_DEG
        elif data == "03":
            st.goto_yaw = -YAW_LIMIT_DEG
        elif data == "04":
            st.goto_yaw = YAW_LIMIT_DEG
        elif data == "05":
            st.goto_yaw = st.goto_pitch = 0.0
        else:
            log.warning("sim: PTZ %s — dải nguy hiểm, phần cứng thật có thể "
                        "khởi động hiệu chuẩn", data)
            return
        st.yaw_speed = st.pitch_speed = 0.0

    def _handle_read(self, cmd: str, data: str) -> str | None:
        st = self.state
        table = {
            "VER": st.version.replace(".", "")[:4].ljust(4, "0"),
            "HWV": st.hardware_version,
            "MOD": "0C",
            "REC": "01" if st.recording else "00",
            "IMG": st.palette,
            "VID": st.resolution,
            "DZM": u8_hex(st.zoom),
            "EXT": "0110",
        }
        if cmd in st.thermal:
            table[cmd] = u8_hex(st.thermal[cmd])
        if cmd == "SDC":
            # 6 hex mỗi giá trị = 12 ký tự. KHÔNG dùng 8+8: trường length chỉ có
            # 1 ký tự hex nên data tối đa là 15 ký tự — ràng buộc này loại luôn
            # mọi format 2×32-bit. Format thật chưa xác minh; pha 1 ghi chuỗi
            # thô để chốt.
            payload = "%06X%06X" % (
                min(st.sd_total_mb, 0xFFFFFF),
                min(st.sd_free_mb, 0xFFFFFF),
            )
            return self._reply("SDC", payload)
        if cmd in table:
            return self._reply(cmd, table[cmd])

        # Lệnh camera không hỗ trợ thì IM LẶNG. Chính sự im lặng đó là dữ liệu
        # mà trang Diagnostics của pha 1 cần thu thập.
        self.unknown.append(cmd)
        log.debug("sim: đọc %s không hỗ trợ — im lặng", cmd)
        return None

    def _reply(self, cmd3: str, data: str) -> str:
        """Camera trả về với src/dest đảo: #TPDU..."""
        return build("U", "r", cmd3, data, src="D")

    # -------------------------------------------------------------- mô phỏng

    async def _tick_loop(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(TICK)
            now = time.monotonic()
            dt = now - last
            last = now
            self._integrate(dt, now)
            self._maybe_push_attitude(now)

    def _integrate(self, dt: float, now: float) -> None:
        st = self.state

        # Hành vi keepalive — điểm hai tài liệu mâu thuẫn.
        if not self.hold_speed and (st.yaw_speed or st.pitch_speed):
            if now - st.last_speed_cmd > SPEED_HOLD_TIMEOUT:
                st.yaw_speed = st.pitch_speed = 0.0
                log.debug("sim: hết keepalive, gimbal tự dừng")

        if st.goto_yaw is not None:
            st.yaw = _approach(st.yaw, st.goto_yaw, 60.0 * dt)
            if abs(st.yaw - st.goto_yaw) < 1e-6:
                st.goto_yaw = None
        else:
            st.yaw += st.yaw_speed * dt

        if st.goto_pitch is not None:
            st.pitch = _approach(st.pitch, st.goto_pitch, 60.0 * dt)
            if abs(st.pitch - st.goto_pitch) < 1e-6:
                st.goto_pitch = None
        else:
            st.pitch += st.pitch_speed * dt

        st.yaw = max(-YAW_LIMIT_DEG, min(YAW_LIMIT_DEG, st.yaw))
        st.pitch = max(-PITCH_LIMIT_DEG, min(PITCH_LIMIT_DEG, st.pitch))

    def _maybe_push_attitude(self, now: float) -> None:
        st = self.state
        if not st.gaa_rate or not self._peers:
            return
        if now - self._last_gac < 1.0 / st.gaa_rate:
            return
        self._last_gac = now
        payload = (
            s16_hex(int(round(st.yaw * 100)))
            + s16_hex(int(round(st.pitch * 100)))
            + s16_hex(int(round(st.roll * 100)))
        )
        frame = build("U", "w", "GAC", payload, src="G")
        for peer in self._peers:
            self._send(frame, peer)

    @property
    def attitude(self) -> Attitude:
        return Attitude(self.state.yaw, self.state.pitch, self.state.roll)

    # -------------------------------------------------------------------- gửi

    def _send(self, frame: str, addr) -> None:
        if self.transport is None:
            return
        if self.chaos_loss and self.rng.random() < self.chaos_loss:
            self.dropped += 1
            return
        payload = to_wire(frame)
        if self.chaos_garbage and self.rng.random() < self.chaos_garbage:
            payload = b"\x00\xffrac" + payload
        if self.chaos_delay:
            delay = self.rng.uniform(0, self.chaos_delay)
            asyncio.get_running_loop().call_later(
                delay, self._raw_send, payload, addr
            )
            return
        self._raw_send(payload, addr)

    def _raw_send(self, payload: bytes, addr) -> None:
        if self.transport is not None:
            self.tx_count += 1
            self.transport.sendto(payload, addr)


class _SimProtocol(asyncio.DatagramProtocol):
    def __init__(self, sim: C12Simulator) -> None:
        self._sim = sim

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: D102
        self._sim.on_datagram(data, addr)


def _s8(text: str) -> int:
    """2 ký tự hex → int8 có dấu."""
    raw = int(text, 16)
    return raw - 0x100 if raw >= 0x80 else raw


def _approach(current: float, target: float, step: float) -> float:
    if abs(target - current) <= step:
        return target
    return current + step * (1 if target > current else -1)


# --------------------------------------------------------------------------


async def _main(args) -> None:
    sim = C12Simulator(
        hold_speed=args.hold_speed,
        supports_gsm=not args.no_gsm,
        chaos_loss=args.chaos_loss,
        chaos_delay=args.chaos_delay,
        chaos_garbage=args.chaos_garbage,
        seed=args.seed,
    )
    await sim.start(args.host, args.port)
    try:
        while True:
            await asyncio.sleep(5)
            log.info(
                "sim: rx=%d tx=%d drop=%d yaw=%.1f pitch=%.1f zoom=%d rec=%s",
                sim.rx_count, sim.tx_count, sim.dropped,
                sim.state.yaw, sim.state.pitch, sim.state.zoom, sim.state.recording,
            )
    except asyncio.CancelledError:
        pass
    finally:
        await sim.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--hold-speed", action="store_true",
                    help="gimbal giữ lệnh tốc độ tới khi có lệnh mới "
                         "(mặc định: tự dừng sau %.0f ms không nhận gói)"
                         % (SPEED_HOLD_TIMEOUT * 1000))
    ap.add_argument("--no-gsm", action="store_true",
                    help="giả lập firmware gimbal < 0.5, không hỗ trợ GSM")
    ap.add_argument("--chaos-loss", type=float, default=0.0, metavar="P",
                    help="xác suất bỏ mỗi gói, 0..1")
    ap.add_argument("--chaos-delay", type=float, default=0.0, metavar="SEC",
                    help="trễ phản hồi ngẫu nhiên tới SEC giây")
    ap.add_argument("--chaos-garbage", type=float, default=0.0, metavar="P",
                    help="xác suất chèn byte rác trước phản hồi")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
