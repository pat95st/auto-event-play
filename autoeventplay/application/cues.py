"""Executes console actions around a playback (unmute/level before, restore/after).

Several playbacks can overlap (media and announcements run in parallel).
Restores are reference counted per console parameter: if two playbacks both
unmute channel 30, it is muted again only after the last one ended, and the
original value captured by the first playback is the one restored.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..domain.levels import MIN_DB, is_off
from ..domain.model import CueAction, CuePhase, CueRule, MuteAction
from ..infrastructure.mixers.base import MixerError
from ..infrastructure.mixers.manager import MixerManager

log = logging.getLogger("aep.cues")


@dataclass
class _Lease:
    count: int = 0
    original: object = None   # bool (mute) or float/None (level); None = unknown
    known: bool = False
    fallback: object = None


@dataclass
class CueSession:
    """Restores owed by one playback."""

    held: list[tuple[str, CueAction]] = field(default_factory=list)  # (lease key, action)
    rules: list[CueRule] = field(default_factory=list)


class CueExecutor:
    def __init__(self, mixers: MixerManager):
        self.mixers = mixers
        self._leases: dict[str, _Lease] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(action: CueAction, param: str) -> str:
        return f"{action.mixer_id}|{action.target.key()}|{param}"

    def describe(self, action: CueAction) -> str:
        return self.mixers.describe(action.mixer_id, action.target)

    async def start(self, rules: list[CueRule]) -> CueSession:
        session = CueSession(rules=rules)
        actions = [a for r in rules for a in sorted(r.actions, key=lambda a: a.sort) if a.phase == CuePhase.START]
        await asyncio.gather(*(self._apply_start(a, session) for a in actions))
        delay = max((r.pre_delay_ms for r in rules), default=0)
        if delay > 0:
            await asyncio.sleep(delay / 1000)
        return session

    async def finish(self, session: CueSession) -> None:
        # 1) restores (reverse order), 2) END actions
        await asyncio.gather(*(self._release(key, action) for key, action in reversed(session.held)))
        end_actions = [a for r in session.rules for a in sorted(r.actions, key=lambda a: a.sort) if a.phase == CuePhase.END]
        await asyncio.gather(*(self._apply_plain(a) for a in end_actions))

    # ------------------------------------------------------------------ internals

    async def _apply_start(self, action: CueAction, session: CueSession) -> None:
        if action.restore:
            if action.mute != MuteAction.UNCHANGED:
                key = self._key(action, "mute")
                await self._acquire(key, action, "mute")
                session.held.append((key, action))
            if action.level_db is not None:
                key = self._key(action, "level")
                await self._acquire(key, action, "level")
                session.held.append((key, action))
        await self._apply_plain(action)

    async def _acquire(self, key: str, action: CueAction, param: str) -> None:
        async with self._lock:
            lease = self._leases.get(key)
            if lease is None:
                lease = _Lease()
                self._leases[key] = lease
                if param == "mute":
                    current = await self.mixers.get_mute(action.mixer_id, action.target)
                    lease.known = current is not None
                    lease.original = current
                    lease.fallback = action.mute != MuteAction.MUTE  # opposite of what we set
                else:
                    current = await self.mixers.get_level(action.mixer_id, action.target)
                    lease.known = current is not None
                    lease.original = current
                    lease.fallback = action.restore_level_db
            lease.count += 1

    async def _release(self, key: str, action: CueAction) -> None:
        async with self._lock:
            lease = self._leases.get(key)
            if lease is None:
                return
            lease.count -= 1
            if lease.count > 0:
                return
            del self._leases[key]
        param = key.rsplit("|", 1)[1]
        value = lease.original if lease.known else lease.fallback
        try:
            if param == "mute":
                if value is None:
                    return
                await self.mixers.set_mute(action.mixer_id, action.target, bool(value))
                log.info("Restored: %s → %s", self.describe(action), "mute" if value else "on")
            else:
                if value is None and not lease.known:
                    log.info("Level of %s not restored (console reports no value, no fallback level)", self.describe(action))
                    return
                await self._fade(action, float(value) if value is not None else MIN_DB, action.fade_ms)
                log.info("Restored: %s → %s dB", self.describe(action), "-∞" if is_off(value) else f"{value:.1f}")
        except MixerError as exc:
            log.error("Restore failed (%s): %s", self.describe(action), exc)

    async def _apply_plain(self, action: CueAction) -> None:
        label = self.describe(action)
        try:
            if action.mute == MuteAction.UNMUTE and action.level_db is not None and action.fade_ms > 0:
                # fade in: set the level low first, unmute, then fade
                await self.mixers.set_level(action.mixer_id, action.target, MIN_DB)
            if action.mute != MuteAction.UNCHANGED:
                muted = action.mute == MuteAction.MUTE
                if muted and action.level_db is not None and action.fade_ms > 0:
                    await self._fade(action, action.level_db, action.fade_ms)
                await self.mixers.set_mute(action.mixer_id, action.target, muted)
                log.info("Console: %s → %s", label, "Mute" if muted else "Unmute")
            if action.level_db is not None and not (action.mute == MuteAction.MUTE and action.fade_ms > 0):
                await self._fade(action, action.level_db, action.fade_ms)
                log.info("Console: %s → %s dB", label, "-∞" if is_off(action.level_db) else f"{action.level_db:.1f}")
        except MixerError as exc:
            log.error("Console action failed (%s): %s", label, exc)

    async def _fade(self, action: CueAction, target_db: float, fade_ms: int) -> None:
        if fade_ms <= 0:
            await self.mixers.set_level(action.mixer_id, action.target, target_db)
            return
        start = await self.mixers.get_level(action.mixer_id, action.target)
        if start is None:
            start = MIN_DB if target_db > MIN_DB else 0.0
        steps = max(2, min(50, fade_ms // 40))
        lo = max(start, -60.0) if is_off(start) is False else -60.0
        hi = max(target_db, -60.0) if not is_off(target_db) else -60.0
        for i in range(1, steps + 1):
            value = lo + (hi - lo) * i / steps
            await self.mixers.set_level(action.mixer_id, action.target, value if i < steps else target_db)
            await asyncio.sleep(fade_ms / 1000 / steps)

    def active_leases(self) -> int:
        return len(self._leases)
