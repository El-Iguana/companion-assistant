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
    assert isinstance(said, media.Done)                         # it worked: shown, not said
    assert seen == ["play some Sigur Ros"] and set(asked) == {"play some Sigur Ros"}   # her name and "can you" gone


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
    assert not isinstance(said, media.Done)                     # a miss is said aloud
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.7)})   # unsure: hers
    assert asyncio.run(media.fast_play(_ctx(tmp_path), "play some Agaetis", "Mira")) is None


# --- ties settled without her (#79) ---

def _track(item_id, title, artist="ACDC", score=0.783):
    return {"item_id": item_id, "kind": "track", "title": title, "display": f"{title} — {artist}",
            "score": score, "playable": True}


BACK_IN_BLACK = [_track(141, "Back in Black - Back In Black"), _track(150, "Back in Black - You Shook Me All Night Long"),
                 _track(152, "Back in Black - Hells Bells"), _track(155, "Back in black - Let Me Put My Love Into You"),
                 _track(246, "I'll Back You Up", "Dave Matthews Band", 0.423)]


@pytest.mark.parametrize("query,want", [
    ("play Back in Black", 0),                           # the "Album - Song" tag pattern
    ("Back in Black", 0),
    ("play hells bells", 2),
    ("play Back in Black by ACDC", 0),                   # "by <artist>" must match too
    ("play Back in Black by Queen", None),
    ("play rock", None),
])
def test_the_exact_title_settles_a_tie(query, want):
    assert media.exact_pick(query, BACK_IN_BLACK) == want


def test_two_exact_titles_are_still_a_tie():
    two = [_track(1, "Crash", "Dave Matthews Band"), _track(2, "Crash", "Someone Else")]
    assert media.exact_pick("play Crash", two) is None
    assert media.exact_pick("play Crash by Dave Matthews Band", two) == 0


def test_a_tie_plays_the_exact_title_without_her(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR], BACK_IN_BLACK, ambiguous=True)
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9)})
    said = asyncio.run(media.fast_play(_ctx(tmp_path), "Mira, play Back in Black", "Mira"))
    assert isinstance(said, media.Done) and o.expanded == [141]


def test_fortuna_settles_what_the_title_cannot_and_only_when_sure(tmp_path, monkeypatch):
    two = [_track(1, "Crash", "Dave Matthews Band"), _track(2, "Crash", "Someone Else")]
    o = FakeOrpheus(monkeypatch, [PREDATOR], two, ambiguous=True)
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9),
                               "Which of these": ("Crash — Someone Else", 0.93)})
    asyncio.run(media.fast_play(_ctx(tmp_path), "play Crash", "Mira"))
    assert o.expanded == [2]
    o = FakeOrpheus(monkeypatch, [PREDATOR], two, ambiguous=True)
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9),
                               "Which of these": ("Crash — Someone Else", 0.55)})
    said = asyncio.run(media.fast_play(_ctx(tmp_path), "play Crash", "Mira"))
    assert said == "Which one: Crash by Dave Matthews Band, or Crash by Someone Else?" and not o.sent   # asks (#81)


# --- either-or without her (#81) ---

def _either(monkeypatch, tmp_path, answer):
    two = [_track(1, "Crash", "Dave Matthews Band"), _track(2, "Crash", "Someone Else")]
    o = FakeOrpheus(monkeypatch, [PREDATOR], two, ambiguous=True)
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9), "Which of these": None,
                               "Which did the user choose": answer})
    ctx = _ctx(tmp_path, session_id="s1")
    asked = asyncio.run(media.fast_play(ctx, "play Crash", "Mira"))
    assert asked.startswith("Which one:") and not isinstance(asked, media.Done)   # a question: said aloud
    return o, ctx


def test_the_answer_to_her_question_plays_without_her(tmp_path, monkeypatch):
    o, ctx = _either(monkeypatch, tmp_path, ("2", 0.92))
    said = asyncio.run(media.fast_play(ctx, "the second one", "Mira"))
    assert isinstance(said, media.Done) and "Someone Else" in said and o.expanded == [2]


