"""Yamaha LS9 (EtherMIDI) and Mackie DL drivers against fake consoles."""

import asyncio
import socket
import struct
import threading

import pytest

from autoeventplay.domain.model import MixerConfig, Target, TargetKind as K
from autoeventplay.infrastructure.mixers import driver_class
from autoeventplay.infrastructure.mixers.mackie_dl import decode_messages, encode_message
from autoeventplay.infrastructure.mixers.yamaha_ls9 import challenge_crc, decode_value, encode_value, fader_db, fader_raw


def drv(type_id, port, **options):
    return driver_class(type_id)(MixerConfig("m", "T", type_id, "127.0.0.1", port, True, options))


class FakeServer:
    def __init__(self, handler):
        self.handler = handler
        self.received = b""
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
            try:
                data = conn.recv(4096)
            except OSError:
                break
            if not data:
                break
            self.received += data
            buf += data
            buf = self.handler(conn, buf)


# ---------------------------------------------------------------- LS9

def test_ls9_value_encoding_and_fader():
    assert encode_value(823) == [0, 0, 0, 0x06, 0x37]
    assert decode_value([0, 0, 0, 0x06, 0x37]) == 823
    assert fader_raw(0) == 823 and fader_raw(10) == 1023 and fader_raw(None) == 0
    assert fader_db(823) == pytest.approx(0.0)
    assert 0 <= challenge_crc(0x12, bytes(6)) <= 0xFFFF


def test_ls9_param_change_matches_documented_example():
    d = drv("yamaha_ls9", 1, device_number=1)
    # INPUT 56 fader to 0 dB (from the protocol notes)
    assert d.param_change(0x33, 0, 55, 823).hex(" ") == "f0 43 10 3e 12 01 00 33 00 00 00 37 00 00 00 06 37 f7"
    pkt = d.sysex_packet(d.param_change(0x33, 0, 55, 823))
    assert struct.unpack(">I", pkt[:4])[0] == len(pkt) == 38 and pkt[4] == 0x16 and pkt[12:16] == b"\xff" * 4
    assert len(d.set_packets(Target(K.MAIN, 1), "mute", True)) == 2 * 38  # stereo L + R


def ls9_handler(conn, buf):
    while len(buf) >= 16:
        total = struct.unpack(">I", buf[:4])[0]
        if len(buf) < total:
            break
        pkt, buf = buf[:total], buf[total:]
        if pkt[4] == 0x20:
            conn.sendall(struct.pack(">IBBBB", 16, 0x21, 0, 0, 1) + bytes(4) + struct.pack(">I", 0))
        elif pkt[4] == 0x16:
            sx = pkt[20:]
            if sx[2] & 0xF0 == 0x30:  # request -> answer with value 623 (-10 dB)
                reply = bytes([0xF0, 0x43, 0x10 | (sx[2] & 0x0F)]) + sx[3:12] + bytes(encode_value(623)) + b"\xf7"
                n = len(reply)
                conn.sendall(struct.pack(">IBBBB", 20 + n, 0x16, 0, 0, 1) + struct.pack(">I", n) + b"\xff" * 4 + struct.pack(">I", n) + reply)
    return buf


def test_ls9_against_fake_console():
    fake = FakeServer(ls9_handler)

    async def run():
        d = drv("yamaha_ls9", fake.port, model="ls9-16")
        assert "-10.0 dB" in await d.test()
        await d.set_mute(Target(K.BUS, 2), True)
        assert await d.get_level(Target(K.CHANNEL, 3)) == pytest.approx(-10.0)
        await d.close()

    asyncio.run(run())
    assert bytes.fromhex("f0 43 10 3e 12 01 00 4c 00 00 00 01 00 00 00 00 00 f7") in fake.received  # mix 2 ON = 0


# ---------------------------------------------------------------- Mackie

def test_mackie_framing_roundtrip():
    msg = encode_message(3, 0, 0x13, struct.pack(">iIf", 49, 0x10500, -10.0))
    assert msg[0] == 0xAB and msg[1] == 3 and struct.unpack(">H", msg[2:4])[0] == 3
    assert struct.unpack(">H", msg[6:8])[0] == (0xFFFF - sum(msg[:6])) & 0xFFFF
    msgs, rest = decode_messages(msg + encode_message(4, 1, 0x01)[:5])
    assert msgs == [(3, 0, 0x13, msg[8:20])] and len(rest) == 5


def test_mackie_addresses():
    d = drv("mackie_dl", 1, model="dl32")
    assert d.address(Target(K.CHANNEL, 1), "level") == (49, "float")
    assert d.address(Target(K.CHANNEL, 2), "mute") == (41 + 132 + 7, "int")
    assert d.address(Target(K.MAIN), "mute") == (5996, "int")
    assert d.address(Target(K.SEND, 1, 2), "level") == (41 + 58 + 3, "float")
    d16 = drv("mackie_dl", 1, model="dl16s")
    assert d16.address(Target(K.BUS, 1), "level") == (2606 + 4, "float")


def mackie_handler(conn, buf):
    msgs, rest = decode_messages(buf)
    for seq, mtype, sub, body in msgs:
        if mtype != 0:
            continue
        reply = b""
        if sub == 0x03:
            reply = bytes([0x10, 0x05]) + bytes(14)  # DL32R-style handshake
        conn.sendall(encode_message(seq, 1, sub, reply))
        if sub == 0x06 and body == bytes([0, 0, 0, 6]):
            # push channel values: input 1 mute=1, fader=-6 dB
            values = struct.pack(">iI", 48, (2 << 16) | 0x0500) + struct.pack(">i", 1) + struct.pack(">f", -6.0)
            conn.sendall(encode_message(1, 0, 0x13, values))
    return rest


def test_mackie_against_fake_console():
    fake = FakeServer(mackie_handler)

    async def run():
        d = drv("mackie_dl", fake.port)
        assert "DL32" in await d.test()
        await asyncio.sleep(0.1)
        assert await d.get_mute(Target(K.CHANNEL, 1)) is True
        assert await d.get_level(Target(K.CHANNEL, 1)) == pytest.approx(-6.0)
        await d.set_level(Target(K.MAIN), None)
        await d.close()

    asyncio.run(run())
    assert struct.pack(">iIf", 5995, 0x10500, -120.0) in fake.received
