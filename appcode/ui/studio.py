"""The Studio — create or edit a character (Ember's "Character Studio").

A full page: the form on the left in sections, a phone-shaped live preview on the right
(the profile header, the greeting bubble and the starter chips, updated as you type).
Everything the classic card editor did is here:

* fields, greetings (a repeater, not one-per-line), starters, lorebook entries;
* personality with voice & tone sliders, and the 3D avatar (VRM import with its audit);
* voice (Kokoro/Piper, pace, test) and the hands-free phrases;
* import a V2/V3 PNG/JSON card, duplicate, delete.

The DOM is the form's state: inputs carry ``data-f="<card field>"`` and ``collect()``
reads them at save time. Leaving with unsaved changes stores a draft in this browser
(``localStorage``) and the next visit offers to restore it — Ember's "drafts auto-restore".
"""
from __future__ import annotations

import json

import js
from pyodide.ffi import create_proxy

from wapyt import message

from bff.library import Library
from bff.studio import Studio
from ui.common import (Listeners, ago, attr, avatar_html, by_id, closest, csrf_token, esc,
                       failed, portrait_url, spawn, toast)

SECTIONS = [("basics", "Basics"), ("persona", "Personality & tone"), ("greet", "Greetings"),
            ("lore", "Lorebook"),
            ("avatar", "3D avatar"), ("voice", "Voice"), ("menu", "Menu"), ("advanced", "Advanced")]


def _draft_key(card_id: str) -> str:
    return f"pantheon.draft.{card_id or 'new'}"


def _local(op: str, key: str, value: str = ""):
    try:
        store = js.window.localStorage
        if op == "get":
            return str(store.getItem(key) or "")
        if op == "set":
            store.setItem(key, value)
        else:
            store.removeItem(key)
    except Exception:  # noqa: BLE001 - private windows can refuse storage
        return ""
    return ""


# The voice & tone sliders: key on CharacterCard.tone, label, and what each end means.
TONE_SLIDERS = (
    ("warmth", "Warmth", "reserved", "warm"),
    ("formality", "Formality", "casual", "formal"),
    ("playfulness", "Playfulness", "serious", "playful"),
    ("detail", "Detail", "brief", "thorough"),
    ("femininity", "Speech style", "masculine", "feminine"),
)


