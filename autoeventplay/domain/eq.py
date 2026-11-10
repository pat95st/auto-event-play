"""Channel equalizer: 4 fully parametric bands plus low cut and high cut.

The EQ is stored as a plain dict (JSON in SQLite)::

    {
      "enabled": true,
      "low_cut":  {"on": false, "freq": 80.0,    "slope": 12},
      "high_cut": {"on": false, "freq": 16000.0, "slope": 12},
      "bands": [
        {"on": true, "type": "low_shelf",  "freq": 100.0,  "gain": 0.0, "q": 0.7},
        {"on": true, "type": "peak",       "freq": 400.0,  "gain": 0.0, "q": 1.0},
        {"on": true, "type": "peak",       "freq": 2500.0, "gain": 0.0, "q": 1.0},
        {"on": true, "type": "high_shelf", "freq": 8000.0, "gain": 0.0, "q": 0.7}
      ]
    }

Every band can be switched between peak (bell), low shelf and high shelf.
Coefficients follow the RBJ "Audio EQ Cookbook"; the cut filters are
Butterworth (12, 18 or 24 dB/oct). The same formulas exist in the web UI
(static/js/eq.js) to draw the response curve.
"""

from __future__ import annotations

import copy
import math
from typing import Any

import numpy as np

BAND_TYPES = ("peak", "low_shelf", "high_shelf")
SLOPES = (12, 18, 24)
FREQ_MIN, FREQ_MAX = 20.0, 20000.0
GAIN_MIN, GAIN_MAX = -18.0, 18.0
Q_MIN, Q_MAX = 0.1, 16.0

DEFAULT_EQ: dict[str, Any] = {
    "enabled": True,
    "low_cut": {"on": False, "freq": 80.0, "slope": 12},
    "high_cut": {"on": False, "freq": 16000.0, "slope": 12},
    "bands": [
        {"on": True, "type": "low_shelf", "freq": 100.0, "gain": 0.0, "q": 0.7},
        {"on": True, "type": "peak", "freq": 400.0, "gain": 0.0, "q": 1.0},
        {"on": True, "type": "peak", "freq": 2500.0, "gain": 0.0, "q": 1.0},
        {"on": True, "type": "high_shelf", "freq": 8000.0, "gain": 0.0, "q": 0.7},
    ],
}


def default_eq() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_EQ)


def _num(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return max(lo, min(hi, v))


def normalize_eq(data: dict[str, Any] | None) -> dict[str, Any]:
    """Validate/clamp user input into a complete EQ dict."""
    base = default_eq()
    if not isinstance(data, dict):
        return base
    out: dict[str, Any] = {"enabled": bool(data.get("enabled", True))}
    for key in ("low_cut", "high_cut"):
        src = data.get(key) if isinstance(data.get(key), dict) else {}
        d = base[key]
        slope = int(_num(src.get("slope"), d["slope"], 6, 48))
        out[key] = {
            "on": bool(src.get("on", d["on"])),
            "freq": round(_num(src.get("freq"), d["freq"], FREQ_MIN, FREQ_MAX), 1),
            "slope": slope if slope in SLOPES else 12,
        }
    bands_in = data.get("bands") if isinstance(data.get("bands"), list) else []
    bands = []
    for i, d in enumerate(base["bands"]):
        src = bands_in[i] if i < len(bands_in) and isinstance(bands_in[i], dict) else {}
        btype = src.get("type", d["type"])
        bands.append({
            "on": bool(src.get("on", d["on"])),
            "type": btype if btype in BAND_TYPES else d["type"],
            "freq": round(_num(src.get("freq"), d["freq"], FREQ_MIN, FREQ_MAX), 1),
            "gain": round(_num(src.get("gain"), d["gain"], GAIN_MIN, GAIN_MAX), 2),
            "q": round(_num(src.get("q"), d["q"], Q_MIN, Q_MAX), 3),
        })
    out["bands"] = bands
    return out


def is_active(eq: dict[str, Any] | None) -> bool:
    """True if the EQ changes the signal at all (enabled and something non-flat)."""
    if not eq or not eq.get("enabled"):
        return False
    if eq["low_cut"]["on"] or eq["high_cut"]["on"]:
        return True
    return any(b["on"] and abs(b["gain"]) >= 0.01 for b in eq["bands"])


# --------------------------------------------------------------------------- coefficients


def _biquad(kind: str, freq: float, fs: float, q: float, gain: float = 0.0) -> np.ndarray:
    """One second-order section [b0, b1, b2, 1, a1, a2] (normalized)."""
    freq = min(freq, fs * 0.49)
    w0 = 2.0 * math.pi * freq / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * q)
    a = 10.0 ** (gain / 40.0)
    if kind == "peak":
        b0, b1, b2 = 1 + alpha * a, -2 * cw, 1 - alpha * a
        a0, a1, a2 = 1 + alpha / a, -2 * cw, 1 - alpha / a
    elif kind == "low_shelf":
        sa = 2 * math.sqrt(a) * alpha
        b0 = a * ((a + 1) - (a - 1) * cw + sa)
        b1 = 2 * a * ((a - 1) - (a + 1) * cw)
        b2 = a * ((a + 1) - (a - 1) * cw - sa)
        a0 = (a + 1) + (a - 1) * cw + sa
        a1 = -2 * ((a - 1) + (a + 1) * cw)
        a2 = (a + 1) + (a - 1) * cw - sa
    elif kind == "high_shelf":
        sa = 2 * math.sqrt(a) * alpha
        b0 = a * ((a + 1) + (a - 1) * cw + sa)
        b1 = -2 * a * ((a - 1) + (a + 1) * cw)
        b2 = a * ((a + 1) + (a - 1) * cw - sa)
        a0 = (a + 1) - (a - 1) * cw + sa
        a1 = 2 * ((a - 1) - (a + 1) * cw)
        a2 = (a + 1) - (a - 1) * cw - sa
    elif kind == "highpass":
        b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    elif kind == "lowpass":
        b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    else:  # pragma: no cover
        raise ValueError(kind)
    return np.array([b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0])


