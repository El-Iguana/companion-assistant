"""Long-term memory: rolling summarization of older turns.

When the running history grows past a threshold, the oldest un-summarized turns
are folded into `session.memory.summary` so the prompt stays within budget while
preserving continuity. Uses the same KoboldCPP backend for the summarization pass.
"""
from __future__ import annotations

from datetime import datetime

from models.schema import ChatSession, CharacterCard, CharacterMemory, Persona
from services.kobold import KoboldClient
from services.prompt_builder import substitute_macros

# Defaults for the rolling summarizer. Overridable per-user via AppSettings
# (summarize_after / keep_recent / summary_max_tokens); these constants remain
# the fallback when no settings are passed.
SUMMARIZE_AFTER = 24        # start summarizing once this many un-summarized messages accumulate
KEEP_RECENT = 12            # keep this many recent turns verbatim in the live prompt
SUMMARY_MAX_TOKENS = 400    # token budget for the summary pass


# Cross-session long-term memory (MEMORY_PLAN Phase 3).
LONGTERM_MAX_FACTS = 40       # hard cap on stored facts — this store must not grow forever
LONGTERM_BUDGET_CHARS = 2400  # ceiling on the injected block, at 8192 context
LONGTERM_SUMMARY_SHARE = 0.6  # of that budget, the share the cumulative history may take


def needs_summary(session: ChatSession, summarize_after: int = SUMMARIZE_AFTER) -> bool:
    unsummarized = len(session.messages) - session.memory.last_summarized_index
    return unsummarized >= summarize_after


def _transcript(messages, char_name: str, user_name: str) -> str:
    """Speaker-labelled transcript of `messages`, macros already substituted."""
    return "\n".join(
        f"{char_name if m.role == 'char' else user_name}: "
        f"{substitute_macros(m.content, char_name, user_name)}"
        for m in messages if m.role != "system"
    )


async def summarize(kobold: KoboldClient, session: ChatSession,
                    card: CharacterCard, persona: Persona,
                    summarize_after: int = SUMMARIZE_AFTER,
                    keep_recent: int = KEEP_RECENT,
                    max_tokens: int = SUMMARY_MAX_TOKENS) -> None:
    """Fold older turns into session.memory.summary (mutates session).

    Thresholds/cap default to the module constants but can be overridden from
    per-user settings. `keep_recent` is capped below `summarize_after` so a fold
    always covers at least one turn."""
    keep_recent = min(keep_recent, summarize_after - 1)
    if not needs_summary(session, summarize_after):
        return

    char_name = card.name or "Companion"
    user_name = persona.name or "User"
    fold_end = len(session.messages) - keep_recent
    if fold_end <= session.memory.last_summarized_index:
        return

    to_fold = session.messages[session.memory.last_summarized_index:fold_end]
    transcript = _transcript(to_fold, char_name, user_name)

    prev = session.memory.summary
    prompt = (
        "Summarize the roleplay so far into a concise memory (a few paragraphs). "
        "Preserve key facts, relationship developments, promises, and unresolved threads. "
        "Write in third person, present tense.\n\n"
        + (f"Existing memory:\n{prev}\n\n" if prev else "")
        + f"New events to fold in:\n{transcript}\n\nUpdated memory:"
    )

    try:
        summary = await kobold.generate_answer(prompt, {
            "max_length": max_tokens, "temperature": 0.5, "top_p": 0.9, "rep_pen": 1.05})
    except Exception:
        return  # summarization is best-effort; never block the chat

    if summary.strip():
        session.memory.summary = summary.strip()
        session.memory.last_summarized_index = fold_end


# ---------------------------------------------------------------------------
# Cross-session long-term memory (per character × persona)
# ---------------------------------------------------------------------------
# `summarize()` above keeps ONE chat inside its context budget. This keeps the
# *pairing* coherent across chats: without it a new session starts blank and she
# has forgotten everything, which is the single biggest continuity gap in the app.
#
# Two triggers, both routed through fold_into_longterm():
#   - threshold — the chat flow folds forward whatever summarize() just folded out
#     of the verbatim window, so nothing leaves the live prompt without being
#     recorded somewhere durable first;
#   - idle — POST /api/sessions/{id}/sweep captures a *finished* conversation
#     rather than cutting at an arbitrary turn boundary mid-scene.
# Both are best-effort and must never block or fail a chat turn.

