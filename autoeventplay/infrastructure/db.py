"""SQLite access with simple, ordered schema migrations."""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("aep.db")

MIGRATIONS: list[str] = [
    # 1: base schema
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE users (
        username      TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        created_at    TEXT NOT NULL
    );
    CREATE TABLE input_channels (
        id       TEXT PRIMARY KEY,
        name     TEXT NOT NULL,
        level_db REAL NOT NULL DEFAULT 0,
        muted    INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE output_channels (
        id       TEXT PRIMARY KEY,
        name     TEXT NOT NULL,
        device   TEXT NOT NULL,
        channel  INTEGER NOT NULL,
        level_db REAL NOT NULL DEFAULT 0,
        muted    INTEGER NOT NULL DEFAULT 0,
        sort     INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE crosspoints (
        input_id  TEXT NOT NULL,
        output_id TEXT NOT NULL REFERENCES output_channels(id) ON DELETE CASCADE,
        PRIMARY KEY (input_id, output_id)
    );
    CREATE TABLE mixers (
        id      TEXT PRIMARY KEY,
        name    TEXT NOT NULL,
        type    TEXT NOT NULL,
        host    TEXT NOT NULL,
        port    INTEGER,
        enabled INTEGER NOT NULL DEFAULT 1,
        options TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE cue_rules (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        enabled       INTEGER NOT NULL DEFAULT 1,
        match_kind    TEXT NOT NULL DEFAULT 'any',
        match_file_id TEXT,
        match_title   TEXT,
        pre_delay_ms  INTEGER NOT NULL DEFAULT 0,
        priority      INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE cue_actions (
        id               TEXT PRIMARY KEY,
        rule_id          TEXT NOT NULL REFERENCES cue_rules(id) ON DELETE CASCADE,
        mixer_id         TEXT NOT NULL,
        target           TEXT NOT NULL,
        phase            TEXT NOT NULL DEFAULT 'start',
        mute             TEXT NOT NULL DEFAULT 'unchanged',
        level_db         REAL,
        fade_ms          INTEGER NOT NULL DEFAULT 0,
        restore          INTEGER NOT NULL DEFAULT 0,
        restore_level_db REAL,
        sort             INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE processed_requests (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        label       TEXT,
        status      TEXT NOT NULL,
        error       TEXT,
        started_at  TEXT,
        finished_at TEXT
    );
    CREATE TABLE file_cache (
        file_id    TEXT PRIMARY KEY,
        version    TEXT NOT NULL,
        kind       TEXT,
        title      TEXT,
        filename   TEXT,
        mime       TEXT,
        size       INTEGER,
        path       TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # 2: channel equalizers
    """
    ALTER TABLE input_channels ADD COLUMN eq TEXT;
    ALTER TABLE output_channels ADD COLUMN eq TEXT
    """,
]


class Database:
    """Thread-safe wrapper around one SQLite connection (WAL mode)."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")

    def migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for index, sql in enumerate(MIGRATIONS, start=1):
                if index <= version:
                    continue
                log.info("Applying database migration %d", index)
                self._conn.execute("BEGIN")
                try:
                    for statement in _split_sql(sql):
                        self._conn.execute(statement)
                    self._conn.execute(f"PRAGMA user_version = {index}")
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def fetchall(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def fetchone(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _split_sql(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
