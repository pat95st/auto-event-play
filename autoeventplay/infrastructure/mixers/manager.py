"""Keeps one driver instance per configured console and tracks connection status."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ...domain.model import MixerConfig, Target
from .base import MixerDriver, MixerError, driver_class
from ...i18n import Msg, t, text_of

log = logging.getLogger("aep.mixer")


@dataclass
class MixerStatus:
    ok: bool | None = None
    message: str = Msg("not used yet")
    last_change: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": t(self.message), "last_change": self.last_change}


class MixerManager:
    def __init__(self) -> None:
        self._configs: dict[str, MixerConfig] = {}
        self._drivers: dict[str, MixerDriver] = {}
        self._status: dict[str, MixerStatus] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def configure(self, configs: list[MixerConfig]) -> None:
        new = {c.id: c for c in configs}
        for mid, drv in list(self._drivers.items()):
            if mid not in new or new[mid] != self._configs.get(mid):
                await self._close(mid)
        self._configs = new
        for mid in list(self._status):
            if mid not in new:
                del self._status[mid]

    async def _close(self, mixer_id: str) -> None:
        drv = self._drivers.pop(mixer_id, None)
        if drv is not None:
            try:
                await drv.close()
            except Exception:  # pragma: no cover
                pass

    async def close_all(self) -> None:
        for mid in list(self._drivers):
            await self._close(mid)

    def config(self, mixer_id: str) -> MixerConfig | None:
        return self._configs.get(mixer_id)

    def driver(self, mixer_id: str) -> MixerDriver:
        cfg = self._configs.get(mixer_id)
        if cfg is None:
            raise MixerError("Console is not configured (any more)")
        if not cfg.enabled:
            raise MixerError(Msg("Console \"{name}\" is disabled", name=cfg.name))
        drv = self._drivers.get(mixer_id)
        if drv is None:
            drv = driver_class(cfg.type)(cfg)
            self._drivers[mixer_id] = drv
        return drv

    def _lock(self, mixer_id: str) -> asyncio.Lock:
        return self._locks.setdefault(mixer_id, asyncio.Lock())

    def _set_status(self, mixer_id: str, ok: bool, message: str) -> None:
        st = self._status.setdefault(mixer_id, MixerStatus())
        if st.ok != ok or st.message != message:
            st.ok, st.message, st.last_change = ok, message, time.time()

    async def _run(self, mixer_id: str, label: str, coro_factory):  # noqa: ANN001, ANN202
        async with self._lock(mixer_id):
            try:
                result = await asyncio.wait_for(coro_factory(self.driver(mixer_id)), timeout=5.0)
                self._set_status(mixer_id, True, "OK")
                return result
            except asyncio.TimeoutError:
                msg = Msg("{action}: timeout", action=label)
            except (MixerError, ConnectionError, OSError) as exc:
                msg = Msg("{action}: {error}", action=label, error=text_of(exc))
            self._set_status(mixer_id, False, msg)
            await self._close(mixer_id)  # reconnect next time
            raise MixerError(msg)

    async def set_mute(self, mixer_id: str, target: Target, muted: bool) -> None:
        await self._run(mixer_id, "Mute", lambda d: d.set_mute(target, muted))

    async def set_level(self, mixer_id: str, target: Target, db: float | None) -> None:
        await self._run(mixer_id, "Level", lambda d: d.set_level(target, db))

    async def _query(self, mixer_id: str, label: str, factory):  # noqa: ANN001, ANN202
        try:
            value = await self._run(mixer_id, label, factory)
        except MixerError:
            return None
        drv = self._drivers.get(mixer_id)
        if value is None and drv is not None and drv.can_query:
            self._set_status(mixer_id, False, Msg("{action}: no reply from the console", action=label))
        return value

    async def get_mute(self, mixer_id: str, target: Target) -> bool | None:
        return await self._query(mixer_id, "Read mute", lambda d: d.get_mute(target))

    async def get_level(self, mixer_id: str, target: Target) -> float | None:
        return await self._query(mixer_id, "Read level", lambda d: d.get_level(target))

    async def test(self, mixer_id: str) -> str:
        return await self._run(mixer_id, "Test", lambda d: d.test())

    def describe(self, mixer_id: str, target: Target) -> str:
        cfg = self._configs.get(mixer_id)
        if cfg is None:
            return f"? {target.key()}"
        try:
            return f"{cfg.name}: {driver_class(cfg.type)(cfg).describe(target)}"
        except Exception:
            return f"{cfg.name}: {target.key()}"

    def statuses(self) -> dict[str, dict[str, Any]]:
        return {mid: self._status.get(mid, MixerStatus()).to_dict() for mid in self._configs}
