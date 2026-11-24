"""The resident's own Settings (RESIDENT_PLAN.md R6.2): what she does at start, on this machine.

Kept here, not in the account, because they are about this machine and must work with the
server down:

  start at login       the two systemd user units are enabled (resident/install.sh writes
                       them). The source of truth is systemd, so it is not in the file.
  show_at_start        off: she starts in the tray (see-through, still listening)
  listening_at_start   the wake word is on as soon as she starts

The file is ~/.config/pantheon-desktop/settings.json, beside geometry.json. Everything above
the window class is plain Python, tested without GTK (tests/test_shell_settings.py).
"""
from __future__ import annotations

import json
import os
import subprocess

UNITS = ("cantinero-resident.service", "cantinero-shell.service")
DEFAULTS = {"show_at_start": True, "listening_at_start": True}


def path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "pantheon-desktop", "settings.json")


def load() -> dict:
    prefs = dict(DEFAULTS)
    try:
        with open(path(), encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            prefs.update({k: saved[k] for k in DEFAULTS if isinstance(saved.get(k), bool)})
    except (OSError, ValueError):
        pass                                     # none yet, or unreadable: the defaults
    return prefs


def save(prefs: dict) -> None:
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: bool(prefs.get(k, v)) for k, v in DEFAULTS.items()}, f, indent=1)
    os.replace(tmp, p)                           # atomic


def page_script(prefs: dict) -> str:
    """What the page is told before its own scripts run (desktop.js: window.__cantineroShell)."""
    return "window.__cantineroShell = %s;" % json.dumps(
        {"listening": bool(prefs.get("listening_at_start", True))})


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10)


def login_state() -> str:
    """"on", "off", or "none" when the units aren't installed (a shell run by hand)."""
    try:
        states = [_systemctl("is-enabled", u).stdout.strip() for u in UNITS]
    except (OSError, subprocess.SubprocessError):
        return "none"
    if any(s in ("", "not-found") for s in states):
        return "none"
    return "on" if all(s == "enabled" for s in states) else "off"


def set_login(on: bool) -> str:
    """Enable or disable both units (never starts or stops them). Returns the new state."""
    try:
        r = _systemctl("enable" if on else "disable", *UNITS)
        if r.returncode:
            print(f"[settings] systemctl: {r.stderr.strip()}")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[settings] systemctl: {e}")
    return login_state()


def SettingsWindow(app, prefs: dict, on_change, on_exit=None):
    """The window, opened from the tray. Each switch applies at once."""
    from gi.repository import Gdk, Gtk

    # The shell makes every window transparent for the avatar; a class wins over that, as the
    # music window's does. The same colours as the music window.
    css = Gtk.CssProvider()
    css.load_from_data(b".cs-window { background: #1b1714; color: #f3e9dc; }"
                       b" .cs-window .dim-label { color: #a8957c; }")
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    prefs = dict(prefs)
    win = Gtk.Window(application=app, title="Cantinero settings")
    win.add_css_class("cs-window")
    win.set_default_size(420, -1)
    win.set_resizable(False)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(18)

    head = Gtk.Label(xalign=0)
    head.set_markup("<b>When she starts</b>")
    box.append(head)

    def row(title: str, detail: str, active: bool, on_set) -> Gtk.Switch:
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        text.append(Gtk.Label(label=title, xalign=0))
        sub = Gtk.Label(label=detail, xalign=0, wrap=True, max_width_chars=44)
        sub.add_css_class("dim-label")
        text.append(sub)
        sw = Gtk.Switch(active=active, valign=Gtk.Align.CENTER)
        sw.connect("state-set", on_set)
        line.append(text)
        line.append(sw)
        box.append(line)
        return sw

    state = login_state()

    def on_login(sw, want):
        now = set_login(want)
        sw.set_state(now == "on")              # what systemd says, not what was asked
        return True                            # the state is set here

    login = row("Start at login",
                "Off: she starts only from Cantinero in the app menu (or Meta+C)."
                if state != "none" else "Not installed as a service on this machine.",
                state == "on", on_login)
    login.set_sensitive(state != "none")

    def pref(key):
        def on_set(_sw, want):
            prefs[key] = bool(want)
            on_change(dict(prefs))
            return False                       # let the switch follow
        return on_set

    row("Show the avatar at start",
        "Off: she starts in the tray, still listening. Show her from the tray or with Meta+C.",
        prefs["show_at_start"], pref("show_at_start"))
    row("Listening at start",
        "The wake word is on as soon as she starts. The tray's Listening switches it for "
        "this session only.",
        prefs["listening_at_start"], pref("listening_at_start"))

    note = Gtk.Label(label="These two apply the next time she starts.", xalign=0)
    note.add_css_class("dim-label")
    box.append(note)

    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    if on_exit:
        # Exit lives here too: the tray is easy to miss (the user didn't find Settings there).
        leave = Gtk.Button(label="Exit Cantinero", halign=Gtk.Align.START, hexpand=True,
                           tooltip_text="Stop her, the music and the resident until the next "
                                        "login or Cantinero in the app menu")
        leave.connect("clicked", lambda *_: on_exit())
        buttons.append(leave)
    close = Gtk.Button(label="Close", halign=Gtk.Align.END, hexpand=not on_exit)
    close.connect("clicked", lambda *_: win.close())
    buttons.append(close)
    box.append(buttons)

    win.set_child(box)
    return win
