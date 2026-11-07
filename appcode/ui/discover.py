"""Discover — the home screen: Ember's hero, a "pick up where you left off" rail, tag
pills, and the 3:4 portrait grid of your characters.

Clicking a card opens its profile (ui/profile.py), as on Ember — a card is never a
one-click chat, because which greeting or persona to start with is the
decision the profile is for. The corner pill is the shortcut: **Continue →** the latest
chat, or **Chat →** a fresh one for someone you have not talked to yet.

The top bar's search filters this grid live (name, tagline, tags). ``#/character/<id>``
opens a profile directly; the rail's "Your characters" links use it.
"""
from __future__ import annotations

import random

import js

from bff.library import Library
from ui.common import Listeners, ago, attr, closest, esc, failed, portrait_url, spawn
from ui.profile import ProfileModal


def _card_html(c: dict) -> str:
    art = (f'<img src="{esc(portrait_url(c["id"], c.get("updated_at", "")))}" alt="" loading="lazy">'
           if c.get("has_portrait") else
           f'<span class="pt-card-letter">{esc((c["name"] or "?")[:1].upper())}</span>')
    pill = ("Continue →", "continue") if c.get("latest_chat") else ("Chat →", "new")
    meta = [f'{c["chats"]} chat{"s" if c["chats"] != 1 else ""}' if c.get("chats") else "new"]
    if c.get("tags"):
        meta.append(f'<span class="pt-tag">{esc(c["tags"][0])}</span>')
    return (
        f'<article class="pt-card" data-open="{esc(c["id"])}" tabindex="0" '
        f'aria-label="{esc(c["name"])}">'
        f'<div class="pt-card-art">{art}</div>'
        f'<button class="pt-card-pill" data-quick="{esc(c["id"])}" data-kind="{pill[1]}">{pill[0]}</button>'
        '<div class="pt-card-foot">'
        f'<b>{esc(c["name"])}</b>'
        f'<span class="pt-card-tag">{esc(c.get("tagline") or "")}</span>'
        f'<span class="pt-card-meta">{" · ".join(meta)}</span>'
        '</div></article>')


