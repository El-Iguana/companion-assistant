"""Guards against a character repeating herself (eval/repetition measured both).

* DRY sampler settings go to KoboldCPP with every reply (and its auto-continue).
* "Another take" quotes the take being replaced, which used to be dropped from the
  prompt entirely, so the model had nothing to differ from.

    .venv/bin/python -m pytest -q tests/test_repetition_guards.py
"""
from __future__ import annotations

import asyncio

import main
from models.schema import ChatRequest, ChatSession, GenerationConfig
from services import prompt_builder
from test_chat_stream import _events, _jobs, cur, model, session  # noqa: F401 — fixtures


def test_dry_is_on_by_default_and_reaches_the_backend():
    params = prompt_builder.kobold_params(ChatSession())
    assert params["dry_multiplier"] == 0.8 and params["dry_allowed_length"] == 2
    old = ChatSession.model_validate({"generation": {"max_tokens": 220}})   # a stored chat
    assert prompt_builder.kobold_params(old)["dry_multiplier"] == 0.8
    off = ChatSession(generation=GenerationConfig(dry_multiplier=0.0))
    assert prompt_builder.kobold_params(off)["dry_multiplier"] == 0.0


def test_another_take_quotes_the_take_it_replaces(cur, session, model, monkeypatch):
    prompts, replies = [], iter(["*Iris laughs and pours the tea.* \"Sit, sit.\"", "*Iris nods.* \"Of course.\""])

    async def tokens(messages, params):
        prompts.append((messages, params))
        yield next(replies)

    monkeypatch.setattr(main.kobold, "chat_generate_stream", tokens)

    async def go():
        for req in (ChatRequest(session_id=session.id, input_text="Tea?"),
                    ChatRequest(session_id=session.id, input_text="", regenerate=True)):
            resp = await main.chat(req, cur=cur)
            _events([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])
            await _jobs()

    asyncio.run(go())
    first, retake = prompts[0][0][-1]["content"], prompts[1][0][-1]["content"]
    assert "another take" not in first
    assert "another take" in retake and "Iris laughs and pours the tea." in retake
    assert retake.rstrip().endswith(prompt_builder.focus_note("Tea?", "Iris", main._default_persona(cur).name,
                                                              False).rstrip())   # focus stays last
    assert prompts[1][1]["dry_multiplier"] == 0.8


def test_retake_note_quotes_at_most_two_trimmed_takes():
    note = prompt_builder.retake_note(["a " * 400, "second", "third"], "Iris", "Eli")
    assert '"second"' in note and '"third"' in note and "a a a" not in note
    assert prompt_builder.retake_note(["", "  "], "Iris", "Eli") == ""
