"""Companion prompt construction.

Builds the OpenAI-style `messages` list consumed by
KoboldClient.chat_generate_stream(). Handles:
  - {{char}} / {{user}} macro substitution
  - system prompt = character definition + persona + memory + lorebook
  - lorebook (character_book) key scanning against recent turns
  - post_history_instructions injected AFTER the chat history
"""
from __future__ import annotations

import re
from typing import List, Dict

from models.schema import CharacterCard, Persona, ChatSession, CharacterBook


# Scene/world roleplay (the historical default).
DEFAULT_ROLEPLAY = (
    "You are {{char}} in an immersive, ongoing roleplay with {{user}}. "
    "Stay fully in character as {{char}} at all times. Write ONLY {{char}}'s single next reply and "
    "then stop: one focused message that moves the scene forward by one concrete beat. "
    "Do not write multiple exchanges or continue the conversation on both sides, and never speak, "
    "act, narrate, or decide for {{user}}. Keep it reasonably concise and reactive to what {{user}} "
    "just said. Do NOT repeat actions, phrasing, or descriptions you have already used — advance "
    "the moment with something new each time. Never break character or mention being an AI."
)

# Personality-forward conversation — no invented setting/scene (for assistant-type
# characters like a Jarvis). Keeps the same anti-repetition / one-reply / stay-in-
# character guardrails, but drops the "advance the scene" framing that makes small
# models improvise a world.
DEFAULT_ASSISTANT = (
    "You are {{char}}, talking directly with {{user}} in a plain, ongoing conversation. "
    "There is no physical setting or scene: do NOT invent surroundings, locations, events, or a "
    "world, and do not narrate a scene. Reply as {{char}} in first person with full personality "
    "and voice — opinions, wit, knowledge, tone; focus on what {{char}} thinks and says. Keep "
    "*action* narration minimal or absent. Write ONLY {{char}}'s single next reply and then stop; "
    "be reasonably concise and reactive to what {{user}} just said. Do NOT repeat phrasing you have "
    "already used. Never speak, act, or decide for {{user}}. Stay fully in character and never "
    "mention being an AI."
)

# Back-compat alias.
DEFAULT_SYSTEM = DEFAULT_ROLEPLAY


def default_system_for(card) -> str:
    """Pick the built-in system prompt for a card's chat_mode."""
    return DEFAULT_ASSISTANT if getattr(card, "chat_mode", "roleplay") == "assistant" else DEFAULT_ROLEPLAY


def substitute_macros(text: str, char_name: str, user_name: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\{\{char\}\}", char_name, text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{user\}\}", user_name, text, flags=re.IGNORECASE)
    return text


# ---------------------------------------------------------------------------
# Lorebook / character_book scanning
# ---------------------------------------------------------------------------
# Relationship stage order (mirrors relationship._STAGE_ORDER) for lorebook gating.
STAGE_ORDER = ["Stranger", "Acquaintance", "Friend", "Close", "Romantic", "Intimate"]


def select_lorebook(book: CharacterBook | None, recent_text: str,
                    stage_rank: int = 99) -> tuple[list[str], list[str]]:
    """Return (before_char_blocks, after_char_blocks) of triggered entries.

    `stage_rank` gates entries: an entry with `min_stage` is skipped until the
    relationship has reached that stage.
    """
    if not book:
        return [], []
    hay = recent_text if book.entries and book.entries[0].case_sensitive else recent_text.lower()
    before: list[tuple[int, str]] = []
    after: list[tuple[int, str]] = []

    for e in book.entries:
        if not e.enabled or not e.content:
            continue
        if e.min_stage:                     # relationship-stage gate
            req = STAGE_ORDER.index(e.min_stage) if e.min_stage in STAGE_ORDER else 0
            if req > stage_rank:
                continue
        triggered = e.constant
        if not triggered:
            keys = e.keys if e.case_sensitive else [k.lower() for k in e.keys]
            triggered = any(k and k in hay for k in keys)
            if triggered and e.selective and e.secondary_keys:
                sk = e.secondary_keys if e.case_sensitive else [k.lower() for k in e.secondary_keys]
                triggered = any(k and k in hay for k in sk)
        if not triggered:
            continue
        bucket = after if e.position == "after_char" else before
        bucket.append((e.insertion_order, e.content))

    before.sort(key=lambda x: x[0])
    after.sort(key=lambda x: x[0])
    return [c for _, c in before], [c for _, c in after]


