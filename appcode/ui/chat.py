"""The chat view (Ember's): a centred column over the character's blurred portrait.

Behaviour is Pantheon's, layout is Ember's:

* **Bubbles.** The character's replies sit left with a small avatar and a
  "Name · time · feeling" label. Yours sit right, accent-filled, with no avatar.
  ``*actions*`` render in serif italic. Day dividers separate days.
* **Per message, on hover** (always visible on touch). The last reply gets swipes
  ‹ 2/3 ›, where › past the end makes another take, exactly as the classic ▶ did.
  Every message gets one ⋯ menu.
* **Edits happen inline in the bubble.** Your own message can be "Saved & resent":
  edit, drop everything after it, regenerate. That is ``main``'s edit + truncate +
  the ordinary regenerate path.
* **Streaming.** "{Name} is thinking" dots, then the text with a cursor. Send turns
  into **Stop**. As on the classic page, a stopped turn is not saved server-side, so
  Stop puts your text back in the box instead of letting it vanish.
* **Around it:** the expression portrait docked beside the column on wide screens
  (it follows the running mood and live ``[expr:x]`` beats), persona and memory count
  in the header, starter chips until you have said something, a "↓ Jump to latest"
  pill, and the idle fold into long-term memory 5 minutes after a reply (the app owns
  that timer, so leaving the screen does not cancel it).

Voice and the 3D avatar are the JavaScript island ``static/js/voice.js`` (audio and frame
rate work Pyodide's FFI handles poorly). This view drives it — the card, each streamed
token, ``[expr:x]`` beats, whether a reply is streaming — and listens for its
``pvoice:*`` events: a transcript to send, a barge-in abort, a beat to show now, button
state. The header's speaker reads replies aloud; the composer has push-to-talk (hold)
and hands-free (toggle); each reply's ⋯ has "Read aloud". The Memory editor is in
ui/memory.py.
"""
from __future__ import annotations

import asyncio
import json
import re

import js

from wapyt import ContextMenu, ContextMenuConfig, MenuItem, message

from bff.chat import Chat
from ui.common import (Listeners, attr, avatar_html, by_id, closest, esc, failed,
                       persona_url, portrait_url, spawn, toast)
_DAY = 86_400_000


def _proxy_once(fn):
    from pyodide.ffi import create_once_callable

    return create_once_callable(fn)


def _scroll_to(el_id: str) -> None:
    el = js.document.getElementById(el_id)
    if el:
        el.scrollIntoView(js.JSON.parse('{"behavior":"smooth","block":"start"}'))


def voice():
    """The voice island, or None while static/js/voice.js is still loading."""
    v = getattr(js.window, "PantheonVoice", None)
    return v if v else None


def _js(obj):
    return js.JSON.parse(json.dumps(obj))


# Sound tags in replies written for the removed Orpheus voices (<laugh> …). Older chats
# still contain them, so they stay hidden.
_VOICE_TAG = re.compile(r"[ \t]*<(?:laugh|chuckle|sigh|gasp|cough|sniffle|groan|yawn)>")


def md(text: str) -> str:
    """Escape, then Pantheon's light markup: *actions*, **bold**, line breaks."""
    html = esc(_VOICE_TAG.sub("", text or ""))
    html = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*([^*\n]+)\*", r'<em class="pt-act">\1</em>', html)
    paras = [p.replace("\n", "<br>") for p in re.split(r"\n{2,}", html.strip())]
    return "".join(f"<p>{p}</p>" for p in paras if p) or "<p></p>"


def _when(iso: str) -> tuple[str, str]:
    """(day key, "4:13 PM") from an ISO timestamp, in local time."""
    try:
        d = js.Date.new(iso)
        if js.isNaN(d.getTime()):
            return "", ""
        return (str(d.toDateString()),
                str(d.toLocaleTimeString(js.undefined, js.JSON.parse('{"hour":"numeric","minute":"2-digit"}'))))
    except Exception:  # noqa: BLE001
        return "", ""


def _day_label(day_key: str) -> str:
    today = js.Date.new()
    if day_key == str(today.toDateString()):
        return "Today"
    if day_key == str(js.Date.new(today.getTime() - _DAY).toDateString()):
        return "Yesterday"
    d = js.Date.new(day_key)
    return str(d.toLocaleDateString(js.undefined, js.JSON.parse('{"month":"short","day":"numeric"}')))


