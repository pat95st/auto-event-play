# Architecture

```
autoeventplay/
├── domain/              Domain model without infrastructure
│   ├── levels.py        dB ↔ gain ↔ fader position (console law, −∞ … +10 dB)
│   ├── eq.py            Channel EQ: low cut, 4 parametric bands, high cut (coefficients)
│   ├── dynamics.py      Channel compressor: settings and static curve (soft knee)
│   └── model.py         Routing, consoles/targets, cues, playback jobs
├── application/         Application layer (CQRS)
│   ├── bus.py           CommandBus / QueryBus
│   ├── commands.py      Commands (write side)
│   ├── queries.py       Queries (read side)
│   ├── command_handlers.py / query_handlers.py
│   ├── playback.py      Playback service (two lanes: media ∥ speech)
│   └── cues.py          Console actions with reference-counted restore
├── infrastructure/
│   ├── db.py            SQLite + migrations
│   ├── repositories.py  Persistence
│   ├── audio/           Engine (matrix, EQ, compressor, meters, several devices), decoder (ffmpeg), TTS, gong
│   ├── erm/             eventrentmanager client and synchronisation
│   ├── mixers/          Console drivers, transports (OSC/UDP, TCP, WebSocket), manager
│   ├── auth.py          Password hashes (scrypt), signed session cookies
│   ├── events.py        Event hub for Server-Sent Events
│   └── logs.py          Rotating log file + ring buffer for the web interface
├── i18n/                Translations (English source, German catalog)
├── web/                 Starlette app, Jinja2 templates, CSS/JS
└── container.py         Composition root (wiring)
```

## CQRS

- **Commands** (`application/commands.py`) express an intent, e.g. `SetInputLevel`, `SetChannelDynamics`,
  `SaveCueRule`, `PairWithErm`, `StopPlayback`, `SetLanguage`.
  - Their handlers validate the input and change state through repositories or services.
  - They return at most an id or a small result.
- **Queries** (`application/queries.py`) change nothing. They return ready-made read models (JSON dicts)
  for the UI, e.g. `GetDashboard`, `ListCueRules`, `GetLogs`.
- The web layer only translates HTTP into commands and queries (`web/app.py`): `POST` → command, `GET` → query.
- Changes raise a `changed` event, and the UI then reloads the affected queries. Meters (`meters`, 20 Hz,
  including compressor gain reduction) and log lines (`log`) arrive over the same SSE stream, `/api/stream`.

## Audio engine

The signal flow per block is:

1. voices
2. source buses (media L/R, TTS, recording)
3. input trim → input EQ and compressor (in the configured order) → input fader/mute → meter
4. matrix (inputs × outputs)
5. output EQ and compressor (in the configured order) → output fader/mute → meter
6. devices

**EQ** (`domain/eq.py`)
- Low cut, four fully parametric bands and high cut.
- The coefficients (RBJ cookbook or Butterworth) are recalculated on every change and filtered as
  second-order sections with `scipy.signal.sosfilt`.
- The filter state is kept across changes, so there are no clicks.
- A flat or disabled EQ is bypassed completely.

**Compressor** (`domain/dynamics.py`, `infrastructure/audio/dyn_processor.py`)
- A feed-forward peak compressor with a soft knee.
- The level is detected in chunks of 16 samples.
- The gain is smoothed with the attack/release time constants in the dB domain, then interpolated
  linearly per sample.
- The current gain reduction is sent with the meters. A disabled compressor is bypassed.

**Trim and order**
- The input trim (−24 … +24 dB) is applied before any processing, so it also sets how hard the signal
  drives the compressor.
- Each channel stores its processing order (`eq_dyn` or `dyn_eq`). The engine applies the two stages in
  that order; a bypassed stage is skipped.

**Devices**
- The first device in the output list is the clock master.
- Further devices are fed through small ring buffers.
- Without a device, a timer thread provides the clock.

## Playback

Jobs from the eventrentmanager queue run in two lanes: media (jingle, audio) and speech (TTS, recording).
- Within a lane, jobs run strictly one after another.
- The two lanes run in parallel, so a new announcement does not cancel a jingle. Jobs are cancelled only by `stop`.
- Parallel lanes can be switched off under *Audio*.

Steps per job:
1. Report `playing`.
2. Prepare the audio (cache, decoder or TTS).
3. Run the cue actions "before playback". This waits at most 2.5 s; after that the audio starts anyway.
4. Wait the pre-delay.
5. Play.
6. Restore and run the actions "after playback".
7. Report `done`, `failed` or `cancelled`.

Status reports are retried on network problems. Processed job ids are stored, so a job is never played twice.

## Internationalisation

- English is the source language. The language is a global setting (`ui.language`, default `en`).
- A middleware sets it per request in a context variable.
- `Msg(template, **params)` is a `str` that keeps its template and parameters. This lets error messages from
  deep inside drivers be translated at the web boundary (`t(text_of(exc))`).
- Templates use `_()`, and JavaScript uses `AEP.t()` with the catalog injected into the page
  (`window.AEP_I18N`).
