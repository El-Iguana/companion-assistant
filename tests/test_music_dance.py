"""#116: the player tells the avatar the playing track's tempo, when the switch is on."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
import musiccore as core  # noqa: E402


def _queue(*entries, index=0):
    q = core.Queue()
    q.load([dict(e) for e in entries], index)
    return q


def test_she_dances_only_to_a_playing_track_with_a_tempo_and_the_switch_on():
    song, book = {"id": 1, "bpm": 128.0}, {"id": 2}
    assert core.dance_bpm(True, "playing", song, None) == 128.0
    assert core.dance_bpm(False, "playing", song, None) is None             # switch off
    assert core.dance_bpm(True, "playing", book, 128.0) is None             # spoken word: no tempo
    assert core.dance_bpm(True, "paused", song, 128.0) is None
    assert core.dance_bpm(True, "idle", song, 128.0) is None
    assert core.dance_bpm(True, "error", song, 128.0) is None
    assert core.dance_bpm(True, "playing", None, 128.0) is None
    assert core.dance_bpm(True, "playing", {"id": 3, "bpm": "x"}, None) is None


def test_between_tracks_she_keeps_going():
    assert core.dance_bpm(True, "loading", {"id": 4}, 128.0) == 128.0
    assert core.dance_bpm(True, "loading", {"id": 4}, None) is None
    assert core.dance_bpm(False, "loading", {"id": 4}, 128.0) is None


def test_tempos_are_asked_for_the_current_track_and_the_next_few_once():
    q = _queue(*[{"id": i} for i in range(10)], index=2)
    q.items[3]["bpm"] = 120.0                                               # came with the queue
    q.items[4]["_tempo_asked"] = True
    assert core.tempo_ids(q) == [2, 5, 6]
    assert core.tempo_ids(core.Queue()) == []


def test_the_page_script_keeps_the_tempo_for_an_avatar_that_loads_later():
    js = core.dance_script(128.5)
    assert "window.__cantineroDance = 128.5;" in js and "'avatar:dance'" in js and "bpm: 128.5" in js
    assert "bpm: null" in core.dance_script(None) and json.dumps(None) in core.dance_script(None)
