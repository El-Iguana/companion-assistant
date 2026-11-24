"""Tiered recall (Memory Phase 7, #21): an always-on core, the rest per turn.

    .venv/bin/python -m pytest -q tests/test_tiered_recall.py
"""
from __future__ import annotations

from datetime import datetime

import main
from models.schema import AssistantMemory, CharacterCard, ChatMessage, ChatSession, MemoryFact
from services import assistant_memory as am
from test_chat_stream import cur  # noqa: F401 — fixture

OLD = "2026-01-01T00:00:00"


def _fact(text, origin="auto", uses=0, created=OLD):
    return MemoryFact(text=text, origin=origin, uses=uses, created_at=created)


def test_a_small_store_is_all_core():
    m = AssistantMemory(facts=[_fact("Brian lives in Porto"), _fact("Brian is vegetarian")])
    assert [f.text for f in am.core_facts(m)] == ["Brian lives in Porto", "Brian is vegetarian"]


def test_the_core_prefers_hand_written_then_used_then_new_within_budget():
    filler = [_fact(f"Brian owns item number {i} in the long inventory list") for i in range(30)]
    mine = _fact("Brian's partner is Inês", origin="user")
    used = _fact("Brian's daughter is Ana", uses=5)
    m = AssistantMemory(facts=filler + [mine, used])
    core = am.core_facts(m)
    assert mine in core and used in core and sum(len(f.text) + 3 for f in core) <= am.CORE_CHARS


def test_recall_finds_what_the_turn_is_about_and_nothing_for_small_talk():
    m = AssistantMemory(facts=[_fact("Brian's car insurance renews in June"), _fact("Brian's favourite film is Blade Runner"),
                               _fact("Brian's dentist is Dr Sousa"), _fact("Brian's daughter Ana is vegetarian")])
    got = [f.text for f in am.recall(m, "When does my car insurance renew?", "Brian")]
    assert got == ["Brian's car insurance renews in June"]
    assert [f.text for f in am.recall(m, "What should I cook for Ana tonight?", "Brian")] == ["Brian's daughter Ana is vegetarian"]
    assert am.recall(m, "morning!", "Brian") == [] and am.recall(m, "Brian here", "Brian") == []


def test_recall_goes_after_the_history_and_is_counted(cur):
    card = CharacterCard(name="Mira")
    main._save(card, cur.cards)
    persona = main._default_persona(cur)
    persona.name = "Brian"
    main._save(persona, cur.personas)
    m = main._load_assistant_memory(cur, card.id, persona.id)
    m.facts = [_fact(f"Brian owns item number {i} in the long inventory list") for i in range(40)]
    m.facts.insert(0, _fact("Brian's car insurance renews in June"))
    main._save_assistant_memory(cur, m)
    s = ChatSession(character_id=card.id, persona_id=persona.id,
                    messages=[ChatMessage(role="user", content="When does my car insurance renew?")])
    got = main.memory_for_turn(cur, card, persona, s, now=datetime(2026, 11, 23))
    assert "insurance renews in June" in got["trailing"] and "insurance" not in got["system"]
    f = next(x for x in main._load_assistant_memory(cur, card.id, persona.id).facts if "insurance" in x.text)
    assert f.uses == 1 and f.last_used.startswith("2026-11-23")
