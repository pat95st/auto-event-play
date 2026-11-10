"""Queries (read side). Handlers return JSON-ready dicts."""

from __future__ import annotations

from dataclasses import dataclass

from .bus import Query


@dataclass
class GetDashboard(Query):
    """Mixer page: channel strips, playback state, connection status."""


@dataclass
class GetRouting(Query):
    pass


@dataclass
class GetAudioSetup(Query):
    pass


@dataclass
class ListMixers(Query):
    pass


@dataclass
class GetMixerTypes(Query):
    pass


@dataclass
class ListCueRules(Query):
    pass


@dataclass
class GetErmStatus(Query):
    pass


@dataclass
class GetPlayback(Query):
    pass


@dataclass
class GetLogs(Query):
    after: int = 0
    level: str = "DEBUG"
    text: str = ""
    limit: int = 500


@dataclass
class GetSystemInfo(Query):
    pass
