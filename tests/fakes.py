"""Test doubles: a fake eventrentmanager player API and a fake OSC console."""

from __future__ import annotations

import io
import json
import socket
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np

from autoeventplay.infrastructure.mixers.transports import osc_decode, osc_encode


def make_wav(seconds: float = 0.4, freq: float = 440.0, sr: int = 48000) -> bytes:
    t = np.arange(int(sr * seconds)) / sr
    data = (np.sin(2 * np.pi * freq * t) * 0.5 * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.tobytes())
    return buf.getvalue()


class FakeErm:
    """Implements the subset of /api/player/v1 used by AutoEventPlay."""

    def __init__(self) -> None:
        self.code = "123456"
        self.token = "mvp_testtoken0000000000000000000000000000000000"
        self.rental = {"id": "r1", "name": "Stadtfest", "location": "Marktplatz", "start_date": "2026-11-10", "end_date": "2026-11-11",
                       "gong_by_default": True, "tts_language": None}
        self.files = {"f1": {"id": "f1", "kind": "jingle", "title": "Opener", "description": None, "filename": "opener.wav",
                             "mime": "audio/wav", "size": 0, "version": "f1.wav", "url": "/api/player/v1/files/f1"}}
        self.file_data = {"f1": make_wav()}
        self.queue: list[dict[str, Any]] = []
        self.status_reports: list[tuple[str, str, str | None]] = []
        self.pair_calls: list[dict[str, Any]] = []
        self.polls = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _json(self, code: int, payload: Any) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authed(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {fake.token}"

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                if not self._authed():
                    return self._json(401, {"error": "Ungültiger oder deaktivierter Geräte-Token."})
                if path == "/api/player/v1/state":
                    fake.polls += 1
                    return self._json(200, {
                        "server_time": "2026-11-10T19:00:00+01:00", "poll_interval_seconds": 1,
                        "player": {"id": "p1", "name": "Pi"}, "tts": {"language": "de-DE", "voice": None, "rate": 1.0},
                        "gong": None, "rental": fake.rental, "files": list(fake.files.values()),
                        "minute_programs": [], "scheduled": [], "queue": [q for q in fake.queue if q.get("_status") is None],
                    })
                if path.startswith("/api/player/v1/files/"):
                    fid = path.rsplit("/", 1)[1]
                    data = fake.file_data.get(fid)
                    if data is None:
                        return self._json(404, {"error": "nicht gefunden"})
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return None
                return self._json(404, {"error": "unbekannt"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path.split("?")[0]
                if path == "/api/player/v1/pair":
                    fake.pair_calls.append({"body": body, "auth": self.headers.get("Authorization")})
                    if body.get("code") != fake.code:
                        return self._json(422, {"error": "Der Code ist ungültig oder abgelaufen."})
                    return self._json(200, {"ok": True, "token": fake.token, "player": {"id": "p1", "name": body.get("name")},
                                            "rental": {k: fake.rental[k] for k in ("id", "name", "location", "start_date", "end_date")},
                                            "poll_interval_seconds": 1})
                if not self._authed():
                    return self._json(401, {"error": "Ungültiger Token"})
                if path.startswith("/api/player/v1/requests/") and path.endswith("/status"):
                    rid = path.split("/")[5]
                    fake.status_reports.append((rid, body.get("status"), body.get("error")))
                    for q in fake.queue:
                        if q["id"] == rid and body.get("status") in ("done", "failed", "cancelled"):
                            q["_status"] = body.get("status")
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "unbekannt"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self) -> "FakeErm":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def add_job(self, job_id: str, kind: str = "jingle", **extra: Any) -> None:
        item = {"id": job_id, "kind": kind, "label": extra.pop("label", "Opener"), "file_id": extra.pop("file_id", "f1" if kind in ("jingle", "audio") else None),
                "file_url": None, "text": None, "language": "de-DE", "recording_url": None, "recording_mime": None, "with_gong": False,
                "requested_by": "Test", "created_at": "", "status_url": f"/api/player/v1/requests/{job_id}/status"}
        item.update(extra)
        self.queue.append(item)


class FakeOscConsole:
    """UDP OSC endpoint that stores values and answers queries (like an X32)."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.values: dict[str, Any] = {}
        self.log: list[tuple[str, list[Any]]] = []
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def start(self) -> "FakeOscConsole":
        self.thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.thread.join(1)
        self.sock.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            address, args = osc_decode(data)
            if args:
                self.values[address] = args[0]
                self.log.append((address, args))
            else:
                if address == "/info":
                    self.sock.sendto(osc_encode("/info", "V2.07", "fake", "X32", "4.06"), addr)
                elif address in self.values:
                    self.sock.sendto(osc_encode(address, self.values[address]), addr)
