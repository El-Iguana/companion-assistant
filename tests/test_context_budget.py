"""The context window (services/context.py): reading the backend's real limit, the
Settings choice, and fitting the prompt by dropping the oldest turns, never the
system block.

    .venv/bin/python -m pytest -q tests/test_context_budget.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PANTHEON_DATA_DIR", tempfile.mkdtemp(prefix="pantheon-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from models.schema import AppSettings, CharacterCard, ChatMessage, ChatSession, Persona  # noqa: E402
from services import context, memory, prompt_builder  # noqa: E402
from services.kobold import KoboldClient  # noqa: E402


# --- the effective window ---------------------------------------------------------

def test_auto_uses_the_backend_limit():
    assert context.effective(0, 16384) == 16384


def test_a_chosen_size_never_exceeds_the_backend():
    assert context.effective(16384, 8192) == 8192
    assert context.effective(4096, 8192) == 4096


def test_unknown_backend_falls_back_to_the_default():
    assert context.effective(0, None) == context.DEFAULT_CONTEXT
    assert context.effective(32768, None) == context.DEFAULT_CONTEXT


def test_settings_accept_only_auto_or_a_standard_size():
    assert AppSettings().context_limit == 0
    assert AppSettings(context_limit=12288).context_limit == 12288
    assert AppSettings(context_limit="16384").context_limit == 16384   # the page sends strings
    with pytest.raises(ValidationError):
        AppSettings(context_limit=10000)


# --- reading the real limit from KoboldCPP -----------------------------------------

# conftest stubs context_limit for every test; this is the real one, captured at
# import time (before any fixture runs).
_REAL_LIMIT = KoboldClient.context_limit


def _client(handler) -> KoboldClient:
    k = KoboldClient("http://kobold.test")
    k._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return k


def test_probe_reads_value_and_keeps_last_good_when_down(monkeypatch):
    state = {"up": True, "calls": 0}

    def handler(req):
        state["calls"] += 1
        assert req.url.path == "/api/extra/true_max_context_length"
        if not state["up"]:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"value": 12288})

    k = _client(handler)
    ask = lambda: asyncio.run(_REAL_LIMIT(k))  # noqa: E731
    assert ask() == 12288
    assert ask() == 12288 and state["calls"] == 1                             # cached
    state["up"] = False
    k._ctx_cache = (12288, -1e9, True)                                         # expire it
    assert ask() == 12288                                                     # last good
    calls = state["calls"]
    assert ask() == 12288 and state["calls"] == calls                         # no retry storm


def test_probe_never_seen_returns_none():
    k = _client(lambda req: httpx.Response(404))
    assert asyncio.run(_REAL_LIMIT(k)) is None


# --- fitting the prompt ------------------------------------------------------------

def _session(turns: int, size: int) -> tuple[CharacterCard, Persona, ChatSession]:
    card = CharacterCard(name="Mira", description="A sailor. " * 20)
    persona = Persona(name="Sam")
    s = ChatSession(character_id=card.id, persona_id=persona.id)
    for i in range(turns):
        role = "user" if i % 2 == 0 else "char"
        s.messages.append(ChatMessage(role=role, content=f"turn {i} " + "x" * size))
    return card, persona, s


def _tokens(msgs) -> int:
    return sum(context.message_tokens(m) for m in msgs)


def test_without_a_window_nothing_is_dropped():
    card, persona, s = _session(40, 2000)
    msgs = prompt_builder.build_messages(card, persona, s)
    assert sum(1 for m in msgs if m["role"] in ("user", "assistant")) == 40


def test_overflow_drops_oldest_turns_and_keeps_system_and_tail():
    card, persona, s = _session(41, 2000)          # ~20k tokens of history
    trimmed: list = []
    msgs = prompt_builder.build_messages(card, persona, s, context_tokens=8192, trimmed=trimmed)
    assert trimmed[0] > 0
    assert msgs[0]["role"] == "system" and "Mira" in msgs[0]["content"]     # system block kept
    assert "[Now]" in msgs[-1]["content"]                                   # trailing note kept
    chat = [m for m in msgs if m["role"] in ("user", "assistant")]
    assert chat[-1]["content"].startswith("turn 40 ")                       # newest kept
    assert not any(m["content"].startswith("turn 0 ") for m in chat)        # oldest gone
    assert _tokens(msgs) + context.reply_reserve(s.generation.max_tokens) <= 8192


def test_a_bigger_window_keeps_more():
    card, persona, s = _session(41, 2000)
    small, big = [], []
    prompt_builder.build_messages(card, persona, s, context_tokens=8192, trimmed=small)
    prompt_builder.build_messages(card, persona, s, context_tokens=16384, trimmed=big)
    assert big[0] < small[0]


def test_the_message_being_answered_is_never_dropped():
    card, persona, s = _session(1, 60000)          # one turn bigger than the window
    msgs = prompt_builder.build_messages(card, persona, s, context_tokens=4096)
    assert any(m["role"] == "user" for m in msgs)


# --- memory folds follow the window --------------------------------------------------

def test_fold_budget_scales_with_the_window():
    assert memory.fold_budget(8192) == memory.FOLD_TRANSCRIPT_CHARS
    assert memory.fold_budget(16384) > memory.fold_budget(8192) > memory.fold_budget(4096)
    assert memory.fold_budget(4096) >= 2000
