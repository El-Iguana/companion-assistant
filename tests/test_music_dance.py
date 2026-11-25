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


def test_the_page_script_keeps_the_dance_for_an_avatar_that_loads_later():
    js = core.dance_script({"bpm": 128.5, "phase": 3.25, "local": 128.0})
    assert 'window.__cantineroDance = {"bpm": 128.5, "phase": 3.25, "local": 128.0};' in js
    assert "'avatar:dance'" in js
    assert '{"bpm": null}' in core.dance_script(None)


def test_where_the_song_is_in_its_beats():
    beats = [1.0, 1.5, 2.0, 2.6, 3.2]
    assert core.beat_phase(beats, 1.25) == (0.5, 120.0)
    p, local = core.beat_phase(beats, 2.3)
    assert abs(p - 2.5) < 1e-9 and abs(local - 100.0) < 1e-9          # a slower gap: 100 BPM there
    assert core.beat_phase(beats, 0.5) == (-1.0, 120.0)                # before the first: carried back
    assert abs(core.beat_phase(beats, 3.8)[0] - 5.0) < 1e-9            # after the last: carried on
    assert core.beat_phase([1.0], 1.2) is None and core.beat_phase([], 1.2) is None


def test_the_dance_state_carries_the_beat_while_playing():
    song = {"id": 1, "bpm": 120.0, "beats": [0.0, 0.5, 1.0, 1.5]}
    assert core.dance_state(True, "playing", song, None, 0.75) == {"bpm": 120.0, "phase": 1.5, "local": 120.0}
    assert core.dance_state(True, "playing", {"id": 2, "bpm": 90.0}, None, 3.0) == {"bpm": 90.0}   # no beats yet
    assert core.dance_state(True, "loading", {"id": 3}, {"bpm": 120.0, "phase": 9}, 0) == {"bpm": 120.0}
    assert core.dance_state(False, "playing", song, None, 0.75) is None
    assert core.dance_state(True, "paused", song, None, 0.75) is None
