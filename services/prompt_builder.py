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
from datetime import datetime
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
    "and voice — opinions, wit, knowledge, tone.\n"
    # "Keep *action* narration minimal or absent" licensed exactly what it was meant to
    # stop — "minimal" is permission — and "focus on what {{char}} thinks and says" invited
    # narrated inner monologue. Assistants were replying with gestures, described
    # expressions, and third-person description of themselves. Forbidden outright now,
    # with the specific forms named: a small model obeys a named form far better than a
    # general prohibition.
    "Write ONLY the words {{char}} says out loud. Nothing else: no action narration, no "
    "gestures, no asterisks or stage directions, no description of {{char}}'s expression, "
    "posture, or surroundings, and no narrated thoughts or feelings. Never write about "
    "{{char}} in the third person. Feeling belongs in word choice, never in a sentence that "
    "describes the feeling. Write {{char}}'s single next reply and then stop; be reasonably "
    "concise and reactive to what {{user}} just said. Do NOT repeat phrasing you have "
    "already used. Never speak, act, or decide for {{user}}. Stay fully in character and "
    "never mention being an AI.\n"
    # Assistant replies are spoken aloud (per-character TTS voice), and the answer that reads
    # best is not the answer that HEARS best. Asked for Dunbar's number, the 9B fine-tune
    # produced a markdown table, ran out of tokens mid-row, and the auto-continue restarted
    # the whole table — output that is unreadable by ear twice over. Steering only; the
    # markdown that gets through anyway is handled in cleanForTTS (app.js).
    "Everything you say is SPOKEN ALOUD, so compose for the ear, not the eye. Plain spoken "
    "prose only: no markdown, no tables, no bullet points, no numbered lists, no headings, no "
    "code blocks, and no asterisks or underscores for emphasis. When something has parts, "
    "levels, or steps, say them in sentences the way you would out loud — \"about five at the "
    "innermost, then roughly fifteen, then fifty\" — never laid out visually. Say numbers and "
    "symbols as words a listener can follow (\"about a hundred and fifty\", not \"~150\"). A "
    "listener cannot skim or re-read, so lead with the answer, keep it short, and stop; offer "
    "the detail and let {{user}} ask for it rather than delivering it unprompted."
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


# The system block loses to the transcript. An assistant card whose history already
# contains narrated replies ("\"Sir,\" he says, tilting his head...") keeps producing
# them however firmly the system prompt forbids it: eighteen demonstrated examples in
# the live window outweigh one rule many turns back. This is the same recency finding
# that put boundary_note last for roleplay cards — and for an assistant that slot is
# now free, so it carries this instead. The last line matters most: the transcript's
# style has to be named as a habit to ignore, or the model reads it as the format.
ASSISTANT_FORMAT_NOTE = (
    "[Reply format] Speak only, in the first person. Write the words {char} says aloud "
    "and nothing else: no narration, no actions or gestures, no description of {char} "
    "or the surroundings, no \"he says\" attributions, and no quotation marks around "
    "{char}'s own words. If earlier replies above are written in a narrated style, do "
    "NOT copy them — that is the transcript's habit, not an instruction."
)


# Inline expression beats. Kept short and shown by example — small models copy a
# demonstrated shape far more reliably than they follow a described one. Static per
# card, so it lives in the system block where KoboldCPP can keep it in the cached
# prefix; a per-turn string here would invalidate the cache every turn.
def expression_note(labels: List[str], assistant: bool = False) -> str:
    if not labels:
        return ""
    # The example must use a label the card actually has. Demonstrating one that is
    # not in the valid list invites the model to copy it, and we would drop the beat.
    ok = sorted(labels)
    preferred = (("thoughtful", "neutral", "happy", "joy", "surprised", "surprise")
                 if assistant else
                 ("embarrassment", "embarrassed", "surprise", "surprised",
                  "sad", "sadness", "happy", "joy"))
    eg = next((l for l in preferred if l in labels), ok[0])

    # Same mechanism, different demonstration. The roleplay example below is a
    # third-person action beat plus quoted dialogue ("She looked away. [expr:x] ..."),
    # and per the note above, a demonstrated shape gets copied far more reliably than a
    # described rule is followed — so on an assistant card it was teaching precisely the
    # narration the assistant system prompt forbids. Shown inside plain first-person
    # speech instead. Beats still have to work here: an assistant with a VRM drives its
    # face from them.
    if assistant:
        return (
            "Expression beats: when your tone genuinely changes mid-reply, mark it inline "
            "at that exact point, then carry on speaking normally.\n"
            f"Valid labels — use one of these exactly, never invent one: {', '.join(ok)}.\n"
            f"Example: I did look into that. [expr:{eg}] It is not as simple as it sounds.\n"
            "Use at most a few per reply, only on a real change. The tag is the ONLY thing "
            "that marks the change — never narrate or describe yourself alongside it. The "
            "tags are stripped before the reader sees them, so never mention or explain them."
        )
    # Never spell out a placeholder tag here. An earlier wording said 'emit a tag
    # [expr:LABEL]' and the model copied it verbatim — it read LABEL as the label.
    # The shape is taught by example only; the list is the sole source of labels.
    return (
        "Expression beats: when your feeling visibly changes mid-reply, mark it inline "
        "at that exact point, then carry on writing normally.\n"
        f"Valid labels — use one of these exactly, never invent one: {', '.join(ok)}.\n"
        f'Example: She looked away. [expr:{eg}] "That is not what I meant."\n'
        "Use at most a few per reply, only on a real change. The tags are stripped "
        "before the reader sees them, so never mention or explain them."
    )


