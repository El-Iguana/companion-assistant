"""Cantinero's music player, the decisions (RESIDENT_PLAN.md R-4).

No GTK and no GStreamer here, so every rule is tested under plain CPython;
``desktop/music.py`` is the window and the pipeline that carry them out.

Commands, from Cantinero through the resident (all but ``duck``/``unduck`` from the page
carry an ``id`` and get an ack):
  play {queue: [{id, title, artist, album, artwork_id, duration_ms}], start_index}
  pause, resume, stop, next, previous, seek {position_ms}, volume {level}, duck {level}, unduck
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any, Optional

DUCK_EXPIRES_S = 60.0       # a duck nobody undoes restores itself
STATE_EVERY_S = 5.0
UPCOMING = 5                # titles after the current one, in the state
PREVIOUS_RESTARTS_AFTER_S = 3.0
REPEATS = ("off", "all", "one")      # the full player's repeat button cycles through these

COMMANDS = ("play", "pause", "resume", "stop", "next", "previous", "seek", "volume", "duck", "unduck")

# What a GStreamer player tells Orpheus it plays: everything, so music is served as it is
# (direct), never re-encoded. Orpheus's report schema (devices/capabilities.go).
CAPABILITIES = {
    "engine": "gstreamer",
    "containers": ["mp3", "flac", "mp4", "ogg", "matroska", "webm", "wav", "aiff", "ape", "wv"],
    "video": [],
    "audio": [{"codec": c, "max_channels": 8}
              for c in ("aac", "mp3", "flac", "opus", "vorbis", "alac", "ac3", "eac3", "pcm", "ape", "wavpack")],
    "audio_passthrough": [],
    "subtitles": {"text": [], "image": []},
    "max_bitrate": None,
}


# --- volume ---------------------------------------------------------------------------------

@dataclass
class Volume:
    """The listener's level (0-100) and a duck on top: ``duck 25`` plays at 25 % of that level
    and remembers it; ``unduck`` restores it exactly; ``volume`` while ducked moves the level
    it returns to; a duck left alone for a minute undoes itself."""

    level: int = 70
    duck_pct: Optional[int] = None
    duck_until: float = 0.0

    @property
    def ducked(self) -> bool:
        return self.duck_pct is not None

    @property
    def effective(self) -> float:
        """0.0-1.0 as the listener hears it (the pipeline maps it to a cubic gain)."""
        v = self.level / 100
        return v * self.duck_pct / 100 if self.ducked else v

    def set(self, level: int) -> None:
        self.level = max(0, min(100, int(level)))

    def duck(self, pct: int, now: float) -> None:
        self.duck_pct = max(0, min(100, int(pct)))
        self.duck_until = now + DUCK_EXPIRES_S

    def unduck(self) -> None:
        self.duck_pct = None

    def expired(self, now: float) -> bool:
        if self.ducked and now >= self.duck_until:
            self.unduck()
            return True
        return False


# --- the queue --------------------------------------------------------------------------------

@dataclass
class Queue:
    """What plays, in the order it plays. ``shuffle`` reorders it: the current item first, the
    rest at random, so "what's next?" and the window's list stay true; switching it off puts
    the order it came in back, still on the same item. ``repeat``: off, all (wrap at the end)
    or one (a track that ends starts again; next and previous still move)."""

    items: list = field(default_factory=list)
    index: int = 0
    shuffle: bool = False
    repeat: str = "off"
    original: list = field(default_factory=list)      # the order it was loaded in

    @property
    def current(self) -> Optional[dict]:
        return self.items[self.index] if 0 <= self.index < len(self.items) else None

    def load(self, items: list, start: int = 0, rng: random.Random | None = None) -> None:
        self.items, self.index = list(items), max(0, min(int(start), max(0, len(items) - 1)))
        self.original = list(self.items)
        if self.shuffle:
            self._shuffle(rng)

    def _shuffle(self, rng: random.Random | None = None) -> None:
        cur = self.current
        rest = [e for i, e in enumerate(self.items) if i != self.index]
        (rng or random).shuffle(rest)
        self.items, self.index = ([cur] if cur is not None else []) + rest, 0

    def set_shuffle(self, on: bool, rng: random.Random | None = None) -> None:
        on = bool(on)
        if on == self.shuffle:
            return
        self.shuffle = on
        if on:
            self._shuffle(rng)
            return
        cur = self.current
        self.items = list(self.original)
        self.index = next((i for i, e in enumerate(self.items) if e is cur), 0)

    def cycle_repeat(self) -> str:
        self.repeat = REPEATS[(REPEATS.index(self.repeat) + 1) % len(REPEATS)]
        return self.repeat

    def advance(self, auto: bool = False) -> bool:
        """To the next item: True when there is one to play (``auto``: the track ended by itself,
        so repeat one plays it again). False at the end without repeat all (the queue stays,
        stopped on its last item)."""
        if not self.items:
            return False
        if auto and self.repeat == "one":
            return True
        if self.index + 1 < len(self.items):
            self.index += 1
            return True
        if self.repeat == "all":
            self.index = 0
            return True
        return False

    def back(self, position_s: float) -> bool:
        """``previous``: restart this track if it has played a few seconds, else the one before."""
        if position_s > PREVIOUS_RESTARTS_AFTER_S or self.index == 0:
            return False                     # caller seeks to 0
        self.index -= 1
        return True


# --- commands -> actions ------------------------------------------------------------------

@dataclass
class Action:
    kind: str                                  # load, pause, resume, stop, next, previous, seek, volume, refuse
    seconds: float = 0.0
    error: str = ""


def _valid_entry(e: Any) -> bool:
    return isinstance(e, dict) and isinstance(e.get("id"), int)


def reduce(cmd: dict, queue: Queue, volume: Volume, now: float, has_media: bool) -> Action:
    """What a command means for this player. Never raises: a bad command is a refusal the
    server hears in the ack."""
    kind = cmd.get("type")
    if kind not in COMMANDS:
        return Action("refuse", error="unknown_command")
    if kind == "play":
        q = cmd.get("queue")
        if not isinstance(q, list) or not q or not all(_valid_entry(e) for e in q):
            return Action("refuse", error="bad_command")
        start = cmd.get("start_index", 0)
        if not isinstance(start, int) or not 0 <= start < len(q):
            return Action("refuse", error="bad_command")
        queue.load(q, start)
        return Action("load")
    if kind in ("volume", "duck"):
        level = cmd.get("level")
        if not isinstance(level, int) or not 0 <= level <= 100:
            return Action("refuse", error="bad_command")
        if kind == "volume":
            volume.set(level)
        else:
            volume.duck(level, now)
        return Action("volume")
    if kind == "unduck":
        volume.unduck()
        return Action("volume")
    if not has_media:
        return Action("refuse", error="nothing_playing")
    if kind == "seek":
        pos = cmd.get("position_ms")
        if not isinstance(pos, int) or pos < 0:
            return Action("refuse", error="bad_command")
        return Action("seek", seconds=pos / 1000)
    return Action(kind)


# --- state ------------------------------------------------------------------------------------

def _brief(e: dict) -> dict:
    return {"id": e.get("id"), "title": e.get("title", ""), "artist": e.get("artist", ""),
            "album": e.get("album", "")}


def state_message(status: str, queue: Queue, volume: Volume, position_s: float, duration_s: float) -> dict:
    """What the player tells Cantinero: enough for "what's playing?" and "what's next?"."""
    cur = queue.current
    return {
        "type": "state",
        "status": status,                     # idle, loading, playing, paused, error
        "index": queue.index if cur else 0,
        "length": len(queue.items),
        "now": _brief(cur) if cur and status != "idle" else None,
        "upcoming": [_brief(e) for e in queue.items[queue.index + 1: queue.index + 1 + UPCOMING]] if cur else [],
        "position_ms": int(max(0.0, position_s) * 1000),
        "duration_ms": int(max(0.0, duration_s) * 1000),
        "volume": volume.level,
        "ducked": volume.ducked,
        "shuffle": queue.shuffle,
        "repeat": queue.repeat,
    }


@dataclass
class StateThrottle:
    """State goes up when anything but the position changes, and every 5 s while playing."""

    last_key: Optional[tuple] = None
    last_at: float = -1e9

    def due(self, msg: dict, now: float) -> bool:
        key = (msg["status"], msg["index"], msg["length"], msg["volume"], msg["ducked"],
               (msg.get("now") or {}).get("id"))
        if key != self.last_key or (msg["status"] == "playing" and now - self.last_at >= STATE_EVERY_S):
            self.last_key, self.last_at = key, now
            return True
        return False


def fmt_time(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# The micro player under her bar (Music M1) sends its buttons as "music:<action>[:<value>]"
# through the shell's message handler. These are the ones it may send.
PAGE_ACTIONS = ("toggle", "previous", "next", "stop", "seek", "volume", "open")


def page_command(msg: str) -> Optional[tuple]:
    """("seek", 81.5), ("next", None)…, or None for anything else."""
    parts = str(msg or "").split(":")
    if len(parts) < 2 or parts[0] != "music" or parts[1] not in PAGE_ACTIONS:
        return None
    action = parts[1]
    if action in ("seek", "volume"):
        try:
            value = float(parts[2])
        except (IndexError, ValueError):
            return None
        if value != value or value < 0:          # NaN or negative
            return None
        return (action, min(value, 100.0) if action == "volume" else value)
    return (action, None)


# --- dancing to the music (#116) -------------------------------------------------------------
# The avatar dances at the playing track's tempo (vtuber.mjs, avatar:dance). Tempos come with
# the server's queue entries (``bpm``) or are asked for (GET /pmedia/music/tempo?ids=) for a
# queue built here (the library). A track with none, spoken word above all, has no dance.
TEMPO_AHEAD = 4                           # ask for the current track's and the next few's


def tempo_ids(queue: "Queue") -> list:
    """The ids worth asking about: the current track and the next few, not asked yet."""
    if queue.current is None:
        return []
    near = queue.items[queue.index: queue.index + 1 + TEMPO_AHEAD]
    return [e["id"] for e in near if "bpm" not in e and not e.get("_tempo_asked")]


def dance_bpm(enabled: bool, status: str, item: Optional[dict], prev: Optional[float]) -> Optional[float]:
    """The tempo she should dance at now, or None (still). Between tracks (loading) she keeps
    going rather than stop and start again; paused, stopped, or a track with no tempo: still."""
    if not enabled or item is None:
        return None
    if status == "loading":
        return prev
    if status != "playing":
        return None
    try:
        bpm = float(item.get("bpm") or 0)
    except (TypeError, ValueError):
        return None
    return bpm if bpm > 0 else None


def dance_script(bpm: Optional[float]) -> str:
    """What the shell runs in the page: vtuber.mjs listens for avatar:dance."""
    # Also kept on window: the avatar module loads on demand, and reads it when it starts, so
    # a dance sent before it was there isn't lost.
    b = json.dumps(bpm)
    return (f"window.__cantineroDance = {b}; "
            f"window.dispatchEvent(new CustomEvent('avatar:dance', {{detail: {{bpm: {b}}}}}))")


# --- the library browser (Music M3) ------------------------------------------------------------
# What the full player's Library tab makes of Orpheus's answers. The same queue entries as the
# server's DJ builds (services/skills/orpheus_media.expand), so a play by hand looks the same.

def entry(item: dict, artist: str = "", album: str = "") -> dict:
    return {"id": int(item["id"]), "title": item.get("title") or "", "artist": artist, "album": album,
            "artwork_id": item.get("artwork_id"), "duration_ms": item.get("duration_ms")}


def album_artist(album: dict) -> str:
    """An album item's artist (GET /items/{id} carries album.album_artist)."""
    return ((album.get("album") or {}).get("album_artist") or {}).get("title", "") or ""


def album_queue(album: dict, children: list, artist: str = "") -> list:
    """An album's playable tracks in disc and track order, as queue entries."""
    artist = artist or album_artist(album)
    kids = sorted((k for k in children or [] if k.get("kind") == "track" and k.get("playable")),
                  key=lambda k: (k.get("disc_number") or 0, k.get("track_number") or 0, k.get("sort_title") or ""))
    return [entry(k, artist, album.get("title", "")) for k in kids]


def browsable_artists(items: list) -> list:
    """Artists worth a row: a guest credit on one track is an artist item with no albums."""
    return [i for i in items or [] if i.get("kind") == "artist" and (i.get("child_count") or 0) > 0]


def album_line(album: dict) -> str:
    """The second line of an album row: year and track count."""
    bits = [str(album["year"])] if album.get("year") else []
    n = album.get("child_count") or 0
    if n:
        bits.append(f"{n} track{'s' if n != 1 else ''}")
    return " · ".join(bits)
