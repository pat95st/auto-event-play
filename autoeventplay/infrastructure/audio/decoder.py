"""Decode audio files (MP3, WAV, FLAC, AAC/M4A, WebM/Opus, OGG …) to float32 numpy arrays via ffmpeg."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("aep.decoder")


class DecodeError(RuntimeError):
    pass


class AudioDecoder:
    def __init__(self, ffmpeg: str = "ffmpeg"):
        self.ffmpeg = ffmpeg

    @property
    def available(self) -> bool:
        return shutil.which(self.ffmpeg) is not None

    def decode(self, path: str | Path, sample_rate: int, channels: int = 2) -> np.ndarray:
        path = Path(path)
        if not path.exists():
            raise DecodeError(f"Datei fehlt: {path.name}")
        if self.available:
            cmd = [self.ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-vn", "-f", "f32le",
                   "-acodec", "pcm_f32le", "-ac", str(channels), "-ar", str(sample_rate), "-"]
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
            if proc.returncode != 0:
                raise DecodeError(proc.stderr.decode("utf-8", "replace").strip()[:300] or "ffmpeg-Fehler")
            data = np.frombuffer(proc.stdout, dtype=np.float32)
            if data.size == 0:
                raise DecodeError("Datei enthält kein Audio")
            return data.reshape(-1, channels).copy()
        if path.suffix.lower() == ".wav":
            return _decode_wav(path, sample_rate, channels)
        raise DecodeError("ffmpeg ist nicht installiert – nur WAV kann abgespielt werden")

    async def decode_async(self, path: str | Path, sample_rate: int, channels: int = 2) -> np.ndarray:
        return await asyncio.to_thread(self.decode, path, sample_rate, channels)


def _decode_wav(path: Path, sample_rate: int, channels: int) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        sr, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise DecodeError(f"WAV mit {width * 8} Bit wird ohne ffmpeg nicht unterstützt")
    data = data.reshape(-1, ch)
    if sr != sample_rate and data.shape[0] > 1:
        idx = np.linspace(0, data.shape[0] - 1, int(data.shape[0] * sample_rate / sr))
        data = np.stack([np.interp(idx, np.arange(data.shape[0]), data[:, c]) for c in range(ch)], axis=1).astype(np.float32)
    if ch != channels:
        data = data.mean(axis=1, keepdims=True)
        if channels > 1:
            data = np.repeat(data, channels, axis=1)
    return data
