"""Facts that end (Memory Phase 7, #20): kind, end date, and leaving the prompt after it.

    .venv/bin/python -m pytest -q tests/test_fact_lifetimes.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from models.schema import AssistantMemory, MemoryFact
from services import assistant_memory as am
from services import fortuna

NOW = datetime(2026, 11, 23, 15, 0)          # a Monday


@pytest.mark.parametrize("text,until", [
    ("Brian's sister is visiting tomorrow", "2026-11-25"),
    ("Brian has the dentist today", "2026-11-24"),
    ("Brian flies to Lisbon on Thursday", "2026-11-27"),
    ("Brian's friends are coming next weekend", "2026-12-07"),
    ("Brian's exam is in three weeks", "2026-12-16"),
    ("Brian's friends Tom and Bill are visiting next month", "2027-01-01"),
    ("Brian's car is in the garage until the 28th", "2026-11-29"),
    ("Brian's car is in the garage until the 3rd", "2026-12-04"),
    ("Brian is away in March", "2027-04-02"),
    ("Brian is on a diet for a while", "2026-12-23"),
])
def test_end_dates(text, until):
    assert am.expires_from(text, NOW).date().isoformat() == until


def test_fortuna_decides_the_kind_and_the_words_decide_without_it(monkeypatch):
    async def says(kind):
        async def choose(state, instructions, criteria):
            return (kind, 0.9)
        return choose
    monkeypatch.setattr(fortuna, "choose", asyncio.run(says("lasting")))
    assert asyncio.run(am.lifetime("Brian lives in Porto", NOW)) == ("", "")
    monkeypatch.setattr(fortuna, "choose", asyncio.run(says("timebound")))
    assert asyncio.run(am.lifetime("Brian's sister is visiting tomorrow", NOW)) == ("timebound", "2026-11-25")

    async def down(*a):
        return None
    monkeypatch.setattr(fortuna, "choose", down)
    assert asyncio.run(am.lifetime("Brian flies to Lisbon on Thursday", NOW))[0] == "timebound"
    assert asyncio.run(am.lifetime("Brian is left-handed", NOW)) == ("", "")


def test_an_expired_fact_leaves_the_prompt():
    m = AssistantMemory(facts=[MemoryFact(text="Brian lives in Porto"),
                               MemoryFact(text="Tom and Bill visit next month", kind="timebound", expires="2027-01-01")])
    assert "Tom and Bill" in am.block(m, "Mira", "Brian", now=datetime(2026, 12, 31))
    later = am.block(m, "Mira", "Brian", now=datetime(2027, 1, 2))
    assert "Tom and Bill" not in later and "Porto" in later


def test_keeping_an_expired_fact_makes_it_lasting():
    import main
    from test_chat_stream import cur as _cur  # noqa: F401
    from models.schema import CharacterCard
    import tempfile, os  # noqa: E401
    from models.schema import User
    from services import auth
    salt = auth.new_salt()
    u = User(username=f"t{os.urandom(3).hex()}", role="user", salt=salt, password_hash=auth.hash_password("x", salt))
    main._save_user(u)
    cur = main._ctx_for(u)
    card = CharacterCard(name="Mira")
    main._save(card, cur.cards)
    pid = main._default_persona(cur).id
    m = main._load_assistant_memory(cur, card.id, pid)
    old = MemoryFact(text="Brian is on call this week", kind="timebound", expires="2026-01-01")
    m.facts.append(old)
    main._save_assistant_memory(cur, m)
    asyncio.run(main.put_assistant_memory({"character_id": card.id, "persona_id": pid,
                                           "facts": [{"id": old.id, "text": old.text, "renew": True}]}, cur=cur))
    f = main._load_assistant_memory(cur, card.id, pid).facts[0]
    assert (f.kind, f.expires) == ("", "") and not am.expired(f)
