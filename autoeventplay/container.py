"""Composition root: builds and wires all services."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import wave
from pathlib import Path

from .application.bus import CommandBus, QueryBus
from .application.command_handlers import register_command_handlers
from .application.cues import CueExecutor
from .application.playback import PlaybackHooks, PlaybackService
from .application.query_handlers import register_query_handlers
from .config import AppConfig
from .infrastructure.audio.decoder import AudioDecoder
from .infrastructure.audio.engine import AudioEngine
from .infrastructure.audio.tts import TtsService, TtsSettings
from .infrastructure.auth import SessionManager
from .infrastructure.db import Database
from .infrastructure.erm.sync import ErmSync
from .infrastructure.events import EventHub
from .infrastructure.logs import ring
from .infrastructure.mixers.manager import MixerManager
from .infrastructure.repositories import (
    CueRepository,
    FileCacheRepository,
    MixerRepository,
    ProcessedRequestRepository,
    RoutingRepository,
    SettingsRepository,
    UserRepository,
)

log = logging.getLogger("aep")


class Services:
    def __init__(self, config: AppConfig):
        self.config = config
        self.started_at = time.time()
        config.ensure_dirs()
        self.db = Database(config.db_path)
        self.db.migrate()
        self.settings = SettingsRepository(self.db)
        self.user_repo = UserRepository(self.db)
        self.routing_repo = RoutingRepository(self.db)
        self.routing_repo.ensure_defaults()
        self.mixer_repo = MixerRepository(self.db)
        self.cue_repo = CueRepository(self.db)
        self.processed_repo = ProcessedRequestRepository(self.db)
        self.file_repo = FileCacheRepository(self.db)

        secret = self.settings.get("auth.secret")
        if not secret:
            secret = secrets.token_urlsafe(48)
            self.settings.set("auth.secret", secret)
        self.sessions = SessionManager(secret)

        self.events = EventHub()
        self.engine = AudioEngine(
            backend=config.audio_backend,
            sample_rate=int(self.settings.get("audio.sample_rate", 48000)),
            blocksize=int(self.settings.get("audio.blocksize", 1024)),
        )
        self.decoder = AudioDecoder(config.ffmpeg)
        self.tts = TtsService(config.voices_dir, config.tmp_dir)
        self.apply_tts_settings()
        self.mixers = MixerManager()
        self.cues = CueExecutor(self.mixers)
        self.sync = ErmSync(self.settings, self.file_repo, self.processed_repo, config.cache_dir, self.notify_change)
        self.playback = PlaybackService(
            self.engine, self.decoder, self.tts, self.cues,
            PlaybackHooks(
                report_status=self.sync.report_status,
                resolve_file=self.sync.resolve_file,
                resolve_recording=self.sync.resolve_recording,
                gong_path=self.sync.gong_path,
                tts_params=self._tts_params,
                cue_rules=self.cue_repo.all,
                on_change=self.notify_change,
            ),
        )
        self.playback.parallel_lanes = bool(self.settings.get("playback.parallel_lanes", True))
        self.sync.enqueue = self.playback.enqueue
        self.sync.knows = self.playback.knows
        self.sync.now_playing = self.playback.now_playing

        self.commands = CommandBus()
        self.queries = QueryBus()
        register_command_handlers(self.commands, self)
        register_query_handlers(self.queries, self)
        self._meter_task: asyncio.Task | None = None
        self._change_pending = False

    # ------------------------------------------------------------------ helpers used by handlers

    def refresh_routing(self) -> None:
        self.engine.apply_routing(self.routing_repo.load())
        self.notify_change("routing")

    def apply_tts_settings(self) -> None:
        self.tts.settings = TtsSettings(
            engine=self.settings.get("tts.engine", "auto"),
            piper_binary=self.settings.get("tts.piper_binary", "piper"),
            espeak_binary=self.settings.get("tts.espeak_binary", "espeak-ng"),
            voice_overrides=self.settings.get("tts.voice_overrides", {}) or {},
        )

    def _tts_params(self) -> dict:
        tts = (self.sync.state or {}).get("tts") or (self.settings.get("erm.last_state") or {}).get("tts") or {}
        return {"language": tts.get("language") or "de-DE", "voice": tts.get("voice"), "rate": tts.get("rate") or 1.0}

    def silence_path(self) -> Path:
        path = self.config.cache_dir / "silence.wav"
        if not path.exists():
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(48000)
                w.writeframes(b"\x00\x00" * 4800)
        return path

    def notify_change(self, topic: str = "state") -> None:
        """Coalesce change notifications into one SSE 'changed' event per loop tick."""
        if self._change_pending:
            return
        self._change_pending = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._change_pending = False
            return

        def fire() -> None:
            self._change_pending = False
            self.events.publish("changed", {"topic": topic, "t": time.time()})

        loop.call_soon(fire)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.events.bind_loop(loop)
        self.engine.bind_loop(loop)
        ring().listeners.append(lambda e: self.events.publish_threadsafe("log", e))
        self.engine.apply_routing(self.routing_repo.load())
        await asyncio.to_thread(self.engine.start)
        await self.mixers.configure(self.mixer_repo.all())
        self.playback.start()
        self.sync.start()
        self._meter_task = asyncio.create_task(self._meter_loop(), name="aep-meters")
        self.processed_repo.prune()
        log.info("AutoEventPlay started – web interface on port %d", self.config.port)

    async def shutdown(self) -> None:
        if self._meter_task:
            self._meter_task.cancel()
        await self.sync.stop()
        await self.playback.stop()
        await self.mixers.close_all()
        await asyncio.to_thread(self.engine.stop)
        self.db.close()

    async def _meter_loop(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            if self.events.has_subscribers:
                self.events.publish("meters", self.engine.read_meters())
