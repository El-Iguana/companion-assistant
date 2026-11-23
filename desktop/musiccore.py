"""Cantinero's music player, the decisions (RESIDENT_PLAN.md R-4).

No GTK and no GStreamer here, so every rule is tested under plain CPython;
``desktop/music.py`` is the window and the pipeline that carry them out.

Commands, from Cantinero through the resident (all but ``duck``/``unduck`` from the page
carry an ``id`` and get an ack):
  play {queue: [{id, title, artist, album, artwork_id, duration_ms}], start_index}
  pause, resume, stop, next, previous, seek {position_ms}, volume {level}, duck {level}, unduck
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

DUCK_EXPIRES_S = 60.0       # a duck nobody undoes restores itself
STATE_EVERY_S = 5.0
UPCOMING = 5                # titles after the current one, in the state
PREVIOUS_RESTARTS_AFTER_S = 3.0

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
    items: list = field(default_factory=list)
    index: int = 0

    @property
    def current(self) -> Optional[dict]:
        return self.items[self.index] if 0 <= self.index < len(self.items) else None

    def load(self, items: list, start: int = 0) -> None:
        self.items, self.index = list(items), max(0, min(int(start), max(0, len(items) - 1)))

    def advance(self) -> bool:
        """To the next item; False at the end (the queue stays, stopped on its last item)."""
        if self.index + 1 < len(self.items):
            self.index += 1
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
