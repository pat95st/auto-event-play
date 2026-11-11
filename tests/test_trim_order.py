"""Input trim and EQ/compressor order: domain, engine and web API."""

from pathlib import Path

import numpy as np
import pytest
from starlette.testclient import TestClient

from autoeventplay.config import AppConfig
from autoeventplay.container import Services
from autoeventplay.domain.dynamics import normalize_dyn
from autoeventplay.domain.eq import normalize_eq
from autoeventplay.domain.model import InputChannel, OutputChannel, RoutingState, SourceBus, normalize_order, normalize_trim
from autoeventplay.infrastructure.audio.engine import AudioEngine
from autoeventplay.web.app import create_app

SR = 48000


def _render_peak(inputs, outputs=None, seconds=1.0, amp=0.25):
    outputs = outputs or [OutputChannel("o1", "o1", "default", 0)]
    e = AudioEngine(backend="null", sample_rate=SR, blocksize=480)
    e.apply_routing(RoutingState(inputs, outputs, {("tts", "o1")}))
    t = np.arange(int(SR * seconds)) / SR
    e.play(SourceBus.TTS, (np.sin(2 * np.pi * 1000 * t) * amp).astype(np.float32)[:, None])
    out = np.concatenate([e.render(480) for _ in range(int(SR * seconds / 480))])
    return float(np.abs(out[len(out) // 2:]).max()), e


def _inputs(tts: InputChannel):
    return [InputChannel("media_l", "L"), InputChannel("media_r", "R"), tts, InputChannel("recording", "A")]


def test_normalizers():
    assert normalize_trim(30) == 24 and normalize_trim(-99) == -24 and normalize_trim("x") == 0 and normalize_trim(3.14) == 3.1
    assert normalize_order("dyn_eq") == "dyn_eq" and normalize_order("bogus") == "eq_dyn"


def test_trim_scales_input():
    flat, _ = _render_peak(_inputs(InputChannel("tts", "T")))
    boosted, e = _render_peak(_inputs(InputChannel("tts", "T", trim_db=6.0)))
    cut, _ = _render_peak(_inputs(InputChannel("tts", "T", trim_db=-12.0)))
    assert boosted / flat == pytest.approx(10 ** (6 / 20), rel=0.02)
    assert cut / flat == pytest.approx(10 ** (-12 / 20), rel=0.02)
    # the input meter follows the trim
    assert e.read_meters()["inputs"]["tts"] == pytest.approx(20 * np.log10(boosted), abs=0.5)


def test_trim_drives_compressor():
    dyn = normalize_dyn({"enabled": True, "threshold": -20, "ratio": 20, "attack": 1, "release": 50, "knee": 0})
    no_trim, _ = _render_peak(_inputs(InputChannel("tts", "T", dyn=dyn)), amp=0.05)  # -26 dBFS: below threshold
    with_trim, _ = _render_peak(_inputs(InputChannel("tts", "T", dyn=dyn, trim_db=18)), amp=0.05)  # -8 dBFS: compressed
    assert no_trim == pytest.approx(0.05, rel=0.05)
    assert 20 * np.log10(with_trim) < -18  # ~ -19.4 dBFS instead of -8


def test_order_changes_result():
    # +12 dB bell at 1 kHz and a hard compressor: EQ first means the boost gets compressed away,
    # compressor first means the boost comes after the compressor.
    eq = normalize_eq({"enabled": True, "bands": [{"on": True, "type": "peak", "freq": 1000, "gain": 12, "q": 1}]})
    dyn = normalize_dyn({"enabled": True, "threshold": -20, "ratio": 20, "attack": 1, "release": 50, "knee": 0})
    eq_first, _ = _render_peak(_inputs(InputChannel("tts", "T", eq=eq, dyn=dyn, order="eq_dyn")))
    dyn_first, _ = _render_peak(_inputs(InputChannel("tts", "T", eq=eq, dyn=dyn, order="dyn_eq")))
    assert 20 * np.log10(dyn_first) - 20 * np.log10(eq_first) == pytest.approx(11.5, abs=1.5)
    # same for outputs
    outs = [OutputChannel("o1", "o1", "default", 0, eq=eq, dyn=dyn, order="dyn_eq")]
    out_dyn_first, _ = _render_peak(_inputs(InputChannel("tts", "T")), outputs=outs)
    assert out_dyn_first == pytest.approx(dyn_first, rel=0.05)


@pytest.fixture()
def client(tmp_path: Path):
    services = Services(AppConfig(data_dir=tmp_path, audio_backend="null", port=0))
    with TestClient(create_app(services)) as cl:
        cl.headers["X-AEP"] = "1"
        assert cl.post("/api/auth/setup", json={"username": "admin", "password": "secret123"}).status_code == 200
        yield cl


def test_api_trim_and_order(client):
    r = client.get("/api/routing").json()["result"]
    assert r["inputs"][0]["trim_db"] == 0 and r["inputs"][0]["order"] == "eq_dyn"
    assert client.post("/api/inputs/tts/trim", json={"trim_db": 40}).json()["result"]["trim_db"] == 24
    assert client.post("/api/inputs/tts/order", json={"order": "dyn_eq"}).status_code == 200
    out_id = r["outputs"][0]["id"] if r["outputs"] else None
    if out_id:
        assert client.post(f"/api/outputs/{out_id}/order", json={"order": "dyn_eq"}).status_code == 200
    bad = client.post("/api/inputs/tts/order", json={"order": "sideways"})
    assert bad.status_code in (400, 422)
    r = client.get("/api/routing").json()["result"]
    tts = next(i for i in r["inputs"] if i["id"] == "tts")
    assert tts["trim_db"] == 24 and tts["order"] == "dyn_eq"
    if out_id:
        assert r["outputs"][0]["order"] == "dyn_eq"
