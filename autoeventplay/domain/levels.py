"""Level maths shared by the audio engine, the web faders and mixer drivers.

All levels are handled in dB. ``None`` or anything at/below ``MIN_DB`` means
minus infinity (silence). The fader law follows the familiar console curve
(X32-style): 0 dB sits at 75 % travel, -10 dB at 50 %, -30 dB at 25 %,
-60 dB at 6.25 % and the bottom is -inf. The top is +10 dB.
"""

from __future__ import annotations

import math

MIN_DB = -90.0  # treated as -inf
MAX_DB = 10.0


def is_off(db: float | None) -> bool:
    return db is None or db <= MIN_DB


def clamp_db(db: float | None) -> float:
    if db is None or (isinstance(db, float) and math.isnan(db)):
        return MIN_DB
    return max(MIN_DB, min(MAX_DB, float(db)))


def db_to_gain(db: float | None) -> float:
    if is_off(db):
        return 0.0
    return 10.0 ** (clamp_db(db) / 20.0)


def gain_to_db(gain: float) -> float:
    if gain <= 1e-9:
        return MIN_DB
    return max(MIN_DB, 20.0 * math.log10(gain))


def fader_to_db(pos: float) -> float:
    """Fader position 0..1 -> dB (X32 law). 0 -> -inf (MIN_DB)."""
    f = max(0.0, min(1.0, float(pos)))
    if f <= 0.0:
        return MIN_DB
    if f >= 0.5:
        db = f * 40.0 - 30.0
    elif f >= 0.25:
        db = f * 80.0 - 50.0
    elif f >= 0.0625:
        db = f * 160.0 - 70.0
    else:
        db = f * 480.0 - 90.0
    return max(MIN_DB, db)


def db_to_fader(db: float | None) -> float:
    """dB -> fader position 0..1 (inverse of :func:`fader_to_db`)."""
    if is_off(db):
        return 0.0
    d = clamp_db(db)
    if d >= -10.0:
        f = (d + 30.0) / 40.0
    elif d >= -30.0:
        f = (d + 50.0) / 80.0
    elif d >= -60.0:
        f = (d + 70.0) / 160.0
    else:
        f = (d + 90.0) / 480.0
    return max(0.0, min(1.0, f))


def interpolate_table(value: float, table: list[tuple[float, float]]) -> float:
    """Piecewise linear interpolation in a sorted (x, y) table, clamped at both ends."""
    if not table:
        raise ValueError("empty table")
    if value <= table[0][0]:
        return table[0][1]
    if value >= table[-1][0]:
        return table[-1][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return y1
            t = (value - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return table[-1][1]


def format_db(db: float | None) -> str:
    if is_off(db):
        return "-∞"
    d = clamp_db(db)
    return f"{d:+.1f}" if abs(d) >= 0.05 else "0.0"
