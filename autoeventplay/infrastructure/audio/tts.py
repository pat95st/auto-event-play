"""Offline text-to-speech: Piper (preferred) with espeak-ng as fallback."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("aep.tts")

# Sensible default Piper voices per language (downloaded by deploy/install.sh).
DEFAULT_VOICES = {
    "de": "de_DE-thorsten-high",
    "en": "en_GB-alan-medium",
    "fr": "fr_FR-siwis-medium",
    "it": "it_IT-paola-medium",
    "es": "es_ES-davefx-medium",
    "nl": "nl_NL-mls-medium",
    "pl": "pl_PL-gosia-medium",
}


class TtsError(RuntimeError):
    pass


@dataclass
class TtsSettings:
    engine: str = "auto"          # auto | piper | espeak
    piper_binary: str = "piper"
    espeak_binary: str = "espeak-ng"
    voice_overrides: dict[str, str] | None = None  # language (de-DE or de) -> piper voice name


class TtsService:
    def __init__(self, voices_dir: Path, tmp_dir: Path, settings: TtsSettings | None = None):
        self.voices_dir = voices_dir
        self.tmp_dir = tmp_dir
        self.settings = settings or TtsSettings()

    # ------------------------------------------------------------ discovery

    def installed_voices(self) -> list[str]:
        return sorted(p.name[:-5] for p in self.voices_dir.glob("*.onnx"))

    def piper_available(self) -> bool:
        return self._piper_cmd() is not None

    def espeak_available(self) -> bool:
        return shutil.which(self.settings.espeak_binary) is not None

    def _piper_cmd(self) -> list[str] | None:
        found = shutil.which(self.settings.piper_binary)
        if found:
            return [found]
        try:  # pip package "piper-tts" provides "python -m piper"
            import importlib.util

            if importlib.util.find_spec("piper") is not None:
                import sys

                return [sys.executable, "-m", "piper"]
        except Exception:
            pass
        return None

    def resolve_voice(self, language: str | None, requested: str | None) -> str | None:
        """Pick an installed Piper voice for the language (BCP-47, e.g. de-DE)."""
        installed = self.installed_voices()
        if not installed:
            return None
        lang = (language or "de-DE").replace("_", "-")
        base = lang.split("-")[0].lower()
        prefix_full = lang.replace("-", "_")  # de_DE
        overrides = self.settings.voice_overrides or {}
        for key in (lang, base):
            v = overrides.get(key)
            if v and v in installed:
                return v
        if requested and requested in installed and requested.lower().startswith(base):
            return requested
        for v in installed:
            if v.startswith(prefix_full + "-"):
                return v
        default = DEFAULT_VOICES.get(base)
        if default in installed:
            return default
        for v in installed:
            if v.lower().startswith(base + "_"):
                return v
        return None

    # ------------------------------------------------------------ synthesis

    def synthesize(self, text: str, language: str | None = None, voice: str | None = None, rate: float = 1.0) -> Path:
        text = (text or "").strip()
        if not text:
            raise TtsError("Kein Text für die Durchsage")
        rate = max(0.5, min(2.0, float(rate or 1.0)))
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        out = self.tmp_dir / f"tts-{uuid.uuid4().hex}.wav"
        engine = self.settings.engine
        errors = []
        if engine in ("auto", "piper"):
            piper = self._piper_cmd()
            model = self.resolve_voice(language, voice)
            if piper and model:
                model_path = str(self.voices_dir / f"{model}.onnx")
                # piper < 1.3 uses underscores, piper-tts >= 1.3 (piper1-gpl) hyphens
                variants = [
                    ["--model", model_path, "--output_file", str(out), "--length_scale", f"{1.0 / rate:.3f}", "--sentence_silence", "0.25"],
                    ["--model", model_path, "--output-file", str(out), "--length-scale", f"{1.0 / rate:.3f}", "--sentence-silence", "0.25"],
                ]
                for args in variants:
                    proc = subprocess.run(piper + args, input=text.encode("utf-8"), capture_output=True, timeout=120)
                    if proc.returncode == 0 or b"unrecognized arguments" not in proc.stderr:
                        break
                if proc.returncode == 0 and out.exists() and out.stat().st_size > 44:
                    log.info("TTS (piper, %s): %s", model, text[:80])
                    return out
                errors.append("piper: " + proc.stderr.decode("utf-8", "replace").strip()[-300:])
            else:
                errors.append("piper: " + ("keine passende Stimme installiert" if piper else "nicht installiert"))
        if engine in ("auto", "espeak"):
            esp = shutil.which(self.settings.espeak_binary)
            if esp:
                lang = (language or "de-DE").lower()
                cmd = [esp, "-v", lang, "-s", str(int(165 * rate)), "-w", str(out), text]
                proc = subprocess.run(cmd, capture_output=True, timeout=60)
                if proc.returncode != 0:  # retry with base language
                    cmd[2] = lang.split("-")[0]
                    proc = subprocess.run(cmd, capture_output=True, timeout=60)
                if proc.returncode == 0 and out.exists():
                    log.info("TTS (espeak-ng, %s): %s", lang, text[:80])
                    return out
                errors.append("espeak-ng: " + proc.stderr.decode("utf-8", "replace").strip()[-200:])
            else:
                errors.append("espeak-ng: nicht installiert")
        raise TtsError("Sprachausgabe fehlgeschlagen (" + "; ".join(errors) + ")")

    async def synthesize_async(self, text: str, language: str | None = None, voice: str | None = None, rate: float = 1.0) -> Path:
        return await asyncio.to_thread(self.synthesize, text, language, voice, rate)
