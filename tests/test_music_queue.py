"""Shuffle and repeat in the player's queue (desktop/musiccore.Queue, Music M2). No GTK needed."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
import musiccore as core  # noqa: E402

TRACKS = [{"id": i, "title": f"t{i}"} for i in range(1, 9)]


def ids(q):
    return [e["id"] for e in q.items]


def test_plain_queue_stops_at_the_end():
    q = core.Queue()
    q.load(TRACKS, 6)
    assert q.advance() and q.current["id"] == 8
    assert not q.advance() and q.current["id"] == 8


def test_repeat_all_wraps():
    q = core.Queue(repeat="all")
    q.load(TRACKS, 7)
    assert q.advance() and q.index == 0


def test_repeat_one_replays_only_when_the_track_ends():
    q = core.Queue(repeat="one")
    q.load(TRACKS, 2)
    assert q.advance(auto=True) and q.current["id"] == 3      # ended by itself: again
    assert q.advance() and q.current["id"] == 4               # "next" still moves


def test_cycle_repeat():
    q = core.Queue()
    assert [q.cycle_repeat() for _ in range(3)] == ["all", "one", "off"]


def test_shuffle_keeps_the_current_track_and_every_track():
    q = core.Queue()
    q.load(TRACKS, 3)
    q.set_shuffle(True, random.Random(1))
    assert q.index == 0 and q.current["id"] == 4
    assert sorted(ids(q)) == sorted(e["id"] for e in TRACKS)
    assert ids(q) != [e["id"] for e in TRACKS]


def test_shuffle_off_restores_the_order_on_the_same_track():
    q = core.Queue()
    q.load(TRACKS, 0)
    q.set_shuffle(True, random.Random(2))
    q.advance(); q.advance()
    playing = q.current["id"]
    q.set_shuffle(False)
    assert ids(q) == [e["id"] for e in TRACKS]
    assert q.current["id"] == playing


def test_a_new_queue_is_shuffled_when_shuffle_is_on():
    q = core.Queue(shuffle=True)
    q.load(TRACKS, 5, random.Random(3))
    assert q.current["id"] == 6 and q.index == 0 and len(q.items) == 8


def test_the_state_carries_the_modes():
    q = core.Queue(shuffle=True, repeat="one")
    q.load(TRACKS)
    st = core.state_message("playing", q, core.Volume(), 0, 0)
    assert st["shuffle"] is True and st["repeat"] == "one"