def test_something_else_after_her_question_goes_on_as_usual(tmp_path, monkeypatch):
    o, ctx = _either(monkeypatch, tmp_path, ("none", 0.95))
    fake_fortuna(monkeypatch, {"want the assistant": ("other", 0.95), "Which did the user choose": ("none", 0.95)})
    assert asyncio.run(media.fast_play(ctx, "what time is it", "Mira")) is None and not o.sent
    assert media._ask_key(ctx) not in media._pending                # asked once, then forgotten


def test_an_unsure_answer_goes_to_her(tmp_path, monkeypatch):
    o, ctx = _either(monkeypatch, tmp_path, ("1", 0.5))
    fake_fortuna(monkeypatch, {"want the assistant": ("other", 0.9), "Which did the user choose": ("1", 0.5)})
    again = asyncio.run(media.fast_play(ctx, "hmm, the Dave one maybe", "Mira"))
    assert again == "Sorry, which one: the first, or the second?" and not o.sent    # once more, fixed words
    assert asyncio.run(media.fast_play(ctx, "hmm, the Dave one maybe", "Mira")) is None and not o.sent   # then hers


def test_no_question_when_the_options_sound_the_same():
    same = [{"id": "1", "name": "Crash — Dave Matthews Band (1996)"}, {"id": "2", "name": "Crash — Dave Matthews Band (2010)"}]
    raw = [{"score": 0.8}, {"score": 0.8}]
    assert media.either_or(same, raw) == []


def test_more_than_three_options_say_three_and_match_all(tmp_path, monkeypatch):
    six = [_track(i, "Greatest Hits", a) for i, a in enumerate(["B.B. King", "Evanescence", "Jimi Hendrix",
                                                                "The Rolling Stones", "Bon Jovi"], 1)]
    o = FakeOrpheus(monkeypatch, [PREDATOR], six, ambiguous=True)
    fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9), "Which of these": None,
                               "Which did the user choose": ("4", 0.9)})
    ctx = _ctx(tmp_path, session_id="s1")
    asked = asyncio.run(media.fast_play(ctx, "play Greatest Hits", "Mira"))
    assert asked == ("Which one: Greatest Hits by B.B. King, Greatest Hits by Evanescence, "
                     "Greatest Hits by Jimi Hendrix, or one of 2 more?")
    said = asyncio.run(media.fast_play(ctx, "the Stones one", "Mira"))
    assert "Rolling Stones" in said and o.expanded == [4]


@pytest.mark.parametrize("text", ["never mind, play Santana", "Mira, actually play Santana",
                                  "no, play Santana", "Mira, never mind, can you play Santana?"])
def test_a_change_of_mind_in_front_is_not_what_to_play(tmp_path, monkeypatch, text):
    o = FakeOrpheus(monkeypatch, [PREDATOR], [{"item_id": 7, "kind": "artist", "display": "Santana", "score": 1.0}])
    asked = fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9)})
    asyncio.run(media.fast_play(_ctx(tmp_path), text, "Mira"))
    assert asked[0] == "play Santana" and o.expanded == [7]


def test_an_artist_tied_with_their_own_work_is_the_artist():
    raw = [{"kind": "artist", "title": "Jimi Hendrix", "display": "Jimi Hendrix", "score": 0.7},
           _track(5, "Purple Haze", "Jimi Hendrix (2010)", 0.7),
           {"kind": "album", "title": "Greatest Hits", "display": "Greatest Hits — Jimi Hendrix (2010)", "score": 0.7}]
    assert media.artist_pick(raw) == 0
    raw[1] = _track(5, "Purple Haze", "Someone Else", 0.7)
    assert media.artist_pick(raw) is None                     # someone else's track tied too


def test_whispers_playback_is_play_back(tmp_path, monkeypatch):
    o = FakeOrpheus(monkeypatch, [PREDATOR], BACK_IN_BLACK, ambiguous=True)
    asked = fake_fortuna(monkeypatch, {"want the assistant": ("play_music", 0.9)})
    said = asyncio.run(media.fast_play(_ctx(tmp_path), "Playback in black.", "Mira"))
    assert asked[0] == "play Back In Black" and isinstance(said, media.Done) and o.expanded == [141]
