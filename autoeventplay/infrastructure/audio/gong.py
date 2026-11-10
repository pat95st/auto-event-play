"""Built-in gong (used when the eventrentmanager has no own gong uploaded).

A classic three-tone announcement chime (G5 - E5 - C5) synthesized with a
few inharmonic partials and exponential decay.
"""

from __future__ import annotations

import numpy as np


def builtin_gong(sample_rate: int) -> np.ndarray:
    notes = [(783.99, 0.0), (659.25, 0.42), (523.25, 0.84)]
    length = 2.6
    n = int(sample_rate * length)
    t = np.arange(n) / sample_rate
    out = np.zeros(n, dtype=np.float64)
    for freq, start in notes:
        mask = t >= start
        tt = t[mask] - start
        env = np.exp(-tt * 2.2) * np.minimum(1.0, tt / 0.004)
        tone = (
            np.sin(2 * np.pi * freq * tt)
            + 0.35 * np.sin(2 * np.pi * freq * 2.01 * tt) * np.exp(-tt * 3.0)
            + 0.12 * np.sin(2 * np.pi * freq * 3.02 * tt) * np.exp(-tt * 5.0)
        )
        out[mask] += tone * env
    out /= np.max(np.abs(out)) or 1.0
    out *= 10 ** (-6 / 20)  # -6 dBFS peak
    return out.astype(np.float32)[:, None]
