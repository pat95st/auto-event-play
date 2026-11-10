import asyncio
import json

import pytest

from autoeventplay.domain.model import MixerConfig, Target, TargetKind as K
from autoeventplay.infrastructure.mixers import driver_class, driver_types
from autoeventplay.infrastructure.mixers.allen_heath import qu_level_value, sq_level_value, sq_value_to_db
from autoeventplay.infrastructure.mixers.base import MixerError
from autoeventplay.infrastructure.mixers.transports import osc_decode, osc_encode
from tests.fakes import FakeOscConsole


def drv(type_id, **options):
    return driver_class(type_id)(MixerConfig("m1", "Test", type_id, "127.0.0.1", None, True, options))


def test_registry_contains_required_consoles():
    ids = {t.type_id for t in driver_types()}
    assert {"behringer_wing", "behringer_x32", "behringer_xair", "soundcraft_ui", "ah_qu", "ah_sq", "generic_osc"} <= ids


def test_osc_codec_roundtrip():
    data = osc_encode("/ch/01/mix/fader", 0.75)
    assert len(data) % 4 == 0
    assert osc_decode(data) == ("/ch/01/mix/fader", [pytest.approx(0.75)])
    addr, args = osc_decode(osc_encode("/x", 1, "abc", 2.5))
    assert addr == "/x" and args[0] == 1 and args[1] == "abc" and args[2] == pytest.approx(2.5)


def test_x32_mapping():
    d = drv("behringer_x32")
    p = d.param(Target(K.CHANNEL, 5), "mute")
    assert p.address == "/ch/05/mix/on" and p.encode(True) == 0 and p.encode(False) == 1
    p = d.param(Target(K.CHANNEL, 5), "level")
    assert p.address == "/ch/05/mix/fader" and p.encode(0.0) == pytest.approx(0.75)
    assert d.param(Target(K.MAIN, 1), "mute").address == "/main/st/mix/on"
    assert d.param(Target(K.MAIN, 2), "level").address == "/main/m/mix/fader"
    assert d.param(Target(K.MATRIX, 3), "level").address == "/mtx/03/mix/fader"
    assert d.param(Target(K.DCA, 2), "mute").address == "/dca/2/on"
    assert d.param(Target(K.SEND, 3, 12), "level").address == "/ch/03/mix/12/level"
    mg = d.param(Target(K.MUTE_GROUP, 1), "mute")
    assert mg.address == "/config/mute/1" and mg.encode(True) == 1
    with pytest.raises(MixerError):
        d.validate(Target(K.CHANNEL, 33), "mute")
    with pytest.raises(MixerError):
        d.validate(Target(K.MUTE_GROUP, 1), "level")


def test_xair_and_wing_mapping():
    x = drv("behringer_xair")
    assert x.param(Target(K.BUS, 2), "mute").address == "/bus/2/mix/on"
    assert x.param(Target(K.MAIN), "level").address == "/lr/mix/fader"
    assert x.param(Target(K.FX_RETURN, 1), "level").address == "/rtn/1/mix/fader"
    w = drv("behringer_wing")
    p = w.param(Target(K.CHANNEL, 12), "mute")
    assert p.address == "/ch/12/mute" and p.encode(True) == 1
    p = w.param(Target(K.BUS, 4), "level")
    assert p.address == "/bus/4/fdr" and p.encode(-6.0) == -6.0 and p.encode(None) == -144.0
    assert w.param(Target(K.SEND, 1, 3), "mute").address == "/ch/1/send/3/on"
    assert w.param(Target(K.MUTE_GROUP, 2), "mute").address == "/mgrp/2/mute"


def test_ah_qu_bytes():
    d = drv("ah_qu", midi_channel=2)
    assert d.mute_bytes(Target(K.CHANNEL, 1), True) == [0x91, 0x20, 0x7F, 0x91, 0x20, 0x00]
    assert d.mute_bytes(Target(K.MAIN), False) == [0x91, 0x67, 0x3F, 0x91, 0x67, 0x00]
    assert d.level_bytes(Target(K.CHANNEL, 32), 0.0) == [0xB1, 0x63, 0x3F, 0xB1, 0x62, 0x17, 0xB1, 0x06, 0x62, 0xB1, 0x26, 0x07]
    assert d.level_bytes(Target(K.SEND, 1, 3), 10.0)[-4:] == [0xB1, 0x06, 0x7F, 0xB1, 0x26, 0x02][-4:]
    assert qu_level_value(None) == 0 and qu_level_value(-10) == 0x3F and qu_level_value(10) == 0x7F


