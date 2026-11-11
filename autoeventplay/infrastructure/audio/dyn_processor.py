"""Real-time compressor for one channel.

Control-rate design: the detector runs on short chunks (16 samples ≈ 0.33 ms
at 48 kHz) with peak detection, attack/release smoothing in the dB domain
and a soft knee; the resulting gain is interpolated linearly per sample.
This keeps the Python loop short (~64 iterations per 1024-sample block)
while sounding like a regular feed-forward peak compressor.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from ...domain.dynamics import static_gain_db

CHUNK = 16


class DynState:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.params: dict[str, Any] | None = None
        self.env_db = 0.0          # current gain reduction (≤ 0)
        self.last_gain = 1.0
        self.gr_peak = 0.0         # most reduction since last meter read (≤ 0)
        self._att = self._rel = 0.0

    def update(self, params: dict[str, Any] | None, sample_rate: int) -> None:
        self.params = params
        self.sample_rate = sample_rate
        if params:
            chunk_s = CHUNK / sample_rate
            self._att = math.exp(-chunk_s / max(1e-5, params["attack"] / 1000.0))
            self._rel = math.exp(-chunk_s / max(1e-5, params["release"] / 1000.0))

    def process(self, x: np.ndarray) -> np.ndarray:
        p = self.params
        if p is None:
            return x
        n = x.shape[0]
        chunks = max(1, (n + CHUNK - 1) // CHUNK)
        pad = chunks * CHUNK - n
        mags = np.abs(np.pad(x, (0, pad)) if pad else x).reshape(chunks, CHUNK).max(axis=1)
        level = 20.0 * np.log10(np.maximum(mags, 1e-9))
        target = static_gain_db(level, p)
        env = self.env_db
        att, rel = self._att, self._rel
        out = np.empty(chunks)
        for i in range(chunks):
            t = target[i]
            coef = att if t < env else rel
            env = t + coef * (env - t)
            out[i] = env
        self.env_db = env
        self.gr_peak = min(self.gr_peak, float(out.min()))
        gains = 10.0 ** ((out + p["makeup"]) / 20.0)
        # linear interpolation from the previous chunk's gain → smooth per sample
        prev = np.concatenate(([self.last_gain], gains[:-1]))
        ramp = (np.arange(CHUNK) + 1) / CHUNK
        per_sample = (prev[:, None] + (gains - prev)[:, None] * ramp[None, :]).reshape(-1)[:n]
        self.last_gain = float(gains[-1])
        return (x * per_sample).astype(np.float32, copy=False)

    def read_gr(self) -> float:
        gr, self.gr_peak = self.gr_peak, 0.0
        return round(gr, 1)
