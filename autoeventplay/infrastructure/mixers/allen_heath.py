"""Allen & Heath consoles – MIDI over TCP (port 51325).

* Qu (classic: Qu-16, Qu-24, Qu-32, Qu-Pac, Qu-SB, firmware ≥ 1.9):
  mutes as Note On, faders as NRPN 0x17, sends as NRPN 0x20.
* SQ (SQ-5, SQ-6, SQ-7, firmware ≥ 1.5) and the new Qu series (Qu-5, Qu-6,
  Qu-7): everything as 14-bit NRPN with the SQ parameter map.

The console's MIDI channel (Setup › Control › MIDI) must match the option
"MIDI-Kanal". Qu classic accepts only one TCP-MIDI connection at a time.
"""

from __future__ import annotations

import asyncio
import logging

from ...domain.levels import MIN_DB, interpolate_table, is_off
from ...domain.model import MixerConfig, Target, TargetKind
from .base import KindSpec, MixerDriver, MixerError, OptionSpec, register_driver
from .transports import TcpClient

log = logging.getLogger("aep.mixer.ah")
K = TargetKind

MIDI_CHANNEL_OPTION = OptionSpec("midi_channel", "MIDI-Kanal", "int", 1, help="Wie am Pult unter Setup › MIDI eingestellt (1–16).")


class _AhBase(MixerDriver):
    family = "Allen & Heath"
    default_port = 51325
    transport = "MIDI/TCP"

    def __init__(self, config: MixerConfig):
        super().__init__(config)
        self._tcp = TcpClient(self.host, self.port, on_data=self._on_data)

    @property
    def n(self) -> int:
        ch = int(self.option("midi_channel", 1) or 1)
        if not 1 <= ch <= 16:
            raise MixerError("MIDI-Kanal muss zwischen 1 und 16 liegen")
        return ch - 1

    def _on_data(self, data: bytes) -> None:  # pragma: no cover - overridden where needed
        return None

    async def connect(self) -> None:
        await self._tcp.connect()

    async def close(self) -> None:
        await self._tcp.close()

    async def send(self, data: bytes | list[int]) -> None:
        await self._tcp.send(bytes(data))

    async def test(self) -> str:
        await self.connect()
        return f"TCP-Verbindung zu {self.host}:{self.port} steht (MIDI-Kanal {self.n + 1})"


# =========================================================================== Qu classic

# fader / send value table from the Qu MIDI protocol (dB -> 7-bit value)
QU_LEVEL_TABLE = [
    (-89.0, 0x01), (-75.0, 0x03), (-60.0, 0x06), (-45.0, 0x0C), (-40.0, 0x10), (-35.0, 0x17), (-30.0, 0x1F),
    (-25.0, 0x27), (-20.0, 0x2F), (-15.0, 0x36), (-10.0, 0x3F), (-5.0, 0x4F), (0.0, 0x62), (5.0, 0x72), (10.0, 0x7F),
]


def qu_level_value(db: float | None) -> int:
    if is_off(db):
        return 0
    return int(round(interpolate_table(float(db), QU_LEVEL_TABLE)))


QU_MIX_LABELS = {1: "Mix 1", 2: "Mix 2", 3: "Mix 3", 4: "Mix 4", 5: "Mix 5-6", 6: "Mix 7-8", 7: "Mix 9-10"}