class StudioView:
    def __init__(self, app, arg: str = "") -> None:
        self.app = app
        self.card_id = arg
        self.root = None
        self.data: dict = {}
        self.lore: list = []                  # original entry dicts (unknown fields survive)
        self.portrait = ""                    # a newly picked image (data URL), not yet saved
        self.remove_portrait = False
        self.vrm_still = ""                   # a freshly captured 3D still (data URL), not yet saved
        self.vrm_still_of = ""
        self.dirty = False
        self.saving = False
        self.listeners = Listeners()
        self._keep: list = []

    # --- lifecycle ---------------------------------------------------------------
    def mount(self, root) -> None:
        self.root = root
        root.innerHTML = '<div class="pt-chat-loading"><span class="pt-spin"></span></div>'
        spawn(self.load(), "studio")

    def unmount(self) -> None:
        if self.dirty and self.data:
            self._save_draft()
        self.listeners.clear()
        self.root = None

    async def load(self) -> None:
        r = await Studio().load(self.card_id)
        if self.root is None:
            return
        if failed(r, "open the studio"):
            self.root.innerHTML = ('<div class="pt-page"><div class="pt-empty">That character could not be '
                                   'opened. <a href="#/discover">Back to the staff</a></div></div>')
            return
        self.data = r
        book = r["card"].get("character_book") or {}
        self.lore = [dict(e) for e in (book.get("entries") or [])]
        self.render()
        self._offer_draft()

    # --- render ------------------------------------------------------------------
    def render(self) -> None:
        c = self.data["card"]
        new = self.data["is_new"]
        title = "Hire a <em>bartender</em>" if new else f"Edit <em>{esc(c['name'])}</em>"
        head_btns = []
        if new:
            head_btns.append('<label class="pt-btn"><i class="mdi mdi-file-import-outline"></i>Import card'
                             '<input type="file" id="st-import" accept=".png,.json,application/json,image/png" hidden></label>')
        else:
            head_btns += ['<button class="pt-btn" data-act="duplicate"><i class="mdi mdi-content-duplicate"></i>Duplicate</button>',
                          '<button class="pt-btn danger" data-act="delete"><i class="mdi mdi-delete-outline"></i>Delete</button>']
        nav = "".join(f'<a class="pt-pill" href="#sec-{k}" data-jump="{k}">{label}</a>' for k, label in SECTIONS)
        self.root.innerHTML = (
            '<div class="pt-page pt-studio">'
            '<div class="pt-studio-head"><div><div class="pt-kicker">Staff studio</div>'
            f'<h1 class="pt-h1">{title}</h1></div>'
            f'<div class="pt-studio-headbtns">{"".join(head_btns)}</div></div>'
            '<div id="st-draft"></div>'
            f'<nav class="pt-pills pt-studio-nav">{nav}</nav>'
            '<div class="pt-studio-grid">'
            f'<div class="pt-studio-form" id="st-form">{self._form_html()}</div>'
            '<aside class="pt-studio-side"><div class="pt-phone" id="st-preview"></div></aside>'
            '</div>'
            '<div class="pt-studio-bar"><span class="pt-dim" id="st-status"></span><span class="pt-flex"></span>'
            '<button class="pt-btn ghost" data-act="cancel">Cancel</button>'
            + ('<button class="pt-btn" data-act="save">Hire</button>'
               '<button class="pt-btn primary" data-act="save-chat"><i class="mdi mdi-message-plus-outline"></i>Hire &amp; chat</button>'
               if new else
               '<button class="pt-btn primary" data-act="save"><i class="mdi mdi-content-save-outline"></i>Save</button>')
            + '</div></div>')
        form = by_id("st-form")
        self.listeners.on(self.root, "click", self._on_click)
        self.listeners.on(form, "input", self._on_input)
        self.listeners.on(form, "change", self._on_change)
        if by_id("st-import"):
            self.listeners.on(by_id("st-import"), "change", lambda _e: spawn(self._import(), "import"))
        self._sync_avatar_mode()
        self._render_vrm_info()
        self._sync_profile()
        self._render_preview()

    # Small field builders. Every value is escaped; data-f names the card field.
    @staticmethod
    def _inp(label: str, f: str, value, hint: str = "", ph: str = "") -> str:
        return (f'<label class="pt-field"><span>{label}</span>'
                f'<input type="text" data-f="{f}" value="{esc(value or "")}" placeholder="{esc(ph)}">'
                + (f'<small>{hint}</small>' if hint else "") + '</label>')

    @staticmethod
    def _area(label: str, f: str, value, rows: int = 3, hint: str = "", ph: str = "") -> str:
        return (f'<label class="pt-field"><span>{label}</span>'
                f'<textarea data-f="{f}" rows="{rows}" placeholder="{esc(ph)}">{esc(value or "")}</textarea>'
                + (f'<small>{hint}</small>' if hint else "") + '</label>')

    @staticmethod
    def _sel(label: str, f: str, options: list, value, hint: str = "") -> str:
        opts = "".join(f'<option value="{esc(v)}"{" selected" if str(v) == str(value or "") else ""}>{esc(t)}</option>'
                       for v, t in options)
        return (f'<label class="pt-field"><span>{label}</span><select data-f="{f}">{opts}</select>'
                + (f'<small>{hint}</small>' if hint else "") + '</label>')

    @staticmethod
    def _tone_row(key: str, label: str, lo: str, hi: str, value) -> str:
        v = max(-1.0, min(1.0, float(value or 0)))
        return (f'<label class="pt-field pt-tone"><span>{label}</span><div class="pt-tone-row">'
                f'<small>{lo}</small><input type="range" data-tone="{key}" min="-1" max="1" step="0.1" value="{v:g}">'
                f'<small>{hi}</small></div></label>')

    def _form_html(self) -> str:
        c = self.data["card"]
        new = self.data["is_new"]
        portrait = ""
        if c.get("has_portrait") and not new:
            portrait = portrait_url(c["id"], c.get("updated_at", ""))
        sec = []
        # --- basics
        sec.append(
            '<section class="pt-sec" id="sec-basics"><h2>Basics</h2>'
            '<div class="pt-portrait-row">'
            f'<div class="pt-portrait-pick" id="st-portrait">{avatar_html(c.get("name") or "?", portrait, 96)}</div>'
            '<div class="pt-portrait-btns">'
            '<label class="pt-btn small"><i class="mdi mdi-image-plus"></i>Choose portrait'
            '<input type="file" id="st-portrait-file" accept="image/*" hidden></label>'
            f'<button class="pt-btn small ghost" data-act="remove-portrait"{"" if portrait else " hidden"}>Remove</button>'
            '<small class="pt-dim">Shown on cards, in the sidebar and in chat. Stored as PNG.</small>'
            '</div></div>'
            + self._profile_html()
            + '<div class="pt-row2">'
            + self._inp("Name", "name", c.get("name"), ph="Who are they?")
            + self._inp("Nickname", "nickname", c.get("nickname"))
            + '</div>'
            + self._sel("Figure", "figure", [("", "Not set"), ("female", "Female"), ("male", "Male")],
                        c.get("figure") or "",
                        "How the 3D avatar carries herself: which poses she uses and how she stands in them.")
            + self._inp("Tagline", "creator_notes", c.get("creator_notes"),
                        "One line under their name on cards and the profile (the card's creator notes).",
                        "Keeps your calendar and the house running")
            + '</section>')
        # --- personality, voice and tone
        tone = c.get("tone") or {}
        sec.append(
            '<section class="pt-sec" id="sec-persona"><h2>Personality &amp; tone <small>use {{char}} and {{user}}</small></h2>'
            + self._area("Description", "description", c.get("description"), 6,
                         "Who they are and what they're for: their role, what they know, how they work.")
            + self._area("Personality", "personality", c.get("personality"), 2, ph="warm, observant, dry humour")
            + '<h3 class="pt-sub">Voice &amp; tone <small>how they talk; the middle is no lean either way</small></h3>'
            + "".join(self._tone_row(key, label, lo, hi, tone.get(key, 0.0))
                      for key, label, lo, hi in TONE_SLIDERS)
            + '</section>')
        # --- greetings + starters
        greets = [c.get("first_mes") or ""] + list(c.get("alternate_greetings") or [])
        sec.append(
            '<section class="pt-sec" id="sec-greet"><h2>Greetings <small>how a chat opens</small></h2>'
            f'<div class="pt-rep" id="st-greets">{"".join(self._greet_row(g, i) for i, g in enumerate(greets))}</div>'
            '<button class="pt-btn small" data-act="add-greet"><i class="mdi mdi-plus"></i>Add an alternate greeting</button>'
            '<h3 class="pt-sub">Conversation starters <small>the "Try saying" chips, up to six</small></h3>'
            f'<div class="pt-rep" id="st-starters">{"".join(self._starter_row(s) for s in c.get("starters") or [])}</div>'
            '<button class="pt-btn small" data-act="add-starter"><i class="mdi mdi-plus"></i>Add a starter</button>'
            '</section>')
        # --- lorebook
        sec.append(
            '<section class="pt-sec" id="sec-lore"><h2>Lorebook <small>injected when a key appears in the chat</small></h2>'
            f'<div class="pt-rep" id="st-lore">{"".join(self._lore_row(e, i) for i, e in enumerate(self.lore))}</div>'
            '<button class="pt-btn small" data-act="add-lore"><i class="mdi mdi-plus"></i>Add an entry</button>'
            '</section>')
        # --- avatar
        vrm_opts = [("", "— none —")] + [(m["name"], f'{m["name"]} ({m.get("size", 0) / 1048576:.0f} MB{" — unusable" if m.get("usable") is False else ""})')
                                         for m in self.data.get("vrms") or []]
        sec.append(
            '<section class="pt-sec" id="sec-avatar"><h2>3D avatar <small>optional; without one, the portrait is shown</small></h2>'
            '<div id="st-vrm-body">'
            '<div class="pt-row2">'
            + self._sel("Model", "vrm", vrm_opts, c.get("vrm") or "")
            + '<div class="pt-field"><span>&nbsp;</span><div class="pt-btnrow">'
            '<label class="pt-btn small"><i class="mdi mdi-upload"></i>Import .vrm<input type="file" id="st-vrm-file" accept=".vrm" hidden></label>'
            '<button class="pt-btn small danger" data-act="vrm-delete">Delete model</button></div></div>'
            '</div><div class="pt-vrm-info" id="st-vrm-info"></div></div>'
            '</section>')
        # --- voice
        voices = [("", "default (af_sarah)")] + [(v, v) for v in self.data.get("voices") or []]
        speed = float(c.get("tts_speed") or 1.0)
        sec.append(
            '<section class="pt-sec" id="sec-voice"><h2>Voice</h2>'
            '<div class="pt-row2">'
            + self._sel("Voice", "tts_voice", voices, c.get("tts_voice") or "")
            + '<label class="pt-field"><span>Pace · <b id="st-speed-val">' + f"{speed:.2f}" + '</b></span>'
            f'<input type="range" data-f="tts_speed" min="0.5" max="1.5" step="0.05" value="{speed:g}">'
            '<small>1.00 = the voice\'s own tempo · lower is slower.</small></label>'
            '</div>'
            '<button class="pt-btn small" data-act="test-voice"><i class="mdi mdi-play"></i>Test voice</button>'
            '<h3 class="pt-sub">Hands-free <small>blank = the global setting</small></h3>'
            '<div class="pt-row3">'
            + self._inp("Wake word", "wake_word", c.get("wake_word"))
            + self._inp("Sleep phrase", "sleep_phrase", c.get("sleep_phrase"))
            + self._inp("Farewell", "farewell", c.get("farewell"))
            + '</div></section>')
        # --- the Menu (skills)
        on = set(c.get("skills") or [])
        items = "".join(
            f'<label class="pt-check pt-menu-item"><input type="checkbox" data-skill="{esc(m["id"])}"'
            f'{" checked" if m["id"] in on else ""}><span><b>{esc(m["title"])}</b>'
            f'<small class="pt-dim">{esc(m["description"])}</small></span></label>'
            for m in self.data.get("menu") or [])
        sec.append(
            '<section class="pt-sec" id="sec-menu"><h2>Menu <small>what they can do beyond talk</small></h2>'
            + (items or '<p class="pt-dim">Nothing on the menu yet.</p>')
            + '<small class="pt-dim">Each item costs a little of every prompt, so add only what they need.</small>'
            '</section>')
        # --- advanced
        sec.append(
            '<section class="pt-sec" id="sec-advanced"><h2>Advanced</h2>'
            + self._area("System prompt", "system_prompt", c.get("system_prompt"), 3,
                         "Replaces the built-in assistant prompt entirely (including its spoken-reply rules).")
            + '</section>')
        return "".join(sec)

    @staticmethod
    def _greet_row(text: str, i: int) -> str:
        label = "First message" if i == 0 else f"Alternate {i}"
        rm = '' if i == 0 else '<button class="pt-mbtn" data-rm="1" title="Remove"><i class="mdi mdi-close"></i></button>'
        return (f'<div class="pt-rep-row" data-rep="greet"><div class="pt-rep-head"><span>{label}</span>{rm}</div>'
                f'<textarea rows="3" data-greet="1">{esc(text)}</textarea></div>')

    @staticmethod
    def _starter_row(text: str) -> str:
        return ('<div class="pt-rep-row inline" data-rep="starter">'
                f'<input type="text" data-starter="1" value="{esc(text)}" placeholder="What&#39;s on today?">'
                '<button class="pt-mbtn" data-rm="1" title="Remove"><i class="mdi mdi-close"></i></button></div>')

    @staticmethod
    def _lore_row(e: dict, i: int) -> str:
        pos = e.get("position") or "before_char"
        return (
            f'<div class="pt-rep-row" data-rep="lore" data-idx="{i}">'
            '<div class="pt-lore-top">'
            f'<input type="text" data-lore="keys" value="{esc(", ".join(e.get("keys") or []))}" placeholder="Trigger keys, comma separated">'
            f'<label class="pt-check"><input type="checkbox" data-lore="constant"{" checked" if e.get("constant") else ""}>Always</label>'
            f'<select data-lore="position"><option value="before_char"{" selected" if pos == "before_char" else ""}>Before</option>'
            f'<option value="after_char"{" selected" if pos == "after_char" else ""}>After</option></select>'
            '<button class="pt-mbtn" data-rm="1" title="Remove entry"><i class="mdi mdi-close"></i></button></div>'
            f'<textarea rows="2" data-lore="content" placeholder="Text injected when a key matches…">{esc(e.get("content") or "")}</textarea>'
            '</div>')

    # --- profile picture -----------------------------------------------------------
    def _profile_html(self) -> str:
        """Where the profile picture comes from: portrait, one sprite, or a 3D still."""
        c = self.data["card"]
        src = c.get("portrait_source") or ""
        still = ("Captured from " + esc(c.get("vrm_still_of") or "the model")) if c.get("has_vrm_still") else "No still yet"
        return (
            '<div class="pt-row2">'
            + self._sel("Profile picture", "portrait_source",
                        [("", "Automatic"), ("image", "Uploaded portrait"), ("vrm", "3D model still")]
                        + ([("sprite", "Expression sprite (older card)")] if src == "sprite" else []), src,
                        "What cards, the sidebar and the chat header show.")
            + '<div class="pt-field" id="st-pp-vrm"><span>3D still</span><div class="pt-btnrow">'
            '<button class="pt-btn small" data-act="capture-still"><i class="mdi mdi-camera"></i>Capture from model</button></div>'
            f'<small id="st-pp-still">{still}. Taken from the model chosen under 3D avatar; retaken on save when the model changes.</small></div>'
            '</div>')

    def _pp_source(self) -> str:
        return self._field("portrait_source")

    def _sync_profile(self) -> None:
        src = self._pp_source()
        for el_id, on in (("st-pp-vrm", src == "vrm"),):
            el = by_id(el_id)
            if el:
                el.hidden = not on

    async def _capture_still(self, quiet: bool = False) -> bool:
        name = self._field("vrm")
        if not name:
            if not quiet:
                toast("Choose a model under 3D avatar first.", "warning")
            return False
        note = by_id("st-pp-still")
        if note:
            note.textContent = "Rendering the model…"
        try:
            url = "/pmedia/vrm/" + str(js.encodeURIComponent(name))
            self.vrm_still = str(await js.window.PantheonVoice.captureVrmStill(url))
            self.vrm_still_of = name
        except Exception as exc:  # noqa: BLE001 - WebGL missing, bad model
            if note:
                note.textContent = "Could not render the model."
            toast(f"Could not render {name}: {exc}", "error")
            return False
        if note:
            note.textContent = f"Captured from {name} — save to keep it."
        self._mark_dirty()
        self._render_preview()
        return True

    # --- avatar --------------------------------------------------------------------
    def _avatar_mode(self) -> str:
        # No sprites to choose any more: a chosen model is the avatar, else the portrait.
        return "vrm" if self._field("vrm") else "sprite"

    def _sync_avatar_mode(self) -> None:
        pass                                # the model picker is always shown now

    def _render_vrm_info(self) -> None:
        box = by_id("st-vrm-info")
        if not box:
            return
        name = self._field("vrm")
        m = next((x for x in self.data.get("vrms") or [] if x.get("name") == name), None)
        if not m:
            box.innerHTML = ('<span class="pt-dim">No model assigned — this character shows their portrait. '
                             'Import a .vrm (VRoid Hub, Booth) to give them a 3D avatar.</span>')
            return
        if m.get("error"):
            box.innerHTML = f'<span class="pt-bad">unreadable: {esc(m["error"])}</span>'
            return
        lic = m.get("license") or {}

        def flag(label, v):
            ok = v in (True, "Allow", "allow", "everyone")
            return f'<span class="{"pt-good" if ok else "pt-warn"}">{label}: {esc("?" if v is None else v)}</span>'
        rows = [f'<code>{esc(m.get("spec"))}</code> {esc(m.get("title") or "untitled")}'
                + (f' — {esc(m["author"])}' if m.get("author") else ""),
                f'{int(m.get("triangles") or 0):,} triangles · {m.get("springs", 0)} spring groups · '
                f'{len(m.get("emotive") or [])}/5 expressions · {len(m.get("visemes") or [])}/5 visemes'
                + (" · blink" if m.get("blink") else "") + (" · gaze" if m.get("look_at") else ""),
                " · ".join([flag("redistribution", lic.get("redistribution")), flag("sexual use", lic.get("sexual")),
                            flag("users", lic.get("allowed_user"))])]
        if m.get("problems"):
            rows.append(f'<span class="pt-bad">{esc("; ".join(m["problems"]))}</span>')
        if m.get("notes"):
            rows.append(f'<span class="pt-warn">{esc("; ".join(m["notes"]))}</span>')
        box.innerHTML = "<br>".join(rows)

    async def _upload_vrm(self) -> None:
        inp = by_id("st-vrm-file")
        if not inp or not inp.files.length:
            return
        f = inp.files.item(0)
        box = by_id("st-vrm-info")
        limit = int(self.data.get("vrm_max_bytes") or 0)
        if limit and int(f.size) > limit:
            # Said here, before a large file is uploaded only to be refused.
            box.innerHTML = (f'<span class="pt-bad">{esc(f.name)} is {int(f.size) / 1048576:.0f} MB; '
                             f'models can be up to {limit // 1048576} MB.</span>')
            inp.value = ""
            return
        box.textContent = f"Importing {f.name} ({int(f.size) / 1048576:.0f} MB)… the model is audited before it is kept"
        fd = js.FormData.new()
        fd.append("file", f)
        r = await self._post_form("/pmedia/vrm", fd)
        inp.value = ""
        if r.get("_error"):
            box.innerHTML = f'<span class="pt-bad">Import failed: {esc(r["_error"])}</span>'
            return
        v = await Studio().vrms()
        self.data["vrms"] = v.get("vrms") or []
        sel = self.root.querySelector('[data-f="vrm"]')
        sel.innerHTML = '<option value="">— none —</option>' + "".join(
            f'<option value="{esc(m["name"])}">{esc(m["name"])}</option>' for m in self.data["vrms"])
        sel.value = r.get("name", "")
        self._mark_dirty()
        self._render_vrm_info()
        toast(f"Imported {r.get('name')}.", "success")

    async def _delete_vrm(self) -> None:
        name = self._field("vrm")
        if not name:
            return
        if not await message.confirm(f"Delete {name}? The file is removed for every character.",
                                     title="Delete model", ok_text="Delete", danger=True):
            return
        r = await Studio().delete_vrm(name)
        if failed(r, f"delete {name}"):                  # refused while a card still uses it
            return
        self.data["vrms"] = r.get("vrms") or []
        sel = self.root.querySelector('[data-f="vrm"]')
        sel.innerHTML = '<option value="">— none —</option>' + "".join(
            f'<option value="{esc(m["name"])}">{esc(m["name"])}</option>' for m in self.data["vrms"])
        sel.value = ""
        self._render_vrm_info()

    async def _post_form(self, url: str, form) -> dict:
        """A multipart POST to one of the plain upload routes, with the CSRF header."""
        opts = js.Object.new()
        opts.method = "POST"
        opts.credentials = "same-origin"
        opts.body = form
        headers = js.Object.new()
        setattr(headers, "X-CSRF-Token", csrf_token())
        opts.headers = headers
        try:
            resp = await js.fetch(url, opts)
            body = json.loads(str(await resp.text()) or "{}")
        except Exception as exc:  # noqa: BLE001
            return {"_error": str(exc)}
        if not resp.ok:
            if int(resp.status) == 413:
                limit = int(self.data.get("vrm_max_bytes") or 0) // 1048576
                return {"_error": "the file is too large" + (f" (the limit is {limit} MB)" if limit else "")}
            return {"_error": body.get("detail") or f"HTTP {resp.status}"}
        return body

    # --- import ----------------------------------------------------------------------
    async def _import(self) -> None:
        inp = by_id("st-import")
        if not inp or not inp.files.length:
            return
        fd = js.FormData.new()
        fd.append("file", inp.files.item(0))
        r = await self._post_form("/pmedia/import", fd)
        inp.value = ""
        if r.get("_error"):
            toast(f"Import failed: {r['_error']}", "error", 8000)
            return
        toast(f"Imported {r.get('name')}.", "success")
        await self.app.refresh()
        self.app.go(f"create/{r['id']}")

    # --- collect + preview ------------------------------------------------------------
    def _field(self, f: str) -> str:
        el = self.root.querySelector(f'[data-f="{f}"]') if self.root else None
        return str(el.value) if el else ""

    def collect(self) -> dict:
        fields = {}
        for el in self.root.querySelectorAll("[data-f]"):
            fields[attr(el, "data-f")] = str(el.value)
        if "tags" in fields:                 # not on the form any more; never wipe them
            fields["tags"] = [t.strip() for t in fields["tags"].split(",") if t.strip()]
        fields["tone"] = {attr(el, "data-tone"): float(el.value or 0)
                          for el in self.root.querySelectorAll("[data-tone]")}
        fields["skills"] = [attr(el, "data-skill") for el in self.root.querySelectorAll("[data-skill]")
                            if el.checked]
        for k in ("tts_speed",):
            if k in fields:
                fields[k] = float(fields[k] or 0)
        greets = [str(t.value) for t in self.root.querySelectorAll("[data-greet]")]
        fields["first_mes"] = greets[0] if greets else ""
        fields["alternate_greetings"] = [g for g in greets[1:] if g.strip()]
        fields["avatar_mode"] = self._avatar_mode()
        starters = [str(s.value).strip() for s in self.root.querySelectorAll("[data-starter]") if str(s.value).strip()]
        lore = []
        for row in self.root.querySelectorAll('[data-rep="lore"]'):
            idx = attr(row, "data-idx")
            entry = dict(self.lore[int(idx)]) if idx.isdigit() and int(idx) < len(self.lore) else {}

            def get(key):
                return row.querySelector(f'[data-lore="{key}"]')
            entry["keys"] = [k.strip() for k in str(get("keys").value).split(",") if k.strip()]
            entry["content"] = str(get("content").value)
            entry["constant"] = bool(get("constant").checked)
            entry["position"] = str(get("position").value)
            entry.pop("min_stage", None)
            entry.setdefault("enabled", True)
            lore.append(entry)
        return {"fields": fields, "starters": starters, "lore": lore}

    def _render_preview(self) -> None:
        box = by_id("st-preview")
        if not box:
            return
        c = self.data["card"]
        name = self._field("name").strip() or "Unnamed"
        tagline = self._field("creator_notes").strip() or self._field("personality").strip()
        persona = ((self.app.shell or {}).get("persona") or {}).get("name") or "you"
        greet_el = self.root.querySelector("[data-greet]")
        greeting = str(greet_el.value) if greet_el else ""
        greeting = greeting.replace("{{char}}", name).replace("{{user}}", persona)
        url = self._preview_url()
        from ui.chat import md

        starters = [str(s.value).strip() for s in self.root.querySelectorAll("[data-starter]") if str(s.value).strip()]
        chips = "".join(f'<span class="pt-sug">{esc(s)}</span>' for s in starters[:4])
        box.innerHTML = (
            '<div class="pt-phone-notch"></div>'
            '<div class="pt-phone-head">' + avatar_html(name, url, 40)
            + f'<div><b>{esc(name)}</b><span>{esc(tagline[:80])}</span></div></div>'
            '<div class="pt-phone-body">'
            + (f'<div class="pt-msg char">{avatar_html(name, url, 26, "pt-msg-ava")}<div class="pt-msg-col">'
               f'<div class="pt-msg-head">{esc(name)}</div><div class="pt-bubble">{md(greeting)}</div></div></div>'
               if greeting.strip() else '<p class="pt-dim pt-phone-empty">Write a first message to see how a chat opens.</p>')
            + '</div>'
            + (f'<div class="pt-phone-chips">{chips}</div>' if chips else "")
            + f'<div class="pt-phone-input">Message {esc(name)}…<i class="mdi mdi-send"></i></div>')
        pick = by_id("st-portrait")
        if pick:
            pick.innerHTML = avatar_html(name, url, 96)

    def _preview_url(self) -> str:
        """The picture the card will show once saved, given the unsaved choices."""
        c = self.data["card"]
        saved = not self.data["is_new"]
        src = self._pp_source()
        if src == "vrm" and self.vrm_still:
            return self.vrm_still
        if src == "sprite" and saved:                # an older card kept on its sprite picture
            return portrait_url(c["id"], c.get("updated_at", ""))
        if self.portrait and src in ("", "image"):
            return self.portrait
        if saved and not self.remove_portrait and (c.get("has_portrait") or c.get("has_vrm_still")
                                                   or c.get("sprites")):
            return portrait_url(c["id"], c.get("updated_at", ""))
        return ""

    # --- drafts ------------------------------------------------------------------------
    def _mark_dirty(self) -> None:
        self.dirty = True
        st = by_id("st-status")
        if st:
            st.textContent = "Unsaved changes"

    def _save_draft(self) -> None:
        try:
            data = self.collect()
        except Exception:  # noqa: BLE001 - a half-torn-down DOM
            return
        data["at"] = str(js.Date.new().toISOString())
        _local("set", _draft_key(self.card_id), json.dumps(data))

    def _offer_draft(self) -> None:
        raw = _local("get", _draft_key(self.card_id))
        if not raw:
            return
        try:
            draft = json.loads(raw)
        except ValueError:
            _local("del", _draft_key(self.card_id))
            return
        self._draft = draft
        by_id("st-draft").innerHTML = (
            '<div class="pt-banner"><i class="mdi mdi-history"></i>'
            f'<span>You left unsaved changes here {esc(ago(draft.get("at", "")))} ago.</span>'
            '<button class="pt-btn small primary" data-act="restore">Restore</button>'
            '<button class="pt-btn small ghost" data-act="discard">Discard</button></div>')

    def _restore_draft(self) -> None:
        d = getattr(self, "_draft", None) or {}
        f = d.get("fields") or {}
        card = self.data["card"]
        for key, value in f.items():
            if key not in ("first_mes", "alternate_greetings", "avatar_mode", "tags"):
                card[key] = value
        if "tags" in f:
            tags = f.get("tags") or []
            card["tags"] = tags if isinstance(tags, list) else [t.strip() for t in str(tags).split(",") if t.strip()]
        card["first_mes"] = f.get("first_mes", card.get("first_mes"))
        card["alternate_greetings"] = f.get("alternate_greetings", card.get("alternate_greetings"))
        card["avatar_mode"] = f.get("avatar_mode", card.get("avatar_mode"))
        card["starters"] = d.get("starters", card.get("starters"))
        self.lore = d.get("lore", self.lore)
        self.listeners.clear()
        self.render()
        self._mark_dirty()
        _local("del", _draft_key(self.card_id))
        toast("Draft restored — Save to keep it.", "success")

    # --- events --------------------------------------------------------------------------
    def _on_input(self, ev) -> None:
        t = ev.target
        f = attr(t, "data-f")
        if f == "tts_speed":
            by_id("st-speed-val").textContent = f"{float(t.value):.2f}"
        self._mark_dirty()
        if f in ("name", "creator_notes", "personality") or t.hasAttribute("data-greet") or t.hasAttribute("data-starter"):
            self._render_preview()

    def _on_change(self, ev) -> None:
        t = ev.target
        if t.id == "st-portrait-file":
            self._pick_portrait(t)
        elif t.id == "st-vrm-file":
            spawn(self._upload_vrm(), "vrm upload")
        elif attr(t, "data-f") == "vrm":
            self._render_vrm_info()
        elif attr(t, "data-f") == "portrait_source":
            self._sync_profile()
            self._render_preview()

    def _pick_portrait(self, inp) -> None:
        if not inp.files.length:
            return
        reader = js.FileReader.new()

        def loaded(*_):
            self.portrait = str(reader.result)
            self.remove_portrait = False
            rm = self.root.querySelector('[data-act="remove-portrait"]')
            if rm:
                rm.hidden = False
            self._mark_dirty()
            self._render_preview()
        proxy = create_proxy(loaded)
        self._keep.append(proxy)
        reader.addEventListener("load", proxy)
        reader.readAsDataURL(inp.files.item(0))

    def _on_click(self, ev) -> None:
        jump = closest(ev, "[data-jump]")
        if jump:
            ev.preventDefault()
            target = by_id(f"sec-{attr(jump, 'data-jump')}")
            if target:
                target.scrollIntoView(js.JSON.parse('{"behavior":"smooth","block":"start"}'))
            return
        rm = closest(ev, "[data-rm]")
        if rm:
            ev.preventDefault()
            rm.closest(".pt-rep-row").remove()
            self._renumber_greets()
            self._mark_dirty()
            self._render_preview()
            return
        act = closest(ev, "[data-act]")
        if not act:
            return
        ev.preventDefault()
        which = attr(act, "data-act")
        if which == "add-greet":
            box = by_id("st-greets")
            box.insertAdjacentHTML("beforeend", self._greet_row("", box.children.length))
            box.lastElementChild.querySelector("textarea").focus()
        elif which == "add-starter":
            box = by_id("st-starters")
            if box.children.length >= 6:
                toast("Six starters at most — the chip row has room for that many.", "info")
                return
            box.insertAdjacentHTML("beforeend", self._starter_row(""))
            box.lastElementChild.querySelector("input").focus()
        elif which == "add-lore":
            box = by_id("st-lore")
            box.insertAdjacentHTML("beforeend", self._lore_row({}, -1))
            box.lastElementChild.querySelector("input").focus()
            self._mark_dirty()
        elif which == "remove-portrait":
            self.portrait = ""
            self.remove_portrait = True
            act.hidden = True
            self._mark_dirty()
            self._render_preview()
        elif which == "capture-still":
            spawn(self._capture_still(), "capture still")
        elif which == "vrm-delete":
            spawn(self._delete_vrm(), "vrm delete")
        elif which == "test-voice":
            self._test_voice()
        elif which in ("save", "save-chat"):
            spawn(self._save(chat=which == "save-chat"), "save")
        elif which == "cancel":
            spawn(self._cancel(), "cancel")
        elif which == "duplicate":
            spawn(self._duplicate(), "duplicate")
        elif which == "delete":
            spawn(self._delete(), "delete")
        elif which == "restore":
            self._restore_draft()
        elif which == "discard":
            _local("del", _draft_key(self.card_id))
            by_id("st-draft").innerHTML = ""

    def _renumber_greets(self) -> None:
        for i, row in enumerate(self.root.querySelectorAll('[data-rep="greet"] .pt-rep-head span')):
            row.textContent = "First message" if i == 0 else f"Alternate {i}"

    def _test_voice(self) -> None:
        voice = self._field("tts_voice")
        speed = self._field("tts_speed") or "1"
        audio = js.Audio.new(f"/pmedia/tts-sample.wav?voice={js.encodeURIComponent(voice)}&speed={speed}")
        audio.play()

    # --- save / cancel / duplicate / delete ------------------------------------------------
    async def _save(self, chat: bool = False) -> None:
        if self.saving:
            return
        data = self.collect()
        if not data["fields"].get("name", "").strip():
            toast("A character needs a name.", "warning")
            self.root.querySelector('[data-f="name"]').focus()
            return
        self.saving = True
        by_id("st-status").textContent = "Saving…"
        # A 3D-still profile needs a still of the model actually assigned: take one now
        # if there is none, or the model changed since the last one.
        c = self.data["card"]
        model = data["fields"].get("vrm") or ""
        if (data["fields"].get("portrait_source") == "vrm" and model and not self.vrm_still
                and (not c.get("has_vrm_still") or c.get("vrm_still_of") != model)):
            await self._capture_still(quiet=True)
        try:
            r = await Studio().save(self.card_id, data["fields"], self.portrait,
                                    self.remove_portrait, data["starters"], data["lore"],
                                    self.vrm_still)
        finally:
            self.saving = False
        if failed(r, "save"):
            by_id("st-status").textContent = "Not saved"
            if isinstance(r, dict) and r.get("field"):
                el = self.root.querySelector(f'[data-f="{r["field"]}"]')
                if el:
                    el.focus()
            return
        self.dirty = False
        _local("del", _draft_key(self.card_id))
        _local("del", _draft_key(r["id"]))
        name = data["fields"]["name"]
        await self.app.refresh()
        if chat:
            s = await Library().start_chat(r["id"])
            if not failed(s, "start a chat"):
                await self.app.refresh()
                self.app.go(f"chat/{s['session_id']}")
            return
        toast(f"{name} saved.", "success")
        if not self.card_id:
            self.app.go(f"create/{r['id']}")          # now a saved card
            return
        if self.portrait or self.remove_portrait:
            c["has_portrait"] = bool(self.portrait)
            c["updated_at"] = r.get("updated_at") or str(js.Date.now())
        self.portrait, self.remove_portrait = "", False
        if self.vrm_still:
            c["has_vrm_still"], c["vrm_still_of"] = True, self.vrm_still_of
        self.vrm_still = ""
        c["portrait_source"] = data["fields"].get("portrait_source", "")
        c["updated_at"] = r.get("updated_at") or str(js.Date.now())
        by_id("st-status").textContent = "Saved"

    async def _cancel(self) -> None:
        if self.dirty and not await message.confirm("Discard your unsaved changes?", title="Discard changes",
                                                    ok_text="Discard", danger=True):
            return
        self.dirty = False
        _local("del", _draft_key(self.card_id))
        self.app.go(f"character/{self.card_id}" if self.card_id else "discover")

    async def _duplicate(self) -> None:
        if self.dirty and not await message.confirm(
                "The copy is made from the saved character — your unsaved changes stay here. Continue?",
                title="Duplicate", ok_text="Duplicate"):
            return
        r = await Studio().duplicate(self.card_id)
        if failed(r, "duplicate"):
            return
        toast("Copy made — you are editing the copy now.", "success")
        await self.app.refresh()
        self.app.go(f"create/{r['id']}")

    async def _delete(self) -> None:
        name = self.data["card"].get("name") or "this character"
        if not await message.confirm(f"Delete {name}? Their memory is deleted too. Chats "
                                     "with them stay on disk but no longer appear.", title="Remove from the staff",
                                     ok_text="Delete", danger=True):
            return
        r = await Library().delete_card(self.card_id)
        if failed(r, f"delete {name}"):
            return
        self.dirty = False
        _local("del", _draft_key(self.card_id))
        toast(f"{name} deleted.", "success")
        await self.app.refresh()
        self.app.go("discover")
