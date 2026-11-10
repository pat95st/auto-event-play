"""Yamaha digital consoles via the Remote Control Protocol (RCP, TCP port 49280).

Covers TF (TF1/TF3/TF5/TF-RACK), QL (QL1/QL5), CL (CL1/CL3/CL5) and the
DM series (DM3, DM5, DM7). All use the same text protocol::

    set MIXER:Current/InCh/Fader/Level 0 0 -1000\\n    -> channel 1 to -10.00 dB
    set MIXER:Current/InCh/Fader/On 0 0 1\\n          -> channel 1 ON (= not muted)
    get MIXER:Current/InCh/Fader/Level 0 0\\n         -> OK get MIXER:... 0 0 -1000

Indices are 0-based, levels are dB × 100 (-32768 = -∞, max +10 dB = 1000).
Yamaha's "ON" key is the inverse of a mute.
"""

from __future__ import annotations

import asyncio
import logging

from ...domain.levels import is_off
from ...domain.model import MixerConfig, Target, TargetKind
from .base import KindSpec, MixerDriver, MixerError, OptionSpec, register_driver
from .transports import TcpClient

log = logging.getLogger("aep.mixer.yamaha")
K = TargetKind

NEG_INF = -32768

# channels: mono inputs, stereo inputs, fx returns, mix, matrix, stereo masters, mono master, dca, mute groups
MODELS: dict[str, dict] = {
    "tf1": {"label": "TF1", "in": 32, "stin": 2, "fxrtn": 2, "mix": 20, "mtrx": 0, "st": 1, "mono": 0, "dca": 8, "mute": 6},
    "tf3": {"label": "TF3", "in": 40, "stin": 2, "fxrtn": 2, "mix": 20, "mtrx": 0, "st": 1, "mono": 0, "dca": 8, "mute": 6},
    "tf5": {"label": "TF5", "in": 40, "stin": 2, "fxrtn": 2, "mix": 20, "mtrx": 0, "st": 1, "mono": 0, "dca": 8, "mute": 6},
    "tfrack": {"label": "TF-RACK", "in": 32, "stin": 2, "fxrtn": 2, "mix": 20, "mtrx": 0, "st": 1, "mono": 0, "dca": 8, "mute": 6},
    "ql1": {"label": "QL1", "in": 32, "stin": 8, "fxrtn": 0, "mix": 16, "mtrx": 8, "st": 1, "mono": 1, "dca": 16, "mute": 8},
    "ql5": {"label": "QL5", "in": 64, "stin": 8, "fxrtn": 0, "mix": 16, "mtrx": 8, "st": 1, "mono": 1, "dca": 16, "mute": 8},
    "cl1": {"label": "CL1", "in": 48, "stin": 8, "fxrtn": 0, "mix": 24, "mtrx": 8, "st": 1, "mono": 1, "dca": 16, "mute": 8},
    "cl3": {"label": "CL3", "in": 64, "stin": 8, "fxrtn": 0, "mix": 24, "mtrx": 8, "st": 1, "mono": 1, "dca": 16, "mute": 8},
    "cl5": {"label": "CL5", "in": 72, "stin": 8, "fxrtn": 0, "mix": 24, "mtrx": 8, "st": 1, "mono": 1, "dca": 16, "mute": 8},
    "dm3": {"label": "DM3 / DM3 Standard", "in": 16, "stin": 1, "fxrtn": 0, "mix": 6, "mtrx": 2, "st": 1, "mono": 0, "dca": 8, "mute": 6},
    "dm5c": {"label": "DM5 Compact", "in": 48, "stin": 0, "fxrtn": 0, "mix": 24, "mtrx": 12, "st": 2, "mono": 0, "dca": 12, "mute": 12},
    "dm5": {"label": "DM5", "in": 60, "stin": 0, "fxrtn": 0, "mix": 24, "mtrx": 12, "st": 2, "mono": 0, "dca": 12, "mute": 12},
    "dm7c": {"label": "DM7 Compact", "in": 72, "stin": 0, "fxrtn": 0, "mix": 48, "mtrx": 12, "st": 2, "mono": 0, "dca": 24, "mute": 12},
    "dm7": {"label": "DM7", "in": 120, "stin": 0, "fxrtn": 0, "mix": 48, "mtrx": 12, "st": 2, "mono": 0, "dca": 24, "mute": 12},
}

SERIES_MODELS = {
    "yamaha_tf": ["tf1", "tf3", "tf5", "tfrack"],
    "yamaha_ql": ["ql1", "ql5"],
    "yamaha_cl": ["cl1", "cl3", "cl5"],
    "yamaha_dm3": ["dm3"],
    "yamaha_dm5": ["dm5", "dm5c"],
    "yamaha_dm7": ["dm7", "dm7c"],
}