class DiscoverView:
    def __init__(self, app, arg: str = "") -> None:
        self.app = app
        self.open_id = arg
        self.tag = ""
        self.listeners = Listeners()
        self.root = None
        self.modal = None

    # --- mount -----------------------------------------------------------------
    def mount(self, root) -> None:
        self.root = root
        self.listeners.on(root, "click", self._on_click)
        self.listeners.on(root, "keydown", self._on_key)
        self.render()
        if self.open_id and self.app.shell:
            self.open_profile(self.open_id)

    def unmount(self) -> None:
        self.listeners.clear()
        if self.modal is not None:
            self.modal.close()

    def on_shell(self, _data) -> None:
        self.render()
        if self.open_id and self.modal is None:
            self.open_profile(self.open_id)

    def on_query(self, _query: str) -> None:
        self._render_grid()

    # --- render ----------------------------------------------------------------
    def render(self) -> None:
        shell = self.app.shell or {}
        chars = shell.get("characters") or []
        recent = shell.get("recent") or []
        if not shell:
            self.root.innerHTML = '<div class="pt-page"><div class="pt-skel-hero"></div></div>'
            return
        if not chars:
            self.root.innerHTML = (
                '<div class="pt-page"><section class="pt-hero">'
                '<div class="pt-kicker">No staff yet</div>'
                '<h1 class="pt-h1 big">Pull up a stool. <em>What\'ll it be?</em></h1>'
                '<p class="pt-lead">No staff yet — hire your first bartender: write one in the '
                'studio, or import a V2/V3 card from SillyTavern or Chub.</p>'
                '<div class="pt-hero-btns"><a class="pt-btn primary" href="#/create">'
                '<i class="mdi mdi-account-plus-outline"></i>Hire</a></div></section></div>')
            return
        n_chats = sum(c.get("chats", 0) for c in chars)
        on_shift = next((c for c in chars if c["id"] == shell.get("on_shift")), None)
        tags: dict[str, int] = {}
        for c in chars:
            for t in c.get("tags") or []:
                tags[t] = tags.get(t, 0) + 1
        top_tags = sorted(tags, key=lambda t: (-tags[t], t.lower()))[:12]
        pills = "".join(
            f'<button class="pt-pill{" on" if self.tag == t else ""}" data-tag="{esc(t)}">{esc(t or "All")}</button>'
            for t in ["", *top_tags])
        self.root.innerHTML = (
            '<div class="pt-page">'
            '<section class="pt-hero">'
            + (f'<div class="pt-kicker">On shift: {esc(on_shift["name"])}</div>' if on_shift else "")
            + '<h1 class="pt-h1 big">Pull up a stool. <em>What\'ll it be?</em></h1>'
            '<p class="pt-lead">Say the name of whoever is on shift, or just start talking. They '
            'keep your standing orders and pick up where you left off.</p>'
            '<div class="pt-hero-btns">'
            '<button class="pt-btn primary" data-act="browse"><i class="mdi mdi-glass-cocktail"></i>See the staff</button>'
            '<a class="pt-btn" href="#/create"><i class="mdi mdi-account-plus-outline"></i>Hire</a>'
            '<button class="pt-btn" data-act="surprise"><i class="mdi mdi-shuffle-variant"></i>Surprise me</button>'
            '</div>'
            '<div class="pt-hero-stats">'
            f'<span><b>{len(chars)}</b> on staff</span><span><b>{n_chats}</b> conversations</span>'
            f'<span><b>{len(shell.get("personas") or [])}</b> personas</span>'
            '</div></section>'
            + self._continue_rail(recent)
            + '<div class="pt-section-h" id="dc-cast"><h2>The staff</h2>'
            f'<span id="dc-count"></span></div>'
            f'<div class="pt-pills">{pills}</div>'
            '<div class="pt-grid" id="dc-grid"></div>'
            '</div>')
        self._render_grid()

    def _continue_rail(self, recent: list) -> str:
        seen, tiles = set(), []
        for c in recent:
            if c["character_id"] in seen:
                continue
            seen.add(c["character_id"])
            art = (f'<img src="{esc(portrait_url(c["character_id"]))}" alt="" loading="lazy">'
                   if c.get("has_portrait") else
                   f'<span class="pt-card-letter">{esc(c["character"][:1].upper())}</span>')
            tiles.append(
                f'<a class="pt-rtile" href="#/chat/{esc(c["id"])}">'
                f'<span class="pt-rtile-art">{art}</span>'
                f'<span class="pt-rtile-txt"><b>{esc(c["character"])}</b>'
                f'<span>{esc(ago(c.get("updated_at", "")))} · {esc(c.get("preview") or "")}</span></span>'
                '<span class="pt-card-pill static">Continue →</span></a>')
            if len(tiles) == 6:
                break
        if not tiles:
            return ""
        return ('<div class="pt-section-h"><h2>Pick up where you left off</h2></div>'
                f'<div class="pt-rail">{"".join(tiles)}</div>')

    def _render_grid(self) -> None:
        grid = self.root.querySelector("#dc-grid") if self.root else None
        if not grid:
            return
        chars = (self.app.shell or {}).get("characters") or []
        q = (self.app.query or "").strip().lower()

        def match(c: dict) -> bool:
            if self.tag and self.tag not in (c.get("tags") or []):
                return False
            if not q:
                return True
            hay = " ".join([c["name"], c.get("tagline") or "", *(c.get("tags") or [])]).lower()
            return all(word in hay for word in q.split())

        shown = [c for c in chars if match(c)]
        count = self.root.querySelector("#dc-count")
        if count:
            count.textContent = (f"{len(shown)} of {len(chars)}" if len(shown) != len(chars)
                                 else f"{len(chars)} on staff")
        grid.innerHTML = "".join(_card_html(c) for c in shown) or (
            '<div class="pt-empty" style="grid-column:1/-1">'
            '<div class="pt-empty-ico"><i class="mdi mdi-magnify"></i></div>'
            'Nobody on the staff matches. Try another search — or <a href="#/create">hire someone</a>.</div>')

    # --- events ----------------------------------------------------------------
    def _on_click(self, ev) -> None:
        quick = closest(ev, "[data-quick]")
        if quick:
            ev.stopPropagation()
            spawn(self._quick(attr(quick, "data-quick")), "quick chat")
            return
        tag = closest(ev, "[data-tag]")
        if tag:
            self.tag = attr(tag, "data-tag")
            for el in self.root.querySelectorAll(".pt-pill"):
                el.classList.toggle("on", attr(el, "data-tag") == self.tag)
            self._render_grid()
            return
        act = closest(ev, "[data-act]")
        if act:
            which = attr(act, "data-act")
            if which == "browse":
                cast = self.root.querySelector("#dc-cast")
                if cast:
                    cast.scrollIntoView(js.JSON.parse('{"behavior":"smooth","block":"start"}'))
            elif which == "surprise":
                chars = (self.app.shell or {}).get("characters") or []
                if chars:
                    self.app.go(f"character/{random.choice(chars)['id']}")
            return
        card = closest(ev, "[data-open]")
        if card:
            self.app.go(f"character/{attr(card, 'data-open')}")

    def _on_key(self, ev) -> None:
        if ev.key in ("Enter", " "):
            card = closest(ev, "[data-open]")
            if card and ev.target == card:
                ev.preventDefault()
                self.app.go(f"character/{attr(card, 'data-open')}")

    async def _quick(self, card_id: str) -> None:
        c = next((c for c in (self.app.shell or {}).get("characters") or [] if c["id"] == card_id), None)
        if c and c.get("latest_chat"):
            self.app.go(f"chat/{c['latest_chat']}")
            return
        r = await Library().start_chat(card_id)
        if failed(r, "start a chat"):
            return
        await self.app.refresh()
        self.app.go(f"chat/{r['session_id']}")

    # --- profile ---------------------------------------------------------------
    def open_profile(self, card_id: str) -> None:
        if self.modal is not None and self.modal.card_id == card_id:
            return
        if self.modal is not None:
            self.modal.close()

        def closed() -> None:
            self.modal = None
            self.open_id = ""
            # Closing the profile returns the URL to plain Discover, so a reload does
            # not reopen it — unless the close was itself a navigation elsewhere (a
            # Start chat moves the hash to #/chat/… before this runs, so read the URL,
            # not the app's route, which still names the profile at that moment).
            if str(js.window.location.hash).startswith("#/character/"):
                self.app.go("discover")

        self.open_id = card_id
        self.modal = ProfileModal(self.app, card_id, on_close=closed)
