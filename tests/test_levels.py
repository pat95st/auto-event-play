import math

import pytest

from autoeventplay.domain.levels import MIN_DB, db_to_fader, db_to_gain, fader_to_db, format_db, gain_to_db, interpolate_table


@pytest.mark.parametrize("db,pos", [(10, 1.0), (0, 0.75), (-10, 0.5), (-30, 0.25), (-60, 0.0625), (MIN_DB, 0.0)])
def test_fader_law_anchor_points(db, pos):
    assert db_to_fader(db) == pytest.approx(pos)
    assert fader_to_db(pos) == pytest.approx(db)


def test_fader_roundtrip():
    for i in range(1, 1001):
        f = i / 1000
        assert db_to_fader(fader_to_db(f)) == pytest.approx(f, abs=1e-9)


def test_gain():
    assert db_to_gain(0) == 1.0
    assert db_to_gain(None) == 0.0
    assert db_to_gain(-90) == 0.0
    assert db_to_gain(-6) == pytest.approx(0.501, abs=1e-3)
    assert gain_to_db(1.0) == 0.0
    assert gain_to_db(0) == MIN_DB
    assert db_to_gain(50) == pytest.approx(10 ** 0.5)  # clamped to +10


def test_format_and_interpolation():
    assert format_db(None) == "-∞"
    assert format_db(3) == "+3.0"
    assert interpolate_table(5, [(0, 0), (10, 100)]) == 50
    assert interpolate_table(-5, [(0, 0), (10, 100)]) == 0
    assert not math.isnan(fader_to_db(0.3))
