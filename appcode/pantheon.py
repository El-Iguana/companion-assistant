"""Pantheon — the pytincture page (Ember's UX). Runs in the browser (Pyodide + wapyt).

PHASE 1 — the shell (PYTINCTURE_PLAN.md): the left rail (brand, tile nav, recent chats,
your characters, persona switcher, theme swatches, account), the top bar (search, backend
status, + Create), hash routing between screens, the five colourways, and the mobile
drawer. The screens themselves are placeholders until their phases (ui/views.py).

Conventions (shared with the Pytinc apps):
* BFF calls are ``await X().method()`` for async exports (no ``_async`` companion);
  every result is ``{"ok": ...}``.
* The UI is built in ``load_ui``, never ``__init__``.
* Import *names* from modules, never modules.
* DOM misses are ``JsNull``: falsy, but not ``None``.

The markup is hand-written (escaped) rather than wapyt widgets because Ember's look is the
point of the rewrite; wapyt supplies the context menu, toasts and dialogs.
"""
from __future__ import annotations

import js
from pyodide.ffi import create_proxy

from wapyt import CellConfig, ContextMenu, ContextMenuConfig, LayoutConfig, MainWindow, MenuItem

from bff.shell import Shell
from ui.common import (Listeners, ago, attr, avatar_html, by_id, closest, esc, failed,
                       persona_url, portrait_url, sign_out, spawn, toast)
from ui.views import (ChatsView, ChatView, CreateView, DiscoverView, PersonasView,
                      PresetsView, SettingsView)

APP_ENTRYPOINT = "Pantheon"

TILES = [  # (route, label, icon)
    ("discover", "Discover", "mdi-compass-outline"),
    ("create", "Create", "mdi-plus"),
    ("chats", "Chats", "mdi-forum-outline"),
    ("personas", "Personas", "mdi-account-outline"),
    ("presets", "Presets", "mdi-tune-variant"),
    ("settings", "Settings", "mdi-cog-outline"),
]
SCREENS = {
    "discover": DiscoverView, "create": CreateView, "chats": ChatsView,
    "personas": PersonasView, "presets": PresetsView, "settings": SettingsView,
    "chat": ChatView,
}
THEMES = [("bone", "Bone", "#f5f1e8"), ("ember", "Ember", "#e8a33d"), ("moss", "Moss", "#9ec978"),
          ("dusk", "Dusk", "#b79df0"), ("harbor", "Harbor", "#7fb4d8")]
THEME_KEY = "pantheon.theme"     # per-browser cache only, to paint the right colours before
                                 # the account's own setting arrives; the account is the truth.
STATUS_MS = 30_000

_KEEP: list = []


def _proxy(fn):
    p = create_proxy(fn)
    _KEEP.append(p)            # held for the page's lifetime (timers)
    return p


def _local_get(key: str) -> str:
    try:
        return str(js.window.localStorage.getItem(key) or "")
    except Exception:  # noqa: BLE001 - private windows can refuse storage
        return ""


def _local_set(key: str, value: str) -> None:
    try:
        js.window.localStorage.setItem(key, value)
    except Exception:  # noqa: BLE001
        pass


