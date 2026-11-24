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


def test_several_albums_play_one_of_them_from_the_song(tmp_path, monkeypatch):
    o = _world(monkeypatch, ANTS)
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9)})
    said = asyncio.run(media.fast_play(_ctx(tmp_path), "put on the album with Ants Marching", "Mira"))
    assert isinstance(said, media.Done) and "Which" not in said          # never a question (user, 2026-11-24)
    album = o.expanded[0]
    assert album in (237, 250, 386) and "Ants Marching" in said
    start = o.sent[0][1]["start_index"]
    assert [238, 257, 388][[237, 250, 386].index(album)] == [album * 10, 257, 238, 388, 152, album * 10 + 1][start]


def test_a_song_that_isnt_there_is_said(tmp_path, monkeypatch):
    o = _world(monkeypatch, [])
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9)})
    said = asyncio.run(media.fast_play(_ctx(tmp_path), "play the album with Tom Sawyer", "Mira"))
    assert said == "I can't find a song called Tom Sawyer in your music." and not o.sent


def test_the_tool_takes_album_of_and_ends_the_turn(tmp_path, monkeypatch):
    o = _world(monkeypatch, [_track(152, "Back in Black - Hells Bells", "ACDC", 0.78)])
    out = asyncio.run(media._play(_ctx(tmp_path), {"album_of": "Hells Bells"}))
    assert out["played"] and out["ok"] and "Best of AcDc" in out["final"] and o.expanded == [140]
