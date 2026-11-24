"""The chat's Memory editor (a modal from the chat ⋮).

One view of the bartender's memory (Memory Phase 7, #16):
* **Standing instructions:** hers, or for every bartender (a toggle per row).
* **What every bartender knows about you:** the shared facts.
* **Where things stand:** her task notes.

Entries written automatically carry an AUTO badge. Rows keep their ids so an edit never
relabels an AUTO fact as yours. Pinned facts per chat are gone: they became shared facts.
"""
from __future__ import annotations

from bff.memory import Memory
from ui.common import Modal, attr, closest, esc, failed, spawn, toast


def _row(kind: str, item: dict, placeholder: str, char: str = "") -> str:
    auto = item.get("origin") == "auto"
    everyone = item.get("scope") == "all"
    scope = ""
    if kind == "directive":
        # Hers, or for every bartender (Memory Phase 7, #16): one click flips it.
        scope = (f'<button class="pt-mbtn pt-scope" data-scope="{"all" if everyone else ""}" type="button" '
                 f'title="Who follows this: click to switch">'
                 f'{"every bartender" if everyone else "only " + esc(char or "her")}</button>')
    return (f'<div class="pt-memrow" data-row="{kind}" data-id="{esc(item.get("id") or "")}">'
            f'<input type="text" value="{esc(item.get("text") or "")}" placeholder="{esc(placeholder)}">'
            + scope
            + ('<span class="pt-tag" title="Written automatically from a chat — edit or remove it">auto</span>' if auto else "")
            + '<button class="pt-mbtn" data-rm="1" title="Remove"><i class="mdi mdi-close"></i></button></div>')


PH = {"fact": "Her sister's name is Nadia",
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
        char = d.get("character") or ""
        if d.get("long") is None:
            body = '<p class="pt-dim">This chat has no bartender, so there is no memory to show.</p>'
        else:
            long = d["long"]
            body = ('<h3>Standing instructions <small>followed in every reply</small></h3>'
                    f'<div class="pt-memlist" data-list="directive">{"".join(_row("directive", x, PH["directive"], char) for x in long.get("directives") or [])}</div>'
                    '<button class="pt-btn small" data-add="directive"><i class="mdi mdi-plus"></i>Add an instruction</button>'
                    '<h3>What every bartender knows about you <small>shared by the whole staff</small></h3>'
                    f'<div class="pt-memlist" data-list="fact">{"".join(_row("fact", x, PH["fact"]) for x in long.get("facts") or [])}</div>'
                    '<button class="pt-btn small" data-add="fact"><i class="mdi mdi-plus"></i>Add a fact</button>'
                    f'<h3>Where things stand with {esc(char or "her")} <small>open threads and decisions, one per line</small></h3>'
                    f'<textarea id="mem-long-text" rows="5" placeholder="Empty is correct when nothing is outstanding.">{esc(chr(10).join(long.get("notes") or []))}</textarea>')
        self.modal.set_html(
            '<div class="pt-form pt-pad pt-memory">'
            f'<h2>Memory <span class="pt-dim">{esc(char)}</span></h2>'
            + body
            + '<div class="pt-form-actions"><button class="pt-btn ghost" data-close="1">Cancel</button>'
            '<button class="pt-btn primary" data-save="1">Save</button></div></div>')

    def _on_click(self, ev) -> None:
        sc = closest(ev, "[data-scope]")
        if sc:
            now_all = attr(sc, "data-scope") != "all"
            sc.setAttribute("data-scope", "all" if now_all else "")
            sc.textContent = "every bartender" if now_all else "only " + (self.data.get("character") or "her")
            return
        add = closest(ev, "[data-add]")
        if add:
            kind = attr(add, "data-add")
            box = self.modal.panel.querySelector(f'[data-list="{kind}"]')
            box.insertAdjacentHTML("beforeend", _row(kind, {}, PH[kind], self.data.get("character") or ""))
            box.lastElementChild.querySelector("input").focus()
            return
        rm = closest(ev, "[data-rm]")
        if rm:
            rm.closest(".pt-memrow").remove()
            return
        if closest(ev, "[data-save]"):
            spawn(self._save(), "save memory")

    def _rows(self, kind: str) -> list:
        out = []
        for row in self.modal.panel.querySelectorAll(f'[data-row="{kind}"]'):
            text = str(row.querySelector("input").value).strip()
            if text:
                item = {"id": attr(row, "data-id"), "text": text}
                sc = row.querySelector("[data-scope]")
                if sc is not None:
                    item["scope"] = attr(sc, "data-scope") or ""
                out.append(item)
        return out

    async def _save(self) -> None:
        if self.data.get("long") is None:
            self.modal.close()
            return
        text = self.modal.panel.querySelector("#mem-long-text")
        long = {"directives": self._rows("directive"), "facts": self._rows("fact"),
                "notes": str(text.value) if text else ""}
        r = await Memory().save(self.sid, long)
        if failed(r, "save the memory"):
            return
        self.modal.close()
        toast("Memory saved.", "success")
