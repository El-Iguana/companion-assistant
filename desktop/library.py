"""The full player's Library tab (Music M3): find music in Orpheus and play it by hand.

  root    a search box; Albums or Artists, a page at a time ("More" fetches the next)
  search  artists, albums and tracks matching, each kind asked for on its own (a plain search
          also finds TV episodes)
  album   cover, title, artist, year; Play, Shuffle; its tracks (a click plays from that one)
  artist  Play all; its albums

Everything Orpheus says goes through ``musiccore`` (tested); this file is only widgets. It
asks Orpheus as the resident's own player device, through the player's ``_orpheus``.
"""
from __future__ import annotations

import json
import random
from urllib.parse import quote

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import musiccore as core  # noqa: E402

PAGE = 60
SEARCH_EACH = 8
SEARCH_AFTER_MS = 300
THUMB = 44


class LibraryBrowser:
    def __init__(self, player):
        self.player = player                       # music.MusicPlayer: _orpheus, play_entries
        self.art_cache: dict[int, Gdk.Texture] = {}
        self.mode = "album"
        self.cursor = None
        self._search_timer = 0
        self._search_seq = 0
        self.views: list[str] = []

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bar = Gtk.Box(spacing=6)
        self.back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        self.back.add_css_class("flat")
        self.back.connect("clicked", lambda *_: self._pop())
        self.back.set_visible(False)
        bar.append(self.back)
        self.search = Gtk.SearchEntry(placeholder_text="Search artists, albums, tracks", hexpand=True)
        self.search.connect("search-changed", self._on_search_changed)
        bar.append(self.search)
        self.widget.append(bar)

        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.widget.append(self.stack)

        # Root: Albums | Artists, or search results while there is text.
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        modes = Gtk.Box(spacing=0, halign=Gtk.Align.START)
        modes.add_css_class("linked")
        self.b_albums = Gtk.ToggleButton(label="Albums", active=True)
        self.b_artists = Gtk.ToggleButton(label="Artists", group=self.b_albums)
        self.b_albums.connect("toggled", lambda b: b.get_active() and self._set_mode("album"))
        self.b_artists.connect("toggled", lambda b: b.get_active() and self._set_mode("artist"))
        modes.append(self.b_albums)
        modes.append(self.b_artists)
        self.modes = modes
        root.append(modes)
        self.root_list, root_scroll = self._listbox()
        root.append(root_scroll)
        self.stack.add_named(root, "root")
        self.loaded = False

    # -- helpers -----------------------------------------------------------------------------------

    def _listbox(self):
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lb.add_css_class("cm-queue")
        lb.connect("row-activated", lambda _l, r: getattr(r, "on_activate", lambda: None)())
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_child(lb)
        return lb, sc

    @staticmethod
    def _clear(lb):
        while (row := lb.get_first_child()) is not None:
            lb.remove(row)

    def _get(self, path: str, done):
        """GET from Orpheus; done(dict) on success, done(None) on any failure."""
        def fin(status, data, err):
            if status != 200 or data is None:
                print(f"[library] {path}: {status} {err}", flush=True)
                done(None)
                return
            try:
                done(json.loads(data.get_data().decode()))
            except (ValueError, UnicodeDecodeError):
                done(None)
        self.player._orpheus("GET", path, None, fin)

    def _thumb(self, artwork_id, size=THUMB) -> Gtk.Picture:
        pic = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.COVER)
        pic.set_size_request(size, size)
        pic.add_css_class("cm-cover")
        if artwork_id is None:
            return pic
        art = int(artwork_id)
        if art in self.art_cache:
            pic.set_paintable(self.art_cache[art])
            return pic

        def done(status, data, _err):
            if status == 200 and data is not None:
                try:
                    tex = Gdk.Texture.new_from_bytes(data)
                except GLib.Error:
                    return
                self.art_cache[art] = tex
                pic.set_paintable(tex)
        self.player._orpheus("GET", f"/api/v1/artwork/{art}", None, done)
        return pic

    def _row(self, title: str, sub: str = "", artwork=None, thumb=True, lead: str = "",
             trail: str = "", on_activate=None) -> Gtk.ListBoxRow:
        line = Gtk.Box(spacing=10)
        if lead:
            n = Gtk.Label(label=lead, xalign=1)
            n.add_css_class("cm-num")
            line.append(n)
        if thumb:
            line.append(self._thumb(artwork))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(Gtk.Label(label=title, xalign=0, ellipsize=3))
        if sub:
            s = Gtk.Label(label=sub, xalign=0, ellipsize=3)
            s.add_css_class("cm-meta")
            text.append(s)
        line.append(text)
        if trail:
            t = Gtk.Label(label=trail, xalign=1)
            t.add_css_class("cm-time")
            line.append(t)
        row = Gtk.ListBoxRow(child=line, activatable=on_activate is not None)
        row.on_activate = on_activate
        return row

    def _note(self, lb, text: str):
        row = Gtk.ListBoxRow(activatable=False)
        lab = Gtk.Label(label=text, xalign=0, margin_top=8, margin_bottom=8)
        lab.add_css_class("cm-meta")
        row.set_child(lab)
        lb.append(row)

    def _head(self, text: str, lb):
        row = Gtk.ListBoxRow(activatable=False)
        lab = Gtk.Label(label=text.upper(), xalign=0, margin_top=6)
        lab.add_css_class("cm-head")
        row.set_child(lab)
        lb.append(row)

    # -- the root: albums or artists ------------------------------------------------------------------

    def shown(self):
        """The tab became visible: fill the list the first time."""
        if not self.loaded:
            self.loaded = True
            self._reload()

    def _set_mode(self, mode: str):
        if mode != self.mode:
            self.mode = mode
            self._reload()

    def _reload(self):
        self._clear(self.root_list)
        self.cursor = None
        self._note(self.root_list, "Loading…")
        self._page()

    def _page(self):
        mode = self.mode
        path = f"/api/v1/items?kind={mode}&sort=title&limit={PAGE}"
        if self.cursor:
            path += "&cursor=" + quote(self.cursor)

        def done(body):
            if mode != self.mode or self.search.get_text().strip():
                return                                     # moved on meanwhile
            last = self.root_list.get_last_child()
            if last is not None and not last.get_activatable():
                self.root_list.remove(last)                # "Loading…" or "More"
            if body is None:
                self._note(self.root_list, "Can't reach the music library.")
                return
            items = body.get("items") or []
            if mode == "artist":
                items = core.browsable_artists(items)
            for it in items:
                self.root_list.append(self._item_row(it))
            self.cursor = body.get("next_cursor")
            if self.cursor:
                more = self._row("More…", thumb=False, on_activate=lambda: (self._note_loading(), self._page()))
                self.root_list.append(more)
            elif self.root_list.get_first_child() is None:
                self._note(self.root_list, "Nothing here yet.")
        self._get(path, done)

    def _note_loading(self):
        last = self.root_list.get_last_child()
        if last is not None:
            last.set_activatable(False)

    def _item_row(self, it: dict) -> Gtk.ListBoxRow:
        kind = it.get("kind")
        if kind == "artist":
            n = it.get("child_count") or 0
            return self._row(it.get("title", ""), f"{n} album{'s' if n != 1 else ''}", it.get("artwork_id"),
                             on_activate=lambda: self._open_artist(it))
        if kind == "album":
            return self._row(it.get("title", ""), core.album_line(it), it.get("artwork_id"),
                             on_activate=lambda: self._open_album(it["id"]))
        # a track (search): play its album from it
        return self._row(it.get("title", ""), "Track", it.get("artwork_id"),
                         trail=core.fmt_time((it.get("duration_ms") or 0) / 1000) if it.get("duration_ms") else "",
                         on_activate=lambda: self._play_track(it))

    # -- search -------------------------------------------------------------------------------------

    def _on_search_changed(self, entry):
        if self._search_timer:
            GLib.source_remove(self._search_timer)
            self._search_timer = 0
        text = entry.get_text().strip()
        self.modes.set_visible(not text)
        while self.views:                                  # a new search starts from the root
            self._pop()
        if not text:
            self._reload()
            return
        self._search_timer = GLib.timeout_add(SEARCH_AFTER_MS, self._search, text)

    def _search(self, text: str):
        self._search_timer = 0
        self._search_seq += 1
        seq = self._search_seq
        self._clear(self.root_list)
        self._note(self.root_list, "Searching…")
        results: dict[str, list] = {}

        def got(kind):
            def done(body):
                if seq != self._search_seq:
                    return
                items = (body or {}).get("items") or []
                results[kind] = core.browsable_artists(items) if kind == "artist" else \
                    [i for i in items if i.get("kind") == kind]
                if len(results) == 3:
                    self._show_results(results)
            return done
        for kind in ("artist", "album", "track"):
            self._get(f"/api/v1/search?q={quote(text)}&kind={kind}&limit={SEARCH_EACH}", got(kind))
        return False

    def _show_results(self, results: dict):
        self._clear(self.root_list)
        any_ = False
        for kind, head in (("artist", "Artists"), ("album", "Albums"), ("track", "Tracks")):
            items = results.get(kind) or []
            if not items:
                continue
            any_ = True
            self._head(head, self.root_list)
            for it in items:
                self.root_list.append(self._item_row(it))
        if not any_:
            self._note(self.root_list, "Nothing matches.")

    # -- album and artist pages ------------------------------------------------------------------------

    def _push(self, name: str, widget: Gtk.Widget):
        self.stack.add_named(widget, name)
        self.views.append(name)
        self.stack.set_visible_child(widget)
        self.back.set_visible(True)

    def _pop(self):
        if not self.views:
            return
        name = self.views.pop()
        child = self.stack.get_child_by_name(name)
        self.stack.set_visible_child_name(self.views[-1] if self.views else "root")
        if child is not None:
            GLib.timeout_add(400, lambda: (self.stack.remove(child), False)[1])   # after the slide
        self.back.set_visible(bool(self.views))

    def _page_box(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        head = Gtk.Box(spacing=12)
        box.append(head)
        lb, sc = self._listbox()
        box.append(sc)
        return box, head, lb

    def _open_album(self, album_id: int, play_from: int | None = None):
        box, head, lb = self._page_box()
        self._note(lb, "Loading…")
        self._push(f"album-{album_id}-{len(self.views)}", box)

        def got_album(album):
            if album is None:
                self._clear(lb)
                self._note(lb, "Can't reach the music library.")
                return
            artist = core.album_artist(album)

            def got_kids(body):
                tracks = core.album_queue(album, (body or {}).get("items") or [], artist)
                self._fill_album(head, lb, album, artist, tracks)
                if play_from is not None:
                    start = next((i for i, e in enumerate(tracks) if e["id"] == play_from), 0)
                    self._play(tracks, start)
            self._get(f"/api/v1/items/{album_id}/children", got_kids)
        self._get(f"/api/v1/items/{album_id}", got_album)

    def _fill_album(self, head, lb, album, artist, tracks):
        head.append(self._thumb(album.get("artwork_id"), 96))
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        t = Gtk.Label(label=album.get("title", ""), xalign=0, wrap=True, lines=2, ellipsize=3, max_width_chars=26)
        t.add_css_class("cm-title")
        info.append(t)
        if artist:
            a = Gtk.Label(label=artist, xalign=0, ellipsize=3)
            a.add_css_class("cm-sub")
            info.append(a)
        line = core.album_line({"year": album.get("year"), "child_count": len(tracks)})
        if line:
            m = Gtk.Label(label=line, xalign=0)
            m.add_css_class("cm-meta")
            info.append(m)
        btns = Gtk.Box(spacing=6, margin_top=4)
        play = Gtk.Button(label="Play")
        play.add_css_class("cm-play-small")
        play.connect("clicked", lambda *_: self._play(tracks, 0))
        shuf = Gtk.Button(icon_name="media-playlist-shuffle-symbolic", tooltip_text="Shuffle")
        shuf.connect("clicked", lambda *_: self._play(tracks, 0, shuffle=True))
        btns.append(play)
        btns.append(shuf)
        info.append(btns)
        head.append(info)
        self._clear(lb)
        if not tracks:
            self._note(lb, "No playable tracks.")
        for i, e in enumerate(tracks):
            lb.append(self._row(e["title"], thumb=False, lead=str(i + 1),
                                trail=core.fmt_time((e.get("duration_ms") or 0) / 1000) if e.get("duration_ms") else "",
                                on_activate=lambda i=i: self._play(tracks, i)))

    def _open_artist(self, artist: dict):
        box, head, lb = self._page_box()
        self._push(f"artist-{artist['id']}-{len(self.views)}", box)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        t = Gtk.Label(label=artist.get("title", ""), xalign=0, wrap=True)
        t.add_css_class("cm-title")
        info.append(t)
        play = Gtk.Button(label="Play all", halign=Gtk.Align.START, sensitive=False)
        info.append(play)
        head.append(info)
        self._note(lb, "Loading…")

        def got(body):
            albums = [a for a in (body or {}).get("items") or [] if a.get("kind") == "album"]
            self._clear(lb)
            if body is None:
                self._note(lb, "Can't reach the music library.")
                return
            if not albums:
                self._note(lb, "No albums.")
            for a in albums:
                lb.append(self._item_row(a))
            if albums:
                play.set_sensitive(True)
                play.connect("clicked", lambda *_: self._play_albums(albums, artist.get("title", "")))
        self._get(f"/api/v1/items/{artist['id']}/children", got)

    # -- playing ------------------------------------------------------------------------------------

    def _play(self, tracks: list, start: int, shuffle: bool = False):
        if not tracks:
            return
        if shuffle:
            self.player.queue.shuffle = True
            start = random.randrange(len(tracks))
        self.player.play_entries(tracks, start)

    def _play_track(self, track: dict):
        """A track from a search: its album, from that track."""
        parent = track.get("parent_id")
        if parent:
            self._open_album(int(parent), play_from=int(track["id"]))
        else:
            self._play([core.entry(track)], 0)

    def _play_albums(self, albums: list, artist: str):
        """An artist's Play all: every album's tracks, in the order listed."""
        out: list = []
        left = list(albums)

        def next_album():
            if not left or len(out) >= 500:
                self._play(out[:500], 0)
                return
            a = left.pop(0)
            self._get(f"/api/v1/items/{a['id']}/children",
                      lambda body: (out.extend(core.album_queue(a, (body or {}).get("items") or [], artist)),
                                    next_album()))
        next_album()