def level_value(db: float | None) -> int:
    if is_off(db):
        return NEG_INF
    return int(round(max(-138.0, min(10.0, float(db))) * 100))


def value_to_db(value: int) -> float:
    from ...domain.levels import MIN_DB

    return MIN_DB if value <= -13800 else value / 100.0


class _YamahaRcpBase(MixerDriver):
    family = "Yamaha"
    default_port = 49280
    transport = "RCP/TCP"
    can_query = True
    series = ""

    @classmethod
    def model_choices(cls) -> list[tuple[str, str]]:
        return [(m, MODELS[m]["label"]) for m in SERIES_MODELS[cls.series]]

    @classmethod
    def model_of(cls, config: MixerConfig | None) -> dict:
        key = (config.options.get("model") if config else None) or SERIES_MODELS[cls.series][0]
        if key not in SERIES_MODELS[cls.series]:
            key = SERIES_MODELS[cls.series][0]
        return MODELS[key]

    @classmethod
    def kinds_for(cls, config: MixerConfig | None) -> list[KindSpec]:
        m = cls.model_of(config)
        kinds = [KindSpec(K.CHANNEL, "Kanal", m["in"])]
        if m["stin"]:
            kinds.append(KindSpec(K.STEREO_IN, "ST IN", m["stin"]))
        if m["fxrtn"]:
            kinds.append(KindSpec(K.FX_RETURN, "FX Return", m["fxrtn"]))
        kinds.append(KindSpec(K.BUS, "Mix", m["mix"]))
        if m["mtrx"]:
            kinds.append(KindSpec(K.MATRIX, "Matrix", m["mtrx"]))
        main_labels = {1: "Stereo" if m["st"] == 1 else "Stereo A"}
        if m["st"] > 1:
            main_labels[2] = "Stereo B"
        if m["mono"]:
            main_labels[m["st"] + 1] = "Mono"
        kinds.append(KindSpec(K.MAIN, "Master", m["st"] + m["mono"], number_labels=main_labels))
        kinds.append(KindSpec(K.DCA, "DCA", m["dca"]))
        kinds.append(KindSpec(K.MUTE_GROUP, "Mute-Gruppe", m["mute"], level=False))
        kinds.append(KindSpec(K.SEND, "Kanal → Mix", m["in"], dest_label="Mix", dest_max=m["mix"]))
        kinds.append(KindSpec(K.RAW, "Roh-Adresse (z. B. MIXER:Current/InCh/Fader/On 4 0)"))
        return kinds

    def __init__(self, config: MixerConfig):
        super().__init__(config)
        self._tcp = TcpClient(self.host, self.port, on_data=self._on_data)
        self._buffer = b""
        self._waiters: dict[str, list[asyncio.Future[str]]] = {}

    # ------------------------------------------------------------- mapping

    def address(self, target: Target, which: str) -> tuple[str, int, int, bool]:
        """(parameter address, x, y, value_is_on_switch)."""
        n = (target.number or 1) - 1
        m = self.model_of(self.config)
        k = target.kind
        if k == K.RAW:
            parts = (target.raw or "").split()
            if not parts or not parts[0].startswith("MIXER:"):
                raise MixerError("Roh-Adresse z. B. „MIXER:Current/InCh/Fader/On 4 0“")
            x = int(parts[1]) if len(parts) > 1 else 0
            y = int(parts[2]) if len(parts) > 2 else 0
            return parts[0], x, y, parts[0].endswith("/On")
        if k == K.MUTE_GROUP:
            return "MIXER:Current/MuteMaster/On", n, 0, False
        if k == K.SEND:
            leaf = "On" if which == "mute" else "Level"
            return f"MIXER:Current/InCh/ToMix/{leaf}", n, (target.dest or 1) - 1, which == "mute"
        if k == K.MAIN:
            if n < m["st"]:
                base, x = "MIXER:Current/St", n
            else:
                base, x = "MIXER:Current/Mono", 0
        else:
            base = {K.CHANNEL: "MIXER:Current/InCh", K.STEREO_IN: "MIXER:Current/StInCh", K.FX_RETURN: "MIXER:Current/FxRtnCh",
                    K.BUS: "MIXER:Current/Mix", K.MATRIX: "MIXER:Current/Mtrx", K.DCA: "MIXER:Current/DCA"}.get(k)
            if base is None:
                raise MixerError(f"nicht unterstützt: {k.value}")
            x = n
        leaf = "Fader/On" if which == "mute" else "Fader/Level"
        return f"{base}/{leaf}", x, 0, which == "mute"

    def set_line(self, target: Target, which: str, value: bool | float | None) -> str:
        addr, x, y, on_switch = self.address(target, which)
        if which == "mute":
            muted = bool(value)
            v = (0 if muted else 1) if on_switch else (1 if muted else 0)
        else:
            v = level_value(value)  # type: ignore[arg-type]
        return f"set {addr} {x} {y} {v}\n"

    # ------------------------------------------------------------- transport

    def _on_data(self, data: bytes) -> None:
        self._buffer += data
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            text = line.decode("utf-8", "replace").strip()
            parts = text.split()
            if len(parts) >= 6 and parts[0] in ("OK", "NOTIFY") and parts[1] in ("get", "set"):
                key = f"{parts[2]} {parts[3]} {parts[4]}"
                for fut in self._waiters.pop(key, []):
                    if not fut.done():
                        fut.set_result(parts[5])
            elif parts and parts[0] == "ERROR":
                log.warning("Yamaha %s: %s", self.config.name, text)

    async def connect(self) -> None:
        await self._tcp.connect()

    async def close(self) -> None:
        await self._tcp.close()

    async def _get(self, addr: str, x: int, y: int, timeout: float = 0.8) -> str | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        key = f"{addr} {x} {y}"
        self._waiters.setdefault(key, []).append(fut)
        await self._tcp.send(f"get {addr} {x} {y}\n".encode())
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            lst = self._waiters.get(key, [])
            if fut in lst:
                lst.remove(fut)

    async def test(self) -> str:
        await self.connect()
        value = await self._get("MIXER:Current/InCh/Fader/Level", 0, 0, 1.5)
        if value is None:
            raise MixerError("Keine Antwort auf RCP-Abfrage (Port 49280, Firmware mit RCP-Unterstützung?)")
        return f"Verbunden – Kanal 1 steht auf {value_to_db(int(value)):.1f} dB"

    async def set_mute(self, target: Target, muted: bool) -> None:
        self.validate(target, "mute")
        await self._tcp.send(self.set_line(target, "mute", muted).encode())

    async def set_level(self, target: Target, db: float | None) -> None:
        self.validate(target, "level")
        await self._tcp.send(self.set_line(target, "level", db).encode())

    async def get_mute(self, target: Target) -> bool | None:
        self.validate(target, "mute")
        addr, x, y, on_switch = self.address(target, "mute")
        raw = await self._get(addr, x, y)
        if raw is None:
            return None
        v = int(float(raw.strip('"')))
        return (v == 0) if on_switch else (v != 0)

    async def get_level(self, target: Target) -> float | None:
        self.validate(target, "level")
        addr, x, y, _ = self.address(target, "level")
        raw = await self._get(addr, x, y)
        return None if raw is None else value_to_db(int(float(raw.strip('"'))))


