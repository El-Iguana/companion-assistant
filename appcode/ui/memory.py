"""The chat's Memory editor and Relationship-context editor (modals from the chat ⋮).

Memory has two tabs, as on the classic page:
* **This chat** — pinned facts (always in context) and the rolling "story so far".
* **Long-term** — per character and persona; absent for scenario chats. Its shape follows
  the card: a roleplay character has remembered facts and a history across chats; an
  assistant has standing instructions it obeys, facts it knows, and "where things stand"
  task notes. Facts written automatically carry an AUTO badge. Rows keep their ids so an
  edit never relabels an AUTO fact as yours.
"""
from __future__ import annotations

from bff.memory import Memory
from ui.common import Modal, attr, closest, esc, failed, spawn, toast
from ui.studio import _power_label


def _row(kind: str, item: dict, placeholder: str) -> str:
    auto = item.get("origin") == "auto"
    return (f'<div class="pt-memrow" data-row="{kind}" data-id="{esc(item.get("id") or "")}">'
            f'<input type="text" value="{esc(item.get("text") or "")}" placeholder="{esc(placeholder)}">'
            + ('<span class="pt-tag" title="Written automatically from a chat — edit or remove it">auto</span>' if auto else "")
            + '<button class="pt-mbtn" data-rm="1" title="Remove"><i class="mdi mdi-close"></i></button></div>')


def _moment_row(m: dict) -> str:
    """A moment is reviewed, not edited: shown read-only, with a remove button."""
    said = f'"{m.get("said")}" → ' if m.get("said") else ""
    feel = f' ({m.get("milestone") or m.get("emotion")})' if (m.get("milestone") or m.get("emotion")) else ""
    text = f'{said}{m.get("shown") or ""}{feel}'
    return (f'<div class="pt-memrow" data-row="moment" data-id="{esc(m.get("id") or "")}">'
            f'<input type="text" value="{esc(text)}" readonly title="{esc(text)}">'
            '<span class="pt-tag" title="Recorded automatically when a reply moved the relationship">auto</span>'
            '<button class="pt-mbtn" data-rm="1" title="Remove"><i class="mdi mdi-close"></i></button></div>')


PH = {"pinned": "Lives on the starship Meridian; afraid of deep water",
      "fact": "Her sister's name is Nadia",
      "directive": "Address me as Sir"}


