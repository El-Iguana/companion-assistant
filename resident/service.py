"""The resident service (RESIDENT_PLAN.md R2): the desktop page, local voice, and the rest
forwarded to the Cantinero server.

| here | what |
|---|---|
| ``GET /desktop``, ``/static/*`` | the desktop page, unchanged from the server's |
| ``POST /pmedia/tts``, ``/pmedia/stt`` | local Kokoro/Piper and Whisper: audio never leaves this machine |
| ``/pdesk/*``, a card's PNG and ``avatar.vrm``, ``/pmedia/music/duck`` | forwarded, path unchanged, with the token |
| ``GET /resident/status`` | paired?, server reachable?, for the shell's tray |

It listens on 127.0.0.1 only. A page from that address is a secure context, so the mic
works with no certificate in the WebView. Because any local page could otherwise post to
it, POSTs need a custom header (``X-CSRF-Token``, which desktop.js always sends and a
cross-site form can't), there is no CORS, and the Host must be loopback (no DNS rebinding).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from resident import config, mdns
from resident.relay import RELAY

HERE = Path(__file__).resolve().parent.parent
# The image's healthcheck probes $PANTHEON_PORT, so the resident answers on it too.
PORT = int(os.environ.get("RESIDENT_PORT") or os.environ.get("PANTHEON_PORT") or "7867")
UNREACHABLE = "I can't reach the bar right now. Check that Cantinero is running on the server."
REMOVED = "This resident was removed from Cantinero. Pair it again: python -m resident pair <server>."

# The desktop page's routes the server lets a resident call (R-2); nothing else is forwarded.
FORWARD_GET = ("/pdesk/state", "/pdesk/card")
FORWARD_POST = ("/pdesk/open", "/pdesk/chat", "/pdesk/sweep", "/pdesk/settings")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
async def _music_line():
    import secrets

    cfg = config.load()
    if config.paired(cfg) and not cfg.get("player_key"):
        cfg["player_key"] = secrets.token_urlsafe(24)     # what the shell's player shows us
        config.save(cfg)
    RELAY.start()                          # the command line from the server to the player


@app.websocket("/player/connect")
async def player_connect(ws: WebSocket):
    """The shell's music player (desktop/music.py). Loopback only, with the local key from
    resident.json, so another local program can't pose as the player."""
    import secrets

    host = (ws.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    key = config.load().get("player_key") or ""
    if host not in ("127.0.0.1", "localhost", "::1") or not key or \
            not secrets.compare_digest(ws.headers.get("x-player-key", ""), key):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        await RELAY.attach_player(ws.send_text, ws.receive_text)
    except WebSocketDisconnect:
        pass


@app.on_event("startup")
async def _register_player_soon():
    """Make sure the server knows this resident's music player (R-6), whenever it is up."""
    async def loop():
        while True:
            cfg = config.load()
            if not (config.paired(cfg) and cfg.get("orpheus_device_id")):
                return
            try:
                client, base, _ = upstream.get()
                r = await client.put(base + "/resident/self", json={
                    "orpheus_player_id": cfg["orpheus_device_id"], "orpheus_player_name": cfg.get("name", "")})
                if r.status_code in (200, 401):
                    return
            except (httpx.HTTPError, HTTPException):
                upstream.reset()
            await asyncio.sleep(30)
    asyncio.ensure_future(loop())
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.middleware("http")
async def revalidate_assets(request: Request, call_next):
    # As on the server (service.py): StaticFiles sends an ETag but no Cache-Control, so the
    # shell's WebView kept running the old voice.js after an --update (seen on predator,
    # 2026-11-24). no-cache = always revalidate, a 304 when unchanged.
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# --- the server -----------------------------------------------------------------------------

class Upstream:
    """The paired server: base URL (resolved once, again after a failure) and a pinned client."""

    def __init__(self) -> None:
        self.cfg: dict = {}
        self.client: httpx.AsyncClient | None = None
        self.base = ""

    def reset(self) -> None:
        if self.client is not None:
            asyncio.ensure_future(self.client.aclose())
        self.client, self.base = None, ""

    def get(self) -> tuple[httpx.AsyncClient, str, dict]:
        if self.client is None:
            self.cfg = config.load()
            if not config.paired(self.cfg):
                raise HTTPException(status_code=503, detail="not paired: python -m resident pair <server>")
            addr = mdns.resolve(self.cfg["host"])
            if self.cfg.get("insecure_http"):
                self.base = f"http://{addr}:{self.cfg.get('http_port', 7867)}"
                verify: object = True
            else:
                self.base = f"https://{addr}:{self.cfg.get('https_port', 7868)}"
                verify = config.tls_context(self.cfg["ca_pem"])
            self.client = httpx.AsyncClient(verify=verify, timeout=httpx.Timeout(30.0, connect=5.0),
                                            headers={"Authorization": f"Bearer {self.cfg['token']}"})
        return self.client, self.base, self.cfg


upstream = Upstream()


def _guard(request: Request, post: bool = False) -> None:
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise HTTPException(status_code=403, detail="loopback only")
    if post:
        if "x-csrf-token" not in request.headers:
            raise HTTPException(status_code=403, detail="missing X-CSRF-Token")
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1].rsplit(":", 1)[0] not in ("127.0.0.1", "localhost", "[::1]"):
            raise HTTPException(status_code=403, detail="cross-origin")


