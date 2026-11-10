"""Command handlers (write side)."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from ..domain.eq import is_active, normalize_eq
from ..domain.levels import MAX_DB, MIN_DB
from ..domain.model import (
    INPUT_IDS,
    CueAction,
    CueRule,
    MixerConfig,
    MuteAction,
    PlaybackItem,
    PlaybackKind,
    Target,
    new_id,
)
from ..infrastructure.auth import hash_password, verify_password
from ..infrastructure.erm.client import ErmError, normalize_base_url
from ..infrastructure.mixers.base import MixerError, driver_class
from . import commands as c
from .bus import CommandBus, DomainError, NotFound

if TYPE_CHECKING:
    from ..container import Services

log = logging.getLogger("aep.commands")


def _level(v: float) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError) as exc:
        raise DomainError("Ungültiger Pegel") from exc
    return max(MIN_DB, min(MAX_DB, v))


def register_command_handlers(bus: CommandBus, s: "Services") -> None:
    # ------------------------------------------------------------ routing

    def _input(cid: str) -> None:
        if cid not in INPUT_IDS:
            raise NotFound("Eingang nicht gefunden")

    def _output(cid: str) -> None:
        if cid not in {o.id for o in s.routing_repo.load().outputs}:
            raise NotFound("Ausgang nicht gefunden")

    def set_input_level(cmd: c.SetInputLevel) -> None:
        _input(cmd.input_id)
        s.routing_repo.set_input(cmd.input_id, level_db=_level(cmd.level_db))
        s.refresh_routing()

    def set_input_mute(cmd: c.SetInputMute) -> None:
        _input(cmd.input_id)
        s.routing_repo.set_input(cmd.input_id, muted=bool(cmd.muted))
        s.refresh_routing()

    def set_output_level(cmd: c.SetOutputLevel) -> None:
        _output(cmd.output_id)
        s.routing_repo.set_output(cmd.output_id, level_db=_level(cmd.level_db))
        s.refresh_routing()

    def set_output_mute(cmd: c.SetOutputMute) -> None:
        _output(cmd.output_id)
        s.routing_repo.set_output(cmd.output_id, muted=bool(cmd.muted))
        s.refresh_routing()

    def rename_channel(cmd: c.RenameChannel) -> None:
        name = (cmd.name or "").strip()[:40]
        if not name:
            raise DomainError("Name darf nicht leer sein")
        if cmd.channel_type == "input":
            _input(cmd.channel_id)
            s.routing_repo.set_input(cmd.channel_id, name=name)
        else:
            _output(cmd.channel_id)
            s.routing_repo.set_output(cmd.channel_id, name=name)
        s.refresh_routing()

    def set_channel_eq(cmd: c.SetChannelEq) -> dict:
        if cmd.channel_type == "input":
            _input(cmd.channel_id)
        elif cmd.channel_type == "output":
            _output(cmd.channel_id)
        else:
            raise DomainError("Unbekannter Kanaltyp")
        eq = normalize_eq(cmd.eq)
        s.routing_repo.set_eq(cmd.channel_type, cmd.channel_id, eq)
        s.refresh_routing()
        return {"eq": eq, "active": is_active(eq)}

    def set_crosspoint(cmd: c.SetCrosspoint) -> None:
        _input(cmd.input_id)
        _output(cmd.output_id)
        s.routing_repo.set_crosspoint(cmd.input_id, cmd.output_id, bool(cmd.enabled))
        s.refresh_routing()
        log.info("Routing: %s → %s %s", cmd.input_id, cmd.output_id, "an" if cmd.enabled else "aus")

    def configure_outputs(cmd: c.ConfigureOutputs) -> None:
        if not cmd.outputs:
            raise DomainError("Mindestens ein Ausgang wird benötigt")
        seen = set()
        cleaned = []
        for o in cmd.outputs:
            device = str(o.get("device") or "default").strip() or "default"
            try:
                channel = int(o.get("channel", 0))
            except (TypeError, ValueError) as exc:
                raise DomainError("Ungültige Kanalnummer") from exc
            if channel < 0 or channel > 63:
                raise DomainError("Kanalnummer muss zwischen 1 und 64 liegen")
            if (device, channel) in seen:
                raise DomainError(f"Kanal {channel + 1} von „{device}“ ist doppelt belegt")
            seen.add((device, channel))
            oid = o.get("id") or None
            if oid is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(oid)):
                raise DomainError("Ungültige Ausgangs-ID")
            cleaned.append({"id": oid, "name": str(o.get("name") or "").strip()[:40], "device": device, "channel": channel})
        s.routing_repo.replace_outputs(cleaned)
        s.refresh_routing()
        log.info("Ausgänge neu konfiguriert: %d Kanäle", len(cleaned))

    def set_test_tone(cmd: c.SetTestTone) -> None:
        _output(cmd.output_id)
        s.engine.set_test_tone(cmd.output_id, bool(cmd.enabled))
        log.info("Testton %s auf %s", "an" if cmd.enabled else "aus", cmd.output_id)

    async def save_audio_settings(cmd: c.SaveAudioSettings) -> None:
        if cmd.sample_rate not in (44100, 48000, 96000):
            raise DomainError("Abtastrate muss 44100, 48000 oder 96000 Hz sein")
        if cmd.blocksize not in (256, 512, 1024, 2048, 4096):
            raise DomainError("Ungültige Blockgröße")
        s.settings.set("audio.sample_rate", cmd.sample_rate)
        s.settings.set("audio.blocksize", cmd.blocksize)
        s.settings.set("playback.parallel_lanes", bool(cmd.parallel_lanes))
        await asyncio.to_thread(s.engine.set_format, cmd.sample_rate, cmd.blocksize)
        await s.playback.set_parallel(bool(cmd.parallel_lanes))

    def save_tts_settings(cmd: c.SaveTtsSettings) -> None:
        if cmd.engine not in ("auto", "piper", "espeak"):
            raise DomainError("Unbekannte Sprachausgabe")
        overrides = {k.strip(): v.strip() for k, v in (cmd.voice_overrides or {}).items() if k.strip() and v and v.strip()}
        s.settings.set("tts.engine", cmd.engine)
        s.settings.set("tts.piper_binary", cmd.piper_binary.strip() or "piper")
        s.settings.set("tts.espeak_binary", cmd.espeak_binary.strip() or "espeak-ng")
        s.settings.set("tts.voice_overrides", overrides)
        s.apply_tts_settings()

    # ------------------------------------------------------------ consoles

    async def save_mixer(cmd: c.SaveMixer) -> str:
        name = (cmd.name or "").strip()
        host = (cmd.host or "").strip()
        if not name:
            raise DomainError("Bitte einen Namen angeben")
        if not host or not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host):
            raise DomainError("Bitte eine gültige IP-Adresse oder einen Hostnamen angeben")
        try:
            cls = driver_class(cmd.type)
        except MixerError as exc:
            raise DomainError(str(exc)) from exc
        port = int(cmd.port) if cmd.port not in (None, "") else cls.default_port
        if port is not None and not 1 <= port <= 65535:
            raise DomainError("Ungültiger Port")
        options = {}
        for spec in cls.option_specs:
            if spec.key in cmd.options:
                value = cmd.options[spec.key]
                if spec.type == "int" and value not in (None, ""):
                    try:
                        value = int(value)
                    except (TypeError, ValueError) as exc:
                        raise DomainError(f"{spec.label}: Zahl erwartet") from exc
                if spec.type == "json" and isinstance(value, str) and value.strip():
                    import json

                    try:
                        json.loads(value)
                    except ValueError as exc:
                        raise DomainError(f"{spec.label}: ungültiges JSON ({exc})") from exc
                options[spec.key] = value
        mixer = MixerConfig(cmd.id or new_id(), name[:60], cmd.type, host, port, bool(cmd.enabled), options)
        s.mixer_repo.save(mixer)
        await s.mixers.configure(s.mixer_repo.all())
        log.info("Mischpult gespeichert: %s (%s, %s:%s)", mixer.name, cls.display_name, host, port)
        return mixer.id

    async def delete_mixer(cmd: c.DeleteMixer) -> None:
        if s.mixer_repo.get(cmd.id) is None:
            raise NotFound("Mischpult nicht gefunden")
        used = [r.name for r in s.cue_repo.all() if any(a.mixer_id == cmd.id for a in r.actions)]
        if used:
            raise DomainError("Das Pult wird noch in Cues verwendet: " + ", ".join(used))
        s.mixer_repo.delete(cmd.id)
        await s.mixers.configure(s.mixer_repo.all())

    async def test_mixer(cmd: c.TestMixer) -> str:
        if s.mixer_repo.get(cmd.id) is None:
            raise NotFound("Mischpult nicht gefunden")
        try:
            return await s.mixers.test(cmd.id)
        except MixerError as exc:
            raise DomainError(str(exc)) from exc

    async def send_mixer_action(cmd: c.SendMixerAction) -> str:
        if s.mixer_repo.get(cmd.mixer_id) is None:
            raise NotFound("Mischpult nicht gefunden")
        try:
            target = Target.from_dict(cmd.target)
            if cmd.mute in ("mute", "unmute"):
                await s.mixers.set_mute(cmd.mixer_id, target, cmd.mute == "mute")
            if cmd.level_db is not None:
                await s.mixers.set_level(cmd.mixer_id, target, _level(cmd.level_db))
        except (MixerError, ValueError) as exc:
            raise DomainError(str(exc)) from exc
        return s.mixers.describe(cmd.mixer_id, target)

    # ------------------------------------------------------------ cues

    def save_cue_rule(cmd: c.SaveCueRule) -> str:
        d = cmd.data
        name = str(d.get("name") or "").strip()
        if not name:
            raise DomainError("Bitte einen Namen für den Cue angeben")
        kind = d.get("match_kind") or "any"
        if kind not in ("any", "jingle", "audio", "tts", "recording", "speech", "media"):
            raise DomainError("Ungültige Auftragsart")
        mixer_ids = {m.id for m in s.mixer_repo.all()}
        actions = []
        for i, a in enumerate(d.get("actions") or []):
            try:
                action = CueAction.from_dict({**a, "sort": i})
            except (KeyError, ValueError, TypeError) as exc:
                raise DomainError(f"Aktion {i + 1}: ungültige Angaben ({exc})") from exc
            if action.mixer_id not in mixer_ids:
                raise DomainError(f"Aktion {i + 1}: Mischpult nicht gefunden")
            if action.mute == MuteAction.UNCHANGED and action.level_db is None:
                raise DomainError(f"Aktion {i + 1}: bitte Mute/Unmute und/oder einen Pegel festlegen")
            if action.level_db is not None:
                action.level_db = _level(action.level_db)
            try:
                drv = driver_class(s.mixer_repo.get(action.mixer_id).type)(s.mixer_repo.get(action.mixer_id))  # type: ignore[union-attr]
                if action.mute != MuteAction.UNCHANGED:
                    drv.validate(action.target, "mute")
                if action.level_db is not None:
                    drv.validate(action.target, "level")
            except MixerError as exc:
                raise DomainError(f"Aktion {i + 1}: {exc}") from exc
            action.fade_ms = max(0, min(10000, action.fade_ms))
            if action.phase.value == "end":
                action.restore = False
            actions.append(action)
        rule = CueRule(
            id=d.get("id") or new_id(),
            name=name[:80],
            enabled=bool(d.get("enabled", True)),
            match_kind=kind,
            match_file_id=d.get("match_file_id") or None,
            match_title=(d.get("match_title") or "").strip() or None,
            pre_delay_ms=max(0, min(10000, int(d.get("pre_delay_ms") or 0))),
            priority=int(d.get("priority") or 0),
            actions=actions,
        )
        s.cue_repo.save(rule)
        log.info("Cue gespeichert: %s (%d Aktionen)", rule.name, len(actions))
        return rule.id

    def delete_cue_rule(cmd: c.DeleteCueRule) -> None:
        s.cue_repo.delete(cmd.id)

    async def test_cue_rule(cmd: c.TestCueRule) -> str:
        rule = s.cue_repo.get(cmd.id)
        if rule is None:
            raise NotFound("Cue nicht gefunden")
        log.info("Cue-Test: %s", rule.name)
        session = await s.cues.start([rule])
        await asyncio.sleep(max(0.5, min(30.0, cmd.duration_s)))
        await s.cues.finish(session)
        return f"Cue „{rule.name}“ ausgeführt"

    # ------------------------------------------------------------ eventrentmanager

    def save_erm_settings(cmd: c.SaveErmSettings) -> None:
        url = normalize_base_url(cmd.base_url)
        if url and not re.match(r"^https?://[^\s/]+", url):
            raise DomainError("Ungültige URL")
        if url != s.settings.get("erm.base_url", ""):
            # token belongs to the old instance
            s.settings.delete("erm.token")
        s.settings.set("erm.base_url", url)
        s.settings.set("erm.device_name", (cmd.device_name or "AutoEventPlay").strip()[:120])
        s.settings.set("erm.verify_tls", bool(cmd.verify_tls))
        if cmd.token:
            token = cmd.token.strip()
            if not token.startswith("mvp_"):
                raise DomainError("Geräte-Token beginnt mit „mvp_“")
            s.settings.set("erm.token", token)
        s.sync.wake()

    async def pair_with_erm(cmd: c.PairWithErm) -> dict:
        code = re.sub(r"[\s-]+", "", cmd.code or "")
        if not re.fullmatch(r"\d{6}", code):
            raise DomainError("Der Code besteht aus 6 Ziffern")
        url = normalize_base_url(cmd.base_url or s.settings.get("erm.base_url", ""))
        if not url:
            raise DomainError("Bitte zuerst die URL der eventrentmanager-Instanz angeben")
        name = (cmd.device_name or s.settings.get("erm.device_name") or "AutoEventPlay").strip()[:120]
        s.settings.set("erm.device_name", name)
        try:
            result = await s.sync.pair(url, code, name)
        except ErmError as exc:
            raise DomainError(f"Kopplung fehlgeschlagen: {exc}") from exc
        return {"player": result.get("player"), "rental": result.get("rental")}

    def unpair_erm(cmd: c.UnpairErm) -> None:
        s.sync.unpair()

    def sync_now(cmd: c.SyncNow) -> None:
        s.sync.wake()

    # ------------------------------------------------------------ playback

    def stop_playback(cmd: c.StopPlayback) -> int:
        n = s.playback.stop_all()
        log.info("Wiedergabe manuell gestoppt (%d)", n)
        return n

    def play_local(cmd: c.PlayLocal) -> str:
        item_id = "local-" + new_id()[:10]
        if cmd.kind in ("jingle", "audio"):
            entry = s.file_repo.get(cmd.file_id or "")
            if entry is None:
                raise DomainError("Datei ist (noch) nicht im lokalen Cache")
            item = PlaybackItem(item_id, PlaybackKind(cmd.kind if cmd.kind in ("jingle", "audio") else "audio"),
                                label=entry.get("title") or "Test", file_id=cmd.file_id, local=True, local_path=entry["path"])
        elif cmd.kind == "tts":
            text = (cmd.text or "").strip()
            if not text:
                raise DomainError("Bitte einen Text eingeben")
            item = PlaybackItem(item_id, PlaybackKind.TTS, label=text[:60], text=text[:2000], language=cmd.language or None,
                                with_gong=bool(cmd.with_gong), local=True)
        elif cmd.kind == "gong":
            item = PlaybackItem(item_id, PlaybackKind.RECORDING, label="Gong-Test", with_gong=True, local=True,
                                local_path=str(s.silence_path()))
        else:
            raise DomainError("Unbekannte Testart")
        s.playback.enqueue(item)
        return item_id

    # ------------------------------------------------------------ users

    def create_admin(cmd: c.CreateAdmin) -> None:
        if s.user_repo.count() > 0:
            raise DomainError("Es existiert bereits ein Administrator", 409)
        _validate_credentials(cmd.username, cmd.password)
        s.user_repo.save(cmd.username.strip(), hash_password(cmd.password))
        log.info("Administrator „%s“ angelegt", cmd.username.strip())

    def change_password(cmd: c.ChangePassword) -> None:
        if not verify_password(cmd.current_password, s.user_repo.get_hash(cmd.username)):
            raise DomainError("Aktuelles Passwort ist falsch", 403)
        _validate_credentials(cmd.username, cmd.new_password)
        s.user_repo.save(cmd.username, hash_password(cmd.new_password))
        log.info("Passwort von „%s“ geändert", cmd.username)

    for message, handler in [
        (c.SetInputLevel, set_input_level), (c.SetInputMute, set_input_mute),
        (c.SetOutputLevel, set_output_level), (c.SetOutputMute, set_output_mute),
        (c.RenameChannel, rename_channel), (c.SetCrosspoint, set_crosspoint), (c.SetChannelEq, set_channel_eq),
        (c.ConfigureOutputs, configure_outputs), (c.SetTestTone, set_test_tone),
        (c.SaveAudioSettings, save_audio_settings), (c.SaveTtsSettings, save_tts_settings),
        (c.SaveMixer, save_mixer), (c.DeleteMixer, delete_mixer), (c.TestMixer, test_mixer),
        (c.SendMixerAction, send_mixer_action),
        (c.SaveCueRule, save_cue_rule), (c.DeleteCueRule, delete_cue_rule), (c.TestCueRule, test_cue_rule),
        (c.SaveErmSettings, save_erm_settings), (c.PairWithErm, pair_with_erm), (c.UnpairErm, unpair_erm),
        (c.SyncNow, sync_now), (c.StopPlayback, stop_playback), (c.PlayLocal, play_local),
        (c.CreateAdmin, create_admin), (c.ChangePassword, change_password),
    ]:
        bus.register(message, handler)


def _validate_credentials(username: str, password: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,40}", (username or "").strip()):
        raise DomainError("Benutzername: 3–40 Zeichen (Buchstaben, Ziffern, . _ -)")
    if len(password or "") < 8:
        raise DomainError("Das Passwort muss mindestens 8 Zeichen lang sein")

