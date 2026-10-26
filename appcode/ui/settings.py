"""Settings (one page, in sections), the in-app Guide, and — for admins — Users.

Every knob is the classic page's ⚙ Settings, with its explanation: thinking, the memory
tuning, recall and automatic facts, and the voice/hands-free settings. The voice ones are
already in use today by the classic page and the desktop avatar, so they are editable
here even though the new page's own voice controls arrive in Phase 6. The microphone
picker comes with them (it needs the browser's device list, granted mic access).
"""
from __future__ import annotations

import js

from wapyt import message

from bff.account import Account
from ui.common import Listeners, attr, by_id, closest, esc, failed, spawn, toast

# (key, label, kind, extra, hint). kind: check | range(min, max, step, fmt) | text | select(options)
GROUPS = [
    ("model", "Model", [
        ("disable_thinking", "Disable model thinking (faster replies)", "check", None,
         "Skips Qwen3's hidden &lt;think&gt; reasoning so the whole reply budget goes to the message. "
         "Turn off if replies feel less thoughtful."),
    ]),
    ("memory", "Memory", [
        ("summarize_after", "Summarize after", "range", (6, 60, 2, "{} messages"),
         "Fold older turns into the running summary once this many un-summarized messages pile up. "
         "Lower summarizes sooner (cheaper prompt, more lossy)."),
        ("keep_recent", "Keep recent verbatim", "range", (2, 40, 2, "{} messages"),
         "The most recent turns are always sent word for word, never summarized."),
        ("summary_max_tokens", "Summary length", "range", (100, 1000, 50, "{} tokens"),
         "Token budget for the “story so far”. Raise it if summaries feel truncated."),
        ("recall_enabled", "Semantic recall", "check", None,
         "Look up related moments from earlier chats and slip the few that match into the context. "
         "Runs on the CPU; uses no VRAM."),
        ("recall_top_k", "Recall depth", "range", (1, 10, 1, "{} snippets"),
         "How many remembered snippets may be injected per turn. Shares the long-term budget."),
        ("auto_facts_enabled", "Remember facts automatically", "check", None,
         "Pull durable facts (names, allergies, preferences) out of finished conversations into "
         "long-term memory. They show as AUTO in a chat's Memory, where you can edit or remove them."),
        ("auto_facts_max_new", "Facts per conversation", "range", (1, 10, 1, "{} at most"),
         "Ceiling on how many may be added from one swept conversation."),
    ]),
    ("voice", "Voice &amp; hands-free", [
        ("wake_word", "Wake word / phrase", "text", "e.g. hey luna (blank = always listen)",
         "In hands-free mode only speech that starts with this is sent. A character's own wake word "
         "overrides it (Studio → Voice)."),
        ("wake_mode", "When you say the wake word", "select",
         [("phrase", "Every time — each request starts with her name"),
          ("continuous", "Start a conversation — follow up without repeating it")], ""),
        ("sleep_phrase", "Sleep phrase", "text", "that will be all", "Ends a continuous conversation."),
        ("farewell", "She says", "text", "Goodbye.", "Spoken when a conversation closes. Blank = nothing."),
        ("sleep_after_s", "Stop listening after", "range", (0, 900, 30, "{} s"),
         "Silence that closes the conversation on its own. 0 = never — an open mic keeps "
         "transcribing the room after you walk away."),
        ("vad_threshold", "Mic sensitivity", "range", (0.005, 0.05, 0.001, "{:.3f}"),
         "Lower picks up quieter speech (and may false-trigger on noise)."),
        ("vad_silence_ms", "End-of-speech silence", "range", (300, 2000, 100, "{} ms"),
         "How long a pause ends your turn."),
        ("full_duplex", "Let me interrupt her while she speaks", "check", None,
         "Only with headphones or an echo-cancelling mic (aec_source) — otherwise she hears her "
         "own voice and replies to herself."),
        ("desktop_card_id", "Desktop-mode character", "chars", None,
         "Who appears in the corner of the screen at /desktop."),
    ]),
]


def _fmt(fmt: str, v) -> str:
    try:
        return fmt.format(float(v)) if "{:" in fmt else fmt.format(int(float(v)) if float(v).is_integer() else v)
    except (TypeError, ValueError):
        return str(v)


