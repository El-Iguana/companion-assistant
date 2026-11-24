"""Quick answers (#101): the time, the date and what's playing, without the model."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

import main
from models.schema import CharacterCard
from services import quick
from services.skills import orpheus_media as om
from test_chat_stream import _events, _reload, cur, session  # noqa: F401 — fixtures
from test_dj_playback import PREDATOR, SIGUR, FakeOrpheus
from test_fortuna import fake_fortuna

NOW = datetime(2026, 11, 24, 15, 15)


@pytest.mark.parametrize("h,m,said", [
    (0, 0, "It's midnight."), (12, 0, "It's noon."), (0, 5, "It's twelve oh five at night."),
    (3, 15, "It's quarter past three in the morning."), (9, 7, "It's nine oh seven in the morning."),
    (11, 45, "It's quarter to twelve in the morning."), (12, 30, "It's half past twelve in the afternoon."),
    (15, 22, "It's three twenty-two in the afternoon."), (17, 45, "It's quarter to six in the evening."),
    (20, 0, "It's eight o'clock in the evening."), (23, 59, "It's eleven fifty-nine at night."),
])
def test_the_time_is_said_for_the_ear(h, m, said):
    assert quick.spoken_time(datetime(2026, 11, 24, h, m)) == said


@pytest.mark.parametrize("day,said", [(1, "Sunday, November the first"), (2, "Monday, November the second"),
                                      (11, "Wednesday, November the eleventh"), (20, "Friday, November the twentieth"),
                                      (22, "Sunday, November the twenty-second"),
                                      (24, "Tuesday, November the twenty-fourth"), (30, "Monday, November the thirtieth")])
def test_the_date_is_said_for_the_ear(day, said):
    assert quick.spoken_date(datetime(2026, 11, day)) == f"It's {said}."


@pytest.mark.parametrize("text,ok", [
    ("Mira, what time is it?", True), ("Do you know what time it is?", True), ("what day is it", True),
    ("What's today's date?", True), ("what's playing", True), ("who is singing this", True),
    ("what time is the game tomorrow", False), ("set a timer for ten minutes", False),
    ("remind me at five", False), ("Play Neil Young", False), ("tell me a story", False),
    ("what time is it " + "x" * 80, False),
])
def test_only_short_questions_about_now_reach_fortuna(text, ok):
    assert bool(quick.candidate(text, "Mira")) is ok


def _ask(monkeypatch, text, label, players=None):
    asked = fake_fortuna(monkeypatch, {"What is the user asking": label})
    return asyncio.run(quick.answer(text, "Mira", players=players, now=NOW)), asked


def test_fortuna_sure_answers_and_unsure_is_hers(monkeypatch):
    said, asked = _ask(monkeypatch, "Mira, what time is it?", ("time", 0.95))
    assert said == "It's quarter past three in the afternoon." and asked == ["what time is it?"]
    assert _ask(monkeypatch, "what day is it", ("date", 0.9))[0] == "It's Tuesday, November the twenty-fourth."
    assert _ask(monkeypatch, "what time is it", ("time", 0.6))[0] is None
    assert _ask(monkeypatch, "What day is Thanksgiving?", ("date", 0.87))[0] == (       # a holiday: worked out
        "Thanksgiving is Thursday, November the twenty-sixth, in two days.")
    assert _ask(monkeypatch, "What day is the party?", ("date", 0.87))[0] is None      # not today
    assert _ask(monkeypatch, "I love this song.", ("playing", 0.9), lambda: [])[0] is None   # not a question
    assert _ask(monkeypatch, "I had a great day", ("other", 0.9))[0] is None
    assert _ask(monkeypatch, "what time is it", None)[0] is None           # Fortuna down: hers


def test_whats_playing_reads_the_player(monkeypatch):
    on = [dict(PREDATOR, state={"status": "playing", "now": {"title": "Heart of Gold", "artist": "Neil Young"}})]
    assert _ask(monkeypatch, "what's playing", ("playing", 0.9), lambda: on)[0] == "This is Heart of Gold by Neil Young."
    off = [dict(PREDATOR, state={"status": "idle"})]
    assert _ask(monkeypatch, "what song is this", ("playing", 0.9), lambda: off)[0] == "Nothing's playing."
    # A card without the DJ isn't offered "playing" at all.
    seen = {}

    async def choose(state, instructions, criteria):
        seen.update(criteria)
        return ("other", 0.9)
    from services import fortuna
    monkeypatch.setattr(fortuna, "choose", choose)
    asyncio.run(quick.answer("what's playing", "Mira"))
    assert "playing" not in seen and "time" in seen


def test_a_quick_answer_never_reaches_the_model_and_is_said(cur, session, monkeypatch):
    card = main._load(cur.cards, session.character_id, CharacterCard)
    card.skills = ["dj"]
    main._save(card, cur.cards)
    FakeOrpheus(monkeypatch, [PREDATOR], SIGUR[:1])
    om.save(cur.cards.parent, {"url": "http://o", "device_id": "ctl", "token": "t"})
    fake_fortuna(monkeypatch, {"What is the user asking": ("time", 0.95)})

    async def no_model(*a, **k):
        raise AssertionError("the model was called")
        yield
    monkeypatch.setattr(main.kobold, "chat_generate_stream", no_model)
    monkeypatch.setattr(main.expressions, "classify", no_model)
    monkeypatch.setattr(main.assistant_memory, "fold", lambda *a, **k: asyncio.sleep(0, False))

    async def go():
        resp = await main.chat(main.ChatRequest(session_id=session.id, input_text="Mira, what time is it?"), cur=cur)
        return _events([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])
    evs = asyncio.run(go())
    tokens = [e["token"] for e in evs if "token" in e]
    assert len(tokens) == 1 and tokens[0].startswith("It's ") and {"quiet": True} not in evs   # said aloud
    assert _reload(cur, session).messages[-1].content == tokens[0]
