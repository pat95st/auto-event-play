"""Network transports for console drivers: OSC over UDP, raw TCP and a minimal WebSocket client.

Implemented without third-party packages to keep the Raspberry Pi install small.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import struct
from typing import Any, Callable
from urllib.parse import urlparse

log = logging.getLogger("aep.mixer.net")


# =========================================================================== OSC codec


def _osc_pad(b: bytes) -> bytes:
    return b + b"\x00" * (4 - (len(b) % 4))


def _osc_str(s: str) -> bytes:
    return _osc_pad(s.encode("utf-8"))


def osc_encode(address: str, *args: Any) -> bytes:
    tags = ","
    payload = b""
    for a in args:
        if isinstance(a, bool):
            tags += "i"
            payload += struct.pack(">i", int(a))
        elif isinstance(a, int):
            tags += "i"
            payload += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            payload += struct.pack(">f", a)
        elif isinstance(a, str):
            tags += "s"
            payload += _osc_str(a)
        elif isinstance(a, (bytes, bytearray)):
            tags += "b"
            payload += struct.pack(">i", len(a)) + _osc_pad(bytes(a))[: ((len(a) + 3) // 4) * 4]
        else:
            raise TypeError(f"unsupported OSC argument {a!r}")
    return _osc_str(address) + _osc_str(tags) + payload


def _read_str(data: bytes, pos: int) -> tuple[str, int]:
    end = data.index(b"\x00", pos)
    s = data[pos:end].decode("utf-8", "replace")
    pos = end + 1
    pos += (4 - pos % 4) % 4
    return s, pos


def osc_decode(data: bytes) -> tuple[str, list[Any]]:
    address, pos = _read_str(data, 0)
    if pos >= len(data):
        return address, []
    tags, pos = _read_str(data, pos)
    args: list[Any] = []
    for t in tags[1:]:
        if t == "i":
            args.append(struct.unpack(">i", data[pos:pos + 4])[0])
            pos += 4
        elif t == "f":
            args.append(struct.unpack(">f", data[pos:pos + 4])[0])
            pos += 4
        elif t == "s":
            s, pos = _read_str(data, pos)
            args.append(s)
        elif t == "b":
            n = struct.unpack(">i", data[pos:pos + 4])[0]
            pos += 4
            args.append(data[pos:pos + n])
            pos += n + (4 - n % 4) % 4
        elif t == "T":
            args.append(True)
        elif t == "F":
            args.append(False)
    return address, args


class _OscProtocol(asyncio.DatagramProtocol):
    def __init__(self, client: "OscUdpClient"):
        self.client = client

    def datagram_received(self, data: bytes, addr: Any) -> None:
        try:
            address, args = osc_decode(data)
        except Exception:
            return
        self.client._on_message(address, args)

    def error_received(self, exc: Exception) -> None:  # pragma: no cover
        log.debug("OSC socket error: %s", exc)


class OscUdpClient:
    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self._transport: asyncio.DatagramTransport | None = None
        self._waiters: dict[str, list[asyncio.Future[list[Any]]]] = {}
        self.on_message: Callable[[str, list[Any]], None] | None = None

    async def connect(self) -> None:
        if self._transport is not None:
            return
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(lambda: _OscProtocol(self), remote_addr=(self.host, self.port))
        except OSError as exc:
            raise ConnectionError(f"{self.host}:{self.port} nicht erreichbar ({exc})") from exc
        self._transport = transport  # type: ignore[assignment]

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def send(self, address: str, *args: Any) -> None:
        await self.connect()
        assert self._transport is not None
        self._transport.sendto(osc_encode(address, *args))

    async def query(self, address: str, timeout: float = 0.6) -> list[Any] | None:
        await self.connect()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[list[Any]] = loop.create_future()
        self._waiters.setdefault(address, []).append(fut)
        assert self._transport is not None
        self._transport.sendto(osc_encode(address))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            lst = self._waiters.get(address, [])
            if fut in lst:
                lst.remove(fut)

    def _on_message(self, address: str, args: list[Any]) -> None:
        for fut in self._waiters.pop(address, []):
            if not fut.done():
                fut.set_result(args)
        if self.on_message:
            self.on_message(address, args)


# =========================================================================== TCP


class TcpClient:
    """Persistent TCP connection with automatic reconnect on the next write."""

    def __init__(self, host: str, port: int, on_data: Callable[[bytes], None] | None = None):
        self.host, self.port = host, port
        self.on_data = on_data
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, timeout: float = 3.0) -> None:
        if self.connected:
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(f"{self.host}:{self.port} nicht erreichbar ({exc or 'Timeout'})") from exc
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                data = await self._reader.read(1024)
                if not data:
                    break
                if self.on_data:
                    self.on_data(data)
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            self._drop()

    def _drop(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._writer = None
        self._reader = None

    async def send(self, data: bytes) -> None:
        async with self._lock:
            for attempt in (1, 2):
                await self.connect()
                assert self._writer is not None
                try:
                    self._writer.write(data)
                    await self._writer.drain()
                    return
                except (OSError, ConnectionError):
                    self._drop()
                    if attempt == 2:
                        raise

    async def close(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
        self._drop()


# =========================================================================== WebSocket (client, text frames)


class WebSocketClient:
    """Tiny RFC 6455 client sufficient for the Soundcraft Ui protocol."""

    def __init__(self, url: str, on_text: Callable[[str], None] | None = None):
        self.url = url
        self.on_text = on_text
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self, timeout: float = 4.0) -> None:
        if self.connected:
            return
        u = urlparse(self.url)
        host, port = u.hostname or "", u.port or 80
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(f"{host}:{port} nicht erreichbar ({exc or 'Timeout'})") from exc
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(request.encode())
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), timeout)
        if b" 101" not in status:
            writer.close()
            raise ConnectionError(f"WebSocket-Handshake abgelehnt: {status.decode(errors='replace').strip()}")
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout)
            if line in (b"\r\n", b"\n", b""):
                break
        self._reader, self._writer = reader, writer
        self._task = asyncio.create_task(self._read_loop())

    async def _read_exact(self, n: int) -> bytes:
        assert self._reader is not None
        return await self._reader.readexactly(n)

    async def _read_loop(self) -> None:
        fragments: list[bytes] = []
        try:
            while True:
                b1, b2 = await self._read_exact(2)
                opcode = b1 & 0x0F
                fin = bool(b1 & 0x80)
                length = b2 & 0x7F
                if length == 126:
                    length = struct.unpack(">H", await self._read_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", await self._read_exact(8))[0]
                mask = await self._read_exact(4) if b2 & 0x80 else None
                payload = await self._read_exact(length) if length else b""
                if mask:
                    payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    await self._send_frame(0xA, payload)
                    continue
                if opcode in (0x1, 0x0, 0x2):
                    fragments.append(payload)
                    if fin:
                        text = b"".join(fragments).decode("utf-8", "replace")
                        fragments = []
                        if self.on_text:
                            self.on_text(text)
        except (asyncio.IncompleteReadError, OSError, asyncio.CancelledError):
            pass
        finally:
            self._drop()

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        assert self._writer is not None
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self._writer.write(header + mask + masked)
        await self._writer.drain()

    async def send_text(self, text: str) -> None:
        async with self._lock:
            await self.connect()
            try:
                await self._send_frame(0x1, text.encode("utf-8"))
            except (OSError, ConnectionError):
                self._drop()
                await self.connect()
                await self._send_frame(0x1, text.encode("utf-8"))

    def _drop(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._writer = None
        self._reader = None

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        self._drop()
