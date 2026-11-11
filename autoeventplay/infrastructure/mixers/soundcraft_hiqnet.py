"""Soundcraft Si and Vi series via Harman HiQnet (TCP port 3804) – experimental.

Soundcraft does not publish a third-party control protocol for Si/Vi; the
only network control is HiQnet as used by ViSi Remote and Mixing Station.
This driver implements the HiQnet transport according to Harman's
"HiQnet Third Party Programmer's Guide" (MultiParamSet / MultiParamGet).
The console-specific addresses (virtual device, object, parameter/SV IDs)
are not public, so they are configured per target kind in the option
"Adress-Map" (JSON) – e.g. taken from Harman support or read from a ViSi
Remote session with Wireshark (HiQnet dissector).

Map format::

    {
      "channel": {"max": 32, "label": "Channel",
                  "vd": 3, "object": 256, "object_step": 1,
                  "mute_sv": 1, "mute_type": "ubyte", "mute_invert": false,
                  "level_sv": 0, "level_type": "long", "level_law": "db100", "level_min": -13800},
      "main":    {"vd": 3, "object": 1024, "level_sv": 0, "level_type": "long", "level_law": "db100"}
    }

``level_law``: ``db100`` (dB × 100), ``db10`` (dB × 10), ``db`` (float dB),
``linear`` (0…1 gain) or ``fader`` (0…1 console fader curve).
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from typing import Any

from ...domain.levels import MIN_DB, db_to_fader, db_to_gain, fader_to_db, gain_to_db, is_off
from ...domain.model import MixerConfig, Target, TargetKind
from .base import KindSpec, MixerDriver, MixerError, OptionSpec, register_driver
from .transports import TcpClient
from ...i18n import Msg

log = logging.getLogger("aep.mixer.hiqnet")
K = TargetKind

HIQNET_PORT = 3804
MSG_MULTI_PARAM_SET = 0x0100
MSG_MULTI_PARAM_GET = 0x0103
FLAG_GUARANTEED = 0x0020
FLAG_INFO = 0x0004
FLAG_ERROR = 0x0008

# HiQnet data types: id, struct format
DATA_TYPES: dict[str, tuple[int, str]] = {
    "byte": (0, ">b"), "ubyte": (1, ">B"), "word": (2, ">h"), "uword": (3, ">H"),
    "long": (4, ">i"), "ulong": (5, ">I"), "float": (6, ">f"), "float64": (7, ">d"),
    "long64": (10, ">q"), "ulong64": (11, ">Q"),
}
TYPE_BY_ID = {v[0]: (k, v[1]) for k, v in DATA_TYPES.items()}


def encode_address(device: int, vd: int, obj: int) -> bytes:
    return struct.pack(">HB", device & 0xFFFF, vd & 0xFF) + (obj & 0xFFFFFF).to_bytes(3, "big")


def encode_message(src: bytes, dst: bytes, msg_id: int, payload: bytes, seq: int, flags: int = FLAG_GUARANTEED) -> bytes:
    header_len = 25
    length = header_len + len(payload)
    header = struct.pack(">BBI", 2, header_len, length) + src + dst + struct.pack(">HHBH", msg_id, flags, 5, seq & 0xFFFF)
    return header + payload


def encode_multi_param_set(params: list[tuple[int, str, Any]]) -> bytes:
    out = struct.pack(">H", len(params))
    for sv, dtype, value in params:
        type_id, fmt = DATA_TYPES[dtype]
        out += struct.pack(">HB", sv, type_id) + struct.pack(fmt, value)
    return out


def decode_message(data: bytes) -> tuple[int, int, bytes, bytes] | None:
    """Returns (msg_id, flags, source address, payload) or None if incomplete."""
    if len(data) < 25:
        return None
    header_len = data[1]
    length = struct.unpack(">I", data[2:6])[0]
    if len(data) < length:
        return None
    src = data[6:12]
    msg_id, flags = struct.unpack(">HH", data[18:22])
    return msg_id, flags, src, data[header_len:length]


def decode_params(payload: bytes) -> dict[int, Any]:
    out: dict[int, Any] = {}
    if len(payload) < 2:
        return out
    count = struct.unpack(">H", payload[:2])[0]
    pos = 2
    for _ in range(count):
        if pos + 3 > len(payload):
            break
        sv, type_id = struct.unpack(">HB", payload[pos:pos + 3])
        pos += 3
        name_fmt = TYPE_BY_ID.get(type_id)
        if name_fmt is None:
            break
        size = struct.calcsize(name_fmt[1])
        out[sv] = struct.unpack(name_fmt[1], payload[pos:pos + size])[0]
        pos += size
    return out


EXAMPLE_MAP: dict[str, Any] = {
    "channel": {"max": 32, "label": "Channel", "vd": 3, "object": 256, "object_step": 1,
                "mute_sv": 1, "mute_type": "ubyte", "level_sv": 0, "level_type": "long", "level_law": "db100"},
}


def _load_map(config: MixerConfig | None) -> dict[str, Any]:
    raw = (config.options.get("map") if config else None) or {}
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise MixerError(Msg("The address map is not valid JSON: {error}", error=str(exc))) from exc
    if not isinstance(raw, dict):
        raise MixerError("The address map must be a JSON object")
    return raw


class _SoundcraftHiqnetBase(MixerDriver):
    family = "Soundcraft"
    default_port = HIQNET_PORT
    transport = "HiQnet/TCP"
    can_query = True
    option_specs = [
        OptionSpec("device", "HiQnet address of the console", "int", 1, help="Shown in the console's HiQnet/network settings."),
        OptionSpec("source_device", "Own HiQnet address", "int", 1337, help="Must be unique on the network; allow it on the console if access control is on."),
        OptionSpec("map", "Adress-Map (JSON)", "json", json.dumps(EXAMPLE_MAP, indent=2),
                   help="Virtual device, object and parameter IDs per target type – Soundcraft does not publish these, see documentation."),
    ]

    @classmethod
    def kinds_for(cls, config: MixerConfig | None) -> list[KindSpec]:
        try:
            mapping = _load_map(config) or EXAMPLE_MAP
        except MixerError:
            mapping = EXAMPLE_MAP
        kinds = []
        for key, spec in mapping.items():
            try:
                kind = TargetKind(key)
            except ValueError:
                continue
            kinds.append(KindSpec(kind, spec.get("label") or key, int(spec.get("max", 1)),
                                  mute="mute_sv" in spec, level="level_sv" in spec,
                                  dest_label=spec.get("dest_label"), dest_max=spec.get("dest_max")))
        kinds.append(KindSpec(TargetKind.RAW, "Raw: vd/object/sv/type (e.g. 3/256/1/ubyte)"))
        return kinds

    def __init__(self, config: MixerConfig):
        super().__init__(config)
        self._tcp = TcpClient(self.host, self.port, on_data=self._on_data)
        self._seq = 0
        self._buffer = b""
        self._waiters: dict[tuple[bytes, int], list[asyncio.Future[Any]]] = {}

    @property
    def src(self) -> bytes:
        return encode_address(int(self.option("source_device", 1337) or 1337), 0, 0)

    def _dst(self, vd: int, obj: int) -> bytes:
        return encode_address(int(self.option("device", 1) or 1), vd, obj)

    def resolve(self, target: Target, which: str) -> tuple[bytes, int, str, dict[str, Any]]:
        """(destination address, SV id, data type, spec)."""
        if target.kind == K.RAW:
            parts = (target.raw or "").replace(" ", "").split("/")
            if len(parts) < 3:
                raise MixerError("Raw address: vd/object/sv[/type], e.g. 3/256/1/ubyte")
            vd, obj, sv = (int(p, 0) for p in parts[:3])
            dtype = parts[3] if len(parts) > 3 else ("ubyte" if which == "mute" else "long")
            return self._dst(vd, obj), sv, dtype, {"level_law": "db100"}
        spec = _load_map(self.config).get(target.kind.value)
        if not spec:
            raise MixerError(Msg("No HiQnet address for {kind} in the map", kind=target.kind.value))
        key_sv = "mute_sv" if which == "mute" else "level_sv"
        if key_sv not in spec:
            raise MixerError(Msg("Map: {key} missing for {kind}", key=key_sv, kind=target.kind.value))
        n = (target.number or 1) - 1
        obj = int(spec.get("object", 0)) + n * int(spec.get("object_step", 1))
        if target.kind == K.SEND:
            obj += ((target.dest or 1) - 1) * int(spec.get("dest_step", 0))
        dtype = spec.get("mute_type" if which == "mute" else "level_type", "ubyte" if which == "mute" else "long")
        if dtype not in DATA_TYPES:
            raise MixerError(Msg("Unknown HiQnet data type {type}", type=dtype))
        return self._dst(int(spec.get("vd", 0)), obj), int(spec[key_sv]), dtype, spec

    @staticmethod
    def encode_level(db: float | None, spec: dict[str, Any]) -> float | int:
        law = spec.get("level_law", "db100")
        if law == "linear":
            return db_to_gain(db)
        if law == "fader":
            return db_to_fader(db)
        if is_off(db):
            return spec.get("level_min", -13800 if law == "db100" else (-1380 if law == "db10" else -138.0))
        if law == "db10":
            return int(round(db * 10))
        if law == "db":
            return float(db)
        return int(round(db * 100))

    @staticmethod
    def decode_level(value: float, spec: dict[str, Any]) -> float:
        law = spec.get("level_law", "db100")
        if law == "linear":
            return gain_to_db(float(value))
        if law == "fader":
            return fader_to_db(float(value))
        if value <= spec.get("level_min", -13800 if law == "db100" else -1380 if law == "db10" else -138.0):
            return MIN_DB
        return value / 100.0 if law == "db100" else value / 10.0 if law == "db10" else float(value)

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq

    def set_message(self, target: Target, which: str, value: Any) -> bytes:
        dst, sv, dtype, spec = self.resolve(target, which)
        if which == "mute":
            muted = bool(value)
            v: Any = int((not muted) if spec.get("mute_invert") else muted)
        else:
            v = self.encode_level(value, spec)
            if DATA_TYPES[dtype][1] in (">f", ">d"):
                v = float(v)
            else:
                v = int(v)
        return encode_message(self.src, dst, MSG_MULTI_PARAM_SET, encode_multi_param_set([(sv, dtype, v)]), self._next_seq())

    # ------------------------------------------------------------- transport

    def _on_data(self, data: bytes) -> None:
        self._buffer += data
        while True:
            msg = decode_message(self._buffer)
            if msg is None:
                if len(self._buffer) > 65536:
                    self._buffer = b""
                return
            length = struct.unpack(">I", self._buffer[2:6])[0]
            self._buffer = self._buffer[length:]
            msg_id, flags, src, payload = msg
            if msg_id != MSG_MULTI_PARAM_GET or flags & FLAG_ERROR:
                continue
            for sv, value in decode_params(payload).items():
                for fut in self._waiters.pop((src[2:], sv), []):  # match on vd+object (device may differ)
                    if not fut.done():
                        fut.set_result(value)

    async def connect(self) -> None:
        await self._tcp.connect()

    async def close(self) -> None:
        await self._tcp.close()

    async def test(self) -> str:
        await self.connect()
        return Msg("TCP connection to {host}:{port} established (HiQnet address {device})", host=self.host, port=self.port, device=self.option("device", 1))

    async def set_mute(self, target: Target, muted: bool) -> None:
        self.validate(target, "mute")
        await self._tcp.send(self.set_message(target, "mute", muted))

    async def set_level(self, target: Target, db: float | None) -> None:
        self.validate(target, "level")
        await self._tcp.send(self.set_message(target, "level", db))

    async def _get(self, target: Target, which: str, timeout: float = 0.8) -> tuple[Any, dict[str, Any]] | None:
        dst, sv, _dtype, spec = self.resolve(target, which)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        key = (dst[2:], sv)
        self._waiters.setdefault(key, []).append(fut)
        payload = struct.pack(">HH", 1, sv)
        await self._tcp.send(encode_message(self.src, dst, MSG_MULTI_PARAM_GET, payload, self._next_seq()))
        try:
            return await asyncio.wait_for(fut, timeout), spec
        except asyncio.TimeoutError:
            return None
        finally:
            lst = self._waiters.get(key, [])
            if fut in lst:
                lst.remove(fut)

    async def get_mute(self, target: Target) -> bool | None:
        self.validate(target, "mute")
        r = await self._get(target, "mute")
        if r is None:
            return None
        value, spec = r
        return (not bool(value)) if spec.get("mute_invert") else bool(value)

    async def get_level(self, target: Target) -> float | None:
        self.validate(target, "level")
        r = await self._get(target, "level")
        return None if r is None else self.decode_level(r[0], r[1])


@register_driver
class SoundcraftSiDriver(_SoundcraftHiqnetBase):
    type_id = "soundcraft_si"
    display_name = "Soundcraft Si (Expression/Performer/Impact/Compact) (experimental)"
    notes = ("Control via HiQnet (TCP 3804), like ViSi Remote. Soundcraft does not publish the parameter addresses – "
             "they have to be entered in the address map. Disable HiQnet access control on the console or allow this device.")


@register_driver
class SoundcraftViDriver(_SoundcraftHiqnetBase):
    type_id = "soundcraft_vi"
    display_name = "Soundcraft Vi (Vi1000/2000/3000/5000/7000) (experimental)"
    notes = ("Control via HiQnet (TCP 3804) on the console's Ethernet/HiQnet port. Soundcraft does not publish the "
             "parameter addresses – they have to be entered in the address map.")
