"""Domain model: routing (inputs, outputs, matrix), mixer consoles, cue rules,
playback items. Plain dataclasses without infrastructure dependencies."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- routing


class SourceBus(str, Enum):
    """The three playback sources of the player."""

    MEDIA = "media"          # jingles and other audio files (stereo)
    TTS = "tts"              # automatic announcements (text-to-speech, mono)
    RECORDING = "recording"  # recorded announcements (microphone, mono)


@dataclass(frozen=True)
class InputDef:
    id: str
    name: str
    bus: SourceBus
    bus_channel: int


# Fixed matrix inputs. Media left and right are separately routable so that
# e.g. a mono sum of the media can go out on one output and the speech sum on
# another.
INPUT_DEFS: tuple[InputDef, ...] = (
    InputDef("media_l", "Media L", SourceBus.MEDIA, 0),
    InputDef("media_r", "Media R", SourceBus.MEDIA, 1),
    InputDef("tts", "TTS", SourceBus.TTS, 0),
    InputDef("recording", "Recording", SourceBus.RECORDING, 0),
)
INPUT_IDS = tuple(d.id for d in INPUT_DEFS)

# Input trim range (dB) – like the gain/trim knob at the top of a console channel.
TRIM_MIN_DB = -24.0
TRIM_MAX_DB = 24.0
# Order of EQ and compressor in a channel: "eq_dyn" = EQ before compressor (default), "dyn_eq" = compressor first.
PROCESSING_ORDERS = ("eq_dyn", "dyn_eq")


def normalize_trim(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return round(min(TRIM_MAX_DB, max(TRIM_MIN_DB, v)), 1)


def normalize_order(value: Any) -> str:
    return value if value in PROCESSING_ORDERS else "eq_dyn"
# default names of versions before the English UI (still treated as "not renamed")
LEGACY_INPUT_NAMES = {"media_l": "Medien L", "media_r": "Medien R", "recording": "Aufnahme"}


@dataclass
class InputChannel:
    id: str
    name: str
    level_db: float = 0.0
    muted: bool = False
    eq: dict[str, Any] | None = None   # see domain/eq.py
    dyn: dict[str, Any] | None = None  # compressor, see domain/dynamics.py
    trim_db: float = 0.0               # input trim, applied before EQ and compressor
    order: str = "eq_dyn"              # processing order, see PROCESSING_ORDERS


@dataclass
class OutputChannel:
    id: str
    name: str
    device: str          # sound device name (or "default")
    channel: int         # 0-based channel index on that device
    level_db: float = 0.0
    muted: bool = False
    sort: int = 0
    eq: dict[str, Any] | None = None   # see domain/eq.py
    dyn: dict[str, Any] | None = None  # compressor, see domain/dynamics.py
    order: str = "eq_dyn"              # processing order, see PROCESSING_ORDERS


@dataclass
class RoutingState:
    inputs: list[InputChannel]
    outputs: list[OutputChannel]
    # (input_id, output_id) pairs that are switched on
    crosspoints: set[tuple[str, str]] = field(default_factory=set)


# --------------------------------------------------------------------------- consoles


class TargetKind(str, Enum):
    """What on a console an action addresses. Drivers declare which kinds they support."""

    CHANNEL = "channel"        # input channel
    STEREO_IN = "stereo_in"    # stereo / aux / line input
    FX_RETURN = "fx_return"
    BUS = "bus"                # aux / mix bus master
    GROUP = "group"            # subgroup master
    FX_SEND = "fx_send"        # fx bus master
    MATRIX = "matrix"
    MAIN = "main"              # main LR / mono
    DCA = "dca"
    MUTE_GROUP = "mute_group"
    SEND = "send"              # channel -> bus send (number = channel, dest = bus)
    RAW = "raw"                # driver specific raw address/path


@dataclass
class Target:
    kind: TargetKind
    number: int | None = None
    dest: int | None = None
    raw: str | None = None

    def key(self) -> str:
        return f"{self.kind.value}:{self.number}:{self.dest}:{self.raw}"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "number": self.number, "dest": self.dest, "raw": self.raw}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Target":
        def _int(v: Any) -> int | None:
            return None if v in (None, "") else int(v)

        return cls(TargetKind(d.get("kind", "channel")), _int(d.get("number")), _int(d.get("dest")), d.get("raw") or None)


@dataclass
class MixerConfig:
    id: str
    name: str
    type: str
    host: str
    port: int | None = None
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- cues


class PlaybackKind(str, Enum):
    JINGLE = "jingle"
    AUDIO = "audio"
    TTS = "tts"
    RECORDING = "recording"
    STOP = "stop"


class MuteAction(str, Enum):
    UNCHANGED = "unchanged"
    MUTE = "mute"
    UNMUTE = "unmute"


class CuePhase(str, Enum):
    START = "start"  # before playback starts
    END = "end"      # after playback ended


@dataclass
class CueAction:
    id: str
    mixer_id: str
    target: Target
    phase: CuePhase = CuePhase.START
    mute: MuteAction = MuteAction.UNCHANGED
    level_db: float | None = None     # None = do not touch the level
    fade_ms: int = 0
    restore: bool = False             # START only: restore previous state after playback
    restore_level_db: float | None = None  # fallback if the console cannot report the previous level
    sort: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mixer_id": self.mixer_id,
            "target": self.target.to_dict(),
            "phase": self.phase.value,
            "mute": self.mute.value,
            "level_db": self.level_db,
            "fade_ms": self.fade_ms,
            "restore": self.restore,
            "restore_level_db": self.restore_level_db,
            "sort": self.sort,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CueAction":
        def _f(v: Any) -> float | None:
            return None if v in (None, "") else float(v)

        return cls(
            id=d.get("id") or new_id(),
            mixer_id=str(d["mixer_id"]),
            target=Target.from_dict(d.get("target") or {}),
            phase=CuePhase(d.get("phase", "start")),
            mute=MuteAction(d.get("mute", "unchanged")),
            level_db=_f(d.get("level_db")),
            fade_ms=int(d.get("fade_ms") or 0),
            restore=bool(d.get("restore")),
            restore_level_db=_f(d.get("restore_level_db")),
            sort=int(d.get("sort") or 0),
        )


@dataclass
class CueRule:
    """Mixer actions for a kind of playback event (optionally a specific file)."""

    id: str
    name: str
    enabled: bool = True
    match_kind: str = "any"            # any | jingle | audio | tts | recording | speech | media
    match_file_id: str | None = None   # specific ERM file
    match_title: str | None = None     # case-insensitive substring of the label/title
    pre_delay_ms: int = 0              # wait after START actions before audio starts
    priority: int = 0
    actions: list[CueAction] = field(default_factory=list)

    def matches(self, kind: str, file_id: str | None, label: str | None) -> bool:
        if not self.enabled:
            return False
        if self.match_kind not in ("any", "", None):
            if self.match_kind == "speech" and kind not in ("tts", "recording"):
                return False
            if self.match_kind == "media" and kind not in ("jingle", "audio"):
                return False
            if self.match_kind not in ("speech", "media") and self.match_kind != kind:
                return False
        if self.match_file_id and self.match_file_id != file_id:
            return False
        if self.match_title and self.match_title.lower() not in (label or "").lower():
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "match_kind": self.match_kind,
            "match_file_id": self.match_file_id,
            "match_title": self.match_title,
            "pre_delay_ms": self.pre_delay_ms,
            "priority": self.priority,
            "actions": [a.to_dict() for a in sorted(self.actions, key=lambda a: a.sort)],
        }


# --------------------------------------------------------------------------- playback


@dataclass
class PlaybackItem:
    """A playback job, either from the eventrentmanager queue or a local test."""

    id: str
    kind: PlaybackKind
    label: str = ""
    file_id: str | None = None
    file_url: str | None = None
    text: str | None = None
    language: str | None = None
    recording_url: str | None = None
    with_gong: bool = False
    status_url: str | None = None
    requested_by: str | None = None
    local: bool = False               # local test from the web UI, not reported to the server
    local_path: str | None = None

    @property
    def lane(self) -> str:
        return "speech" if self.kind in (PlaybackKind.TTS, PlaybackKind.RECORDING) else "media"

    @classmethod
    def from_queue(cls, q: dict[str, Any]) -> "PlaybackItem":
        kind = PlaybackKind(q.get("kind", "audio"))
        return cls(
            id=str(q["id"]),
            kind=kind,
            label=q.get("label") or "",
            file_id=q.get("file_id"),
            file_url=q.get("file_url"),
            text=q.get("text"),
            language=q.get("language"),
            recording_url=q.get("recording_url"),
            with_gong=bool(q.get("with_gong")),
            status_url=q.get("status_url"),
            requested_by=q.get("requested_by"),
        )