def _first_order(kind: str, freq: float, fs: float) -> np.ndarray:
    k = math.tan(math.pi * min(freq, fs * 0.49) / fs)
    if kind == "highpass":
        b0, b1 = 1 / (1 + k), -1 / (1 + k)
    else:
        b0, b1 = k / (1 + k), k / (1 + k)
    a1 = (k - 1) / (k + 1)
    return np.array([b0, b1, 0.0, 1.0, a1, 0.0])


# Butterworth section Qs for the cut filters
_BUTTER_Q = {12: [0.70710678], 18: [1.0], 24: [0.54119610, 1.30656296]}


def _cut_sections(kind: str, freq: float, slope: int, fs: float) -> list[np.ndarray]:
    sections = [_biquad(kind, freq, fs, q) for q in _BUTTER_Q[slope]]
    if slope == 18:
        sections.append(_first_order(kind, freq, fs))
    return sections


def design_sos(eq: dict[str, Any] | None, fs: float) -> np.ndarray | None:
    """Second-order sections for the whole EQ, or None if it is flat/bypassed."""
    if not is_active(eq):
        return None
    assert eq is not None
    sections: list[np.ndarray] = []
    if eq["low_cut"]["on"]:
        sections += _cut_sections("highpass", eq["low_cut"]["freq"], eq["low_cut"]["slope"], fs)
    for b in eq["bands"]:
        if b["on"] and abs(b["gain"]) >= 0.01:
            sections.append(_biquad(b["type"], b["freq"], fs, b["q"], b["gain"]))
    if eq["high_cut"]["on"]:
        sections += _cut_sections("lowpass", eq["high_cut"]["freq"], eq["high_cut"]["slope"], fs)
    return np.vstack(sections) if sections else None


def response_db(eq: dict[str, Any] | None, freqs: np.ndarray, fs: float = 48000.0) -> np.ndarray:
    """Magnitude response in dB at the given frequencies (used by tests)."""
    sos = design_sos(eq, fs)
    if sos is None:
        return np.zeros_like(freqs, dtype=float)
    z = np.exp(1j * 2 * np.pi * freqs / fs)
    h = np.ones_like(z)
    for b0, b1, b2, _, a1, a2 in sos:
        h *= (b0 + b1 / z + b2 / z ** 2) / (1 + a1 / z + a2 / z ** 2)
    return 20 * np.log10(np.maximum(np.abs(h), 1e-12))
