"""Commands (write side). One dataclass per intention."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .bus import Command


# ---- routing / levels

@dataclass
class SetInputLevel(Command):
    input_id: str
    level_db: float


@dataclass
class SetInputMute(Command):
    input_id: str
    muted: bool


@dataclass
class SetOutputLevel(Command):
    output_id: str
    level_db: float


@dataclass
class SetOutputMute(Command):
    output_id: str
    muted: bool


@dataclass
class RenameChannel(Command):
    channel_type: str  # input | output
    channel_id: str
    name: str


@dataclass
class SetChannelEq(Command):
    channel_type: str  # input | output
    channel_id: str
    eq: dict[str, Any]


@dataclass
class SetChannelDynamics(Command):
    channel_type: str  # input | output
    channel_id: str
    dyn: dict[str, Any]


@dataclass
class SetInputTrim(Command):
    input_id: str
    trim_db: float


@dataclass
class SetChannelOrder(Command):
    """Order of EQ and compressor in a channel: "eq_dyn" or "dyn_eq"."""
    channel_type: str  # input | output
    channel_id: str
    order: str


@dataclass
class SetCrosspoint(Command):
    input_id: str
    output_id: str
    enabled: bool


@dataclass
class ConfigureOutputs(Command):
    outputs: list[dict[str, Any]]


@dataclass
class SetTestTone(Command):
    output_id: str
    enabled: bool


@dataclass
class SaveAudioSettings(Command):
    sample_rate: int
    blocksize: int
    parallel_lanes: bool


@dataclass
class SaveTtsSettings(Command):
    engine: str
    piper_binary: str
    espeak_binary: str
    voice_overrides: dict[str, str] = field(default_factory=dict)


# ---- consoles

@dataclass
class SaveMixer(Command):
    id: str | None
    name: str
    type: str
    host: str
    port: int | None
    enabled: bool
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeleteMixer(Command):
    id: str


@dataclass
class TestMixer(Command):
    id: str


@dataclass
class SendMixerAction(Command):
    """Manual action from the UI (test a target)."""

    mixer_id: str
    target: dict[str, Any]
    mute: str = "unchanged"
    level_db: float | None = None


# ---- cues

@dataclass
class SaveCueRule(Command):
    data: dict[str, Any]


@dataclass
class DeleteCueRule(Command):
    id: str


@dataclass
class TestCueRule(Command):
    id: str
    duration_s: float = 3.0


# ---- eventrentmanager

@dataclass
class SaveErmSettings(Command):
    base_url: str
    device_name: str
    verify_tls: bool = True
    token: str | None = None  # optional manual token (alternative to pairing)


@dataclass
class PairWithErm(Command):
    base_url: str
    code: str
    device_name: str


@dataclass
class UnpairErm(Command):
    pass


@dataclass
class SyncNow(Command):
    pass


# ---- playback

@dataclass
class StopPlayback(Command):
    pass


@dataclass
class PlayLocal(Command):
    kind: str                    # jingle | audio | tts | recording(gong test) | gong
    file_id: str | None = None
    text: str | None = None
    language: str | None = None
    with_gong: bool = False


# ---- user interface

@dataclass
class SetLanguage(Command):
    language: str  # en | de


# ---- users

@dataclass
class CreateAdmin(Command):
    username: str
    password: str


@dataclass
class ChangePassword(Command):
    username: str
    current_password: str
    new_password: str