def build_messages(card: CharacterCard, persona: Persona, session: ChatSession,
                   relationship_note: str = "", stage_rank: int = 99,
                   boundary_note: str = "", longterm_note: str = "",
                   expr_labels: List[str] | None = None) -> List[Dict]:
    char_name = card.name or "Companion"
    user_name = persona.name or "User"
    assistant = getattr(card, "chat_mode", "roleplay") == "assistant"

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

    # memory, widest scope first: pinned facts (this chat, always-on) -> cross-session
    # history for this pairing -> this chat's rolling summary. Session-scoped material
    # sits closest to the history it precedes; `longterm_note` arrives pre-rendered with
    # real names (same convention as relationship_note) so it isn't re-substituted here.
    mem = session.memory
    if mem.pinned:
        sys_parts.append("Important facts:\n" + "\n".join(f"- {sub(p)}" for p in mem.pinned))
    if longterm_note:
        sys_parts.append(longterm_note)
    if mem.summary:
        # "Story so far" is scene framing, and an assistant has no story. The block
        # itself is written by memory.summarize(), which picks its prompt the same way.
        sys_parts.append(("Notes from earlier in this conversation:\n" if assistant
                          else "Story so far:\n") + sub(mem.summary))

    messages: List[Dict] = [{"role": "system", "content": "\n\n".join(p for p in sys_parts if p.strip())}]

    # --- chat history (skip messages already folded into the summary) ---
    for m in session.messages[mem.last_summarized_index:]:
        if m.role == "system":
            continue
        role = "assistant" if m.role == "char" else "user"
        content = m.swipes[m.swipe_index] if (m.role == "char" and m.swipes) else m.content
        messages.append({"role": role, "content": sub(content)})

    # --- post-history instructions (jailbreak / UJB) go after the history ---
    if card.post_history_instructions:
        messages.append({"role": "system", "content": sub(card.post_history_instructions)})

    # --- current date/time, for assistant-mode characters only ---
    # Nothing else in the prompt carries the date, so asked the time the model simply
    # invented one ("Monday, October 24 ... 9:15 AM"). Gated on assistant mode: a roleplay
    # character announcing the real-world date breaks the scene.
    # Placed AFTER the history, not in the system block, for two reasons: this string
    # changes every minute, and KoboldCPP only reuses its cached prefix up to the first
    # changed token — in the system block that would re-process the entire history every
    # turn. Trailing also puts it where small models weight it most, the same finding that
    # drove boundary_note's placement.
    if assistant:
        now = datetime.now().astimezone()
        hour = now.strftime("%I").lstrip("0") or "12"
        messages.append({"role": "system", "content": (
            f"Current date and time: {now.strftime('%A, %B')} {now.day}, {now.year}, "
            f"{hour}:{now.strftime('%M %p')} {now.strftime('%Z')}. "
            f"This is authoritative — use it when asked about the date or time, never guess.")})

    # --- expression beats: trailing, for the same recency reason as boundary_note ---
    # Measured, not assumed. With the instruction in the system block the 9B tagged 1 of
    # 6 replies (and invented a label); moved here it tagged 3 of 6 with none invalid.
    # The system block is many turns back by mid-conversation and the trailing boundary
    # reminder was out-weighting it. Placed before boundary_note so that still goes last.
    expr_hint = expression_note(expr_labels or [], assistant=assistant)
    if expr_hint:
        messages.append({"role": "system", "content": expr_hint})

    # --- assistant format reminder goes LAST, the slot boundary_note holds for
    # roleplay cards. Nothing else in the prompt sits after the history, which is
    # where the narration is being learned from.
    if assistant:
        messages.append({"role": "system",
                         "content": ASSISTANT_FORMAT_NOTE.format(char=char_name)})

    # --- physical boundary goes LAST, after any UJB ---
    # The full relationship descriptor is in the system message, but by mid-conversation
    # that is many turns back and small models weight recent tokens far more heavily.
    # Deliberately placed after post_history_instructions so a card's UJB cannot bury it.
    if boundary_note:
        messages.append({"role": "system", "content": sub(boundary_note)})

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
