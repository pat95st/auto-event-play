"""Query handlers (read side)."""

from __future__ import annotations

import platform
import shutil
import socket
import time
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..domain.eq import is_active, normalize_eq
from ..domain.levels import db_to_fader
from ..domain.model import INPUT_DEFS
from ..infrastructure.audio.engine import AudioEngine
from ..infrastructure.audio.tts import DEFAULT_VOICES
from ..infrastructure.logs import ring
from ..infrastructure.mixers.base import driver_class, driver_types
from . import queries as q
from .bus import QueryBus

if TYPE_CHECKING:
    from ..container import Services


def register_query_handlers(bus: QueryBus, s: "Services") -> None:
    def routing_view() -> dict[str, Any]:
        state = s.routing_repo.load()
        bus_of = {d.id: d.bus.value for d in INPUT_DEFS}
        return {
            "inputs": [{"id": i.id, "name": i.name, "level_db": i.level_db, "muted": i.muted, "bus": bus_of.get(i.id),
                        "fader": db_to_fader(i.level_db), "eq": normalize_eq(i.eq), "eq_active": is_active(i.eq)} for i in state.inputs],
            "outputs": [{"id": o.id, "name": o.name, "device": o.device, "channel": o.channel, "level_db": o.level_db,
                         "muted": o.muted, "fader": db_to_fader(o.level_db), "eq": normalize_eq(o.eq), "eq_active": is_active(o.eq)}
                        for o in state.outputs],
            "crosspoints": sorted([list(cp) for cp in state.crosspoints]),
        }

    def erm_status() -> dict[str, Any]:
        last = s.settings.get("erm.last_state") or {}
        return {
            "base_url": s.settings.get("erm.base_url", ""),
            "device_name": s.settings.get("erm.device_name", "AutoEventPlay"),
            "verify_tls": bool(s.settings.get("erm.verify_tls", True)),
            "paired": s.sync.paired,
            "token_hint": _hint(s.settings.get("erm.token")),
            "player": s.settings.get("erm.player"),
            "status": dict(s.sync.status),
            "rental": (s.sync.state or last).get("rental"),
            "tts": (s.sync.state or last).get("tts"),
            "scheduled": (s.sync.state or last).get("scheduled") or [],
            "minute_programs": (s.sync.state or last).get("minute_programs") or [],
            "server_time": (s.sync.state or last).get("server_time"),
        }

    def files_view() -> list[dict[str, Any]]:
        return [
            {"id": f["file_id"], "title": f["title"], "kind": f["kind"], "filename": f["filename"], "size": f["size"],
             "version": f["version"], "updated_at": f["updated_at"]}
            for f in s.file_repo.all().values() if not f["file_id"].startswith("__")
        ]

    def playback_view() -> dict[str, Any]:
        snap = s.playback.snapshot()
        snap["recent"] = s.processed_repo.recent(30)
        return snap

    def dashboard(_: q.GetDashboard) -> dict[str, Any]:
        return {
            "routing": routing_view(),
            "playback": playback_view(),
            "erm": erm_status(),
            "audio": s.engine.status(),
            "mixers": [{"id": m.id, "name": m.name, "type": m.type, "enabled": m.enabled, "status": s.mixers.statuses().get(m.id)}
                       for m in s.mixer_repo.all()],
            "files": files_view(),
        }

    def get_routing(_: q.GetRouting) -> dict[str, Any]:
        return routing_view()

    def audio_setup(_: q.GetAudioSetup) -> dict[str, Any]:
        tts = s.tts
        return {
            "devices": AudioEngine.list_devices(),
            "status": s.engine.status(),
            "outputs": routing_view()["outputs"],
            "sample_rate": s.engine.sample_rate,
            "blocksize": s.engine.blocksize,
            "parallel_lanes": s.playback.parallel_lanes,
            "tts": {
                "engine": tts.settings.engine,
                "piper_binary": tts.settings.piper_binary,
                "espeak_binary": tts.settings.espeak_binary,
                "voice_overrides": tts.settings.voice_overrides or {},
                "piper_available": tts.piper_available(),
                "espeak_available": tts.espeak_available(),
                "voices": tts.installed_voices(),
                "default_voices": DEFAULT_VOICES,
            },
            "ffmpeg": s.decoder.available,
        }

    def list_mixers(_: q.ListMixers) -> dict[str, Any]:
        statuses = s.mixers.statuses()
        items = []
        for m in s.mixer_repo.all():
            try:
                info = driver_class(m.type).info(m).to_dict()
            except Exception:
                info = None
            items.append({**m.to_dict(), "status": statuses.get(m.id), "driver": info})
        return {"mixers": items}

    def mixer_types(_: q.GetMixerTypes) -> dict[str, Any]:
        return {"types": [t.to_dict() for t in driver_types()]}

    def list_cues(_: q.ListCueRules) -> dict[str, Any]:
        rules = []
        for r in s.cue_repo.all():
            d = r.to_dict()
            for a in d["actions"]:
                a["label"] = s.mixers.describe(a["mixer_id"], _target(a["target"]))
            rules.append(d)
        return {"rules": rules, "files": files_view()}

    def get_erm(_: q.GetErmStatus) -> dict[str, Any]:
        return {**erm_status(), "files": files_view()}

    def get_playback(_: q.GetPlayback) -> dict[str, Any]:
        return playback_view()

    def get_logs(query: q.GetLogs) -> dict[str, Any]:
        entries = ring().entries(query.after, query.level, query.text, query.limit)
        return {"entries": entries, "last": entries[-1]["seq"] if entries else query.after}

    def system_info(_: q.GetSystemInfo) -> dict[str, Any]:
        disk = shutil.disk_usage(str(s.config.data_dir))
        return {
            "version": __version__,
            "hostname": socket.gethostname(),
            "ip_addresses": _ips(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "uptime_s": int(time.time() - s.started_at),
            "data_dir": str(s.config.data_dir),
            "disk_free_mb": disk.free // (1024 * 1024),
            "users": s.user_repo.list(),
            "commands": bus_names(s),
        }

    for message, handler in [
        (q.GetDashboard, dashboard), (q.GetRouting, get_routing), (q.GetAudioSetup, audio_setup),
        (q.ListMixers, list_mixers), (q.GetMixerTypes, mixer_types), (q.ListCueRules, list_cues),
        (q.GetErmStatus, get_erm), (q.GetPlayback, get_playback), (q.GetLogs, get_logs),
        (q.GetSystemInfo, system_info),
    ]:
        bus.register(message, handler)


def bus_names(s: "Services") -> dict[str, list[str]]:
    return {"commands": s.commands.registered(), "queries": s.queries.registered()}


def _target(d: dict[str, Any]):  # noqa: ANN202
    from ..domain.model import Target

    return Target.from_dict(d)


def _hint(token: str | None) -> str | None:
    if not token:
        return None
    return token[:8] + "…" + token[-4:]


def _ips() -> list[str]:
    ips: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            ips.add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))
