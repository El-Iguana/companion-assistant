"""Splitting a range of turns into context-sized fold passes.

Every memory fold (a session summary, a cross-session memory, an assistant's
notes) renders a stretch of turns as a transcript and asks the model to fold it
into what it already has. This module decides how much transcript one pass may
carry and where the passes break. What the pass *says* — the prompt — belongs to
the caller's profile. Moved from Pantheon's services/memory.py.
"""
from __future__ import annotations

from typing import Callable, Sequence

from . import context

# How much transcript one fold pass may carry. Sized from the effective context
# window (muninn.context: the backend's real limit, or a smaller one the app's
# settings chose), passed in as `context_tokens`; FOLD_CONTEXT_TOKENS is only the
# default when no caller says. A range larger than this is folded in successive
# passes rather than truncated — see fold_chunks() for why truncation is not an
# option here.
#
# Budgeted at 4.0 chars/token even though PROMPT_BUDGET.md measured ~5.0: that
# figure is an average over one corpus, not a floor, and the whole point of this
# cap is the case where it is wrong. The reserve covers the instructions, the
# existing memory (LONGTERM_BUDGET_CHARS) or the Known-facts list quoted back to
# extract_auto_facts (up to 40 x 200 chars — the binding case), and the reply
# being generated (summary_max_tokens, up to 1200). At 4.0 the worst prompt of
# either kind lands ~6% under the window; at the measured 5.0 it is ~25% under.
# The cost of being wrong the other way is one extra pass, so err low.
FOLD_CONTEXT_TOKENS = context.DEFAULT_CONTEXT
FOLD_CHARS_PER_TOKEN = 4.0
FOLD_RESERVE_TOKENS = 3000
FOLD_TRANSCRIPT_CHARS = int((FOLD_CONTEXT_TOKENS - FOLD_RESERVE_TOKENS) * FOLD_CHARS_PER_TOKEN)


def fold_budget(context_tokens: int) -> int:
    """Transcript characters one fold pass may carry in a `context_tokens` window."""
    return context.fold_transcript_chars(context_tokens, FOLD_RESERVE_TOKENS)


def fold_chunks(messages: Sequence, start: int, upto: int,
                render: Callable[[Sequence], str],
                budget: int = FOLD_TRANSCRIPT_CHARS) -> list[tuple[int, str]]:
    """Split [start, upto) into (end_index, transcript) passes that each fit `budget`.

    `render(messages)` turns a slice of turns into transcript text (speaker labels,
    macro substitution — the app's business); it must return "" for a slice with
    nothing worth folding, such as system-only turns.

    A long backlog — the first idle sweep of a finished chat, or a session that
    ran well past the threshold — renders a transcript that can exceed the whole
    context window. KoboldCPP truncates from the FRONT with no error, so the
    casualty is the fold prompt itself: the fold runs with none of the rules
    above it, which is precisely the tuning Pantheon's MEMORY.md records as
    hard-won.

    Truncating the transcript instead would be worse than useless — it drops
    events without saying so, and the fold is the ONLY thing standing between
    those turns and being forgotten. Successive passes keep every rule in every
    pass and lose nothing; it is what repeated threshold folds already do.
    """
    chunks: list[tuple[int, str]] = []
    lo = start
    while lo < upto:
        hi, size = lo, 0
        while hi < upto:
            line = len(render(messages[hi:hi + 1])) + 1
            if size and size + line > budget:
                break
            size += line
            hi += 1
        hi = max(hi, lo + 1)          # one turn longer than the whole budget: take it alone
        text = render(messages[lo:hi])[:budget]
        if text.strip():              # skip system-only stretches
            chunks.append((hi, text))
        lo = hi
    return chunks
