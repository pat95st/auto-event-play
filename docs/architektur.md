# Architektur

```
autoeventplay/
├── domain/              Fachmodell ohne Infrastruktur
│   ├── levels.py        dB ↔ Gain ↔ Faderposition (Konsolen-Kurve, −∞ … +10 dB)
│   └── model.py         Routing, Pulte/Ziele, Cues, Wiedergabe-Aufträge
├── application/         Anwendungsschicht (CQRS)
│   ├── bus.py           CommandBus / QueryBus
│   ├── commands.py      Commands (Schreibseite)
│   ├── queries.py       Queries (Leseseite)
│   ├── command_handlers.py / query_handlers.py
│   ├── playback.py      Wiedergabe-Service (zwei Spuren: Medien ∥ Sprache)
│   └── cues.py          Pult-Aktionen mit referenzgezähltem Zurücksetzen
├── infrastructure/
│   ├── db.py            SQLite + Migrationen
│   ├── repositories.py  Persistenz
│   ├── audio/           Engine (Matrix, Meter, mehrere Geräte), Decoder (ffmpeg), TTS, Gong
│   ├── erm/             eventrentmanager-Client und Synchronisation
│   ├── mixers/          Pult-Treiber, Transporte (OSC/UDP, TCP, WebSocket), Manager
│   ├── auth.py          Passwort-Hashes (scrypt), signierte Session-Cookies
│   ├── events.py        Event-Hub für Server-Sent Events
│   └── logs.py          Rotierende Logdatei + Ringpuffer für die Weboberfläche
├── web/                 Starlette-App, Jinja2-Templates, CSS/JS
└── container.py         Composition Root (Verdrahtung)
```

## CQRS

- **Commands** (`application/commands.py`) drücken eine Absicht aus (`SetInputLevel`, `SaveCueRule`,
  `PairWithErm`, `StopPlayback` …). Ihre Handler validieren, ändern den Zustand über Repositories bzw.
  Services und geben höchstens eine ID oder ein kleines Ergebnis zurück.
- **Queries** (`application/queries.py`) ändern nichts und liefern fertige Lesemodelle (JSON-Dicts) für
  die Oberfläche (`GetDashboard`, `ListCueRules`, `GetLogs` …).
- Die Web-Schicht übersetzt HTTP nur in Commands/Queries (`web/app.py`): `POST` → Command, `GET` → Query.
- Änderungen lösen ein `changed`-Event aus; die Oberfläche lädt daraufhin die betroffenen Queries neu.
  Meter (`meters`, 20 Hz) und Logzeilen (`log`) kommen über denselben SSE-Stream `/api/stream`.

## Audio-Engine

Pro Block: Voices → Quell-Busse (Medien L/R, TTS, Aufnahme) → Eingangsfader/Mute → Meter →
Matrix (Eingänge × Ausgänge) → Ausgangsfader/Mute → Meter → Geräte. Das erste Gerät der Ausgangsliste ist
Taktgeber; weitere Geräte werden über kleine Ringpuffer versorgt. Ohne Gerät taktet ein Timer-Thread.

## Wiedergabe

Aufträge aus der Warteschlange des eventrentmanagers laufen in zwei Spuren: Medien (Jingle, Audio) und
Sprache (TTS, Aufnahme). Innerhalb einer Spur strikt nacheinander, zwischen den Spuren parallel (eine neue
Durchsage bricht keinen Jingle ab, abbrechen nur per `stop`). Abschaltbar unter *Audio*.

Ablauf je Auftrag: `playing` melden → Audio vorbereiten (Cache/Decoder/TTS) → Cue-Aktionen „vor dem
Abspielen“ (max. 2,5 s Wartezeit, danach startet das Audio trotzdem) → Vorlauf → Abspielen →
Zurücksetzen + Aktionen „nach dem Abspielen“ → `done`/`failed`/`cancelled` melden. Statusmeldungen werden
bei Netzproblemen wiederholt; bereits bearbeitete Auftrags-IDs werden gespeichert und nie doppelt gespielt.