@register_driver
class AllenHeathQuDriver(_AhBase):
    type_id = "ah_qu"
    display_name = "Allen & Heath Qu (Qu-16/24/32/Pac/SB)"
    kinds = [
        KindSpec(K.CHANNEL, "Kanal", 32),
        KindSpec(K.STEREO_IN, "Stereo-Eingang", 3, number_labels={1: "ST1", 2: "ST2", 3: "ST3"}),
        KindSpec(K.FX_RETURN, "FX Return", 4),
        KindSpec(K.BUS, "Mix", 7, number_labels=QU_MIX_LABELS),
        KindSpec(K.GROUP, "Gruppe", 4, number_labels={1: "Grp 1-2", 2: "Grp 3-4", 3: "Grp 5-6", 4: "Grp 7-8"}),
        KindSpec(K.FX_SEND, "FX-Send", 4),
        KindSpec(K.MATRIX, "Matrix", 2, number_labels={1: "Mtx 1-2", 2: "Mtx 3-4"}),
        KindSpec(K.MAIN, "Main LR", 1),
        KindSpec(K.DCA, "DCA", 4),
        KindSpec(K.MUTE_GROUP, "Mute-Gruppe", 4, level=False),
        KindSpec(K.SEND, "Kanal-Send", 32, mute=False, dest_label="Mix", dest_max=7, dest_labels=QU_MIX_LABELS),
    ]
    option_specs = [MIDI_CHANNEL_OPTION]
    notes = ("Qu-Firmware ≥ 1.9. Das Pult kann seinen Zustand nicht melden – „Zurücksetzen“ nutzt deshalb "
             "die Gegenaktion bzw. den eingetragenen Rücksetz-Pegel.")

    _ch_base = {K.CHANNEL: 0x20, K.STEREO_IN: 0x40, K.FX_RETURN: 0x08, K.BUS: 0x60, K.GROUP: 0x68,
                K.FX_SEND: 0x00, K.MATRIX: 0x6C, K.DCA: 0x10, K.MUTE_GROUP: 0x50}

    def channel_byte(self, target: Target) -> int:
        if target.kind == K.MAIN:
            return 0x67
        if target.kind == K.SEND:
            return 0x20 + (target.number or 1) - 1
        return self._ch_base[target.kind] + (target.number or 1) - 1

    def mute_bytes(self, target: Target, muted: bool) -> list[int]:
        ch = self.channel_byte(target)
        st = 0x90 | self.n
        return [st, ch, 0x7F if muted else 0x3F, st, ch, 0x00]

    def level_bytes(self, target: Target, db: float | None) -> list[int]:
        b = 0xB0 | self.n
        ch = self.channel_byte(target)
        va = qu_level_value(db)
        if target.kind == K.SEND:
            return [b, 0x63, ch, b, 0x62, 0x20, b, 0x06, va, b, 0x26, (target.dest or 1) - 1]
        return [b, 0x63, ch, b, 0x62, 0x17, b, 0x06, va, b, 0x26, 0x07]

    async def set_mute(self, target: Target, muted: bool) -> None:
        self.validate(target, "mute")
        await self.send(self.mute_bytes(target, muted))

    async def set_level(self, target: Target, db: float | None) -> None:
        self.validate(target, "level")
        await self.send(self.level_bytes(target, db))


# =========================================================================== SQ / Qu-5/6/7


def _parse_table(text: str) -> list[tuple[float, int]]:
    out: list[tuple[float, int]] = []
    for cell in text.replace("\n", "|").split("|"):
        parts = cell.split()
        if len(parts) != 3 or parts[0] == "-inf":
            continue
        out.append((float(parts[0]), int(parts[1], 16) * 128 + int(parts[2], 16)))
    return sorted(out)


