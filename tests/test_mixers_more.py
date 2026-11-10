"""Yamaha RCP, A&H dLive/Avantis and Soundcraft Si/Vi (HiQnet) drivers."""

import asyncio
import json
import socket
import struct
import threading

import pytest

from autoeventplay.domain.model import MixerConfig, Target, TargetKind as K
from autoeventplay.infrastructure.mixers import driver_class, driver_types
from autoeventplay.infrastructure.mixers.allen_heath import dlive_level_value, dlive_value_to_db
from autoeventplay.infrastructure.mixers.soundcraft_hiqnet import decode_message, decode_params, encode_message, encode_multi_param_set
from autoeventplay.infrastructure.mixers.yamaha import level_value


def drv(type_id, port=None, **options):
    return driver_class(type_id)(MixerConfig("m", "Test", type_id, "127.0.0.1", port, True, options))


def test_new_series_registered():
    ids = {t.type_id for t in driver_types()}
    assert {"yamaha_tf", "yamaha_ql", "yamaha_cl", "yamaha_dm3", "yamaha_dm5", "yamaha_dm7",
            "ah_avantis", "ah_dlive", "soundcraft_si", "soundcraft_vi"} <= ids


# ---------------------------------------------------------------- Yamaha

def test_yamaha_rcp_lines():
    d = drv("yamaha_cl", model="cl5")
    assert d.set_line(Target(K.CHANNEL, 1), "level", -10.0) == "set MIXER:Current/InCh/Fader/Level 0 0 -1000\n"
    assert d.set_line(Target(K.CHANNEL, 72), "mute", True) == "set MIXER:Current/InCh/Fader/On 71 0 0\n"
    assert d.set_line(Target(K.CHANNEL, 5), "mute", False) == "set MIXER:Current/InCh/Fader/On 4 0 1\n"
    assert d.set_line(Target(K.BUS, 3), "level", None) == "set MIXER:Current/Mix/Fader/Level 2 0 -32768\n"
    assert d.set_line(Target(K.MATRIX, 8), "level", 10) == "set MIXER:Current/Mtrx/Fader/Level 7 0 1000\n"
    assert d.set_line(Target(K.MAIN, 1), "mute", True) == "set MIXER:Current/St/Fader/On 0 0 0\n"
    assert d.set_line(Target(K.MAIN, 2), "level", 0) == "set MIXER:Current/Mono/Fader/Level 0 0 0\n"
    assert d.set_line(Target(K.DCA, 2), "level", -5) == "set MIXER:Current/DCA/Fader/Level 1 0 -500\n"
    assert d.set_line(Target(K.MUTE_GROUP, 3), "mute", True) == "set MIXER:Current/MuteMaster/On 2 0 1\n"
    assert d.set_line(Target(K.SEND, 4, 2), "level", -20) == "set MIXER:Current/InCh/ToMix/Level 3 1 -2000\n"
    assert d.set_line(Target(K.RAW, raw="MIXER:Current/StInCh/Fader/On 1 0"), "mute", True) == "set MIXER:Current/StInCh/Fader/On 1 0 0\n"
    assert level_value(None) == -32768 and level_value(3.456) == 346


def test_yamaha_models_differ():
    tf = drv("yamaha_tf", model="tf5")
    kinds = {k.kind: k for k in tf.kinds_for(tf.config)}
    assert kinds[K.CHANNEL].max == 40 and K.MATRIX not in kinds and K.FX_RETURN in kinds
    dm7 = drv("yamaha_dm7", model="dm7c")
    kinds = {k.kind: k for k in dm7.kinds_for(dm7.config)}
    assert kinds[K.CHANNEL].max == 72 and kinds[K.MAIN].max == 2
    assert dm7.set_line(Target(K.MAIN, 2), "level", 0) == "set MIXER:Current/St/Fader/Level 1 0 0\n"


class FakeRcp:
    def __init__(self):
        self.values = {"MIXER:Current/InCh/Fader/Level 0 0": "-1000"}
        self.lines = []
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        threading.Thread(target=self.run, daemon=True).start()

    @property
    def port(self):
        return self.srv.getsockname()[1]

    def run(self):
        conn, _ = self.srv.accept()
        buf = b""
        while True:
            data = conn.recv(4096)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                parts = line.decode().split()
                self.lines.append(line.decode())
                key = " ".join(parts[1:4])
                if parts[0] == "set":
                    self.values[key] = parts[4]
                    conn.sendall(f"OK {line.decode()}\n".encode())
                elif parts[0] == "get":
                    conn.sendall(f"OK get {key} {self.values.get(key, '0')}\n".encode())


