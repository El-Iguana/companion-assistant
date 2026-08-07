"""Long-term memory: rolling summarization of older turns.

When the running history grows past a threshold, the oldest un-summarized turns
are folded into `session.memory.summary` so the prompt stays within budget while
preserving continuity. Uses the same KoboldCPP backend for the summarization pass.
"""
from __future__ import annotations

from models.schema import ChatSession, CharacterCard, Persona
from services.kobold import KoboldClient
from services.prompt_builder import substitute_macros

# Start summarizing once this many un-summarized messages accumulate,
# and keep this many recent turns verbatim in the live prompt.
SUMMARIZE_AFTER = 24
KEEP_RECENT = 12


def needs_summary(session: ChatSession) -> bool:
    unsummarized = len(session.messages) - session.memory.last_summarized_index
    return unsummarized >= SUMMARIZE_AFTER


async def summarize(kobold: KoboldClient, session: ChatSession,
                    card: CharacterCard, persona: Persona) -> None:
    """Fold older turns into session.memory.summary (mutates session)."""
    if not needs_summary(session):
        return

    char_name = card.name or "Companion"
    user_name = persona.name or "User"
    fold_end = len(session.messages) - KEEP_RECENT
    if fold_end <= session.memory.last_summarized_index:
        return

    to_fold = session.messages[session.memory.last_summarized_index:fold_end]
    transcript = "\n".join(
        f"{char_name if m.role == 'char' else user_name}: "
        f"{substitute_macros(m.content, char_name, user_name)}"
        for m in to_fold if m.role != "system"
    )

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
            "max_length": 400, "temperature": 0.5, "top_p": 0.9, "rep_pen": 1.05})
    except Exception:
        return  # summarization is best-effort; never block the chat

    if summary.strip():
        session.memory.summary = summary.strip()
        session.memory.last_summarized_index = fold_end
