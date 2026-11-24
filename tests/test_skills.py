"""The Menu: the skill runtime, the tool loop in chat(), and the DJ's pairing and search.

No model and no Orpheus server: KoboldCPP's stream and the Orpheus client are faked.

    .venv/bin/python -m pytest -q tests/test_skills.py
"""
from __future__ import annotations

import asyncio
import json
import stat

import httpx
import pytest

import main
from models.schema import CharacterCard
from services import kobold as kobold_mod
from services import skills
from services.kobold import KoboldClient, ToolCallsEvent
from services.skills import media
from services.skills import orpheus_media as om
from test_chat_stream import _events, _reload, cur, session  # noqa: F401 — fixtures


# --- validation -------------------------------------------------------------------------

SCHEMA = {"type": "object", "properties": {
    "query": {"type": "string", "maxLength": 20},
    "kinds": {"type": "array", "maxItems": 2, "items": {"type": "string", "enum": ["album", "track"]}},
    "level": {"type": "integer", "minimum": 0, "maximum": 100}}, "required": ["query"]}


def test_validation_is_strict():
    assert skills.validate({"query": " rush ", "level": "30"}, SCHEMA) == {"query": "rush", "level": 30}
    for bad, why in (({}, "required"), ({"query": "x", "url": "http://evil"}, "unknown"),
                     ({"query": "x" * 21}, "too long"), ({"query": "x", "kinds": ["video"]}, "one of"),
                     ({"query": "x", "level": 101}, "at most"), ({"query": "x", "level": 2.5}, "whole"),
                     ({"query": "x", "kinds": ["album", "track", "album"]}, "too many")):
        with pytest.raises(skills.ArgError, match=why):
            skills.validate(bad, SCHEMA)


# --- execution --------------------------------------------------------------------------

def _ctx(tmp_path):
    return skills.SkillContext(user_id="u1", root=tmp_path, card_id="c1", session_id="s1")


def test_run_normalises_every_outcome_and_audits(tmp_path, monkeypatch):
    async def ok(ctx, args):
        return {"found": args["query"]}

    async def boom(ctx, args):
        raise RuntimeError("secret detail")

    async def slow(ctx, args):
        await asyncio.sleep(1)

    tools = [skills.Tool("t_ok", "", SCHEMA, ok), skills.Tool("t_boom", "", {"type": "object", "properties": {}}, boom),
             skills.Tool("t_slow", "", {"type": "object", "properties": {}}, slow)]
    monkeypatch.setattr(skills, "TOOL_TIMEOUT_S", 0.05)
    run = lambda name, args="{}": asyncio.run(skills.run({"name": name, "arguments": args}, tools, _ctx(tmp_path)))
    assert run("t_ok", '{"query": "rush"}')["found"] == "rush"
    assert run("t_ok", "not json")["error"] == "BAD_ARGUMENTS"
    assert run("t_ok", '{"query": "rush", "path": "/etc"}')["error"] == "BAD_ARGUMENTS"
    assert run("nope")["error"] == "UNKNOWN_TOOL"
    failed = run("t_boom")
    assert failed["error"] == "TOOL_FAILED" and "secret detail" not in failed["message"]
    assert run("t_slow")["error"] == "TIMEOUT"
    audit = [json.loads(l) for l in (tmp_path / "skills" / "audit.jsonl").read_text().splitlines()]
    assert [a["tool"] for a in audit] == ["t_ok", "t_ok", "t_ok", "nope", "t_boom", "t_slow"]
    assert audit[0]["ok"] and audit[0]["args"] == {"query": "rush"} and audit[0]["op"].startswith("op-")


def test_result_text_is_capped():
    assert len(skills.result_text({"ok": True, "x": "a" * 5000})) <= skills.RESULT_MAX_CHARS


# --- KoboldCPP: tool_calls deltas become one ToolCallsEvent ---------------------------------

