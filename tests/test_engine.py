import numpy as np
import pytest

from autoeventplay.domain.model import InputChannel, OutputChannel, RoutingState, SourceBus
from autoeventplay.infrastructure.audio.engine import AudioEngine


def state(cps, in_levels=None, out_mute=None):
    in_levels = in_levels or {}
    inputs = [InputChannel(i, i, in_levels.get(i, 0.0)) for i in ("media_l", "media_r", "tts", "recording")]
    outputs = [OutputChannel("o1", "o1", "default", 0, muted=bool(out_mute == "o1")), OutputChannel("o2", "o2", "default", 1)]
    return RoutingState(inputs, outputs, set(cps))


def engine(cps, **kw):
    e = AudioEngine(backend="null", sample_rate=48000, blocksize=256)
    e.apply_routing(state(cps, **kw))
    return e


def test_media_left_right_routing_and_mono_sum():
    e = engine({("media_l", "o1"), ("media_r", "o1"), ("tts", "o2")})
    stereo = np.zeros((1000, 2), dtype=np.float32)
    stereo[:, 0] = 0.25
    stereo[:, 1] = 0.125
    e.play(SourceBus.MEDIA, stereo)
    e.play(SourceBus.TTS, np.full((1000, 1), 0.5, dtype=np.float32))
    out = e.render(256)
    assert out.shape == (256, 2)
    assert out[10, 0] == pytest.approx(0.375)  # L + R mono sum
    assert out[10, 1] == pytest.approx(0.5)    # speech only


def test_input_level_output_mute_and_meters():
    e = engine({("tts", "o1"), ("tts", "o2")}, in_levels={"tts": -6.0}, out_mute="o1")
    e.play(SourceBus.TTS, np.full((1000, 1), 0.5, dtype=np.float32))
    out = e.render(256)
    assert out[0, 0] == 0.0
    assert out[0, 1] == pytest.approx(0.5 * 10 ** (-6 / 20), rel=1e-4)
    m = e.read_meters()
    assert m["inputs"]["tts"] == pytest.approx(-6.0 - 6.02, abs=0.1)
    assert m["outputs"]["o1"] <= -89
    assert e.read_meters()["inputs"]["tts"] <= -89  # reset after read


def test_voice_finishes_and_mono_is_upmixed_on_media():
    e = engine({("media_r", "o2")})
    v = e.play(SourceBus.MEDIA, np.full((300,), 0.2, dtype=np.float32))
    e.render(256)
    assert not v.finished
    e.render(256)
    assert v.finished  # no loop bound -> fired synchronously
    assert e.render(256)[:, 1].max() == 0.0


def test_stop_all_fades_out():
    e = engine({("media_l", "o1")})
    e.play(SourceBus.MEDIA, np.full((48000, 2), 0.5, dtype=np.float32))
    e.render(256)
    e.stop_all(fade_ms=5)
    out = np.concatenate([e.render(256) for _ in range(3)])
    assert out[-1, 0] == 0.0
    assert 0 < out[100, 0] < out[0, 0]


def test_test_tone():
    e = engine(set())
    e.set_test_tone("o2", True)
    out = e.render(4800)
    assert np.abs(out[:, 0]).max() == 0.0
    # pink noise at -18 dBFS RMS
    assert 20 * np.log10(np.sqrt(np.mean(out[:, 1] ** 2))) == pytest.approx(-18, abs=1.5)


def test_pink_noise_spectrum_and_loop():
    from autoeventplay.infrastructure.audio.engine import make_pink_noise
    sr, n = 48000, 1 << 17
    x = make_pink_noise(n, sr).astype(np.float64)
    assert 20 * np.log10(np.sqrt(np.mean(x ** 2))) == pytest.approx(-18, abs=0.01)
    spec = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(n, 1 / sr)
    band = lambda lo: spec[(f >= lo) & (f < 2 * lo)].sum()  # noqa: E731 - energy per octave
    # pink: equal energy per octave (white noise would rise by 3 dB per octave)
    for lo in (125, 500, 2000):
        assert 10 * np.log10(band(2 * lo) / band(lo)) == pytest.approx(0, abs=1.0)
    # seamless loop: the jump across the seam is no larger than a normal sample-to-sample step
    steps = np.abs(np.diff(x))
    assert abs(x[0] - x[-1]) < np.percentile(steps, 99.9)