def _make(series: str, type_id: str, name: str, notes: str) -> type[_YamahaRcpBase]:
    cls = type(f"Yamaha{series.split('_')[1].upper()}Driver", (_YamahaRcpBase,), {
        "type_id": type_id,
        "display_name": name,
        "series": series,
        "notes": notes,
    })
    cls.option_specs = [OptionSpec("model", "Modell", "select", SERIES_MODELS[series][0], choices=cls.model_choices())]
    return register_driver(cls)


YamahaTFDriver = _make("yamaha_tf", "yamaha_tf", "Yamaha TF (TF1/TF3/TF5/TF-RACK)",
                       "RCP über TCP 49280. Die TF-Serie hat keine Matrix-Busse; Pegel als dB × 100, „ON“ = nicht gemutet.")
YamahaQLDriver = _make("yamaha_ql", "yamaha_ql", "Yamaha QL (QL1/QL5)",
                       "RCP über TCP 49280 (Setup › Network). Stereo- und Mono-Master, 16 Mix, 8 Matrix, 16 DCA.")
YamahaCLDriver = _make("yamaha_cl", "yamaha_cl", "Yamaha CL (CL1/CL3/CL5)",
                       "RCP über TCP 49280 (Setup › Network). Stereo- und Mono-Master, 24 Mix, 8 Matrix, 16 DCA.")
YamahaDM3Driver = _make("yamaha_dm3", "yamaha_dm3", "Yamaha DM3",
                        "RCP über TCP 49280 (Firmware mit RCP-Unterstützung). 16 Mono + 1 Stereo-Eingang, 6 Mix, 2 Matrix.")
YamahaDM5Driver = _make("yamaha_dm5", "yamaha_dm5", "Yamaha DM5 / DM5 Compact",
                        "DM5 basiert auf der DM7-Plattform: RCP über TCP 49280. Stereo A/B als Master 1/2.")
YamahaDM7Driver = _make("yamaha_dm7", "yamaha_dm7", "Yamaha DM7 / DM7 Compact",
                        "RCP über TCP 49280. Stereo A/B als Master 1/2, 48 Mix, 12 Matrix, 24 DCA.")
