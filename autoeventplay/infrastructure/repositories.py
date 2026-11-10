"""Repositories: persistence of the write model in SQLite."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..domain.levels import clamp_db
from ..domain.model import (
    INPUT_DEFS,
    CueAction,
    CuePhase,
    CueRule,
    InputChannel,
    MixerConfig,
    MuteAction,
    OutputChannel,
    RoutingState,
    Target,
    new_id,
)
from .db import Database


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _eq(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class SettingsRepository:
    """Key/value settings stored as JSON."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE key = ?", (key,))

    def get_group(self, prefix: str) -> dict[str, Any]:
        rows = self.db.fetchall("SELECT key, value FROM settings WHERE key LIKE ?", (prefix + "%",))
        return {r["key"][len(prefix):]: json.loads(r["value"]) for r in rows}


class UserRepository:
    def __init__(self, db: Database):
        self.db = db

    def count(self) -> int:
        row = self.db.fetchone("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def get_hash(self, username: str) -> str | None:
        row = self.db.fetchone("SELECT password_hash FROM users WHERE username = ?", (username,))
        return row["password_hash"] if row else None

    def save(self, username: str, password_hash: str) -> None:
        self.db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash",
            (username, password_hash, now_iso()),
        )

    def list(self) -> list[str]:
        return [r["username"] for r in self.db.fetchall("SELECT username FROM users ORDER BY username")]


class RoutingRepository:
    """Inputs, outputs and crosspoints of the routing matrix."""

    def __init__(self, db: Database):
        self.db = db

    def ensure_defaults(self) -> None:
        for d in INPUT_DEFS:
            self.db.execute("INSERT OR IGNORE INTO input_channels (id, name, level_db, muted) VALUES (?, ?, 0, 0)", (d.id, d.name))
        if not self.db.fetchone("SELECT id FROM output_channels LIMIT 1"):
            with self.db.transaction() as c:
                c.execute("INSERT INTO output_channels (id, name, device, channel, sort) VALUES ('out1', 'Out 1', 'default', 0, 0)")
                c.execute("INSERT INTO output_channels (id, name, device, channel, sort) VALUES ('out2', 'Out 2', 'default', 1, 1)")
                for inp, out in (("media_l", "out1"), ("media_r", "out2"), ("tts", "out1"), ("tts", "out2"), ("recording", "out1"), ("recording", "out2")):
                    c.execute("INSERT INTO crosspoints (input_id, output_id) VALUES (?, ?)", (inp, out))

    def load(self) -> RoutingState:
        order = {d.id: i for i, d in enumerate(INPUT_DEFS)}
        inputs = [
            InputChannel(r["id"], r["name"], float(r["level_db"]), bool(r["muted"]), _eq(r.get("eq")))
            for r in self.db.fetchall("SELECT * FROM input_channels")
            if r["id"] in order
        ]
        inputs.sort(key=lambda i: order[i.id])
        outputs = [
            OutputChannel(r["id"], r["name"], r["device"], int(r["channel"]), float(r["level_db"]), bool(r["muted"]), int(r["sort"]),
                          _eq(r.get("eq")))
            for r in self.db.fetchall("SELECT * FROM output_channels ORDER BY sort, id")
        ]
        cps = {(r["input_id"], r["output_id"]) for r in self.db.fetchall("SELECT * FROM crosspoints")}
        return RoutingState(inputs, outputs, cps)

    def set_input(self, input_id: str, level_db: float | None = None, muted: bool | None = None, name: str | None = None) -> None:
        if level_db is not None:
            self.db.execute("UPDATE input_channels SET level_db = ? WHERE id = ?", (clamp_db(level_db), input_id))
        if muted is not None:
            self.db.execute("UPDATE input_channels SET muted = ? WHERE id = ?", (int(muted), input_id))
        if name is not None:
            self.db.execute("UPDATE input_channels SET name = ? WHERE id = ?", (name, input_id))

    def set_output(self, output_id: str, level_db: float | None = None, muted: bool | None = None, name: str | None = None) -> None:
        if level_db is not None:
            self.db.execute("UPDATE output_channels SET level_db = ? WHERE id = ?", (clamp_db(level_db), output_id))
        if muted is not None:
            self.db.execute("UPDATE output_channels SET muted = ? WHERE id = ?", (int(muted), output_id))
        if name is not None:
            self.db.execute("UPDATE output_channels SET name = ? WHERE id = ?", (name, output_id))

    def set_eq(self, channel_type: str, channel_id: str, eq: dict[str, Any]) -> None:
        table = "input_channels" if channel_type == "input" else "output_channels"
        self.db.execute(f"UPDATE {table} SET eq = ? WHERE id = ?", (json.dumps(eq), channel_id))

    def set_crosspoint(self, input_id: str, output_id: str, enabled: bool) -> None:
        if enabled:
            self.db.execute("INSERT OR IGNORE INTO crosspoints (input_id, output_id) VALUES (?, ?)", (input_id, output_id))
        else:
            self.db.execute("DELETE FROM crosspoints WHERE input_id = ? AND output_id = ?", (input_id, output_id))

    def replace_outputs(self, outputs: list[dict[str, Any]]) -> None:
        """Replace the output list (device layout). Keeps level/mute/routing of outputs whose id stays."""
        existing = {o.id: o for o in self.load().outputs}
        keep_ids = {o["id"] for o in outputs if o.get("id")}
        with self.db.transaction() as c:
            for oid in existing:
                if oid not in keep_ids:
                    c.execute("DELETE FROM output_channels WHERE id = ?", (oid,))
            for i, o in enumerate(outputs):
                oid = o.get("id") or new_id()[:8]
                old = existing.get(oid)
                c.execute(
                    "INSERT INTO output_channels (id, name, device, channel, level_db, muted, sort) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET name = excluded.name, device = excluded.device, channel = excluded.channel, sort = excluded.sort",
                    (oid, o.get("name") or f"Out {i + 1}", o.get("device") or "default", int(o.get("channel", i)),
                     old.level_db if old else 0.0, int(old.muted) if old else 0, i),
                )


