"""Cantinero's music player: the window and the pipeline (RESIDENT_PLAN.md R-4, R-5, R-7).

A small window built for an assistant to drive and a person to glance at:
- the cover, the title, the artist and album;
- a seek bar with times;
- previous / play-pause / next and a volume slider;
- the queue.

It takes its orders from Cantinero through the resident service
(``ws://127.0.0.1:<port>/player/connect``). Each track's stream comes from Orpheus, asked as this
resident's own Orpheus player device. GStreamer plays it: ``playbin3``, direct, never re-encoded.

The rules live in ``musiccore.py`` (tested without GTK); this file only carries them out.
Closing the window hides it and the music plays on. The tray's "Music player" shows it again.
"""
from __future__ import annotations

import json
import os
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstAudio", "1.0")
gi.require_version("Soup", "3.0")
from gi.repository import Gdk, GLib, Gst, GstAudio, Gtk, Soup  # noqa: E402

import musiccore as core  # noqa: E402

RECONNECT_FIRST, RECONNECT_MAX = 1.0, 30.0
PLAY_ACK_WAIT_S = 4.0
PROGRESS_EVERY_S = 15.0
DEBUG = os.environ.get("CANTINERO_MUSIC_DEBUG") == "1"

CSS = b"""
.cm-window { background: #1b1714; color: #f3e9dc; }
.cm-cover { border-radius: 10px; background: #2a231e; }
.cm-title { font-size: 1.25em; font-weight: 700; }
.cm-sub { color: #c9b79f; }
.cm-time { color: #a8957c; font-size: 0.85em; font-feature-settings: "tnum"; }
.cm-queue row { padding: 4px 8px; }
.cm-queue row.cm-now { background: alpha(#d4a24c, 0.18); border-radius: 6px; }
.cm-btn { min-width: 40px; min-height: 40px; border-radius: 20px; }
.cm-status { color: #a8957c; font-size: 0.85em; }
"""


