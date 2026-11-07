"""The character profile modal (Ember's): cover, avatar, actions, then who they are.

Actions, in Ember's order with Pantheon's extras:
- **Continue** — the latest chat, when it has gone past the greeting.
- **Start chat** — a new chat with the greeting chosen in the Greeting section (‹ 1/3 ›),
  as the active persona. ``main.create_session`` does the rest, unchanged.
- **Edit** (the Studio, Phase 4) · **Export** (V2/V3 PNG) · **Copy to user…** (admins: a clone
  into another account's library, ``main.copy_to_user``) · **Delete** (confirmed).
"""
from __future__ import annotations

import js

from wapyt import ContextMenu, ContextMenuConfig, MenuItem, message

from bff.account import Account
from bff.library import Library
from ui.common import Modal, ago, attr, avatar_html, closest, esc, failed, portrait_url, spawn, toast

def _paras(text: str) -> str:
    """Card prose as paragraphs; *asterisk* actions in serif italic, like chat narration."""
    import re

    out = []
    for block in [b.strip() for b in (text or "").split("\n\n") if b.strip()]:
        html = esc(block).replace("\n", "<br>")
        html = re.sub(r"\*([^*\n]+)\*", r'<em class="pt-act">\1</em>', html)
        out.append(f"<p>{html}</p>")
    return "".join(out)


