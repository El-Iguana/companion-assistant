"""Background memory work yields to a reply (main._yield_memory_jobs).

KoboldCPP does one generation at a time. The memory job (summary pass, long-term
fold, fact extraction) runs right after `done`, so a message sent soon after waited
behind it: measured, the next reply's first token at 52 s instead of 2-3 s. A reply
now cancels a running job by its genkey; the job stores nothing and runs again after
a later turn. A job that has already yielded MEMORY_MAX_YIELDS times in a row finishes.

    .venv/bin/python -m pytest -q tests/test_memory_yields.py
"""
from __future__ import annotations

import asyncio

import main
from models.schema import ChatRequest, ChatSession
from test_chat_stream import _events, _jobs, cur, model, session  # noqa: F401 — fixtures


def _wire(monkeypatch, gate: asyncio.Event, aborted: list):
    """A summary pass that makes one (slow, gated) model call through the job's client."""
    stored = []

    async def summarize(kobold, session, card, persona, **k):
        try:                                      # best-effort, like the real one
            text, _ = await kobold.generate_answer_full("summarize", {"max_length": 400})
        except Exception:
            return
        session.memory.summary = text
        session.memory.last_summarized_index = len(session.messages) - 1
        stored.append(text)

    async def slow_answer(prompt, params):
        assert params.get("genkey", "").startswith("pantheon-mem-")
        await gate.wait()
        return "S", False

    async def tokens(messages, params):
        yield "Hello."

    async def abort(genkey):
        aborted.append(genkey)

    monkeypatch.setattr(main.memory, "summarize", summarize)
    monkeypatch.setattr(main.kobold, "generate_answer_full", slow_answer)
    monkeypatch.setattr(main.kobold, "chat_generate_stream", tokens)
    monkeypatch.setattr(main.kobold, "abort", abort)
    monkeypatch.setattr(main.memory, "needs_summary", lambda s, n=24: True)
    return stored


async def _turn(cur, session, text):
    resp = await main.chat(ChatRequest(session_id=session.id, input_text=text), cur=cur)
    return _events([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])


def test_a_reply_cancels_the_running_memory_job(cur, session, model, monkeypatch):
    gate, aborted = asyncio.Event(), []
    stored = _wire(monkeypatch, gate, aborted)
    main._memory_yields.pop(session.id, None)

    async def go():
        await _turn(cur, session, "first")
        await asyncio.sleep(0.05)                     # the job is waiting on the model
        assert session.id in main._memory_running
        key = main._memory_running[session.id].key
        await _turn(cur, session, "second")           # starts before the job finishes
        assert aborted == [key]
        gate.set()                                    # the cancelled call returns its partial text
        await _jobs()

    asyncio.run(go())
    assert stored == ["S"]                            # only the job after the second turn stored
    saved = main._load(cur.sessions, session.id, ChatSession)
    assert saved.memory.summary == "S"
    assert main._memory_yields.get(session.id) is None   # reset by the job that finished


def test_a_job_that_yielded_enough_is_left_to_finish(cur, session, model, monkeypatch):
    gate, aborted = asyncio.Event(), []
    _wire(monkeypatch, gate, aborted)
    main._memory_yields[session.id] = main.MEMORY_MAX_YIELDS

    async def go():
        await _turn(cur, session, "first")
        await asyncio.sleep(0.05)
        await main._yield_memory_jobs()
        assert aborted == []
        assert not main._memory_running[session.id].cancelled
        gate.set()
        await _jobs()

    asyncio.run(go())
    assert main._load(cur.sessions, session.id, ChatSession).memory.summary == "S"


def test_a_job_cancelled_before_its_call_doesnt_start_it(cur, session, model, monkeypatch):
    calls = []

    async def answer(prompt, params):
        calls.append(prompt)
        return "S", False

    monkeypatch.setattr(main.kobold, "generate_answer_full", answer)
    k = main._YieldingKobold(main.kobold, "pantheon-mem-test")
    k.cancelled = True
    try:
        asyncio.run(k.generate_answer_full("p", {}))
        raised = False
    except main._MemoryYielded:
        raised = True
    assert raised and calls == []


def test_best_effort_code_cant_swallow_the_yield(cur, session, monkeypatch):
    """The memory steps catch `Exception` around every model call. The yield must get
    past that, or the job carries on and resets its yield count (so the starvation
    guard never trips)."""
    carried_on = []

    async def maintain(cur_, sid, k):
        k.cancelled = True                       # a reply arrived
        try:
            await k.generate_answer("summarize", {})
        except Exception:                        # what memory.summarize does
            pass
        carried_on.append("fold")                # the next step must not run

    monkeypatch.setattr(main, "_maintain_memory", maintain)
    main._memory_yields[session.id] = 1
    asyncio.run(main._memory_job(cur, session.id))
    assert carried_on == []
    assert main._memory_yields[session.id] == 1