# From the SQ / Qu-5/6/7 MIDI protocol documents (dB, coarse, fine).
SQ_LINEAR = _parse_table("""
-45 4C 7D | -29 5B 69 | -19 65 0C | -9 6E 2F | +1 77 53
-89 24 16 | -40 51 4F | -28 5C 60 | -18 66 03 | -8 6F 26 | +2 78 49
-85 27 71 | -38 53 3C | -27 5D 56 | -17 66 7A | -7 70 1D | +3 79 40
-80 2C 42 | -36 55 2A | -26 5E 4D | -16 67 70 | -6 71 14 | +4 7A 37
-75 31 14 | -35 56 21 | -25 5F 44 | -15 68 67 | -5 72 0A | +5 7B 2E
-70 35 65 | -34 57 17 | -24 60 3B | -14 69 5E | -4 73 01 | +6 7C 24
-65 3A 37 | -33 58 0E | -23 61 31 | -13 6A 55 | -3 73 78 | +7 7D 1B
-60 3F 09 | -32 59 05 | -22 62 28 | -12 6B 4B | -2 74 6F | +8 7E 12
-55 43 5A | -31 59 7C | -21 63 1F | -11 6C 42 | -1 75 65 | +9 7F 08
-50 48 2C | -30 5A 72 | -20 64 16 | -10 6D 39 |  0 76 5C | +10 7F 7F
""")
SQ_AUDIO = _parse_table("""
-45 0C 00 | -29 20 40 | -19 30 00 | -9 41 40 | +1 65 40
-89 01 40 | -40 0F 40 | -28 22 00 | -18 31 40 | -8 44 40 | +2 69 00
-85 02 00 | -38 12 40 | -27 23 40 | -17 33 00 | -7 48 00 | +3 6C 40
-80 02 40 | -36 15 40 | -26 25 00 | -16 34 40 | -6 4B 00 | +4 70 00
-75 03 40 | -35 17 00 | -25 26 40 | -15 36 00 | -5 4E 40 | +5 73 40
-70 04 00 | -34 19 00 | -24 28 40 | -14 38 00 | -4 52 40 | +6 75 40
-65 05 00 | -33 1A 40 | -23 2A 00 | -13 39 40 | -3 56 40 | +7 78 00
-60 06 00 | -32 1C 00 | -22 2B 40 | -12 3B 00 | -2 5A 00 | +8 7A 40
-55 07 00 | -31 1D 40 | -21 2D 00 | -11 3C 40 | -1 5E 00 | +9 7D 00
-50 08 00 | -30 1F 00 | -20 2E 40 | -10 3E 00 |  0 62 00 | +10 7F 40
""")


def sq_level_value(db: float | None, law: str = "audio") -> int:
    if is_off(db):
        return 0
    table = SQ_LINEAR if law == "linear" else SQ_AUDIO
    return int(round(interpolate_table(float(db), [(x, float(y)) for x, y in table])))


def sq_value_to_db(value: int, law: str = "audio") -> float:
    if value <= 0:
        return MIN_DB
    table = SQ_LINEAR if law == "linear" else SQ_AUDIO
    inverse = sorted((float(v), x) for x, v in table)
    if value < inverse[0][0]:
        return MIN_DB
    return interpolate_table(float(value), inverse)


SQ_MODELS = {
    "sq": {"inputs": 48, "groups": 12, "fx_returns": 8, "mixes": 12, "matrices": 3, "stereo": 0},
    "qu567": {"inputs": 32, "groups": 12, "fx_returns": 6, "mixes": 12, "matrices": 3, "stereo": 3},
}
QU567_STEREO_LSB = {1: 0x20, 2: 0x22, 3: 0x24}


