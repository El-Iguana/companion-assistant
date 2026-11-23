"""The Studio — create or edit a character (Ember's "Character Studio").

A full page: the form on the left in sections, a phone-shaped live preview on the right
(the profile header, the greeting bubble and the starter chips, updated as you type).
Everything the classic card editor did is here:

* fields, greetings (a repeater, not one-per-line), starters, lorebook entries;
* expression sprites (generate a set as a background job, per-sprite regenerate and
  delete, at in-chat portrait framing) and the 3D avatar (VRM import with its audit);
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

STYLES = ["anime", "none", "photorealistic", "fantasy_art", "cinematic", "dark_fantasy"]
SECTIONS = [("basics", "Basics"), ("persona", "Personality & tone"), ("greet", "Greetings"),
            ("lore", "Lorebook"),
            ("avatar", "3D avatar"), ("voice", "Voice"), ("advanced", "Advanced")]
JOB_POLL_MS = 2000


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
        self.job = None
        self._poll = None
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
        self._stop_poll()
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
        if r.get("job"):
            self._follow_job(r["job"])

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
        self._sync_sprite_opts()

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

    # --- sprites -------------------------------------------------------------------
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

    def _sprites_html(self) -> str:
        """The sprite panel, wrapped so it can be redrawn (after a save adds or removes the
        portrait) without touching the rest of the Studio."""
        return f'<div id="st-spr-panel">{self._sprites_inner()}</div>'

    def _sprites_inner(self) -> str:
        c = self.data["card"]
        if self.data["is_new"]:
            return '<p class="pt-dim">Save the character first — sprites are generated onto a saved card.</p>'
        cps = self.data.get("checkpoints") or []
        kontext_ok = bool(self.data.get("kontext"))
        has_portrait = bool(c.get("has_portrait"))
        # Remembered from the last set: where it came from and how its emotions were made.
        src = "portrait" if (c.get("expr_from_upload") and has_portrait) else "prompt"
        if has_portrait and not (c.get("sprites") or []):
            src = "portrait"                          # a portrait and no sprites yet: the obvious start
        method = "kontext" if (kontext_ok and c.get("expr_kontext", True)) else "img2img"
        # Default model: the one this card's sprites were made with (so a single regen
        # matches the set), else FLUX when it's installed, else the first one.
        want = c.get("expr_checkpoint") if c.get("expr_checkpoint") in cps else next(
            (x for x in cps if "flux" in x.lower()), cps[0] if cps else "")
        cp_opts = "".join(f'<option value="{esc(x)}"{" selected" if x == want else ""}>{esc(x)}</option>'
                          for x in cps) or '<option value="">ComfyUI is not running</option>'
        styles = "".join(f'<option value="{s}">{s}</option>' for s in STYLES)
        denoise = float(c.get("expr_denoise") or 0.55)

        def seg(name, value, label, on, disabled=False, title=""):
            return (f'<label title="{esc(title)}"><input type="radio" name="{name}" value="{value}"'
                    f'{" checked" if on else ""}{" disabled" if disabled else ""}><span>{label}</span></label>')

        cid = esc(c["id"])
        bust = esc(c.get("updated_at", ""))
        thumb = (f'<img class="pt-spr-thumb" src="/pmedia/card/{cid}.png?upload=1&v={bust}" alt="The uploaded portrait">'
                 if has_portrait else "")
        no_portrait = ('<p class="pt-dim" id="st-spr-noportrait">No portrait yet: upload one under '
                       '<b>Choose portrait</b> under Basics. It is saved for you when you generate.</p>')
        return (
            '<div class="pt-spr-choice">'
            '<div class="pt-field"><span>Make sprites from</span><div class="pt-seg">'
            + seg("st-spr-src", "portrait", "The portrait", src == "portrait",
                  title="Every sprite is the uploaded portrait with a different expression.")
            + seg("st-spr-src", "prompt", "The appearance prompt", src == "prompt",
                  title="A new picture is drawn from the appearance prompt, then given each expression.")
            + '</div></div>'
            '<div class="pt-field"><span>Change the expression with</span><div class="pt-seg">'
            + seg("st-spr-method", "kontext", "Flux Kontext", method == "kontext", not kontext_ok,
                  "Edits the picture by instruction: only the expression changes, so the likeness holds. "
                  "About 50 s per sprite." if kontext_ok else "Flux Kontext isn't installed in ComfyUI.")
            + seg("st-spr-method", "img2img", "A model (img2img)", method == "img2img",
                  title="Redraws the picture with the model below. Faster (about 15 s per sprite); "
                        "the expression is subtler and the likeness drifts more.")
            + '</div></div></div>'
            f'<div class="pt-spr-portrait" id="st-spr-portrait">{thumb or no_portrait}'
            + ('<small class="pt-dim">The neutral sprite is the portrait itself; each other '
               'expression is made from it.</small>' if thumb else '') + '</div>'
            '<div class="pt-row3">'
            f'<label class="pt-field" id="st-spr-model"><span>Model</span><select id="st-checkpoint">{cp_opts}</select></label>'
            f'<label class="pt-field"><span>Style</span><select id="st-style">{styles}</select></label>'
            '<label class="pt-field"><span>Set</span><select id="st-set"><option value="core">Core 8 — fast</option>'
            '<option value="full">Full 28 — SillyTavern</option></select></label></div>'
            f'<label class="pt-field" id="st-spr-strength"><span>Expression strength · <b id="st-denoise-val">{denoise:.2f}</b></span>'
            f'<input type="range" id="st-denoise" min="0.2" max="0.9" step="0.05" value="{denoise:g}">'
            '<small>Lower keeps the likeness; higher shows the feeling more strongly.</small></label>'
            '<div class="pt-btnrow">'
            '<label class="pt-check"><input type="checkbox" id="st-transparent" checked>Transparent background</label>'
            '<span class="pt-flex"></span>'
            '<button class="pt-btn primary small" data-act="generate">'
            '<i class="mdi mdi-creation"></i><span id="st-gen-label">Generate sprites</span></button></div>'
            '<div class="pt-job" id="st-job" hidden></div>'
            f'<div class="pt-sprites" id="st-sprites">{self._gallery_html()}</div>')

    def _spr_choice(self, name: str) -> str:
        el = self.root.querySelector(f'input[name="{name}"]:checked') if self.root else None
        return str(el.value) if el else ""

    def _sync_sprite_opts(self) -> None:
        """Show what the chosen source and method need, and say what the button will do."""
        if not by_id("st-spr-panel") or self.data.get("is_new"):
            return
        src, method = self._spr_choice("st-spr-src"), self._spr_choice("st-spr-method")
        portrait = src == "portrait"
        has = bool(self.data["card"].get("has_portrait")) or bool(self.portrait)
        cps = self.data.get("checkpoints") or []
        # The model draws the base (prompt source) and does img2img; portrait + Kontext uses none.
        needs_model = not (portrait and method == "kontext")
        by_id("st-spr-portrait").hidden = not portrait
        by_id("st-spr-model").style.visibility = "visible" if needs_model else "hidden"
        by_id("st-spr-strength").hidden = method != "img2img"
        label = by_id("st-gen-label")
        if label:
            label.textContent = "Generate sprites from the portrait" if portrait else "Generate sprites"
        btn = self.root.querySelector('[data-act="generate"]')
        if btn:
            btn.disabled = bool(self.job) or (portrait and not has) or (needs_model and not cps)
            btn.title = ("Upload a portrait first" if portrait and not has
                         else "Start ComfyUI to pick a model" if needs_model and not cps else "")

    def _render_sprite_panel(self) -> None:
        """After a save added or removed the portrait: redraw only the portrait preview.
        Rebuilding the whole panel reset the model, style, set and transparency the user
        had just chosen, and a generate that saved first then ran with the defaults."""
        box = by_id("st-spr-portrait")
        if box is None:
            return
        tmp = js.document.createElement("div")
        tmp.innerHTML = self._sprites_inner()
        part = tmp.querySelector("#st-spr-portrait")
        if part is not None:
            box.innerHTML = part.innerHTML
        self._sync_sprite_opts()

    def _gallery_html(self) -> str:
        have = set(self.data["card"].get("sprites") or [])
        sets = self.data.get("emotions") or {"core": [], "full": []}
        full = any(e not in sets["core"] for e in have)
        target = sets["full"] if full else sets["core"]
        cid = self.data["card"]["id"]
        bust = self.data["card"].get("updated_at", "")
        cells = []
        for emo in target:
            img = (f'<img src="/pmedia/card/{esc(cid)}/expr/{esc(emo)}.png?v={esc(bust)}" alt="{esc(emo)}" loading="lazy">'
                   if emo in have else '<span class="pt-dim">—</span>')
            delete = (f'<button class="pt-mbtn" data-spr-del="{esc(emo)}" title="Delete"><i class="mdi mdi-close"></i></button>'
                      if emo in have else "")
            cells.append(f'<div class="pt-sprite{"" if emo in have else " empty"}"><div class="pt-sprite-img">{img}</div>'
                         f'<div class="pt-sprite-foot"><span>{esc(emo)}</span>'
                         f'<button class="pt-mbtn" data-spr-regen="{esc(emo)}" title="{"Regenerate" if emo in have else "Generate"}">'
                         f'<i class="mdi mdi-autorenew"></i></button>{delete}</div></div>')
        return (f'<div class="pt-dim pt-sprites-count">{len(have)} sprite{"s" if len(have) != 1 else ""} · '
                f'showing {"Full 28" if full else "Core 8"}</div><div class="pt-sprite-grid">{"".join(cells)}</div>')

    def _gen_params(self) -> dict:
        def val(i):
            el = by_id(i)
            return str(el.value) if el else ""
        source = self._spr_choice("st-spr-src") or "prompt"
        method = self._spr_choice("st-spr-method") or "img2img"
        return {"source": source, "method": method,
                # Portrait + Kontext uses no model; sending none says so.
                "checkpoint": "" if (source == "portrait" and method == "kontext") else val("st-checkpoint"),
                "style": val("st-style") or "anime",
                "preset": val("st-set") or "core", "transparent": bool(by_id("st-transparent").checked),
                "denoise": float(val("st-denoise") or 0.55),
                "appearance": str(self.root.querySelector('[data-f="appearance"]').value or "")}

    async def _generate(self, emotion: str = "") -> None:
        if self.job:
            toast("A generation is already running for this character.", "warning")
            return
        params = self._gen_params()
        if not emotion and params["source"] == "portrait" and (self.portrait or self.remove_portrait):
            # A portrait picked (or removed) but not saved: the server only knows the saved
            # one, so save first rather than generate from the old picture.
            if self.remove_portrait:
                toast("The portrait is marked for removal — save, then upload one to generate from.", "warning")
                return
            await self._save()
            if self.dirty or self.portrait:
                return                                   # the save failed and said why
            params = self._gen_params()
        if not emotion and self.data["card"].get("sprites"):
            if not await message.confirm(
                    "Generate a new set? It replaces every sprite this character has when it finishes.",
                    title="Generate sprites", ok_text="Generate"):
                return
        r = await (Studio().regenerate(self.card_id, emotion, params) if emotion
                   else Studio().generate(self.card_id, params))
        if failed(r, "start generating"):
            return
        self._follow_job(r["job"])

    def _follow_job(self, job: dict) -> None:
        self.job = job
        self._render_job()
        self._stop_poll()
        proxy = create_proxy(lambda *_: spawn(self._poll_job(), "job"))
        self._keep.append(proxy)
        self._poll = js.setInterval(proxy, JOB_POLL_MS)

    def _stop_poll(self) -> None:
        if self._poll is not None:
            js.clearInterval(self._poll)
            self._poll = None

    async def _poll_job(self) -> None:
        if not self.job:
            return
        r = await Studio().job(self.job["id"])
        if not isinstance(r, dict) or not r.get("ok"):
            self._stop_poll()
            self.job = None
            self._render_job(error=(r or {}).get("error", "the job vanished"))
            return
        self.job = r["job"]
        self._render_job()
        if self.job["state"] != "running":
            self._stop_poll()
            job, self.job = self.job, None
            if job["state"] == "done":
                toast(f"{job['label'].replace('Generating', 'Generated')} — done.", "success")
                s = await Studio().sprites(self.card_id)
                if isinstance(s, dict) and s.get("ok"):
                    self.data["card"]["sprites"] = s["sprites"]
                    self.data["card"]["updated_at"] = str(js.Date.now())     # bust the image cache
                    box = by_id("st-sprites")
                    if box:
                        box.innerHTML = self._gallery_html()
            self._render_job(final=job)

    def _render_job(self, final: dict = None, error: str = "") -> None:
        box = by_id("st-job")
        if not box:
            return
        job = final or self.job
        box.hidden = not (job or error)
        self._sync_sprite_opts()                     # the button: job running, portrait, model
        if error:
            box.innerHTML = f'<i class="mdi mdi-alert"></i>{esc(error)}'
            return
        if not job:
            return
        mins, secs = divmod(int(job.get("elapsed") or 0), 60)
        if job["state"] == "running":
            box.innerHTML = (f'<span class="pt-spin small"></span>{esc(job["label"])}… '
                             f'<span class="pt-dim">{mins}:{secs:02d} — a full set takes a few minutes; '
                             'you can keep editing, or leave and come back.</span>')
        elif job["state"] == "done":
            box.innerHTML = f'<i class="mdi mdi-check"></i>{esc(job["label"])} finished in {mins}:{secs:02d}.'
        else:
            box.innerHTML = f'<i class="mdi mdi-alert"></i>{esc(job["label"])} failed: {esc(job.get("error"))}'

    async def _delete_sprite(self, emo: str) -> None:
        if not await message.confirm(f'Delete the "{emo}" sprite?', title="Delete sprite", ok_text="Delete", danger=True):
            return
        r = await Studio().delete_sprite(self.card_id, emo)
        if failed(r, "delete the sprite"):
            return
        self.data["card"]["sprites"] = [e for e in self.data["card"].get("sprites") or [] if e != emo]
        by_id("st-sprites").innerHTML = self._gallery_html()

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
            box.innerHTML = ('<span class="pt-dim">No model assigned — this character uses expression sprites. '
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
        box.textContent = f"Importing {f.name}… (the model is audited before it is kept)"
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
        if src == "sprite" and saved:
            spr = self._field("portrait_sprite") or "neutral"
            if spr in (c.get("sprites") or []):
                return (f"/pmedia/card/{js.encodeURIComponent(c['id'])}/expr/{js.encodeURIComponent(spr)}.png"
                        f"?v={js.encodeURIComponent(c.get('updated_at', ''))}")
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
        elif t.id == "st-denoise":
            by_id("st-denoise-val").textContent = f"{float(t.value):.2f}"
            return
        if t.id in ("st-checkpoint", "st-style", "st-set", "st-transparent") or \
                attr(t, "name") in ("st-spr-src", "st-spr-method"):
            return
        self._mark_dirty()
        if f in ("name", "creator_notes", "personality") or t.hasAttribute("data-greet") or t.hasAttribute("data-starter"):
            self._render_preview()

    def _on_change(self, ev) -> None:
        t = ev.target
        if t.id == "st-portrait-file":
            self._pick_portrait(t)
        elif t.id == "st-vrm-file":
            spawn(self._upload_vrm(), "vrm upload")
        elif attr(t, "name") == "st-avatar":
            self._sync_avatar_mode()
            self._mark_dirty()
        elif attr(t, "data-f") == "vrm":
            self._render_vrm_info()
        elif attr(t, "name") in ("st-spr-src", "st-spr-method"):
            self._sync_sprite_opts()
        elif attr(t, "data-f") in ("portrait_source", "portrait_sprite"):
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
            self._sync_sprite_opts()
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
        regen = closest(ev, "[data-spr-regen]")
        if regen:
            spawn(self._generate(attr(regen, "data-spr-regen")), "sprite")
            return
        sdel = closest(ev, "[data-spr-del]")
        if sdel:
            spawn(self._delete_sprite(attr(sdel, "data-spr-del")), "sprite delete")
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
            self._sync_sprite_opts()
        elif which == "generate":
            spawn(self._generate(), "generate")
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
            self.app.go(f"create/{r['id']}")          # now a saved card: sprites unlock
            return
        if self.portrait or self.remove_portrait:
            # The sprite panel offers "from the portrait" only when there is one: it used to
            # read has_portrait once, at load, so a portrait added here never unlocked it.
            c["has_portrait"] = bool(self.portrait)
            c["updated_at"] = r.get("updated_at") or str(js.Date.now())
            self.portrait, self.remove_portrait = "", False
            self._render_sprite_panel()
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
