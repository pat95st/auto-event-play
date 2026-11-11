"""Yamaha LS9 (LS9-16 / LS9-32) – experimental.

The LS9 has no text protocol like RCP. Its documented remote control is the
MIDI "Parameter Change" SysEx (owner's manual, MIDI data format)::

    F0 43 1n 3E 12 01  eH eL  iH iL  cH cL  d4 d3 d2 d1 d0  F7
    (n = device number, e = element, i = index, c = channel, d = 5×7-bit value)

Over Ethernet the LS9 speaks Yamaha "EtherMIDI" on TCP port 12300 (the
protocol of Yamaha's Network-MIDI/DME-N driver). Packet layout, handshake and
element numbers follow the reverse-engineered documentation of
github.com/leocb/Yamaha-LS9-Network-Protocol-Library (PROTOCOL.md); they
are not yet confirmed on hardware by that project either, so treat this
driver as experimental and test before a show.
"""

from __future__ import annotations

import asyncio
import logging
import struct

from ...domain.levels import MIN_DB, interpolate_table, is_off
from ...domain.model import MixerConfig, Target, TargetKind
from .base import KindSpec, MixerDriver, MixerError, OptionSpec, register_driver
from .transports import TcpClient

log = logging.getLogger("aep.mixer.ls9")
K = TargetKind

CMD_SYSEX, CMD_OPEN, CMD_OPEN_ACK, CMD_CHALLENGE, CMD_CHALLENGE_OK = 0x16, 0x20, 0x21, 0x23, 0x24
COPYRIGHT = b"COPYRIGHT (C) YAMAHA Corp."

# element numbers (index 0): (on, fader)
ELEMENTS = {K.CHANNEL: (0x0031, 0x0033), K.BUS: (0x004C, 0x004E), K.MATRIX: (0x005D, 0x005F), K.MAIN: (0x006B, 0x006D)}
MODELS = {"ls9-16": {"label": "LS9-16", "in": 32}, "ls9-32": {"label": "LS9-32", "in": 64}}

# LS9 fader curve (raw 0…1023). 0 dB = 823 and +10 dB = 1023 are documented; the
# points below -10 dB approximate Yamaha's 1024-step table (PRM TABLE #03).
FADER_TABLE = [(1, -138.0), (273, -60.0), (423, -30.0), (623, -10.0), (823, 0.0), (1023, 10.0)]


def fader_raw(db: float | None) -> int:
    if is_off(db):
        return 0
    return int(round(interpolate_table(float(db), [(d, float(r)) for r, d in FADER_TABLE])))


def fader_db(raw: int) -> float:
    return MIN_DB if raw <= 0 else interpolate_table(float(raw), [(float(r), d) for r, d in FADER_TABLE])


def encode_value(value: int) -> list[int]:
    v = value & 0x7FFFFFFFF  # 35 bits
    return [(v >> shift) & 0x7F for shift in (28, 21, 14, 7, 0)]


def decode_value(data: bytes | list[int]) -> int:
    v = 0
    for b in data:
        v = (v << 7) | (b & 0x7F)
    if v & (1 << 34):  # sign
        v -= 1 << 35
    return v


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else (c << 1)
        table.append(c & 0xFFFFFFFF)
    return table


CRC_TABLE = _crc_table()


def challenge_crc(seed: int, mac: bytes) -> int:
    """16-bit answer to the console's MAC challenge (see PROTOCOL.md §6)."""
    crc = (CRC_TABLE[(~seed) & 0xFF] ^ 0xFFFFFF00) & 0xFFFFFFFF
    last = 0
    for byte in mac + COPYRIGHT:
        idx = ((crc >> 24) ^ byte) & 0xFF
        last = CRC_TABLE[idx]
        crc = (last ^ (crc << 8)) & 0xFFFFFFFF
    low = (last & 0xFF) << 8
    high = CRC_TABLE[(crc >> 24) & 0xFF] & 0xFFFF
    return (high ^ low) & 0xFFFF


