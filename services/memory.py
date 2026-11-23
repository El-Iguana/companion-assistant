"""Shared helpers for the assistant memory fold (services/assistant_memory.py).

The roleplay app this repo was split from (El-Iguana/companion, 2026-11-07) keeps its
companion memory here: rolling story summary, cross-session history, semantic recall,
story memory. An assistant uses none of that, so only the pieces the assistant fold
needs remain: the fold threshold, the speaker-labelled transcript, and the fold budget
from Muninn.
"""
from __future__ import annotations

from services.muninn import fold as _fold
# The fold budget lives in Muninn (services/muninn, copied from Muninn; MEMORY_CORE_PLAN.md); re-exported here
# under the names assistant_memory and the tests use.
from services.muninn.fold import (FOLD_CHARS_PER_TOKEN, FOLD_CONTEXT_TOKENS,  # noqa: F401
                         FOLD_RESERVE_TOKENS, FOLD_TRANSCRIPT_CHARS, fold_budget)

from models.schema import ChatSession
from services.prompt_builder import substitute_macros, speaker

# Defaults for the fold threshold. Overridable per-user via AppSettings
# (summarize_after / keep_recent / summary_max_tokens); these constants remain
# the fallback when no settings are passed.
SUMMARIZE_AFTER = 24        # fold once this many un-folded messages accumulate
KEEP_RECENT = 12            # keep this many recent turns verbatim in the live prompt
SUMMARY_MAX_TOKENS = 400    # token budget for one fold pass


def needs_summary(session: ChatSession, summarize_after: int = SUMMARIZE_AFTER) -> bool:
    unsummarized = len(session.messages) - session.memory.last_summarized_index
    return unsummarized >= summarize_after


def _transcript(messages, char_name: str, user_name: str) -> str:
    """Speaker-labelled transcript of `messages`, macros already substituted.
    Only the two speakers: a "narrator" message copied over from the roleplay app
    is a scene event, not something either of them said."""
    return "\n".join(
        f"{speaker(m, char_name, user_name)}: "
        f"{substitute_macros(m.content, char_name, user_name)}"
        for m in messages if m.role in ("char", "user")
    )


def _fold_chunks(messages, start: int, upto: int, char_name: str, user_name: str,
                 budget: int = FOLD_TRANSCRIPT_CHARS) -> list[tuple[int, str]]:
    """Split [start, upto) into (end_index, transcript) passes that each fit `budget`.

    muninn.fold.fold_chunks, with this app's speaker-labelled transcript. Why the
    range is split into passes rather than truncated is documented there."""
    return _fold.fold_chunks(messages, start, upto,
                             lambda ms: _transcript(ms, char_name, user_name), budget)