def test_yamaha_against_fake_console():
    fake = FakeRcp()

    async def run():
        d = drv("yamaha_ql", port=fake.port, model="ql5")
        assert "-10.0 dB" in await d.test()
        await d.set_mute(Target(K.CHANNEL, 3), True)
        await d.set_level(Target(K.CHANNEL, 3), -6.5)
        assert await d.get_mute(Target(K.CHANNEL, 3)) is True
        assert await d.get_level(Target(K.CHANNEL, 3)) == pytest.approx(-6.5)
        await d.close()

    asyncio.run(run())
    assert "set MIXER:Current/InCh/Fader/On 2 0 0" in fake.lines


# ---------------------------------------------------------------- dLive / Avantis

def test_dlive_level_law():
    assert dlive_level_value(0) == 0x6B and dlive_level_value(10) == 0x7F and dlive_level_value(None) == 0
    assert dlive_level_value(-10) == 0x57
    assert dlive_value_to_db(0x6B) == pytest.approx(0.0, abs=0.3)


def test_dlive_messages():
    d = drv("ah_dlive", midi_channel=12)  # N = 0x0B
    assert d.mute_bytes(Target(K.CHANNEL, 1), True) == [0x9B, 0x00, 0x7F, 0x9B, 0x00, 0x00]
    assert d.mute_bytes(Target(K.DCA, 1), False) == [0x9F, 0x36, 0x3F, 0x9F, 0x36, 0x00]
    assert d.mute_bytes(Target(K.MUTE_GROUP, 8), True)[1] == 0x55
    assert d.level_bytes(Target(K.BUS, 63), 0.0) == [0xBD, 0x63, 0x40, 0xBD, 0x62, 0x17, 0xBD, 0x06, 0x6B]  # stereo aux 1
    assert d.level_bytes(Target(K.MAIN, 1), 0.0)[2] == 0x30
    assert d.level_bytes(Target(K.SEND, 2, 1), -10.0) == [0xF0, 0x00, 0x00, 0x1A, 0x50, 0x10, 0x01, 0x00, 0x0B, 0x0D, 0x01, 0x0D, 0x00, 0x57, 0xF7]

    async def get():
        task = asyncio.ensure_future(d._get([0], (0x0B, 0x05, "level")))
        await asyncio.sleep(0)
        d._on_data(bytes([0xBB, 0x63, 0x05, 0x62, 0x17, 0x06, 0x6B]))  # running status
        return await task

    d.send = lambda data: asyncio.sleep(0)  # type: ignore[assignment]
    assert asyncio.run(get()) == 0x6B


def test_avantis_ranges():
    d = drv("ah_avantis", midi_channel=1)
    assert d.mute_bytes(Target(K.MUTE_GROUP, 1), True)[:2] == [0x94, 0x46]
    kinds = {k.kind: k for k in d.kinds_for(d.config)}
    assert kinds[K.CHANNEL].max == 96 and kinds[K.MAIN].max == 3
    with pytest.raises(Exception):
        drv("ah_avantis", midi_channel=13).address(Target(K.DCA, 1))  # 13 + 4 > 16


# ---------------------------------------------------------------- Soundcraft HiQnet

def test_hiqnet_encoding_and_mapping():
    mapping = {"channel": {"max": 24, "vd": 3, "object": 0x100, "mute_sv": 1, "mute_type": "ubyte",
                           "level_sv": 2, "level_type": "long", "level_law": "db100"}}
    d = drv("soundcraft_si", device=7, source_device=1337, map=json.dumps(mapping))
    msg = d.set_message(Target(K.CHANNEL, 5), "level", -12.5)
    msg_id, flags, src, payload = decode_message(msg)
    assert msg[0] == 2 and msg[1] == 25 and struct.unpack(">I", msg[2:6])[0] == len(msg)
    assert msg_id == 0x0100 and src == bytes([0x05, 0x39, 0, 0, 0, 0])
    assert msg[12:18] == bytes([0x00, 0x07, 0x03, 0x00, 0x01, 0x04])  # device 7, vd 3, object 0x104
    assert decode_params(payload) == {2: -1250}
    assert decode_params(encode_multi_param_set([(1, "ubyte", 1), (9, "float", 0.5)])) == {1: 1, 9: 0.5}
    assert decode_message(encode_message(b"\0" * 6, b"\0" * 6, 0x0103, b"\x00\x00", 1))[0] == 0x0103
    kinds = {k.kind for k in d.kinds_for(d.config)}
    assert K.CHANNEL in kinds and K.RAW in kinds
