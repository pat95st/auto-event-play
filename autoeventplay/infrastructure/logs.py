"""Logging: rotating log file plus an in-memory ring buffer for the web UI."""

from __future__ import annotations

import collections
import logging
import logging.handlers
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

LOG_FILE_NAME = "autoeventplay.log"


class RingBufferHandler(logging.Handler):
    """Keeps the last N records with a monotonically increasing sequence number."""

    def __init__(self, capacity: int = 3000):
        super().__init__()
        self._records: collections.deque[dict[str, Any]] = collections.deque(maxlen=capacity)
        self._seq = 0
        self._lock2 = threading.Lock()
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            with self._lock2:
                self._seq += 1
                entry = {
                    "seq": self._seq,
                    "time": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                }
                self._records.append(entry)
            for listener in list(self.listeners):
                try:
                    listener(entry)
                except Exception:  # pragma: no cover - never break logging
                    pass
        except Exception:  # pragma: no cover
            self.handleError(record)

    def entries(self, after: int = 0, min_level: str = "DEBUG", text: str = "", limit: int = 500) -> list[dict[str, Any]]:
        threshold = logging.getLevelName(min_level.upper()) if isinstance(logging.getLevelName(min_level.upper()), int) else 0
        needle = text.lower()
        with self._lock2:
            items = [
                e for e in self._records
                if e["seq"] > after
                and logging.getLevelName(e["level"]) >= threshold
                and (not needle or needle in e["message"].lower() or needle in e["logger"].lower())
            ]
        return items[-limit:]


_ring: RingBufferHandler | None = None


def setup_logging(log_dir: Path, level: str = "INFO") -> RingBufferHandler:
    global _ring
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(log_dir / LOG_FILE_NAME, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level.upper())
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(level.upper())
    root.addHandler(console)

    _ring = RingBufferHandler()
    _ring.setLevel(logging.DEBUG)
    root.addHandler(_ring)

    for noisy in ("uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return _ring


def ring() -> RingBufferHandler:
    global _ring
    if _ring is None:
        _ring = RingBufferHandler()
        logging.getLogger().addHandler(_ring)
    return _ring


def log_files(log_dir: Path) -> list[Path]:
    return sorted(log_dir.glob(LOG_FILE_NAME + "*"), key=lambda p: p.name)