class ChatView:
    def __init__(self, app, session_id: str = "") -> None:
        self.app = app
        self.sid = session_id
        self.data: dict = {}
        self.listeners = Listeners()
        self.root = None
        self.streaming = False
        self.task = None
        self.stopped = False
        self.editing = ""                   # message id being edited inline
        self.menu = ContextMenu(ContextMenuConfig(items=[MenuItem(id="x", label="…")]))
        self.menu.on_select(self._on_menu)
        self._menu_mid = ""

    # --- lifecycle -------------------------------------------------------------
    def mount(self, root) -> None:
        self.root = root
        root.innerHTML = '<div class="pt-chat-loading"><span class="pt-spin"></span></div>'
        self.listeners.on(root, "click", self._on_click)
        w = js.window
        self.listeners.on(w, "pvoice:ready", lambda _e: self._attach_voice())
        self.listeners.on(w, "pvoice:send", self._on_voice_send)
        self.listeners.on(w, "pvoice:abort", lambda _e: self.stop())
        self.listeners.on(w, "pvoice:state", lambda e: self._paint_voice(e.detail))
        self.listeners.on(w, "pvoice:persist", self._on_voice_persist)
        self.listeners.on(w, "pvoice:toast", lambda e: toast(str(e.detail.msg), str(e.detail.kind or "info"), 6000))
        # The chat ends when the hands-free conversation does, or with the page:
        # save it to long-term memory then (app.end_chat), as on leaving it (unmount).
        self.listeners.on(w, "pvoice:asleep", lambda _e: self.app.end_chat(self.sid))
        self.listeners.on(w, "pagehide", lambda _e: self.app.end_chat(self.sid, leaving=True))
        spawn(self.load(), "open chat")

    def unmount(self) -> None:
        # A reply still streaming is left to finish: stopping it would discard the turn
        # (see the module note). Its updates land on detached elements, harmlessly, and
        # the rail refreshes when it completes.
        self.listeners.clear()
        self.menu.destroy()
        self.root = None
        # Leaving a chat (to another chat, a new one, any other screen) ends it.
        self.app.end_chat(self.sid)
        v = voice()
        if v:
            v.detach()

    async def load(self) -> None:
        r = await Chat().open(self.sid)
        if self.root is None:
            return
        if not isinstance(r, dict) or not r.get("ok"):
            if isinstance(r, dict) and r.get("signed_out"):
                failed(r, "open the chat")
                return
            self.root.innerHTML = (
                '<div class="pt-page"><div class="pt-empty"><div class="pt-empty-ico">'
                '<i class="mdi mdi-message-off-outline"></i></div>'
                f'{esc((r or {}).get("error") or "Could not open that chat.")}'
                '<div><a class="pt-btn" href="#/discover">Back to the staff</a></div></div></div>')
            return
        self.data = r
        self.render()

    # --- render ----------------------------------------------------------------
    def render(self) -> None:
        d = self.data
        card = d["card"]
        portrait = portrait_url(card["id"], card.get("updated_at", "")) if card.get("has_portrait") else ""
        bg = f'<div class="pt-chat-bg" style="background-image:url(&quot;{esc(portrait)}&quot;)"></div>' if portrait else ""
        # The stage: a column of its own beside the chat for the 3D avatar, so the
        # messages never run underneath it. Only for cards that have one.
        stage = bool(card.get("has_vrm"))
        self.root.innerHTML = (
            f'<div class="pt-chat{" has-stage" if stage else ""}">'
            + bg
            + '<div class="pt-chatwrap">'
            '<header class="pt-chead">'
            '<button class="pt-ibtn pt-chead-burger" data-burger="1" title="Menu"><i class="mdi mdi-menu"></i></button>'
            f'<a class="pt-chead-who" href="#/character/{esc(card["id"])}" title="Profile">'
            + avatar_html(card["name"], portrait, 48, "pt-chead-ava")
            + f'<span class="pt-chead-txt"><b>{esc(card["name"])}</b>'
            '<span class="pt-chead-sub" id="ch-sub"></span></span></a>'
            '<button class="pt-ibtn" id="ch-speaker" data-voice="speaker" title="Read replies aloud">'
            '<i class="mdi mdi-volume-off"></i></button>'
            '<button class="pt-ibtn" data-menu="head" title="Chat options"><i class="mdi mdi-dots-vertical"></i></button>'
            '</header>'
            '<div class="pt-msgs" id="ch-msgs"></div>'
            '<button class="pt-jump" id="ch-jump" hidden><i class="mdi mdi-arrow-down"></i>Jump to latest</button>'
            '<div class="pt-inputbar">'
            '<div class="pt-sugs" id="ch-sugs"></div>'
            + '<div class="pt-inrow">'
            '<button class="pt-ibtn" data-menu="input" title="More options"><i class="mdi mdi-menu"></i></button>'
            f'<textarea id="ch-input" rows="1" placeholder="Message {esc(card["name"])}…" '
            'aria-label="Message"></textarea>'
            '<button class="pt-ibtn pt-vbtn" id="ch-hf" data-voice="hf" title="Hands-free: listen for my voice">'
            '<i class="mdi mdi-ear-hearing"></i></button>'
            '<button class="pt-ibtn pt-vbtn" id="ch-ptt" title="Hold to talk"><i class="mdi mdi-microphone"></i></button>'
            '<button class="pt-send" id="ch-send" title="Send"><i class="mdi mdi-send"></i></button>'
            '</div>'
            f'<div class="pt-hint">{self._hint()}</div>'
            '</div></div>'
            '<aside class="pt-stage" id="ch-stage"' + ("" if stage else " hidden") + '>'
            '<div class="pt-avatar-slot" id="ch-avatar-slot"></div>'
            '</aside></div>')
        inp = by_id("ch-input")
        self.listeners.on(inp, "keydown", self._on_key)
        self.listeners.on(inp, "input", lambda _e: self._grow())
        self.listeners.on(by_id("ch-msgs"), "scroll", lambda _e: self._sync_jump())
        self._render_header()
        self._render_messages()
        self._render_sugs()
        ptt = by_id("ch-ptt")
        for name in ("pointerdown", "pointerup", "pointerleave", "pointercancel"):
            self.listeners.on(ptt, name, self._on_ptt)
        self._attach_voice()
        inp.focus()

    def _rerollable(self) -> int:
        """Index of the reply "Another take" re-rolls, or -1."""
        msgs = self.data["session"]["messages"]
        if len(msgs) < 2:
            return -1
        return max((i for i, m in enumerate(msgs) if m["role"] == "char"), default=-1)

    def _hint(self) -> str:
        name = esc(self.data["card"]["name"])
        tail = f"{name} keeps notes and standing instructions across chats."
        return f"Enter to send · Shift+Enter for a new line · {tail}"

    def _render_header(self) -> None:
        d = self.data
        bits = [f'as {d["persona"]["name"]}']
        if d.get("memories"):
            bits.append(f'{d["memories"]} memor{"ies" if d["memories"] != 1 else "y"}')
        sub = by_id("ch-sub")
        if sub:
            sub.textContent = " · ".join(b for b in bits if b)

    def _render_messages(self) -> None:
        box = by_id("ch-msgs")
        if not box:
            return
        msgs = self.data["session"]["messages"]
        last_char = self._rerollable()
        out, day = [], ""
        for i, m in enumerate(msgs):
            if m["role"] not in ("char", "user"):      # "narrator": roleplay-app sessions only
                continue
            key, _t = _when(m.get("timestamp", ""))
            if key and key != day:
                day = key
                out.append(f'<div class="pt-day"><span>{esc(_day_label(key))}</span></div>')
            out.append(self._msg_html(m, i, i == last_char))
        box.innerHTML = "".join(out)
        self._scroll_bottom()

    def _msg_html(self, m: dict, i: int, swipeable: bool) -> str:
        card, persona = self.data["card"], self.data["persona"]
        char = m["role"] == "char"
        _key, time = _when(m.get("timestamp", ""))
        content = m["content"]
        if char and m.get("swipes"):
            content = m["swipes"][max(0, min(m.get("swipe_index", 0), len(m["swipes"]) - 1))]
        if char:
            head = esc(card["name"]) + (f" · {esc(time)}" if time else "")
        else:
            head = esc(time) if time else esc(persona["name"])
        acts = []
        if swipeable:
            n = max(1, len(m.get("swipes") or []))
            idx = m.get("swipe_index", 0)
            acts.append(
                '<span class="pt-swipe">'
                f'<button class="pt-mbtn" data-swipe="-1" title="Previous take"{" disabled" if idx <= 0 else ""}>'
                '<i class="mdi mdi-chevron-left"></i></button>'
                f'<span>{idx + 1}/{n}</span>'
                '<button class="pt-mbtn" data-swipe="1" title="'
                + ("Another take" if idx >= n - 1 else "Next take")
                + '"><i class="mdi mdi-chevron-right"></i></button></span>')
        acts.append(f'<button class="pt-mbtn" data-mmenu="{esc(m["id"])}" title="Message actions">'
                    '<i class="mdi mdi-dots-horizontal"></i></button>')
        if self.editing == m["id"]:
            body = self._editor_html(m, content)
        else:
            body = f'<div class="pt-bubble">{md(content)}</div>'
        ava = ""
        if char:
            url = portrait_url(card["id"], card.get("updated_at", "")) if card.get("has_portrait") else ""
            ava = avatar_html(card["name"], url, 34, "pt-msg-ava")
        return (f'<div class="pt-msg {"char" if char else "user"}" data-mid="{esc(m["id"])}" data-i="{i}">'
                f'{ava}<div class="pt-msg-col"><div class="pt-msg-head">{head}</div>{body}'
                f'<div class="pt-macts">{"".join(acts)}</div></div></div>')

    def _editor_html(self, m: dict, content: str) -> str:
        mine = m["role"] == "user"
        caption = ("Editing your message — Save & resend picks the conversation up from here." if mine
                   else f"Editing {esc(self.data['card']['name'])}'s reply.")
        buttons = ('<button class="pt-btn small ghost" data-edit="cancel">Cancel</button>'
                   + ('<button class="pt-btn small" data-edit="save">Save</button>'
                      '<button class="pt-btn small primary" data-edit="resend">Save &amp; resend</button>'
                      if mine else '<button class="pt-btn small primary" data-edit="save">Save</button>'))
        return (f'<div class="pt-editor"><div class="pt-dim">{caption}</div>'
                f'<textarea id="ch-edit">{esc(content)}</textarea>'
                f'<div class="pt-editor-btns">{buttons}</div></div>')

    def _render_sugs(self) -> None:
        box = by_id("ch-sugs")
        if not box:
            return
        said = any(m["role"] == "user" for m in self.data["session"]["messages"])
        starters = [] if said else self.data["card"].get("starters") or []
        box.innerHTML = "".join(f'<button class="pt-sug" data-sug="{esc(s)}">{esc(s)}</button>'
                                for s in starters)
        box.hidden = not starters

    # --- scrolling ---------------------------------------------------------------
    def _near_bottom(self) -> bool:
        box = by_id("ch-msgs")
        return bool(box) and box.scrollHeight - box.scrollTop - box.clientHeight < 160

    def _scroll_bottom(self) -> None:
        box = by_id("ch-msgs")
        if box:
            box.scrollTop = box.scrollHeight
        self._sync_jump()

    def _sync_jump(self) -> None:
        jump = by_id("ch-jump")
        if jump:
            jump.hidden = self._near_bottom()

    def _grow(self) -> None:
        inp = by_id("ch-input")
        if inp:
            inp.style.height = "auto"
            inp.style.height = f"{min(140, inp.scrollHeight)}px"

    # --- sending -----------------------------------------------------------------
    def _set_streaming(self, on: bool) -> None:
        self.streaming = on
        v = voice()
        if v:
            v.setStreaming(on)
        btn = by_id("ch-send")
        if btn:
            btn.classList.toggle("stop", on)
            btn.title = "Stop" if on else "Send"
            btn.innerHTML = '<i class="mdi mdi-stop"></i>' if on else '<i class="mdi mdi-send"></i>'

    def send(self, text: str, regenerate: bool = False, restore: str = "") -> None:
        if self.streaming or not self.data:
            return
        text = (text or "").strip()
        if not text and not regenerate:
            return
        self.task = spawn(self._stream(text, regenerate, restore or text), "chat")

    async def _stream(self, text: str, regenerate: bool, restore: str) -> None:
        card = self.data["card"]
        box = by_id("ch-msgs")
        msgs = self.data["session"]["messages"]
        if regenerate and msgs and msgs[-1]["role"] == "char" and len(msgs) > 1:
            # Re-roll: the reply being replaced leaves the screen while its new take streams.
            node = box.querySelector(f'[data-mid="{msgs[-1]["id"]}"]') if box else None
            if node:
                node.remove()
        if text and not regenerate and box:
            box.insertAdjacentHTML("beforeend", self._msg_html(
                {"id": "pending-user", "role": "user", "content": text,
                 "timestamp": str(js.Date.new().toISOString())}, -1, False))
        if box:
            url = portrait_url(card["id"], card.get("updated_at", "")) if card.get("has_portrait") else ""
            box.insertAdjacentHTML(
                "beforeend",
                '<div class="pt-msg char live" id="ch-live">'
                + avatar_html(card["name"], url, 34, "pt-msg-ava")
                + f'<div class="pt-msg-col"><div class="pt-msg-head">{esc(card["name"])}</div>'
                '<div class="pt-bubble"><span class="pt-typing"><i></i><i></i><i></i></span>'
                f'<span class="pt-dim"> {esc(card["name"])} is thinking</span></div></div></div>')
        by_id("ch-sugs").hidden = True
        self._scroll_bottom()
        self._set_streaming(True)
        self.stopped = False
        acc, done, error = "", None, ""
        v = voice()
        if v:
            v.beginReply()
        stream = Chat().send(self.sid, text, bool(regenerate))
        try:
            async for ev in stream:
                if not isinstance(ev, dict):
                    continue
                if "token" in ev:
                    stick = self._near_bottom()
                    acc += ev["token"]
                    if v:
                        v.feed(acc, False)
                    live = by_id("ch-live")
                    if live:
                        live.querySelector(".pt-bubble").innerHTML = md(acc) + '<span class="pt-cursor"></span>'
                    if stick:
                        self._scroll_bottom()
                elif "expr" in ev:
                    if v:
                        v.expr(str(ev["expr"]))      # timed to the audio when speaking
                elif "error" in ev:
                    error = str(ev["error"])
                elif ev.get("done"):
                    done = ev
                    if v:
                        v.feed(acc, True)
                        v.flushExpr()
                        v.mood(str(ev.get("mood") or ""), float(ev.get("valence") or 0), "")
        except asyncio.CancelledError:
            self.stopped = True
        finally:
            try:
                await stream.aclose()          # aborts the request if it is still open
            except Exception:  # noqa: BLE001
                pass
            self._set_streaming(False)
        # A stopped or failed turn gives back what was typed.
        await self._after(acc, done, error,
                          restore if (self.stopped or error) and not regenerate else "")

    async def _after(self, acc: str, done, error: str, restore: str) -> None:
        # Reload: the server's copy is the truth (stored swipes, ids, expression, mood).
        r = await Chat().open(self.sid)
        if isinstance(r, dict) and r.get("ok") and self.root is not None:
            self.data = r
            self._render_header()
            self._render_messages()
            self._render_sugs()
        if self.stopped:
            toast("Stopped — that turn was not saved." + (" What you typed is back in the box." if restore else ""),
                  "info", 5000)
        elif error:
            toast(f"{self.data['card']['name']} could not answer: {error}", "error", 8000)
        if restore and by_id("ch-input"):
            by_id("ch-input").value = restore
            self._grow()
        await self.app.refresh()

    def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()

    # --- voice -------------------------------------------------------------------
    def _attach_voice(self) -> None:
        v = voice()
        if not v or not self.data or self.root is None:
            return
        v.attach(_js(self.data["card"]), _js(self.data.get("voice") or {}))
        self._paint_voice(v.state())
        mood = self.data["session"].get("mood") or {}
        v.mood(str(mood.get("mood") or ""), float(mood.get("valence") or 0), "")

    def _paint_voice(self, st) -> None:
        if st is None or self.root is None:
            return
        sp = by_id("ch-speaker")
        if sp:
            on = bool(st.speaker)
            sp.classList.toggle("on", on)
            sp.title = "Reading replies aloud — click to stop" if on else "Read replies aloud"
            sp.innerHTML = f'<i class="mdi {"mdi-volume-high" if on else "mdi-volume-off"}"></i>'
        hf = by_id("ch-hf")
        if hf:
            hf.classList.toggle("on", bool(st.hfActive))
            hf.classList.toggle("listening", bool(st.listening))
            hf.classList.toggle("awake", bool(st.awake))
            hf.title = ("Hands-free is listening — click to stop" if st.hfActive
                        else "Hands-free: listen for my voice")
        ptt = by_id("ch-ptt")
        if ptt:
            ptt.classList.toggle("recording", bool(st.recording))
            ptt.classList.toggle("busy", bool(st.busy))

    def _on_ptt(self, ev) -> None:
        v = voice()
        if not v:
            return
        if ev.type == "pointerdown":
            ev.preventDefault()
            v.pttStart()
        else:
            v.pttStop()

    def _on_voice_send(self, ev) -> None:
        text = str(ev.detail.text or "").strip()
        if text and not self.streaming and self.data:
            self.send(text)

    def _on_voice_persist(self, ev) -> None:
        patch = ev.detail.patch.to_py() if ev.detail.patch else {}
        (self.data.get("voice") or {}).update(patch)
        spawn(self._persist(patch), "save voice settings")

    async def _persist(self, patch: dict) -> None:
        from bff.account import Account

        r = await Account().save_settings(patch)
        if isinstance(r, dict) and not r.get("ok"):
            failed(r, "save the voice setting")

    # --- events ------------------------------------------------------------------
    def _on_key(self, ev) -> None:
        if ev.key == "Enter" and not ev.shiftKey and not ev.isComposing:
            ev.preventDefault()
            if self.streaming:
                return
            inp = by_id("ch-input")
            text = str(inp.value or "")
            if text.strip():
                inp.value = ""
                self._grow()
                self.send(text)

    def _on_click(self, ev) -> None:
        if closest(ev, "#ch-send"):
            if self.streaming:
                self.stop()
            else:
                inp = by_id("ch-input")
                text = str(inp.value or "")
                if text.strip():
                    inp.value = ""
                    self._grow()
                    self.send(text)
            return
        vb = closest(ev, "[data-voice]")
        if vb:
            v = voice()
            if v is None:
                toast("Voice is still loading — try again in a moment.", "info")
            elif attr(vb, "data-voice") == "speaker":
                v.speaker()
            else:
                v.handsFree()
            return
        if closest(ev, "#ch-jump"):
            self._scroll_bottom()
            return
        if closest(ev, "[data-burger]"):
            by_id("pt-app").classList.toggle("drawer")
            return
        sug = closest(ev, "[data-sug]")
        if sug:
            self.send(attr(sug, "data-sug"))
            return
        sw = closest(ev, "[data-swipe]")
        if sw and not sw.disabled:
            spawn(self._swipe(int(attr(sw, "data-swipe"))), "swipe")
            return
        mm = closest(ev, "[data-mmenu]")
        if mm:
            self._open_msg_menu(attr(mm, "data-mmenu"), mm)
            return
        menu = closest(ev, "[data-menu]")
        if menu:
            self._open_menu(attr(menu, "data-menu"), menu)
            return
        edit = closest(ev, "[data-edit]")
        if edit:
            spawn(self._edit_action(attr(edit, "data-edit")), "edit")

    # --- swipes ------------------------------------------------------------------
    async def _swipe(self, delta: int) -> None:
        if self.streaming:
            return
        msgs = self.data["session"]["messages"]
        i = self._rerollable()
        if i < 0:
            return
        m = msgs[i]
        n = len(m.get("swipes") or [])
        target = m.get("swipe_index", 0) + delta
        if target < 0:
            return
        if target >= n:
            self.send("", regenerate=True)                    # another take
            return
        m["swipe_index"] = target
        m["content"] = m["swipes"][target]
        self._render_messages()
        r = await Chat().swipe(self.sid, target)
        if isinstance(r, dict) and r.get("expression"):
            m["expression"] = r["expression"]
            self._render_messages()

    # --- menus -------------------------------------------------------------------
    def _msg(self, mid: str):
        return next((m for m in self.data["session"]["messages"] if m["id"] == mid), None)

    def _open_msg_menu(self, mid: str, button) -> None:
        msgs = self.data["session"]["messages"]
        m = self._msg(mid)
        if m is None:
            return
        last_char = self._rerollable()
        idx = msgs.index(m)
        items = []
        if m["role"] == "char" and idx == last_char:
            items.append(MenuItem(id="regen", label="Another take", icon="mdi-autorenew"))
        if m["role"] == "char":
            items.append(MenuItem(id="speak", label="Read aloud", icon="mdi-volume-high"))
        items += [
            MenuItem(id="edit", label="Edit & resend" if m["role"] == "user" else "Edit reply",
                     icon="mdi-pencil-outline"),
            MenuItem(id="copy", label="Copy", icon="mdi-content-copy"),
        ]
        if idx > 0:
            items += [MenuItem(separator=True),
                      MenuItem(id="delete", label="Delete", icon="mdi-delete-outline", danger=True)]
        self._menu_mid = mid
        self.menu.set_items(items)
        rect = button.getBoundingClientRect()
        self.menu.show_at(rect.left, rect.bottom + 4)

    def _open_menu(self, which: str, button) -> None:
        card = self.data["card"]
        self._menu_mid = ""
        if which == "head":
            items = [MenuItem(id="profile", label="Profile", icon="mdi-account-details-outline"),
                     MenuItem(id="studio", label="Edit character", icon="mdi-pencil-outline"),
                     MenuItem(separator=True),
                     MenuItem(id="memory", label="Memory…", icon="mdi-pin-outline"),
                     MenuItem(id="voicecfg", label="Voice settings…", icon="mdi-tune-variant"),
                     MenuItem(separator=True),
                     MenuItem(id="clear", label="Clear chat", icon="mdi-broom", danger=True)]
        else:
            items = [MenuItem(id="regen", label=f"Another take on {card['name']}'s last reply",
                              icon="mdi-autorenew"),
                     MenuItem(id="clear", label="Clear chat", icon="mdi-broom", danger=True)]
        self.menu.set_items(items)
        rect = button.getBoundingClientRect()
        if which == "input":
            self.menu.show_at(rect.left, rect.top - 8)
        else:
            self.menu.show_at(rect.right - 260, rect.bottom + 4)

    def _on_menu(self, p) -> None:
        item = (p or {}).get("id") or ""
        mid = self._menu_mid
        card = self.data["card"]
        if item == "regen":
            if not self.streaming and self._rerollable() >= 0:
                self.send("", regenerate=True)
        elif item == "edit" and mid:
            self.editing = mid
            self._render_messages()
            ed = by_id("ch-edit")
            if ed:
                ed.focus()
                ed.style.height = f"{min(320, max(120, ed.scrollHeight))}px"
        elif item == "copy" and mid:
            m = self._msg(mid)
            if m:
                js.navigator.clipboard.writeText(m["content"])
                toast("Copied.", "success", 2000)
        elif item == "delete" and mid:
            spawn(self._delete(mid), "delete message")
        elif item == "profile":
            self.app.go(f"character/{card['id']}")
        elif item == "studio":
            self.app.go(f"create/{card['id']}")
        elif item == "memory":
            from ui.memory import MemoryModal

            MemoryModal(self.sid)
        elif item == "voicecfg":
            self.app.go("settings")
            js.setTimeout(_proxy_once(lambda: _scroll_to("set-voice")), 400)
        elif item == "speak" and mid:
            m = self._msg(mid)
            v = voice()
            if m and v:
                content = m["content"]
                if m.get("swipes"):
                    content = m["swipes"][max(0, min(m.get("swipe_index", 0), len(m["swipes"]) - 1))]
                v.readAloud(content)
        elif item == "clear":
            spawn(self._clear(), "clear chat")

    # --- edit / delete / clear -----------------------------------------------------
    async def _edit_action(self, which: str) -> None:
        mid = self.editing
        if which == "cancel" or not mid:
            self.editing = ""
            self._render_messages()
            return
        text = str(by_id("ch-edit").value or "").strip()
        if not text:
            toast("A message cannot be empty — delete it instead.", "warning")
            return
        r = await Chat().edit(self.sid, mid, text)
        if failed(r, "save the edit"):
            return
        self.editing = ""
        if which == "resend":
            t = await Chat().truncate_after(self.sid, mid)
            if failed(t, "resend"):
                return
            r = await Chat().open(self.sid)
            if isinstance(r, dict) and r.get("ok"):
                self.data = r
                self._render_messages()
            self.send("", regenerate=True)
            return
        r = await Chat().open(self.sid)
        if isinstance(r, dict) and r.get("ok"):
            self.data = r
            self._render_messages()

    async def _delete(self, mid: str) -> None:
        r = await Chat().delete(self.sid, mid)
        if failed(r, "delete the message"):
            return
        self.data["session"]["messages"] = [m for m in self.data["session"]["messages"] if m["id"] != mid]
        self._render_messages()
        self._render_sugs()

    async def _clear(self) -> None:
        name = self.data["card"]["name"]
        if not await message.confirm(
                f"Clear this chat? Everything after {name}'s greeting goes. What {name} already "
                "remembers about you stays.", title="Clear chat", ok_text="Clear", danger=True):
            return
        r = await Chat().clear(self.sid)
        if failed(r, "clear the chat"):
            return
        await self.load()
        await self.app.refresh()
