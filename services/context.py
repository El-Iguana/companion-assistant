"""The context window — Muninn's (muninn.context), plus this app's selectable sizes.

Everything else (effective size, token estimate, history fit, fold budget) moved to
Muninn so Rhapsode budgets its prompts the same way; see that module for why the
history is trimmed here and never left to KoboldCPP's front truncation.
"""
from __future__ import annotations

from muninn.context import (CHARS_PER_TOKEN, CONTINUE_TOKENS, DEFAULT_CONTEXT,  # noqa: F401
                            MESSAGE_OVERHEAD, SAFETY_TOKENS, effective, fit_history,
                            fold_transcript_chars, message_tokens, reply_reserve, tokens)

from models.schema import CONTEXT_SIZES  # noqa: F401

__all__ = ["CONTEXT_SIZES", "DEFAULT_CONTEXT", "effective", "tokens", "message_tokens",
           "reply_reserve", "fit_history", "fold_transcript_chars"]