def test_stream_assembles_tool_calls_from_deltas():
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "media_", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "find", "arguments": '{"query": '}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"Rush"}'}}]}, "finish_reason": "tool_calls"}]},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    k = KoboldClient("http://kobold")
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))

    async def go():
        return [x async for x in k.chat_generate_stream([{"role": "user", "content": "x"}], {})]
    out = asyncio.run(go())
    assert len(out) == 1 and isinstance(out[0], ToolCallsEvent)
    assert list(out[0]) == [{"id": "call_1", "name": "media_find", "arguments": '{"query": "Rush"}'}]


# --- the tool loop in chat() --------------------------------------------------------------

@pytest.fixture()
def echo_skill():
    seen = []

    async def handler(ctx, args):
        seen.append((ctx.user_id, args))
        return {"candidates": [{"id": 7, "name": "Moving Pictures — Rush"}]}
    skills.register(skills.Skill("echo", "Echo", "test", lambda ctx: [skills.Tool(
        "echo_find", "find", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        handler, status="Looking…")]))
    yield seen
    skills.REGISTRY.pop("echo", None)


def test_a_tool_turn_runs_the_tool_and_answers_with_it(cur, session, echo_skill, monkeypatch):
    card = main._load(cur.cards, session.character_id, CharacterCard)
    card.skills = ["echo"]
    main._save(card, cur.cards)
    passes = []

    async def fake_stream(messages, params):
        passes.append((messages, params))
        if len(passes) == 1:
            yield ToolCallsEvent([{"id": "c1", "name": "echo_find", "arguments": '{"query": "rush"}'}])
            return
        for t in ("I found Moving Pictures by Rush. ", "Which one did you want?"):
            yield t

    async def neutral(*a, **k):
        return "neutral"
    monkeypatch.setattr(main.kobold, "chat_generate_stream", fake_stream)
    monkeypatch.setattr(main.expressions, "classify", neutral)
    monkeypatch.setattr(main.assistant_memory, "fold", lambda *a, **k: asyncio.sleep(0, False))

    async def go():
        resp = await main.chat(main.ChatRequest(session_id=session.id, input_text="play rush"), cur=cur)
        return _events([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])
    evs = asyncio.run(go())

    assert echo_skill == [(cur.user.id, {"query": "rush"})]          # bound to the signed-in user
    assert {"skill": {"tool": "echo_find", "status": "Looking…"}} in evs
    first_msgs, first_params = passes[0]
    assert [t["function"]["name"] for t in first_params["tools"]] == ["echo_find"]
    second = passes[1][0]
    roles = [m["role"] for m in second]
    assert roles[-3:] == ["assistant", "tool", "system"]               # the trailing note stays last
    assert second[-3]["tool_calls"][0]["function"]["name"] == "echo_find"
    assert "Moving Pictures" in second[-2]["content"]
    saved = _reload(cur, session).messages[-1].content
    assert saved.endswith("Which one did you want?")                   # the tool's question survives


def test_a_card_without_skills_gets_no_tools(cur, session, monkeypatch):
    got = {}

    async def fake_stream(messages, params):
        got.update(params)
        yield "Hello."
    monkeypatch.setattr(main.kobold, "chat_generate_stream", fake_stream)

    async def go():
        resp = await main.chat(main.ChatRequest(session_id=session.id, input_text="hi"), cur=cur)
        return [c async for c in resp.body_iterator]
    asyncio.run(go())
    assert "tools" not in got


# --- the DJ ------------------------------------------------------------------------------------

def test_dj_offers_connect_until_paired_then_playback(tmp_path):
    ctx = _ctx(tmp_path)
    dj = skills.REGISTRY["dj"]
    assert [t.name for t in dj.tools(ctx)] == ["media_connect"] and dj.note(ctx) == ""
    om.save(tmp_path, {"url": "http://o", "device_id": "d", "token": "t"})
    assert [t.name for t in dj.tools(ctx)] == ["media_play", "media_control", "media_find", "media_status"]
    assert "media_play" in dj.note(ctx)


