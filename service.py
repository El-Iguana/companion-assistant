#!/usr/bin/env python3
"""Pantheon assistant — the server.

    python service.py                 # http://127.0.0.1:7867/

``main`` is Pantheon itself: accounts, data, the KoboldCPP/ComfyUI clients, the assistant
memory, and the handlers every feature runs through. The page is a pytincture
app built here (``appcode/``): its BFF classes call ``main``'s handlers directly with the
signed-in user's context, and ``pantheon_web/`` adds the plain routes a JSON BFF cannot
serve (images, uploads, audio, the VRM model, the desktop avatar's page).

**Any device on the LAN, plain HTTP.** Pantheon is a local product for whoever installs it,
so it answers on every interface at ``http://<host>:7867/``. pytincture's
``allow_development_auth_origin`` mode does exactly that (origin taken from the request,
non-``Secure`` cookies, no host list) but refuses any client that isn't loopback, so
``LanAsLoopback`` tells it every client is. ``localhost`` is redirected to ``127.0.0.1``.
Browsers allow the microphone only on ``localhost``/``127.0.0.1`` or HTTPS, so voice input
works on this computer, not from other devices over plain HTTP.

``main.app`` (the old FastAPI app with its ``/api`` routes) is no longer served; it remains
the home of the handlers and is what the unit tests exercise.

Environment:

| var | default | what |
|---|---|---|
| ``PANTHEON_PORT`` | 7867 | listen port, and the port in the browser's address — publish it on the same number |
| ``PANTHEON_BIND`` | 0.0.0.0 | listen address |
| ``PANTHEON_DATA_DIR`` | ./data | a throwaway library for dev/test |
"""
from __future__ import annotations

import logging
import os
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APPCODE = HERE / "appcode"
# main.py, services/, models/ and pantheon_web/ import from the repo root; BFF modules are
# loaded by pytincture from appcode/ and import `bff.*` / `ui.*` names from there.
for p in (str(HERE), str(APPCODE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.staticfiles import StaticFiles  # noqa: E402
from pytincture import PytinctureConfig, create_app  # noqa: E402

import main  # noqa: E402  — Pantheon itself
from pantheon_web.login_page import LoginPageMiddleware, check_at_startup  # noqa: E402
from pantheon_web.desktop import router as desktop_router  # noqa: E402
from pantheon_web.media import router as media_router  # noqa: E402

logging.basicConfig(level=logging.INFO)

APPLICATION = "pantheon"
PORT = int(os.environ.get("PANTHEON_PORT", "7867"))
BIND = os.environ.get("PANTHEON_BIND", "0.0.0.0")
ORIGIN = f"http://127.0.0.1:{PORT}"   # what the startup line prints; any host name works


def _session_secret() -> str:
    """pytincture's cookie key, kept beside Pantheon's own signing secret."""
    f = main.AUTH / "pytincture.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip()


class BlobImagesMiddleware:
    """Let the page load images it made itself.

    three.js's GLTFLoader turns a .vrm's embedded textures into ``blob:`` URLs and loads
    them (Chrome: ``fetch`` + ImageBitmap; others: ``<img>``). pytincture's CSP allows
    ``blob:`` for scripts and workers but not ``img-src``/``connect-src``, and has no knob
    for it, so every texture failed and the model rendered untextured. A blob URL can only
    be minted by this page, so adding it opens no other origin.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def patched(message):
            if message["type"] == "http.response.start":
                headers = []
                for k, v in message.get("headers", []):
                    if k.lower() == b"content-security-policy":
                        v = (v.replace(b"img-src 'self'", b"img-src 'self' blob:")
                              .replace(b"connect-src 'self'", b"connect-src 'self' blob:"))
                    headers.append((k, v))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, patched)


class LanAsLoopback:
    """Present every client to pytincture as 127.0.0.1 (the LAN spike).

    ``allow_development_auth_origin`` is the pytincture mode that signs users in over plain
    HTTP on whatever address the browser used, but a middleware of its own answers 403 to
    any non-loopback client. Pantheon is never exposed beyond the home network, so the
    check is answered here. Outermost, ahead of that middleware.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope.get("client"):
            scope = dict(scope, client=("127.0.0.1", scope["client"][1]))
        await self.app(scope, receive, send)


class LocalhostRedirect:
    """``localhost`` -> ``127.0.0.1``, same port and path.

    Sessions are per host name, so one name for this computer keeps one sign-in (it was
    required when pytincture's host check admitted only the literal IP).
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
            name, _, port = host.partition(":")
            if name.lower() == "localhost":
                path = scope.get("raw_path") or scope.get("path", "/").encode()
                qs = scope.get("query_string") or b""
                target = f"http://127.0.0.1:{port or PORT}{path.decode('latin-1')}" + (
                    f"?{qs.decode('latin-1')}" if qs else "")
                await send({"type": "http.response.start", "status": 307,
                            "headers": [(b"location", target.encode("latin-1")),
                                        (b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


def build_web_app():
    application = create_app(PytinctureConfig(
        modules_path=str(APPCODE),
        default_application=APPLICATION,
        enable_user_login=True,
        # Password login over plain HTTP on any address (see LanAsLoopback).
        allow_development_auth_origin=True,
        session_https_only=False,
        allow_microphone=True,           # hands-free and push-to-talk (Phase 6)
        session_secret=_session_secret(),
        cookie_namespace="pantheon",
        environment={
            # Pantheon's own accounts and scrypt hashes (pantheon_web/auth.py).
            "AUTH_USER_AUTHENTICATOR": "pantheon_web.auth.authenticate",
            "AUTH_SESSION_CLAIM_KEYS": "user_id,username,role",
        },
    ))
    check_at_startup()
    application.add_middleware(LoginPageMiddleware)
    application.add_middleware(BlobImagesMiddleware)
    application.include_router(media_router)
    # Ahead of pytincture's own routes: its "/{application}" catch-all would otherwise
    # read /desktop as an app called "desktop" and send it to /desktop/login.
    before = len(application.router.routes)
    application.include_router(desktop_router)
    added = application.router.routes[before:]
    del application.router.routes[before:]
    application.router.routes[0:0] = added

    @application.get("/api/health", include_in_schema=False)
    def health():
        # Public and cheap: the stack launcher and compose healthchecks probe it. It says
        # the server is up, not that KoboldCPP is (that shows on the page).
        return {"ok": True}

    # The new page's own assets, and Pantheon's static tree (three.js, the VRM island).
    application.mount("/pstatic", StaticFiles(directory=str(APPCODE / "static")), name="pstatic")
    application.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @application.middleware("http")
    async def revalidate_assets(request, call_next):
        # StaticFiles sends an ETag but no Cache-Control, so a browser may reuse a stale
        # stylesheet after a rebuild without asking. no-cache = always revalidate (a 304
        # when unchanged), so a new image's CSS shows on the next load.
        response = await call_next(request)
        if request.url.path.startswith(("/pstatic/", "/static/")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # main's clients (KoboldCPP, ComfyUI) were closed by main.app's shutdown hook when it was
    # the served app; it no longer is.
    from contextlib import asynccontextmanager

    base_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_):
        async with base_lifespan(app_) as state:
            yield state
        await main._shutdown()

    application.router.lifespan_context = lifespan

    application.add_middleware(LocalhostRedirect)
    application.add_middleware(LanAsLoopback)            # last added = outermost
    return application


app = build_web_app()


if __name__ == "__main__":
    import uvicorn

    print(f"Cantinero: {ORIGIN}/", flush=True)
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")
