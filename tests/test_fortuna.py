"""Fortuna in Cantinero: the emotion classifier and the music fast lane. Fortuna is faked.

    .venv/bin/python -m pytest -q tests/test_fortuna.py
"""
from __future__ import annotations

import asyncio

import pytest

import main
from models.schema import CharacterCard
from services import expressions, fortuna
from services.skills import media
from services.skills import orpheus_media as om
from test_chat_stream import _events, _reload, cur, session  # noqa: F401 — fixtures
from test_dj_playback import PREDATOR, SIGUR, FakeOrpheus


def fake_fortuna(monkeypatch, answers):
    """answers: instructions-substring -> (label, confidence) or None (down)."""
    asked = []

    async def choose(state, instructions, criteria):
        asked.append(state)
        for key, ans in answers.items():
            if key in instructions:
                return ans
        return None
    monkeypatch.setattr(fortuna, "choose", choose)
    return asked


def test_emotion_from_fortuna_and_neutral_when_unsure(monkeypatch):
    class NoKobold:
        async def generate_answer(self, *a, **k):
            raise AssertionError("KoboldCPP was asked while Fortuna answered")
    fake_fortuna(monkeypatch, {"emotion": ("thoughtful", 0.84)})
    assert asyncio.run(expressions.classify(NoKobold(), "Hmm, let me think.")) == "thoughtful"
    fake_fortuna(monkeypatch, {"emotion": ("amused", 0.24)})
    assert asyncio.run(expressions.classify(NoKobold(), "Playing it now.")) == "neutral"


def test_emotion_falls_back_to_koboldcpp_when_fortuna_is_down(monkeypatch):
    class Kobold:
        async def generate_answer(self, *a, **k):
            return "grateful"
    fake_fortuna(monkeypatch, {})                                    # None: down or off
    assert asyncio.run(expressions.classify(Kobold(), "Thank you so much!")) == "grateful"


def _ctx(tmp_path, **kw):
    from services import skills
    om.save(tmp_path, {"url": "http://o", "device_id": "ctl", "token": "t"})
    return skills.SkillContext(user_id="u1", root=tmp_path, **kw)


def test_fast_lane_plays_a_clear_match_without_the_model(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR], SIGUR[:1])
    seen = []
    real = o.resolve

    async def resolve(base, token, query, kinds, limit=5):
        seen.append(query)
        return await real(base, token, query, kinds, limit)
    monkeypatch.setattr(om, "resolve", resolve)
    asked = fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.88)})
    said = asyncio.run(media.fast_play(_ctx(tmp_path), "Mira, can you play some Sigur Ros?", "Mira"))
    assert said == "Playing ( ) by Sigur Rós on Predator." and o.sent[0][1]["type"] == "play"
    assert seen == asked == ["play some Sigur Ros"]            # her name and "can you" gone, for both


@pytest.mark.parametrize("intent,ambiguous", [(("play_music", 0.5), False), (("other", 0.97), False),
                                              (("play_music", 0.9), True), (None, False)])
def test_fast_lane_hands_anything_unsure_to_her(tmp_path, monkeypatch, intent, ambiguous):
    o = FakeOrpheus(monkeypatch, [PREDATOR], SIGUR, ambiguous=ambiguous)
    fake_fortuna(monkeypatch, {"want the assistant": intent})
    assert asyncio.run(media.fast_play(_ctx(tmp_path), "play sigur ros", "Mira")) is None and not o.sent


def test_a_play_in_the_fast_lane_never_reaches_the_model(cur, session, monkeypatch):
    card = main._load(cur.cards, session.character_id, CharacterCard)
    card.skills = ["dj"]
    main._save(card, cur.cards)
    o = FakeOrpheus(monkeypatch, [PREDATOR], SIGUR[:1])
    om.save(cur.cards.parent, {"url": "http://o", "device_id": "ctl", "token": "t"})
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9)})

    async def no_model(*a, **k):
        raise AssertionError("the model was called")
        yield
    monkeypatch.setattr(main.kobold, "chat_generate_stream", no_model)
    monkeypatch.setattr(main.expressions, "classify", no_model)
    monkeypatch.setattr(main.assistant_memory, "fold", lambda *a, **k: asyncio.sleep(0, False))

    async def go():
        resp = await main.chat(main.ChatRequest(session_id=session.id, input_text="Play Sigur Ros."), cur=cur)
        return _events([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])
    evs = asyncio.run(go())
    assert {"token": "Playing ( ) by Sigur Rós on Predator."} in evs and len(o.sent) == 1
    assert _reload(cur, session).messages[-1].content == "Playing ( ) by Sigur Rós on Predator."


