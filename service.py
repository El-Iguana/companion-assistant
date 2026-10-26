#!/usr/bin/env python3
"""Pantheon — the process that serves both pages during the pytincture transition.

    python service.py                 # both: classic page :7861, new page :7866
    PANTHEON_SERVE=web python service.py      # only the new page (host-side dev)

**One process, two servers** (PYTINCTURE_PLAN.md). ``main`` is Pantheon itself — accounts,
data, KoboldCPP/ComfyUI clients, every route handler. The classic page is ``main.app``,
served exactly as before. The new page is a pytincture app built here, whose BFF classes
(``appcode/bff/``) call ``main``'s handlers directly. Same process, so both pages share one
copy of Pantheon's state: caches, the embeddings model, the idle-sweep timers.

They are separate servers, not one app, because the new page's login is loopback-only
(pytincture refuses a non-loopback Host when it signs users in over plain HTTP) and its
stricter headers would otherwise apply to the classic page and cut its LAN access.

Environment:

| var | default | what |
|---|---|---|
| ``PANTHEON_SERVE`` | ``both`` | ``both`` · ``web`` (new page only) · ``classic`` |
| ``PANTHEON_WEB_PORT`` | 7866 | the new page's port |
| ``PANTHEON_WEB_BIND`` | 127.0.0.1 | its listen address; the image sets 0.0.0.0 and publishes on 127.0.0.1 only |
| ``PANTHEON_CLASSIC_PORT`` | 7861 | the classic page's port |
| ``PANTHEON_CLASSIC_BIND`` | 0.0.0.0 | its listen address (as it always was; compose's host-network variant narrows it) |
| ``PANTHEON_DATA_DIR`` | ./data | a throwaway library for dev/test |
"""
from __future__ import annotations

import asyncio
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
from pantheon_web.media import router as media_router  # noqa: E402

logging.basicConfig(level=logging.INFO)

APPLICATION = "pantheon"
WEB_PORT = int(os.environ.get("PANTHEON_WEB_PORT", "7866"))
WEB_BIND = os.environ.get("PANTHEON_WEB_BIND", "127.0.0.1")
CLASSIC_PORT = int(os.environ.get("PANTHEON_CLASSIC_PORT", "7861"))
CLASSIC_BIND = os.environ.get("PANTHEON_CLASSIC_BIND", "0.0.0.0")
# The address the browser uses. A literal IP: pytincture refuses "localhost" with
# loopback login, so the new page is http://127.0.0.1:7866/, never localhost.
WEB_ORIGIN = f"http://127.0.0.1:{WEB_PORT}"


def _session_secret() -> str:
    """pytincture's cookie key, kept beside Pantheon's own signing secret."""
    f = main.AUTH / "pytincture.key"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        f.chmod(0o600)
    return f.read_text().strip()


def build_web_app():
    application = create_app(PytinctureConfig(
        modules_path=str(APPCODE),
        default_application=APPLICATION,
        enable_user_login=True,
        # Loopback plain-HTTP login only. Off-box access is a later phase, behind TLS.
        enable_dev_email_login=True,
        session_secret=_session_secret(),
        allowed_hosts=("127.0.0.1",),
        canonical_origin=WEB_ORIGIN,
        cookie_namespace="pantheon",
        environment={
            # Pantheon's own accounts and scrypt hashes (pantheon_web/auth.py).
            "AUTH_USER_AUTHENTICATOR": "pantheon_web.auth.authenticate",
            "AUTH_SESSION_CLAIM_KEYS": "user_id,username,role",
        },
    ))
    check_at_startup()
    application.add_middleware(LoginPageMiddleware)
    application.include_router(media_router)
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
    return application


app = build_web_app()


async def _serve(which: str) -> None:
    import uvicorn

    servers = []
    if which in ("both", "web"):
        servers.append(uvicorn.Server(uvicorn.Config(app, host=WEB_BIND, port=WEB_PORT,
                                                     log_level="info")))
        print(f"Pantheon (new page): {WEB_ORIGIN}/", flush=True)
    if which in ("both", "classic"):
        servers.append(uvicorn.Server(uvicorn.Config(main.app, host=CLASSIC_BIND, port=CLASSIC_PORT,
                                                     log_level="info")))
        print(f"Pantheon (classic page): http://localhost:{CLASSIC_PORT}/", flush=True)
    if not servers:
        raise SystemExit(f"PANTHEON_SERVE must be both, web or classic — not {which!r}")
    # Each Server installs signal handlers; with two, let the first own them and the
    # second follow its exit.
    for extra in servers[1:]:
        extra.install_signal_handlers = lambda: None
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for s in servers:
        s.should_exit = True
    await asyncio.gather(*pending, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(_serve(os.environ.get("PANTHEON_SERVE", "both")))
