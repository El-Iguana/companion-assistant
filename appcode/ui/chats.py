"""Chats — every conversation, searchable (the rail shows only the 30 most recent).

Rows are Ember's chat rows at page width: avatar, "Character · persona", the last line,
when, how long; a two-tap × deletes. The top-bar search filters by character, persona
or the last line.
"""
from __future__ import annotations

from bff.shell import Shell
from ui.common import Listeners, ago, attr, avatar_html, closest, esc, failed, portrait_url, spawn, toast


class ChatsView:
    def __init__(self, app, arg: str = "") -> None:
        self.app = app
        self.root = None
        self.chats: list = []
        self.sure = ""
        self.listeners = Listeners()

    def mount(self, root) -> None:
        self.root = root
        root.innerHTML = ('<div class="pt-page"><div class="pt-kicker">all conversations</div>'
                          '<h1 class="pt-h1">Your <em>chats</em></h1>'
                          '<p class="pt-lead" id="cl-lead">Loading…</p><div class="pt-chatlist" id="cl-list"></div></div>')
        self.listeners.on(root, "click", self._on_click)
        spawn(self.load(), "chats")

    def unmount(self) -> None:
        self.listeners.clear()
        self.root = None

    def on_query(self, _q: str) -> None:
        self.render()

    def on_shell(self, _data) -> None:
        spawn(self.load(), "chats")

    async def load(self) -> None:
        r = await Shell().all_chats()
        if self.root is None or failed(r, "list your chats"):
            return
        self.chats = r.get("chats") or []
        self.render()

    def render(self) -> None:
        if self.root is None:
            return
        q = (self.app.query or "").strip().lower()
        shown = [c for c in self.chats
                 if not q or all(w in f'{c["character"]} {c["persona"]} {c["preview"]}'.lower() for w in q.split())]
        lead = self.root.querySelector("#cl-lead")
        lead.textContent = (f"{len(shown)} of {len(self.chats)} match “{self.app.query.strip()}”." if q else
                            f"{len(self.chats)} conversation{'s' if len(self.chats) != 1 else ''}, newest first.")
        rows = []
        for c in shown:
            url = portrait_url(c["character_id"]) if c.get("has_portrait") else ""
            who = c["character"] + (f" · {c['persona']}" if c.get("persona") else "")
            sure = self.sure == c["id"]
            rows.append(
                f'<a class="pt-crow" href="#/chat/{esc(c["id"])}">'
                + avatar_html(c["character"], url, 44)
                + f'<span class="pt-crow-txt"><span class="pt-crow-name">{esc(who)}</span>'
                f'<span class="pt-crow-sub">{esc(c.get("preview") or "No messages yet")}</span></span>'
                f'<span class="pt-crow-meta">{esc(ago(c.get("updated_at", "")))}<br>'
                f'{c["messages"]} message{"s" if c["messages"] != 1 else ""}</span>'
                f'<button class="pt-citem-x{" sure" if sure else ""}" data-del="{esc(c["id"])}" '
                f'title="Delete this chat">{"Sure?" if sure else "×"}</button></a>')
        self.root.querySelector("#cl-list").innerHTML = "".join(rows) or (
            '<div class="pt-empty"><div class="pt-empty-ico"><i class="mdi mdi-forum-outline"></i></div>'
            + ("Nothing matches." if q else 'No chats yet — <a href="#/discover">pick someone from the Staff</a>.')
            + '</div>')

    def _on_click(self, ev) -> None:
        d = closest(ev, "[data-del]")
        if not d:
            return
        ev.preventDefault()
        ev.stopPropagation()
        sid = attr(d, "data-del")
        if self.sure != sid:
            self.sure = sid
            self.render()
            return
        self.sure = ""
        spawn(self._delete(sid), "delete chat")

    async def _delete(self, sid: str) -> None:
        r = await Shell().delete_chat(sid)
        if failed(r, "delete the chat"):
            return
        toast("Chat deleted.", "success")
        self.chats = [c for c in self.chats if c["id"] != sid]
        self.render()
        await self.app.refresh()
