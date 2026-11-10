"""Universal mixer driver interface.

Every console family is one :class:`MixerDriver` subclass that

* declares which target kinds it supports (channel, bus, matrix, main, DCA,
  mute group, send …) with their numbering (:class:`KindSpec`) – the web UI
  builds its target pickers from this metadata,
* translates ``set_mute`` / ``set_level`` (always in dB, ``None`` = -inf)
  into the console protocol,
* optionally reports current values (``get_mute`` / ``get_level``) so cues
  can restore the previous state exactly.

Adding another manufacturer means writing one subclass and decorating it
with :func:`register_driver`. For simple OSC consoles no code is needed at
all: the "Generic OSC" driver takes address templates from its options.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from ...domain.model import MixerConfig, Target, TargetKind

log = logging.getLogger("aep.mixer")


class MixerError(RuntimeError):
    pass


@dataclass
class KindSpec:
    kind: TargetKind
    label: str
    max: int = 1
    mute: bool = True
    level: bool = True
    number_labels: dict[int, str] | None = None
    dest_label: str | None = None      # SEND: label of the destination (e.g. "Bus")
    dest_max: int | None = None
    dest_labels: dict[int, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass
class OptionSpec:
    key: str
    label: str
    type: str = "str"  # str | int | select | json | bool
    default: Any = None
    choices: list[tuple[str, str]] | None = None
    help: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DriverInfo:
    type_id: str
    display_name: str
    family: str
    default_port: int | None
    transport: str
    can_query: bool
    kinds: list[KindSpec] = field(default_factory=list)
    options: list[OptionSpec] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type_id": self.type_id,
            "display_name": self.display_name,
            "family": self.family,
            "default_port": self.default_port,
            "transport": self.transport,
            "can_query": self.can_query,
            "kinds": [k.to_dict() for k in self.kinds],
            "options": [o.to_dict() for o in self.options],
            "notes": self.notes,
        }


class MixerDriver(abc.ABC):
    type_id: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    family: ClassVar[str] = ""
    default_port: ClassVar[int | None] = None
    transport: ClassVar[str] = ""
    can_query: ClassVar[bool] = False
    kinds: ClassVar[list[KindSpec]] = []
    option_specs: ClassVar[list[OptionSpec]] = []
    notes: ClassVar[str] = ""

    def __init__(self, config: MixerConfig):
        self.config = config

    # ---------------------------------------------------------------- metadata

    @classmethod
    def info(cls, config: MixerConfig | None = None) -> DriverInfo:
        return DriverInfo(cls.type_id, cls.display_name, cls.family, cls.default_port, cls.transport, cls.can_query,
                          cls.kinds_for(config), list(cls.option_specs), cls.notes)

    @classmethod
    def kinds_for(cls, config: MixerConfig | None) -> list[KindSpec]:
        """Kinds may depend on options (e.g. console model); default: static list."""
        return list(cls.kinds)

    @property
    def host(self) -> str:
        return self.config.host

    @property
    def port(self) -> int:
        return int(self.config.port or self.default_port or 0)

    def option(self, key: str, default: Any = None) -> Any:
        value = self.config.options.get(key)
        if value in (None, ""):
            for spec in self.option_specs:
                if spec.key == key:
                    return spec.default if default is None else default
            return default
        return value

    def spec(self, kind: TargetKind) -> KindSpec:
        for k in self.kinds_for(self.config):
            if k.kind == kind:
                return k
        raise MixerError(f"{self.display_name} unterstützt '{kind.value}' nicht")

    def validate(self, target: Target, param: str) -> KindSpec:
        spec = self.spec(target.kind)
        if param == "mute" and not spec.mute:
            raise MixerError(f"{spec.label}: Mute wird von {self.display_name} nicht unterstützt")
        if param == "level" and not spec.level:
            raise MixerError(f"{spec.label}: Pegel wird von {self.display_name} nicht unterstützt")
        if target.kind == TargetKind.RAW:
            if not target.raw:
                raise MixerError("Roh-Adresse fehlt")
            return spec
        if spec.max > 1 or target.kind != TargetKind.MAIN:
            n = target.number or 1
            if n < 1 or n > spec.max:
                raise MixerError(f"{spec.label} {n} gibt es nicht (1–{spec.max})")
        if target.kind == TargetKind.SEND:
            d = target.dest or 0
            if d < 1 or (spec.dest_max and d > spec.dest_max):
                raise MixerError(f"Ziel {d} gibt es nicht (1–{spec.dest_max})")
        return spec

    def describe(self, target: Target) -> str:
        try:
            spec = self.spec(target.kind)
        except MixerError:
            return target.key()
        if target.kind == TargetKind.RAW:
            return f"Roh: {target.raw}"
        n = target.number or 1
        name = (spec.number_labels or {}).get(n) or (f"{spec.label} {n}" if spec.max > 1 else spec.label)
        if target.kind == TargetKind.SEND:
            dest = (spec.dest_labels or {}).get(target.dest or 0) or f"{spec.dest_label or 'Bus'} {target.dest}"
            return f"{name} → {dest}"
        return name

    # ---------------------------------------------------------------- lifecycle

    async def connect(self) -> None:  # pragma: no cover - default no-op
        return None

    async def close(self) -> None:  # pragma: no cover
        return None

    async def test(self) -> str:
        await self.connect()
        return "Verbindung aufgebaut"

    # ---------------------------------------------------------------- control

    @abc.abstractmethod
    async def set_mute(self, target: Target, muted: bool) -> None: ...

    @abc.abstractmethod
    async def set_level(self, target: Target, db: float | None) -> None: ...

    async def get_mute(self, target: Target) -> bool | None:
        return None

    async def get_level(self, target: Target) -> float | None:
        return None


_REGISTRY: dict[str, type[MixerDriver]] = {}


def register_driver(cls: type[MixerDriver]) -> type[MixerDriver]:
    if not cls.type_id:
        raise ValueError("driver needs a type_id")
    _REGISTRY[cls.type_id] = cls
    return cls


def driver_class(type_id: str) -> type[MixerDriver]:
    try:
        return _REGISTRY[type_id]
    except KeyError as exc:
        raise MixerError(f"Unbekannter Mischpult-Typ '{type_id}'") from exc


def driver_types() -> list[DriverInfo]:
    return [cls.info() for cls in sorted(_REGISTRY.values(), key=lambda c: (c.family, c.display_name))]
