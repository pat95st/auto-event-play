"""Mackie DL series (DL16S, DL32S, DL32R) – experimental.

Mackie does not publish the protocol of the Master Fader app. Framing,
handshake and parameter addresses follow Jon Skeet's reverse-engineered
DigiMixer project (github.com/jskeet/DemoCode, DigiMixer/Protocols/mackie.md
and DigiMixer.Mackie, Apache License 2.0).

Transport: one TCP connection to port 50001. Message::

    ab  seq  chunks(2)  type  subtype  header-checksum(2)  [body (4-byte chunks)  body-checksum(4)]

header checksum = 0xffff − Σ header bytes, body checksum = 0xffffffff − Σ body bytes.
Types: 0 request, 1 response, 5 error, 8 broadcast. Every request is answered
with a response carrying the same sequence number. Channel values (subtype
0x13): chunk 0 = start address, chunk 1 = count<<16 | type 5<<8, then one
chunk per value (faders: float32 dB, mutes: int 0/1). Older iPad dock models
(DL1608, DL806) use a different protocol and are not supported.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass

from ...domain.levels import MIN_DB, is_off
from ...domain.model import MixerConfig, Target, TargetKind
from .base import KindSpec, MixerDriver, MixerError, OptionSpec, register_driver
from .transports import TcpClient

log = logging.getLogger("aep.mixer.mackie")
K = TargetKind

T_REQUEST, T_RESPONSE, T_ERROR, T_BROADCAST = 0, 1, 5, 8
C_KEEPALIVE, C_HANDSHAKE, C_CHANNEL_INFO, C_VALUES = 0x01, 0x03, 0x06, 0x13


@dataclass(frozen=True)
class Profile:
    inputs: int
    returns: int
    fx: int
    aux: int
    input1: int
    input_size: int
    input_aux1: int
    input_fx1: int
    return1: int
    return_size: int
    fxin1: int
    fxin_size: int
    aux1: int
    aux_size: int
    fxout1: int
    fxout_size: int
    main_fader: int
    main_mute: int


DL16S = Profile(inputs=16, returns=2, fx=4, aux=6, input1=1, input_size=100, input_aux1=50, input_fx1=68,
                return1=1601, return_size=88, fxin1=1949, fxin_size=66, aux1=2606, aux_size=90,
                fxout1=1777, fxout_size=12, main_fader=2519, main_mute=2520)
DL32 = Profile(inputs=32, returns=4, fx=4, aux=14, input1=41, input_size=132, input_aux1=58, input_fx1=100,
               return1=4265, return_size=120, fxin1=4965, fxin_size=98, aux1=6130, aux_size=120,
               fxout1=4745, fxout_size=24, main_fader=5995, main_mute=5996)
PROFILES = {"dl16s": DL16S, "dl32": DL32}


def encode_message(seq: int, mtype: int, subtype: int, body: bytes = b"") -> bytes:
    if len(body) % 4:
        raise ValueError("body must be a multiple of 4 bytes")
    header = bytes([0xAB, seq & 0xFF]) + struct.pack(">H", len(body) // 4) + bytes([mtype, subtype])
    out = header + struct.pack(">H", (0xFFFF - sum(header)) & 0xFFFF)
    if body:
        out += body + struct.pack(">I", (0xFFFFFFFF - sum(body)) & 0xFFFFFFFF)
    return out


def decode_messages(buffer: bytes) -> tuple[list[tuple[int, int, int, bytes]], bytes]:
    """Split a byte stream into (seq, type, subtype, body) messages; returns the remainder."""
    out = []
    while len(buffer) >= 8:
        if buffer[0] != 0xAB:
            idx = buffer.find(b"\xab", 1)
            buffer = buffer[idx:] if idx >= 0 else b""
            continue
        chunks = struct.unpack(">H", buffer[2:4])[0]
        total = 8 if chunks == 0 else 12 + chunks * 4
        if len(buffer) < total:
            break
        body = buffer[8:8 + chunks * 4] if chunks else b""
        out.append((buffer[1], buffer[4], buffer[5], body))
        buffer = buffer[total:]
    return out, buffer


@register_driver
class MackieDLDriver(MixerDriver):
    type_id = "mackie_dl"
    display_name = "Mackie DL16S / DL32S / DL32R (experimental)"
    family = "Mackie"
    default_port = 50001
    transport = "TCP"
    can_query = True
    option_specs = [
        OptionSpec("model", "Model", "select", "auto",
                   choices=[("auto", "Detect automatically"), ("dl16s", "DL16S"), ("dl32", "DL32S / DL32R")]),
    ]
    notes = ("Same protocol as the Master Fader app (TCP 50001), based on the reverse engineering of the DigiMixer project. "
             "DL1608/DL806 are not supported. Please test before use.")

    @classmethod
    def profile_for(cls, config: MixerConfig | None, detected: Profile | None = None) -> Profile:
        key = (config.options.get("model") if config else None) or "auto"
        if key in PROFILES:
            return PROFILES[key]
        return detected or DL32

    @classmethod
    def kinds_for(cls, config: MixerConfig | None) -> list[KindSpec]:
        p = cls.profile_for(config)
        return [
            KindSpec(K.CHANNEL, "Channel", p.inputs),
            KindSpec(K.STEREO_IN, "Return", p.returns),
            KindSpec(K.FX_RETURN, "FX return", p.fx),
            KindSpec(K.BUS, "Aux", p.aux),
            KindSpec(K.FX_SEND, "FX master", p.fx),
            KindSpec(K.MAIN, "Main LR", 1),
            KindSpec(K.SEND, "Channel → aux", p.inputs, dest_label="Aux", dest_max=p.aux),
            KindSpec(K.RAW, "Raw address (fader:float / mute:int, e.g. 49:float)"),
        ]

    def __init__(self, config: MixerConfig):
        super().__init__(config)
        self._tcp = TcpClient(self.host, self.port, on_data=self._on_data)
        self._buffer = b""
        self._seq = 0
        self._pending: dict[int, asyncio.Future[bytes]] = {}
        self._values: dict[int, bytes] = {}
        self._detected: Profile | None = None
        self._ready = False
        self._keepalive: asyncio.Task | None = None

    @property
    def profile(self) -> Profile:
        return self.profile_for(self.config, self._detected)

    # ------------------------------------------------------------- addressing

    def address(self, target: Target, which: str) -> tuple[int, str]:
        """(address, value kind 'float'|'int')."""
        p = self.profile
        n = (target.number or 1) - 1
        k = target.kind
        if k == K.RAW:
            raw = (target.raw or "").strip()
            addr, _, kind = raw.partition(":")
            try:
                return int(addr, 0), (kind or ("int" if which == "mute" else "float"))
            except ValueError as exc:
                raise MixerError("Raw address: number[:float|int]") from exc
        if k == K.CHANNEL:
            base = p.input1 + n * p.input_size
            return (base + 7, "int") if which == "mute" else (base + 8, "float")
        if k == K.STEREO_IN:
            base = p.return1 + n * p.return_size
            return (base + 7, "int") if which == "mute" else (base + 8, "float")
        if k == K.FX_RETURN:
            base = p.fxin1 + n * p.fxin_size
            return (base + 2, "int") if which == "mute" else (base + 3, "float")
        if k == K.BUS:
            base = p.aux1 + n * p.aux_size
            return (base + 3, "int") if which == "mute" else (base + 4, "float")
        if k == K.FX_SEND:
            base = p.fxout1 + n * p.fxout_size
            return (base + 2, "int") if which == "mute" else (base + 3, "float")
        if k == K.MAIN:
            return (p.main_mute, "int") if which == "mute" else (p.main_fader, "float")
        if k == K.SEND:
            if which == "mute":
                raise MixerError("Aux sends have no mute")
            return p.input1 + n * p.input_size + p.input_aux1 + ((target.dest or 1) - 1) * 3, "float"
        raise MixerError(f"not supported: {k.value}")

    @staticmethod
    def values_body(address: int, kind: str, value: float | int) -> bytes:
        payload = struct.pack(">f", float(value)) if kind == "float" else struct.pack(">i", int(value))
        return struct.pack(">iI", address, 0x00010500) + payload

    # ------------------------------------------------------------- transport

    def _next_seq(self) -> int:
        self._seq = self._seq % 255 + 1  # 1…255, never 0
        return self._seq

    def _on_data(self, data: bytes) -> None:
        messages, self._buffer = decode_messages(self._buffer + data)
        for seq, mtype, subtype, body in messages:
            if mtype in (T_RESPONSE, T_ERROR):
                fut = self._pending.pop(seq, None)
                if fut and not fut.done():
                    if mtype == T_ERROR:
                        fut.set_exception(MixerError(f"Mixer rejected request 0x{subtype:02x}"))
                    else:
                        fut.set_result(body)
            elif mtype == T_REQUEST:
                if subtype == C_VALUES:
                    self._store_values(body)
                asyncio.ensure_future(self._tcp.send(encode_message(seq, T_RESPONSE, subtype)))

    def _store_values(self, body: bytes) -> None:
        if len(body) < 8:
            return
        start = struct.unpack(">i", body[:4])[0]
        count = struct.unpack(">I", body[4:8])[0] >> 16
        for i in range(min(count, (len(body) - 8) // 4)):
            self._values[start + i] = body[8 + i * 4: 12 + i * 4]

    async def request(self, subtype: int, body: bytes = b"", timeout: float = 2.0) -> bytes:
        loop = asyncio.get_running_loop()
        seq = self._next_seq()
        fut: asyncio.Future[bytes] = loop.create_future()
        self._pending[seq] = fut
        await self._tcp.send(encode_message(seq, T_REQUEST, subtype, body))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(seq, None)

    async def connect(self) -> None:
        if self._tcp.connected and self._ready:
            return
        self._ready = False
        await self._tcp.connect()
        try:
            await self.request(C_KEEPALIVE)
            await self.request(C_CHANNEL_INFO, bytes(8))
            handshake = await self.request(C_HANDSHAKE)
        except asyncio.TimeoutError as exc:
            raise MixerError("No reply to the Mackie handshake") from exc
        self._detected = DL32 if len(handshake) == 16 and handshake[1] == 5 else DL16S
        self._ready = True
        try:
            await self.request(C_CHANNEL_INFO, bytes([0, 0, 0, 6]))  # ask for all channel values
        except (asyncio.TimeoutError, MixerError):
            pass
        if self._keepalive is None or self._keepalive.done():
            self._keepalive = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        try:
            while self._tcp.connected:
                await asyncio.sleep(2.5)
                await self.request(C_KEEPALIVE)
        except (asyncio.CancelledError, asyncio.TimeoutError, ConnectionError, OSError, MixerError):
            self._ready = False

    async def close(self) -> None:
        if self._keepalive:
            self._keepalive.cancel()
        await self._tcp.close()
        self._ready = False

    async def test(self) -> str:
        await self.connect()
        name = "DL32S/DL32R" if self._detected is DL32 else "DL16S"
        return f"Connected – detected {name}"

    async def set_mute(self, target: Target, muted: bool) -> None:
        self.validate(target, "mute")
        await self.connect()
        addr, kind = self.address(target, "mute")
        await self.request(C_VALUES, self.values_body(addr, kind, 1 if muted else 0))
        self._values[addr] = struct.pack(">i", 1 if muted else 0)

    async def set_level(self, target: Target, db: float | None) -> None:
        self.validate(target, "level")
        await self.connect()
        addr, kind = self.address(target, "level")
        value = -120.0 if is_off(db) else max(-120.0, float(db))  # type: ignore[arg-type]
        await self.request(C_VALUES, self.values_body(addr, kind, value))
        self._values[addr] = struct.pack(">f", value)

    async def get_mute(self, target: Target) -> bool | None:
        self.validate(target, "mute")
        await self.connect()
        raw = self._values.get(self.address(target, "mute")[0])
        return None if raw is None else struct.unpack(">i", raw)[0] != 0

    async def get_level(self, target: Target) -> float | None:
        self.validate(target, "level")
        await self.connect()
        raw = self._values.get(self.address(target, "level")[0])
        if raw is None:
            return None
        v = struct.unpack(">f", raw)[0]
        return MIN_DB if v <= -119.9 else v