class MemoryModal:
    def __init__(self, session_id: str) -> None:
        self.sid = session_id
        self.data: dict = {}
        self.modal = Modal('<div class="pt-prof-loading"><span class="pt-spin"></span></div>', cls="pt-wide")
        self.modal.listeners.on(self.modal.panel, "click", self._on_click)
        spawn(self.load(), "memory")

    async def load(self) -> None:
        r = await Memory().open(self.sid)
        if not self.modal.open:
            return
        if failed(r, "open the memory"):
            self.modal.close()
            return
        self.data = r
        self.render()

    def render(self) -> None:
        d = self.data
        s = d["session"]
        kind = d.get("long_kind")
        long = d.get("long") or {}
        tabs = ('<div class="pt-tabs"><button class="pt-tab on" data-tab="chat">This chat</button>'
                + ('<button class="pt-tab" data-tab="long">Long-term</button>' if kind else "") + '</div>')
        chat = (
            '<div data-pane="chat">'
            '<h3>Pinned facts <small>always kept in context, never summarized away</small></h3>'
            f'<div class="pt-memlist" data-list="pinned">{"".join(_row("pinned", {"text": t}, PH["pinned"]) for t in s.get("pinned") or [])}</div>'
            '<button class="pt-btn small" data-add="pinned"><i class="mdi mdi-plus"></i>Add a fact</button>'
            '<h3>Story so far <small>the rolling summary of older turns — edit to correct it</small></h3>'
            f'<textarea id="mem-summary" rows="7" placeholder="No summary yet — it builds up as the chat grows.">{esc(s.get("summary") or "")}</textarea>'
            '</div>')
        lt = ""
        if kind == "assistant":
            lt = ('<div data-pane="long" hidden>'
                  '<h3>Standing instructions <small>followed in every reply</small></h3>'
                  f'<div class="pt-memlist" data-list="directive">{"".join(_row("directive", x, PH["directive"]) for x in long.get("directives") or [])}</div>'
                  '<button class="pt-btn small" data-add="directive"><i class="mdi mdi-plus"></i>Add an instruction</button>'
                  '<h3>Things it knows about you</h3>'
                  f'<div class="pt-memlist" data-list="fact">{"".join(_row("fact", x, PH["fact"]) for x in long.get("facts") or [])}</div>'
                  '<button class="pt-btn small" data-add="fact"><i class="mdi mdi-plus"></i>Add a fact</button>'
                  '<h3>Where things stand <small>open threads and decisions, one per line</small></h3>'
                  f'<textarea id="mem-long-text" rows="5" placeholder="Empty is correct when nothing is outstanding.">{esc(chr(10).join(long.get("notes") or []))}</textarea>'
                  '</div>')
        elif kind == "companion":
            lt = ('<div data-pane="long" hidden>'
                  f'<h3>Remembered facts <small>carried into every chat with {esc(d.get("character"))}</small></h3>'
                  f'<div class="pt-memlist" data-list="fact">{"".join(_row("fact", x, PH["fact"]) for x in long.get("facts") or [])}</div>'
                  '<button class="pt-btn small" data-add="fact"><i class="mdi mdi-plus"></i>Add a fact</button>'
                  '<h3>Moments that shaped the relationship <small>why she feels the way she does — remove any that are wrong</small></h3>'
                  f'<div class="pt-memlist" data-list="moment">{"".join(_moment_row(m) for m in long.get("moments") or []) or "<p class=pt-dim>None yet — kept when a reply moves the relationship sharply.</p>"}</div>'
                  '<h3>History across chats <small>written when a chat is folded in — edit to correct it</small></h3>'
                  f'<textarea id="mem-long-text" rows="7" placeholder="Nothing remembered yet — it builds up as you finish conversations.">{esc(long.get("summary") or "")}</textarea>'
                  '</div>')
        self.modal.set_html(
            '<div class="pt-form pt-pad pt-memory">'
            f'<h2>Memory <span class="pt-dim">{esc(d.get("character"))}</span></h2>'
            + tabs + chat + lt
            + '<div class="pt-form-actions"><button class="pt-btn ghost" data-close="1">Cancel</button>'
            '<button class="pt-btn primary" data-save="1">Save</button></div></div>')

    def _on_click(self, ev) -> None:
        tab = closest(ev, "[data-tab]")
        if tab:
            which = attr(tab, "data-tab")
            for el in self.modal.panel.querySelectorAll("[data-tab]"):
                el.classList.toggle("on", attr(el, "data-tab") == which)
            for el in self.modal.panel.querySelectorAll("[data-pane]"):
                el.hidden = attr(el, "data-pane") != which
            return
        add = closest(ev, "[data-add]")
        if add:
            kind = attr(add, "data-add")
            box = self.modal.panel.querySelector(f'[data-list="{kind}"]')
            box.insertAdjacentHTML("beforeend", _row(kind, {}, PH[kind]))
            box.lastElementChild.querySelector("input").focus()
            return
        rm = closest(ev, "[data-rm]")
        if rm:
            rm.closest(".pt-memrow").remove()
            return
        if closest(ev, "[data-save]"):
            spawn(self._save(), "save memory")

    def _rows(self, kind: str, with_ids: bool = True) -> list:
        out = []
        for row in self.modal.panel.querySelectorAll(f'[data-row="{kind}"]'):
            text = str(row.querySelector("input").value).strip()
            if text:
                out.append({"id": attr(row, "data-id"), "text": text} if with_ids else text)
        return out

    async def _save(self) -> None:
        panel = self.modal.panel
        long = None
        kind = self.data.get("long_kind")
        text = panel.querySelector("#mem-long-text")
        if kind == "assistant":
            long = {"directives": self._rows("directive"), "facts": self._rows("fact"),
                    "notes": str(text.value) if text else ""}
        elif kind == "companion":
            long = {"facts": self._rows("fact"), "summary": str(text.value).strip() if text else "",
                    "moments": [attr(r, "data-id") for r in panel.querySelectorAll('[data-row="moment"]')]}
        r = await Memory().save(self.sid, self._rows("pinned", with_ids=False),
                                str(panel.querySelector("#mem-summary").value).strip(), long)
        if failed(r, "save the memory"):
            return
        self.modal.close()
        toast("Memory saved.", "success")


