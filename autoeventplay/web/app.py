"""Web application: HTML pages, JSON API (commands/queries) and the SSE live stream."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..application import commands as c
from ..application import queries as q
from ..application.bus import DomainError
from ..container import Services
from ..infrastructure.auth import SESSION_COOKIE, SESSION_MAX_AGE, verify_password
from ..infrastructure.logs import log_files

log = logging.getLogger("aep.web")
HERE = Path(__file__).parent

PUBLIC_PATHS = ("/login", "/setup", "/static/", "/api/auth/", "/api/health")


def create_app(services: Services) -> Starlette:
    s = services
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    vendor_ok = (HERE / "static" / "vendor" / "bootstrap" / "bootstrap.min.css").exists()

    def render(request: Request, template: str, **ctx: Any) -> HTMLResponse:
        html = env.get_template(template).render(
            request=request, user=request.scope.get("user"), version=__version__, vendor_ok=vendor_ok,
            active=ctx.pop("active", ""), **ctx,
        )
        return HTMLResponse(html)

    # ------------------------------------------------------------------ auth middleware

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
            path = request.url.path
            user = s.sessions.verify(request.cookies.get(SESSION_COOKIE))
            request.scope["user"] = user
            if path.startswith(PUBLIC_PATHS):
                return await call_next(request)
            if s.user_repo.count() == 0:
                return _deny(request, "/setup")
            if not user:
                return _deny(request, "/login?next=" + path)
            if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.headers.get("x-aep") != "1":
                return JSONResponse({"error": "Fehlender Anfrage-Header (CSRF-Schutz)"}, status_code=403)
            return await call_next(request)

    def _deny(request: Request, location: str) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "Nicht angemeldet", "redirect": location}, status_code=401)
        return RedirectResponse(location, status_code=303)

    # ------------------------------------------------------------------ helpers

    async def body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def api(fn: Callable[..., Awaitable[Any]]) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            try:
                result = await fn(request)
            except DomainError as exc:
                return JSONResponse({"error": str(exc)}, status_code=exc.status)
            except (KeyError, TypeError, ValueError) as exc:
                log.debug("bad request", exc_info=True)
                return JSONResponse({"error": f"Ungültige Anfrage: {exc}"}, status_code=400)
            if isinstance(result, Response):
                return result
            return JSONResponse({"ok": True, "result": result})
        return endpoint

    def command(factory: Callable[[Request, dict[str, Any]], Any]) -> Callable[[Request], Awaitable[Response]]:
        async def run(request: Request) -> Any:
            data = await body(request)
            return await s.commands.dispatch(factory(request, data))
        return api(run)

    def query(factory: Callable[[Request], Any]) -> Callable[[Request], Awaitable[Response]]:
        async def run(request: Request) -> Any:
            return await s.queries.dispatch(factory(request))
        return api(run)

    def p(request: Request, name: str) -> str:
        return request.path_params[name]

    # ------------------------------------------------------------------ pages

    async def page_mixer(request: Request) -> Response:
        return render(request, "mixer.html", active="mixer", title="Mischpult")

    async def page_routing(request: Request) -> Response:
        return render(request, "routing.html", active="routing", title="Routing")

    async def page_consoles(request: Request) -> Response:
        return render(request, "consoles.html", active="consoles", title="Pulte")

    async def page_cues(request: Request) -> Response:
        return render(request, "cues.html", active="cues", title="Cues")

    async def page_instance(request: Request) -> Response:
        return render(request, "instance.html", active="instance", title="eventrentmanager")

    async def page_audio(request: Request) -> Response:
        return render(request, "audio.html", active="audio", title="Audio")

    async def page_logs(request: Request) -> Response:
        return render(request, "logs.html", active="logs", title="Logs")

    async def page_system(request: Request) -> Response:
        return render(request, "system.html", active="system", title="System")

    async def page_login(request: Request) -> Response:
        if s.user_repo.count() == 0:
            return RedirectResponse("/setup", status_code=303)
        return render(request, "login.html", title="Anmelden")

    async def page_setup(request: Request) -> Response:
        if s.user_repo.count() > 0:
            return RedirectResponse("/login", status_code=303)
        return render(request, "setup.html", title="Einrichtung")

    # ------------------------------------------------------------------ auth API

    def _session_response(username: str) -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.set_cookie(SESSION_COOKIE, s.sessions.issue(username), max_age=SESSION_MAX_AGE, httponly=True, samesite="strict")
        return resp

    login_failures: dict[str, list[float]] = {}

    async def api_login(request: Request) -> Response:
        data = await body(request)
        ip = request.client.host if request.client else "?"
        loop = asyncio.get_running_loop()
        now = loop.time()
        recent = [t for t in login_failures.get(ip, []) if now - t < 300]
        if len(recent) >= 10:
            return JSONResponse({"error": "Zu viele Fehlversuche – bitte 5 Minuten warten"}, status_code=429)
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        ok = await asyncio.to_thread(verify_password, password, s.user_repo.get_hash(username))
        if not ok:
            recent.append(now)
            login_failures[ip] = recent
            log.warning("Fehlgeschlagene Anmeldung für „%s“ von %s", username, ip)
            return JSONResponse({"error": "Benutzername oder Passwort falsch"}, status_code=401)
        login_failures.pop(ip, None)
        log.info("Anmeldung: %s (%s)", username, ip)
        return _session_response(username)

    async def api_setup(request: Request) -> Response:
        data = await body(request)
        try:
            await s.commands.dispatch(c.CreateAdmin(str(data.get("username") or ""), str(data.get("password") or "")))
        except DomainError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        return _session_response(str(data.get("username")).strip())

    async def api_logout(request: Request) -> Response:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    async def api_health(request: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__, "audio": s.engine.status_message, "erm": s.sync.status.get("message")})

    # ------------------------------------------------------------------ live stream (SSE)

    async def api_stream(request: Request) -> Response:
        queue = s.events.subscribe()

        async def gen():  # noqa: ANN202
            try:
                yield "retry: 2000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                        yield payload
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                s.events.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ logs download

    async def api_log_download(request: Request) -> Response:
        files = log_files(s.config.log_dir)
        if not files:
            return PlainTextResponse("Noch keine Logdatei vorhanden.")
        name = request.query_params.get("file") or files[0].name
        match = next((f for f in files if f.name == name), None)
        if match is None:
            return PlainTextResponse("Datei nicht gefunden", status_code=404)
        return FileResponse(match, media_type="text/plain; charset=utf-8", filename=match.name)

    async def api_log_files(request: Request) -> Response:
        return JSONResponse({"files": [{"name": f.name, "size": f.stat().st_size} for f in log_files(s.config.log_dir)]})

    # ------------------------------------------------------------------ routes

    def _f(v: Any) -> float | None:
        return None if v in (None, "") else float(v)

    routes = [
        Route("/", page_mixer),
        Route("/routing", page_routing),
        Route("/pulte", page_consoles),
        Route("/cues", page_cues),
        Route("/instanz", page_instance),
        Route("/audio", page_audio),
        Route("/logs", page_logs),
        Route("/system", page_system),
        Route("/login", page_login),
        Route("/setup", page_setup),

        Route("/api/health", api_health),
        Route("/api/auth/login", api_login, methods=["POST"]),
        Route("/api/auth/setup", api_setup, methods=["POST"]),
        Route("/api/auth/logout", api_logout, methods=["POST"]),
        Route("/api/stream", api_stream),

        # queries
        Route("/api/dashboard", query(lambda r: q.GetDashboard())),
        Route("/api/routing", query(lambda r: q.GetRouting())),
        Route("/api/audio", query(lambda r: q.GetAudioSetup())),
        Route("/api/mixers", query(lambda r: q.ListMixers())),
        Route("/api/mixer-types", query(lambda r: q.GetMixerTypes())),
        Route("/api/cues", query(lambda r: q.ListCueRules())),
        Route("/api/erm", query(lambda r: q.GetErmStatus())),
        Route("/api/playback", query(lambda r: q.GetPlayback())),
        Route("/api/logs", query(lambda r: q.GetLogs(int(r.query_params.get("after", 0)), r.query_params.get("level", "DEBUG"),
                                                     r.query_params.get("text", ""), min(2000, int(r.query_params.get("limit", 500)))))),
        Route("/api/logs/files", api_log_files),
        Route("/api/logs/download", api_log_download),
        Route("/api/system", query(lambda r: q.GetSystemInfo())),

        # commands
        Route("/api/inputs/{id}/level", command(lambda r, d: c.SetInputLevel(p(r, "id"), d["level_db"])), methods=["POST"]),
        Route("/api/inputs/{id}/mute", command(lambda r, d: c.SetInputMute(p(r, "id"), bool(d["muted"]))), methods=["POST"]),
        Route("/api/inputs/{id}/name", command(lambda r, d: c.RenameChannel("input", p(r, "id"), d["name"])), methods=["POST"]),
        Route("/api/outputs/{id}/level", command(lambda r, d: c.SetOutputLevel(p(r, "id"), d["level_db"])), methods=["POST"]),
        Route("/api/outputs/{id}/mute", command(lambda r, d: c.SetOutputMute(p(r, "id"), bool(d["muted"]))), methods=["POST"]),
        Route("/api/outputs/{id}/name", command(lambda r, d: c.RenameChannel("output", p(r, "id"), d["name"])), methods=["POST"]),
        Route("/api/outputs/{id}/tone", command(lambda r, d: c.SetTestTone(p(r, "id"), bool(d["enabled"]))), methods=["POST"]),
        Route("/api/outputs", command(lambda r, d: c.ConfigureOutputs(list(d["outputs"]))), methods=["POST"]),
        Route("/api/crosspoints", command(lambda r, d: c.SetCrosspoint(d["input_id"], d["output_id"], bool(d["enabled"]))), methods=["POST"]),
        Route("/api/audio/settings", command(lambda r, d: c.SaveAudioSettings(int(d["sample_rate"]), int(d["blocksize"]), bool(d.get("parallel_lanes", True)))), methods=["POST"]),
        Route("/api/audio/tts", command(lambda r, d: c.SaveTtsSettings(d.get("engine", "auto"), d.get("piper_binary", "piper"), d.get("espeak_binary", "espeak-ng"), dict(d.get("voice_overrides") or {}))), methods=["POST"]),
        Route("/api/mixers/save", command(lambda r, d: c.SaveMixer(d.get("id") or None, d.get("name", ""), d.get("type", ""), d.get("host", ""),
                                                                    int(d["port"]) if d.get("port") not in (None, "") else None,
                                                                    bool(d.get("enabled", True)), dict(d.get("options") or {}))), methods=["POST"]),
        Route("/api/mixers/{id}/delete", command(lambda r, d: c.DeleteMixer(p(r, "id"))), methods=["POST"]),
        Route("/api/mixers/{id}/test", command(lambda r, d: c.TestMixer(p(r, "id"))), methods=["POST"]),
        Route("/api/mixers/{id}/action", command(lambda r, d: c.SendMixerAction(p(r, "id"), dict(d["target"]), d.get("mute", "unchanged"), _f(d.get("level_db")))), methods=["POST"]),
        Route("/api/cues/save", command(lambda r, d: c.SaveCueRule(d)), methods=["POST"]),
        Route("/api/cues/{id}/delete", command(lambda r, d: c.DeleteCueRule(p(r, "id"))), methods=["POST"]),
        Route("/api/cues/{id}/test", command(lambda r, d: c.TestCueRule(p(r, "id"), float(d.get("duration_s") or 3))), methods=["POST"]),
        Route("/api/erm/settings", command(lambda r, d: c.SaveErmSettings(d.get("base_url", ""), d.get("device_name", ""), bool(d.get("verify_tls", True)), d.get("token") or None)), methods=["POST"]),
        Route("/api/erm/pair", command(lambda r, d: c.PairWithErm(d.get("base_url", ""), str(d.get("code", "")), d.get("device_name", ""))), methods=["POST"]),
        Route("/api/erm/unpair", command(lambda r, d: c.UnpairErm()), methods=["POST"]),
        Route("/api/erm/sync", command(lambda r, d: c.SyncNow()), methods=["POST"]),
        Route("/api/playback/stop", command(lambda r, d: c.StopPlayback()), methods=["POST"]),
        Route("/api/playback/test", command(lambda r, d: c.PlayLocal(d.get("kind", ""), d.get("file_id"), d.get("text"), d.get("language"), bool(d.get("with_gong")))), methods=["POST"]),
        Route("/api/account/password", command(lambda r, d: c.ChangePassword(r.scope["user"], d.get("current_password", ""), d.get("new_password", ""))), methods=["POST"]),

        Mount("/static", app=StaticFiles(directory=HERE / "static"), name="static"),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):  # noqa: ANN202
        await s.start()
        try:
            yield
        finally:
            await s.shutdown()

    return Starlette(routes=routes, middleware=[Middleware(AuthMiddleware)], lifespan=lifespan)
