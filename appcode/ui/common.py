"""Browser-side helpers shared by every screen. Runs in Pyodide only.

Import *names* from here (``from ui.common import spawn``), never the module: pytincture's
packager follows only that form, and the other boots a blank page.
"""
from __future__ import annotations

import asyncio
import html
import traceback

import js
from pyodide.ffi import create_proxy

from wapyt import message

APPLICATION = "pantheon"


def spawn(coro, label: str):
    """Run a coroutine detached, but never silently — ensure_future swallows errors."""

    async def _guarded():
        try:
            await coro
        except Exception:  # noqa: BLE001 - last line of defence
            js.console.error(f"[pantheon] {label} failed:\n{traceback.format_exc()}")

    return asyncio.ensure_future(_guarded())


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def toast(text: str, kind: str = "info", timeout_ms: int = 4000) -> None:
    message.toast(text, kind=kind, timeout_ms=timeout_ms)


def failed(result, what: str) -> bool:
    """True (and a toast) when a BFF result is an error: ``if failed(r, "x"): return``.
    A signed-out answer goes back to the login page instead."""
    if isinstance(result, dict) and result.get("ok"):
        return False
    if isinstance(result, dict) and result.get("signed_out"):
        js.window.location.assign(f"/{APPLICATION}/login")
        return True
    error = result.get("error") if isinstance(result, dict) else None
    toast(f"Could not {what}: {error}" if error else f"Could not {what}.", "error", 7000)
    return True


def by_id(element_id: str):
    """``getElementById``; a miss is ``JsNull`` (falsy, but not ``None``)."""
    return js.document.getElementById(element_id)


def attr(element, name: str) -> str:
    """``getAttribute`` as a str ('' when missing). ``dataset`` raises in Pyodide."""
    if not element:
        return ""
    value = element.getAttribute(name)
    return str(value) if value else ""


def closest(event, selector: str):
    target = event.target
    return target.closest(selector) if target and hasattr(target, "closest") else None


def portrait_url(card_id: str, version: str = "") -> str:
    return f"/pmedia/card/{js.encodeURIComponent(card_id)}.png" + (
        f"?v={js.encodeURIComponent(version)}" if version else "")


def persona_url(persona_id: str) -> str:
    return f"/pmedia/persona/{js.encodeURIComponent(persona_id)}.png"


def avatar_html(name: str, url: str = "", size: int = 38, cls: str = "") -> str:
    """A round avatar: the image when there is one, else the name's initial on the
    accent — Ember's fallback, so a card without art still has a face in the rail."""
    initial = esc((name or "?").strip()[:1].upper() or "?")
    style = f"width:{size}px;height:{size}px;font-size:{max(12, size // 2.4):.0f}px"
    if url:
        return (f'<span class="pt-ava {cls}" style="{style}">'
                f'<img src="{esc(url)}" alt="" loading="lazy"></span>')
    return f'<span class="pt-ava pt-ava-letter {cls}" style="{style}">{initial}</span>'


def csrf_token() -> str:
    for part in str(js.document.cookie).split(";"):
        name, _, value = part.strip().partition("=")
        if "csrf" in name.lower() and APPLICATION in name.lower():
            return str(js.decodeURIComponent(value))
    for part in str(js.document.cookie).split(";"):
        name, _, value = part.strip().partition("=")
        if "csrf" in name.lower():
            return str(js.decodeURIComponent(value))
    return ""


async def sign_out() -> None:
    """pytincture's logout is a POST that needs the CSRF header, hence a fetch."""
    options = js.Object.new()
    options.method = "POST"
    options.credentials = "same-origin"
    headers = js.Object.new()
    setattr(headers, "X-CSRF-Token", csrf_token())
    options.headers = headers
    try:
        await js.fetch(f"/{APPLICATION}/auth/logout", options)
    except Exception:  # noqa: BLE001
        pass
    js.window.location.assign(f"/{APPLICATION}/login")


def ago(iso: str) -> str:
    """'just now' / '5 min' / '3 h' / 'Oct 12' from an ISO timestamp."""
    if not iso:
        return ""
    try:
        then = js.Date.new(iso).getTime()
    except Exception:  # noqa: BLE001
        return ""
    s = max(0, int((js.Date.now() - then) / 1000))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h"
    return str(js.Date.new(iso).toLocaleDateString(js.undefined, js.JSON.parse('{"month":"short","day":"numeric"}')))


class Listeners:
    """Owns DOM listeners and their proxies, so a screen can drop them all at once."""

    def __init__(self) -> None:
        self._items: list = []

    def on(self, target, name: str, handler) -> None:
        if not target:
            return
        proxy = create_proxy(handler)
        target.addEventListener(name, proxy)
        self._items.append((target, name, proxy))

    def clear(self) -> None:
        for target, name, proxy in self._items:
            try:
                target.removeEventListener(name, proxy)
                proxy.destroy()
            except Exception:  # noqa: BLE001
                pass
        self._items = []
