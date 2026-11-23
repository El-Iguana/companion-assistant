"""The Orpheus player built into the resident (RESIDENT_PLAN.md R-4, R-5).

A second window of the shell's process: Orpheus's own web player in a WebKit view, signed
in as this resident's Orpheus player device (its token set as the ``orpheus_session``
cookie), with sound allowed to start without a click. It is always on. It starts
minimized, closing it only minimizes it, and a crashed or disconnected page is reloaded,
so a play from any chat reaches it at once.

Proven on predator (WebKitGTK 6 / 2.54): Pyodide boots in under 5 s, and from amd-halo,
with the window minimized (page "hidden"):
- an album plays, acked in 351 ms once sound starts;
- next, volume, duck/unduck and pause work.

**Both autoplay gates must be open.** One is the `media-playback-requires-user-gesture`
setting. The other is the per-site `WebsitePolicies(autoplay=ALLOW)`; without it the media
loaded (readyState 4) and sat paused, with no error anywhere.

The token comes from the resident's own file (``python -m resident pair-orpheus``), never
from the page.
"""
from __future__ import annotations

import json
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("Soup", "3.0")
from gi.repository import GLib, Gtk, Soup, WebKit  # noqa: E402

WATCH_EVERY_S = 30
DEBUG = os.environ.get("CANTINERO_PLAYER_DEBUG") == "1"
# What the page's media elements are doing, logged every 3 s with CANTINERO_PLAYER_DEBUG=1.
PROBE = """JSON.stringify({remote: document.body && document.body.dataset.remote,
  visible: document.visibilityState,
  media: [...document.querySelectorAll('audio,video')].map(e => ({paused: e.paused, t: +e.currentTime.toFixed(1),
    ready: e.readyState, net: e.networkState, src: (e.currentSrc || '').slice(0, 60), err: e.error && e.error.code}))})"""
RELOAD_AFTER = 3            # checks in a row without a control connection


def resident_config() -> dict:
    d = os.environ.get("RESIDENT_DATA") or os.path.join(GLib.get_user_data_dir(), "cantinero-resident")
    try:
        with open(os.path.join(d, "resident.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def configured(cfg: dict) -> bool:
    return bool(cfg.get("orpheus_url") and cfg.get("orpheus_token"))


class PlayerWindow:
    def __init__(self, app: Gtk.Application, cfg: dict):
        self.url = cfg["orpheus_url"].rstrip("/")
        self.token = cfg["orpheus_token"]
        self.misses = 0
        data_dir = os.path.join(GLib.get_user_data_dir(), "pantheon-desktop", "orpheus")
        os.makedirs(data_dir, exist_ok=True)
        ns = WebKit.NetworkSession.new(data_dir, data_dir)
        self.cookies = ns.get_cookie_manager()
        self.cookies.set_persistent_storage(os.path.join(data_dir, "cookies.sqlite"),
                                            WebKit.CookiePersistentStorage.SQLITE)
        # Sound may start with nobody clicking (Orpheus AD-7). WebKit has two gates: the
        # gesture setting below, and the per-site autoplay policy, which otherwise lets a
        # page start only silent media.
        policies = WebKit.WebsitePolicies(autoplay=WebKit.AutoplayPolicy.ALLOW)
        self.view = WebKit.WebView(network_session=ns, website_policies=policies)
        s = self.view.get_settings()
        for prop, val in (("media-playback-requires-user-gesture", False),   # AD-7, without a flag
                          ("enable-mediasource", True),
                          ("enable-write-console-messages-to-stdout", True)):
            try:
                s.set_property(prop, val)
            except Exception:                                  # noqa: BLE001
                pass
        # A page whose web process died (out of memory, a GPU reset) comes back by itself.
        self.view.connect("web-process-terminated", lambda *_: GLib.timeout_add_seconds(2, self._reload))

        self.win = Gtk.Window(application=app, title="Orpheus — Cantinero")
        self.win.set_default_size(1000, 700)
        self.win.set_name("cantinero-orpheus")
        self.win.set_child(self.view)
        # Always on: closing only tucks it away.
        self.win.connect("close-request", self._on_close)

    def start(self) -> None:
        host = self.url.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[0]
        cookie = Soup.Cookie.new("orpheus_session", self.token, host, "/", -1)
        cookie.set_http_only(True)
        cookie.set_same_site_policy(Soup.SameSitePolicy.STRICT)
        self.cookies.add_cookie(cookie, None, self._cookie_set, None)
        # Shown, then minimized once loaded: a view that was never mapped may be throttled.
        self.win.present()
        GLib.timeout_add_seconds(WATCH_EVERY_S, self._watch)
        if DEBUG:
            GLib.timeout_add_seconds(3, self._debug)

    def _cookie_set(self, cm, res, _u):
        try:
            cm.add_cookie_finish(res)
        except GLib.Error as e:
            print(f"[player] couldn't set the Orpheus session: {e.message}")
        self.view.load_uri(self.url + "/player/")
        GLib.timeout_add_seconds(8, self._tuck_away_once)

    def _tuck_away_once(self) -> bool:
        self.win.minimize()
        return False

    def _on_close(self, *_):
        self.win.minimize()
        return True                                          # keep it

    def toggle(self) -> None:
        if self.win.is_active():
            self.win.minimize()
        else:
            self.win.present()

    def _reload(self) -> bool:
        print("[player] reloading the Orpheus player")
        self.view.load_uri(self.url + "/player/")
        return False

    def _debug(self) -> bool:
        def got(view, res, _u):
            try:
                print("[player]", view.evaluate_javascript_finish(res).to_string(), flush=True)
            except GLib.Error as e:
                print("[player] probe failed:", e.message, flush=True)
        self.view.evaluate_javascript(PROBE, -1, None, None, None, got, None)
        return True

    def _watch(self) -> bool:
        """Reload a player that has lost its control connection for a while (the server
        restarted, the network dropped and the page's own backoff gave up)."""
        def got(view, res, _u):
            try:
                state = view.evaluate_javascript_finish(res).to_string()
            except GLib.Error:
                state = ""
            self.misses = 0 if state == "connected" else self.misses + 1
            if self.misses >= RELOAD_AFTER:
                self.misses = 0
                self._reload()
        self.view.evaluate_javascript("document.body && document.body.dataset.remote || ''",
                                      -1, None, None, None, got, None)
        return True
