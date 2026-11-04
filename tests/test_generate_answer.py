"""generate_answer returns only the answer, whatever template the model answers in.

Every memory fold, fact extraction and classifier call goes through it. Models
that ignore the prefilled empty <think> wrote reasoning plus a stray closing tag
(or `</assistant>`), and all of it was stored as memory.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.kobold import KoboldClient  # noqa: E402


def _answer(raw: str) -> str:
    k = KoboldClient("http://unused")
    sent = {}

    async def fake_generate_full(prompt, params):
        sent["prompt"], sent["params"] = prompt, params
        return raw, False
    k.generate_full = fake_generate_full
    out = asyncio.run(k.generate_answer("Summarize.", {"max_length": 50}))
    assert sent["params"]["stop_sequence"] == ["<|im_end|>"]
    assert sent["prompt"].endswith("<think>\n\n</think>\n\n")
    return out


def test_reasoning_before_a_stray_close_tag_is_not_returned():
    raw = "I should keep it terse. DO NOT record Mira's replies.\n\n</thought>\n\n- Jonah has a cat"
    assert _answer(raw) == "- Jonah has a cat"


def test_stray_end_of_turn_tag_is_removed():
    assert _answer("- Eli plays bass\n\n</assistant>") == "- Eli plays bass"


def test_plain_answer_unchanged():
    assert _answer("joy") == "joy"
