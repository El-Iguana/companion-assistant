"""Memory commands (Memory Phase 7, #22): "remember…", "forget…" and "what do you remember"
act at once through the Memory skill's tools, on the same store as the fold and the modal.

    .venv/bin/python -m pytest -q tests/test_memory_commands.py
"""
from __future__ import annotations

import asyncio
import json

import main
from models.schema import CharacterCard, ChatMessage, ChatSession, MemoryFact
from services import assistant_memory, skills
from services.kobold import ToolCallsEvent
from test_chat_stream import cur  # noqa: F401 — fixture


def _setup(cur, skills_on=("memory",)):
    card = CharacterCard(name="Mira", skills=list(skills_on))
    rex = CharacterCard(name="Rex")
    main._save(card, cur.cards)
    main._save(rex, cur.cards)
    pid = main._default_persona(cur).id
    ctx = skills.SkillContext(user_id=cur.user.id, root=cur.cards.parent, card_id=card.id, persona_id=pid)
    return card, rex, pid, ctx


def _call(ctx, name, **args):
    tools = skills.tools_for(main._load(ctx.root / "characters", ctx.card_id, CharacterCard), ctx)
    return asyncio.run(skills.run({"name": name, "arguments": json.dumps(args)}, tools, ctx))


def test_the_menu_offers_memory_only_to_a_card_that_has_it(cur):
    card, rex, _, ctx = _setup(cur)
    assert [t.name for t in skills.tools_for(card, ctx)] == ["memory_remember", "memory_forget", "memory_list"]
    assert skills.tools_for(rex, ctx) == []
    assert "memory" in {m["id"] for m in skills.menu()}


def test_remember_writes_the_users_own_entries_at_once(cur):
    card, rex, pid, ctx = _setup(cur)
    r = _call(ctx, "memory_remember", text="My sister's name is Nadia", kind="fact")
    assert r["ok"] and r["final"] == "Noted: My sister's name is Nadia."
    r = _call(ctx, "memory_remember", text="Use 24-hour times", kind="instruction", everyone=True)
    assert r["ok"] and r["for_every_bartender"]
    _call(ctx, "memory_remember", text="Keep answers short", kind="instruction")

    mine = main._load_assistant_memory(cur, card.id, pid)
    assert [(f.text, f.origin) for f in mine.facts] == [("My sister's name is Nadia", "user")]
    assert {d.text for d in mine.directives} == {"Use 24-hour times", "Keep answers short"}
    other = main._load_assistant_memory(cur, rex.id, pid)
    assert [f.text for f in other.facts] == ["My sister's name is Nadia"]      # facts are shared
    assert [d.text for d in other.directives] == ["Use 24-hour times"]          # only the one for all


def test_remembering_something_known_makes_it_the_users(cur):
    card, _, pid, ctx = _setup(cur)
    mem = main._load_assistant_memory(cur, card.id, pid)
    mem.facts.append(MemoryFact(text="Brian lives in Porto", origin="auto"))
    main._save_assistant_memory(cur, mem)
    r = _call(ctx, "memory_remember", text="brian lives in porto.", kind="fact")
    assert r["already_known"]
    facts = main._load_assistant_memory(cur, card.id, pid).facts
    assert [(f.text, f.origin) for f in facts] == [("Brian lives in Porto", "user")]


def test_forget_removes_what_matches_and_says_what(cur):
    card, _, pid, ctx = _setup(cur)
    mem = main._load_assistant_memory(cur, card.id, pid)
    mem.facts += [MemoryFact(text="Her sister's name is Nadia", origin="auto"),
                  MemoryFact(text="Brian lives in Porto")]
    mem.notes = ["The DNS switch is still to do"]
    main._save_assistant_memory(cur, mem)

    r = _call(ctx, "memory_forget", about="my sister Nadia")
    assert r["removed"] == ["Her sister's name is Nadia"]
    assert r["final"] == "Forgotten: Her sister's name is Nadia."
    r = _call(ctx, "memory_forget", about="the DNS thing")
    assert r["removed"] == ["The DNS switch is still to do"]
    after = main._load_assistant_memory(cur, card.id, pid)
    assert [f.text for f in after.facts] == ["Brian lives in Porto"]
    assert after.notes == []
    assert after.forgotten == ["Her sister's name is Nadia", "The DNS switch is still to do"]

    r = _call(ctx, "memory_forget", about="my favourite colour")
    assert r["removed"] == [] and r["final"] == "I had nothing about that."


