"""Soundcraft Ui series (Ui12, Ui16, Ui24R).

The Ui consoles are not controlled via OSC but via the same WebSocket
connection their browser app uses (``ws://<ip>:80``). Commands are text
messages like ``3:::SETD^i.0.mute^1``; the console pushes its full state
on connect, which is cached here so current values can be reported.
"""

from __future__ import annotations

import asyncio
import json
import logging

from ...domain.levels import MIN_DB, interpolate_table, is_off
from ...domain.model import MixerConfig, Target, TargetKind
from .base import KindSpec, MixerDriver, MixerError, OptionSpec, register_driver
from .transports import WebSocketClient

log = logging.getLogger("aep.mixer.ui")
K = TargetKind

# Approximation of the Ui fader law (dB -> 0..1). 0 dB = 0.7647 is exact; the
# other points can be calibrated with the option "fader_table".
UI_FADER_TABLE = [
    (-90.0, 0.0), (-70.0, 0.03), (-60.0, 0.06), (-50.0, 0.11), (-40.0, 0.18), (-30.0, 0.29),
    (-20.0, 0.43), (-10.0, 0.6), (-5.0, 0.68), (0.0, 0.7647), (5.0, 0.88), (10.0, 1.0),
]


@register_driver
class SoundcraftUiDriver(MixerDriver):
    type_id = "soundcraft_ui"
    display_name = "Soundcraft Ui (Ui12/Ui16/Ui24R)"
    family = "Soundcraft"
    default_port = 80
    transport = "WebSocket"
    can_query = True
    kinds = [
        KindSpec(K.CHANNEL, "Kanal", 24),
        KindSpec(K.STEREO_IN, "Line In", 2, number_labels={1: "Line In L", 2: "Line In R"}),
        KindSpec(K.FX_RETURN, "FX Return", 4),
        KindSpec(K.BUS, "Aux", 10),
        KindSpec(K.MAIN, "Master", 1, mute=False),
        KindSpec(K.MUTE_GROUP, "Mute-Gruppe", 6, level=False),
        KindSpec(K.SEND, "Kanal → Aux", 24, dest_label="Aux", dest_max=10),
        KindSpec(K.RAW, "Roh-Pfad (z. B. p.0)"),
    ]
    option_specs = [
        OptionSpec("fader_table", "Faderkurve (JSON, optional)", "json", None,
                   help='Kalibrierpunkte [[dB, Wert 0..1], …] falls Pegel nicht exakt passen.'),
    ]
    notes = ("Steuerung über die WebSocket-Schnittstelle der Ui-Weboberfläche (kein OSC). "
             "Ui12/Ui16 haben weniger Kanäle und Aux-Wege als der Ui24R. Roh-Pfade: z. B. p.0 (Player), v.0 (VCA).")

    def __init__(self, config: MixerConfig):
        super().__init__(config)
        self._ws = WebSocketClient(f"ws://{self.host}:{self.port}/", on_text=self._on_text)
        self._state: dict[str, str] = {}
        self._alive_task: asyncio.Task | None = None
        self._ready = asyncio.Event()

    # ------------------------------------------------------------- connection

    async def connect(self) -> None:
        if self._ws.connected:
            return
        self._ready.clear()
        await self._ws.connect()
        if self._alive_task is None or self._alive_task.done():
            self._alive_task = asyncio.create_task(self._keepalive())
        try:  # give the console a moment to push its state
            await asyncio.wait_for(self._ready.wait(), 1.5)
        except asyncio.TimeoutError:
            pass

    async def _keepalive(self) -> None:
        try:
            while self._ws.connected:
                await asyncio.sleep(1.0)
                await self._ws.send_text("3:::ALIVE")
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass

    async def close(self) -> None:
        if self._alive_task:
            self._alive_task.cancel()
        await self._ws.close()

    def _on_text(self, text: str) -> None:
        for line in text.splitlines():
            if line.startswith("2::"):
                asyncio.ensure_future(self._ws.send_text(line))
                continue
            payload = line.split(":::", 1)[1] if ":::" in line else line
            if payload.startswith(("SETD^", "SETS^")):
                parts = payload.split("^", 2)
                if len(parts) == 3:
                    self._state[parts[1]] = parts[2]
                    self._ready.set()

    async def test(self) -> str:
        await self.connect()
        model = self._state.get("model") or self._state.get("settings.model") or "Ui"
        return f"Verbunden ({model}, {len(self._state)} Parameter empfangen)"

    # ------------------------------------------------------------- mapping

    def path(self, target: Target) -> str:
        n = (target.number or 1) - 1
        k = target.kind
        if k == K.CHANNEL:
            return f"i.{n}"
        if k == K.STEREO_IN:
            return f"l.{n}"
        if k == K.FX_RETURN:
            return f"f.{n}"
        if k == K.BUS:
            return f"a.{n}"
        if k == K.MAIN:
            return "m"
        if k == K.SEND:
            return f"i.{n}.aux.{(target.dest or 1) - 1}"
        if k == K.RAW:
            return (target.raw or "").strip()
        raise MixerError(f"nicht unterstützt: {k.value}")

    def _table(self) -> list[tuple[float, float]]:
        custom = self.option("fader_table")
        if custom:
            try:
                points = json.loads(custom) if isinstance(custom, str) else custom
                return sorted((float(a), float(b)) for a, b in points)
            except (ValueError, TypeError):
                log.warning("Ungültige Faderkurve für %s – Standard wird verwendet", self.config.name)
        return UI_FADER_TABLE

    def db_to_value(self, db: float | None) -> float:
        if is_off(db):
            return 0.0
        return round(interpolate_table(float(db), self._table()), 4)

    def value_to_db(self, value: float) -> float:
        inverse = sorted((v, d) for d, v in self._table())
        return MIN_DB if value <= 0 else interpolate_table(value, inverse)

    async def _set(self, path: str, value: object) -> None:
        await self.connect()
        await self._ws.send_text(f"3:::SETD^{path}^{value}")
        self._state[path] = str(value)

    # ------------------------------------------------------------- control

    async def set_mute(self, target: Target, muted: bool) -> None:
        self.validate(target, "mute")
        if target.kind == K.MUTE_GROUP:
            await self.connect()
            mask = int(float(self._state.get("mgmask", "0") or 0))
            bit = 1 << ((target.number or 1) - 1)
            mask = (mask | bit) if muted else (mask & ~bit)
            await self._set("mgmask", mask)
            return
        await self._set(self.path(target) + ".mute", 1 if muted else 0)

    async def set_level(self, target: Target, db: float | None) -> None:
        self.validate(target, "level")
        suffix = ".value" if target.kind == K.SEND else ".mix"
        path = self.path(target)
        if target.kind == K.RAW and path.split(".")[-1] in ("mix", "value", "gain"):
            suffix = ""
        await self._set(path + suffix, self.db_to_value(db))

    async def get_mute(self, target: Target) -> bool | None:
        self.validate(target, "mute")
        await self.connect()
        if target.kind == K.MUTE_GROUP:
            raw = self._state.get("mgmask")
            return None if raw is None else bool(int(float(raw)) & (1 << ((target.number or 1) - 1)))
        raw = self._state.get(self.path(target) + ".mute")
        return None if raw is None else raw.strip() not in ("0", "0.0", "")

    async def get_level(self, target: Target) -> float | None:
        self.validate(target, "level")
        await self.connect()
        suffix = ".value" if target.kind == K.SEND else ".mix"
        raw = self._state.get(self.path(target) + suffix)
        return None if raw is None else self.value_to_db(float(raw))
