# Console control

## Concept

All consoles are addressed through a common interface (`autoeventplay/infrastructure/mixers/base.py`):

- **Target** (`Target`) has three parts:
  - a kind: `channel`, `stereo_in`, `fx_return`, `bus`, `group`, `fx_send`, `matrix`, `main`, `dca`,
    `mute_group`, `send` or `raw`
  - a number
  - for sends, a destination (bus)
- **Actions:** `set_mute(target, muted)` and `set_level(target, dB)`. Levels are always in dB; `None` means −∞.
- **Optional:** `get_mute` / `get_level`. With these, "restore afterwards" brings back exactly the old state.
- Each driver uses `KindSpec` to describe which target kinds it supports and how they are numbered. The
  web interface builds its selection lists from this, so a new console needs no front-end code.
- Labels and notes are English source texts. They are translated through the catalog in
  `autoeventplay/i18n/de.py`.

## A new brand without programming: "Generic OSC console"

Under *Consoles*, choose the type "Generic OSC console" and enter the address map as JSON:

```json
{
  "channel": {"max": 32, "label": "Channel", "mute": "/ch/{n:02d}/mix/on", "mute_invert": true,
              "level": "/ch/{n:02d}/mix/fader", "level_law": "x32"},
  "bus":     {"max": 16, "label": "Bus", "mute": "/bus/{n}/mute", "level": "/bus/{n}/fader",
              "level_law": "db", "min_db": -144},
  "send":    {"max": 32, "dest_max": 16, "dest_label": "Bus",
              "level": "/ch/{n}/send/{d}/level", "level_law": "db"}
}
```

- `{n}` is the number and `{d}` the destination (for sends). Python format specs such as `{n:02d}` are allowed.
- Set `mute_invert: true` if the address means "on" rather than "muted" (1 = on).
- `level_law` is one of:
  - `x32`: 0…1 on the X32 fader law
  - `db`: float in dB, with `min_db` for −∞
  - `db_int`
  - `linear`: 0…1 linear gain

## A new driver (Python)

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
    kinds = [KindSpec(K.CHANNEL, "Channel", 48), KindSpec(K.MAIN, "Main", 1)]

    def param(self, target: Target, which: str) -> OscParam:
        base = "/main" if target.kind == K.MAIN else f"/input/{target.number}"
        if which == "mute":
            return OscParam(base + "/mute", mute_direct, decode_mute_direct)
        return OscParam(base + "/level", level_db(-100), decode_level_db)
