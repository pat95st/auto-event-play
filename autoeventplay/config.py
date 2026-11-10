"""Runtime configuration from environment variables.

Everything the operator changes at runtime lives in the SQLite settings table
and is edited in the web UI. Only things needed *before* the database exists
are environment variables (see deploy/autoeventplay.env).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    host: str = "0.0.0.0"
    port: int = 8080
    # "sounddevice" (real ALSA/PortAudio output) or "null" (no hardware, clocked by a timer)
    audio_backend: str = "sounddevice"
    ffmpeg: str = "ffmpeg"
    log_level: str = "INFO"
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autoeventplay.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "voices"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.cache_dir, self.cache_dir / "files", self.log_dir, self.voices_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            data_dir=Path(os.environ.get("AEP_DATA_DIR", "./data")).resolve(),
            host=os.environ.get("AEP_HOST", "0.0.0.0"),
            port=int(os.environ.get("AEP_PORT", "8080")),
            audio_backend=os.environ.get("AEP_AUDIO_BACKEND", "sounddevice"),
            ffmpeg=os.environ.get("AEP_FFMPEG", "ffmpeg"),
            log_level=os.environ.get("AEP_LOG_LEVEL", "INFO"),
        )
