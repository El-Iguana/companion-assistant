"""\"The album with <song>\" (#87). Orpheus and Fortuna are faked."""
from __future__ import annotations

import asyncio

import pytest

from services.skills import media
from services.skills import orpheus_media as om
from test_dj_playback import PREDATOR, FakeOrpheus
from test_fortuna import _ctx, _track, fake_fortuna

ANTS = [_track(238, "Ants Marching", "Dave Matthews Band (2014)", 1.0),
        _track(257, "Ants Marching", "Dave Matthews Band (1994)", 1.0),
        _track(388, "Ants Marching", "Dave Matthews Band (2025)", 1.0),
        _track(580, "Time Is Marching", "John Lee Hooker (1991)", 0.43)]
PARENTS = {238: (237, "Remember Two Things"), 257: (250, "Under The Table And Dreaming"),
           388: (386, "Where Are You Going: The Singles"), 152: (140, "Best of AcDc")}


@pytest.mark.parametrize("query,song", [
    ("put on the album with Ants Marching", "Ants Marching"),
    ("play the record that has Crash Into Me", "Crash Into Me"),
    ("the album with the song Hells Bells", "Hells Bells"),
    ("play the album containing \"Satellite\"", "Satellite"),
    ("play Back in Black", ""),
    ("play some albums with guitar", ""),
])
def test_the_ask_is_recognised(query, song):
    assert media.album_of(query) == song


def _world(monkeypatch, tracks):
    o = FakeOrpheus(monkeypatch, [PREDATOR], tracks, ambiguous=len(tracks) > 1)

    async def request(method, url, token="", **kw):
        item = int(url.rsplit("/", 1)[1])
        album, title = PARENTS[item]
        return {"id": item, "parent": {"id": album, "title": title}}

    async def expand(base, token, *, item_id=None, playlist_id=""):
        o.expanded.append(item_id)
        return [{"id": t} for t in (item_id * 10, 257, 238, 388, 152, item_id * 10 + 1)]
    monkeypatch.setattr(om, "_request", request)
    monkeypatch.setattr(om, "expand", expand)
    return o


def test_one_album_plays_from_the_song(tmp_path, monkeypatch):
    o = _world(monkeypatch, [_track(152, "Back in Black - Hells Bells", "ACDC", 0.78)])
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9)})
    said = asyncio.run(media.fast_play(_ctx(tmp_path), "play the album with Hells Bells", "Mira"))
    assert said == "Playing Best of AcDc by ACDC from Back in Black - Hells Bells on Predator."
    assert o.expanded == [140] and o.sent[0][1]["start_index"] == 4


def test_several_albums_ask_and_the_answer_plays_that_album_from_the_song(tmp_path, monkeypatch):
    o = _world(monkeypatch, ANTS)
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9), "Which did the user choose": ("2", 0.93)})
    ctx = _ctx(tmp_path, session_id="s1")
    asked = asyncio.run(media.fast_play(ctx, "put on the album with Ants Marching", "Mira"))
    assert asked == ("Which one: Remember Two Things by Dave Matthews Band, Under The Table And Dreaming by "
                     "Dave Matthews Band, or Where Are You Going: The Singles by Dave Matthews Band?")
    said = asyncio.run(media.fast_play(ctx, "Under the Table and Dreaming", "Mira"))
    assert "Under The Table And Dreaming" in said and o.expanded == [250] and o.sent[0][1]["start_index"] == 1


def test_a_song_that_isnt_there_is_said(tmp_path, monkeypatch):
    o = _world(monkeypatch, [])
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9)})
    said = asyncio.run(media.fast_play(_ctx(tmp_path), "play the album with Tom Sawyer", "Mira"))
    assert said == "I can't find a song called Tom Sawyer in your music." and not o.sent


def test_the_tool_takes_album_of_and_ends_the_turn(tmp_path, monkeypatch):
    o = _world(monkeypatch, [_track(152, "Back in Black - Hells Bells", "ACDC", 0.78)])
    out = asyncio.run(media._play(_ctx(tmp_path), {"album_of": "Hells Bells"}))
    assert out["played"] and out["ok"] and "Best of AcDc" in out["final"] and o.expanded == [140]


def test_fortuna_reads_the_years_she_doesnt_say(tmp_path, monkeypatch):
    o = _world(monkeypatch, ANTS)
    seen = {}
    from services import fortuna

    async def choose(state, instructions, criteria):
        if "want the assistant" in instructions:
            return ("play_music", 0.9)
        seen.update(criteria)
        return ("2", 0.9)
    monkeypatch.setattr(fortuna, "choose", choose)
    ctx = _ctx(tmp_path, session_id="s1")
    asked = asyncio.run(media.fast_play(ctx, "put on the album with Ants Marching", "Mira"))
    assert "1994" not in asked                                      # not said aloud
    asyncio.run(media.fast_play(ctx, "the one from when I was in college", "Mira"))   # Fortuna's to place
    assert seen["2"] == "the second one: Under The Table And Dreaming by Dave Matthews Band (1994)"
    assert o.expanded == [250]


OPTS = [{"name": "Remember Two Things — Dave Matthews Band (2014)"},
        {"name": "Under The Table And Dreaming — Dave Matthews Band (1994)"},
        {"name": "Where Are You Going: The Singles — Dave Matthews Band (2025)"}]


@pytest.mark.parametrize("text,want", [
    ("the first", 0), ("first one", 0), ("the second one please", 1), ("number three", 2), ("the last one", 2),
    ("the nineties one", 1), ("the 1990s one", 1), ("the 2014 one", 0),
    ("the singles", 2), ("Under the Table and Dreaming", 1), ("remember two things", 0),
    ("the live one", None), ("Dave Matthews Band", None), ("whichever", None),
])
def test_answers_code_can_place(text, want):
    assert media.answer_pick(text, OPTS) == want