class SettingsView:
    def __init__(self, app, arg: str = "") -> None:
        self.app = app
        self.root = None
        self.data: dict = {}
        self.users: dict = {}
        self.listeners = Listeners()

    def mount(self, root) -> None:
        self.root = root
        root.innerHTML = '<div class="pt-chat-loading"><span class="pt-spin"></span></div>'
        self.listeners.on(root, "click", self._on_click)
        self.listeners.on(root, "input", self._on_input)
        spawn(self.load(), "settings")

    def unmount(self) -> None:
        self.listeners.clear()
        self.root = None

    async def load(self) -> None:
        r = await Account().settings()
        if self.root is None or failed(r, "load your settings"):
            return
        self.data = r
        self.render()
        if r["me"]["role"] == "admin":
            await self.load_users()

    def _control(self, key, label, kind, extra, hint) -> str:
        v = self.data["settings"].get(key)
        if kind == "check":
            return (f'<label class="pt-setting check"><input type="checkbox" data-s="{key}"{" checked" if v else ""}>'
                    f'<span><b>{label}</b><small>{hint}</small></span></label>')
        if kind == "range":
            lo, hi, step, fmt = extra
            return (f'<label class="pt-setting"><span><b>{label}</b> · <output data-o="{key}" data-fmt="{esc(fmt)}">{esc(_fmt(fmt, v))}</output></span>'
                    f'<input type="range" data-s="{key}" data-kind="num" min="{lo}" max="{hi}" step="{step}" value="{v}">'
                    f'<small>{hint}</small></label>')
        if kind == "text":
            return (f'<label class="pt-setting"><span><b>{label}</b></span>'
                    f'<input type="text" data-s="{key}" value="{esc(v or "")}" placeholder="{esc(extra or "")}">'
                    + (f'<small>{hint}</small>' if hint else "") + '</label>')
        if kind in ("select", "chars"):
            options = extra if kind == "select" else (
                [("", "her most recent chat's character")] + [(c["id"], c["name"]) for c in self.data.get("characters") or []])
            opts = "".join(f'<option value="{esc(o)}"{" selected" if str(o) == str(v or "") else ""}>{esc(t)}</option>'
                           for o, t in options)
            return (f'<label class="pt-setting"><span><b>{label}</b></span><select data-s="{key}">{opts}</select>'
                    + (f'<small>{hint}</small>' if hint else "") + '</label>')
        return ""

    def render(self) -> None:
        me = self.data["me"]
        groups = "".join(
            f'<section class="pt-sec" id="set-{gid}"><h2>{title}</h2>'
            + (('<p class="pt-dim">Used by the classic page and the desktop avatar today; the new page\'s own '
                'voice controls arrive in Phase 6. The microphone picker comes with them.</p>') if gid == "voice" else "")
            + "".join(self._control(*row) for row in rows) + '</section>'
            for gid, title, rows in GROUPS)
        self.root.innerHTML = (
            '<div class="pt-page pt-settings"><div class="pt-studio-head"><div><div class="pt-kicker">pantheon</div>'
            '<h1 class="pt-h1">Settings</h1></div>'
            '<div class="pt-studio-headbtns"><a class="pt-btn" href="#/help"><i class="mdi mdi-book-open-variant"></i>Guide</a></div></div>'
            '<section class="pt-sec" id="set-account"><h2>Account</h2>'
            f'<p>Signed in as <b>{esc(me["username"])}</b>{" · admin" if me["role"] == "admin" else ""}.</p>'
            '<div class="pt-row3">'
            '<label class="pt-setting"><span><b>Current password</b></span><input type="password" id="pw-cur" autocomplete="current-password"></label>'
            '<label class="pt-setting"><span><b>New password</b></span><input type="password" id="pw-new" autocomplete="new-password"></label>'
            '<label class="pt-setting"><span>&nbsp;</span><button class="pt-btn" data-act="password">Change password</button></label>'
            '</div></section>'
            + groups
            + ('<section class="pt-sec" id="set-users"><h2>Users <small>each has a private library</small></h2>'
               '<div id="set-users-list"><span class="pt-spin small"></span></div></section>' if me["role"] == "admin" else "")
            + '<div class="pt-studio-bar"><span class="pt-dim" id="set-status"></span><span class="pt-flex"></span>'
            '<button class="pt-btn primary" data-act="save"><i class="mdi mdi-content-save-outline"></i>Save settings</button></div>'
            '</div>')

    async def load_users(self) -> None:
        r = await Account().users()
        box = by_id("set-users-list")
        if not box or failed(r, "list accounts"):
            return
        self.users = r
        rows = []
        for u in r.get("users") or []:
            me = u["id"] == r.get("me")
            rows.append(
                f'<div class="pt-urow"><i class="mdi mdi-account-circle-outline"></i><b>{esc(u["username"])}</b>'
                f'<span class="pt-tag">{esc(u["role"])}</span>{"<span class=pt-dim>you</span>" if me else ""}'
                '<span class="pt-flex"></span>'
                f'<button class="pt-btn small" data-reset="{esc(u["id"])}" data-name="{esc(u["username"])}">Reset password</button>'
                + ("" if me else f'<button class="pt-btn small danger" data-udel="{esc(u["id"])}" data-name="{esc(u["username"])}">Delete</button>')
                + '</div>')
        box.innerHTML = (
            "".join(rows)
            + '<div class="pt-row3 pt-newuser">'
            '<label class="pt-setting"><span><b>Username</b></span><input type="text" id="nu-name" autocomplete="off"></label>'
            '<label class="pt-setting"><span><b>Password</b></span><input type="password" id="nu-pass" autocomplete="new-password"></label>'
            '<label class="pt-setting"><span><b>Role</b></span><select id="nu-role"><option value="user">user</option>'
            '<option value="admin">admin</option></select></label></div>'
            '<button class="pt-btn" data-act="add-user"><i class="mdi mdi-account-plus-outline"></i>Add user</button>')

    def _on_input(self, ev) -> None:
        key = attr(ev.target, "data-s")
        if not key:
            return
        out = self.root.querySelector(f'[data-o="{key}"]')
        if out:
            out.textContent = _fmt(attr(out, "data-fmt"), ev.target.value)
        by_id("set-status").textContent = "Unsaved changes"

    def collect(self) -> dict:
        values = {}
        for el in self.root.querySelectorAll("[data-s]"):
            key = attr(el, "data-s")
            if str(el.type) == "checkbox":
                values[key] = bool(el.checked)
            elif attr(el, "data-kind") == "num":
                n = float(el.value)
                values[key] = int(n) if n.is_integer() and key != "vad_threshold" else n
            else:
                values[key] = str(el.value)
        return values

    def _on_click(self, ev) -> None:
        act = closest(ev, "[data-act]")
        if act:
            which = attr(act, "data-act")
            if which == "save":
                spawn(self._save(), "save settings")
            elif which == "password":
                spawn(self._password(), "password")
            elif which == "add-user":
                spawn(self._add_user(), "add user")
            return
        reset = closest(ev, "[data-reset]")
        if reset:
            spawn(self._reset(attr(reset, "data-reset"), attr(reset, "data-name")), "reset password")
            return
        udel = closest(ev, "[data-udel]")
        if udel:
            spawn(self._delete_user(attr(udel, "data-udel"), attr(udel, "data-name")), "delete user")

    async def _save(self) -> None:
        r = await Account().save_settings(self.collect())
        if failed(r, "save settings"):
            return
        by_id("set-status").textContent = "Saved"
        toast("Settings saved.", "success")

    async def _password(self) -> None:
        cur, new = str(by_id("pw-cur").value), str(by_id("pw-new").value)
        if len(new) < 4:
            toast("The new password needs at least 4 characters.", "warning")
            return
        r = await Account().change_password(cur, new)
        if failed(r, "change the password"):
            return
        by_id("pw-cur").value = ""
        by_id("pw-new").value = ""
        toast("Password changed — for both pages.", "success")

    async def _add_user(self) -> None:
        name, pw, role = str(by_id("nu-name").value).strip(), str(by_id("nu-pass").value), str(by_id("nu-role").value)
        r = await Account().create_user(name, pw, role)
        if failed(r, "add the user"):
            return
        toast(f"{name} added.", "success")
        await self.load_users()

    async def _reset(self, uid: str, name: str) -> None:
        pw = await message.prompt(f"New password for {name} (at least 4 characters):",
                                  title="Reset password", password=True)
        if not pw:
            return
        r = await Account().reset_password(uid, pw)
        if not failed(r, "reset the password"):
            toast(f"{name}'s password was reset.", "success")

    async def _delete_user(self, uid: str, name: str) -> None:
        if not await message.confirm(f"Delete {name} and their entire private library — characters, chats, "
                                     "memories? This cannot be undone.", title="Delete user", ok_text="Delete",
                                     danger=True):
            return
        r = await Account().delete_user(uid)
        if not failed(r, f"delete {name}"):
            toast(f"{name} deleted.", "success")
            await self.load_users()


class HelpView:
    """The guide — the same text the classic page shows (templates/help.html)."""

    def __init__(self, app, arg: str = "") -> None:
        self.app = app
        self.root = None
        self.listeners = Listeners()

    def mount(self, root) -> None:
        self.root = root
        root.innerHTML = '<div class="pt-page pt-help"><div class="pt-kicker">guide</div><div id="help-body">…</div></div>'
        self.listeners.on(root, "click", self._on_click)
        from ui.common import spawn as _spawn

        _spawn(self.load(), "help")

    def unmount(self) -> None:
        self.listeners.clear()
        self.root = None

    async def load(self) -> None:
        resp = await js.fetch("/pmedia/help.html", js.JSON.parse('{"credentials":"same-origin"}'))
        if self.root is None:
            return
        # Our own page from our own template — trusted markup, not user content.
        by_id("help-body").innerHTML = str(await resp.text()) if resp.ok else "The guide could not be loaded."

    def _on_click(self, ev) -> None:
        btn = closest(ev, ".help-copy")
        if not btn:
            return
        pre = btn.closest(".help-example").querySelector("pre")
        js.navigator.clipboard.writeText(pre.textContent)
        btn.textContent = "Copied ✓"