def _status_error(r: httpx.Response) -> HTTPException:
    if r.status_code == 401:
        # The page reloads on 401 (a session expiring); a removed resident would loop.
        return HTTPException(status_code=403, detail=REMOVED)
    try:
        detail = r.json().get("detail") or r.text
    except ValueError:
        detail = r.text
    return HTTPException(status_code=r.status_code, detail=str(detail)[:300])


async def _forward(request: Request, path: str, body: bytes | None) -> Response:
    try:
        client, base, _ = upstream.get()
        r = await client.request(request.method, base + path, params=dict(request.query_params),
                                 content=body, headers={"Content-Type": "application/json"} if body else {})
    except httpx.HTTPError:
        upstream.reset()                     # the server may have moved (DHCP): resolve again
        raise HTTPException(status_code=502, detail=UNREACHABLE) from None
    if r.status_code >= 400:
        raise _status_error(r)
    keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "etag", "cache-control")}
    return Response(content=r.content, status_code=r.status_code, headers=keep)


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


async def _say(text: str):
    """A reply in the bartender's own voice when the server can't give one (R-9): the page
    speaks tokens, so this is spoken like anything she says."""
    yield _sse({"token": text})
    yield _sse({"done": True, "message_id": "", "expression": None, "mood": "", "valence": 0})


# --- routes ----------------------------------------------------------------------------------

@app.get("/desktop", include_in_schema=False)
def desktop(request: Request):
    from pantheon_web.desktop import PAGE

    _guard(request)
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})


@app.get("/api/health", include_in_schema=False)
def health():
    return {"ok": True, "role": "resident"}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/desktop")


@app.get("/pantheon/", include_in_schema=False)
def full_window():
    """The page's "open the full window" link: the server's web chat (configuration)."""
    cfg = config.load()
    if not config.paired(cfg):
        raise HTTPException(status_code=503, detail="not paired")
    return RedirectResponse(f"http://{cfg['host']}:{cfg.get('http_port', 7867)}/pantheon/")


@app.get("/resident/status")
async def status(request: Request):
    _guard(request)
    cfg = config.load()
    if not config.paired(cfg):
        return {"paired": False}
    try:
        client, base, _ = upstream.get()
        r = await client.get(base + "/resident/self")
        reachable, me = r.status_code == 200, (r.json() if r.status_code == 200 else {})
        removed = r.status_code == 401
    except httpx.HTTPError:
        upstream.reset()
        reachable, me, removed = False, {}, False
    return {"paired": not removed, "host": cfg["host"], "reachable": reachable,
            "resident": me.get("resident"), "account": me.get("account"),
            "music": {"line": RELAY.connected, "player": RELAY.player is not None,
                      "status": RELAY.player_state.get("status"), "volume": RELAY.player_state.get("volume"),
                      "ducked": RELAY.player_state.get("ducked"),
                      "now": (RELAY.player_state.get("now") or {}).get("title")}}