class Pantheon(MainWindow):
    layout_config = LayoutConfig(rows=[CellConfig(id="app", grow=1)])

    # --- construction ------------------------------------------------------------
    def load_ui(self):
        self.shell: dict = {}
        self.screen = None
        self.route = ("", "")
        self.listeners = Listeners()
        self._sure_id = ""
        self._load_css()
        self.set_theme("dark")
        self.apply_theme(_local_get(THEME_KEY) or "bone")
        js.document.title = "Pantheon"

        self.attach_html("app", self._skeleton())
        root = by_id("pt-app")
        self.listeners.on(root, "click", self._on_click)
        self.listeners.on(by_id("pt-q"), "keydown", self._on_search_key)
        self.listeners.on(js.window, "hashchange", lambda _e: self._route())

        self.persona_menu = ContextMenu(ContextMenuConfig(items=[MenuItem(id="none", label="…")]))
        self.persona_menu.on_select(self._on_persona_pick)

        self._route()
        spawn(self.refresh(), "shell")
        spawn(self.refresh_status(), "status")
        js.setInterval(_proxy(lambda *_: spawn(self.refresh_status(), "status")), STATUS_MS)

    def _load_css(self) -> None:
        if by_id("pt-css"):
            return
        link = js.document.createElement("link")
        link.id = "pt-css"
        link.rel = "stylesheet"
        link.href = "/pstatic/pantheon.css"
        js.document.head.appendChild(link)

    def _skeleton(self) -> str:
        tiles = "".join(
            f'<a class="pt-tile" href="#/{r}" data-tile="{r}"><i class="mdi {i}"></i>{esc(label)}</a>'
            for r, label, i in TILES)
        swatches = "".join(
            f'<button class="pt-swatch" data-theme="{t}" title="{esc(label)}"><i style="background:{dot}"></i></button>'
            for t, label, dot in THEMES)
        return (
            '<div class="pt-app" id="pt-app">'
            '<aside class="pt-side" id="pt-side">'
            '<a class="pt-brand" href="#/discover"><span class="pt-logo">P</span>'
            '<span><b>Pantheon</b><small>characters who remember you</small></span></a>'
            f'<nav class="pt-nav">{tiles}</nav>'
            '<div class="pt-side-scroll">'
            '<h4>Recent chats</h4><div id="pt-recent"></div>'
            '<h4>Your characters</h4><div id="pt-mine"></div>'
            '</div>'
            '<div class="pt-side-foot">'
            '<button class="pt-btn pt-persona-btn" id="pt-persona" title="Who you are in chats">'
            '<span id="pt-persona-ava"></span><span class="pt-pname" id="pt-persona-name">…</span>'
            '<i class="mdi mdi-chevron-up"></i></button>'
            f'<div class="pt-themes"><span id="pt-theme-name">BONE</span>{swatches}</div>'
            '<div class="pt-me"><i class="mdi mdi-account-circle-outline"></i>'
            '<span class="pt-flex"><b id="pt-user">…</b> <span id="pt-role"></span></span>'
            '<button class="pt-btn ghost small" id="pt-signout">Sign out</button></div>'
            '</div>'
            '</aside>'
            '<div class="pt-veil" id="pt-veil"></div>'
            '<main class="pt-main">'
            '<header class="pt-top">'
            '<button class="pt-ibtn pt-burger" id="pt-burger" title="Menu"><i class="mdi mdi-menu"></i></button>'
            '<label class="pt-search"><i class="mdi mdi-magnify"></i>'
            '<input id="pt-q" type="search" autocomplete="off" placeholder="Search characters, tags…"></label>'
            '<span class="pt-spacer"></span>'
            '<span class="pt-status" id="pt-status" title="Backends">'
            '<span class="pt-dot" id="pt-dot-k"></span><span class="pt-dot" id="pt-dot-c"></span>'
            '<span class="pt-status-txt" id="pt-status-txt">checking…</span></span>'
            '<a class="pt-btn primary" href="#/create"><i class="mdi mdi-plus"></i>'
            '<span class="pt-create-txt">Create</span></a>'
            '</header>'
            '<div class="pt-view" id="pt-view"></div>'
            '</main>'
            '</div>')

    # --- theme -------------------------------------------------------------------
    def apply_theme(self, theme: str) -> None:
        names = {t: label for t, label, _ in THEMES}
        theme = theme if theme in names else "bone"
        js.document.documentElement.setAttribute("data-ptheme", theme)
        _local_set(THEME_KEY, theme)
        label = by_id("pt-theme-name")
        if label:
            label.textContent = names[theme].upper()
        for el in js.document.querySelectorAll(".pt-swatch"):
            el.classList.toggle("on", attr(el, "data-theme") == theme)

    async def _set_theme(self, theme: str) -> None:
        self.apply_theme(theme)
        r = await Shell().set_theme(theme)
        failed(r, "save the theme")

    # --- routing -------------------------------------------------------------------
    def go(self, route: str) -> None:
        js.window.location.hash = f"#/{route}"

    def _route(self) -> None:
        raw = str(js.window.location.hash or "").lstrip("#").strip("/")
        name, _, arg = raw.partition("/")
        if name not in SCREENS:
            name, arg = "discover", ""
        js.document.getElementById("pt-app").classList.remove("drawer")
        if (name, arg) == self.route and self.screen is not None:
            return
        if self.screen is not None:
            try:
                self.screen.unmount()
            except Exception:  # noqa: BLE001
                js.console.error(f"[pantheon] unmount {self.route[0]} failed")
        self.route = (name, arg)
        view = by_id("pt-view")
        view.innerHTML = ""
        view.scrollTop = 0
        host = js.document.createElement("div")
        view.appendChild(host)
        self.screen = SCREENS[name](self, arg)
        self.screen.mount(host)
        for el in js.document.querySelectorAll(".pt-tile"):
            el.classList.toggle("on", attr(el, "data-tile") == name)
        self._mark_active_chat()

    # --- data ----------------------------------------------------------------------
    async def refresh(self) -> None:
        r = await Shell().shell()
        if failed(r, "load your library"):
            return
        self.shell = r
        self.apply_theme(r.get("theme") or "bone")
        self._render_rail()
        if self.screen is not None and hasattr(self.screen, "on_shell"):
            self.screen.on_shell(r)
        # The placeholder screens read shell data at mount; re-mount the current one once
        # the first load lands so it is not stuck on the empty pre-load render.
        if self.screen is not None and not getattr(self, "_mounted_with_data", False):
            self._mounted_with_data = True
            self.route = ("", "")
            self._route()

    async def refresh_status(self) -> None:
        r = await Shell().status()
        if not isinstance(r, dict) or not r.get("ok"):
            return
        k, c = r.get("kobold") or {}, r.get("comfy") or {}
        by_id("pt-dot-k").className = "pt-dot " + ("on" if k.get("online") else "off")
        by_id("pt-dot-c").className = "pt-dot " + ("on" if c.get("online") else "off")
        if k.get("online"):
            model = str(k.get("model") or "").split("/", 1)[-1]
            text = model[:28] + ("…" if len(model) > 28 else "")
        else:
            text = "KoboldCPP offline"
        by_id("pt-status-txt").textContent = text
        by_id("pt-status").title = (
            f"KoboldCPP: {'up · ' + str(k.get('model')) if k.get('online') else 'offline'}\n"
            f"ComfyUI: {'up' if c.get('online') else 'offline'}")

    # --- rail ----------------------------------------------------------------------
    def _render_rail(self) -> None:
        s = self.shell
        recent = s.get("recent") or []
        by_id("pt-recent").innerHTML = "".join(self._chat_row(c) for c in recent) or (
            '<div class="pt-empty-side">No chats yet — start one from Discover.</div>')
        chars = s.get("characters") or []
        by_id("pt-mine").innerHTML = "".join(
            f'<a class="pt-citem" href="#/discover" data-char="{esc(c["id"])}">'
            + avatar_html(c["name"], portrait_url(c["id"], c.get("updated_at", "")) if c.get("has_portrait") else "", 34)
            + f'<span class="pt-citem-txt"><span class="pt-citem-name">{esc(c["name"])}</span>'
            f'<span class="pt-citem-sub">{esc(c.get("tagline") or "")}</span></span></a>'
            for c in chars) or '<div class="pt-empty-side">No characters yet — make one with Create.</div>'
        p = s.get("persona") or {}
        by_id("pt-persona-ava").innerHTML = avatar_html(
            p.get("name", "?"), persona_url(p["id"]) if p.get("has_portrait") else "", 28)
        by_id("pt-persona-name").textContent = p.get("name") or "Persona"
        me = s.get("me") or {}
        by_id("pt-user").textContent = me.get("username", "")
        by_id("pt-role").textContent = "· admin" if me.get("role") == "admin" else ""
        self._mark_active_chat()

    def _chat_row(self, c: dict) -> str:
        who = c["character"] + (f" · {c['persona']}" if c.get("persona") else "")
        url = portrait_url(c["character_id"]) if c.get("has_portrait") else ""
        return (
            f'<a class="pt-citem" href="#/chat/{esc(c["id"])}" data-chat="{esc(c["id"])}" '
            f'title="{esc(ago(c.get("updated_at", "")))}">'
            + avatar_html(c["character"], url, 38)
            + f'<span class="pt-citem-txt"><span class="pt-citem-name">{esc(who)}</span>'
            f'<span class="pt-citem-sub">{esc(c.get("preview") or "No messages yet")}</span></span>'
            f'<button class="pt-citem-x" data-del="{esc(c["id"])}" title="Delete this chat">×</button>'
            '</a>')

    def _mark_active_chat(self) -> None:
        active = self.route[1] if self.route[0] == "chat" else ""
        for el in js.document.querySelectorAll("[data-chat]"):
            el.classList.toggle("on", attr(el, "data-chat") == active)

    # --- events --------------------------------------------------------------------
    def _on_click(self, ev) -> None:
        app = by_id("pt-app")
        if closest(ev, "#pt-burger"):
            app.classList.toggle("drawer")
            return
        if closest(ev, "#pt-veil"):
            app.classList.remove("drawer")
            return
        delete = closest(ev, "[data-del]")
        if delete:
            ev.preventDefault()
            ev.stopPropagation()
            self._two_tap_delete(delete)
            return
        swatch = closest(ev, "[data-theme]")
        if swatch:
            spawn(self._set_theme(attr(swatch, "data-theme")), "theme")
            return
        if closest(ev, "#pt-persona"):
            self._open_persona_menu(ev)
            return
        if closest(ev, "#pt-signout"):
            spawn(sign_out(), "sign out")
            return
        if closest(ev, "a[href^='#']"):
            app.classList.remove("drawer")      # every navigation closes the drawer

    def _two_tap_delete(self, button) -> None:
        """Ember's pattern: × turns into a red "Sure?"; a second tap deletes."""
        sid = attr(button, "data-del")
        if self._sure_id != sid:
            for el in js.document.querySelectorAll(".pt-citem-x.sure"):
                el.classList.remove("sure")
                el.textContent = "×"
            self._sure_id = sid
            button.classList.add("sure")
            button.textContent = "Sure?"
            return
        self._sure_id = ""
        spawn(self._delete_chat(sid), "delete chat")

    async def _delete_chat(self, sid: str) -> None:
        r = await Shell().delete_chat(sid)
        if failed(r, "delete the chat"):
            return
        toast("Chat deleted.", "success")
        if self.route == ("chat", sid):
            self.go("discover")
        await self.refresh()

    def _open_persona_menu(self, ev) -> None:
        personas = self.shell.get("personas") or []
        active = (self.shell.get("persona") or {}).get("id")
        items = [MenuItem(id=f"p:{p['id']}",
                          label=p["name"] + (f" — {p['title']}" if p.get("title") else ""),
                          icon="mdi-check" if p["id"] == active else "mdi-account-outline")
                 for p in personas]
        items += [MenuItem(separator=True),
                  MenuItem(id="manage", label="Manage personas…", icon="mdi-cog-outline")]
        self.persona_menu.set_items(items)
        rect = by_id("pt-persona").getBoundingClientRect()
        self.persona_menu.show_at(rect.left, rect.top - 8)

    def _on_persona_pick(self, p) -> None:
        item = (p or {}).get("id") or ""
        if item == "manage":
            self.go("personas")
        elif item.startswith("p:"):
            spawn(self._use_persona(item[2:]), "persona")

    async def _use_persona(self, persona_id: str) -> None:
        r = await Shell().use_persona(persona_id)
        if failed(r, "switch persona"):
            return
        await self.refresh()
        toast(f"You are now {(self.shell.get('persona') or {}).get('name', 'that persona')}.", "success")

    def _on_search_key(self, ev) -> None:
        if ev.key == "Enter":
            # Discover does the filtering (Phase 2); for now searching lands there.
            self.go("discover")
