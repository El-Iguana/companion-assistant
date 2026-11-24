"""Vague plays through Fortuna over the album catalog (#80). Fortuna and Orpheus are faked."""
from __future__ import annotations

import asyncio
import random
import time

from services.skills import media, music_catalog
from services.skills import orpheus_media as om
from test_dj_playback import PREDATOR, SIGUR, FakeOrpheus
from test_fortuna import _ctx, fake_fortuna

ALBUMS = [
    {"id": 10, "title": "The Best of John Lee Hooker", "artist": "John Lee Hooker", "year": 1991, "spoken": False},
    {"id": 11, "title": "BEST SONGS OF MAROON 5", "artist": "Maroon 5", "year": 2013, "spoken": False},
    {"id": 12, "title": "The Joy of Hate, Unabridged", "artist": "", "year": None, "spoken": True},
    {"id": 13, "title": "Greatest Hits", "artist": "Bon Jovi", "year": 2010, "spoken": False},
    {"id": 14, "title": "Greatest Hits", "artist": "Neil Young", "year": 2004, "spoken": False},
]


def _catalog(root, albums=ALBUMS):
    music_catalog._save(root, {"built_at": time.time(), "albums": albums})


def test_labels_and_the_spoken_word_left_out():
    assert music_catalog.label(ALBUMS[0]) == "The Best of John Lee Hooker — John Lee Hooker (1991)"
    assert music_catalog.label(ALBUMS[2]) == "The Joy of Hate, Unabridged"
    assert [a["id"] for a in music_catalog.music_albums({"albums": ALBUMS})] == [10, 11, 13, 14]


def test_a_sample_is_capped_and_its_labels_unique():
    many = [dict(ALBUMS[0], id=i, title=f"A{i}") for i in range(60)]
    got = music_catalog.sample(many, 24, random.Random(1))
    assert len(got) == 24 and len(set(got)) == 24
    two = music_catalog.sample(ALBUMS[3:5] + ALBUMS[3:4])          # same album twice: one label
    assert len(two) == 2


def test_a_described_play_plays_fortunas_album_shuffled(tmp_path, monkeypatch):
    _catalog(tmp_path)
    o = FakeOrpheus(monkeypatch, [PREDATOR], [])
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9),
                               "name what to play": ("described", 0.98),
                               "Which album": ("The Best of John Lee Hooker — John Lee Hooker (1991)", 0.63)})
    said = asyncio.run(media.fast_play(_ctx(tmp_path), "play some blues", "Mira"))
    assert isinstance(said, media.Done) and "John Lee Hooker" in said and "shuffled" in said
    assert o.expanded == [10] and o.sent[0][1]["type"] == "play"


def test_a_clear_name_wins_over_described(tmp_path, monkeypatch):
    _catalog(tmp_path)
    o = FakeOrpheus(monkeypatch, [PREDATOR], [dict(SIGUR[0], kind="artist", display="Sigur Rós", score=1.0)])
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9),
                               "name what to play": ("described", 0.9),
                               "Which album": ("BEST SONGS OF MAROON 5 — Maroon 5 (2013)", 0.5)})
    asyncio.run(media.fast_play(_ctx(tmp_path), "play some Sigur Ros", "Mira"))
    assert o.expanded and o.expanded[0] != 11                       # Sigur Rós, not Fortuna's album


def test_no_catalog_yet_or_no_answer_goes_to_her(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR], [])
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9), "name what to play": ("described", 0.98)})
    assert asyncio.run(media.fast_play(_ctx(tmp_path), "play some blues", "Mira")) is None and not o.sent
    _catalog(tmp_path)
    assert asyncio.run(media.fast_play(_ctx(tmp_path), "play some blues", "Mira")) is None and not o.sent


def test_build_reads_orpheus_marks_spoken_word_and_asks_once(tmp_path, monkeypatch):
    pages = {"/api/v1/items?kind=album&limit=200": {"items": [
        {"id": 1, "title": "Crash", "year": 1996, "playable": True},
        {"id": 2, "title": "The Joy of Hate, Unabridged", "playable": True}], "next_cursor": None},
        "/api/v1/items/1": {"album": {"album_artist": {"title": "Dave Matthews Band"}}},
        "/api/v1/items/2": {"album": {}}}

    async def request(method, url, token="", **kw):
        return pages[url.replace("http://o", "")]
    monkeypatch.setattr(om, "_request", request)
    asked = fake_fortuna(monkeypatch, {"music album, or spoken": ("music", 0.9)})
    from services import fortuna

    async def choose(state, instructions, criteria):
        asked.append(state)
        return ("spoken", 0.94) if "Unabridged" in state else ("music", 0.97)
    monkeypatch.setattr(fortuna, "choose", choose)
    st = {"url": "http://o", "token": "t"}
    cat = asyncio.run(music_catalog.build(tmp_path, st))
    assert [(a["artist"], a["spoken"]) for a in cat["albums"]] == [("Dave Matthews Band", False), ("", True)]
    n = len(asked)
    asyncio.run(music_catalog.build(tmp_path, st))
    assert len(asked) == n                                          # already known: not asked again


# --- media_play ends the turn itself (#82) ---

from test_fortuna import BACK_IN_BLACK, _track  # noqa: E402


def test_the_tool_plays_a_described_request_and_says_so(tmp_path, monkeypatch):
    _catalog(tmp_path)
    o = FakeOrpheus(monkeypatch, [PREDATOR], [])
    fake_fortuna(monkeypatch, {"name what to play": ("described", 0.97),
                               "Which album": ("BEST SONGS OF MAROON 5 — Maroon 5 (2013)", 0.3)})
    out = asyncio.run(media._play(_ctx(tmp_path), {"query": "something upbeat"}))
    assert out["ok"] and "Maroon 5" in out["final"] and o.expanded == [11]


def test_the_tool_says_a_miss_itself(tmp_path, monkeypatch):
    FakeOrpheus(monkeypatch, [PREDATOR], [])
    fake_fortuna(monkeypatch, {"name what to play": ("named", 0.97)})
    out = asyncio.run(media._play(_ctx(tmp_path), {"query": "play Rush"}))
    assert not out["ok"] and out["final"] == "I can't find Rush in your music."


def test_the_tool_settles_a_tie_by_title(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR], BACK_IN_BLACK, ambiguous=True)
    fake_fortuna(monkeypatch, {"name what to play": ("named", 0.97)})
    out = asyncio.run(media._play(_ctx(tmp_path), {"query": "Back in Black"}))
    assert out["played"] and o.expanded == [141]


def test_the_tool_asks_either_or_itself_and_the_answer_takes_the_fast_lane(tmp_path, monkeypatch):
    two = [_track(1, "Crash", "Dave Matthews Band"), _track(2, "Crash", "Someone Else")]
    o = FakeOrpheus(monkeypatch, [PREDATOR], two, ambiguous=True)
    fake_fortuna(monkeypatch, {"name what to play": ("named", 0.97), "Which of these": None,
                               "Which did the user choose": ("1", 0.9)})
    ctx = _ctx(tmp_path, session_id="s1")
    out = asyncio.run(media._play(ctx, {"query": "Crash"}))
    assert out["asked"] and out["final"].startswith("Which one: Crash by Dave Matthews Band")
    said = asyncio.run(media.fast_play(ctx, "the first one", "Mira"))
    assert isinstance(said, media.Done) and o.expanded == [1]
