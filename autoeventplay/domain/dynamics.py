"""Channel compressor settings and static curve.

Stored as a dict (JSON in SQLite)::

    {"enabled": false, "threshold": -20.0, "ratio": 3.0, "attack": 10.0,
     "release": 150.0, "knee": 6.0, "makeup": 0.0}

threshold/knee/makeup in dB, attack/release in ms. Ratio 1…20 (20 ≈ limiter).
The same curve is drawn in the web UI (static/js/dyn.js).
"""

from __future__ import annotations

import copy
import math
from typing import Any

import numpy as np

DEFAULT_DYN: dict[str, Any] = {
    "enabled": False, "threshold": -20.0, "ratio": 3.0, "attack": 10.0, "release": 150.0, "knee": 6.0, "makeup": 0.0,
}
LIMITS = {
    "threshold": (-60.0, 0.0), "ratio": (1.0, 20.0), "attack": (0.1, 200.0), "release": (10.0, 3000.0),
    "knee": (0.0, 24.0), "makeup": (0.0, 24.0),
}


def default_dyn() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_DYN)


def normalize_dyn(data: dict[str, Any] | None) -> dict[str, Any]:
    out = default_dyn()
    if not isinstance(data, dict):
        return out
    out["enabled"] = bool(data.get("enabled", False))
    for key, (lo, hi) in LIMITS.items():
        try:
            v = float(data.get(key, out[key]))
        except (TypeError, ValueError):
            continue
        if math.isnan(v) or math.isinf(v):
            continue
        out[key] = round(max(lo, min(hi, v)), 2)
    return out


def is_active(dyn: dict[str, Any] | None) -> bool:
    return bool(dyn and dyn.get("enabled"))


def static_gain_db(level_db: np.ndarray | float, dyn: dict[str, Any]) -> np.ndarray:
    """Gain change in dB (≤ 0, before makeup) for an input level, soft knee."""
    x = np.asarray(level_db, dtype=np.float64)
    t, r, w = dyn["threshold"], dyn["ratio"], dyn["knee"]
    slope = 1.0 / r - 1.0
    over = x - t
    gain = np.zeros_like(x)
    if w > 0:
        in_knee = np.abs(over) <= w / 2
        gain = np.where(in_knee, slope * (over + w / 2) ** 2 / (2 * w), gain)
        gain = np.where(over > w / 2, slope * over, gain)
    else:
        gain = np.where(over > 0, slope * over, gain)
    return gain
