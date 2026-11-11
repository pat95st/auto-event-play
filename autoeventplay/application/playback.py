"""Playback service: works off the queue, plays audio and drives console cues.

Two lanes run in parallel by default: *media* (jingles, audio files) and
*speech* (TTS and recorded announcements). Within a lane items play in
order (oldest first). A new announcement never interrupts a running jingle;
only a ``stop`` request does. Lanes can be merged into one strict sequence
in the settings.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

from ..domain.model import CueRule, PlaybackItem, PlaybackKind, SourceBus
from ..infrastructure.audio.decoder import AudioDecoder, DecodeError
from ..infrastructure.audio.engine import AudioEngine, Voice
from ..infrastructure.audio.gong import builtin_gong
from ..infrastructure.audio.tts import TtsError, TtsService
from .cues import CueExecutor
from ..i18n import Msg, text_of

log = logging.getLogger("aep.playback")

GONG_PAUSE_S = 0.3
# Console actions must never hold back an announcement for long: after this
# time playback starts anyway, the actions keep running in the background.
CUE_START_TIMEOUT_S = 2.5


class PlaybackError(RuntimeError):
    pass


class _StopRequested(Exception):
    pass


@dataclass
class Running:
    item: PlaybackItem
    voices: list[Voice] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    stopped: bool = False
    phase: str = "preparing"


@dataclass
class PlaybackHooks:
    """Callbacks into the infrastructure, injected by the composition root."""

    report_status: Callable[[PlaybackItem, str, str | None], Awaitable[None]]
    resolve_file: Callable[[PlaybackItem], Awaitable[Path]]
    resolve_recording: Callable[[PlaybackItem], Awaitable[Path]]
    gong_path: Callable[[], Path | None]
    tts_params: Callable[[], dict[str, Any]]
    cue_rules: Callable[[], list[CueRule]]
    on_change: Callable[[], None]


class PlaybackService:
    def __init__(self, engine: AudioEngine, decoder: AudioDecoder, tts: TtsService, cues: CueExecutor, hooks: PlaybackHooks):
        self.engine = engine
        self.decoder = decoder
        self.tts = tts
        self.cues = cues
        self.hooks = hooks
        self.parallel_lanes = True
        self._queues: dict[str, asyncio.Queue[PlaybackItem]] = {}
        self._workers: list[asyncio.Task] = []
        self._pending: dict[str, PlaybackItem] = {}   # queued, not yet started
        self._running: dict[str, Running] = {}        # by item id
        self._history: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        lanes = ("media", "speech") if self.parallel_lanes else ("all",)
        for lane in lanes:
            q: asyncio.Queue[PlaybackItem] = asyncio.Queue()
            self._queues[lane] = q
            self._workers.append(asyncio.create_task(self._worker(lane, q), name=f"aep-lane-{lane}"))
        log.info("Playback ready (%s)", "media and announcements in parallel" if self.parallel_lanes else "one shared queue")

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._queues.clear()

    async def set_parallel(self, parallel: bool) -> None:
        if parallel == self.parallel_lanes and self._workers:
            return
        pending = list(self._pending.values())
        await self.stop()
        self._pending.clear()
        self.parallel_lanes = parallel
        self.start()
        for item in pending:
            self.enqueue(item)

    # ------------------------------------------------------------------ queue

    def knows(self, item_id: str) -> bool:
        return item_id in self._pending or item_id in self._running

    def enqueue(self, item: PlaybackItem) -> bool:
        if self.knows(item.id):
            return False
        if item.kind == PlaybackKind.STOP:
            asyncio.create_task(self._handle_stop(item))
            return True
        self._pending[item.id] = item
        lane = item.lane if self.parallel_lanes else "all"
        self._queues[lane].put_nowait(item)
        log.info("Request queued: %s \"%s\"", item.kind.value, item.label or item.text or item.file_id)
        self.hooks.on_change()
        return True

    async def _handle_stop(self, item: PlaybackItem) -> None:
        count = self.stop_all()
        log.info("Stop request: %d running playback(s) stopped", count)
        if not item.local:
            await self.hooks.report_status(item, "done", None)
        self._remember(item, "done", None)

    def stop_all(self) -> int:
        n = 0
        for running in self._running.values():
            running.stopped = True
            for v in running.voices:
                self.engine.stop_voice(v)
            n += 1
        self.engine.stop_all()
        self.hooks.on_change()
        return n

    async def _worker(self, lane: str, q: asyncio.Queue[PlaybackItem]) -> None:
        while True:
            item = await q.get()
            if item.id not in self._pending:  # removed meanwhile
                continue
            try:
                await self._play(item)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - safety net
                log.exception("Unexpected error in request %s", item.id)

    # ------------------------------------------------------------------ playing

    async def _play(self, item: PlaybackItem) -> None:
        self._pending.pop(item.id, None)
        running = Running(item)
        self._running[item.id] = running
        self.hooks.on_change()
        rules = [r for r in self.hooks.cue_rules() if r.matches(item.kind.value, item.file_id, item.label)]
        session = None
        cue_task: asyncio.Task | None = None
        status, error = "done", None
        cancelled_task = False
        try:
            if not item.local:
                await self.hooks.report_status(item, "playing", None)
            # prepare audio first so the console is not opened for a broken file
            running.phase = "loading"
            self.hooks.on_change()
            parts = await self._prepare(item)
            if running.stopped:
                raise _StopRequested
            if rules:
                running.phase = "console"
                self.hooks.on_change()
                log.info("Console cues for \"%s\": %s", item.label or item.kind.value, ", ".join(r.name for r in rules))
                cue_task = asyncio.create_task(self.cues.start(rules))
                try:
                    session = await asyncio.wait_for(asyncio.shield(cue_task), CUE_START_TIMEOUT_S)
                except asyncio.TimeoutError:
                    log.warning("Console actions take too long – starting playback anyway")
            running.phase = "playing"
            self.hooks.on_change()
            bus = SourceBus.MEDIA if item.lane == "media" else (SourceBus.TTS if item.kind == PlaybackKind.TTS else SourceBus.RECORDING)
            for idx, part in enumerate(parts):
                if running.stopped:
                    break
                if part is None:  # pause between gong and announcement
                    await asyncio.sleep(GONG_PAUSE_S)
                    continue
                voice = self.engine.play(bus, part, label=item.label or item.kind.value)
                running.voices.append(voice)
                await self.engine.wait(voice)
            if running.stopped:
                status = "cancelled"
            log.info("Playback %s: %s \"%s\"", "cancelled" if running.stopped else "finished", item.kind.value, item.label)
        except _StopRequested:
            status = "cancelled"
        except asyncio.CancelledError:
            status = "cancelled"
            cancelled_task = True
        except (PlaybackError, DecodeError, TtsError) as exc:
            status, error = "failed", text_of(exc)
            log.error("Playback failed (%s \"%s\"): %s", item.kind.value, item.label, exc)
        except Exception as exc:  # pragma: no cover
            status, error = "failed", Msg("Internal error: {error}", error=text_of(exc))
            log.exception("Playback failed")
        finally:
            if session is None and cue_task is not None:
                try:
                    session = await cue_task
                except Exception:  # pragma: no cover
                    log.exception("Console actions failed")
            if session is not None:
                try:
                    await self.cues.finish(session)
                except Exception:  # pragma: no cover
                    log.exception("Console restore failed")
            self._running.pop(item.id, None)
            if not item.local:
                await self.hooks.report_status(item, status, error)
            self._remember(item, status, error)
            self.hooks.on_change()
        if cancelled_task:
            raise asyncio.CancelledError

    async def _prepare(self, item: PlaybackItem) -> list[np.ndarray | None]:
        sr = self.engine.sample_rate
        if item.kind in (PlaybackKind.JINGLE, PlaybackKind.AUDIO):
            path = Path(item.local_path) if item.local_path else await self.hooks.resolve_file(item)
            return [await self.decoder.decode_async(path, sr, 2)]
        parts: list[np.ndarray | None] = []
        if item.with_gong:
            parts += [await self._gong(), None]
        if item.kind == PlaybackKind.TTS:
            params = self.hooks.tts_params()
            wav = await self.tts.synthesize_async(item.text or item.label, item.language or params.get("language"),
                                                  params.get("voice"), float(params.get("rate") or 1.0))
            try:
                parts.append(await self.decoder.decode_async(wav, sr, 1))
            finally:
                wav.unlink(missing_ok=True)
        elif item.kind == PlaybackKind.RECORDING:
            path = Path(item.local_path) if item.local_path else await self.hooks.resolve_recording(item)
            parts.append(await self.decoder.decode_async(path, sr, 1))
        else:
            raise PlaybackError(Msg("Unknown request type {kind}", kind=item.kind.value))
        return parts

    async def _gong(self) -> np.ndarray:
        path = self.hooks.gong_path()
        if path is not None and path.exists():
            try:
                return await self.decoder.decode_async(path, self.engine.sample_rate, 1)
            except DecodeError as exc:
                log.warning("Custom gong not playable (%s) – using built-in gong", exc)
        return builtin_gong(self.engine.sample_rate)

    # ------------------------------------------------------------------ read side

    def _remember(self, item: PlaybackItem, status: str, error: str | None) -> None:
        self._history.insert(0, {"id": item.id, "kind": item.kind.value, "label": item.label or item.text or "",
                                 "status": status, "error": error, "local": item.local, "time": time.time()})
        del self._history[50:]

    def now_playing(self) -> str:
        labels = [r.item.label or r.item.kind.value for r in self._running.values() if r.phase == "playing"]
        return ", ".join(labels)[:250]

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": [
                {"id": r.item.id, "kind": r.item.kind.value, "label": r.item.label or r.item.text or "", "phase": r.phase,
                 "since": r.started, "local": r.item.local}
                for r in self._running.values()
            ],
            "pending": [{"id": i.id, "kind": i.kind.value, "label": i.label or i.text or ""} for i in self._pending.values()],
            "history": self._history[:20],
            "parallel": self.parallel_lanes,
        }