def test_pairing_needs_approval_and_keeps_the_token_out_of_results(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    approved = {"now": False}

    async def discover(saved=""):
        return "http://homelab:8096", {"product": "orpheus", "server_id": "S1", "name": "Orpheus"}

    async def start(base):
        return {"pairing_id": "p-secret", "code": "482193", "expires_at": "later"}

    async def poll(base, pid):
        assert pid == "p-secret"
        return {"device_id": "dev1", "token": "TOKEN-xyz"} if approved["now"] else {"status": "pending"}
    monkeypatch.setattr(om, "discover", discover)
    monkeypatch.setattr(om, "start_pairing", start)
    monkeypatch.setattr(om, "poll_pairing", poll)

    first = asyncio.run(media._connect(ctx, {}))
    assert first["status"] == "waiting_for_approval" and first["code"] == "482193"
    assert asyncio.run(media._connect(ctx, {}))["status"] == "waiting_for_approval"
    approved["now"] = True
    done = asyncio.run(media._connect(ctx, {}))
    assert done == {"ok": True, "status": "paired", "server": "Orpheus"}
    assert "TOKEN" not in json.dumps([first, done])
    state = om.load(tmp_path)
    assert state["token"] == "TOKEN-xyz" and state["url"] == "http://homelab:8096"
    assert stat.S_IMODE((tmp_path / "media" / "orpheus.json").stat().st_mode) == 0o600


def test_find_returns_candidates_and_never_a_question(tmp_path, monkeypatch):
    om.save(tmp_path, {"url": "http://o", "device_id": "d", "token": "t"})

    async def resolve(base, token, query, kinds, limit=5):
        assert (base, token, query) == ("http://o", "t", "sigur ros")
        assert kinds == ["artist", "album", "track", "playlist"]       # music only by default
        return {"candidates": [{"item_id": 9042, "kind": "album", "display": "( ) — Sigur Rós (2002)", "score": 0.83},
                               {"item_id": 9050, "kind": "album", "display": "Ágætis byrjun — Sigur Rós (1999)", "score": 0.8}],
                "needs_disambiguation": True, "disambiguation_prompt": "The album ( ), or Ágætis byrjun?"}
    monkeypatch.setattr(om, "resolve", resolve)
    out = asyncio.run(media._find(_ctx(tmp_path), {"query": "sigur ros"}))
    assert out["ambiguous"] and "ask" not in out                    # she never asks which (2026-11-24)
    assert [c["id"] for c in out["candidates"]] == ["9042", "9050"]


def test_discovery_refuses_a_wide_or_public_network(monkeypatch):
    monkeypatch.delenv("ORPHEUS_MEDIA_URL", raising=False)
    for cidr in ("10.0.0.0/8", "8.8.8.0/24"):
        monkeypatch.setenv("ORPHEUS_MEDIA_SCAN_CIDR", cidr)
        with pytest.raises(om.OrpheusError) as e:
            asyncio.run(om.discover())
        assert e.value.code == "NOT_CONFIGURED"


def test_find_drops_lookalikes_and_never_offers_a_film(tmp_path, monkeypatch):
    om.save(tmp_path, {"url": "http://o", "device_id": "d", "token": "t"})

    async def resolve(base, token, query, kinds, limit=5):
        # What the live library answered for a band it doesn't have.
        return {"candidates": [{"item_id": 1, "kind": "track", "display": "Sugar Mama — John Lee Hooker", "score": 0.34},
                               {"item_id": 2, "kind": "movie", "display": "Shazam! Fury of the Gods", "score": 0.61}],
                "needs_disambiguation": True, "disambiguation_prompt": "Sugar Mama, or Shazam?"}
    monkeypatch.setattr(om, "resolve", resolve)
    out = asyncio.run(media._find(_ctx(tmp_path), {"query": "sigur ros"}))
    assert out["ok"] is False and out["error"] == "MEDIA_NOT_FOUND"