@app.post("/pdesk/chat")
async def chat(request: Request):
    """The reply stream, passed through as it arrives (the page speaks it sentence by sentence)."""
    _guard(request, post=True)
    body = await request.body()
    try:
        client, base, _ = upstream.get()
        req = client.build_request("POST", base + "/pdesk/chat", content=body,
                                   headers={"Content-Type": "application/json"},
                                   timeout=httpx.Timeout(None, connect=5.0))
        r = await client.send(req, stream=True)
    except httpx.HTTPError:
        upstream.reset()
        return StreamingResponse(_say(UNREACHABLE), media_type="text/event-stream")
    if r.status_code >= 400:
        await r.aclose()
        text = REMOVED if r.status_code == 401 else f"The server refused that ({r.status_code})."
        return StreamingResponse(_say(text), media_type="text/event-stream")

    async def relay():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        except httpx.HTTPError:
            yield _sse({"error": "the connection to the server dropped"})
        finally:
            await r.aclose()
    return StreamingResponse(relay(), media_type="text/event-stream")


@app.api_route("/pdesk/{name}", methods=["GET", "POST"])
async def pdesk(name: str, request: Request):
    path = f"/pdesk/{name}"
    post = request.method == "POST"
    _guard(request, post=post)
    if path not in (FORWARD_POST if post else FORWARD_GET):
        raise HTTPException(status_code=404)
    return await _forward(request, path, await request.body() if post else None)


@app.get("/pmedia/card/{card_id}.png")
async def card_png(card_id: str, request: Request):
    _guard(request)
    return await _forward(request, f"/pmedia/card/{card_id}.png", None)


@app.get("/pmedia/card/{card_id}/avatar.vrm")
async def avatar(card_id: str, request: Request):
    _guard(request)
    return await _forward(request, f"/pmedia/card/{card_id}/avatar.vrm", None)


@app.post("/pmedia/music/duck")
async def duck(request: Request):
    """She is speaking or listening: lower this machine's music, here and at once (R-7)."""
    _guard(request, post=True)
    body = await request.json()
    await RELAY.duck_local(bool(body.get("on")) if isinstance(body, dict) else False)
    return {"ok": True}


@app.post("/pmedia/tts")
async def tts(request: Request):
    """One sentence as WAV, made here (decision 1: audio stays on this machine)."""
    from services import tts as tts_mod

    _guard(request, post=True)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="bad body")
    text = str(body.get("text") or "").strip()[:2000]
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    speed = max(0.5, min(1.5, float(body.get("speed") or 1.0)))
    try:
        wav = await asyncio.to_thread(tts_mod.synth_wav, text, str(body.get("voice") or "af_sarah"), speed)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"tts failed: {e}") from None
    return Response(content=wav, media_type="audio/wav")


@app.websocket("/pmedia/listen")
async def listen_ws(ws: WebSocket):
    """The open microphone, decided here (#6): 16 kHz PCM in, utterances and messages out
    (services/listen.py). Loopback and same-origin only, like the rest of this service."""
    from services import listen

    host = (ws.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    origin = ws.headers.get("origin") or ""
    if host not in ("127.0.0.1", "localhost", "::1") or (
            origin and origin.split("://", 1)[-1].rsplit(":", 1)[0] not in ("127.0.0.1", "localhost", "[::1]")):
        await ws.close(code=4403)
        return
    await ws.accept()
    session = listen.Session(ws.send_json)

    async def ticker():
        while True:
            await asyncio.sleep(2)
            await session.tick()
    tick = asyncio.ensure_future(ticker())
    try:
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                break
            if m.get("bytes"):
                await session.audio(m["bytes"])
            elif m.get("text"):
                try:
                    msg = json.loads(m["text"])
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await session.message(msg)
    except WebSocketDisconnect:
        pass
    finally:
        tick.cancel()
        await session.close()


@app.post("/pmedia/stt")
async def stt(request: Request, file: UploadFile = File(...), strict: str = Form(""),
              wake_word: str = Form("")):
    """An utterance -> {text}, transcribed here. ``wake_word`` is named to Whisper (stt.py)."""
    from services import stt as stt_mod

    _guard(request, post=True)
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="empty audio")
    try:
        text = await asyncio.to_thread(stt_mod.transcribe, audio, None, strict in ("1", "true"),
                                       wake_word[:40])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"stt failed: {e}") from None
    return {"text": text}


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=os.environ.get("RESIDENT_BIND", "127.0.0.1"), port=PORT, log_level="info")