@register_driver
class AllenHeathSqDriver(_AhBase):
    type_id = "ah_sq"
    display_name = "Allen & Heath SQ / Qu-5/6/7"
    can_query = True
    option_specs = [
        MIDI_CHANNEL_OPTION,
        OptionSpec("model", "Modell", "select", "sq", choices=[("sq", "SQ-5 / SQ-6 / SQ-7"), ("qu567", "Qu-5 / Qu-6 / Qu-7 (neue Qu-Serie)")]),
        OptionSpec("fader_law", "NRPN-Faderkurve", "select", "audio",
                   choices=[("audio", "Audio Taper"), ("linear", "Linear Taper")],
                   help="Muss zur Einstellung „NRPN Fader Law“ am Pult passen."),
    ]
    notes = "SQ-Firmware ≥ 1.5 bzw. Qu-5/6/7. Pegel und Mutes können abgefragt werden (exaktes Zurücksetzen)."

    @classmethod
    def kinds_for(cls, config: MixerConfig | None) -> list[KindSpec]:
        model = SQ_MODELS.get((config.options.get("model") if config else None) or "sq", SQ_MODELS["sq"])
        kinds = [
            KindSpec(K.CHANNEL, "Kanal", model["inputs"]),
            KindSpec(K.GROUP, "Gruppe", model["groups"]),
            KindSpec(K.FX_RETURN, "FX Return", model["fx_returns"]),
            KindSpec(K.BUS, "Mix/Aux", model["mixes"]),
            KindSpec(K.FX_SEND, "FX-Send", 4),
            KindSpec(K.MATRIX, "Matrix", model["matrices"]),
            KindSpec(K.MAIN, "Main LR", 1),
            KindSpec(K.DCA, "DCA", 8),
            KindSpec(K.MUTE_GROUP, "Mute-Gruppe", 8, level=False),
            KindSpec(K.SEND, "Kanal-Send", model["inputs"], mute=False, dest_label="Mix", dest_max=model["mixes"]),
        ]
        if model["stereo"]:
            kinds.insert(1, KindSpec(K.STEREO_IN, "Stereo-Eingang", 3, number_labels={1: "ST1", 2: "ST2", 3: "USB"}))
        return kinds

    def __init__(self, config: MixerConfig):
        super().__init__(config)
        self._rx_running = 0
        self._rx: dict[int, int] = {}
        self._rx_buffer: list[int] = []
        self._waiters: dict[tuple[int, int], list[asyncio.Future[int]]] = {}

    @property
    def law(self) -> str:
        return "linear" if self.option("fader_law", "audio") == "linear" else "audio"

    def mute_param(self, t: Target) -> tuple[int, int]:
        n = (t.number or 1) - 1
        k = t.kind
        if k == K.CHANNEL:
            return 0x00, n
        if k == K.STEREO_IN:
            return 0x00, QU567_STEREO_LSB[n + 1]
        if k == K.GROUP:
            return 0x00, 0x30 + n
        if k == K.FX_RETURN:
            return 0x00, 0x3C + n
        if k == K.MAIN:
            return 0x00, 0x44
        if k == K.BUS:
            return 0x00, 0x45 + n
        if k == K.FX_SEND:
            return 0x00, 0x51 + n
        if k == K.MATRIX:
            return 0x00, 0x55 + n
        if k == K.DCA:
            return 0x02, n
        if k == K.MUTE_GROUP:
            return 0x04, n
        raise MixerError(f"nicht unterstützt: {k.value}")

    def level_param(self, t: Target) -> tuple[int, int]:
        n = (t.number or 1) - 1
        k = t.kind
        if k == K.CHANNEL:
            return 0x40, n
        if k == K.STEREO_IN:
            return 0x40, QU567_STEREO_LSB[n + 1]
        if k == K.GROUP:
            return 0x40, 0x30 + n
        if k == K.FX_RETURN:
            return 0x40, 0x3C + n
        if k == K.MAIN:
            return 0x4F, 0x00
        if k == K.BUS:
            return 0x4F, 0x01 + n
        if k == K.FX_SEND:
            return 0x4F, 0x0D + n
        if k == K.MATRIX:
            return 0x4F, 0x11 + n
        if k == K.DCA:
            return 0x4F, 0x20 + n
        if k == K.SEND:
            p = (0x40 << 7 | 0x44) + n * 12 + ((t.dest or 1) - 1)
            return p >> 7, p & 0x7F
        raise MixerError(f"nicht unterstützt: {k.value}")

    def _nrpn(self, msb: int, lsb: int, vc: int, vf: int) -> list[int]:
        b = 0xB0 | self.n
        return [b, 0x63, msb, b, 0x62, lsb, b, 0x06, vc & 0x7F, b, 0x26, vf & 0x7F]

    def mute_bytes(self, target: Target, muted: bool) -> list[int]:
        msb, lsb = self.mute_param(target)
        return self._nrpn(msb, lsb, 0, 1 if muted else 0)

    def level_bytes(self, target: Target, db: float | None) -> list[int]:
        msb, lsb = self.level_param(target)
        v = sq_level_value(db, self.law)
        return self._nrpn(msb, lsb, v >> 7, v & 0x7F)

    async def set_mute(self, target: Target, muted: bool) -> None:
        self.validate(target, "mute")
        await self.send(self.mute_bytes(target, muted))

    async def set_level(self, target: Target, db: float | None) -> None:
        self.validate(target, "level")
        await self.send(self.level_bytes(target, db))

    # ------------------------------------------------------------- queries

    async def _get(self, msb: int, lsb: int, timeout: float = 0.6) -> int | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[int] = loop.create_future()
        self._waiters.setdefault((msb, lsb), []).append(fut)
        b = 0xB0 | self.n
        await self.send([b, 0x63, msb, b, 0x62, lsb, b, 0x60, 0x7F])
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            lst = self._waiters.get((msb, lsb), [])
            if fut in lst:
                lst.remove(fut)

    async def get_mute(self, target: Target) -> bool | None:
        self.validate(target, "mute")
        v = await self._get(*self.mute_param(target))
        return None if v is None else bool(v & 0x7F)

    async def get_level(self, target: Target) -> float | None:
        self.validate(target, "level")
        v = await self._get(*self.level_param(target))
        return None if v is None else sq_value_to_db(v, self.law)

    def _on_data(self, data: bytes) -> None:
        """Parse NRPN replies (with running status) and resolve pending queries."""
        for byte in data:
            if byte & 0x80:
                if 0xF8 <= byte:  # realtime, ignore
                    continue
                self._rx_running = byte
                self._rx_buffer = []
                continue
            if (self._rx_running & 0xF0) != 0xB0:
                continue
            self._rx_buffer.append(byte)
            if len(self._rx_buffer) < 2:
                continue
            cc, value = self._rx_buffer
            self._rx_buffer = []
            self._rx[cc] = value
            if cc == 0x26:
                key = (self._rx.get(0x63, -1), self._rx.get(0x62, -1))
                full = self._rx.get(0x06, 0) * 128 + value
                for fut in self._waiters.pop(key, []):
                    if not fut.done():
                        fut.set_result(full)


