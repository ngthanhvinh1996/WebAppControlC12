"""A simulated C12 camera over UDP.

Not a toy: it is the precondition for phase 0 existing at all (the dev machine
had no camera to connect), and in the long run it is the only way to test the
safety paths without physically unplugging a cable dozens of times.

The simulator models:

* the read/write command table exactly as the bytecode describes it,
* a gimbal integrating speed at 50 Hz, with mechanical limits,
* the ``GAC`` push stream once ``GAA`` enables it,
* **both keepalive behaviours** — see ``--hold-speed``.

That last point matters: the two source documents disagree on whether the gimbal
stops on its own when packets stop arriving, and the documents cannot settle it.
So the simulator can run either way, and the control loop must be correct in both.

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
"""Simulation period, in seconds. 50 Hz — finer than the 20 Hz control rate."""

SPEED_HOLD_TIMEOUT = 0.15
"""With no new speed packet within this window the gimbal stops on its own
(only when ``hold_speed`` is False)."""

YAW_LIMIT_DEG = 90.0
PITCH_LIMIT_DEG = 90.0


class CameraState:
    """State of the simulated camera."""

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
        """CAP has no corresponding read command. The only indirect evidence is
        the card's free space dropping, so the simulator has to model that too —
        otherwise phase 3's verification path could not be tested."""
        # TSM (scene mode) is deliberately absent: skydroid-c12-protocol.md §5
        # suspects it is a C13 feature rather than a C12 one, and the registry
        # has no write command for it either. The simulator models that
        # expectation by staying silent — if real hardware does answer TSM, the
        # findings report flags it as a SURPRISE and we learn the expectation
        # was wrong.
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
        """GAA only takes effect once the camera has video — the simulator models
        that constraint too."""


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
            "simulated C12 on %s:%d (hold_speed=%s, GSM=%s)",
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

    # -------------------------------------------------------------- receiving

    def on_datagram(self, data: bytes, addr) -> None:
        self._peers.add(addr)
        text = data.decode("utf-8", errors="replace")

        if self.chaos_loss and self.rng.random() < self.chaos_loss:
            self.dropped += 1
            log.debug("chaos: dropped inbound %r", text.strip())
            return

        frames = split_frames(text)
        if not frames:
            # Tell real garbage apart from a bad checksum so the chaos tests can
            # read the difference.
            try:
                parse(text, verify=False)
                self.bad_checksum += 1
            except FrameError:
                pass
            log.debug("no frame could be split out of: %r", text.strip())
            return

        for frame in frames:
            self.rx_count += 1
            reply = self.handle(frame)
            if reply is not None:
                self._send(reply, addr)

    def handle(self, frame) -> str | None:
        """Handle one frame, returning a reply frame or ``None``."""
        st = self.state
        cmd, data, rw = frame.cmd3, frame.data, frame.rw

        if rw == "r":
            return self._handle_read(cmd, data)

        # ---- camera ----
        if cmd == "CAP":
            if st.sd_total_mb:
                st.photos += 1
                st.sd_free_mb = max(0, st.sd_free_mb - st.photo_mb)
            log.info("sim: photo taken (%d total, %d MB free)", st.photos, st.sd_free_mb)
            return None
        if cmd == "REC":
            st.recording = data == "01" if data in ("00", "01") else not st.recording
            log.info("sim: recording = %s", st.recording)
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
                log.debug("sim: GSM unsupported (gimbal firmware < 0.5)")
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
                log.info("sim: ignoring GAA — the camera has no video yet")
                return None
            st.gaa_rate = rate
            log.info("sim: pushing attitude at %d Hz", rate)
            return None

        self.unknown.append(frame.raw)
        log.info("sim: unsupported command %s — staying silent (like real hardware)",
                 cmd)
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
            log.warning("sim: PTZ %s — dangerous range, real hardware might start "
                        "a calibration", data)
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
            # 6 hex characters per value = 12 total. NOT 8+8: the length field is
            # a single hex character so data is at most 15 characters — that
            # constraint rules out every 2×32-bit format. The real format is
            # unverified; phase 1 records the raw string to settle it.
            payload = "%06X%06X" % (
                min(st.sd_total_mb, 0xFFFFFF),
                min(st.sd_free_mb, 0xFFFFFF),
            )
            return self._reply("SDC", payload)
        if cmd in table:
            return self._reply(cmd, table[cmd])

        # An unsupported camera command stays SILENT. That silence is exactly the
        # data phase 1's Diagnostics page needs to collect.
        self.unknown.append(cmd)
        log.debug("sim: read %s unsupported — staying silent", cmd)
        return None

    def _reply(self, cmd3: str, data: str) -> str:
        """The camera replies with src/dest swapped: #TPDU..."""
        return build("U", "r", cmd3, data, src="D")

    # ---------------------------------------------------------------- physics

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

        # Keepalive behaviour — the point where the two documents disagree.
        if not self.hold_speed and (st.yaw_speed or st.pitch_speed):
            if now - st.last_speed_cmd > SPEED_HOLD_TIMEOUT:
                st.yaw_speed = st.pitch_speed = 0.0
                log.debug("sim: keepalive expired, gimbal stopped itself")

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

    # ----------------------------------------------------------------- sending

    def _send(self, frame: str, addr) -> None:
        if self.transport is None:
            return
        if self.chaos_loss and self.rng.random() < self.chaos_loss:
            self.dropped += 1
            return
        payload = to_wire(frame)
        if self.chaos_garbage and self.rng.random() < self.chaos_garbage:
            payload = b"\x00\xffjunk" + payload
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
    """2 hex characters → signed int8."""
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
                    help="the gimbal holds the last speed command until a new "
                         "one arrives (default: stops on its own after %.0f ms "
                         "with no packet)" % (SPEED_HOLD_TIMEOUT * 1000))
    ap.add_argument("--no-gsm", action="store_true",
                    help="simulate gimbal firmware < 0.5, which lacks GSM")
    ap.add_argument("--chaos-loss", type=float, default=0.0, metavar="P",
                    help="probability of dropping each packet, 0..1")
    ap.add_argument("--chaos-delay", type=float, default=0.0, metavar="SEC",
                    help="delay replies randomly by up to SEC seconds")
    ap.add_argument("--chaos-garbage", type=float, default=0.0, metavar="P",
                    help="probability of prefixing a reply with junk bytes")
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
