"""Shared logic for OSC consoles: address mapping + value encoding per parameter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ...domain.levels import MIN_DB, clamp_db, db_to_fader, fader_to_db
from ...domain.model import Target, TargetKind
from .base import MixerDriver, MixerError
from .transports import OscUdpClient
from ...i18n import Msg


@dataclass
class OscParam:
    address: str
    encode: Callable[[Any], Any]          # value (bool muted / float dB) -> OSC argument
    decode: Callable[[list[Any]], Any]    # reply args -> bool muted / float dB


def _last_number(args: list[Any]) -> float | None:
    for a in reversed(args):
        if isinstance(a, (int, float)) and not isinstance(a, bool):
            return float(a)
    return None


# ---- encoders shared by Behringer families


def mute_via_on(muted: bool) -> int:
    """X32/X-AIR '/mix/on': 1 = channel on (unmuted)."""
    return 0 if muted else 1


def decode_on(args: list[Any]) -> bool | None:
    v = _last_number(args)
    return None if v is None else v < 0.5


def mute_direct(muted: bool) -> int:
    return 1 if muted else 0


def decode_mute_direct(args: list[Any]) -> bool | None:
    v = _last_number(args)
    return None if v is None else v >= 0.5


def level_x32(db: float | None) -> float:
    return float(round(db_to_fader(db), 4))


def decode_level_x32(args: list[Any]) -> float | None:
    v = _last_number(args)
    return None if v is None else fader_to_db(v)


def level_db(minimum: float) -> Callable[[float | None], float]:
    def enc(db: float | None) -> float:
        if db is None or db <= MIN_DB:
            return float(minimum)
        return float(max(minimum, clamp_db(db)))
    return enc


def decode_level_db(args: list[Any]) -> float | None:
    v = _last_number(args)
    return None if v is None else max(MIN_DB, v)


class OscMixerDriver(MixerDriver):
    transport = "OSC/UDP"
    can_query = True

    def __init__(self, config):  # noqa: ANN001
        super().__init__(config)
        self._client = OscUdpClient(self.host, self.port)

    def param(self, target: Target, which: str) -> OscParam:  # pragma: no cover - abstract
        raise NotImplementedError

    def raw_param(self, target: Target, which: str) -> OscParam:
        """RAW targets: '/address' – mute sends int 1=muted (int 0=muted if the address ends in '/on'),
        level sends the driver's native level format."""
        address = (target.raw or "").strip()
        if not address.startswith("/"):
            raise MixerError("Raw address must start with '/'")
        if which == "mute":
            if address.endswith("/on"):
                return OscParam(address, mute_via_on, decode_on)
            return OscParam(address, mute_direct, decode_mute_direct)
        return self._raw_level_param(address)

    def _raw_level_param(self, address: str) -> OscParam:
        return OscParam(address, level_x32, decode_level_x32)

    def _resolve(self, target: Target, which: str) -> OscParam:
        self.validate(target, which)
        if target.kind == TargetKind.RAW:
            return self.raw_param(target, which)
        return self.param(target, which)

    async def connect(self) -> None:
        await self._client.connect()

    async def close(self) -> None:
        self._client.close()

    async def set_mute(self, target: Target, muted: bool) -> None:
        p = self._resolve(target, "mute")
        await self._client.send(p.address, p.encode(muted))

    async def set_level(self, target: Target, db: float | None) -> None:
        p = self._resolve(target, "level")
        await self._client.send(p.address, p.encode(db))

    async def get_mute(self, target: Target) -> bool | None:
        p = self._resolve(target, "mute")
        reply = await self._client.query(p.address)
        return None if reply is None else p.decode(reply)

    async def get_level(self, target: Target) -> float | None:
        p = self._resolve(target, "level")
        reply = await self._client.query(p.address)
        return None if reply is None else p.decode(reply)

    async def info_query(self) -> list[Any] | None:
        return await self._client.query("/info", timeout=1.0)

    async def test(self) -> str:
        await self.connect()
        reply = await self.info_query()
        if reply is None:
            raise MixerError(Msg("No reply from {host}:{port} (/info)", host=self.host, port=self.port))
        return Msg("Reply: {reply}", reply=" · ".join(str(a) for a in reply if not isinstance(a, bytes)))