```

To add the driver:
1. Put the file in `autoeventplay/infrastructure/mixers/`.
2. Import it in `mixers/__init__.py`.
3. Optionally, add German translations of its labels to `i18n/de.py`.

For protocols other than OSC, derive directly from `MixerDriver`. Examples:
- `allen_heath.py`: MIDI over TCP
- `yamaha.py`: text protocol over TCP
- `yamaha_ls9.py`: SysEx in EtherMIDI
- `mackie_dl.py`: binary TCP protocol with a value cache
- `soundcraft.py`: WebSocket
- `soundcraft_hiqnet.py`: HiQnet

## Protocol details of the built-in drivers

| Driver | Mute | Level | Query |
|---|---|---|---|
| X32/M32 | `/ch/NN/mix/on` (1 = on) | `/ch/NN/mix/fader` 0…1 (X32 law) | address without argument |
| X-AIR/M-AIR | `/ch/NN/mix/on`, `/bus/N/mix/on`, `/lr/mix/on` | `…/mix/fader` | yes |
| WING | `/ch/N/mute` (1 = muted) | `/ch/N/fdr` in dB (−144 = −∞) | yes |
| Soundcraft Ui | `SETD^i.N.mute^1` | `SETD^i.N.mix^0..1` | state cache of the connection |
| A&H Qu (classic) | Note On `9N CH 7F/3F` | NRPN `BN 63 CH 62 17 06 VA 26 07` | no |
| A&H SQ, Qu-5/6/7 | NRPN, value `00/01` | NRPN 14 bit (audio/linear taper table) | NRPN `60 7F` |
| A&H dLive / Avantis | Note On `9N CH 7F/3F` (N = base + type offset) | NRPN `BN 63 CH 62 17 06 LV`, LV = (dB+54)/64·127 | dLive: SysEx `05 09`/`05 0B` |
| Yamaha (RCP) | `set MIXER:Current/InCh/Fader/On x 0 0/1` (1 = on) | `…/Fader/Level x 0 dB×100` (−32768 = −∞) | `get …` |
| Yamaha LS9 | Parameter Change, element `0x31`/`0x4C`/`0x5D`/`0x6B` "on" (1 = on) | element `0x33`/`0x4E`/`0x5F`/`0x6D` fader 0…1023 (823 = 0 dB) | Parameter Request |
| Mackie DL | type `0x13` value message, mute address (int, 1 = muted) | fader address (float32 in dB) | value cache from the console's broadcasts |
| Soundcraft Si/Vi | HiQnet MultiParamSet (0x0100) | HiQnet MultiParamSet | MultiParamGet (0x0103) |

## Yamaha LS9 (EtherMIDI)

The LS9 has no RCP. It is controlled with MIDI Parameter Change messages over its network port
("EtherMIDI", TCP 12300), as the Studio Manager and LS9 Editor do.

**Message format**
- The driver wraps SysEx `F0 43 1n 3E 12 01 eH eL iH iL cH cL d4…d0 F7` in the 16-byte EtherMIDI header.
- `n` is the device number minus 1, `e` the element, `i` the index and `c` the channel. `d` is the value as 7-bit groups.

**Connection**
- The connection is opened with `0x20` and acknowledged with `0x21`.
- If the console sends an authentication challenge, the driver answers it with the console's MAC address
  (option "MAC address", shown under *MISC SETUP*).

**Targets**
- Input channels (16 or 32 depending on the model)
- 16 mixes
- 8 matrices
- Stereo master (L/R and mono)
- Raw targets `element/index/channel` (e.g. `0x31/0/4`)

**Fader values**
- Fader values 0…1023 are converted with the LS9 fader table: 823 = 0 dB, 1023 = +10 dB.
- Below −10 dB the table is an approximation.

**Status:** The protocol knowledge comes from community reverse engineering and has not been confirmed on
every firmware version, so the driver is marked *experimental*.

## Mackie DL (DL16S / DL32S / DL32R)

The DL mixers are controlled with the same TCP protocol as the Master Fader app (port 50001). The driver is
ported from the [DigiMixer](https://github.com/jskeet/DemoCode/tree/main/DigiMixer) project (Apache 2.0).

**Message format**
- Header `0xAB`, sequence number, chunk count, message type and checksums.
- Values are sent as type `0x13` messages: address + value (float for faders, int for mutes).

**Connection**
1. Handshake (`0x01`).
2. Info request (`0x06`).
3. Model detection (`0x03`), which selects the DL16S or DL32 address profile.
4. Request all values.
5. Keep-alive every 2.5 s.

**Values and targets**
- The console broadcasts every value change. The driver keeps a cache, so mutes and levels can be queried
  and restored exactly.
- Targets: input channels, stereo returns, FX returns, aux buses, FX sends, main, aux sends per channel,
  and raw addresses (`49:float`, `12:int`).

**Not supported:** DL1608/DL806 (iPad dock mixers use a different protocol).

## Soundcraft Si / Vi (HiQnet)

**Background:** Soundcraft has not published an official remote-control interface for Si and Vi. They
can only be controlled remotely over HiQnet, as ViSi Remote and Mixing Station do.

**Protocol:** The driver implements HiQnet according to Harman's "HiQnet Third Party Programmer's Guide":
- 25-byte header
- MultiParamSet `0x0100`
- MultiParamGet `0x0103`
- TCP port 3804

**Address map:** The addresses of the individual parameters (virtual device, object, parameter/SV id, data
type) are console-specific and not public. They are entered in the "Address map" option:

```json
{
  "channel": {"max": 32, "label": "Channel", "vd": 3, "object": 256, "object_step": 1,
              "mute_sv": 1, "mute_type": "ubyte", "mute_invert": false,
              "level_sv": 0, "level_type": "long", "level_law": "db100", "level_min": -13800},
  "main":    {"label": "Master LR", "vd": 3, "object": 1024, "level_sv": 0, "level_type": "long", "level_law": "db100"}
}
```

- The object id is `object` + (number − 1) × `object_step`. For sends, (destination − 1) × `dest_step` is added.
- Data types: `byte`, `ubyte`, `word`, `uword`, `long`, `ulong`, `float`, `float64`.
- `level_law`: `db100`, `db10`, `db`, `linear` (0…1) or `fader` (0…1 fader law).
- Individual parameters can also be addressed as a raw target `vd/object/sv/type` (e.g. `3/256/1/ubyte`).

**Finding addresses**
1. Wireshark has a HiQnet dissector. Capture a session of ViSi Remote with the console.
2. Move the desired fader or mute.
3. Read the address, SV id and value range from the MultiParamSet packets.

Alternatively, ask Harman support.

**Access control:** On the console, disable HiQnet access control or allow this device's HiQnet address
(option, default 1337).