# =========================================================================== dLive / Avantis

# Channel selection: (MIDI channel offset from base N, first note, count)
DLIVE_LAYOUT = {
    "dlive": {
        "inputs": (0, 0x00, 128),
        "mono_group": (1, 0x00, 62), "stereo_group": (1, 0x40, 31),
        "mono_aux": (2, 0x00, 62), "stereo_aux": (2, 0x40, 31),
        "mono_matrix": (3, 0x00, 62), "stereo_matrix": (3, 0x40, 31),
        "mono_fx_send": (4, 0x00, 16), "stereo_fx_send": (4, 0x10, 16),
        "fx_return": (4, 0x20, 16), "main": (4, 0x30, 6), "dca": (4, 0x36, 24), "mute_group": (4, 0x4E, 8),
    },
    "avantis": {
        "inputs": (0, 0x00, 96),
        "mono_group": (1, 0x00, 54), "stereo_group": (1, 0x40, 27),
        "mono_aux": (2, 0x00, 54), "stereo_aux": (2, 0x40, 27),
        "mono_matrix": (3, 0x00, 54), "stereo_matrix": (3, 0x40, 27),
        "mono_fx_send": (4, 0x00, 12), "stereo_fx_send": (4, 0x10, 12),
        "fx_return": (4, 0x20, 12), "main": (4, 0x30, 3), "dca": (4, 0x36, 16), "mute_group": (4, 0x46, 8),
    },
}
AH_SYSEX_HEADER = [0xF0, 0x00, 0x00, 0x1A, 0x50, 0x10, 0x01, 0x00]


def dlive_level_value(db: float | None) -> int:
    """LV = (dB + 54) / 64 × 127, -∞ = 0, +10 dB = 0x7F, 0 dB = 0x6B."""
    if is_off(db):
        return 0
    v = (max(-54.0, min(10.0, float(db))) + 54.0) / 64.0 * 127.0
    return max(0, min(0x7F, int(round(v))))


def dlive_value_to_db(lv: int) -> float:
    return MIN_DB if lv <= 0 else lv / 127.0 * 64.0 - 54.0


def _mono_stereo_labels(mono: int, stereo: int, mono_label: str, stereo_label: str) -> dict[int, str]:
    labels = {i: f"{mono_label} {i}" for i in range(1, mono + 1)}
    labels.update({mono + i: f"{stereo_label} {i}" for i in range(1, stereo + 1)})
    return labels