# ---------------------------------------------------------------------------
# Message assembly
# ---------------------------------------------------------------------------
def _char_block(card: CharacterCard, char_name: str, user_name: str) -> str:
    parts = []
    if card.description:
        parts.append(substitute_macros(card.description, char_name, user_name))
    if card.personality:
        parts.append(f"{char_name}'s personality: " +
                     substitute_macros(card.personality, char_name, user_name))
    if card.scenario:
        parts.append("Scenario: " + substitute_macros(card.scenario, char_name, user_name))
    return "\n\n".join(p for p in parts if p.strip())


def build_messages(card: CharacterCard, persona: Persona, session: ChatSession,
                   relationship_note: str = "", stage_rank: int = 99) -> List[Dict]:
    char_name = card.name or "Companion"
    user_name = persona.name or "User"

    def sub(t: str) -> str:
        return substitute_macros(t, char_name, user_name)

    # Scan recent turns for lorebook triggers
    scan_depth = card.character_book.scan_depth if card.character_book else 4
    recent = "\n".join(m.content for m in session.messages[-scan_depth:]) if session.messages else ""
    lb_before, lb_after = select_lorebook(card.character_book, recent, stage_rank)

    # --- system message ---
    sys_parts: list[str] = [sub(card.system_prompt) if card.system_prompt else sub(default_system_for(card))]
    persona_bits = [b for b in (getattr(persona, "title", ""), persona.description,
                                getattr(persona, "background", "")) if b and b.strip()]
    if persona_bits:
        sys_parts.append(f"About {user_name}: " + sub(". ".join(persona_bits)))
    if relationship_note:
        sys_parts.append(relationship_note)
    if lb_before:
        sys_parts.append("\n".join(sub(b) for b in lb_before))
    char_def = _char_block(card, char_name, user_name)
    if char_def:
        sys_parts.append(char_def)
    if lb_after:
        sys_parts.append("\n".join(sub(b) for b in lb_after))
    if card.mes_example:
        sys_parts.append("Example dialogue:\n" + sub(card.mes_example))

    # long-term memory
    mem = session.memory
    if mem.pinned:
        sys_parts.append("Important facts:\n" + "\n".join(f"- {sub(p)}" for p in mem.pinned))
    if mem.summary:
        sys_parts.append("Story so far:\n" + sub(mem.summary))

    messages: List[Dict] = [{"role": "system", "content": "\n\n".join(p for p in sys_parts if p.strip())}]

    # --- chat history (skip messages already folded into the summary) ---
    for m in session.messages[mem.last_summarized_index:]:
        if m.role == "system":
            continue
        role = "assistant" if m.role == "char" else "user"
        content = m.swipes[m.swipe_index] if (m.role == "char" and m.swipes) else m.content
        messages.append({"role": role, "content": sub(content)})

    # --- post-history instructions (jailbreak / UJB) go LAST ---
    if card.post_history_instructions:
        messages.append({"role": "system", "content": sub(card.post_history_instructions)})

    return messages


def kobold_params(session: ChatSession) -> dict:
    g = session.generation
    return {
        "max_tokens": g.max_tokens,
        "temperature": g.temperature,
        "top_p": g.top_p,
        "top_k": g.top_k,
        "rep_pen": g.rep_pen,
        "min_p": g.min_p,
        "stop": g.stop_sequences or None,
    }