class MixerRepository:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _hydrate(r: dict[str, Any]) -> MixerConfig:
        return MixerConfig(r["id"], r["name"], r["type"], r["host"], r["port"], bool(r["enabled"]), json.loads(r["options"] or "{}"))

    def all(self) -> list[MixerConfig]:
        return [self._hydrate(r) for r in self.db.fetchall("SELECT * FROM mixers ORDER BY name")]

    def get(self, mixer_id: str) -> MixerConfig | None:
        r = self.db.fetchone("SELECT * FROM mixers WHERE id = ?", (mixer_id,))
        return self._hydrate(r) if r else None

    def save(self, m: MixerConfig) -> None:
        self.db.execute(
            "INSERT INTO mixers (id, name, type, host, port, enabled, options) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, type = excluded.type, host = excluded.host, "
            "port = excluded.port, enabled = excluded.enabled, options = excluded.options",
            (m.id, m.name, m.type, m.host, m.port, int(m.enabled), json.dumps(m.options)),
        )

    def delete(self, mixer_id: str) -> None:
        self.db.execute("DELETE FROM mixers WHERE id = ?", (mixer_id,))


class CueRepository:
    def __init__(self, db: Database):
        self.db = db

    def all(self) -> list[CueRule]:
        rules = {
            r["id"]: CueRule(
                r["id"], r["name"], bool(r["enabled"]), r["match_kind"], r["match_file_id"], r["match_title"],
                int(r["pre_delay_ms"]), int(r["priority"]), [],
            )
            for r in self.db.fetchall("SELECT * FROM cue_rules ORDER BY priority, name")
        }
        for a in self.db.fetchall("SELECT * FROM cue_actions ORDER BY sort"):
            rule = rules.get(a["rule_id"])
            if rule is None:
                continue
            rule.actions.append(CueAction(
                id=a["id"],
                mixer_id=a["mixer_id"],
                target=Target.from_dict(json.loads(a["target"])),
                phase=CuePhase(a["phase"]),
                mute=MuteAction(a["mute"]),
                level_db=a["level_db"],
                fade_ms=int(a["fade_ms"]),
                restore=bool(a["restore"]),
                restore_level_db=a["restore_level_db"],
                sort=int(a["sort"]),
            ))
        return list(rules.values())

    def get(self, rule_id: str) -> CueRule | None:
        return next((r for r in self.all() if r.id == rule_id), None)

    def save(self, rule: CueRule) -> None:
        with self.db.transaction() as c:
            c.execute(
                "INSERT INTO cue_rules (id, name, enabled, match_kind, match_file_id, match_title, pre_delay_ms, priority) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name, enabled = excluded.enabled, "
                "match_kind = excluded.match_kind, match_file_id = excluded.match_file_id, match_title = excluded.match_title, "
                "pre_delay_ms = excluded.pre_delay_ms, priority = excluded.priority",
                (rule.id, rule.name, int(rule.enabled), rule.match_kind, rule.match_file_id, rule.match_title, rule.pre_delay_ms, rule.priority),
            )
            c.execute("DELETE FROM cue_actions WHERE rule_id = ?", (rule.id,))
            for i, a in enumerate(rule.actions):
                c.execute(
                    "INSERT INTO cue_actions (id, rule_id, mixer_id, target, phase, mute, level_db, fade_ms, restore, restore_level_db, sort) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (a.id, rule.id, a.mixer_id, json.dumps(a.target.to_dict()), a.phase.value, a.mute.value, a.level_db,
                     a.fade_ms, int(a.restore), a.restore_level_db, i),
                )

    def delete(self, rule_id: str) -> None:
        self.db.execute("DELETE FROM cue_rules WHERE id = ?", (rule_id,))


