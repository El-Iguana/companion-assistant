"""The DJ's playback on Cantinero's own player (RESIDENT_PLAN.md R-4–R-7): media_play,
media_control, choosing the resident, the pending choice, the fast path and ducking. Orpheus is faked.

    .venv/bin/python -m pytest -q tests/test_dj_playback.py
"""
from __future__ import annotations

import asyncio

import pytest

import main
from models.schema import CharacterCard
from services import skills
from services.skills import media
from services.skills import orpheus_media as om
from test_chat_stream import _events, _reload, cur, session  # noqa: F401 — fixtures

SIGUR = [{"item_id": 9042, "kind": "album", "display": "( ) — Sigur Rós", "score": 0.83},
         {"item_id": 9050, "kind": "album", "display": "Ágætis byrjun — Sigur Rós", "score": 0.8}]


class FakeOrpheus:
    """The DJ's world, faked: Orpheus's resolve and queue expansion, and the user's residents
    on the hub (RESIDENT_PLAN.md R-6, R-7). Commands to residents are recorded in ``sent``."""

    def __init__(self, monkeypatch, players=(), candidates=(), ambiguous=False):
        self.players = [dict(p) for p in players]
        self.candidates, self.ambiguous = list(candidates), ambiguous
        self.sent, self.refuse, self.expanded = [], None, []
        monkeypatch.setattr(om, "resolve", self.resolve)
        monkeypatch.setattr(om, "expand", self.expand)
        monkeypatch.setattr(media, "_residents", lambda user_id: [dict(p, state=p.get("state") or {})
                                                                  for p in self.players])
        monkeypatch.setattr(media, "_send", self.send)

    async def resolve(self, base, token, query, kinds, limit=5):
        return {"candidates": self.candidates, "needs_disambiguation": self.ambiguous,
                "disambiguation_prompt": "( ), or Ágætis byrjun?" if self.ambiguous else ""}

    async def expand(self, base, token, *, item_id=None, playlist_id=""):
        self.expanded.append(item_id or playlist_id)
        return [{"id": 1000 + n, "title": f"t{n}", "artist": "a", "album": "b"} for n in range(3)]

    async def send(self, resident_id, body):
        if self.refuse:
            raise om.OrpheusError(self.refuse, self.refuse.lower())
        self.sent.append((resident_id, body))


def _plays(o):
    """What was played where: (resident, what was expanded) per play command."""
    plays = [(d, b) for d, b in o.sent if b["type"] == "play"]
    return [(d, e) for (d, _), e in zip(plays, o.expanded)]


def _ctx(tmp_path, **kw):
    om.save(tmp_path, {"url": "http://o", "device_id": "ctl", "token": "t"})
    return skills.SkillContext(user_id="u1", root=tmp_path, **kw)


PREDATOR = {"id": "p1", "name": "Predator", "role": "player", "online": True, "state": {"volume": 70}}


def test_a_clear_match_plays_at_once(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR], SIGUR[:1])
    ctx = _ctx(tmp_path)
    out = asyncio.run(media._play(ctx, {"query": "sigur ros", "shuffle": True}))
    assert out == {"ok": True, "played": True, "playing": "( ) — Sigur Rós", "player": "Predator",
                   "final": "Playing ( ) by Sigur Rós on Predator."}                # said without a second pass
    assert _plays(o) == [("p1", 9042)] and len(o.sent[0][1]["queue"]) == 3
    assert o.sent[0][1]["start_index"] == 0


def test_an_ambiguous_match_plays_one_and_an_id_still_plays(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR], SIGUR, ambiguous=True)
    ctx = _ctx(tmp_path)
    out = asyncio.run(media._play(ctx, {"query": "sigur ros"}))      # never a question (user, 2026-11-24)
    assert out["played"] is True and "choices" not in out and len(_plays(o)) == 1
    assert "waiting" not in skills.REGISTRY["dj"].note(ctx)
    picked = asyncio.run(media._play(ctx, {"id": "9050"}))           # an id from media_find
    assert picked["played"] and _plays(o)[-1] == ("p1", 9050)


def test_a_playlist_plays_by_its_id(tmp_path, monkeypatch):
    pl = {"playlist_id": "4b8e-uuid", "kind": "playlist", "display": "Road Trip — playlist", "score": 1.0}
    o = FakeOrpheus(monkeypatch, [PREDATOR], [pl])
    asyncio.run(media._play(_ctx(tmp_path), {"query": "my road trip playlist"}))
    assert _plays(o) == [("p1", "4b8e-uuid")]


def test_where_it_plays(tmp_path, monkeypatch):
    """R-6: named, else the resident's own, else the default, else the only one open."""
    lounge = dict(PREDATOR, id="p2", name="Lounge")
    o = FakeOrpheus(monkeypatch, [PREDATOR, lounge], SIGUR[:1])
    ctx = _ctx(tmp_path)
    assert asyncio.run(media._play(ctx, {"query": "x"}))["error"] == "WHICH_PLAYER"
    assert asyncio.run(media._play(ctx, {"query": "x", "player": "lounge"}))["player"] == "Lounge"
    media.set_default_player(tmp_path, "p1", "Predator")
    assert asyncio.run(media._play(ctx, {"query": "y"}))["player"] == "Predator"            # the default
    resident = _ctx(tmp_path, player_id="p2")
    assert asyncio.run(media._play(resident, {"query": "z"}))["player"] == "Lounge"         # its own wins
    assert asyncio.run(media._play(resident, {"query": "z", "player": "predator"}))["player"] == "Predator"
    assert [d for d, _ in o.sent] == ["p2", "p1", "p2", "p1"]


