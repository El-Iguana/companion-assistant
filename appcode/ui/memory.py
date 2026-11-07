"""The chat's Memory editor (a modal from the chat ⋮).

Two tabs:
* **This chat** — pinned facts (always in context).
* **Long-term** — per character and persona: standing instructions it obeys, facts it
  knows, and "where things stand" task notes. Entries written automatically carry an
  AUTO badge. Rows keep their ids so an edit never relabels an AUTO fact as yours.
"""
from __future__ import annotations

from bff.memory import Memory
from ui.common import Modal, attr, closest, esc, failed, spawn, toast


def _row(kind: str, item: dict, placeholder: str) -> str:
    auto = item.get("origin") == "auto"
    return (f'<div class="pt-memrow" data-row="{kind}" data-id="{esc(item.get("id") or "")}">'
            f'<input type="text" value="{esc(item.get("text") or "")}" placeholder="{esc(placeholder)}">'
            + ('<span class="pt-tag" title="Written automatically from a chat — edit or remove it">auto</span>' if auto else "")
            + '<button class="pt-mbtn" data-rm="1" title="Remove"><i class="mdi mdi-close"></i></button></div>')


PH = {"pinned": "We are planning the trip to Lisbon in May",
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
        has_long = d.get("long") is not None
        long = d.get("long") or {}
        tabs = ('<div class="pt-tabs"><button class="pt-tab on" data-tab="chat">This chat</button>'
                + ('<button class="pt-tab" data-tab="long">Long-term</button>' if has_long else "") + '</div>')
        chat = (
            '<div data-pane="chat">'
            '<h3>Pinned facts <small>always kept in context for this chat</small></h3>'
            f'<div class="pt-memlist" data-list="pinned">{"".join(_row("pinned", {"text": t}, PH["pinned"]) for t in s.get("pinned") or [])}</div>'
            '<button class="pt-btn small" data-add="pinned"><i class="mdi mdi-plus"></i>Add a fact</button>'
            '</div>')
        lt = ""
        if has_long:
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
        text = panel.querySelector("#mem-long-text")
        if self.data.get("long") is not None:
            long = {"directives": self._rows("directive"), "facts": self._rows("fact"),
                    "notes": str(text.value) if text else ""}
        r = await Memory().save(self.sid, self._rows("pinned", with_ids=False), long)
        if failed(r, "save the memory"):
            return
        self.modal.close()
        toast("Memory saved.", "success")
