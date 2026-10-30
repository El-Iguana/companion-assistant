#!/usr/bin/env python3
"""Pantheon — the server.

    python service.py                 # http://127.0.0.1:7861/

``main`` is Pantheon itself: accounts, data, the KoboldCPP/ComfyUI clients, the memory and
relationship code, and the handlers every feature runs through. The page is a pytincture
app built here (``appcode/``): its BFF classes call ``main``'s handlers directly with the
signed-in user's context, and ``pantheon_web/`` adds the plain routes a JSON BFF cannot
serve (images, uploads, audio, the VRM model, the desktop avatar's page).

**Loopback only, by default.** pytincture signs users in over plain HTTP only on a literal
loopback Host, so the address is ``http://127.0.0.1:7861/``. ``localhost`` is redirected there.

**LAN mode** (``PANTHEON_PUBLIC_ORIGIN``, set by ai-stack's ``stack lan on``): the ai-stack
HTTPS proxy serves Pantheon at that address, and pytincture runs in its production mode for
it: password login only, exact hosts, ``Secure`` cookies, forwarded headers trusted. A page
load on the loopback address is then sent to the HTTPS one, because the cookies and the
origin check only work there. ``/api/health`` stays answerable on loopback for ``stack``.

``main.app`` (the old FastAPI app with its ``/api`` routes) is no longer served; it remains
the home of the handlers and is what the unit tests exercise.

Environment:

| var | default | what |
|---|---|---|
| ``PANTHEON_PORT`` | 7861 | listen port, and the port in the browser's address — publish it on the same number |
| ``PANTHEON_BIND`` | 127.0.0.1 | listen address; the image sets 0.0.0.0 and publishes on 127.0.0.1 only |
| ``PANTHEON_DATA_DIR`` | ./data | a throwaway library for dev/test |
| ``PANTHEON_PUBLIC_ORIGIN`` | (unset) | LAN mode: the proxy's ``https://host:port`` for Pantheon |
"""
from __future__ import annotations

import logging
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

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
PORT = int(os.environ.get("PANTHEON_PORT", "7861"))
BIND = os.environ.get("PANTHEON_BIND", "127.0.0.1")
# The address the browser uses. A literal IP: pytincture refuses "localhost" with
# loopback login, so it is http://127.0.0.1:7861/, never localhost.
LOOPBACK_ORIGIN = f"http://127.0.0.1:{PORT}"
PUBLIC_ORIGIN = os.environ.get("PANTHEON_PUBLIC_ORIGIN", "").strip().rstrip("/")
if PUBLIC_ORIGIN:
    _pub = urlsplit(PUBLIC_ORIGIN)
    if _pub.scheme != "https" or not _pub.hostname or _pub.path or _pub.query:
        raise SystemExit(f"PANTHEON_PUBLIC_ORIGIN must be https://host[:port], got {PUBLIC_ORIGIN!r}")
ORIGIN = PUBLIC_ORIGIN or LOOPBACK_ORIGIN


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


class LocalhostRedirect:
    """``localhost`` -> ``127.0.0.1``, same port and path.

    pytincture's host check only admits the literal IP (its plain-HTTP login is loopback
    only), so a bookmark, a launcher or the desktop shell pointing at localhost would get a
    bare 400. Outermost, so it answers before that check.

    In LAN mode both loopback names go to ``PUBLIC_ORIGIN`` instead: there the session
    cookie is ``Secure`` and the origin check expects the HTTPS address, so signing in on
    ``http://127.0.0.1`` would fail. The health probe is left alone.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
            name, _, port = host.partition(":")
            name = name.lower()
            lan = PUBLIC_ORIGIN and name in ("localhost", "127.0.0.1") \
                and scope.get("path") != "/api/health"
            if lan or name == "localhost":
                path = scope.get("raw_path") or scope.get("path", "/").encode()
                qs = scope.get("query_string") or b""
                base = PUBLIC_ORIGIN if lan else f"http://127.0.0.1:{port or PORT}"
                target = f"{base}{path.decode('latin-1')}" + (
                    f"?{qs.decode('latin-1')}" if qs else "")
                await send({"type": "http.response.start", "status": 307,
                            "headers": [(b"location", target.encode("latin-1")),
                                        (b"content-length", b"0")]})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


def _network_settings() -> dict:
    """pytincture's host/origin/login settings: loopback dev mode, or LAN mode."""
    if not PUBLIC_ORIGIN:
        # Loopback plain-HTTP login only (pytincture's development login).
        return {"enable_dev_email_login": True, "allowed_hosts": ("127.0.0.1",)}
    # Production mode behind the proxy. 127.0.0.1 stays allowed for the health probe and
    # for LocalhostRedirect's answer; it can't sign in there (Secure cookie, origin check).
    return {
        "enable_dev_email_login": False,
        "allowed_hosts": ("127.0.0.1", urlsplit(PUBLIC_ORIGIN).hostname),
        "trusted_proxy_headers": True,
        "session_https_only": True,
    }


def build_web_app():
    application = create_app(PytinctureConfig(
        modules_path=str(APPCODE),
        default_application=APPLICATION,
        enable_user_login=True,
        allow_microphone=True,           # hands-free and push-to-talk (Phase 6)
        session_secret=_session_secret(),
        **_network_settings(),
        canonical_origin=ORIGIN,
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

    application.add_middleware(LocalhostRedirect)        # last added = outermost
    return application


app = build_web_app()


if __name__ == "__main__":
    import uvicorn

    print(f"Pantheon: {ORIGIN}/", flush=True)
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")
