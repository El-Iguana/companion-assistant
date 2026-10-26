"""Screens behind the rail's tiles. PHASE 1: routing and empty states.

Each later phase replaces one placeholder with the real screen (PYTINCTURE_PLAN.md):
Discover + the profile modal in Phase 2, the chat view in 3, the Studio (Create) in 4,
Personas / Presets / Settings in 5. Until a screen exists here, its empty state says so and,
where the old page can do the job, links to it — nothing becomes unreachable meanwhile.

A screen is a class with ``mount(root)`` and ``unmount()``; ``on_shell(data)`` (optional)
receives the rail's data whenever it refreshes.
"""
from __future__ import annotations

import js

from ui.common import esc


def classic_url(fragment: str = "") -> str:
    """The old page. Same host, port 7861 (the new page is 7866 during the transition)."""
    return f"{js.window.location.protocol}//{js.window.location.hostname}:7861/{fragment}"


class Placeholder:
    icon = "mdi-hammer-wrench"
    title = ""
    lead = ""
    phase = ""
    classic = True

    def __init__(self, app, arg: str = "") -> None:
        self.app = app
        self.arg = arg

    def body(self) -> str:
        return ""

    def mount(self, root) -> None:
        classic = (f'<a class="pt-btn" href="{esc(classic_url())}" target="_blank" rel="noopener">'
                   '<i class="mdi mdi-open-in-new"></i>Open the classic page</a>') if self.classic else ""
        root.innerHTML = (
            '<div class="pt-page">'
            f'<div class="pt-kicker">{esc(self.phase)}</div>'
            f'<h1 class="pt-h1">{self.title}</h1>'
            f'<p class="pt-lead">{esc(self.lead)}</p>'
            f'{self.body()}'
            '<div class="pt-empty">'
            f'<div class="pt-empty-ico"><i class="mdi {self.icon}"></i></div>'
            f'<b>Not built yet.</b> This screen arrives in {esc(self.phase.lower())}.'
            f'<div>{classic}</div></div>'
            '</div>')

    def unmount(self) -> None:
        pass


class DiscoverView(Placeholder):
    icon = "mdi-compass-outline"
    title = "Pick someone. <em>They'll remember you.</em>"
    lead = ("Your characters, as a gallery of portraits — tap one for their profile, then start "
            "or continue a chat.")
    phase = "Phase 2"

    def body(self) -> str:
        chars = (self.app.shell or {}).get("characters") or []
        if not chars:
            return ""
        return ('<div class="pt-section-h"><h2>Your cast</h2>'
                f'<span>{len(chars)} character{"s" if len(chars) != 1 else ""}</span></div>')

    def on_shell(self, _data) -> None:
        pass


class CreateView(Placeholder):
    icon = "mdi-account-plus-outline"
    title = "Character <em>studio</em>"
    lead = ("Every card field in one place — personality, greetings, lorebook, expressions, "
            "voice, avatar — with a live preview beside the form.")
    phase = "Phase 4"


class ChatsView(Placeholder):
    icon = "mdi-forum-outline"
    title = "All <em>chats</em>"
    lead = "Every conversation, searchable. The rail shows the 30 most recent."
    phase = "Phase 3"


class PersonasView(Placeholder):
    icon = "mdi-account-outline"
    title = "Your <em>personas</em>"
    lead = ("Who you are in a chat: name, role, background and portrait. The active one is "
            "switched from the bottom of the rail.")
    phase = "Phase 5"


class PresetsView(Placeholder):
    icon = "mdi-tune-variant"
    title = "Relationship <em>presets</em>"
    lead = "Reusable starting points for scenario chats: the relationship's numbers and a note."
    phase = "Phase 5"


class SettingsView(Placeholder):
    icon = "mdi-cog-outline"
    title = "Settings"
    lead = "Voice and hands-free, memory and recall, thinking, and accounts."
    phase = "Phase 5"


class ChatView(Placeholder):
    icon = "mdi-message-text-outline"
    title = "Chat"
    lead = "The Ember-style chat view comes next, in Phase 3."
    phase = "Phase 3"

    def mount(self, root) -> None:
        chat = next((c for c in (self.app.shell or {}).get("recent") or []
                     if c["id"] == self.arg), None)
        if chat:
            self.title = f"{esc(chat['character'])} <em>· {esc(chat['persona'] or 'you')}</em>"
            self.lead = chat.get("preview") or "No messages yet."
        super().mount(root)
