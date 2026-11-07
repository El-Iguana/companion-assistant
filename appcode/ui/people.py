"""Personas — a library screen with an Ember-style editor in a modal.

Who you are in a chat. A grid of persona cards (portrait, name, role, "active" badge, how
many chats); the editor sets name, role, description, background, portrait and "make
active". The active one is also switched from the rail.
"""
from __future__ import annotations

import js
from pyodide.ffi import create_once_callable

from wapyt import message

from bff.people import People
from ui.common import Listeners, Modal, attr, avatar_html, closest, esc, failed, persona_url, spawn, toast


class PersonasView:
    def __init__(self, app, arg: str = "") -> None:
        self.app = app
        self.root = None
        self.data: dict = {}
        self.listeners = Listeners()
        self.editor = None

    def mount(self, root) -> None:
        self.root = root
        root.innerHTML = '<div class="pt-chat-loading"><span class="pt-spin"></span></div>'
        self.listeners.on(root, "click", self._on_click)
        spawn(self.load(), "personas")

    def unmount(self) -> None:
        self.listeners.clear()
        if self.editor is not None:
            self.editor.modal.close()
        self.root = None

    async def load(self) -> None:
        r = await People().personas()
        if self.root is None or failed(r, "list your personas"):
            return
        self.data = r
        self.stamp = str(js.Date.now())          # personas have no updated_at: bust per load
        self.render()

    def render(self) -> None:
        cards = []
        for p in self.data.get("personas") or []:
            url = persona_url(p["id"]) + f"?v={self.stamp}" if p.get("has_portrait") else ""
            active = p["id"] == self.data.get("active")
            cards.append(
                f'<article class="pt-pcard{" on" if active else ""}" data-edit="{esc(p["id"])}" tabindex="0">'
                + avatar_html(p["name"], url, 72)
                + f'<b>{esc(p["name"])}</b><span>{esc(p.get("title") or "")}</span>'
                f'<small>{p.get("chats", 0)} chat{"s" if p.get("chats", 0) != 1 else ""}</small>'
                + ('<span class="pt-tag">active</span>' if active else
                   f'<button class="pt-btn small" data-use="{esc(p["id"])}">Use</button>')
                + '</article>')
        self.root.innerHTML = (
            '<div class="pt-page"><div class="pt-studio-head"><div><div class="pt-kicker">who you are in a chat</div>'
            '<h1 class="pt-h1">Your <em>personas</em></h1></div>'
            '<div class="pt-studio-headbtns"><button class="pt-btn primary" data-new="1">'
            '<i class="mdi mdi-plus"></i>New persona</button></div></div>'
            '<p class="pt-lead">Characters see the active persona as {{user}}: its name, role and background. '
            'Memory is kept per character <em>and</em> persona.</p>'
            f'<div class="pt-pgrid">{"".join(cards)}</div>'
            + ('' if cards else '<div class="pt-empty">No personas yet. <b>Characters call you “User”</b> until you make one.</div>')
            + '</div>')

    def _on_click(self, ev) -> None:
        use = closest(ev, "[data-use]")
        if use:
            ev.stopPropagation()
            spawn(self._use(attr(use, "data-use")), "use persona")
            return
        if closest(ev, "[data-new]"):
            self.editor = PersonaEditor(self, None)
            return
        card = closest(ev, "[data-edit]")
        if card:
            p = next((x for x in self.data.get("personas") or [] if x["id"] == attr(card, "data-edit")), None)
            if p:
                self.editor = PersonaEditor(self, p)

    async def _use(self, pid: str) -> None:
        from bff.shell import Shell

        r = await Shell().use_persona(pid)
        if failed(r, "switch persona"):
            return
        await self.app.refresh()
        await self.load()


