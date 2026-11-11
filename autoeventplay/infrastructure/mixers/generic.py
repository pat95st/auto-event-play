"""Generic OSC driver – adapt other console brands without writing code.

The option ``map`` is a JSON object with one entry per target kind::

    {
      "channel": {"max": 32, "label": "Channel",
                  "mute": "/ch/{n:02d}/mix/on", "mute_invert": true,
                  "level": "/ch/{n:02d}/mix/fader", "level_law": "x32"},
      "main":    {"mute": "/main/st/mix/on", "mute_invert": true,
                  "level": "/main/st/mix/fader", "level_law": "x32"},
      "send":    {"max": 32, "dest_max": 16, "level": "/ch/{n:02d}/mix/{d:02d}/level", "level_law": "x32"}
    }

Placeholders: ``{n}`` number, ``{d}`` destination (sends). ``level_law``:
``x32`` (0..1 X32 fader curve), ``db`` (float dB, optional ``min_db``),
``linear`` (0..1 linear gain) or ``db_int`` (integer dB).
"""

from __future__ import annotations

import json
from typing import Any

from ...domain.levels import db_to_fader, db_to_gain, fader_to_db, gain_to_db, is_off
from ...domain.model import MixerConfig, Target, TargetKind
from .base import KindSpec, MixerError, OptionSpec, register_driver
from .osc_base import OscMixerDriver, OscParam, _last_number
from ...i18n import Msg

EXAMPLE_MAP = {
    "channel": {"max": 32, "label": "Channel", "mute": "/ch/{n:02d}/mix/on", "mute_invert": True,
                "level": "/ch/{n:02d}/mix/fader", "level_law": "x32"},
    "main": {"label": "Main", "mute": "/main/st/mix/on", "mute_invert": True, "level": "/main/st/mix/fader", "level_law": "x32"},
}


def _load_map(config: MixerConfig | None) -> dict[str, Any]:
    raw = (config.options.get("map") if config else None) or EXAMPLE_MAP
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise MixerError(Msg("The address map is not valid JSON: {error}", error=str(exc))) from exc
    if not isinstance(raw, dict):
        raise MixerError("The address map must be a JSON object")
    return raw


@register_driver
class GenericOscDriver(OscMixerDriver):
    type_id = "generic_osc"
    display_name = "Generic OSC console"
    family = "Andere"
    default_port = 10023
    option_specs = [
        OptionSpec("map", "Adress-Map (JSON)", "json", json.dumps(EXAMPLE_MAP, indent=2),
                   help="OSC addresses per target type with {n}/{d}, mute_invert and level_law (x32, db, linear, db_int)."),
        OptionSpec("test_address", "Test address", "str", "/info", help="Queried during the connection test."),
    ]
    notes = "For other brands with OSC control: enter the addresses in the map, no programming needed."

    @classmethod
    def kinds_for(cls, config: MixerConfig | None) -> list[KindSpec]:
        try:
            mapping = _load_map(config)
        except MixerError:
            mapping = EXAMPLE_MAP
        kinds = []
        for key, spec in mapping.items():
            try:
                kind = TargetKind(key)
            except ValueError:
                continue
            kinds.append(KindSpec(kind, spec.get("label") or key, int(spec.get("max", 1)), mute=bool(spec.get("mute")),
                                  level=bool(spec.get("level")), dest_label=spec.get("dest_label"),
                                  dest_max=spec.get("dest_max")))
        kinds.append(KindSpec(TargetKind.RAW, "Raw OSC address"))
        return kinds

    def param(self, target: Target, which: str) -> OscParam:
        spec = _load_map(self.config).get(target.kind.value) or {}
        template = spec.get(which)
        if not template:
            raise MixerError(Msg("No {which} address for {kind} in the map", which=which, kind=target.kind.value))
        address = template.format(n=target.number or 1, d=target.dest or 1)
        if which == "mute":
            invert = bool(spec.get("mute_invert"))
            return OscParam(address, lambda m: int((not m) if invert else m),
                            lambda a: None if _last_number(a) is None else ((_last_number(a) < 0.5) if invert else (_last_number(a) >= 0.5)))
        law = spec.get("level_law", "x32")
        min_db = float(spec.get("min_db", -90))
        if law == "db":
            return OscParam(address, lambda db: float(min_db if is_off(db) else max(min_db, db)), lambda a: _last_number(a))
        if law == "db_int":
            return OscParam(address, lambda db: int(min_db if is_off(db) else max(min_db, round(db))), lambda a: _last_number(a))
        if law == "linear":
            return OscParam(address, lambda db: float(db_to_gain(db)), lambda a: None if _last_number(a) is None else gain_to_db(_last_number(a)))
        return OscParam(address, lambda db: float(round(db_to_fader(db), 4)), lambda a: None if _last_number(a) is None else fader_to_db(_last_number(a)))

    async def info_query(self):  # noqa: ANN201
        return await self._client.query(self.option("test_address", "/info") or "/info", timeout=1.0)