class ContextModal:
    """Power dynamic / social acceptance for this relationship only."""

    def __init__(self, session_id: str, on_saved=None) -> None:
        self.sid = session_id
        self.on_saved = on_saved
        self.modal = Modal('<div class="pt-prof-loading"><span class="pt-spin"></span></div>', cls="pt-narrow")
        self.modal.listeners.on(self.modal.panel, "click", self._on_click)
        self.modal.listeners.on(self.modal.panel, "input", self._on_input)
        spawn(self.load(), "context")

    async def load(self) -> None:
        r = await Memory().context(self.sid)
        if not self.modal.open:
            return
        if failed(r, "open the relationship context"):
            self.modal.close()
            return
        p, s = float(r["power_dynamic"]), float(r["social_acceptance"])
        self.modal.set_html(
            '<div class="pt-form">'
            f'<h2>Relationship context <span class="pt-dim">with {esc(r["character"])}</span></h2>'
            f'<label class="pt-slider"><span>Power dynamic · <b id="cx-p">{esc(_power_label(p))}</b></span>'
            f'<input type="range" id="cx-power" min="-100" max="100" step="5" value="{p:g}"></label>'
            '<p class="pt-dim">◀ you hold power · centre = equal footing · they hold power ▶. Either way slows '
            'intimacy and adds caution — never compliance.</p>'
            f'<label class="pt-slider"><span>Social acceptance · <b id="cx-s">{s:g}</b></span>'
            f'<input type="range" id="cx-social" min="0" max="100" step="5" value="{s:g}"></label>'
            f'<p class="pt-dim">Overrides the character\'s defaults ({_power_label(r["card_power"])}, '
            f'{r["card_social"]:g} acceptance) for this relationship only.'
            + (' <b>Currently overridden.</b>' if r.get("overridden") else "") + '</p>'
            '<div class="pt-form-actions"><button class="pt-btn ghost" data-clear="1">Use card default</button>'
            '<span class="pt-flex"></span><button class="pt-btn ghost" data-close="1">Cancel</button>'
            '<button class="pt-btn primary" data-save="1">Save</button></div></div>')

    def _on_input(self, ev) -> None:
        t = ev.target
        if t.id == "cx-power":
            self.modal.panel.querySelector("#cx-p").textContent = _power_label(float(t.value))
        elif t.id == "cx-social":
            self.modal.panel.querySelector("#cx-s").textContent = str(t.value)

    def _on_click(self, ev) -> None:
        if closest(ev, "[data-save]"):
            spawn(self._save(False), "context")
        elif closest(ev, "[data-clear]"):
            spawn(self._save(True), "context")

    async def _save(self, clear: bool) -> None:
        panel = self.modal.panel
        r = await Memory().set_context(self.sid, float(panel.querySelector("#cx-power").value),
                                       float(panel.querySelector("#cx-social").value), clear)
        if failed(r, "save the context"):
            return
        self.modal.close()
        toast("Back to the character's defaults." if clear else "Relationship context saved.", "success")
        if self.on_saved:
            self.on_saved()
