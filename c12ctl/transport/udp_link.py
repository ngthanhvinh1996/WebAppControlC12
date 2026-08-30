"""Kênh UDP tới C12: một socket, một writer, RX demux theo command word.

Camera là thiết bị nhúng nhỏ; bắn song song nhiều lệnh dễ làm nó bỏ gói. Mọi lệnh
xếp hàng qua **một** task ghi duy nhất với khoảng cách tối thiểu giữa hai gói.

Task đọc tách gói, đối chiếu ``CMD3`` rồi phân phối về hai hướng:

* :class:`asyncio.Future` đang chờ — lệnh đọc,
* bus telemetry — gói ``GAC`` camera tự đẩy.

Lệnh chuyển động gimbal đi **hàng ưu tiên**, không xếp sau lệnh đọc: ở nhịp 20 Hz
một gói tốc độ bị kẹt sau một lệnh đọc đang timeout là gimbal giật.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from ..protocol.codec import Frame, FrameError, parse, split_frames, to_wire
from ..protocol.registry import Command, CommandNotAllowed, COMMANDS

log = logging.getLogger("c12ctl.udp")

DEFAULT_HOST = "192.168.144.108"
DEFAULT_PORT = 5000
DEFAULT_LOCAL_PORT = 5000

MIN_TX_GAP = 0.015
"""Khoảng cách tối thiểu giữa hai gói, giây."""


class PortBusyError(RuntimeError):
    """Không bind được cổng local. Lỗi vận hành số một của hệ này."""


@dataclass
class LinkStats:
    tx: int = 0
    rx: int = 0
    rx_bad: int = 0
    timeouts: int = 0
    last_rx_at: float | None = None

    def as_dict(self) -> dict:
        return {
            "tx": self.tx,
            "rx": self.rx,
            "rx_bad": self.rx_bad,
            "timeouts": self.timeouts,
            "last_rx_at": self.last_rx_at,
        }


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, link: "UdpLink") -> None:
        self._link = link

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: D102
        self._link._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:  # noqa: D102
        log.warning("lỗi socket UDP: %s", exc)


class UdpLink:
    """Kênh lệnh tới camera.

    :param dry_run: in gói ra log thay vì gửi. Không mở socket, không cần phần cứng.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        local_port: int = DEFAULT_LOCAL_PORT,
        *,
        dry_run: bool = False,
        log_path: str | os.PathLike | None = None,
        min_tx_gap: float = MIN_TX_GAP,
    ) -> None:
        self.addr = (host, port)
        self.local_port = local_port
        self.dry_run = dry_run
        self.min_tx_gap = min_tx_gap
        self.stats = LinkStats()

        self._transport: asyncio.DatagramTransport | None = None
        self._normal: asyncio.Queue[str] = asyncio.Queue()
        self._priority: asyncio.Queue[str] = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None
        self._pending: dict[str, list[asyncio.Future]] = {}
        self._subscribers: list[Callable[[Frame], None | Awaitable[None]]] = []
        self._last_tx = 0.0
        self._buffer = ""

        self._log_file = None
        if log_path is not None:
            path = Path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = path.open("a", encoding="utf-8")

    # ---------------------------------------------------------------- vòng đời

    async def start(self) -> None:
        if not self.dry_run:
            loop = asyncio.get_running_loop()
            try:
                self._transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Protocol(self),
                    local_addr=("0.0.0.0", self.local_port),
                    reuse_port=False,
                )
            except OSError as exc:
                raise PortBusyError(
                    "Không bind được cổng UDP %d: %s.\n"
                    "Cổng này hay bị app trợ lý hoặc ground station chiếm — "
                    "tắt chúng rồi thử lại, hoặc chạy với --local-port 0."
                    % (self.local_port, exc)
                ) from exc
        self._writer_task = asyncio.create_task(self._writer(), name="c12-tx")
        log.info(
            "kênh UDP %s → %s:%d%s",
            "DRY-RUN" if self.dry_run else "mở",
            self.addr[0],
            self.addr[1],
            "" if self.dry_run else " (local :%d)" % self.local_port,
        )

    async def close(self) -> None:
        if self._writer_task is not None:
            self._writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer_task
            self._writer_task = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        for futures in self._pending.values():
            for fut in futures:
                if not fut.done():
                    fut.cancel()
        self._pending.clear()
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    async def __aenter__(self) -> "UdpLink":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------------- gửi

    def send_frame(self, frame: str, *, priority: bool = False) -> None:
        """Xếp một khung đã dựng sẵn vào hàng đợi. Không chờ."""
        (self._priority if priority else self._normal).put_nowait(frame)

    def send(self, command: Command | str, *args, priority: bool = False, **kwargs) -> str:
        """Xếp một lệnh của registry. Trả về khung đã dựng."""
        cmd = self._resolve(command)
        frame = cmd.frame(*args, **kwargs)
        self.send_frame(frame, priority=priority)
        return frame

    async def request(
        self, command: Command | str, *args, timeout: float | None = None, **kwargs
    ):
        """Gửi một lệnh đọc rồi chờ phản hồi.

        Trả về giá trị đã giải mã nếu lệnh có ``decode``, ngược lại trả chuỗi data
        thô. Trả ``None`` khi timeout — lệnh camera không hỗ trợ sẽ **im lặng**,
        và im lặng chính là câu trả lời mà pha 1 cần ghi lại.

        :param timeout: ghi đè timeout khai báo trong registry. Trang Diagnostics
            quét hàng chục lệnh và phần lớn sẽ im lặng, nên nó cần rút ngắn
            khoảng chờ thay vì cộng dồn ``len(COMMANDS) × 1 s``.
        """
        cmd = self._resolve(command)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending.setdefault(cmd.cmd3, []).append(fut)

        self.send(cmd, *args, **kwargs)
        try:
            frame: Frame = await asyncio.wait_for(
                fut, timeout=cmd.timeout if timeout is None else timeout
            )
        except asyncio.TimeoutError:
            self.stats.timeouts += 1
            return None
        finally:
            waiters = self._pending.get(cmd.cmd3)
            if waiters and fut in waiters:
                waiters.remove(fut)

        if cmd.decode is not None:
            try:
                return cmd.decode(frame.data)
            except Exception:  # pragma: no cover - dữ liệu lạ từ phần cứng
                log.warning("không giải mã được %s data=%r", cmd.cmd3, frame.data)
                return frame.data
        return frame.data

    @staticmethod
    def _resolve(command: Command | str) -> Command:
        if isinstance(command, Command):
            # Vẫn phải nằm trong registry: allowlist không có ngoại lệ.
            if COMMANDS.get(command.name) is not command:
                raise CommandNotAllowed(
                    "%r không phải mục của registry" % command.name
                )
            return command
        return COMMANDS[command] if command in COMMANDS else _reject(command)

    # ------------------------------------------------------------------ nhận

    def subscribe(self, callback: Callable[[Frame], None | Awaitable[None]]) -> None:
        """Đăng ký nhận mọi khung đến — dùng cho bus telemetry và log UI."""
        self._subscribers.append(callback)

    def unsubscribe(self, callback) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(callback)

    def feed(self, data: bytes) -> list[Frame]:
        """Nạp byte thô vào bộ tách khung. Tách riêng để test không cần socket."""
        self._buffer += data.decode("utf-8", errors="replace")
        frames = split_frames(self._buffer)
        if frames:
            last = self._buffer.rfind(frames[-1].raw)
            self._buffer = self._buffer[last + len(frames[-1].raw) :]
        elif len(self._buffer) > 4096:
            # Không tìm thấy khung nào và buffer đang phình — vứt, giữ phần đuôi
            # phòng khi một khung bị cắt ngang giữa hai gói.
            self._buffer = self._buffer[-64:]
        return frames

    def _on_datagram(self, data: bytes, addr) -> None:
        raw = data.decode("utf-8", errors="replace")
        frames = self.feed(data)
        if not frames:
            self.stats.rx_bad += 1
            self._journal("rx-bad", raw)
            return
        for frame in frames:
            self.stats.rx += 1
            self.stats.last_rx_at = time.monotonic()
            self._journal("rx", frame.raw, frame)
            self._dispatch(frame)

    def _dispatch(self, frame: Frame) -> None:
        waiters = self._pending.get(frame.cmd3)
        if waiters:
            for fut in list(waiters):
                if not fut.done():
                    fut.set_result(frame)
                    waiters.remove(fut)
                    break
        for callback in list(self._subscribers):
            try:
                result = callback(frame)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:  # pragma: no cover - subscriber lỗi không được giết link
                log.exception("subscriber lỗi khi xử lý %s", frame.cmd3)

    # ----------------------------------------------------------------- writer

    async def _writer(self) -> None:
        while True:
            frame = await self._next_frame()
            gap = self.min_tx_gap - (time.monotonic() - self._last_tx)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last_tx = time.monotonic()
            self.stats.tx += 1
            self._journal("tx", frame)
            if self.dry_run:
                log.info("DRY-RUN TX %s", frame)
            elif self._transport is not None:
                self._transport.sendto(to_wire(frame), self.addr)

    async def _next_frame(self) -> str:
        """Ưu tiên hàng gimbal; chỉ lấy hàng thường khi hàng ưu tiên rỗng."""
        if not self._priority.empty():
            return self._priority.get_nowait()
        get_priority = asyncio.ensure_future(self._priority.get())
        get_normal = asyncio.ensure_future(self._normal.get())
        try:
            done, _ = await asyncio.wait(
                {get_priority, get_normal}, return_when=asyncio.FIRST_COMPLETED
            )
            if get_priority in done:
                if get_normal in done:
                    # Cả hai cùng xong: giữ lại gói thường cho lượt sau, đừng vứt.
                    self._normal.put_nowait(get_normal.result())
                return get_priority.result()
            return get_normal.result()
        finally:
            for task in (get_priority, get_normal):
                if not task.done():
                    task.cancel()
                elif not task.cancelled() and task.exception() is None:
                    pass

    # ------------------------------------------------------------------- log

    def _journal(self, direction: str, raw: str, frame: Frame | None = None) -> None:
        if self._log_file is None:
            return
        record = {"t": time.time(), "mono": time.monotonic(), "dir": direction, "raw": raw}
        if frame is not None:
            record |= {"cmd3": frame.cmd3, "data": frame.data,
                       "src": frame.src, "dest": frame.dest, "rw": frame.rw}
        self._log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._log_file.flush()


def _reject(name: str) -> Command:
    raise CommandNotAllowed(
        "%r không có trong registry. Allowlist chứ không phải blocklist: "
        "lệnh chưa khai báo thì không gửi được." % name
    )