def resident_config() -> dict:
    d = os.environ.get("RESIDENT_DATA") or os.path.join(GLib.get_user_data_dir(), "cantinero-resident")
    try:
        with open(os.path.join(d, "resident.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def configured(cfg: dict) -> bool:
    return bool(cfg.get("player_key") and cfg.get("orpheus_url") and cfg.get("orpheus_token"))


class MusicPlayer:
    def __init__(self, app: Gtk.Application, cfg: dict, resident_port: int = 7867):
        Gst.init(None)
        self.cfg = cfg
        self.orpheus = cfg["orpheus_url"].rstrip("/")
        # CANTINERO_RESIDENT_URL points the player at another resident service (development).
        base = os.environ.get("CANTINERO_RESIDENT_URL") or f"http://127.0.0.1:{resident_port}"
        self.ws_url = base.replace("http", "ws", 1).rstrip("/") + "/player/connect"
        self.queue, self.volume, self.throttle = core.Queue(), core.Volume(), core.StateThrottle()
        self.status = "idle"
        self.session_id = ""                  # an Orpheus playback session to close, if any
        self.ws = None
        self.failures = 0
        self.pending_play = None              # (command id, deadline) until the first sound
        self.want_playing = False             # what was asked: a seek's flush passes through PAUSED
        self.last_progress = 0.0
        self.http = Soup.Session(timeout=15)

        self.pipe = Gst.ElementFactory.make("playbin3", "music")
        self.pipe.set_property("flags", self.pipe.get_property("flags") & ~0x1)   # audio only: no video
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_eos)
        bus.connect("message::error", self._on_error)
        bus.connect("message::state-changed", self._on_state)
        self._apply_volume()

        self._build(app)
        GLib.timeout_add(500, self._tick)

    # -- window ----------------------------------------------------------------------------------

    def _build(self, app):
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.win = Gtk.Window(application=app, title="Cantinero — Music")
        self.win.set_default_size(400, 620)
        self.win.add_css_class("cm-window")
        self.win.connect("close-request", lambda *_: (self.win.set_visible(False), True)[1])

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=16, margin_bottom=16,
                      margin_start=16, margin_end=16)
        self.cover = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.COVER)
        self.cover.set_size_request(368, 368)
        self.cover.add_css_class("cm-cover")
        box.append(self.cover)

        self.title = Gtk.Label(label="Nothing playing", xalign=0, ellipsize=3)
        self.title.add_css_class("cm-title")
        self.sub = Gtk.Label(label="Ask the bartender for some music.", xalign=0, ellipsize=3)
        self.sub.add_css_class("cm-sub")
        box.append(self.title)
        box.append(self.sub)

        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.seek.set_draw_value(False)
        self._seek_handler = self.seek.connect("change-value", self._on_seek)
        box.append(self.seek)
        times = Gtk.Box(spacing=0)
        self.t_pos = Gtk.Label(label="0:00", xalign=0, hexpand=True)
        self.t_dur = Gtk.Label(label="0:00", xalign=1)
        for t in (self.t_pos, self.t_dur):
            t.add_css_class("cm-time")
            times.append(t)
        box.append(times)

        row = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.b_prev = Gtk.Button(icon_name="media-skip-backward-symbolic")
        self.b_play = Gtk.Button(icon_name="media-playback-start-symbolic")
        self.b_next = Gtk.Button(icon_name="media-skip-forward-symbolic")
        for b, act in ((self.b_prev, "previous"), (self.b_play, "toggle"), (self.b_next, "next")):
            b.add_css_class("cm-btn")
            b.connect("clicked", lambda _b, a=act: self._local(a))
            row.append(b)
        box.append(row)

        vol = Gtk.Box(spacing=8)
        vol.append(Gtk.Image(icon_name="audio-volume-medium-symbolic"))
        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.vol.set_hexpand(True)
        self.vol.set_draw_value(False)
        self.vol.set_value(self.volume.level)
        self.vol.connect("value-changed", self._on_volume_slider)
        vol.append(self.vol)
        box.append(vol)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("cm-queue")
        self.list.connect("row-activated", lambda _l, r: self._jump(r.get_index()))
        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=120)
        scroll.set_child(self.list)
        box.append(scroll)

        self.status_label = Gtk.Label(label="", xalign=0)
        self.status_label.add_css_class("cm-status")
        box.append(self.status_label)
        self.win.set_child(box)

    def show(self):
        self.win.present()

    def playing(self) -> bool:
        return self.status == "playing"

    def stop(self):
        """Exit Cantinero: stop, report where it stopped, close the Orpheus session."""
        if self.status != "idle":
            self._stop()

    def toggle(self):
        if self.win.get_visible():
            self.win.set_visible(False)
        else:
            self.win.present()

    # -- the line to the resident --------------------------------------------------------------

    def start(self):
        self._put_capabilities()
        self._connect()

    def _connect(self):
        msg = Soup.Message.new("GET", self.ws_url)
        msg.get_request_headers().append("X-Player-Key", self.cfg["player_key"])
        self.http.websocket_connect_async(msg, None, None, GLib.PRIORITY_DEFAULT, None, self._connected, None)

    def _connected(self, session, res, _u):
        try:
            self.ws = session.websocket_connect_finish(res)
        except GLib.Error as e:
            self._line(f"Can't reach the resident service ({e.message}).")
            self._retry()
            return
        self.failures = 0
        self.ws.set_keepalive_interval(0)
        self.ws.connect("message", self._on_message)
        self.ws.connect("closed", lambda *_: (setattr(self, "ws", None), self._retry()))
        self._line("")
        self.throttle = core.StateThrottle()
        self._report(force=True)

    def _retry(self):
        delay = min(RECONNECT_MAX, RECONNECT_FIRST * 2 ** self.failures)
        self.failures += 1
        GLib.timeout_add(int(delay * 1000), lambda: (self._connect(), False)[1])

    def _send(self, msg: dict):
        if self.ws is not None and self.ws.get_state() == Soup.WebsocketState.OPEN:
            self.ws.send_text(json.dumps(msg))

    def _ack(self, cid, ok=True, error=""):
        if cid:
            m = {"type": "ack", "id": cid, "ok": ok}
            if error and not ok:
                m["error"] = error
            self._send(m)

    def _on_message(self, _ws, kind, data):
        if kind != Soup.WebsocketDataType.TEXT:
            return
        try:
            cmd = json.loads(data.get_data().decode())
        except (ValueError, UnicodeDecodeError):
            return
        cid = cmd.get("id", "")
        act = core.reduce(cmd, self.queue, self.volume, time.monotonic(), self.queue.current is not None)
        if DEBUG:
            print("[music]", cmd.get("type"), "->", act.kind, act.error, flush=True)
        if act.kind == "refuse":
            self._ack(cid, False, act.error)
            return
        if act.kind == "load":
            self._show_queue()
            self.pending_play = (cid, time.monotonic() + PLAY_ACK_WAIT_S)
            self._load_current()
            return                                     # acked when sound starts (or the wait ends)
        {"pause": self._pause, "resume": self._resume, "stop": self._stop,
         "next": lambda: self._advance(), "previous": self._previous,
         "seek": lambda: self._seek_to(act.seconds), "volume": self._apply_volume}[act.kind]()
        self._ack(cid)
        self._report(force=True)

    # -- Orpheus -------------------------------------------------------------------------------

    def _orpheus(self, method: str, path: str, body: dict | None, done):
        msg = Soup.Message.new(method, self.orpheus + path)
        msg.get_request_headers().append("Authorization", f"Bearer {self.cfg['orpheus_token']}")
        if body is not None:
            msg.set_request_body_from_bytes("application/json", GLib.Bytes.new(json.dumps(body).encode()))

        def fin(session, res, _u):
            try:
                data = session.send_and_read_finish(res)          # GLib.Bytes
                status = msg.get_status()
            except GLib.Error as e:
                done(0, None, e.message)
                return
            done(status, data, "")
        self.http.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, None, fin, None)

    def _put_capabilities(self):
        def done(status, _d, err):
            if status != 200 and status != 204:
                print(f"[music] Orpheus didn't take the capability report: {status} {err}", flush=True)
        self._orpheus("PUT", "/api/v1/devices/self/capabilities", core.CAPABILITIES, done)

    def _close_session(self):
        if self.session_id:
            self._orpheus("DELETE", f"/api/v1/playback/{self.session_id}", None, lambda *_: None)
            self.session_id = ""

    def _load_current(self):
        item = self.queue.current
        if item is None:
            return
        self.pipe.set_state(Gst.State.NULL)
        # Drop whatever the replaced stream left on the bus (its end, an error from tearing it
        # down): handled now, it would count against the new track and skip it.
        bus = self.pipe.get_bus()
        bus.set_flushing(True)
        bus.set_flushing(False)
        self.uri_set = False
        self._close_session()
        self._set_status("loading")
        self._show_now(item)
        wanted = self.queue.index

        def done(status, data, err):
            if wanted != self.queue.index:
                return                                 # moved on meanwhile
            try:
                decision = json.loads(data.get_data().decode()) if data is not None else {}
            except (ValueError, UnicodeDecodeError):
                decision = {}
            url = decision.get("url")
            if status != 200 or not url:
                print(f"[music] no stream for {item.get('title')!r}: {status} {err}", flush=True)
                self._fail_pending("no_stream")
                self._skip_soon()
                return
            self.session_id = decision.get("session_id") or ""
            self.pipe.set_property("uri", self.orpheus + url if url.startswith("/") else url)
            self.want_playing = True
            self.uri_set = True
            self.pipe.set_state(Gst.State.PLAYING)
        self._orpheus("POST", f"/api/v1/items/{item['id']}/playback", {}, done)

    # -- transport -------------------------------------------------------------------------------

    def _pause(self):
        self.want_playing = False
        self.pipe.set_state(Gst.State.PAUSED)

    def _resume(self):
        self.want_playing = True
        if self.status == "idle" and self.queue.current:
            self._load_current()
        else:
            self.pipe.set_state(Gst.State.PLAYING)

    def _stop(self):
        self.want_playing = False
        self._report_progress(final=True)
        self.pipe.set_state(Gst.State.NULL)
        self._close_session()
        self._set_status("idle")

    def _advance(self, auto: bool = False):
        self._report_progress(final=True)
        if self.queue.advance():
            self._load_current()
        elif auto:
            self.want_playing = False
            self.pipe.set_state(Gst.State.NULL)
            self._close_session()
            self._set_status("idle")
        self._show_queue()

    def _previous(self):
        if self.queue.back(self._position()):
            self._load_current()
            self._show_queue()
        else:
            self._seek_to(0)

    def _jump(self, index: int):
        if 0 <= index < len(self.queue.items):
            self.queue.index = index
            self._load_current()
            self._show_queue()

    def _seek_to(self, seconds: float):
        dur = self._duration()
        if dur and seconds >= dur - 0.5:
            self._advance(auto=True)                  # past the end: the next track, as if it ended
            return
        self.pipe.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                              int(seconds * Gst.SECOND))

    def _apply_volume(self):
        linear = GstAudio.StreamVolume.convert_volume(GstAudio.StreamVolumeFormat.CUBIC,
                                                      GstAudio.StreamVolumeFormat.LINEAR, self.volume.effective)
        self.pipe.set_property("volume", linear)
        if hasattr(self, "vol") and int(self.vol.get_value()) != self.volume.level:
            self._syncing = True
            self.vol.set_value(self.volume.level)
            self._syncing = False

    def _local(self, action: str):
        """A button on the window: the same rules as a command, no ack."""
        if action == "toggle":
            action = "pause" if self.status == "playing" else "resume"
        if self.queue.current is None:
            return
        {"pause": self._pause, "resume": self._resume, "next": lambda: self._advance(),
         "previous": self._previous}[action]()
        self._report(force=True)

    def _on_volume_slider(self, scale):
        if getattr(self, "_syncing", False):
            return
        self.volume.set(int(scale.get_value()))
        self._apply_volume()
        self._report(force=True)

    def _on_seek(self, _scale, _scroll, value):
        if self.queue.current is not None:
            self._seek_to(value)
        return False

    # -- pipeline events -------------------------------------------------------------------------

    def _on_state(self, bus, msg):
        if msg.src != self.pipe:
            return
        _old, new, _pending = msg.parse_state_changed()
        # The status is what was asked, confirmed by the pipeline: a seek flushes through
        # PAUSED and back, which must not read as the listener pausing.
        if new == Gst.State.PLAYING and self.want_playing:
            self._set_status("playing")
        elif new == Gst.State.PAUSED and not self.want_playing and self.status == "playing":
            self._set_status("paused")

    def _on_eos(self, bus, msg):
        # Only the track that is playing may end: an end-of-stream from the one just replaced
        # (a seek past its end, then next) arrives while the new one loads, and advanced twice.
        if self.status in ("playing", "paused"):
            self._advance(auto=True)

    def _on_error(self, bus, msg):
        if not getattr(self, "uri_set", False):
            return                                     # the replaced stream's, not this track's
        err, dbg = msg.parse_error()
        print(f"[music] playback error: {err.message} ({dbg})", flush=True)
        self._fail_pending("playback_failed")
        self._skip_soon()

    def _skip_soon(self):
        """Past a track that can't play, once: one failure often raises several errors (an HTTP
        416, then "internal data stream error"), and each used to skip a track."""
        if getattr(self, "_skipping", False):
            return
        self._skipping = True

        def go():
            self._skipping = False
            self._advance(auto=True)
            return False
        GLib.timeout_add(800, go)

    def _fail_pending(self, error: str):
        if self.pending_play:
            self._ack(self.pending_play[0], False, error)
            self.pending_play = None

    def _position(self) -> float:
        ok, pos = self.pipe.query_position(Gst.Format.TIME)
        return pos / Gst.SECOND if ok else 0.0

    def _duration(self) -> float:
        ok, dur = self.pipe.query_duration(Gst.Format.TIME)
        if ok and dur > 0:
            return dur / Gst.SECOND
        item = self.queue.current or {}
        return (item.get("duration_ms") or 0) / 1000

    def _tick(self) -> bool:
        now = time.monotonic()
        if self.volume.expired(now):
            self._apply_volume()
        pos, dur = self._position(), self._duration()
        if self.pending_play:
            cid, deadline = self.pending_play
            if (self.status == "playing" and pos > 0) or now > deadline:
                self._ack(cid, True)
                self.pending_play = None
                self._report(force=True)
        self.seek.handler_block(self._seek_handler)
        self.seek.set_range(0, max(dur, 1))
        self.seek.set_value(pos)
        self.seek.handler_unblock(self._seek_handler)
        self.t_pos.set_label(core.fmt_time(pos))
        self.t_dur.set_label(core.fmt_time(dur))
        if self.status == "playing" and now - self.last_progress >= PROGRESS_EVERY_S:
            self._report_progress()
        self._report()
        return True

    # -- what it tells others -------------------------------------------------------------------

    def _report(self, force: bool = False):
        msg = core.state_message(self.status, self.queue, self.volume, self._position(), self._duration())
        if self.throttle.due(msg, time.monotonic()) or force:
            self._send(msg)

    def _report_progress(self, final: bool = False):
        """Where this track is, for Orpheus's play counts and resume (best effort)."""
        item = self.queue.current
        if not item or self.status not in ("playing", "paused"):
            return
        self.last_progress = time.monotonic()
        pos, dur = self._position(), self._duration()
        self._orpheus("POST", "/api/v1/me/progress", {
            "item_id": item["id"], "position_ms": int(pos * 1000), "duration_ms": int(dur * 1000),
            "playing": self.status == "playing" and not final}, lambda *_: None)

    # -- the window's contents -------------------------------------------------------------------

    def _set_status(self, status: str):
        self.status = status
        self.b_play.set_icon_name("media-playback-pause-symbolic" if status == "playing"
                                  else "media-playback-start-symbolic")
        if status == "idle":
            self.title.set_label("Nothing playing")
            self.sub.set_label("Ask the bartender for some music.")
            self.cover.set_paintable(None)
        self._report(force=True)

    def _line(self, text: str):
        self.status_label.set_label(text)

    def _show_now(self, item: dict):
        self.title.set_label(item.get("title") or "")
        self.sub.set_label(" — ".join(x for x in (item.get("artist"), item.get("album")) if x))
        art = item.get("artwork_id")
        if art is None:
            self.cover.set_paintable(None)
            return

        def done(status, data, _err):
            if status == 200 and data is not None and self.queue.current is item:
                try:
                    self.cover.set_paintable(Gdk.Texture.new_from_bytes(data))
                except GLib.Error:
                    self.cover.set_paintable(None)
        self._orpheus("GET", f"/api/v1/artwork/{int(art)}", None, done)

    def _show_queue(self):
        while (row := self.list.get_first_child()) is not None:
            self.list.remove(row)
        for i, e in enumerate(self.queue.items[:200]):
            label = Gtk.Label(label=f"{e.get('title', '')}" + (f"  ·  {e['artist']}" if e.get("artist") else ""),
                              xalign=0, ellipsize=3)
            row = Gtk.ListBoxRow(child=label, activatable=True)
            if i == self.queue.index:
                row.add_css_class("cm-now")
            self.list.append(row)
