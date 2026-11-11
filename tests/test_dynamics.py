import numpy as np
import pytest

from autoeventplay.domain.dynamics import default_dyn, normalize_dyn, static_gain_db
from autoeventplay.domain.model import InputChannel, OutputChannel, RoutingState, SourceBus
from autoeventplay.infrastructure.audio.engine import AudioEngine


def test_normalize_and_defaults():
    d = normalize_dyn({"enabled": True, "ratio": 100, "threshold": -99, "attack": "x"})
    assert d["enabled"] and d["ratio"] == 20 and d["threshold"] == -60 and d["attack"] == default_dyn()["attack"]


def test_static_curve_hard_and_soft_knee():
    d = normalize_dyn({"enabled": True, "threshold": -20, "ratio": 4, "knee": 0})
    g = static_gain_db(np.array([-30.0, -20.0, -10.0, 0.0]), d)
    assert g[0] == 0 and g[1] == 0
    assert g[2] == pytest.approx(-7.5) and g[3] == pytest.approx(-15.0)
    d["knee"] = 10
    g = static_gain_db(np.array([-26.0, -20.0, -14.0]), d)
    assert g[0] == 0 and -2 < g[1] < 0 and g[2] == pytest.approx(-4.5)


def test_engine_compresses_loud_signal():
    dyn = normalize_dyn({"enabled": True, "threshold": -20, "ratio": 10, "attack": 1, "release": 100, "knee": 0, "makeup": 0})
    inputs = [InputChannel("media_l", "L"), InputChannel("media_r", "R"), InputChannel("tts", "T", dyn=dyn), InputChannel("recording", "A")]
    outputs = [OutputChannel("o1", "o1", "default", 0)]
    e = AudioEngine(backend="null", sample_rate=48000, blocksize=480)
    e.apply_routing(RoutingState(inputs, outputs, {("tts", "o1")}))
    t = np.arange(48000) / 48000
    tone = (np.sin(2 * np.pi * 1000 * t) * 0.5).astype(np.float32)  # -6 dBFS peak
    e.play(SourceBus.TTS, tone[:, None])
    out = np.concatenate([e.render(480) for _ in range(100)])
    peak = np.abs(out[24000:]).max()
    expected = 10 ** ((-6 + (-20 - -6) * (1 - 1 / 10)) / 20)  # ≈ -18.6 dBFS
    assert peak == pytest.approx(expected, rel=0.15)
    m = e.read_meters()
    assert m["gr"]["in:tts"] < -10
