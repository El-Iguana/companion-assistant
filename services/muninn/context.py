"""The context window: how big it really is, and fitting a prompt into it.

KoboldCPP decides the real limit (its `--contextsize`, 8192 by default) and reports
it at `/api/extra/true_max_context_length`. The user may choose a smaller window
in the app's settings (Pantheon: `AppSettings.context_limit`, 0 = Auto). The app can never use more
than the backend has, so the effective window is the smaller of the two.

Why this matters (PROMPT_BUDGET.md): an over-long prompt is not refused. KoboldCPP
cuts it from the FRONT without any error, and the front is the system block:
persona, relationship, memory. So the history is trimmed here, oldest turns first,
before the prompt is sent.
"""
from __future__ import annotations

import math

DEFAULT_CONTEXT = 8192       # what we assume when the backend can't be asked
# Characters per token for budgeting. Measured ~5.0 on this model and content
# (PROMPT_BUDGET.md), but that is an average and these are limits, so err low:
# a low estimate costs one dropped turn, a high one costs the system prompt.
CHARS_PER_TOKEN = 4.0
MESSAGE_OVERHEAD = 4         # chat-template tokens around each message
CONTINUE_TOKENS = 80         # the auto-continue round's cap (kobold.chat_generate_stream)
SAFETY_TOKENS = 64


def effective(setting: int, backend: int | None) -> int:
    """The window to budget against: the user's choice, never above the backend's."""
    real = backend if backend and backend > 0 else DEFAULT_CONTEXT
    if setting and setting > 0:
        return min(setting, real)
    return real


def tokens(text: str) -> int:
    return math.ceil(len(text or "") / CHARS_PER_TOKEN)


def message_tokens(m: dict) -> int:
    return tokens(m.get("content") or "") + MESSAGE_OVERHEAD


def reply_reserve(max_tokens: int | None, continue_rounds: int = 1) -> int:
    """Room the reply needs. Each continue round re-sends the reply so far as a prefill
    and may add CONTINUE_TOKENS more: Pantheon's client runs one round, Rhapsode's three."""
    return int(max_tokens or 220) + CONTINUE_TOKENS * max(0, continue_rounds) + SAFETY_TOKENS


def fit_history(fixed: list[dict], history: list[dict], limit: int,
                reserve: int) -> tuple[list[dict], int]:
    """Drop the oldest history messages until fixed + history + reserve fits `limit`.

    `fixed` is everything that is never dropped here (system block, trailing
    notes). The newest history message is always kept: it is what is being
    answered. Returns (kept_history, dropped_count).
    """
    budget = limit - reserve - sum(message_tokens(m) for m in fixed)
    sizes = [message_tokens(m) for m in history]
    total = sum(sizes)
    drop = 0
    while total > budget and drop < len(history) - 1:
        total -= sizes[drop]
        drop += 1
    return history[drop:], drop


def fold_transcript_chars(limit: int, reserve_tokens: int) -> int:
    """How much transcript one memory-fold pass may carry in a `limit` window.

    Floored so a small window still makes progress: a fold that cannot carry a
    single turn would stall the summary forever.
    """
    return max(int((limit - reserve_tokens) * CHARS_PER_TOKEN), 2000)


__all__ = ["DEFAULT_CONTEXT", "effective", "tokens", "message_tokens",
           "reply_reserve", "fit_history", "fold_transcript_chars"]