class PersonaEditor:
    def __init__(self, view: PersonasView, p) -> None:
        self.view = view
        self.p = p or {}
        self.portrait = ""
        self.remove = False
        new = not p
        url = persona_url(self.p["id"]) if self.p.get("has_portrait") else ""
        self.modal = Modal(
            '<div class="pt-form pt-pad">'
            f'<h2>{"New persona" if new else "Edit " + esc(self.p.get("name"))}</h2>'
            '<div class="pt-portrait-row"><div id="pe-ava">' + avatar_html(self.p.get("name") or "?", url, 72) + '</div>'
            '<div class="pt-portrait-btns"><label class="pt-btn small"><i class="mdi mdi-image-plus"></i>Choose portrait'
            '<input type="file" id="pe-file" accept="image/*" hidden></label>'
            f'<button class="pt-btn small ghost" data-rmimg="1"{"" if url else " hidden"}>Remove</button></div></div>'
            f'<label>Name<input type="text" id="pe-name" value="{esc(self.p.get("name") or "")}" placeholder="What characters call you"></label>'
            f'<label>Role / title<input type="text" id="pe-title" value="{esc(self.p.get("title") or "")}" placeholder="Ship captain"></label>'
            f'<label>Description<textarea id="pe-desc" rows="3">{esc(self.p.get("description") or "")}</textarea></label>'
            f'<label>Background<textarea id="pe-bg" rows="4">{esc(self.p.get("background") or "")}</textarea></label>'
            f'<label class="pt-check plain"><input type="checkbox" id="pe-default"{" checked" if self.p.get("is_default") or (new and not view.data.get("personas")) else ""}>'
            'Make this the active persona</label>'
            '<div class="pt-form-actions">'
            + ('' if new else '<button class="pt-btn danger" data-delete="1">Delete</button><span class="pt-flex"></span>')
            + '<button class="pt-btn ghost" data-close="1">Cancel</button>'
            '<button class="pt-btn primary" data-save="1">Save</button></div></div>', cls="pt-narrow")
        panel = self.modal.panel
        self.modal.listeners.on(panel, "click", self._on_click)
        self.modal.listeners.on(panel.querySelector("#pe-file"), "change", self._pick)

    def _pick(self, ev) -> None:
        inp = ev.target
        if not inp.files.length:
            return
        reader = js.FileReader.new()

        def loaded(*_):
            self.portrait = str(reader.result)
            self.remove = False
            self.modal.panel.querySelector("#pe-ava").innerHTML = avatar_html("?", self.portrait, 72)
            self.modal.panel.querySelector("[data-rmimg]").hidden = False
        # One-shot: frees itself after the load event (destroying a proxy from inside
        # its own call fails).
        reader.addEventListener("load", create_once_callable(loaded))
        reader.readAsDataURL(inp.files.item(0))

    def _on_click(self, ev) -> None:
        if closest(ev, "[data-rmimg]"):
            self.portrait, self.remove = "", True
            self.modal.panel.querySelector("#pe-ava").innerHTML = avatar_html(self.p.get("name") or "?", "", 72)
            self.modal.panel.querySelector("[data-rmimg]").hidden = True
        elif closest(ev, "[data-save]"):
            spawn(self._save(), "save persona")
        elif closest(ev, "[data-delete]"):
            spawn(self._delete(), "delete persona")

    async def _save(self) -> None:
        q = self.modal.panel.querySelector
        fields = {"name": str(q("#pe-name").value).strip(), "title": str(q("#pe-title").value).strip(),
                  "description": str(q("#pe-desc").value).strip(), "background": str(q("#pe-bg").value).strip(),
                  "is_default": bool(q("#pe-default").checked)}
        r = await People().save_persona(self.p.get("id", ""), fields, self.portrait, self.remove)
        if failed(r, "save the persona"):
            return
        self.modal.close()
        toast(f"{fields['name']} saved.", "success")
        await self.view.app.refresh()
        await self.view.load()

    async def _delete(self) -> None:
        name = self.p.get("name") or "this persona"
        if not await message.confirm(f"Delete {name}? Chats made as {name} keep working, but every "
                                     f"character's memory of {name} is deleted.",
                                     title="Delete persona",
                                     ok_text="Delete", danger=True):
            return
        r = await People().delete_persona(self.p["id"])
        if failed(r, "delete the persona"):
            return
        self.modal.close()
        await self.view.app.refresh()
        await self.view.load()
