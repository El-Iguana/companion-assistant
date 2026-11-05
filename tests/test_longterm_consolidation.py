"""Long-term consolidation (services.memory, muninn.consolidate).

A fold used to rewrite the whole long-term memory from the last one plus the new
turns, so whatever the model didn't copy was lost (eval: a first-chat detail gone
in 1 of 3 runs). With `consolidate`, a fold appends only new bullets and the idle
sweep runs one review that may only delete, by number.

    .venv/bin/python -m pytest -q tests/test_longterm_consolidation.py
"""
from __future__ import annotations

import asyncio

import main
from services import memory
from models.schema import CharacterCard, CharacterMemory, ChatMessage, ChatSession, Persona
from test_chat_stream import cur, session  # noqa: F401 — fixtures


class Kob:
    """Answers model calls from a queue; records the prompts."""
    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    async def generate_answer(self, prompt, params):
        self.prompts.append(prompt)
        return self.answers.pop(0)


def _chat(*lines):
    s = ChatSession(character_id="c", persona_id="p")
    for i, t in enumerate(lines):
        s.messages.append(ChatMessage(role="user" if i % 2 == 0 else "assistant", content=t))
    return s


CARD, PERSONA = CharacterCard(name="Mira"), Persona(name="Priya")


def _fold(kob, mem, s, **k):
    return asyncio.run(memory.fold_into_longterm(kob, mem, s, CARD, PERSONA,
                                                 consolidate=True, **k))


def test_a_fold_appends_and_never_rewrites_what_is_there():
    mem = CharacterMemory(summary="- Priya is a marine biologist.\n- Gus is Priya's tortoise.")
    kob = Kob("- Priya plays cello in a quartet.")
    assert _fold(kob, mem, _chat("I play cello, second chair.", "Distinguished."))
    assert mem.summary == ("- Priya is a marine biologist.\n- Gus is Priya's tortoise.\n"
                           "- Priya plays cello in a quartet.")
    assert "ONLY the new bullets" in kob.prompts[0] and "Gus is Priya's tortoise" in kob.prompts[0]


def test_none_is_a_successful_pass():
    mem = CharacterMemory(summary="- Priya is a marine biologist.")
    s = _chat("Nice weather.", "It is.")
    assert _fold(Kob("NONE"), mem, s)
    assert mem.summary == "- Priya is a marine biologist."
    assert mem.swept_sessions[s.id] == 2                       # the mark still moves


def test_an_empty_or_junk_answer_is_a_failed_pass():
    mem = CharacterMemory(summary="- Priya is a marine biologist.")
    s = _chat("Nice weather.", "It is.")
    assert not _fold(Kob("Here is the updated memory:"), mem, s)
    assert s.id not in mem.swept_sessions


def test_the_cap_evicts_oldest_into_the_recall_index(tmp_path, monkeypatch):
    added = []
    monkeypatch.setattr(memory, "index_add", lambda path, entries, **k: added.extend(entries))
    monkeypatch.setattr(memory, "LONGTERM_KEEP_BULLETS", 2)
    mem = CharacterMemory(summary="- one\n- two")
    _fold(Kob("- three"), mem, _chat("x", "y"), index_path=tmp_path / "idx")
    assert mem.summary == "- two\n- three"
    assert {"text": "one", "kind": "memory"} in added


def test_the_review_only_deletes_what_it_names():
    mem = CharacterMemory(summary="- Jonah owns the Kestrel.\n- Jonah plans to sail to Orkney.\n"
                                  "- The Orkney trip is off.")
    kob = Kob("2")
    assert asyncio.run(memory.review_longterm(kob, mem, CARD, PERSONA))
    assert mem.summary == "- Jonah owns the Kestrel.\n- The Orkney trip is off."
    assert "3. The Orkney trip is off." in kob.prompts[0]


def test_a_greedy_review_is_ignored():
    mem = CharacterMemory(summary="- a\n- b\n- c\n- d")
    assert not asyncio.run(memory.review_longterm(Kob("1, 2, 3"), mem, CARD, PERSONA))
    assert mem.summary == "- a\n- b\n- c\n- d"


def test_the_block_drops_oldest_lines_first():
    mem = CharacterMemory(summary="\n".join(f"- bullet number {i:02d} about Priya" for i in range(40)))
    block = memory.longterm_block(mem, "Mira", "Priya", budget_chars=600)
    assert "bullet number 39" in block and "bullet number 00" not in block
    assert "…" not in block


def test_the_sweep_reviews_once_per_memory_state(cur, session, monkeypatch):
    cid, pid = session.character_id, session.persona_id
    mem = main._load_character_memory(cur, cid, pid)
    mem.summary = "- Jonah plans to sail to Orkney.\n- The Orkney trip is off."
    main._save_character_memory(cur, mem)
    calls = []

    async def fold(kobold, m, s, card, persona, **k):
        assert k["consolidate"] is True
        return False                                           # nothing new to fold

    async def answer(prompt, params):
        calls.append(prompt)
        return "1"

    st = main._load_settings(cur)
    st.longterm_consolidate = True
    monkeypatch.setattr(main, "_load_settings", lambda c: st)
    monkeypatch.setattr(main.memory, "fold_into_longterm", fold)
    monkeypatch.setattr(main.kobold, "generate_answer", answer)
    r = asyncio.run(main.sweep_session(session.id, cur=cur))
    assert r["reviewed"] is True
    saved = main._load_character_memory(cur, cid, pid)
    assert saved.summary == "- The Orkney trip is off."
    asyncio.run(main.sweep_session(session.id, cur=cur))       # idle again, nothing new
    assert len(calls) == 1


def test_consolidation_off_rewrites_as_before(cur, session, monkeypatch):
    st = main._load_settings(cur)
    assert st.longterm_consolidate is False                    # the default
    seen = {}

    async def fold(kobold, m, s, card, persona, **k):
        seen["consolidate"] = k["consolidate"]
        return False

    monkeypatch.setattr(main.memory, "fold_into_longterm", fold)
    monkeypatch.setattr(main, "_load_settings", lambda c: st)
    r = asyncio.run(main.sweep_session(session.id, cur=cur))
    assert seen["consolidate"] is False and r["reviewed"] is False
