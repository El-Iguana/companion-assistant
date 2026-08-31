#!/usr/bin/env python3
"""Pantheon desktop shell (Phase 6).

A borderless, transparent GTK4 window holding a WebKit view of the app's
/desktop page. It contains no application logic at all -- the container on
:7861 stays the brain, and this is a picture frame.

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
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gtk, Gdk, GLib, WebKit  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tray import Tray, MenuItem  # noqa: E402

APP_ID = "ai.jotunheim.pantheon.desktop"
DEFAULT_URL = "http://localhost:7861/desktop"
# The HUD lives in a strip along the bottom. Only that strip accepts clicks;
# everything above it passes through to whatever is on the desktop behind her.
HUD_STRIP_PX = 56


class DesktopShell(Gtk.Application):
    def __init__(self, url: str, width: int, height: int, tray: bool):
        super().__init__(application_id=APP_ID)
        self.url, self.w, self.h, self.want_tray = url, width, height, tray
        self.win: Gtk.Window | None = None
        self.view: WebKit.WebView | None = None
        self.click_through = True
        self.tray = None
        self.icon_name = "face-smile"

    # -- lifecycle ---------------------------------------------------------
    def do_activate(self):
        if self.win:
            self.win.present()
            return

        self.win = Gtk.ApplicationWindow(application=self)
        self.win.set_default_size(self.w, self.h)
        self.win.set_decorated(False)
        self.win.set_title("Pantheon")
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
        s = self.view.get_settings()
        for prop, val in (
            ("enable-webgl", True),
            ("enable-media-stream", True),
            ("enable-mediasource", True),
            ("media-playback-requires-user-gesture", False),
            ("enable-developer-extras", True),
        ):
            try:
                s.set_property(prop, val)
            except Exception:                       # noqa: BLE001
                pass                                # older WebKit: skip quietly

        # Mic and autoplay: this is a local app the user launched deliberately,
        # so a prompt with no window chrome to host it would just be a dead end.
        self.view.connect("permission-request", self._on_permission)
        self.view.set_background_color(Gdk.RGBA())  # fully transparent
        self.win.set_child(self.view)
        self.view.load_uri(self.url)

        self.win.connect("notify::default-width", lambda *_: self._apply_input_region())
        self.win.connect("notify::default-height", lambda *_: self._apply_input_region())
        self.win.present()
        GLib.timeout_add(300, self._apply_input_region_once)

        if self.want_tray:
            self._build_tray()

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
            if self.click_through:
                strip = cairo.RectangleInt(0, max(0, h - HUD_STRIP_PX), w, HUD_STRIP_PX)
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
        items = [
            MenuItem(1, "Show / hide", self._menu_toggle_visible),
            self._mi_through,
            MenuItem(3, is_sep=True),
            MenuItem(4, "Reload", lambda _i: self.view and self.view.reload()),
            MenuItem(5, "Quit", lambda _i: self.quit()),
        ]
        self.tray = Tray(APP_ID, "Pantheon", self.icon_name, items,
                         on_activate=self._menu_toggle_visible)
        if not self.tray.start():
            self.tray = None

    def _menu_toggle_visible(self, *_):
        if not self.win:
            return
        if self.win.get_visible():
            self.win.set_visible(False)
        else:
            self.win.set_visible(True)
            self.win.present()
            # The surface is recreated on re-show, so the input region goes with it.
            GLib.timeout_add(250, self._apply_input_region_once)

    def _menu_click_through(self, item):
        self.click_through = item.checked
        self._apply_input_region()


def main() -> int:
    ap = argparse.ArgumentParser(description="Pantheon desktop shell")
    ap.add_argument("--url", default=os.environ.get("PANTHEON_URL", DEFAULT_URL))
    ap.add_argument("--width", type=int, default=420)
    ap.add_argument("--height", type=int, default=620)
    ap.add_argument("--no-tray", action="store_true", help="run without a tray icon")
    ap.add_argument("--icon", default="face-smile", help="themed tray icon name")
    args = ap.parse_args()

    app = DesktopShell(args.url, args.width, args.height, not args.no_tray)
    app.icon_name = args.icon
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
