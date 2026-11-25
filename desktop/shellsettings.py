"""The resident's own Settings (RESIDENT_PLAN.md R6.2): what she does at start, on this machine.

Kept here, not in the account, because they are about this machine and must work with the
server down:

  start at login       the two systemd user units are enabled (resident/install.sh writes
                       them). The source of truth is systemd, so it is not in the file.
  show_at_start        off: she starts in the tray (see-through, still listening)
  listening_at_start   the wake word is on as soon as she starts
  dance_to_music       she dances to music at its tempo (#116); the tray's "Dance to the
                       music" switches it, at once, and it is kept here. Off by default
  dance_clip           which dance (#132): "" Auto (a clip that fits each song), "generated"
                       (the simple bounce, no clip), or a clip's id (always that one)

The file is ~/.config/pantheon-desktop/settings.json, beside geometry.json. Everything above
the window class is plain Python, tested without GTK (tests/test_shell_settings.py).
"""
from __future__ import annotations

import json
import os
import subprocess

UNITS = ("cantinero-resident.service", "cantinero-shell.service")
DEFAULTS = {"show_at_start": True, "listening_at_start": True, "dance_to_music": False,
            "dance_clip": ""}
GENERATED = "generated"


def path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "pantheon-desktop", "settings.json")


def load() -> dict:
    prefs = dict(DEFAULTS)
    try:
        with open(path(), encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            prefs.update({k: saved[k] for k, v in DEFAULTS.items() if isinstance(saved.get(k), type(v))})
    except (OSError, ValueError):
        pass                                     # none yet, or unreadable: the defaults
    return prefs


def save(prefs: dict) -> None:
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: type(v)(prefs.get(k, v)) for k, v in DEFAULTS.items()}, f, indent=1)
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


# --- dancing (#132) --------------------------------------------------------------------------

TEMPO_MIN, TEMPO_MAX = 50, 220


def dance_choices(clips: list) -> list:
    """The dance selector's entries: (value, label), Auto and the simple bounce first."""
    out = [("", "Auto (a dance that fits each song)"), (GENERATED, "Simple bounce (no dance clip)")]
    return out + [(c["id"], c.get("name") or c["id"]) for c in sorted(clips, key=lambda c: (c.get("name") or "").lower())]


def fits(bpm: float) -> str:
    """What a dance at this tempo fits in Auto: one move per song beat, at 0.8-1.25x."""
    return f"In Auto it dances to songs of about {round(bpm * 0.8)}–{round(bpm * 1.25)} BPM."


def SettingsWindow(app, prefs: dict, on_change, on_exit=None, dance=None):
    """The window, opened from the tray. Each switch applies at once. ``on_change`` gets only
    what changed: the tray's switches change the same settings while this is open.
    ``dance``: ``clips()`` (the dance clips: id, name, bpm) and ``set_tempo(id, bpm)``."""
    from gi.repository import Gdk, GLib, Gtk

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
            on_change({key: bool(want)})       # only this: the tray may have changed others
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

    if dance is not None:
        _dance_section(box, prefs, on_change, dance, Gtk, GLib)

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


def _dance_section(box, prefs: dict, on_change, dance, Gtk, GLib):
    """Which dance she does, and the chosen dance's own tempo (#132). The tempo is the dance's,
    on the server: slide it while she dances until her moves land on the beat, and Auto then
    fits that dance to the right songs, on every machine."""
    head = Gtk.Label(xalign=0, margin_top=6)
    head.set_markup("<b>Dancing</b>")
    box.append(head)
    hint = Gtk.Label(label="She dances when the tray's \u201cDance to the music\u201d is on.",
                     xalign=0, wrap=True, max_width_chars=48)
    hint.add_css_class("dim-label")
    box.append(hint)

    try:
        clips = {c["id"]: dict(c) for c in dance.clips()}
    except Exception as e:                              # noqa: BLE001 — the server may be down
        print(f"[settings] dance clips: {e}")
        clips = {}
    choices = dance_choices(list(clips.values()))
    pick = Gtk.DropDown.new_from_strings([label for _, label in choices])
    values = [v for v, _ in choices]
    current = prefs.get("dance_clip", "")
    pick.set_selected(values.index(current) if current in values else 0)
    line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    line.append(Gtk.Label(label="Dance", xalign=0, hexpand=True))
    line.append(pick)
    box.append(line)

    tempo = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, TEMPO_MIN, TEMPO_MAX, 1)
    tempo.set_digits(0)
    tempo.set_draw_value(True)
    tempo.set_hexpand(True)
    tline = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    tline.append(Gtk.Label(label="Tempo", xalign=0))
    tline.append(tempo)
    box.append(tline)
    info = Gtk.Label(xalign=0, wrap=True, max_width_chars=48)
    info.add_css_class("dim-label")
    box.append(info)

    state = {"loading": False, "timer": 0}

    def show():
        value = values[pick.get_selected()] if pick.get_selected() < len(values) else ""
        clip = clips.get(value)
        tempo.set_sensitive(clip is not None)
        if clip is None:
            info.set_label("Auto picks a dance whose tempo fits the song, else the simple bounce."
                           if value == "" else "Her own bounce and sway, on the song's beats.")
            return
        state["loading"] = True
        tempo.set_value(float(clip["bpm"]))
        state["loading"] = False
        info.set_label("Her moves per minute. Slide it while she dances until they land on the "
                       "beat; it's saved for this dance. " + fits(clip["bpm"]))

    def on_pick(*_):
        value = values[pick.get_selected()] if pick.get_selected() < len(values) else ""
        prefs["dance_clip"] = value
        on_change({"dance_clip": value})
        show()

    def save(clip_id, bpm):
        state["timer"] = 0
        try:
            got = dance.set_tempo(clip_id, bpm)
            clips[clip_id]["bpm"] = float((got or {}).get("bpm") or bpm)
            info.set_label("Saved. " + fits(clips[clip_id]["bpm"]))
        except Exception as e:                          # noqa: BLE001
            info.set_label(f"Not saved: {e}")
        return False

    def on_tempo(_scale):
        value = values[pick.get_selected()] if pick.get_selected() < len(values) else ""
        if state["loading"] or value not in clips:
            return
        if state["timer"]:
            GLib.source_remove(state["timer"])
        bpm = round(tempo.get_value())
        state["timer"] = GLib.timeout_add(400, save, value, bpm)   # once the slider rests

    pick.connect("notify::selected", on_pick)
    tempo.connect("value-changed", on_tempo)
    show()
