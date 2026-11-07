"""Assistant prompt construction.

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
from services import context


# The built-in system prompt. Personality-forward conversation with no invented
# setting or scene; a card's own `system_prompt` replaces it.
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
    # markdown that gets through anyway is handled in cleanForTTS (static/js/voice.js).
    "Everything you say is SPOKEN ALOUD, so compose for the ear, not the eye. Plain spoken "
    "prose only: no markdown, no tables, no bullet points, no numbered lists, no headings, no "
    "code blocks, and no asterisks or underscores for emphasis. When something has parts, "
    "levels, or steps, say them in sentences the way you would out loud — \"about five at the "
    "innermost, then roughly fifteen, then fifty\" — never laid out visually. Say numbers and "
    "symbols as words a listener can follow (\"about a hundred and fifty\", not \"~150\"). A "
    "listener cannot skim or re-read, so lead with the answer, keep it short, and stop; offer "
    "the detail and let {{user}} ask for it rather than delivering it unprompted."
)

# Courtesy questions an assistant tacks onto a finished answer. The prompt rule
# above moved this from 9 replies in 15 to 8: a strong conversational habit does
# not yield to being told, which is the same finding as the narration fix.
#
# So a trailing question is dropped in code — but ONLY these forms, and only when
# it is the last sentence and something else was actually said. A question is
# legitimate when the assistant cannot act without the answer, and no regex can
# tell that apart; what it CAN tell is "is there anything else?", which is never
# clarification. Anything not on this list survives.
_COURTESY_Q = re.compile(
    r"(?:(?<=[.!?])\s+|^)[^.!?]*?\b("
    r"anything (?:else|further|more)|is there (?:anything|something)|"
    r"how (?:may|can|might) i (?:assist|help|be of)|what (?:else )?can i (?:do|help)|"
    r"would you (?:like|care|prefer)|shall i|do you (?:need|want|require) anything|"
    r"how (?:is|are|was|has) (?:your|you)|and yourself|"
    r"anything (?:i can|you would like)"
    r")\b[^.!?]*\?\s*$", re.I)


# The declarative half of the same habit: "I shall remain at your command should
# you require anything further." Not a question, so the strip below never sees it.
# Narrower list than the questions on purpose — a declarative sentence carries
# real content far more often than "anything else?" does, so only these fixed
# courtesy formulas go, and only as the final sentence.
_CLOSER = re.compile(
    r"(?:(?<=[.!?])\s+|^)[^.!?]*?\b("
    r"at your (?:command|disposal|service)|should you (?:require|need)|"
    r"do not hesitate|don't hesitate|stand(?:ing)? by (?:for|to|until)|"
    r"here (?:if|whenever|should) you need|remain (?:at|available|on hand)|"
    r"call (?:out |upon me |on me )?(?:if|should|whenever)"
    # Terminator optional: a reply cut off at max_tokens ends mid-sentence, and the
    # one closer that survived 25 measured replies was exactly that — "There is
    # further information available should you require it" with no full stop.
    r")\b[^.!?]*[.!?]?\s*$", re.I)


def strip_closing_offer(text: str) -> str:
    """Drop a trailing courtesy sign-off. Same guard as the question strip: if it
    is the whole reply, keep it — an assistant whose entire answer was a sign-off
    has nothing left to say, and an empty turn is worse than a florid one."""
    if not text:
        return text
    trimmed = _CLOSER.sub("", text).strip()
    return trimmed if trimmed else text


def strip_initiative_question(text: str) -> str:
    """Drop a trailing courtesy question from an assistant reply.

    Returns `text` unchanged when the question is the whole reply — an assistant
    that genuinely only asked something is either clarifying or has nothing else
    to say, and deleting that leaves an empty turn.
    """
    if not text or "?" not in text:
        return text
    trimmed = _COURTESY_Q.sub("", text).strip()
    return trimmed if trimmed else text


def substitute_macros(text: str, char_name: str, user_name: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\{\{char\}\}", char_name, text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{user\}\}", user_name, text, flags=re.IGNORECASE)
    return text


# ---------------------------------------------------------------------------
# Lorebook / character_book scanning
# ---------------------------------------------------------------------------
def select_lorebook(book: CharacterBook | None, recent_text: str) -> tuple[list[str], list[str]]:
    """Return (before_char_blocks, after_char_blocks) of triggered entries."""
    if not book:
        return [], []
    hay = recent_text if book.entries and book.entries[0].case_sensitive else recent_text.lower()
    before: list[tuple[int, str]] = []
    after: list[tuple[int, str]] = []

    for e in book.entries:
        if not e.enabled or not e.content:
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
        # A card's scenario field, imported from a roleplay card, is background here:
        # the assistant prompt has no scene, so it must not read as one to act out.
        parts.append("Background: " + substitute_macros(card.scenario, char_name, user_name))
    return "\n\n".join(p for p in parts if p.strip())


# The system block loses to the transcript. An assistant card whose history already
# contains narrated replies ("\"Sir,\" he says, tilting his head...") keeps producing
# them however firmly the system prompt forbids it: eighteen demonstrated examples in
# the live window outweigh one rule many turns back. This is the same recency finding
# the recency finding that puts trailing notes after the history. The last line matters most: the transcript's
# style has to be named as a habit to ignore, or the model reads it as the format.
ASSISTANT_FORMAT_NOTE = (
    "[Reply format] Speak only, in the first person. Write the words {char} says aloud "
    "and nothing else: no narration, no actions or gestures, no description of {char} "
    "or the surroundings, no \"he says\" attributions, and no quotation marks around "
    "{char}'s own words. If earlier replies above are written in a narrated style, do "
    "NOT copy them — that is the transcript's habit, not an instruction.\n"
    # Measured 2/15 clean before this paragraph: 9 replies in 15 ended by asking the
    # user something, and 3 closed with an offer of further help. An assistant that
    # volleys a question back on every turn is making conversation, which is not the
    # job. Questions are for clarification only — the test is whether the assistant
    # can act without the answer.
    "Answer what was asked and stop. No closing offer of further help, no \"at your "
    "disposal\", no \"anything else?\" — {user} will ask if there is more. Ask a "
    "question ONLY when you cannot carry out the request without the answer; never "
    "to be polite, to offer more, or to keep the conversation going. You have no "
    "body, senses, hardware or location, so never mention temperature, rooms, "
    "lighting, fans, circuits or how anything feels."
)


# Inline expression beats. Kept short and shown by example — small models copy a
# demonstrated shape far more reliably than they follow a described one. Static per
# card, so it lives in the system block where KoboldCPP can keep it in the cached
# prefix; a per-turn string here would invalidate the cache every turn.
def expression_note(labels: List[str]) -> str:
    if not labels:
        return ""
    # The example must use a label the card actually has. Demonstrating one that is
    # not in the valid list invites the model to copy it, and we would drop the beat.
    # Shown inside plain first-person speech: a demonstrated shape gets copied far more
    # reliably than a described rule is followed, and an action-beat example taught the
    # narration the system prompt forbids. Beats drive the sprite or the VRM face.
    # Never spell out a placeholder tag ('[expr:LABEL]'): the model copied it verbatim.
    ok = sorted(labels)
    preferred = ("thoughtful", "neutral", "happy", "joy", "surprised", "surprise")
    eg = next((l for l in preferred if l in labels), ok[0])
    return (
        "Expression beats: when your tone genuinely changes mid-reply, mark it inline "
        "at that exact point, then carry on speaking normally.\n"
        f"Valid labels — use one of these exactly, never invent one: {', '.join(ok)}.\n"
        f"Example: I did look into that. [expr:{eg}] It is not as simple as it sounds.\n"
        "Use at most a few per reply, only on a real change. The tag is the ONLY thing "
        "that marks the change — never narrate or describe yourself alongside it. Always "
        "write the whole tag, brackets and expr: included; a bare label word on its own is "
        "never valid. The tags are stripped before the reader sees them, so never mention "
        "or explain them."
    )


# A question about the clock or calendar. Only then does the exact time trail the
# prompt; otherwise the system block's date line is all the model gets.
_ASKS_TIME = re.compile(
    r"\b(time|date|day|today|tonight|tomorrow|yesterday|clock|hour|o'?clock|"
    r"week|weekend|month|year|morning|afternoon|evening)\b.*\?|"
    r"\b(what time|what day|what date|what's the date|whats the date|today's date|"
    r"the time|the date)\b", re.I)

_FOCUS_QUOTE_CHARS = 200


RETAKE_MAX = 2            # earlier takes quoted
RETAKE_CHARS = 280        # of each


def retake_note(takes: List[str], char: str, user: str) -> str:
    """Ask for a reply that differs from the earlier take(s) the user set aside."""
    quoted = []
    for t in [x for x in takes if x and x.strip()][-RETAKE_MAX:]:
        t = " ".join(t.split())
        quoted.append('"' + (t if len(t) <= RETAKE_CHARS
                             else t[:RETAKE_CHARS].rsplit(" ", 1)[0] + " …") + '"')
    if not quoted:
        return ""
    return (f"This is another take on {char}'s reply; {user} set aside "
            + ("this one: " if len(quoted) == 1 else "these: ") + " / ".join(quoted)
            + " Write a clearly different reply: a different opening and different words, "
            "not a rephrasing of it.")


def focus_note(last_user: str, char: str, user: str) -> str:
    """The closing line of every prompt: answer THIS, from where things stand now.

    Quotes the message back (shortened) because a small model's attention fades
    across the rules between the history and the reply; one short quote right at
    the end puts the moment back in front of it."""
    q = " ".join(last_user.split())
    if len(q) > _FOCUS_QUOTE_CHARS:
        q = q[:_FOCUS_QUOTE_CHARS].rsplit(" ", 1)[0] + " …"
    return (
        f"[Now] Reply to {user}'s latest message: \"{q}\"\n"
        f"Respond to exactly that, in the context of the conversation so far. Do not "
        f"repeat an earlier reply of yours or bring up topics {user} did not raise, and "
        f"do not announce the date or time unless asked — even if earlier replies did. "
        f"If it is a short answer like \"yes\" or \"go on\", act on what was just "
        f"being discussed.")


def speaker(m, char: str, user: str) -> str:
    return char if m.role == "char" else user


# Orpheus voices perform these instead of reading them (services/orpheus.py). Offered
# only to cards whose voice is Orpheus; the chat view hides them, Kokoro strips them.
# A subset of what Orpheus knows: cough/sniffle/yawn read as noise in conversation.
VOICE_TAGS = ("laugh", "chuckle", "sigh", "gasp", "groan")


def voice_tag_note(card) -> str:
    """Tell an Orpheus-voiced card it may write sound tags. Shown by example, like the
    expression beats: a small model copies a demonstrated shape far more reliably than
    it follows a described one. Trailing, after ASSISTANT_FORMAT_NOTE, because that note
    forbids stage directions and the later instruction is the one that wins."""
    if not str(getattr(card, "tts_voice", "") or "").startswith("orpheus:"):
        return ""
    return (
        "Voice tags: your replies are spoken by a voice that can really laugh, chuckle, "
        "sigh, gasp and groan. React with one when the moment calls for it — something "
        "funny or absurd: <laugh> or <chuckle>; a setback or something tiresome: <sigh> or "
        "<groan>; a real surprise: <gasp>. Write the tag where the sound happens, usually "
        "at the very start of the reply.\n"
        "Example: <chuckle> That is exactly what I said. You just did not believe me.\n"
        "At most one per reply; plain questions and requests need none. Use only these five "
        "tags, written exactly so. They are the one allowed exception to having no stage "
        "directions; never describe a sound in words instead (no \"haha\", no *laughs*). "
        "They are performed, not shown, so never mention them."
    )


def build_messages(card: CharacterCard, persona: Persona, session: ChatSession,
                   longterm_note: str = "",
                   expr_labels: List[str] | None = None,
                   context_tokens: int = 0, trimmed: list | None = None,
                   retake_of: List[str] | None = None) -> List[Dict]:
    """The chat prompt. With `context_tokens`, the oldest history turns are dropped
    until the whole prompt plus the reply fits that window (services/context.py);
    the count dropped is appended to `trimmed` when one is passed."""
    char_name = card.name or "Assistant"
    user_name = persona.name or "User"

    def sub(t: str) -> str:
        return substitute_macros(t, char_name, user_name)

    # Scan recent turns for lorebook triggers
    scan_depth = card.character_book.scan_depth if card.character_book else 4
    recent = "\n".join(m.content for m in session.messages[-scan_depth:]) if session.messages else ""
    lb_before, lb_after = select_lorebook(card.character_book, recent)

    # --- system message ---
    sys_parts: list[str] = [sub(card.system_prompt or DEFAULT_ASSISTANT)]
    persona_bits = [b for b in (getattr(persona, "title", ""), persona.description,
                                getattr(persona, "background", "")) if b and b.strip()]
    if persona_bits:
        sys_parts.append(f"About {user_name}: " + sub(". ".join(persona_bits)))
    if lb_before:
        sys_parts.append("\n".join(sub(b) for b in lb_before))
    char_def = _char_block(card, char_name, user_name)
    if char_def:
        sys_parts.append(char_def)
    if lb_after:
        sys_parts.append("\n".join(sub(b) for b in lb_after))
    if card.mes_example:
        sys_parts.append("Example dialogue:\n" + sub(card.mes_example))

    # memory, widest scope first: pinned facts (this chat, always-on) -> the pairing's
    # assistant memory. `longterm_note` arrives pre-rendered with real names, so it
    # isn't re-substituted here. `session.memory.summary` is never injected: nothing
    # writes it in this app, and a session copied over from the roleplay app may carry
    # a narrative one, which is exactly the "worked example of narrating" the assistant
    # pipeline exists to keep out (assistant_memory.fold clears it).
    mem = session.memory
    if mem.pinned:
        sys_parts.append("Important facts:\n" + "\n".join(f"- {sub(p)}" for p in mem.pinned))
    if longterm_note:
        sys_parts.append(longterm_note)

    # The DATE here, not the time. It changes once a day, so it costs one cache miss a
    # day instead of one a minute. The minute-accurate time used to trail every prompt,
    # and sitting last it was the loudest thing in the prompt: replies volunteered the
    # clock unasked. The exact time now trails only when asked for.
    today = datetime.now().astimezone()
    sys_parts.append(
        f"Today is {today.strftime('%A, %B')} {today.day}, {today.year}. You are not "
        f"told the time of day, so never state or guess one. Mention the date only "
        f"when {user_name} asks for it.")

    messages: List[Dict] = [{"role": "system", "content": "\n\n".join(p for p in sys_parts if p.strip())}]

    # --- chat history (skip messages already folded into memory) ---
    # "narrator" messages exist only in sessions copied from the roleplay app; they are
    # scene events with no place in an assistant conversation, so they are left out.
    history: List[Dict] = []
    for m in session.messages[mem.last_summarized_index:]:
        if m.role not in ("char", "user"):
            continue
        role = "assistant" if m.role == "char" else "user"
        content = m.swipes[m.swipe_index] if (m.role == "char" and m.swipes) else m.content
        history.append({"role": role, "content": sub(content)})

    # --- post-history instructions (jailbreak / UJB) go after the history ---
    after: List[Dict] = []
    if card.post_history_instructions:
        after.append({"role": "system", "content": sub(card.post_history_instructions)})

    # --- ONE trailing note: everything that must sit after the history ---
    # These used to be separate system turns after {{user}}'s message (time, expression
    # rules, format reminder), so the last thing the model read before replying was a
    # wall of rules rather than what was just said — and it answered the rules. Merged
    # into one message that ENDS by pointing back at the latest message.
    last_user = next((m.content for m in reversed(session.messages)
                      if m.role == "user" and m.content and m.content.strip()), "")
    tail: list[str] = []
    if _ASKS_TIME.search(last_user):
        now = datetime.now().astimezone()
        hour = now.strftime("%I").lstrip("0") or "12"
        tail.append(
            f"Current date and time: {now.strftime('%A, %B')} {now.day}, {now.year}, "
            f"{hour}:{now.strftime('%M %p')} {now.strftime('%Z')}. "
            f"This is authoritative — use it, never guess.")
    expr_hint = expression_note(expr_labels or [])
    if expr_hint:
        tail.append(expr_hint)
    tail.append(ASSISTANT_FORMAT_NOTE.format(char=char_name, user=user_name))
    voice_hint = voice_tag_note(card)
    if voice_hint:
        tail.append(voice_hint)
    if retake_of:
        # "Another take": the take being replaced is no longer in the history, so the
        # model had nothing to differ from and wrote much the same reply again. Quoting
        # it also lets the DRY sampler penalise reusing its wording.
        tail.append(retake_note(retake_of, char_name, user_name))
    if last_user:
        tail.append(focus_note(sub(last_user), char_name, user_name))
    after.append({"role": "system", "content": "\n\n".join(tail)})

    # --- fit the window: drop the oldest verbatim turns, never the system block ---
    # Overflow is otherwise silent and cuts from the FRONT (PROMPT_BUDGET.md), which
    # deletes the persona and memory first. Dropping old turns instead loses only what
    # the memory fold is about to absorb anyway.
    if context_tokens:
        history, dropped = context.fit_history(
            messages + after, history, context_tokens,
            context.reply_reserve(session.generation.max_tokens))
        if trimmed is not None:
            trimmed.append(dropped)

    return messages + history + after


def kobold_params(session: ChatSession) -> dict:
    g = session.generation
    return {
        "max_tokens": g.max_tokens,
        "temperature": g.temperature,
        "top_p": g.top_p,
        "top_k": g.top_k,
        "rep_pen": g.rep_pen,
        "min_p": g.min_p,
        "dry_multiplier": g.dry_multiplier,
        "dry_base": g.dry_base,
        "dry_allowed_length": g.dry_allowed_length,
        "dry_penalty_last_n": g.dry_penalty_last_n,
        "stop": g.stop_sequences or None,
    }