def test_ah_sq_bytes_and_parsing():
    d = drv("ah_sq", midi_channel=1, fader_law="audio")
    assert d.mute_bytes(Target(K.MAIN), True) == [0xB0, 0x63, 0x00, 0xB0, 0x62, 0x44, 0xB0, 0x06, 0x00, 0xB0, 0x26, 0x01]
    assert d.mute_bytes(Target(K.DCA, 3), False)[:6] == [0xB0, 0x63, 0x02, 0xB0, 0x62, 0x02]
    lv = d.level_bytes(Target(K.BUS, 1), 0.0)
    assert lv[2] == 0x4F and lv[5] == 0x01 and lv[8] == 0x62 and lv[11] == 0x00
    # Ip6 -> Aux1 = 41 00, Ip48 -> Aux12 = 45 03 (from the protocol document)
    assert d.level_param(Target(K.SEND, 6, 1)) == (0x41, 0x00)
    assert d.level_param(Target(K.SEND, 48, 12)) == (0x45, 0x03)
    assert sq_level_value(0, "linear") == 0x76 * 128 + 0x5C
    assert sq_value_to_db(0x62 * 128, "audio") == pytest.approx(0.0)

    async def query():
        fut_task = asyncio.ensure_future(d._get(0x00, 0x44))

        async def fake_send(data):  # console answers with running status
            d._on_data(bytes([0xB0, 0x63, 0x00, 0x62, 0x44, 0x06, 0x00, 0x26, 0x01]))
        d.send = fake_send  # type: ignore[assignment]
        return await fut_task

    assert asyncio.run(query()) == 1


def test_qu567_model_has_stereo_inputs():
    d = drv("ah_sq", model="qu567")
    kinds = {k.kind: k for k in d.kinds_for(d.config)}
    assert kinds[K.CHANNEL].max == 32 and K.STEREO_IN in kinds
    assert d.mute_param(Target(K.STEREO_IN, 3)) == (0x00, 0x24)


def test_soundcraft_paths():
    d = drv("soundcraft_ui")
    assert d.path(Target(K.CHANNEL, 1)) == "i.0"
    assert d.path(Target(K.BUS, 3)) == "a.2"
    assert d.path(Target(K.SEND, 2, 4)) == "i.1.aux.3"
    assert d.db_to_value(0) == pytest.approx(0.7647)
    assert d.db_to_value(None) == 0.0
    assert d.value_to_db(0.7647) == pytest.approx(0.0, abs=0.01)


def test_generic_osc_from_map():
    mapping = {"bus": {"max": 8, "label": "Bus", "mute": "/bus/{n}/mute", "level": "/bus/{n}/gain", "level_law": "db", "min_db": -100}}
    d = drv("generic_osc", map=json.dumps(mapping))
    kinds = {k.kind for k in d.kinds_for(d.config)}
    assert K.BUS in kinds and K.RAW in kinds
    p = d.param(Target(K.BUS, 3), "level")
    assert p.address == "/bus/3/gain" and p.encode(None) == -100.0 and p.encode(-3.0) == -3.0
    assert d.param(Target(K.BUS, 3), "mute").encode(True) == 1


def test_x32_against_fake_console():
    console = FakeOscConsole().start()
    try:
        async def run():
            d = driver_class("behringer_x32")(MixerConfig("m", "X", "behringer_x32", "127.0.0.1", console.port, True, {}))
            assert "X32" in await d.test()
            await d.set_mute(Target(K.CHANNEL, 7), False)
            await d.set_level(Target(K.CHANNEL, 7), -10.0)
            await asyncio.sleep(0.1)
            assert console.values["/ch/07/mix/on"] == 1
            assert console.values["/ch/07/mix/fader"] == pytest.approx(0.5)
            assert await d.get_mute(Target(K.CHANNEL, 7)) is False
            assert await d.get_level(Target(K.CHANNEL, 7)) == pytest.approx(-10.0, abs=0.01)
            await d.close()
        asyncio.run(run())
    finally:
        console.stop()