@register_driver
class YamahaLS9Driver(MixerDriver):
    type_id = "yamaha_ls9"
    display_name = "Yamaha LS9 (experimental)"
    family = "Yamaha"
    default_port = 12300
    transport = "EtherMIDI/TCP"
    can_query = True
    option_specs = [
        OptionSpec("model", "Model", "select", "ls9-32", choices=[(k, v["label"]) for k, v in MODELS.items()]),
        OptionSpec("device_number", "MIDI device number", "int", 1, help="MIDI Rx channel of the LS9 (1–16); Parameter Change Rx/Tx must be on."),
        OptionSpec("mac", "Console MAC address", "str", "", help="Only needed if the console asks for authentication (MISC SETUP shows the MAC)."),
    ]
    notes = ("EtherMIDI over TCP 12300 with MIDI Parameter Change (SysEx). Based on reverse engineering – please test before use. "
             "Enable PARAMETER CHANGE Rx/Tx in the LS9 MIDI setup.")

    @classmethod
    def kinds_for(cls, config: MixerConfig | None) -> list[KindSpec]:
        model = MODELS.get((config.options.get("model") if config else None) or "ls9-32", MODELS["ls9-32"])
        return [
            KindSpec(K.CHANNEL, "Channel", model["in"]),
            KindSpec(K.BUS, "Mix", 16),
            KindSpec(K.MATRIX, "Matrix", 8),
            KindSpec(K.MAIN, "Master", 2, number_labels={1: "Stereo", 2: "Mono"}),
            KindSpec(K.RAW, "Raw element/index/channel (e.g. 0x31/0/4)"),
        ]

    def __init__(self, config: MixerConfig):
        super().__init__(config)
        self._tcp = TcpClient(self.host, self.port, on_data=self._on_data)
        self._buffer = b""
        self._open_ack: asyncio.Future[bytes] | None = None
        self._challenge_ok: asyncio.Future[bool] | None = None
        self._opened = False
        self._waiters: dict[tuple[int, int, int], list[asyncio.Future[int]]] = {}

    @property
    def device(self) -> int:
        n = int(self.option("device_number", 1) or 1)
        if not 1 <= n <= 16:
            raise MixerError("MIDI device number must be 1–16")
        return n - 1

    # ------------------------------------------------------------- packets

    @staticmethod
    def packet(cmd: int, b8: bytes = b"\x00" * 4, b12: bytes = b"\xff" * 4, payload: bytes = b"", port: int = 0, dev: int = 1) -> bytes:
        total = 16 + len(payload)
        return struct.pack(">IBBBB", total, cmd, 0, port, dev) + b8 + b12 + payload

    @classmethod
    def sysex_packet(cls, sysex: bytes) -> bytes:
        n = len(sysex)
        return cls.packet(CMD_SYSEX, struct.pack(">I", n), b"\xff" * 4, struct.pack(">I", n) + sysex)

    def param_change(self, element: int, index: int, channel: int, value: int) -> bytes:
        return bytes([0xF0, 0x43, 0x10 | self.device, 0x3E, 0x12, 0x01, element >> 7 & 0x7F, element & 0x7F,
                      index >> 7 & 0x7F, index & 0x7F, channel >> 7 & 0x7F, channel & 0x7F] + encode_value(value) + [0xF7])

    def param_request(self, element: int, index: int, channel: int) -> bytes:
        return bytes([0xF0, 0x43, 0x30 | self.device, 0x3E, 0x12, 0x01, element >> 7 & 0x7F, element & 0x7F,
                      index >> 7 & 0x7F, index & 0x7F, channel >> 7 & 0x7F, channel & 0x7F, 0xF7])

    def addresses(self, target: Target, which: str) -> list[tuple[int, int, int]]:
        """[(element, index, channel)] – the stereo master addresses L and R."""
        if target.kind == K.RAW:
            parts = (target.raw or "").replace(" ", "").split("/")
            if len(parts) != 3:
                raise MixerError("Raw address: element/index/channel, e.g. 0x31/0/4")
            return [tuple(int(p, 0) for p in parts)]  # type: ignore[list-item]
        if target.kind not in ELEMENTS:
            raise MixerError(f"not supported: {target.kind.value}")
        on, fader = ELEMENTS[target.kind]
        element = on if which == "mute" else fader
        n = (target.number or 1) - 1
        if target.kind == K.MAIN:
            return [(element, 0, 0), (element, 0, 1)] if n == 0 else [(element, 0, 2)]
        return [(element, 0, n)]

    def set_packets(self, target: Target, which: str, value: bool | float | None) -> bytes:
        if which == "mute":
            raw = 0 if value else 1  # ON = not muted
        else:
            raw = fader_raw(value)  # type: ignore[arg-type]
        return b"".join(self.sysex_packet(self.param_change(e, i, c, raw)) for e, i, c in self.addresses(target, which))

    # ------------------------------------------------------------- transport

    def _on_data(self, data: bytes) -> None:
        self._buffer += data
        while len(self._buffer) >= 16:
            total = struct.unpack(">I", self._buffer[:4])[0]
            if total < 16 or total > 5000:
                self._buffer = b""
                return
            if len(self._buffer) < total:
                return
            pkt, self._buffer = self._buffer[:total], self._buffer[total:]
            cmd = pkt[4]
            if cmd == CMD_OPEN_ACK and self._open_ack and not self._open_ack.done():
                self._open_ack.set_result(pkt)
            elif cmd == CMD_CHALLENGE_OK and self._challenge_ok and not self._challenge_ok.done():
                self._challenge_ok.set_result(True)
            elif cmd == CMD_SYSEX and len(pkt) >= 20:
                self._on_sysex(pkt[20:])

    def _on_sysex(self, sx: bytes) -> None:
        if len(sx) < 18 or sx[0] != 0xF0 or sx[1] != 0x43 or sx[3] != 0x3E or sx[4] != 0x12 or (sx[2] & 0xF0) != 0x10:
            return
        element = sx[6] << 7 | sx[7]
        index = sx[8] << 7 | sx[9]
        channel = sx[10] << 7 | sx[11]
        value = decode_value(sx[12:17])
        for fut in self._waiters.pop((element, index, channel), []):
            if not fut.done():
                fut.set_result(value)

    async def connect(self) -> None:
        if self._tcp.connected and self._opened:
            return
        self._opened = False
        await self._tcp.connect()
        loop = asyncio.get_running_loop()
        self._open_ack = loop.create_future()
        await self._tcp.send(self.packet(CMD_OPEN, b"\x01\x00\x00\x00", struct.pack(">I", 0x20000000)))
        try:
            ack = await asyncio.wait_for(self._open_ack, 3.0)
        except asyncio.TimeoutError as exc:
            raise MixerError("LS9 did not acknowledge the EtherMIDI connection") from exc
        capability = struct.unpack(">I", ack[12:16])[0]
        if capability & 0x80000000:
            mac_text = str(self.option("mac", "") or "").replace("-", ":").strip()
            if not mac_text:
                raise MixerError("The LS9 requests authentication – enter the console MAC address in the options")
            mac = bytes(int(x, 16) for x in mac_text.split(":"))
            self._challenge_ok = loop.create_future()
            crc = challenge_crc(ack[10], mac)
            await self._tcp.send(self.packet(CMD_CHALLENGE, struct.pack(">HH", crc, 0), b"\x00" * 4))
            try:
                await asyncio.wait_for(self._challenge_ok, 3.0)
            except asyncio.TimeoutError as exc:
                raise MixerError("LS9 rejected the authentication (check the MAC address)") from exc
        self._opened = True

    async def close(self) -> None:
        if self._tcp.connected:
            try:
                await self._tcp.send(self.packet(0x30, b"\x00" * 4))
            except Exception:
                pass
        await self._tcp.close()
        self._opened = False

    async def test(self) -> str:
        await self.connect()
        value = await self._get(ELEMENTS[K.CHANNEL][1], 0, 0, 1.5)
        if value is None:
            return "EtherMIDI connected, but no reply to a parameter request (is PARAMETER CHANGE Rx/Tx enabled?)"
        return f"Connected – channel 1 fader at {fader_db(value):.1f} dB"

    async def set_mute(self, target: Target, muted: bool) -> None:
        self.validate(target, "mute")
        await self.connect()
        await self._tcp.send(self.set_packets(target, "mute", muted))

    async def set_level(self, target: Target, db: float | None) -> None:
        self.validate(target, "level")
        await self.connect()
        await self._tcp.send(self.set_packets(target, "level", db))

    async def _get(self, element: int, index: int, channel: int, timeout: float = 0.8) -> int | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[int] = loop.create_future()
        key = (element, index, channel)
        self._waiters.setdefault(key, []).append(fut)
        await self._tcp.send(self.sysex_packet(self.param_request(element, index, channel)))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            lst = self._waiters.get(key, [])
            if fut in lst:
                lst.remove(fut)

    async def get_mute(self, target: Target) -> bool | None:
        self.validate(target, "mute")
        await self.connect()
        v = await self._get(*self.addresses(target, "mute")[0])
        return None if v is None else v == 0

    async def get_level(self, target: Target) -> float | None:
        self.validate(target, "level")
        await self.connect()
        v = await self._get(*self.addresses(target, "level")[0])
        return None if v is None else fader_db(v)