def test_a_closed_chosen_player_is_an_error_not_another_room(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [dict(PREDATOR, online=False), dict(PREDATOR, id="p2", name="Lounge")], SIGUR[:1])
    ctx = _ctx(tmp_path)
    media.set_default_player(tmp_path, "p1", "Predator")
    out = asyncio.run(media._play(ctx, {"query": "x"}))
    assert out["error"] == "PLAYER_OFFLINE" and "Predator" in out["message"] and not o.sent
    assert asyncio.run(media._play(ctx, {"query": "x", "player": "nowhere"}))["error"] == "NO_SUCH_PLAYER"


def test_control_and_its_refusals(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR])
    ctx = _ctx(tmp_path)
    assert asyncio.run(media._control(ctx, {"action": "seek", "position_s": 90}))["ok"]
    assert asyncio.run(media._control(ctx, {"action": "volume", "level": 30}))["ok"]
    assert o.sent == [("p1", {"type": "seek", "position_ms": 90000}), ("p1", {"type": "volume", "level": 30})]
    assert asyncio.run(media._control(ctx, {"action": "seek"}))["error"] == "BAD_ARGUMENTS"
    o.refuse = "PLAYER_CLOSED"
    out = asyncio.run(media._control(ctx, {"action": "resume"}))
    assert out["error"] == "PLAYER_CLOSED"


@pytest.mark.parametrize("said,cmd", [
    ("Pause.", {"type": "pause"}), ("pause the music please", {"type": "pause"}),
    ("Stop the music", {"type": "stop"}), ("Stop.", {"type": "stop"}), ("stop it", {"type": "stop"}),
    ("stop playing", {"type": "stop"}), ("turn the music off", {"type": "stop"}), ("That's enough.", {"type": "stop"}),
    ("could you resume", {"type": "resume"}),
    ("Skip this song", {"type": "next"}), ("next track", {"type": "next"}), ("Next.", {"type": "next"}),
    ("go back", {"type": "previous"}), ("play the previous song", {"type": "previous"}),
    ("Turn it up", {"type": "volume", "step": 15}), ("a bit quieter", {"type": "volume", "step": -15}),
    ("set the volume to 40%", {"type": "volume", "level": 40}), ("volume 150", {"type": "volume", "level": 100}),
    ("Play.", {"type": "resume"}), ("play the music", {"type": "resume"}), ("could you play music", {"type": "resume"}),
])
def test_fast_commands(said, cmd):
    assert media.fast_command(said) == cmd
    assert media.fast_command(f"Mira, {said}", "Mira") == cmd        # her name in front, typed


@pytest.mark.parametrize("said", ["stop the timer", "continue", "I paused the movie earlier", "play Sigur Rós", "what's next on my list",
                                  "skip the intro and tell me about the weather", "turn it up to eleven, the meeting"])
def test_not_fast_commands(said):
    assert media.fast_command(said) is None                           # these go to her


def test_fast_control_answers_in_words(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR])
    ctx = _ctx(tmp_path)
    assert asyncio.run(media.fast_control(ctx, {"type": "volume", "step": -15})) == "Volume 55."
    paused = asyncio.run(media.fast_control(ctx, {"type": "pause"}))
    assert paused == "Paused." and isinstance(paused, media.Done)          # worked: not spoken
    o.refuse = "NOTHING_PLAYING"
    nothing = asyncio.run(media.fast_control(ctx, {"type": "next"}))
    assert nothing == "Nothing's playing." and not isinstance(nothing, media.Done)   # said aloud
    o.players[0]["online"] = False
    assert asyncio.run(media.fast_control(ctx, {"type": "pause"})) == "The music player isn't on."


def test_the_web_pages_duck_goes_to_the_default_resident_and_never_raises(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR])
    om.save(tmp_path, {"url": "http://o", "device_id": "ctl", "token": "t"})
    asyncio.run(media.duck("u1", tmp_path, True))
    assert not o.sent                                                  # no default: nothing
    media.set_default_player(tmp_path, "p1", "Predator")
    asyncio.run(media.duck("u1", tmp_path, True))
    asyncio.run(media.duck("u1", tmp_path, False))
    assert o.sent == [("p1", {"type": "duck", "level": 25}), ("p1", {"type": "unduck"})]
    o.refuse = "PLAYER_OFFLINE"
    asyncio.run(media.duck("u1", tmp_path, True))                      # silent


