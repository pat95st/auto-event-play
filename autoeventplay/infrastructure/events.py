"""In-process event hub feeding the Server-Sent-Events stream of the web UI."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger("aep.events")


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subscribers)

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def publish(self, event: str, data: Any) -> None:
        """Publish from the event loop thread."""
        if not self._subscribers:
            return
        payload = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # slow client: drop the oldest entry
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass

    def publish_threadsafe(self, event: str, data: Any) -> None:
        """Publish from any thread (e.g. the log handler or audio callbacks)."""
        if self._loop is None or not self._subscribers:
            return
        try:
            self._loop.call_soon_threadsafe(self.publish, event, data)
        except RuntimeError:
            pass
