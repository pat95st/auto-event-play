# Mischpult-Steuerung

## Konzept

Alle Pulte werden über eine gemeinsame Schnittstelle angesprochen
(`autoeventplay/infrastructure/mixers/base.py`):

- **Ziel** (`Target`): Art (`channel`, `stereo_in`, `fx_return`, `bus`, `group`, `fx_send`, `matrix`,
  `main`, `dca`, `mute_group`, `send`, `raw`), Nummer und bei Sends das Ziel (Bus).
- **Aktionen**: `set_mute(target, muted)`, `set_level(target, dB)` – Pegel immer in dB, `None` = −∞.
- **Optional**: `get_mute` / `get_level` – damit stellt „danach zurücksetzen“ exakt den alten Zustand her.
- Jeder Treiber beschreibt per `KindSpec`, welche Zielarten mit welcher Nummerierung er kann. Die
  Weboberfläche baut ihre Auswahllisten daraus – für ein neues Pult ist kein Frontend-Code nötig.

## Neues Fabrikat ohne Programmierung: „Generisches OSC-Pult“

Unter *Pulte* den Typ „Generisches OSC-Pult“ wählen und die Adress-Map als JSON eintragen:

```json
{
  "channel": {"max": 32, "label": "Kanal", "mute": "/ch/{n:02d}/mix/on", "mute_invert": true,
              "level": "/ch/{n:02d}/mix/fader", "level_law": "x32"},
  "bus":     {"max": 16, "label": "Bus", "mute": "/bus/{n}/mute", "level": "/bus/{n}/fader",
              "level_law": "db", "min_db": -144},
  "send":    {"max": 32, "dest_max": 16, "dest_label": "Bus",
              "level": "/ch/{n}/send/{d}/level", "level_law": "db"}
}
```

- `{n}` = Nummer, `{d}` = Ziel (bei Sends); Python-Formatangaben wie `{n:02d}` sind erlaubt.
- `mute_invert: true`, wenn die Adresse „an“ statt „gemutet“ bedeutet (1 = an).
- `level_law`: `x32` (0…1 nach X32-Faderkurve), `db` (Float in dB, `min_db` für −∞), `db_int`,
  `linear` (0…1 lineare Verstärkung).

## Neuer Treiber (Python)

```python
from autoeventplay.domain.model import Target, TargetKind as K
from autoeventplay.infrastructure.mixers.base import KindSpec, register_driver
from autoeventplay.infrastructure.mixers.osc_base import OscMixerDriver, OscParam, mute_direct, decode_mute_direct, level_db, decode_level_db


@register_driver
class MyConsoleDriver(OscMixerDriver):
    type_id = "acme_x100"
    display_name = "ACME X100"
    family = "ACME"
    default_port = 9000
    kinds = [KindSpec(K.CHANNEL, "Kanal", 48), KindSpec(K.MAIN, "Main", 1)]

    def param(self, target: Target, which: str) -> OscParam:
        base = "/main" if target.kind == K.MAIN else f"/input/{target.number}"
        if which == "mute":
            return OscParam(base + "/mute", mute_direct, decode_mute_direct)
        return OscParam(base + "/level", level_db(-100), decode_level_db)
```

Die Datei in `autoeventplay/infrastructure/mixers/` ablegen und in `mixers/__init__.py` importieren.
Für Nicht-OSC-Protokolle direkt von `MixerDriver` ableiten (Beispiele: `allen_heath.py` für MIDI/TCP,
`soundcraft.py` für WebSocket).

## Protokoll-Details der eingebauten Treiber

| Treiber | Mute | Pegel | Abfrage |
|---|---|---|---|
| X32/M32 | `/ch/NN/mix/on` (1 = an) | `/ch/NN/mix/fader` 0…1 (X32-Kurve) | Adresse ohne Argument |
| X-AIR/M-AIR | `/ch/NN/mix/on`, `/bus/N/mix/on`, `/lr/mix/on` | `…/mix/fader` | ja |
| WING | `/ch/N/mute` (1 = gemutet) | `/ch/N/fdr` in dB (−144 = −∞) | ja |
| Soundcraft Ui | `SETD^i.N.mute^1` | `SETD^i.N.mix^0..1` | Zustandscache der Verbindung |
| A&H Qu (alt) | Note On `9N CH 7F/3F` | NRPN `BN 63 CH 62 17 06 VA 26 07` | nein |
| A&H SQ, Qu-5/6/7 | NRPN, Wert `00/01` | NRPN 14 Bit (Audio-/Linear-Taper-Tabelle) | NRPN `60 7F` |
