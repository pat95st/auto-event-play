"""i18n (Msg/t), language setting and all web pages rendered in English and German."""

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from autoeventplay.config import AppConfig
from autoeventplay.container import Services
from autoeventplay.i18n import DE, Msg, set_language, t
from autoeventplay.i18n.de import DE as DE_CATALOG
from autoeventplay.web.app import create_app

PAGES = ["/", "/routing", "/cues", "/consoles", "/instance", "/audio", "/logs", "/system"]


def test_msg_and_translation():
    m = Msg("Channel {ch} of \"{device}\" is used twice", ch=3, device="USB")
    assert m == 'Channel 3 of "USB" is used twice'
    assert t(m, "en") == 'Channel 3 of "USB" is used twice'
    assert t(m, "de") == "Kanal 3 von „USB“ ist doppelt belegt"
    assert t("Mixer", "de") == "Mischpult"
    assert t("some unknown text", "de") == "some unknown text"
    # nested Msg parameters are translated too
    nested = Msg("Error: {error}", error=Msg("{action}: timeout", action="set"))
    assert t(nested, "de") == "Fehler: set: Zeitüberschreitung"
    token = set_language("de")
    try:
        assert t("Save") == "Speichern"
    finally:
        set_language("en")
    assert t("Save") == "Save"
    assert DE is DE_CATALOG or DE == DE_CATALOG


def test_catalog_placeholders_match():
    ph = lambda s: sorted(re.findall(r"\{(\w*)\}", s))  # noqa: E731
    assert all(ph(k) == ph(v) for k, v in DE_CATALOG.items())


@pytest.fixture()
def client(tmp_path: Path):
    services = Services(AppConfig(data_dir=tmp_path, audio_backend="null", port=0))
    app = create_app(services)
    with TestClient(app) as cl:
        cl.headers["X-AEP"] = "1"
        yield cl


def _login(cl):
    r = cl.post("/api/auth/setup", json={"username": "admin", "password": "secret123"})
    assert r.status_code == 200, r.text


def test_pages_english_default_and_german(client):
    r = client.get("/setup")
    assert r.status_code == 200
    assert '<html lang="en"' in r.text and "Create account" in r.text
    _login(client)
    for page in PAGES:
        r = client.get(page)
        assert r.status_code == 200, page
        assert '<html lang="en"' in r.text
        assert "Mischpult" not in r.text
    assert client.get("/pulte", follow_redirects=False).headers["location"] == "/consoles"

    assert client.post("/api/ui/language", json={"language": "de"}).status_code == 200
    for page in PAGES:
        r = client.get(page)
        assert r.status_code == 200, page
        assert '<html lang="de"' in r.text
        assert "Mischpult" in r.text  # navigation
    r = client.get("/consoles")
    assert "Gekoppelte Mischpulte" in r.text
    assert "window.AEP_I18N" in r.text and "Speichern" in r.text

    # API errors and driver metadata are translated
    r = client.post("/api/mixers/save", json={"name": "X", "type": "nope", "host": "1.2.3.4", "options": {}})
    assert r.status_code in (400, 422), r.text
    assert "Unbekannter Pulttyp" in r.json()["error"]
    types = client.get("/api/mixer-types").json()["result"]["types"]
    ls9 = next(x for x in types if x["type_id"] == "yamaha_ls9")
    assert "experimentell" in ls9["display_name"]

    assert client.post("/api/ui/language", json={"language": "en"}).status_code == 200
    assert "Linked consoles" in client.get("/consoles").text


def test_dynamics_api(client):
    _login(client)
    routing = client.get("/api/routing").json()["result"]
    ch = routing["inputs"][0]
    assert ch["dyn"]["enabled"] is False and ch["dyn_active"] is False
    r = client.post(f"/api/inputs/{ch['id']}/dyn", json={"dyn": {"enabled": True, "threshold": -80, "ratio": 4}})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert res["active"] is True and res["dyn"]["threshold"] == -60 and res["dyn"]["ratio"] == 4
    routing = client.get("/api/routing").json()["result"]
    assert routing["inputs"][0]["dyn"]["enabled"] is True
