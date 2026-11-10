import numpy as np
import pytest

from autoeventplay.domain.eq import default_eq, design_sos, is_active, normalize_eq, response_db
from autoeventplay.domain.model import InputChannel, OutputChannel, RoutingState, SourceBus
from autoeventplay.infrastructure.audio.engine import AudioEngine


def test_default_is_flat_and_bypassed():
    eq = default_eq()
    assert not is_active(eq)
    assert design_sos(eq, 48000) is None


def test_normalize_clamps_values():
    eq = normalize_eq({"bands": [{"type": "nonsense", "freq": 5, "gain": 99, "q": 0}], "low_cut": {"on": True, "slope": 7}})
    b = eq["bands"][0]
    assert b["type"] == "low_shelf" and b["freq"] == 20 and b["gain"] == 18 and b["q"] == 0.1
    assert eq["low_cut"]["slope"] == 12 and len(eq["bands"]) == 4


def test_peak_band_response():
    eq = default_eq()
    eq["bands"][1].update(freq=1000, gain=6.0, q=1.0)
    r = response_db(eq, np.array([1000.0, 50.0, 15000.0]))
    assert r[0] == pytest.approx(6.0, abs=0.05)
    assert abs(r[1]) < 0.3 and abs(r[2]) < 0.3


def test_shelves_and_cuts():
    eq = default_eq()
    eq["bands"][0].update(gain=-6.0)        # low shelf 100 Hz
    eq["bands"][3].update(gain=4.0)         # high shelf 8 kHz
    eq["low_cut"].update(on=True, freq=100, slope=24)
    eq["high_cut"].update(on=True, freq=10000, slope=12)
    r = response_db(eq, np.array([20.0, 100.0, 1000.0, 10000.0, 20000.0]))
    assert r[0] < -24          # well below the low cut
    assert r[2] == pytest.approx(0.0, abs=0.5)
    assert r[4] < r[3] - 3     # high cut above 10 kHz


@pytest.mark.parametrize("slope,expected", [(12, -3.0), (18, -3.0), (24, -3.0)])
def test_cut_is_minus_3db_at_corner(slope, expected):
    eq = default_eq()
    eq["low_cut"].update(on=True, freq=200, slope=slope)
    assert response_db(eq, np.array([200.0]))[0] == pytest.approx(expected, abs=0.1)


def test_engine_applies_input_and_output_eq():
    eq = default_eq()
    eq["low_cut"].update(on=True, freq=1000, slope=24)
    inputs = [InputChannel("media_l", "L", eq=eq), InputChannel("media_r", "R"), InputChannel("tts", "T"), InputChannel("recording", "A")]
    outputs = [OutputChannel("o1", "o1", "default", 0), OutputChannel("o2", "o2", "default", 1, eq=eq)]
    e = AudioEngine(backend="null", sample_rate=48000, blocksize=480)
    e.apply_routing(RoutingState(inputs, outputs, {("media_l", "o1"), ("media_r", "o1"), ("tts", "o2")}))
    t = np.arange(48000) / 48000
    low = (np.sin(2 * np.pi * 50 * t) * 0.5).astype(np.float32)
    e.play(SourceBus.MEDIA, np.stack([low, np.zeros_like(low)], axis=1))
    e.play(SourceBus.TTS, low[:, None])
    out = np.concatenate([e.render(480) for _ in range(60)])[24000:]
    assert np.abs(out[:, 0]).max() < 0.01   # input EQ removed the 50 Hz tone on media L
    assert np.abs(out[:, 1]).max() < 0.01   # output EQ removed it on o2