class _AhDliveBase(_AhBase):
    platform = "dlive"
    can_query = True
    option_specs = [OptionSpec("midi_channel", "MIDI-Basiskanal", "int", 12,
                               help="Wie am Pult unter Utility › Control › MIDI (belegt Basiskanal bis Basiskanal + 4).")]

    @classmethod
    def kinds_for(cls, config: MixerConfig | None) -> list[KindSpec]:
        L = DLIVE_LAYOUT[cls.platform]
        mg, sg = L["mono_group"][2], L["stereo_group"][2]
        ma, sa = L["mono_aux"][2], L["stereo_aux"][2]
        mm, sm = L["mono_matrix"][2], L["stereo_matrix"][2]
        mf, sf = L["mono_fx_send"][2], L["stereo_fx_send"][2]
        aux_labels = _mono_stereo_labels(ma, sa, "Aux", "St-Aux")
        return [
            KindSpec(K.CHANNEL, "Kanal", L["inputs"][2]),
            KindSpec(K.GROUP, "Gruppe", mg + sg, number_labels=_mono_stereo_labels(mg, sg, "Grp", "St-Grp")),
            KindSpec(K.BUS, "Aux", ma + sa, number_labels=aux_labels),
            KindSpec(K.MATRIX, "Matrix", mm + sm, number_labels=_mono_stereo_labels(mm, sm, "Mtx", "St-Mtx")),
            KindSpec(K.FX_SEND, "FX-Send", mf + sf, number_labels=_mono_stereo_labels(mf, sf, "FX", "St-FX")),
            KindSpec(K.FX_RETURN, "FX Return", L["fx_return"][2]),
            KindSpec(K.MAIN, "Main", L["main"][2]),
            KindSpec(K.DCA, "DCA", L["dca"][2]),
            KindSpec(K.MUTE_GROUP, "Mute-Gruppe", L["mute_group"][2], level=False),
            KindSpec(K.SEND, "Kanal → Aux", L["inputs"][2], mute=False, dest_label="Aux", dest_max=ma + sa, dest_labels=aux_labels),
        ]

    def __init__(self, config: MixerConfig):
        super().__init__(config)
        self._rx_status = 0
        self._rx_data: list[int] = []
        self._rx_cc: dict[int, dict[int, int]] = {}
        self._waiters: dict[tuple[int, int, str], list[asyncio.Future[int]]] = {}

    def _split(self, key_mono: str, key_stereo: str, n: int) -> tuple[int, int]:
        L = DLIVE_LAYOUT[self.platform]
        off, first, count = L[key_mono]
        if n <= count:
            return off, first + n - 1
        off, first, count_st = L[key_stereo]
        if n - count > count_st:
            raise MixerError("Nummer außerhalb des Bereichs")
        return off, first + (n - count) - 1

    def address(self, target: Target) -> tuple[int, int]:
        """(MIDI channel 0..15, note/CH) for a target."""
        n = target.number or 1
        L = DLIVE_LAYOUT[self.platform]
        k = target.kind
        if k in (K.CHANNEL, K.SEND):
            off, first, _ = L["inputs"]
            ch = (off, first + n - 1)
        elif k == K.GROUP:
            ch = self._split("mono_group", "stereo_group", n)
        elif k == K.BUS:
            ch = self._split("mono_aux", "stereo_aux", n)
        elif k == K.MATRIX:
            ch = self._split("mono_matrix", "stereo_matrix", n)
        elif k == K.FX_SEND:
            ch = self._split("mono_fx_send", "stereo_fx_send", n)
        elif k in (K.FX_RETURN, K.MAIN, K.DCA, K.MUTE_GROUP):
            off, first, _ = L[{K.FX_RETURN: "fx_return", K.MAIN: "main", K.DCA: "dca", K.MUTE_GROUP: "mute_group"}[k]]
            ch = (off, first + n - 1)
        else:
            raise MixerError(f"nicht unterstützt: {k.value}")
        midi = self.n + ch[0]
        if midi > 15:
            raise MixerError("MIDI-Basiskanal zu hoch (Basiskanal + 4 muss ≤ 16 sein)")
        return midi, ch[1]

    def mute_bytes(self, target: Target, muted: bool) -> list[int]:
        midi, ch = self.address(target)
        st = 0x90 | midi
        return [st, ch, 0x7F if muted else 0x3F, st, ch, 0x00]

    def level_bytes(self, target: Target, db: float | None) -> list[int]:
        lv = dlive_level_value(db)
        if target.kind == K.SEND:
            midi, ch = self.address(target)
            dest_midi, dest_ch = self.address(Target(K.BUS, target.dest or 1))
            return AH_SYSEX_HEADER + [midi, 0x0D, ch, dest_midi, dest_ch, lv, 0xF7]
        midi, ch = self.address(target)
        b = 0xB0 | midi
        return [b, 0x63, ch, b, 0x62, 0x17, b, 0x06, lv]

    async def set_mute(self, target: Target, muted: bool) -> None:
        self.validate(target, "mute")
        await self.send(self.mute_bytes(target, muted))

    async def set_level(self, target: Target, db: float | None) -> None:
        self.validate(target, "level")
        await self.send(self.level_bytes(target, db))

    # ------------------------------------------------------------- queries (dLive documents get messages)

    async def _get(self, request: list[int], key: tuple[int, int, str], timeout: float = 0.6) -> int | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[int] = loop.create_future()
        self._waiters.setdefault(key, []).append(fut)
        await self.send(request)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            lst = self._waiters.get(key, [])
            if fut in lst:
                lst.remove(fut)

    async def get_mute(self, target: Target) -> bool | None:
        self.validate(target, "mute")
        midi, ch = self.address(target)
        v = await self._get(AH_SYSEX_HEADER + [midi, 0x05, 0x09, ch, 0xF7], (midi, ch, "mute"))
        return None if v is None else v >= 0x40

    async def get_level(self, target: Target) -> float | None:
        self.validate(target, "level")
        if target.kind == K.SEND:
            return None
        midi, ch = self.address(target)
        v = await self._get(AH_SYSEX_HEADER + [midi, 0x05, 0x0B, 0x17, ch, 0xF7], (midi, ch, "level"))
        return None if v is None else dlive_value_to_db(v)

    def _resolve(self, key: tuple[int, int, str], value: int) -> None:
        for fut in self._waiters.pop(key, []):
            if not fut.done():
                fut.set_result(value)

    def _on_data(self, data: bytes) -> None:
        for byte in data:
            if byte >= 0xF8:
                continue
            if byte & 0x80:
                self._rx_status = byte
                self._rx_data = []
                continue
            if self._rx_status == 0xF0:  # ignore SysEx payload
                continue
            self._rx_data.append(byte)
            if len(self._rx_data) < 2:
                continue
            a, b = self._rx_data
            self._rx_data = []
            midi = self._rx_status & 0x0F
            kind = self._rx_status & 0xF0
            if kind == 0x90 and b != 0:
                self._resolve((midi, a, "mute"), b)
            elif kind == 0xB0:
                cc = self._rx_cc.setdefault(midi, {})
                cc[a] = b
                if a == 0x06 and cc.get(0x62) == 0x17 and 0x63 in cc:
                    self._resolve((midi, cc[0x63], "level"), b)


@register_driver
class AllenHeathDliveDriver(_AhDliveBase):
    type_id = "ah_dlive"
    display_name = "Allen & Heath dLive"
    platform = "dlive"
    notes = ("MIDI über TCP (Port 51325 unverschlüsselt) an Surface oder MixRack, Firmware ≥ 1.9. Belegt 5 MIDI-Kanäle ab dem "
             "Basiskanal. Aux/Gruppen/Matrix: erst Mono-, dann Stereo-Busse nummeriert. Mutes und Fader abfragbar.")


@register_driver
class AllenHeathAvantisDriver(_AhDliveBase):
    type_id = "ah_avantis"
    display_name = "Allen & Heath Avantis"
    platform = "avantis"
    can_query = False  # the Avantis protocol documents no get messages
    notes = ("MIDI über TCP (Port 51325), Firmware ≥ 2.0. Belegt 5 MIDI-Kanäle ab dem Basiskanal (Utility › Control › MIDI). "
             "Aux/Gruppen/Matrix: erst Mono-, dann Stereo-Busse nummeriert.")

    async def get_mute(self, target: Target) -> bool | None:  # noqa: D102
        return None

    async def get_level(self, target: Target) -> float | None:  # noqa: D102
        return None
