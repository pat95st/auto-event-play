"""Background synchronisation with the eventrentmanager instance.

* polls ``GET /api/player/v1/state`` (heartbeat + queue),
* keeps the audio files and the gong cached locally (by ``version``) so
  playback works instantly even on a shaky network,
* hands new queue items to the playback service and reports status back
  (status reports are retried until they reach the server).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from ...domain.model import PlaybackItem
from ..repositories import FileCacheRepository, ProcessedRequestRepository, SettingsRepository
from .client import ErmClient, ErmError, ErmUnauthorized, normalize_base_url
from ...i18n import Msg, text_of

log = logging.getLogger("aep.erm")

SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ErmSync:
    def __init__(self, settings: SettingsRepository, files: FileCacheRepository, processed: ProcessedRequestRepository,
                 cache_dir: Path, on_change: Callable[[], None]):
        self.settings = settings
        self.files = files
        self.processed = processed
        self.cache_dir = cache_dir
        self.on_change = on_change
        self.enqueue: Callable[[PlaybackItem], bool] | None = None
        self.knows: Callable[[str], bool] | None = None
        self.now_playing: Callable[[], str] = lambda: ""
        self._task: asyncio.Task | None = None
        self._file_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._downloads: dict[str, asyncio.Lock] = {}
        self._status_retry: list[tuple[str, str, str | None, int]] = []
        self.state: dict[str, Any] = {}
        self.status: dict[str, Any] = {"online": False, "message": "not paired", "last_ok": None, "last_error": None}

    # ------------------------------------------------------------------ config

    @property
    def base_url(self) -> str:
        return normalize_base_url(self.settings.get("erm.base_url", "") or "")

    @property
    def token(self) -> str | None:
        return self.settings.get("erm.token") or None

    @property
    def verify_tls(self) -> bool:
        return bool(self.settings.get("erm.verify_tls", True))

    def client(self) -> ErmClient:
        return ErmClient(self.base_url, self.token, self.verify_tls)

    @property
    def paired(self) -> bool:
        return bool(self.base_url and self.token)

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="aep-erm-sync")

    async def stop(self) -> None:
        for t in (self._task, self._file_task):
            if t:
                t.cancel()
        await asyncio.gather(*(t for t in (self._task, self._file_task) if t), return_exceptions=True)

    async def _loop(self) -> None:
        backoff = 2.0
        while True:
            interval = 2.0
            if not self.paired:
                self._set_status(False, Msg("not paired") if not self.base_url else Msg("no device token – please pair"))
                interval = 3.0
            else:
                try:
                    await self.poll_once()
                    interval = float(self.state.get("poll_interval_seconds") or 2)
                    backoff = 2.0
                except ErmUnauthorized as exc:
                    self._set_status(False, Msg("Token rejected: {error} – please pair again", error=text_of(exc)), error=True)
                    interval = 15.0
                except ErmError as exc:
                    self._set_status(False, text_of(exc), error=True)
                    interval = backoff
                    backoff = min(30.0, backoff * 2)
                except Exception as exc:  # pragma: no cover
                    log.exception("Synchronisation failed")
                    self._set_status(False, Msg("Error: {error}", error=text_of(exc)), error=True)
                    interval = backoff
                    backoff = min(30.0, backoff * 2)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.5, interval))
            except asyncio.TimeoutError:
                pass

    def _set_status(self, online: bool, message: str, error: bool = False) -> None:
        changed = self.status.get("online") != online or self.status.get("message") != message
        self.status["online"] = online
        self.status["message"] = message
        if online:
            self.status["last_ok"] = time.time()
        if error:
            self.status["last_error"] = message
        if changed:
            (log.info if online or not error else log.warning)("eventrentmanager: %s", message)
            self.on_change()

    # ------------------------------------------------------------------ polling

    async def poll_once(self) -> None:
        client = self.client()
        await self._flush_status_retries(client)
        state = await client.state(self.now_playing())
        self.state = state
        rental = state.get("rental")
        self._set_status(True, Msg("connected · {event}", event=rental["name"]) if rental else Msg("connected · no active event"))
        self.settings.set("erm.player", state.get("player"))
        self.settings.set("erm.last_state", {k: state.get(k) for k in ("rental", "tts", "gong", "files", "scheduled", "minute_programs", "server_time")})
        if self._file_task is None or self._file_task.done():
            self._file_task = asyncio.create_task(self.sync_files(state))
        for q in state.get("queue") or []:
            qid = str(q.get("id"))
            if not qid or (self.knows and self.knows(qid)) or self.processed.is_known(qid):
                continue
            try:
                item = PlaybackItem.from_queue(q)
            except (ValueError, KeyError) as exc:
                log.warning("Unknown request %s: %s", qid, exc)
                continue
            if self.enqueue:
                self.enqueue(item)

    # ------------------------------------------------------------------ files

    def _file_path(self, file_id: str, version: str) -> Path:
        return self.cache_dir / "files" / SAFE.sub("_", f"{file_id}_{version}")

    async def sync_files(self, state: dict[str, Any]) -> None:
        client = self.client()
        manifest = {str(f["id"]): f for f in state.get("files") or []}
        cached = self.files.all()
        changed = False
        for fid, f in manifest.items():
            entry = cached.get(fid)
            path = Path(entry["path"]) if entry else None
            if entry and entry["version"] == f.get("version") and path and path.exists():
                if entry.get("title") != f.get("title") or entry.get("kind") != f.get("kind"):
                    self.files.update_meta(fid, f.get("kind"), f.get("title"))
                continue
            try:
                await self._download_file(client, f)
                changed = True
            except ErmError as exc:
                log.warning("Could not download file \"%s\": %s", f.get("title"), exc)
        # files that are no longer in the manifest are removed – but only when an event is active
        # (an empty manifest during a network hiccup must not wipe the cache)
        if state.get("rental") is not None:
            for fid, entry in cached.items():
                if fid not in manifest and not fid.startswith("__"):
                    Path(entry["path"]).unlink(missing_ok=True)
                    self.files.delete(fid)
                    changed = True
                    log.info("File removed from cache: %s", entry.get("title"))
        gong = state.get("gong")
        if gong:
            entry = cached.get("__gong__")
            if not entry or entry["version"] != gong.get("version") or not Path(entry["path"]).exists():
                try:
                    target = self._file_path("gong", str(gong.get("version")))
                    size = await client.download(gong["url"], target)
                    if entry:
                        Path(entry["path"]).unlink(missing_ok=True)
                    self.files.save("__gong__", str(gong.get("version")), "gong", "Gong", None, gong.get("mime"), size, str(target))
                    log.info("Custom gong downloaded")
                    changed = True
                except ErmError as exc:
                    log.warning("Could not download gong: %s", exc)
        elif "__gong__" in cached:
            Path(cached["__gong__"]["path"]).unlink(missing_ok=True)
            self.files.delete("__gong__")
            changed = True
        if changed:
            self.on_change()

    async def _download_file(self, client: ErmClient, f: dict[str, Any]) -> Path:
        fid = str(f["id"])
        lock = self._downloads.setdefault(fid, asyncio.Lock())
        async with lock:
            entry = self.files.get(fid)
            if entry and entry["version"] == f.get("version") and Path(entry["path"]).exists():
                return Path(entry["path"])
            target = self._file_path(fid, str(f.get("version")))
            log.info("Downloading file \"%s\" (%s)", f.get("title"), f.get("filename"))
            size = await client.download(f.get("url") or f"/api/player/v1/files/{fid}", target)
            if entry and entry["path"] != str(target):
                Path(entry["path"]).unlink(missing_ok=True)
            self.files.save(fid, str(f.get("version")), f.get("kind"), f.get("title"), f.get("filename"), f.get("mime"), size, str(target))
            return target

    async def resolve_file(self, item: PlaybackItem) -> Path:
        if not item.file_id:
            raise ErmError("Request without file")
        entry = self.files.get(item.file_id)
        if entry and Path(entry["path"]).exists():
            return Path(entry["path"])
        meta = next((f for f in self.state.get("files") or [] if str(f.get("id")) == item.file_id), None)
        if meta is None:
            meta = {"id": item.file_id, "version": "direct", "url": item.file_url, "title": item.label}
        return await self._download_file(self.client(), meta)

    async def resolve_recording(self, item: PlaybackItem) -> Path:
        if not item.recording_url:
            raise ErmError("Request without recording")
        target = self.cache_dir / SAFE.sub("_", f"recording_{item.id}")
        if not target.exists():
            await self.client().download(item.recording_url, target)
        return target

    def gong_path(self) -> Path | None:
        entry = self.files.get("__gong__")
        return Path(entry["path"]) if entry else None

    # ------------------------------------------------------------------ status reports

    async def report_status(self, item: PlaybackItem, status: str, error: str | None) -> None:
        if status == "playing":
            self.processed.start(item.id, item.kind.value, item.label)
        else:
            self.processed.finish(item.id, status, error)
        if not item.status_url:
            return
        try:
            await self.client().report_status(item.status_url, status, error)
        except ErmError as exc:
            if exc.status in (404, 422):
                log.warning("Status \"%s\" for request %s rejected: %s", status, item.id, exc)
                return
            log.warning("Status \"%s\" for request %s not reported (%s) – will retry", status, item.id, exc)
            self._status_retry.append((item.status_url, status, error, 0))

    async def _flush_status_retries(self, client: ErmClient) -> None:
        pending, self._status_retry = self._status_retry, []
        for url, status, error, attempts in pending:
            try:
                await client.report_status(url, status, error)
            except ErmError as exc:
                if exc.status not in (404, 422) and attempts < 20:
                    self._status_retry.append((url, status, error, attempts + 1))

    # ------------------------------------------------------------------ pairing

    async def pair(self, base_url: str, code: str, name: str) -> dict[str, Any]:
        url = normalize_base_url(base_url)
        client = ErmClient(url, self.token if url == self.base_url else None, self.verify_tls)
        result = await client.pair(code, name)
        if result.get("token"):
            self.settings.set("erm.token", result["token"])
        elif not self.token or url != self.base_url:
            raise ErmError("Server did not return a device token")
        self.settings.set("erm.base_url", url)
        self.settings.set("erm.player", result.get("player"))
        self.settings.set("erm.paired_rental", result.get("rental"))
        log.info("Paired with %s (%s) for \"%s\"", url, (result.get("player") or {}).get("name"),
                 (result.get("rental") or {}).get("name"))
        self.wake()
        return result

    def unpair(self) -> None:
        self.settings.delete("erm.token")
        self.settings.delete("erm.player")
        self.settings.delete("erm.paired_rental")
        self.state = {}
        log.info("Pairing with eventrentmanager removed")
        self.wake()
