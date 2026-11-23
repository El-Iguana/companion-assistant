"""The DJ's playback (Orpheus Phase 5): media_play, media_control, choosing and opening a
player, the pending choice, the fast path and ducking. Orpheus is faked.

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
    """devices, resolve and command, recorded."""

    def __init__(self, monkeypatch, players=(), candidates=(), ambiguous=False):
        self.players = [dict(p) for p in players]
        self.candidates, self.ambiguous = list(candidates), ambiguous
        self.sent, self.refuse = [], None
        self.on_poll = None
        monkeypatch.setattr(om, "devices", self.devices)
        monkeypatch.setattr(om, "resolve", self.resolve)
        monkeypatch.setattr(om, "command", self.command)

    async def devices(self, base, token):
        if self.on_poll:
            self.on_poll()
        return [{"id": "ctl", "name": "Cantinero", "role": "controller", "online": False}] + self.players

    async def resolve(self, base, token, query, kinds, limit=5):
        return {"candidates": self.candidates, "needs_disambiguation": self.ambiguous,
                "disambiguation_prompt": "( ), or Ágætis byrjun?" if self.ambiguous else ""}

    async def command(self, base, token, device_id, body):
        if self.refuse:
            raise om.OrpheusError(self.refuse, self.refuse.lower())
        self.sent.append((device_id, body))
        return {"ok": True}


def _ctx(tmp_path, **kw):
    om.save(tmp_path, {"url": "http://o", "device_id": "ctl", "token": "t"})
    return skills.SkillContext(user_id="u1", root=tmp_path, **kw)


PREDATOR = {"id": "p1", "name": "Predator", "role": "player", "online": True, "state": {"volume": 70}}


def test_a_clear_match_plays_at_once(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR], SIGUR[:1])
    ctx = _ctx(tmp_path)
    out = asyncio.run(media._play(ctx, {"query": "sigur ros", "shuffle": True}))
    assert out == {"ok": True, "played": True, "playing": "( ) — Sigur Rós", "player": "Predator"}
    assert o.sent == [("p1", {"type": "play", "item_id": 9042, "shuffle": True})]


def test_an_ambiguous_match_asks_and_the_choice_survives_the_turn(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR], SIGUR, ambiguous=True)
    ctx = _ctx(tmp_path)
    out = asyncio.run(media._play(ctx, {"query": "sigur ros"}))
    assert out["played"] is False and out["ask"].startswith("( )") and not o.sent
    note = skills.REGISTRY["dj"].note(ctx)
    assert "Ágætis byrjun" in note and "[id 9050]" in note              # the next turn knows the choices
    picked = asyncio.run(media._play(ctx, {"id": "9050"}))
    assert picked["playing"] == "Ágætis byrjun — Sigur Rós"
    assert o.sent == [("p1", {"type": "play", "item_id": 9050})]
    assert "waiting" not in skills.REGISTRY["dj"].note(ctx)            # answered: forgotten


def test_a_playlist_plays_by_its_id(tmp_path, monkeypatch):
    pl = {"playlist_id": "4b8e-uuid", "kind": "playlist", "display": "Road Trip — playlist", "score": 1.0}
    o = FakeOrpheus(monkeypatch, [PREDATOR], [pl])
    asyncio.run(media._play(_ctx(tmp_path), {"query": "my road trip playlist"}))
    assert o.sent == [("p1", {"type": "play", "playlist_id": "4b8e-uuid"})]


def test_where_it_plays(tmp_path, monkeypatch):
    """R-6: named, else the resident's own, else the default, else the only one open."""
    lounge = dict(PREDATOR, id="p2", name="Lounge")
    o = FakeOrpheus(monkeypatch, [PREDATOR, lounge], SIGUR[:1])
    ctx = _ctx(tmp_path)
    assert asyncio.run(media._play(ctx, {"query": "x"}))["error"] == "WHICH_PLAYER"
    assert asyncio.run(media._play(ctx, {"query": "x", "player": "lounge"}))["player"] == "Lounge"
    media.set_default_player(tmp_path, "p1", "Predator")
    assert asyncio.run(media._play(ctx, {"query": "x"}))["player"] == "Predator"            # the default
    resident = _ctx(tmp_path, player_id="p2")
    assert asyncio.run(media._play(resident, {"query": "x"}))["player"] == "Lounge"         # its own wins
    assert asyncio.run(media._play(resident, {"query": "x", "player": "predator"}))["player"] == "Predator"
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
    o.refuse = "AUTOPLAY_BLOCKED"
    out = asyncio.run(media._control(ctx, {"action": "resume"}))
    assert out["error"] == "AUTOPLAY_BLOCKED" and "click" in out["message"]


@pytest.mark.parametrize("said,cmd", [
    ("Pause.", {"type": "pause"}), ("pause the music please", {"type": "pause"}),
    ("Stop the music", {"type": "pause"}), ("could you resume", {"type": "resume"}),
    ("Skip this song", {"type": "next"}), ("next track", {"type": "next"}), ("Next.", {"type": "next"}),
    ("go back", {"type": "previous"}), ("play the previous song", {"type": "previous"}),
    ("Turn it up", {"type": "volume", "step": 15}), ("a bit quieter", {"type": "volume", "step": -15}),
    ("set the volume to 40%", {"type": "volume", "level": 40}), ("volume 150", {"type": "volume", "level": 100}),
])
def test_fast_commands(said, cmd):
    assert media.fast_command(said) == cmd


@pytest.mark.parametrize("said", ["stop", "continue", "I paused the movie earlier", "play Sigur Rós", "what's next on my list",
                                  "skip the intro and tell me about the weather", "turn it up to eleven, the meeting"])
def test_not_fast_commands(said):
    assert media.fast_command(said) is None                           # these go to her


def test_fast_control_answers_in_words(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR])
    ctx = _ctx(tmp_path)
    assert asyncio.run(media.fast_control(ctx, {"type": "volume", "step": -15})) == "Volume 55."
    assert asyncio.run(media.fast_control(ctx, {"type": "pause"})) == "Paused."
    o.refuse = "NOTHING_PLAYING"
    assert asyncio.run(media.fast_control(ctx, {"type": "next"})) == "Nothing's playing."
    o.players[0]["online"] = False
    assert asyncio.run(media.fast_control(ctx, {"type": "pause"})) == "The music player isn't open."


def test_duck_goes_to_the_residents_player_else_the_default_and_never_raises(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR])
    om.save(tmp_path, {"url": "http://o", "device_id": "ctl", "token": "t"})
    asyncio.run(media.duck(tmp_path, True))
    assert not o.sent                                                  # no resident, no default: nothing
    asyncio.run(media.duck(tmp_path, True, "p9"))
    media.set_default_player(tmp_path, "p1", "Predator")
    asyncio.run(media.duck(tmp_path, False))
    assert o.sent == [("p9", {"type": "duck", "level": 25}), ("p1", {"type": "unduck"})]
    o.refuse = "OFFLINE"
    asyncio.run(media.duck(tmp_path, True))                            # silent


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