class ProcessedRequestRepository:
    """Remembers queue items already handled so nothing is played twice."""

    def __init__(self, db: Database):
        self.db = db

    def is_known(self, request_id: str) -> bool:
        return self.db.fetchone("SELECT id FROM processed_requests WHERE id = ?", (request_id,)) is not None

    def start(self, request_id: str, kind: str, label: str) -> None:
        self.db.execute(
            "INSERT INTO processed_requests (id, kind, label, status, started_at) VALUES (?, ?, ?, 'playing', ?) "
            "ON CONFLICT(id) DO UPDATE SET status = 'playing', started_at = excluded.started_at",
            (request_id, kind, label, now_iso()),
        )

    def finish(self, request_id: str, status: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE processed_requests SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, error, now_iso(), request_id),
        )

    def mark(self, request_id: str, kind: str, label: str, status: str, error: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO processed_requests (id, kind, label, status, error, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status = excluded.status, error = excluded.error, finished_at = excluded.finished_at",
            (request_id, kind, label, status, error, now_iso(), now_iso()),
        )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.fetchall("SELECT * FROM processed_requests ORDER BY COALESCE(finished_at, started_at) DESC LIMIT ?", (limit,))

    def prune(self, keep: int = 2000) -> None:
        self.db.execute(
            "DELETE FROM processed_requests WHERE id NOT IN (SELECT id FROM processed_requests ORDER BY started_at DESC LIMIT ?)",
            (keep,),
        )


class FileCacheRepository:
    def __init__(self, db: Database):
        self.db = db

    def all(self) -> dict[str, dict[str, Any]]:
        return {r["file_id"]: r for r in self.db.fetchall("SELECT * FROM file_cache ORDER BY kind, title")}

    def get(self, file_id: str) -> dict[str, Any] | None:
        return self.db.fetchone("SELECT * FROM file_cache WHERE file_id = ?", (file_id,))

    def save(self, file_id: str, version: str, kind: str | None, title: str | None, filename: str | None,
             mime: str | None, size: int | None, path: str) -> None:
        self.db.execute(
            "INSERT INTO file_cache (file_id, version, kind, title, filename, mime, size, path, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(file_id) DO UPDATE SET version = excluded.version, kind = excluded.kind, title = excluded.title, "
            "filename = excluded.filename, mime = excluded.mime, size = excluded.size, path = excluded.path, updated_at = excluded.updated_at",
            (file_id, version, kind, title, filename, mime, size, path, now_iso()),
        )

    def update_meta(self, file_id: str, kind: str | None, title: str | None) -> None:
        self.db.execute("UPDATE file_cache SET kind = ?, title = ? WHERE file_id = ?", (kind, title, file_id))

    def delete(self, file_id: str) -> None:
        self.db.execute("DELETE FROM file_cache WHERE file_id = ?", (file_id,))