LONGTERM_PROMPT = (
    "You maintain a durable memory of the relationship between {char} and {user} "
    "across all of their conversations.\n\n"
    "Update it with the new events below. Keep concrete, lasting things: facts about "
    "{user}, decisions, promises, shared history, how they feel about each other, "
    "unresolved threads. Drop small talk and moment-to-moment scene detail — this is "
    "what {char} would still remember weeks later, not a transcript.\n"
    "Write in third person, present tense, a few paragraphs at most."
)


async def fold_into_longterm(kobold: KoboldClient, mem: CharacterMemory,
                             session: ChatSession, card: CharacterCard, persona: Persona,
                             upto: int | None = None,
                             max_tokens: int = 500) -> bool:
    """Fold this session's turns up to `upto` into `mem` (mutates it).

    Returns True if `mem` changed and the caller should persist it. Idempotent:
    `mem.swept_sessions[session.id]` is the high-water mark already folded in, so
    the threshold and idle triggers can both fire without double-counting.

    `upto` defaults to the whole session (the idle sweep). The chat flow passes
    `session.memory.last_summarized_index` so long-term memory absorbs exactly
    what the per-session summary just absorbed, and recent verbatim turns aren't
    recorded twice while they're still in the live prompt.
    """
    start = mem.swept_sessions.get(session.id, 0)
    upto = len(session.messages) if upto is None else upto
    if upto <= start:
        return False

    char_name = card.name or "Companion"
    user_name = persona.name or "User"
    transcript = _transcript(session.messages[start:upto], char_name, user_name)
    if not transcript.strip():
        # Nothing foldable in the range (system-only turns) — still advance the
        # mark so the same empty range isn't rescanned on every later sweep.
        mem.swept_sessions[session.id] = upto
        mem.updated_at = datetime.now().isoformat()
        return True

    prompt = (
        LONGTERM_PROMPT.format(char=char_name, user=user_name) + "\n\n"
        + (f"Existing memory:\n{mem.summary}\n\n" if mem.summary else "")
        + f"New events:\n{transcript}\n\nUpdated memory:"
    )

    try:
        summary = await kobold.generate_answer(prompt, {
            "max_length": max_tokens, "temperature": 0.5, "top_p": 0.9, "rep_pen": 1.05})
    except Exception:
        return False  # best-effort; never block or fail the chat turn

    if not summary.strip():
        return False

    mem.summary = summary.strip()
    mem.swept_sessions[session.id] = upto
    mem.updated_at = datetime.now().isoformat()
    return True


def longterm_block(mem: CharacterMemory | None, char_name: str, user_name: str,
                   budget_chars: int = LONGTERM_BUDGET_CHARS) -> str:
    """Render `mem` as a system-prompt block, or "" if there's nothing to say.

    Budget handling is deliberate: the cumulative history is TRUNCATED to its
    share and facts are dropped OLDEST-FIRST, so an oversized entry can only cost
    itself. (The obvious alternative — bail out on the first thing that doesn't
    fit — silently drops everything after it, with no error and no log line.)
    """
    if mem is None:
        return ""

    facts = [f for f in mem.facts if f.text and f.text.strip()]
    summary = (mem.summary or "").strip()
    if not facts and not summary:
        return ""

    # The fixed header/label text counts against the budget too — otherwise
    # `budget_chars` quietly understates what actually lands in the prompt.
    header = f"What {char_name} remembers about {user_name} from previous conversations:"
    budget = max(0, budget_chars - len(header) - (len("Earlier history:\n") if summary else 0))

    if summary:
        cap = int(budget * LONGTERM_SUMMARY_SHARE)
        if len(summary) > cap:
            summary = summary[:cap].rsplit(" ", 1)[0].rstrip(",.;:") + " …"

    remaining = budget - len(summary)
    kept: list[str] = []
    for f in reversed(facts):                     # newest first; oldest are the ones dropped
        line = f"- {f.text.strip()}"
        if len(line) + 1 > remaining:
            break
        kept.append(line)
        remaining -= len(line) + 1
    kept.reverse()                                # back to the order the user wrote them

    parts = [header]
    if kept:
        parts.append("\n".join(kept))
    if summary:
        parts.append(f"Earlier history:\n{summary}")
    return "\n".join(parts) if (kept or summary) else ""
