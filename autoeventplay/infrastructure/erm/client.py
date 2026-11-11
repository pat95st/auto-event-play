"""HTTP client for the eventrentmanager player API (see docs/abspielgeraet-api.md there).

Uses only the standard library (urllib in a worker thread) – no extra
dependencies on the Pi.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ... import __version__
from ...i18n import Msg

log = logging.getLogger("aep.erm")

USER_AGENT = f"AutoEventPlay/{__version__}"


class ErmError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ErmUnauthorized(ErmError):
    pass


def normalize_base_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


class ErmClient:
    def __init__(self, base_url: str, token: str | None, verify_tls: bool = True, timeout: float = 10.0):
        self.base_url = normalize_base_url(base_url)
        self.token = token
        self.timeout = timeout
        self._ctx = ssl.create_default_context()
        if not verify_tls:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return self.base_url + (path if path.startswith("/") else "/" + path)

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, auth: bool = True,
                 timeout: float | None = None) -> tuple[int, bytes]:
        if not self.base_url:
            raise ErmError("No eventrentmanager URL configured")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._url(path), data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth and self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self._ctx) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            message = _error_message(payload) or f"HTTP {exc.code}"
            if exc.code == 401:
                raise ErmUnauthorized(message, 401) from exc
            raise ErmError(message, exc.code) from exc
        except urllib.error.URLError as exc:
            raise ErmError(Msg("Server not reachable: {error}", error=str(exc.reason))) from exc
        except (TimeoutError, OSError) as exc:
            raise ErmError(Msg("Server not reachable: {error}", error=str(exc))) from exc

    def _json(self, method: str, path: str, body: dict[str, Any] | None = None, auth: bool = True) -> dict[str, Any]:
        _, payload = self._request(method, path, body, auth)
        try:
            data = json.loads(payload.decode("utf-8") or "{}")
        except ValueError as exc:
            raise ErmError("Response is not JSON – is the URL correct?") from exc
        if not isinstance(data, dict):
            raise ErmError("Unexpected response from the server")
        return data

    # ------------------------------------------------------------------ API (sync)

    def pair_sync(self, code: str, name: str) -> dict[str, Any]:
        # send an existing token along so an already paired device is re-assigned instead of duplicated
        return self._json("POST", "/api/player/v1/pair", {"code": code, "name": name, "version": __version__}, auth=True)

    def state_sync(self, playing: str = "") -> dict[str, Any]:
        query = urllib.parse.urlencode({"version": __version__, "playing": playing})
        return self._json("GET", f"/api/player/v1/state?{query}")

    def report_status_sync(self, status_url: str, status: str, error: str | None = None) -> None:
        body: dict[str, Any] = {"status": status}
        if error:
            body["error"] = error[:480]
        self._json("POST", status_url, body)

    def download_sync(self, path: str, target: Path) -> int:
        tmp = target.with_suffix(target.suffix + ".part")
        req = urllib.request.Request(self._url(path), method="GET")
        req.add_header("User-Agent", USER_AGENT)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=60, context=self._ctx) as resp, open(tmp, "wb") as fh:
                size = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    size += len(chunk)
        except urllib.error.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            if exc.code == 401:
                raise ErmUnauthorized("Download not allowed (invalid token)", 401) from exc
            raise ErmError(_error_message(exc.read()) or Msg("Download failed (HTTP {code})", code=exc.code), exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            tmp.unlink(missing_ok=True)
            raise ErmError(Msg("Download failed: {error}", error=str(exc))) from exc
        tmp.replace(target)
        return size

    # ------------------------------------------------------------------ API (async)

    async def pair(self, code: str, name: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.pair_sync, code, name)

    async def state(self, playing: str = "") -> dict[str, Any]:
        return await asyncio.to_thread(self.state_sync, playing)

    async def report_status(self, status_url: str, status: str, error: str | None = None) -> None:
        await asyncio.to_thread(self.report_status_sync, status_url, status, error)

    async def download(self, path: str, target: Path) -> int:
        return await asyncio.to_thread(self.download_sync, path, target)


def _error_message(payload: bytes) -> str | None:
    try:
        data = json.loads(payload.decode("utf-8"))
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
    except Exception:
        pass
    return None