class ProfileModal:
    def __init__(self, app, card_id: str, on_close=None) -> None:
        self.app = app
        self.card_id = card_id
        self.data: dict = {}
        self.greeting = 0
        self.export_menu = None
        self._on_close = on_close
        self.modal = Modal('<div class="pt-prof-loading"><span class="pt-spin"></span></div>',
                           cls="pt-prof", on_close=self._closed)
        self.modal.listeners.on(self.modal.panel, "click", self._on_click)
        spawn(self.load(), "profile")

    def close(self) -> None:
        self.modal.close()

    def _closed(self) -> None:
        """Runs once however the modal closes (button, Esc, backdrop, navigation)."""
        if self.export_menu is not None:
            self.export_menu.destroy()
            self.export_menu = None
        if self._on_close:
            self._on_close()

    async def load(self) -> None:
        r = await Library().profile(self.card_id)
        if not self.modal.open:
            return
        if failed(r, "open the profile"):
            self.close()
            return
        self.data = r
        self.render()

    # --- render ----------------------------------------------------------------
    def render(self) -> None:
        d = self.data
        c, chats = d["card"], d["chats"]
        url = portrait_url(c["id"], c.get("updated_at", "")) if c.get("has_portrait") else ""
        cover = (f'<img class="pt-prof-bg" src="{esc(url)}" alt=""><img class="pt-prof-coverimg" src="{esc(url)}" alt="">'
                 if url else '<div class="pt-prof-bg letter"></div>')
        stats = [f'<span><i class="mdi mdi-forum-outline"></i><b>{chats["count"]}</b> your chats</span>']
        if chats.get("latest_at"):
            stats.append(f'<span>last {esc(ago(chats["latest_at"]))}</span>')
        if c.get("has_vrm"):
            stats.append('<span class="pt-tag">3D avatar</span>')
        elif c.get("sprites"):
            stats.append(f'<span class="pt-tag">{c["sprites"]} expressions</span>')
        if c.get("creator"):
            stats.append(f'<span>by <b>{esc(c["creator"])}</b></span>')

        can_continue = bool(chats.get("resume_id"))
        actions = []
        if can_continue:
            actions.append('<button class="pt-btn primary" data-act="continue">'
                           '<i class="mdi mdi-play"></i>Continue</button>')
        actions.append(f'<button class="pt-btn{"" if can_continue else " primary"}" data-act="start">'
                       '<i class="mdi mdi-message-plus-outline"></i>Start chat</button>')
        actions += [
            f'<a class="pt-btn" href="#/create/{esc(c["id"])}"><i class="mdi mdi-pencil-outline"></i>Edit</a>',
            '<button class="pt-btn" data-act="export"><i class="mdi mdi-export-variant"></i>Export</button>',
        ]
        if ((self.app.shell or {}).get("me") or {}).get("role") == "admin":
            actions.append('<button class="pt-btn" data-act="copyto"><i class="mdi mdi-account-arrow-right-outline"></i>'
                           'Copy to user…</button>')
        actions += [
            '<button class="pt-btn danger" data-act="delete"><i class="mdi mdi-delete-outline"></i>Delete</button>',
            '<button class="pt-btn ghost" data-close="1">Close</button>',
        ]
        tags = "".join(f'<span class="pt-pill static">{esc(t)}</span>' for t in c.get("tags") or [])
        sections = []
        for title, key in (("About", "description"), ("Personality", "personality"), ("Background", "scenario")):
            if (c.get(key) or "").strip():
                sections.append(f'<section class="pt-prof-sec"><h3>{title}</h3>'
                                f'<div class="pt-prose clamp" data-clamp="1">{_paras(c[key])}</div></section>')
        sections.append(self._greeting_html())
        if c.get("starters"):
            chips = "".join(f'<span class="pt-chip">{esc(s)}</span>' for s in c["starters"])
            sections.append(f'<section class="pt-prof-sec"><h3>Try saying</h3><div class="pt-chips">{chips}</div></section>')
        self.modal.set_html(
            f'<div class="pt-prof-cover">{cover}</div>'
            '<button class="pt-ibtn pt-prof-x" data-close="1" title="Close"><i class="mdi mdi-close"></i></button>'
            '<div class="pt-prof-body">'
            '<div class="pt-prof-head">'
            + avatar_html(c["name"], url, 76, "pt-prof-ava")
            + f'<div><h2>{esc(c["name"])}</h2><p>{esc(c.get("tagline") or "")}</p></div></div>'
            f'<div class="pt-prof-stats">{"".join(stats)}</div>'
            f'<div class="pt-prof-actions">{"".join(actions)}</div>'
            + (f'<div class="pt-prof-tags">{tags}</div>' if tags else "")
            + "".join(sections)
            + '</div>')

    def _greeting_html(self) -> str:
        greetings = [g for g in self.data["card"]["greetings"]]
        if not any((g or "").strip() for g in greetings):
            return ""
        i = max(0, min(self.greeting, len(greetings) - 1))
        nav = ""
        if len(greetings) > 1:
            nav = (f'<span class="pt-swipe"><button class="pt-ibtn sm" data-greet="-1" title="Previous greeting">'
                   f'<i class="mdi mdi-chevron-left"></i></button><span>{i + 1}/{len(greetings)}</span>'
                   '<button class="pt-ibtn sm" data-greet="1" title="Next greeting"><i class="mdi mdi-chevron-right"></i></button></span>')
        text = greetings[i] or "(empty — a generic opener is used)"
        return (f'<section class="pt-prof-sec"><h3>Greeting {nav}</h3>'
                f'<div class="pt-greet">{_paras(text)}</div>'
                + ('<p class="pt-dim">Start chat opens with this one.</p>' if len(greetings) > 1 else "")
                + '</section>')

    # --- events ----------------------------------------------------------------
    def _on_click(self, ev) -> None:
        clamp = closest(ev, "[data-clamp]")
        if clamp:
            clamp.classList.toggle("clamp")
        greet = closest(ev, "[data-greet]")
        if greet:
            n = len(self.data["card"]["greetings"])
            self.greeting = (self.greeting + int(attr(greet, "data-greet"))) % n
            sec = greet.closest(".pt-prof-sec")
            sec.outerHTML = self._greeting_html()
            return
        act = closest(ev, "[data-act]")
        if not act:
            return
        which = attr(act, "data-act")
        if which == "continue":
            self.app.go(f"chat/{self.data['chats']['resume_id']}")
        elif which == "start":
            spawn(self._start(), "start chat")
        elif which == "export":
            self._export_menu(act)
        elif which == "copyto":
            CopyCharacterDialog(self.card_id, self.data["card"]["name"])
        elif which == "delete":
            spawn(self._delete(), "delete character")

    async def _start(self) -> None:
        r = await Library().start_chat(self.card_id, self.greeting)
        if failed(r, "start a chat"):
            return
        await self.app.refresh()
        self.app.go(f"chat/{r['session_id']}")

    def _export_menu(self, button) -> None:
        if self.export_menu is None:
            self.export_menu = ContextMenu(ContextMenuConfig(items=[
                MenuItem(id="2", label="PNG card · V2 (SillyTavern, most tools)", icon="mdi-file-image-outline"),
                MenuItem(id="3", label="PNG card · V3", icon="mdi-file-image-outline"),
            ]))
            self.export_menu.on_select(lambda p: self._download((p or {}).get("id") or "2"))
        rect = button.getBoundingClientRect()
        self.export_menu.show_at(rect.left, rect.bottom + 6)

    def _download(self, version: str) -> None:
        a = js.document.createElement("a")
        a.href = f"/pmedia/card/{js.encodeURIComponent(self.card_id)}/export.png?version={version}"
        a.download = ""
        js.document.body.appendChild(a)
        a.click()
        a.remove()

    async def _delete(self) -> None:
        name = self.data["card"]["name"]
        if not await message.confirm(
                f"Delete {name}? Their card and memory are deleted. Chats with "
                "them stay on disk but no longer appear.", title="Remove from the staff", ok_text="Delete",
                danger=True):
            return
        r = await Library().delete_card(self.card_id)
        if failed(r, f"delete {name}"):
            return
        toast(f"{name} deleted.", "success")
        self.close()
        await self.app.refresh()