def test_not_music_goes_to_her_as_before(cur, session, monkeypatch):
    card = main._load(cur.cards, session.character_id, CharacterCard)
    card.skills = ["dj"]
    main._save(card, cur.cards)
    FakeOrpheus(monkeypatch, [PREDATOR], SIGUR[:1])
    om.save(cur.cards.parent, {"url": "http://o", "device_id": "ctl", "token": "t"})
    fake_fortuna(monkeypatch, {"want the assistant": ("other", 0.97), "emotion": ("neutral", 0.9)})

    async def model(messages, params):
        yield "It'll be sunny."
    monkeypatch.setattr(main.kobold, "chat_generate_stream", model)
    monkeypatch.setattr(main.assistant_memory, "fold", lambda *a, **k: asyncio.sleep(0, False))

    async def go():
        resp = await main.chat(main.ChatRequest(session_id=session.id, input_text="weather tomorrow?"), cur=cur)
        return _events([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])
    assert {"token": "It'll be sunny."} in asyncio.run(go())


def test_the_memory_gate_leaves_out_what_fortuna_is_sure_holds_nothing(monkeypatch):
    from models.schema import ChatMessage
    from services import assistant_memory

    verdicts = {"morning": ("nothing", 0.95), "Good night Mira.": ("nothing", 0.96),
                "Keep your answers short in the mornings.": ("nothing", 0.83),     # doubted: kept
                "I'm vegetarian.": ("fact", 0.9)}

    async def choose(state, instructions, criteria):
        return verdicts.get(state)
    monkeypatch.setattr(fortuna, "choose", choose)
    turns = [ChatMessage(role="user", content=t) for t in verdicts]
    kept = asyncio.run(assistant_memory.worth_reading(turns))
    assert [m.content for m in kept] == ["Keep your answers short in the mornings.", "I'm vegetarian."]
    fake_fortuna(monkeypatch, {})                                    # Fortuna down: every turn
    assert len(asyncio.run(assistant_memory.worth_reading(turns))) == 4


def test_junk_notes_are_dropped_and_real_ones_kept():
    from services.assistant_memory import _junk_notes
    junk = ["(empty)", "Updated list:", "</assink>", "The interaction shows Brian checking if Mira is awake",
            "None.", "The current list is empty, and remains so"]
    real = ["Brian needs the API key by Friday", "Migration to the new server is half done",
            "Tom and Bill visit next month"]
    assert _junk_notes(junk + real) == real


def test_a_fold_of_pure_small_talk_spends_no_model_time(monkeypatch):
    from models.schema import AssistantMemory, ChatMessage, ChatSession, Persona
    from services import assistant_memory

    async def choose(state, instructions, criteria):
        return ("nothing", 0.97)
    monkeypatch.setattr(fortuna, "choose", choose)

    class NoKobold:
        async def generate_answer(self, *a, **k):
            raise AssertionError("the model was asked to fold small talk")
    s = ChatSession(character_id="c", persona_id="p", messages=[
        ChatMessage(role="user", content="morning"), ChatMessage(role="char", content="Good morning."),
        ChatMessage(role="user", content="ok carry on"), ChatMessage(role="char", content="Very good.")])
    mem = AssistantMemory(notes=["Brian needs the API key by Friday"])
    changed = asyncio.run(assistant_memory.fold(NoKobold(), mem, s, CharacterCard(name="Mira"), Persona(name="Brian")))
    assert changed and mem.swept_sessions[s.id] == 4 and mem.notes == ["Brian needs the API key by Friday"]


CROWDED = [{"item_id": 276, "kind": "album", "display": "Before These Crowded Streets — Dave Matthews Band (1998)",
            "score": 1.0}]


def test_an_exact_name_takes_the_lane_when_fortuna_is_borderline(tmp_path, monkeypatch):
    # R5, 2026-11-24: Fortuna scored this 0.76 and the play took 25 s through the model.
    o = FakeOrpheus(monkeypatch, [PREDATOR], CROWDED)
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.76)})
    said = asyncio.run(media.fast_play(_ctx(tmp_path), "play Before These Crowded Streets", "Mira"))
    assert said.startswith("Playing Before These Crowded Streets") and o.sent[0][1]["type"] == "play"


def test_borderline_without_an_exact_name_is_still_hers(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR], SIGUR[:1])                    # score 0.83, not exact
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.76)})
    assert asyncio.run(media.fast_play(_ctx(tmp_path), "play sigur ros", "Mira")) is None and not o.sent


def test_a_sure_play_of_something_missing_is_answered_at_once(tmp_path, monkeypatch):
    # R5: through the model, three media_play calls and 68 s to say the same.
    o = FakeOrpheus(monkeypatch, [PREDATOR], [], ambiguous=True)
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.95)})
    said = asyncio.run(media.fast_play(_ctx(tmp_path), "Mira, play Ágætis byrjun", "Mira"))
    assert said == "I can't find Ágætis byrjun in your music." and not o.sent
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.7)})   # unsure: hers
    assert asyncio.run(media.fast_play(_ctx(tmp_path), "play some Agaetis", "Mira")) is None
