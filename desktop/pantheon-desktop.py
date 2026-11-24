#!/usr/bin/env python3
"""Pantheon desktop shell (Phase 6).

A borderless, transparent GTK4 window holding a WebKit view of the app's
/desktop page. It contains no application logic at all -- the container on
:7867 stays the brain, and this is a picture frame.

Why GTK4 + WebKitGTK rather than Electron: both are already on this host, and
WebKitGTK was measured to do everything the avatar needs -- WebGL 2.0,
getUserMedia (it found the webcam mic), MediaRecorder for STT, AudioContext
with worklets -- and the avatar renderer was confirmed running inside it.

Why no layer-shell: gtk4-layer-shell is packaged but installing it means
rpm-ostree layering plus a reboot, which this machine's conventions avoid. It
turns out not to be needed. Of the four things a desktop overlay wants, plain
GTK4 on Wayland gives us two directly -- transparency and, via
GdkSurface.set_input_region, click-through -- and the other two, position and
keep-above, a Wayland client is not allowed to set for itself under any
toolkit. Those come from a KWin rule instead (see kwinrule/ next to this file).

Usage:
    ./pantheon-desktop.py [--url URL] [--width N] [--height N] [--no-tray]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import gi

# stdout is a pipe under systemd, so Python block-buffers it and every print()
# in this file was invisible in the journal until the process exited.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:                                        # noqa: BLE001
    pass

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gtk, Gdk, Gio, GLib, WebKit  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tray import Tray, MenuItem  # noqa: E402
from kwin import KWinGeometry  # noqa: E402
import music  # noqa: E402
import shellsettings  # noqa: E402

APP_ID = "ai.jotunheim.pantheon.desktop"
# The tray icon: El Iguana behind the bar. The SNI host looks the icon name up here first.
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
DEFAULT_URL = "http://127.0.0.1:7867/desktop"
# The HUD lives in a strip along the bottom. Only that strip accepts clicks;
# everything above it passes through to whatever is on the desktop behind her.
HUD_STRIP_PX = 56
CORNER = "bottom-right"   # where she sits until dragged (geometry.json "anchor")


class DesktopShell(Gtk.Application):
    def __init__(self, url: str, width: int, height: int, tray: bool, music: bool = True):
        super().__init__(application_id=APP_ID)
        self.url, self.w, self.h, self.want_tray = url, width, height, tray
        self.want_music = music
        self.player = None             # Cantinero's music player (desktop/music.py)
        self.win: Gtk.Window | None = None
        self.view: WebKit.WebView | None = None
        self.click_through = True
        self.position_mode = False
        self.drag_bar = None
        self._mi_through = None
        self._mi_chars = None
        self._char_ids = {}
        self.tray = None
        self.hidden = False            # see-through and click-through (see _hide)
        self.prefs = shellsettings.load()   # this machine's Settings (R6.2)
        self.settings_win = None
        self.hud_extra = 0             # px the page's micro player adds to the clickable strip
        self._music_pushed = ""
        self._exiting = False
        # `cantinero --exit` reaches this over D-Bus (org.gtk.Actions.Activate "exit").
        exit_action = Gio.SimpleAction.new("exit", None)
        exit_action.connect("activate", lambda *_: self._menu_exit())
        self.add_action(exit_action)
        self.icon_name = "cantinero"
        self.kwin = None
        self._geom = None
        self._geom_file = os.path.join(
            GLib.get_user_config_dir(), "pantheon-desktop", "geometry.json")

    # -- lifecycle ---------------------------------------------------------
    def do_activate(self):
        # A second activation is the launcher (desktop/cantinero) asking for her: show her
        # even when she was hidden to the tray, the same way the tray's Show does, and wave,
        # so the shortcut always answers: with her already on screen, showing changes nothing.
        if self.win:
            was_hidden = self._show()
            GLib.timeout_add(300 if was_hidden else 0, self._wave)
            return

        self.win = Gtk.ApplicationWindow(application=self)
        self.win.set_default_size(self.w, self.h)
        self.win.set_decorated(False)
        self.win.set_title("Cantinero")
        # Matched by the KWin rule; keep in step with kwinrule/pantheon.kwinrule.
        self.win.set_name("pantheon-desktop")

        # Cookies must persist or every launch lands on the login screen. The
        # app is login-gated and the shell has no credentials of its own.
        data_dir = os.path.join(
            GLib.get_user_data_dir(), "pantheon-desktop")
        os.makedirs(data_dir, exist_ok=True)
        nm = WebKit.NetworkSession.new(data_dir, data_dir)
        nm.get_cookie_manager().set_persistent_storage(
            os.path.join(data_dir, "cookies.sqlite"),
            WebKit.CookiePersistentStorage.SQLITE)

        self.view = WebKit.WebView(network_session=nm)
        # What she does at start comes from this machine's Settings, handed to the page
        # before its own scripts run (desktop.js reads window.__cantineroShell).
        ucm = self.view.get_user_content_manager()
        ucm.add_script(WebKit.UserScript.new(
            shellsettings.page_script(self.prefs),
            WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.START, None, None))
        # The page's ⚙ button asks for Settings: window.webkit.messageHandlers.cantinero.
        ucm.connect("script-message-received::cantinero", self._on_page_message)
        ucm.register_script_message_handler("cantinero", None)
        s = self.view.get_settings()
        for prop, val in (
            ("enable-webgl", True),
            ("enable-media-stream", True),
            ("enable-mediasource", True),
            ("media-playback-requires-user-gesture", False),
            ("enable-developer-extras", True),
            # Without this a console.error in the page goes nowhere -- there is
            # no devtools window to read, and the journal is the only channel.
            ("enable-write-console-messages-to-stdout", True),
        ):
            try:
                s.set_property(prop, val)
            except Exception:                       # noqa: BLE001
                pass                                # older WebKit: skip quietly

        # Mic and autoplay: this is a local app the user launched deliberately,
        # so a prompt with no window chrome to host it would just be a dead end.
        self.view.connect("permission-request", self._on_permission)
        # Gdk.RGBA() default-constructs to OPAQUE WHITE (1,1,1,1), not to zeroes,
        # so passing a bare one here painted the view solid white -- set every
        # component explicitly.
        transparent = Gdk.RGBA()
        transparent.red = transparent.green = transparent.blue = transparent.alpha = 0.0
        self.view.set_background_color(transparent)

        # Drag-to-position. A Wayland client may not set its own coordinates --
        # but it may ask the compositor for an interactive move, which is what
        # Gtk.WindowHandle does. So instead of editing numbers in a KWin rule,
        # you grab this bar and drag her where you want. It is hidden until
        # position mode is on, so it costs no screen space the rest of the time.
        self.drag_bar = Gtk.WindowHandle()
        grip = Gtk.Label(label="⠿  drag to position  ·  release when placed")
        grip.add_css_class("dim-label")
        grip.set_margin_top(6); grip.set_margin_bottom(6)
        self.drag_bar.set_child(grip)
        self.drag_bar.set_visible(False)

        # Three layers have to agree for the desktop to show through: the page
        # (CSS), the WebView (above) and the GTK window itself, which otherwise
        # paints the theme's background behind everything.
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window, window > box, box { background-color: transparent; background-image: none; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.drag_bar)
        self.view.set_vexpand(True)
        box.append(self.view)
        self.win.set_child(box)
        # The resident's service may not be listening yet: at login both units start
        # together and the container takes a few seconds. A failed load is retried every
        # 3 s rather than leaving an empty window for the session (found on predator,
        # 2026-11-23: the page had never loaded since the install).
        self.view.connect("load-failed", self._on_load_failed)
        # A fresh page has no micro player yet: send it the music state again.
        self.view.connect("load-changed", lambda _v, ev: ev == WebKit.LoadEvent.FINISHED
                          and setattr(self, "_music_pushed", ""))
        self.view.load_uri(self.url)

        self.win.connect("notify::default-width", lambda *_: self._apply_input_region())
        self.win.connect("notify::default-height", lambda *_: self._apply_input_region())
        if not self.prefs["show_at_start"]:
            self.hidden = True                     # in the tray, but mapped: still listening
            self.win.set_opacity(0.0)
        self.win.present()
        GLib.timeout_add(300, self._apply_input_region_once)

        # The resident's music player: always running, so music from any chat plays here.
        cfg = music.resident_config()
        if self.want_music and music.configured(cfg):
            self.player = music.MusicPlayer(self, cfg, int(os.environ.get("RESIDENT_PORT") or 7867))
            self.player.start()
            GLib.timeout_add(500, self._push_music)
        elif self.want_music:
            print("[shell] no music yet: pair the resident, then python -m resident pair-orpheus")

        if self.want_tray:
            self._build_tray()

        # Position persistence. A Wayland client cannot place itself, and KWin's
        # own Remember rule was measured not to write back -- the avatar sat at
        # 1070,387 while the rule still read 1400,380. So we ask the compositor
        # where we are and tell it where to go, and keep the value ourselves.
        self.kwin = KWinGeometry("pantheon", self._on_geometry)
        self.kwin.start()
        GLib.timeout_add(1500, self._restore_geometry)
        # Poll rather than saving only on exit: systemd stops the shell with
        # SIGTERM and it gets killed outright often enough that an exit-only save
        # would lose the very drag it exists to keep.
        GLib.timeout_add_seconds(4, self._poll_geometry)
        # First fill once the page has had time to log in and load, then keep it
        # current so characters added in the main window show up here.
        GLib.timeout_add_seconds(4, self._refresh_characters)
        GLib.timeout_add_seconds(30, self._refresh_characters)

    # -- position persistence ------------------------------------------------
    def _load_geometry(self):
        try:
            with open(self._geom_file, encoding="utf-8") as f:
                g = json.load(f)
            if all(isinstance(g.get(k), int) for k in ("x", "y", "w", "h")):
                # A file from before the corner anchor has no "anchor": it gets the corner,
                # which is where she belongs unless dragged somewhere since.
                g.setdefault("anchor", CORNER)
                return g
        except Exception:                                    # noqa: BLE001
            pass
        return None

    def _anchor(self) -> str:
        g = self._geom or self._load_geometry()
        return g.get("anchor", CORNER) if g else CORNER

    def _restore_geometry(self):
        """Put the window back. Safe to call whenever a surface appears. By default she sits
        in the bottom-right corner; once dragged, where she was dropped."""
        if not self.kwin:
            return False
        g = self._geom or self._load_geometry()
        if self._anchor() == CORNER:
            if g:
                self.kwin.apply(g["x"], g["y"], g["w"], g["h"])   # her size, then the corner
            GLib.timeout_add(300, lambda: self.kwin.anchor() and False)
            print("[shell] restored to the bottom-right corner")
        elif g:
            self.kwin.apply(g["x"], g["y"], g["w"], g["h"])
            print(f"[shell] restored to {g['x']},{g['y']} {g['w']}x{g['h']}")
        return False                                          # never repeats itself

    def _menu_corner(self, *_):
        """Back to the corner: forget the drag."""
        self._set_anchor(CORNER)
        if self.kwin:
            self.kwin.anchor()

    def _set_anchor(self, anchor: str) -> None:
        g = dict(self._geom or self._load_geometry() or {})
        if not g:
            g = {"x": 0, "y": 0, "w": 420, "h": 620}
        g["anchor"] = anchor
        self._geom = None
        self._write_geometry(g)

    def _poll_geometry(self):
        # Only while mapped: a hidden window has no surface, so KWin either
        # reports nothing or reports something meaningless.
        if self.kwin and self.win and self.win.get_visible():
            self.kwin.request()
        return True

    def _on_geometry(self, x, y, w, h):
        g = {"x": x, "y": y, "w": w, "h": h, "anchor": self._anchor()}
        if g == self._geom:
            return                                            # unchanged; no write
        # Ignore a bogus report rather than persist a position that would put her
        # off-screen on the next launch.
        if w < 80 or h < 80:
            return
        self._write_geometry(g)

    def _write_geometry(self, g: dict) -> None:
        self._geom = g
        try:
            os.makedirs(os.path.dirname(self._geom_file), exist_ok=True)
            tmp = self._geom_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(g, f)
            os.replace(tmp, self._geom_file)                  # atomic
        except Exception as e:                                # noqa: BLE001
            print(f"[shell] could not save geometry: {e}")

    def _on_load_failed(self, _view, _event, uri, error):
        if getattr(self, "_retrying", False):
            return True
        self._retrying = True
        print(f"[shell] couldn't load {uri} ({error.message}); trying again in 3 s")

        def again():
            self._retrying = False
            self.view.load_uri(self.url)
            return False
        GLib.timeout_add_seconds(3, again)
        return True                                    # no WebKit error page in her window

    def _on_permission(self, _view, request):
        request.allow()
        return True

    # -- click-through -----------------------------------------------------
    def _apply_input_region_once(self):
        self._apply_input_region()
        return False                                # one shot

    def _apply_input_region(self):
        """Accept clicks only in the HUD strip, or everywhere when pinned.

        A fully input-transparent window would leave no way to reach the HUD,
        and a fully opaque one sits on top of the desktop it is meant to share.
        The strip is the compromise: she never blocks a click, the controls
        stay reachable.
        """
        if not self.win:
            return
        surface = self.win.get_surface()
        if surface is None:
            return
        try:
            import cairo
            w = self.win.get_width() or self.w
            h = self.win.get_height() or self.h
            if self.hidden:
                surface.set_input_region(cairo.Region())          # nothing: clicks pass through
            elif self.click_through and not self.position_mode:
                tall = HUD_STRIP_PX + self.hud_extra
                strip = cairo.RectangleInt(0, max(0, h - tall), w, tall)
                surface.set_input_region(cairo.Region(strip))
            else:
                surface.set_input_region(cairo.Region(cairo.RectangleInt(0, 0, w, h)))
        except Exception as e:                      # noqa: BLE001
            print(f"[shell] input region not applied: {e}", file=sys.stderr)

    def toggle_click_through(self, *_):
        self.click_through = not self.click_through
        self._apply_input_region()

    # -- tray --------------------------------------------------------------
    # StatusNotifierItem straight over D-Bus (see tray.py). AppIndicator3 is
    # available but is GTK3 and wants a Gtk.Menu that GTK4 removed, which would
    # mean two GTK versions in one process.
    def _build_tray(self):
        self._mi_through = MenuItem(
            2, "Click through the avatar", self._menu_click_through,
            toggle=True, checked=self.click_through)
        # Characters live in a submenu, filled in once the page has loaded. The
        # page is the authenticated side -- it holds the session cookie -- so the
        # list is read out of it rather than fetched from the API here.
        self._mi_chars = MenuItem(10, "Character", children=[
            MenuItem(11, "loading…", enabled=False)])
        self._mi_listen = MenuItem(
            7, "Listening", self._menu_listen, toggle=True, checked=False)
        self._mi_position = MenuItem(
            6, "Move her (drag to position)", self._menu_position_mode,
            toggle=True, checked=False)
        items = [
            MenuItem(1, "Show / hide", self._menu_toggle_visible),
            MenuItem(8, "Music player", lambda _i: self.player and self.player.toggle(),
                     enabled=self.player is not None),
            self._mi_chars,
            self._mi_listen,
            self._mi_position,
            MenuItem(9, "Put her in the corner", self._menu_corner),
            self._mi_through,
            MenuItem(3, is_sep=True),
            MenuItem(12, "Settings…", self._menu_settings),
            MenuItem(4, "Reload", lambda _i: self.view and self.view.reload()),
            MenuItem(5, "Exit Cantinero", self._menu_exit),
        ]
        self.tray = Tray(APP_ID, "Cantinero", self.icon_name, items,
                         on_activate=self._menu_toggle_visible, icon_theme_path=ICON_DIR)
        if not self.tray.start():
            self.tray = None

    # Hidden is see-through and click-through, never unmapped. An unmapped WebView is a
    # hidden page, and WebKit throttles it: measured on predator 2026-11-24, the mic
    # loop ran 1 block in 20 s hidden against 20 shown, so hiding her made her deaf to
    # the wake word. Kept mapped, she keeps listening, and her place never changes.
    def _menu_toggle_visible(self, *_):
        if not self.win:
            return
        if self.hidden:
            self._show()
        else:
            self._hide()

    def _on_page_message(self, _ucm, value):
        try:
            msg = value.to_string()
        except Exception:                                  # noqa: BLE001
            return
        if msg == "settings":
            self._menu_settings()
        elif msg.startswith("hud:"):
            # The micro player shows or goes: the page says how much taller the strip is.
            try:
                extra = max(0, min(400, int(float(msg[4:]))))
            except ValueError:
                return
            if extra != self.hud_extra:
                self.hud_extra = extra
                print(f"[shell] micro player {'shown, ' + str(extra) + ' px' if extra else 'gone'}")
                self._apply_input_region()
        elif self.player and (cmd := music.core.page_command(msg)):
            self.player.control(*cmd)
            self._push_music()

    def _push_music(self):
        """The micro player under her bar (Music M1): the player's state, when it changed."""
        if self.player and self.view and not self._exiting:
            js = "window.__cantineroMusic && window.__cantineroMusic(%s)" % json.dumps(self.player.snapshot())
            if js != self._music_pushed:
                self._music_pushed = js
                self.view.evaluate_javascript(js, -1, None, None, None, None, None)
        return True                                        # the timer keeps going

    def _menu_settings(self, *_):
        if self.settings_win is None:
            self.settings_win = shellsettings.SettingsWindow(self, self.prefs, self._on_prefs,
                                                             on_exit=self._menu_exit)
            self.settings_win.connect("close-request", self._on_settings_closed)
        self.settings_win.present()

    # -- exit (R6.3) ---------------------------------------------------------
    # Everything stops: the music, the avatar, the tray, the player and the resident service
    # (the mic, her voice, the line to the server). Nothing restarts it until the launcher or
    # the next login: the shell exits 0, so its unit's Restart=on-failure leaves it down, and a
    # stopped unit is never restarted by Restart=always.
    def _menu_exit(self, *_):
        if self._exiting:
            return
        if self.player and self.player.playing():
            d = Gtk.AlertDialog(message="Exit Cantinero?",
                                detail="The music is playing; it stops too.",
                                buttons=["Cancel", "Exit"], cancel_button=0, default_button=1)
            d.choose(self.settings_win, None, self._on_exit_answer)
            return
        self._exit()

    def _on_exit_answer(self, dialog, res):
        try:
            if dialog.choose_finish(res) == 1:
                self._exit()
        except GLib.Error:
            pass                                   # dismissed: same as Cancel

    def _exit(self):
        self._exiting = True
        print("[shell] exit: stopping the music, the shell and the resident")
        if self.player:
            self.player.stop()                     # reports where it stopped, closes the session
        if self.view:
            self.view.load_uri("about:blank")      # pagehide: the chat is saved to memory
        if self.settings_win:
            self.settings_win.close()
        if self.win:
            self._hide()
        # A moment for those requests to leave before the process does.
        GLib.timeout_add(1200, self._exit_finish)

    def _exit_finish(self):
        try:
            # --no-block: the container takes up to 5 s to stop; nothing here needs to wait.
            subprocess.run(["systemctl", "--user", "--no-block", "stop", "cantinero-resident.service"],
                           capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[shell] could not stop the resident: {e}")
        self.quit()
        return False

    def _on_settings_closed(self, *_):
        self.settings_win = None
        return False                               # let it close

    def _on_prefs(self, prefs: dict):
        self.prefs = prefs
        shellsettings.save(prefs)

    def _hide(self):
        if not self.win:
            return
        self.hidden = True
        self.win.set_opacity(0.0)
        self._apply_input_region()

    def _wave(self):
        """Her hello (vtuber.mjs listens for avatar:wave; a card with no 3D avatar ignores it,
        and the avatar's own 6 s cooldown absorbs a held-down shortcut)."""
        if self.view:
            self.view.evaluate_javascript(
                'window.dispatchEvent(new CustomEvent("avatar:wave"))',
                -1, None, None, None, None, None)
        return False                                   # one shot

    def _show(self) -> bool:
        """Show and raise her. Returns True if she was hidden."""
        if not self.win:
            return False
        was_hidden = self.hidden
        self.hidden = False
        self.win.set_opacity(1.0)
        self.win.present()
        self._apply_input_region()
        return was_hidden

    # -- character switching -------------------------------------------------
    # Ids are opaque strings, but dbusmenu addresses items by integer, so the
    # submenu keeps its own index -> id map rather than trying to encode one in
    # the other.
    def _refresh_characters(self, *_):
        if not self.view:
            return True
        def cb(view, res, _u):
            try:
                val = view.evaluate_javascript_finish(res)
                cards = json.loads(val.to_string() or "[]")
            except Exception:                              # noqa: BLE001
                return                                     # page not ready yet
            if not isinstance(cards, list) or not cards:
                return
            self._char_ids = {}
            kids = []
            for i, c in enumerate(cards):
                mid = 100 + i
                self._char_ids[mid] = c.get("id")
                label = c.get("name") or "?"
                if not c.get("avatar"):
                    # Sprite tier renders a flat PNG, not a 3D avatar -- worth
                    # saying here, since picking one is the likeliest surprise.
                    label += "  (no 3D avatar)"
                kids.append(MenuItem(mid, label, self._menu_pick_character,
                                     toggle=True, checked=bool(c.get("current"))))
            changed = [(k.id, k.label, k.checked) for k in kids] != \
                      [(k.id, k.label, k.checked) for k in self._mi_chars.children]
            if changed:
                self._mi_chars.children = kids
            if self._sync_listening() or changed:
                if self.tray:
                    self.tray.update()
        self.view.evaluate_javascript(
            "JSON.stringify(window.__desktopCharacters ? window.__desktopCharacters() : [])",
            -1, None, None, None, cb, None)
        return True                                        # keep the timer alive

    def _sync_listening(self) -> bool:
        """Pull listening state off the page. Returns True if the menu changed."""
        if not self.view or not self._mi_listen:
            return False
        before = (self._mi_listen.checked, self._mi_listen.label)
        def cb(view, res, _u):
            try:
                d = json.loads(view.evaluate_javascript_finish(res).to_string() or "{}")
            except Exception:                              # noqa: BLE001
                return
            self._mi_listen.checked = bool(d.get("on"))
            wake = (d.get("wake") or "").strip()
            sleep = (d.get("sleep") or "").strip()
            # Naming the phrase saves a trip to Settings to find out why nothing
            # happens when you talk -- and in continuous mode the label doubles as
            # the only indication that she is mid-conversation and still hearing you.
            if not d.get("on"):
                self._mi_listen.label = (f'Listening — off (wake word "{wake}")'
                                         if wake else "Listening — off")
            elif d.get("mode") == "continuous" and d.get("awake"):
                self._mi_listen.label = (f'In conversation — say "{sleep}" to end'
                                         if sleep else "In conversation")
            elif wake:
                self._mi_listen.label = f'Listening (say "{wake}")'
            else:
                self._mi_listen.label = "Listening"
            if self.tray and (self._mi_listen.checked, self._mi_listen.label) != before:
                self.tray.update()
        self.view.evaluate_javascript(
            "JSON.stringify(window.__desktopListening ? window.__desktopListening() : {})",
            -1, None, None, None, cb, None)
        return False

    def _menu_listen(self, item):
        if not self.view:
            return
        js = "window.__desktopSetListening && window.__desktopSetListening(%s)" % (
            "true" if item.checked else "false")
        self.view.evaluate_javascript(js, -1, None, None, None, None, None)
        GLib.timeout_add_seconds(2, self._refresh_characters)

    def _menu_pick_character(self, item):
        cid = getattr(self, "_char_ids", {}).get(item.id)
        if not cid or not self.view:
            return
        # Tick this one immediately; the refresh below reconciles with the page.
        for k in self._mi_chars.children:
            k.checked = (k.id == item.id)
        if self.tray:
            self.tray.update()
        self.view.evaluate_javascript(
            f"window.__desktopSetCharacter && window.__desktopSetCharacter({json.dumps(cid)})",
            -1, None, None, None, None, None)
        GLib.timeout_add_seconds(2, self._refresh_characters)

    def _menu_click_through(self, item):
        self.click_through = item.checked
        self._apply_input_region()

    def _menu_position_mode(self, item):
        """Show the drag bar and make the whole window grabbable.

        Position mode and click-through are mutually exclusive by definition:
        you cannot drag a window that does not accept the click. So entering it
        suspends click-through and leaving it restores whatever was set before.
        """
        self.position_mode = item.checked
        self.drag_bar.set_visible(self.position_mode)
        if self.position_mode:
            self._set_anchor("")                  # dragged: where she is dropped, not the corner
        if self.position_mode:
            self._saved_click_through = self.click_through
            self.click_through = False
        else:
            self.click_through = getattr(self, "_saved_click_through", True)
        # Keep the checkbox honest in BOTH directions: it reflects click_through,
        # and position mode changes click_through, so a stale tick here would
        # have the menu reporting a state the window is not in.
        if self._mi_through:
            self._mi_through.checked = self.click_through
            self._mi_through.enabled = not self.position_mode
        self._apply_input_region()
        if self.tray:
            self.tray.update()


def main() -> int:
    ap = argparse.ArgumentParser(description="Cantinero desktop shell")
    ap.add_argument("--url", default=os.environ.get("PANTHEON_URL", DEFAULT_URL))
    ap.add_argument("--width", type=int, default=420)
    ap.add_argument("--height", type=int, default=620)
    ap.add_argument("--no-tray", action="store_true", help="run without a tray icon")
    ap.add_argument("--no-music", action="store_true", help="don't run the music player")
    ap.add_argument("--icon", default="cantinero",
                    help="tray icon: a name in desktop/icons/ (default: cantinero) or the icon theme")
    args = ap.parse_args()

    app = DesktopShell(args.url, args.width, args.height, not args.no_tray, not args.no_music)
    app.icon_name = args.icon
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