def test_status_says_whats_playing_and_whats_next(tmp_path, monkeypatch):
    now = {"status": "playing", "index": 1, "length": 12, "volume": 60,
           "now": {"title": "Tom Sawyer", "artist": "Rush"}, "upcoming": [{"title": "Red Barchetta", "artist": "Rush"}]}
    FakeOrpheus(monkeypatch, [dict(PREDATOR, state=now), dict(PREDATOR, id="p2", name="Lounge", online=False)])
    out = asyncio.run(media._status(_ctx(tmp_path), {}))
    assert out["players"][0] == {"name": "Predator", "on": True, "status": "playing", "playing": "Tom Sawyer by Rush",
                                 "up_next": ["Red Barchetta by Rush"], "volume": 60, "track": 2, "of": 12}
    assert out["players"][1]["status"] == "off"


def test_no_residents_means_nowhere_to_play(tmp_path, monkeypatch):
    FakeOrpheus(monkeypatch, [], SIGUR[:1])
    assert asyncio.run(media._play(_ctx(tmp_path), {"query": "x"}))["error"] == "NO_PLAYERS"


def test_a_fast_command_never_reaches_the_model(cur, session, monkeypatch):
    card = main._load(cur.cards, session.character_id, CharacterCard)
    card.skills = ["dj"]
    main._save(card, cur.cards)
    o = FakeOrpheus(monkeypatch, [PREDATOR])
    om.save(cur.cards.parent, {"url": "http://o", "device_id": "ctl", "token": "t"})

    async def no_model(*a, **k):
        raise AssertionError("the model was called")
        yield
    monkeypatch.setattr(main.kobold, "chat_generate_stream", no_model)
    monkeypatch.setattr(main.expressions, "classify", no_model)
    monkeypatch.setattr(main.assistant_memory, "fold", lambda *a, **k: asyncio.sleep(0, False))

    async def go():
        resp = await main.chat(main.ChatRequest(session_id=session.id, input_text="Pause the music."), cur=cur)
        return _events([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])
    evs = asyncio.run(go())
    assert {"token": "Paused."} in evs and any(e.get("done") for e in evs)
    assert o.sent == [("p1", {"type": "pause"})]
    assert _reload(cur, session).messages[-1].content == "Paused."


def test_the_same_play_twice_doesnt_restart_the_music(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [dict(PREDATOR, state={"status": "playing"})], SIGUR[:1])
    ctx = _ctx(tmp_path)
    asyncio.run(media._play(ctx, {"query": "sigur ros"}))
    again = asyncio.run(media._play(ctx, {"query": "Sigur Ros"}))
    assert again["already_playing"] and again["final"] == "It's playing." and len(o.sent) == 1
    o.players[0]["state"] = {"status": "paused"}                      # paused since: play it again
    assert asyncio.run(media._play(ctx, {"query": "sigur ros"})).get("already_playing") is None
    assert len(o.sent) == 2


def test_spoken_names():
    assert media._spoken("Crash — Dave Matthews Band (1996)") == "Crash by Dave Matthews Band"
    assert media._spoken("Road Trip — playlist") == "the Road Trip playlist"
    assert media._spoken("Road Trip — Sam's playlist") == "Sam's Road Trip playlist"


def test_the_note_says_what_is_true_now(tmp_path, monkeypatch):
    now = {"status": "playing", "now": {"title": "Crash Into Me", "artist": "Dave Matthews Band"}}
    FakeOrpheus(monkeypatch, [dict(PREDATOR, state=now), dict(PREDATOR, id="p2", name="Lounge", online=False)])
    note = skills.REGISTRY["dj"].note(_ctx(tmp_path))
    assert "Predator: playing (Crash Into Me by Dave Matthews Band)" in note and "Lounge: not connected" in note


def test_a_finished_tool_ends_the_turn_without_a_second_pass(cur, session, monkeypatch):
    card = main._load(cur.cards, session.character_id, CharacterCard)
    card.skills = ["dj"]
    main._save(card, cur.cards)
    o = FakeOrpheus(monkeypatch, [PREDATOR], SIGUR[:1])
    om.save(cur.cards.parent, {"url": "http://o", "device_id": "ctl", "token": "t"})
    passes = []

    async def stream(messages, params):
        passes.append(1)
        yield main.ToolCallsEvent([{"id": "c1", "name": "media_play", "arguments": '{"query": "sigur ros"}'}])

    async def no_classifier(*a, **k):
        raise AssertionError("a fixed line was sent to the classifier")
    monkeypatch.setattr(main.kobold, "chat_generate_stream", stream)
    monkeypatch.setattr(main.expressions, "classify", no_classifier)
    monkeypatch.setattr(main.assistant_memory, "fold", lambda *a, **k: asyncio.sleep(0, False))
    card.vrm, card.avatar_mode = "baseline.vrm", "vrm"     # a card whose face would be classified
    main._save(card, cur.cards)

    async def go():
        resp = await main.chat(main.ChatRequest(session_id=session.id, input_text="play sigur ros"), cur=cur)
        return _events([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])
    evs = asyncio.run(go())
    assert len(passes) == 1 and {"token": "Playing ( ) by Sigur Rós on Predator."} in evs
    assert _reload(cur, session).messages[-1].content == "Playing ( ) by Sigur Rós on Predator."
