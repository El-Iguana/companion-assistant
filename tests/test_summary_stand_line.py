"""The rolling summary keeps its "Where things stand:" line when the cap cuts it.

The 400-token summary cap cut the closing stand line off, and the cut text was saved.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from models.schema import CharacterCard, ChatMessage, ChatSession, Persona  # noqa: E402
from services import memory  # noqa: E402


class FakeKobold:
    """Answers the summary pass with `fold` (text, truncated); the stand-line
    repair (max_length = muninn.summary.STAND_MAX_TOKENS) with `stand`."""

    def __init__(self, fold, stand="Where things stand: Mira and Eli are on the sofa."):
        self.fold, self.stand, self.calls = fold, stand, []

    async def generate_answer_full(self, prompt, params):
        self.calls.append("fold")
        return self.fold

    async def generate_answer(self, prompt, params):
        self.calls.append("stand")
        return self.stand


def _summarize(kob, n=30):
    s = ChatSession(character_id="c", persona_id="p")
    for i in range(n):
        s.messages.append(ChatMessage(role="user" if i % 2 == 0 else "char", content=f"turn {i}"))
    asyncio.run(memory.summarize(kob, s, CharacterCard(name="Mira"), Persona(name="Eli")))
    return s.memory


def test_cut_off_summary_loses_the_half_line_and_gets_its_stand_line_back():
    kob = FakeKobold(("- Eli came home late.\n- They ate curry.\nWhere things stand: Eli and Mi", True))
    mem = _summarize(kob)
    assert mem.summary == ("- Eli came home late.\n- They ate curry.\n"
                           "Where things stand: Mira and Eli are on the sofa.")
    assert kob.calls == ["fold", "stand"] and mem.last_summarized_index == 30 - memory.KEEP_RECENT


def test_complete_summary_needs_no_repair_and_keeps_one_stand_line_last():
    kob = FakeKobold(("Where things stand: at the door.\n- Eli came home.\n"
                      "Where things stand: Eli is asleep.", False))
    mem = _summarize(kob)
    assert mem.summary == "- Eli came home.\nWhere things stand: Eli is asleep."
    assert kob.calls == ["fold"]


def test_failed_repair_still_stores_the_notes():
    kob = FakeKobold(("- Eli came home.\n- They ate curry and talked about the Ork", True), stand="")
    assert _summarize(kob).summary == "- Eli came home."
