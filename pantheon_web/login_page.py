"""Pantheon's sign-in page: pytincture's login form, relabelled and restyled.

pytincture renders the form itself. Two things have to change, one functional and one
cosmetic:

- **It asks for an email** (``<input type="email" required>``), and a browser refuses to
  submit ``ADMIN`` into that field — Pantheon accounts are usernames. So the field becomes
  ``type="text"``. Without this nobody but an email-shaped account can sign in.
- **It looks like pytincture**, not Pantheon. The page gets Ember's "Bone" look: near-black
  ground, cream accent, the serif brand mark.

Raw ASGI middleware, buffering only paths ending in ``/login`` (pattern from
``~/Development/Pytinc/basilisk_wapyt``). Each rewrite anchors on an exact fragment;
``check_at_startup`` logs loudly if a pytincture upgrade stops emitting one, rather than the
form silently going back to demanding an email.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("pantheon.login")

_HEAD = b"""<style>
  :root{color-scheme:dark;}
  html,body{height:100%;}
  body{margin:0;background:#0e0e12 !important;color:#f5f1e8;
       font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
       display:flex;align-items:center;justify-content:center;
       background-image:radial-gradient(900px 500px at 50% -10%,rgba(245,241,232,.07),transparent 70%) !important;}
  .login-container{background:#17171e !important;border:1px solid rgba(245,241,232,.09) !important;
       border-radius:22px !important;box-shadow:0 20px 60px rgba(0,0,0,.5) !important;
       padding:34px 34px 30px !important;width:min(360px,90vw) !important;max-width:none !important;
       text-align:center;}
  .su-mark{width:52px;height:52px;margin:0 auto 14px;border-radius:14px;background:#f5f1e8;color:#0e0e12;
       display:flex;align-items:center;justify-content:center;font:700 28px Georgia,Charter,serif;}
  .login-container h2{margin:0 0 4px !important;font:700 26px Georgia,Charter,serif !important;color:#f5f1e8 !important;}
  .su-tag{margin:0 0 22px;color:#a39c8f;font-size:11px;letter-spacing:1.6px;text-transform:uppercase;font-weight:700;}
  .input-field{width:100% !important;box-sizing:border-box;background:#141419 !important;color:#f5f1e8 !important;
       border:1px solid rgba(245,241,232,.12) !important;border-radius:12px !important;
       padding:12px 14px !important;font-size:15px !important;margin:0 0 10px !important;}
  .input-field::placeholder{color:#6f6a62;}
  .input-field:focus{outline:none;border-color:#f3ddae !important;box-shadow:0 0 0 3px rgba(245,241,232,.12);}
  .submit-button{width:100% !important;margin-top:6px !important;background:#f5f1e8 !important;color:#0e0e12 !important;
       border:0 !important;border-radius:999px !important;padding:12px 18px !important;
       font-weight:800 !important;font-size:15px !important;cursor:pointer;}
  .submit-button:hover{filter:brightness(1.05);}
  .error,.login-error,.alert{color:#f0a3a3 !important;}
</style>
<title>Pantheon</title>
</head>"""

_REWRITES: tuple[tuple[bytes, bytes], ...] = (
    (b'type="email" name="email"', b'type="text" name="email" autocomplete="username" autofocus'),
    (b'placeholder="Email"', b'placeholder="Username"'),
    (b'value="Login with Email"', b'value="Sign in"'),
    (b"<h2>Welcome</h2>", b'<div class="su-mark">P</div><h2>Pantheon</h2>'),
    (b"<p>Please log in to continue</p>", b'<p class="su-tag">characters who remember you</p>'),
    # Last, so these rules override the page's own stylesheet.
    (b"</head>", _HEAD),
)


def rewrite(body: bytes) -> bytes:
    for old, new in _REWRITES:
        body = body.replace(old, new)
    return body


def missing_fragments(login_html: bytes) -> list[str]:
    return [old.decode("utf-8", "replace") for old, _ in _REWRITES if old not in login_html]


class LoginPageMiddleware:
    """Rewrites only responses for paths ending in /login; everything else passes through."""

    def __init__(self, app, path_suffix: str = "/login") -> None:
        self._app = app
        self._suffix = path_suffix

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").endswith(self._suffix):
            await self._app(scope, receive, send)
            return
        start: dict | None = None
        chunks: list[bytes] = []

        async def capture(message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self._app(scope, receive, capture)
        if start is None:
            return
        # A rewrite changes the length; a stale Content-Length truncates the page.
        headers = [(k, v) for k, v in start.get("headers", [])
                   if k.lower() not in (b"content-length", b"content-encoding")]
        body = b"".join(chunks)
        if any(k.lower() == b"content-type" and b"text/html" in v for k, v in headers):
            body = rewrite(body)
        headers.append((b"content-length", str(len(body)).encode()))
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})


def check_at_startup() -> None:
    """Log loudly if pytincture's login markup no longer matches these rewrites."""
    try:
        import inspect

        from pytincture.backend import app as backend

        source = inspect.getsource(backend.login).encode()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not verify the login markup: %s", exc)
        return
    missing = missing_fragments(source)
    if missing:
        logger.error("login page rewrite is STALE — pytincture no longer emits %s; the form "
                     "will demand an email and reject usernames. Update "
                     "pantheon_web/login_page.py.", ", ".join(map(repr, missing)))