def test_forget_asks_rather_than_wiping_many(cur):
    card, _, pid, ctx = _setup(cur)
    mem = main._load_assistant_memory(cur, card.id, pid)
    mem.facts += [MemoryFact(text=f"Brian's car number {i} is blue") for i in range(5)]
    main._save_assistant_memory(cur, mem)
    r = _call(ctx, "memory_forget", about="car")
    assert r["removed"] == [] and "Which should I forget?" in r["final"]
    assert len(main._load_assistant_memory(cur, card.id, pid).facts) == 5


def test_remembering_again_lifts_the_forget(cur):
    card, _, pid, ctx = _setup(cur)
    _call(ctx, "memory_remember", text="Brian is vegetarian", kind="fact")
    _call(ctx, "memory_forget", about="vegetarian")
    assert main._load_assistant_memory(cur, card.id, pid).forgotten == ["Brian is vegetarian"]
    _call(ctx, "memory_remember", text="Brian is vegetarian", kind="fact")
    mem = main._load_assistant_memory(cur, card.id, pid)
    assert mem.forgotten == [] and [f.text for f in mem.facts] == ["Brian is vegetarian"]


def test_list_hands_the_model_what_she_knows(cur):
    card, _, pid, ctx = _setup(cur)
    assert _call(ctx, "memory_list")["empty"]
    _call(ctx, "memory_remember", text="Keep answers short", kind="instruction")
    _call(ctx, "memory_remember", text="Brian lives in Porto", kind="fact")
    r = _call(ctx, "memory_list")
    assert r["instructions"] == ["Keep answers short"] and r["facts"] == ["Brian lives in Porto"]
    assert "final" not in r                                       # she answers in her own words


def test_the_extractor_never_reads_a_command_and_knows_what_was_forgotten(cur, monkeypatch):
    card, _, pid, ctx = _setup(cur)
    mem = main._load_assistant_memory(cur, card.id, pid)
    mem.forgotten = ["Brian's sister is Nadia"]
    s = ChatSession(character_id=card.id, persona_id=pid, messages=[
        ChatMessage(role="user", content="My sister is Nadia"),
        ChatMessage(role="char", content="Lovely."),
        ChatMessage(role="user", content="Forget that my sister is Nadia", memory_command=True),
        ChatMessage(role="char", content="Forgotten.")])
    prompts = []

    async def generate_answer(prompt, params):
        prompts.append(prompt)
        return "FACT: Brian's sister is Nadia" if "Durable items" in prompt else "NONE"

    class K:
        pass
    k = K()
    k.generate_answer = generate_answer
    monkeypatch.setattr(assistant_memory, "worth_reading", lambda turns: asyncio.sleep(0, turns))
    asyncio.run(assistant_memory.fold(k, mem, s, card, main._default_persona(cur)))
    extract_prompt = next(p for p in prompts if "Durable items" in p)
    assert "Forget that" not in extract_prompt                   # the command turn is not mined
    assert "(fact) Brian's sister is Nadia" in extract_prompt     # forgotten = already known
    assert [f.text for f in mem.facts] == []                      # and not written back


def test_a_remember_turn_answers_in_one_pass_and_marks_the_turn(cur, monkeypatch):
    card, _, pid, _ = _setup(cur)
    s = ChatSession(character_id=card.id, persona_id=pid)
    main._save(s, cur.sessions)
    passes = []

    async def fake_stream(messages, params):
        passes.append(params)
        yield ToolCallsEvent([{"id": "c1", "name": "memory_remember",
                               "arguments": '{"text": "Brian is allergic to peanuts", "kind": "fact"}'}])

    monkeypatch.setattr(main.kobold, "chat_generate_stream", fake_stream)
    monkeypatch.setattr(main.assistant_memory, "fold", lambda *a, **k: asyncio.sleep(0, False))

    async def go():
        resp = await main.chat(main.ChatRequest(session_id=s.id, input_text="remember I'm allergic to peanuts"),
                               cur=cur)
        return [json.loads(c[6:]) for chunk in [c if isinstance(c, str) else c.decode()
                                                 async for c in resp.body_iterator]
                for c in chunk.split("\n\n") if c.startswith("data: ")]
    evs = asyncio.run(go())

    assert len(passes) == 1                                        # the fixed line, no second pass
    assert {"skill": {"tool": "memory_remember", "status": "Making a note…", "icon": "📝"}} in evs
    saved = main._load(cur.sessions, s.id, ChatSession)
    assert saved.messages[-1].content == "Noted: Brian is allergic to peanuts."
    assert saved.messages[-2].memory_command is True
    facts = main._load_assistant_memory(cur, card.id, pid).facts
    assert [(f.text, f.origin) for f in facts] == [("Brian is allergic to peanuts", "user")]
