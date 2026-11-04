"""Memory writers drop untagged reasoning before storing (muninn.checks).

Lines like these were stored even after the tag fix: "Here is the updated memory:", "*I'll update Jonah's memory…*",
"No new durable facts." as a fact, and the prompt's rules echoed back.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from models.schema import (CharacterCard, CharacterMemory, ChatMessage,  # noqa: E402
                           ChatSession, Persona)
from services import memory  # noqa: E402

CARD, PERSONA = CharacterCard(name="Mira"), Persona(name="Jonah")


class FakeKobold:
    def __init__(self, *answers):
        self.answers = list(answers)

    async def generate_answer(self, prompt, params):
        return self.answers.pop(0)

    async def generate_answer_full(self, prompt, params):
        return self.answers.pop(0), False


def _session(n=30):
    s = ChatSession(character_id="c", persona_id="p")
    for i in range(n):
        s.messages.append(ChatMessage(role="user" if i % 2 == 0 else "char",
                                      content=f"I work nights on the cardiac ward, turn {i}"))
    return s


def test_long_term_fold_stores_only_the_notes():
    mem = CharacterMemory(character_id="c", persona_id="p")
    kob = FakeKobold("Here is the updated memory:\n\n- Jonah is a cardiac nurse.\n"
                     "*I'll keep it terse.*\n- Terse bullets, one line each. No preamble, "
                     "no closing sentence, no headings.")
    assert asyncio.run(memory.fold_into_longterm(kob, mem, _session(), CARD, PERSONA))
    assert mem.summary == "- Jonah is a cardiac nurse."


def test_long_term_pass_of_nothing_but_reasoning_stores_nothing():
    mem = CharacterMemory(character_id="c", persona_id="p", summary="- Jonah has a cat.")
    kob = FakeKobold("I need to condense this into a short, terse memory:\n*Mira's memory updates.*")
    assert not asyncio.run(memory.fold_into_longterm(kob, mem, _session(), CARD, PERSONA))
    assert mem.summary == "- Jonah has a cat." and mem.swept_sessions == {}


def test_junk_fact_lines_dont_take_a_real_facts_slot():
    mem = CharacterMemory(character_id="c", persona_id="p")
    kob = FakeKobold("No new durable facts.\nI'll list them as requested:\n"
                     "Jonah is allergic to shellfish.\nJonah has a sister named Nadia.")
    facts = asyncio.run(memory.extract_auto_facts(kob, mem, "Jonah: hi", "Mira", "Jonah", max_new=2))
    assert facts == ["Jonah is allergic to shellfish.", "Jonah has a sister named Nadia."]


def test_rolling_summary_drops_meta_lines():
    s = _session()
    kob = FakeKobold("Here's my updated memory:\n- Jonah came home late.\n"
                     "Where things stand: Jonah is asleep.")
    asyncio.run(memory.summarize(kob, s, CARD, PERSONA))
    assert s.memory.summary == "- Jonah came home late.\nWhere things stand: Jonah is asleep."