class CopyCharacterDialog:
    """Admin: pick another account and copy this character into its library."""

    def __init__(self, card_id: str, name: str) -> None:
        self.card_id, self.name = card_id, name
        self.modal = Modal('<div class="pt-prof-loading"><span class="pt-spin"></span></div>', cls="pt-narrow")
        self.modal.listeners.on(self.modal.panel, "click", self._on_click)
        spawn(self.load(), "copy to user")

    async def load(self) -> None:
        r = await Account().users()
        if not self.modal.open:
            return
        if failed(r, "list accounts"):
            self.modal.close()
            return
        others = [u for u in r.get("users") or [] if u["id"] != r.get("me")]
        rows = "".join(
            f'<button class="pt-btn pt-copyto" data-uid="{esc(u["id"])}" data-name="{esc(u["username"])}">'
            f'<i class="mdi mdi-account-circle-outline"></i>{esc(u["username"])}'
            f'<span class="pt-tag">{esc(u["role"])}</span></button>' for u in others)
        self.modal.set_html(
            '<div class="pt-form">'
            f'<h2>Copy {esc(self.name)} <span class="pt-dim">to another user</span></h2>'
            '<p class="pt-dim">They get their own copy: portrait, sprites, voice and VRM included. '
            'Your chats and memory with this character are not copied.</p>'
            + (f'<div class="pt-copyto-list">{rows}</div>' if rows
               else '<p class="pt-dim">There are no other accounts yet — add one in Settings → Users.</p>')
            + '<div class="pt-form-actions"><button class="pt-btn ghost" data-close="1">Cancel</button></div></div>')

    def _on_click(self, ev) -> None:
        btn = closest(ev, "[data-uid]")
        if btn:
            spawn(self._copy(attr(btn, "data-uid"), attr(btn, "data-name")), "copy to user")

    async def _copy(self, uid: str, username: str) -> None:
        for b in self.modal.panel.querySelectorAll("[data-uid]"):
            b.disabled = True
        r = await Account().copy_to_user(uid, [{"kind": "character", "id": self.card_id}])
        if failed(r, f"copy to {username}"):
            for b in self.modal.panel.querySelectorAll("[data-uid]"):
                b.disabled = False
            return
        self.modal.close()
        note = next((c["note"] for c in r.get("copied") or [] if c.get("note")), "")
        toast(f"{self.name} copied to {username}." + (f" {note}." if note else ""),
              "warning" if note else "success")
