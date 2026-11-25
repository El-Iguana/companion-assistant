"""The resident service (RESIDENT_PLAN.md R2) against a fake Cantinero server.

    .venv/bin/python -m pytest -q tests/test_resident_service.py
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from resident import config, service
from resident import __main__ as cli

H = {"X-CSRF-Token": ""}          # what desktop.js sends on a resident (no cookie, empty value)


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """A fake amd-halo; records what reached it."""
    monkeypatch.setenv("RESIDENT_DATA", str(tmp_path))
    config.save({"host": "halo.test", "token": "TOK", "ca_pem": "", "insecure_http": True})
    seen, state = [], {"down": False, "status": 200}

    def handle(req: httpx.Request):
        if state["down"]:
            raise httpx.ConnectError("down")
        seen.append((req.method, req.url.path, req.headers.get("authorization"), req.content))
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"detail": "no"})
        if req.url.path == "/pdesk/chat":
            async def sse():                     # streamed, as the real reply is
                yield b'data: {"token": "Hi"}\n\n'
                yield b'data: {"done": true}\n\n'
            return httpx.Response(200, content=sse(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"ok": True, "path": req.url.path})

    def fake_get():
        if service.upstream.client is None:
            service.upstream.client = httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                                        headers={"Authorization": "Bearer TOK"})
            service.upstream.base = "http://halo.test"
        return service.upstream.client, service.upstream.base, {}
    monkeypatch.setattr(service.upstream, "get", fake_get)
    monkeypatch.setattr(service.upstream, "reset", lambda: None)
    service.upstream.client = None
    yield seen, state
    service.upstream.client = None


@pytest.fixture()
def client():
    return TestClient(service.app, base_url="http://127.0.0.1:7867")


def test_the_desktop_page_and_its_routes_forward_with_the_token(server, client):
    seen, _ = server
    assert "/static/js/desktop.js" in client.get("/desktop").text
    assert client.get("/static/js/voice.js").status_code == 200
    assert client.get("/pdesk/state").json()["path"] == "/pdesk/state"
    client.post("/pdesk/open", json={"character_id": "mira"}, headers=H)
    client.post("/pmedia/music/duck", json={"on": True}, headers=H)                 # local: never forwarded
    assert [(m, p, a) for m, p, a, _ in seen] == [("GET", "/pdesk/state", "Bearer TOK"),
                                                  ("POST", "/pdesk/open", "Bearer TOK")]
    assert json.loads(seen[1][3]) == {"character_id": "mira"}


def test_only_the_allowlist_is_forwarded(server, client):
    seen, _ = server
    assert client.post("/pdesk/admin", json={}, headers=H).status_code == 404
    assert client.get("/pdesk/open").status_code == 404           # a POST route, asked with GET
    assert client.post("/pmedia/import", headers=H).status_code in (404, 405)
    assert seen == []


def test_other_sites_and_rebinding_are_refused(server, client):
    seen, _ = server
    assert client.post("/pdesk/open", json={}).status_code == 403                      # no custom header
    assert client.post("/pdesk/open", json={}, headers={**H, "Origin": "https://evil.example"}).status_code == 403
    rebound = TestClient(service.app, base_url="http://evil.example:7867")
    assert rebound.get("/pdesk/state").status_code == 403
    assert seen == []


def test_the_chat_streams_through(server, client):
    body = client.post("/pdesk/chat", json={"session_id": "s", "text": "hi"}, headers=H).text
    assert '"token": "Hi"' in body and '"done": true' in body


def test_server_down_she_says_so_and_nothing_hangs(server, client):
    _, state = server
    state["down"] = True
    assert client.get("/pdesk/state").status_code == 502
    said = client.post("/pdesk/chat", json={"session_id": "s", "text": "hi"}, headers=H).text
    assert "can't reach the bar" in said and '"done": true' in said             # spoken, then done
    assert client.post("/pmedia/music/duck", json={"on": True}, headers=H).json() == {"ok": True}  # local


def test_a_removed_resident_doesnt_reload_forever(server, client):
    _, state = server
    state["status"] = 401
    r = client.get("/pdesk/state")
    assert r.status_code == 403 and "removed" in r.json()["detail"]            # not 401: the page reloads on that
    assert "removed" in client.post("/pdesk/chat", json={}, headers=H).text


def test_voice_is_local(server, client, monkeypatch):
    seen, _ = server
    from services import stt, tts
    monkeypatch.setattr(tts, "synth_wav", lambda text, voice, speed: b"RIFF" + text.encode())
    monkeypatch.setattr(stt, "transcribe", lambda audio, lang, strict, wake_word="": f"heard {len(audio)} strict={strict}")
    r = client.post("/pmedia/tts", json={"text": "Hello", "voice": "af_sarah"}, headers=H)
    assert r.content == b"RIFFHello" and r.headers["content-type"] == "audio/wav"
    r = client.post("/pmedia/stt", files={"file": ("a.wav", b"12345")}, data={"strict": "1"}, headers=H)
    assert r.json() == {"text": "heard 5 strict=True"}
    assert seen == []                                                          # nothing went to the server


def test_pairing_pins_the_servers_ca_and_saves_the_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RESIDENT_DATA", str(tmp_path))
    import shutil
    if not shutil.which("openssl"):
        pytest.skip("no openssl here")
    from pantheon_web import tls
    tls.ensure(tmp_path / "srv", ["halo.local"])
    pem = tls.ca_pem(tmp_path / "srv").decode()
    assert config.fingerprint(pem) == tls.fingerprint(pem.encode())      # same as the /ca page
    polls = {"n": 0}

    def handle(req: httpx.Request):
        if req.url.path == "/ca.crt":
            return httpx.Response(200, text=pem)
        if req.url.path == "/resident/pair":
            assert json.loads(req.content)["name"] == "Predator"
            return httpx.Response(200, json={"pairing_id": "P", "code": "123456", "expires_in": 600})
        polls["n"] += 1
        return httpx.Response(200, json={"status": "pending"} if polls["n"] < 2 else
                              {"status": "paired", "resident_id": "R", "token": "SECRET"})
    transport = httpx.MockTransport(handle)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "get", lambda url, **kw: real_client(transport=transport).get(url))
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=transport))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.mdns, "resolve", lambda h: "10.0.0.5")
    assert cli.pair("halo.test", "Predator", 7867, 7868, False) == 0
    out = capsys.readouterr().out
    assert "123 456" in out and config.fingerprint(pem) in out and "SECRET" not in out
    cfg = config.load()
    assert cfg["token"] == "SECRET" and cfg["ca_pem"] == pem and cfg["host"] == "halo.test"
    assert (tmp_path / "resident.json").stat().st_mode & 0o777 == 0o600


def test_pair_orpheus_pairs_a_player_and_tells_cantinero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RESIDENT_DATA", str(tmp_path))
    config.save({"host": "halo.test", "token": "TOK", "ca_pem": "", "insecure_http": True, "name": "Predator"})
    told, polls = [], {"n": 0}

    def handle(req: httpx.Request):
        if req.url.host == "halo.test" or req.url.host == "10.0.0.5":
            if req.method == "GET":
                return httpx.Response(200, json={"resident": {"id": "R"}, "account": "eli",
                                                 "orpheus_url": "http://homelab:8096"})
            told.append(json.loads(req.content))
            return httpx.Response(200, json={"resident": {}})
        if req.url.path == "/api/v1/pair":
            body = json.loads(req.content)
            assert body["role"] == "player" and body["name"] == "Predator (Cantinero resident)"
            return httpx.Response(200, json={"pairing_id": "P", "code": "654321"})
        polls["n"] += 1
        return httpx.Response(200, json={"status": "pending"} if polls["n"] < 2 else
                              {"device_id": "dev-9", "token": "OTOKEN"})
    transport = httpx.MockTransport(handle)
    real_client = httpx.Client

    def via(method):
        return lambda url, **kw: real_client(transport=transport).request(method, url, json=kw.get("json"))
    monkeypatch.setattr(httpx, "get", via("GET"))
    monkeypatch.setattr(httpx, "post", via("POST"))
    monkeypatch.setattr(httpx, "put", via("PUT"))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.mdns, "resolve", lambda h: "10.0.0.5")
    assert cli.pair_orpheus("") == 0
    out = capsys.readouterr().out
    assert "pair approve 654321" in out and "OTOKEN" not in out
    cfg = config.load()
    assert (cfg["orpheus_url"], cfg["orpheus_device_id"], cfg["orpheus_token"]) == ("http://homelab:8096", "dev-9", "OTOKEN")
    assert cfg["token"] == "TOK"                                         # the Cantinero pairing is kept
    assert told == [{"orpheus_player_id": "dev-9", "orpheus_player_name": "Predator"}]


def test_static_files_are_revalidated(client):
    # Without it the shell's WebView kept a stale voice.js after an --update.
    r = client.get("/static/js/voice.js")
    assert r.headers["cache-control"] == "no-cache"
    assert client.get("/static/js/voice.js", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_the_tempo_lookup_is_forwarded_with_its_ids(server, client):
    seen, _ = server
    assert client.get("/pmedia/music/tempo?ids=11,12").status_code == 200        # #114
    assert [(m, p, a) for m, p, a, _ in seen] == [("GET", "/pmedia/music/tempo", "Bearer TOK")]
