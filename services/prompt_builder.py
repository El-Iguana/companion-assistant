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

# Back-compat alias.
DEFAULT_SYSTEM = DEFAULT_ROLEPLAY


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
        # The card's scenario is where the story STARTED, and it stays in every
        # prompt. Labelled plainly "Scenario:" it read as the current situation, so
        # long chats kept getting dragged back to the opening scene.
        parts.append("Opening premise (where the story began; the chat may have moved "
                     "on since, so follow where it is now): " +
                     substitute_macros(card.scenario, char_name, user_name))
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
            "that marks the change — never narrate or describe yourself alongside it. Always "
            "write the whole tag, brackets and expr: included; a bare label word on its own is "
            "never valid. The tags are stripped before the reader sees them, so never mention "
            "or explain them."
        )
    # Never spell out a placeholder tag here. An earlier wording said 'emit a tag
    # [expr:LABEL]' and the model copied it verbatim — it read LABEL as the label.
    # The shape is taught by example only; the list is the sole source of labels.
    return (
        "Expression beats: when your feeling visibly changes mid-reply, mark it inline "
        "at that exact point, then carry on writing normally.\n"
        f"Valid labels — use one of these exactly, never invent one: {', '.join(ok)}.\n"
        f'Example: She looked away. [expr:{eg}] "That is not what I meant."\n'
        "Use at most a few per reply, only on a real change. Always write the whole tag, "
        "brackets and expr: included; a bare label word on its own is never valid. The tags "
        "are stripped before the reader sees them, so never mention or explain them."
    )


# A question about the clock or calendar. Only then does the exact time trail the
# prompt; otherwise the system block's date line is all the model gets.
_ASKS_TIME = re.compile(
    r"\b(time|date|day|today|tonight|tomorrow|yesterday|clock|hour|o'?clock|"
    r"week|weekend|month|year|morning|afternoon|evening)\b.*\?|"
    r"\b(what time|what day|what date|what's the date|whats the date|today's date|"
    r"the time|the date)\b", re.I)

_FOCUS_QUOTE_CHARS = 200


def focus_note(last_user: str, char: str, user: str, assistant: bool) -> str:
    """The closing line of every prompt: answer THIS, from where things stand now.

    Quotes the message back (shortened) because a small model's attention fades
    across the rules between the history and the reply; one short quote right at
    the end puts the moment back in front of it. Roleplay and assistant differ
    only in what "drifting" looks like for each."""
    q = " ".join(last_user.split())
    if len(q) > _FOCUS_QUOTE_CHARS:
        q = q[:_FOCUS_QUOTE_CHARS].rsplit(" ", 1)[0] + " …"
    if assistant:
        return (
            f"[Now] Reply to {user}'s latest message: \"{q}\"\n"
            f"Respond to exactly that, in the context of the conversation so far. Do not "
            f"repeat an earlier reply of yours or bring up topics {user} did not raise, and "
            f"do not announce the date or time unless asked — even if earlier replies did. "
            f"If it is a short answer like \"yes\" or \"go on\", act on what was just "
            f"being discussed.")
    return (
        f"[Now] Reply to {user}'s latest message: \"{q}\"\n"
        f"Continue the scene from exactly where it stands at this moment — same place, "
        f"same people, same thread. Do not recap or re-describe what already happened "
        f"or the setting, do not restart the scene, and do not jump to a new topic or "
        f"location unless {user} takes it there. Write {char}'s next beat only."
    )


def roleplay_turn_note(latest, char: str, user: str, sub) -> str:
    """The closing line when {user} didn't speak last: the narrator did, or {char} did and
    is asked to go on (Continue). Same role as focus_note; roleplay only."""
    if latest.role == "narrator":
        q = " ".join(sub(latest.content).split())
        if len(q) > _FOCUS_QUOTE_CHARS:
            q = q[:_FOCUS_QUOTE_CHARS].rsplit(" ", 1)[0] + " …"
        return (
            f"[Now] Something just happened in the scene (narration, not {user}'s words): \"{q}\"\n"
            f"React to it as {char}, in the moment, from where the scene stands — same place, "
            f"same people. Write {char}'s next beat only; do not decide how {user} reacts, "
            f"and do not retell the event.")
    return (
        f"[Now] {user} has not spoken yet — {char} carries the scene on. Take it one beat "
        f"further on {char}'s own initiative: do, say or notice something new that follows "
        f"from {char}'s last message. Do not repeat or restate it, do not answer for {user}, "
        f"and do not wrap the scene up. Write {char}'s next beat only.")


# ---------------------------------------------------------------------------
# Narrator events and drafts of {{user}}'s reply (roleplay only)
#
# Both are their own short prompts rather than build_messages() variants: the narrator is
# not {{char}} and the draft is written as {{user}}, so neither may carry {{char}}'s
# system prompt, relationship framing or boundary note. The scene goes in as a labelled
# transcript in one user turn, so the model writes the requested piece instead of
# continuing the chat as {{char}}.
# ---------------------------------------------------------------------------
NARRATION_LABEL = "[Narrator] "
SCENE_TURNS = 16                 # recent messages the narrator / draft prompt sees

NARRATOR_SYSTEM = (
    "You are the narrator of an ongoing roleplay between {{char}} and {{user}}. You are not "
    "a character in it. Write ONE short narration of something that happens in the scene "
    "right now — something in the surroundings, a sound, a change in light or weather, "
    "someone else arriving, an object, news, an interruption — that gives both of them "
    "something to react to.\n"
    "Keep it neutral: it takes no side, resolves nothing, and does not push the scene toward "
    "romance, intimacy or violence. Third person, present tense, two to four sentences. "
    "Never write dialogue, actions, thoughts, feelings or decisions for {{char}} or {{user}}; "
    "describe only the world and anyone else in it. Stay consistent with the setting and "
    "what has happened. Do not recap, and never mention being a narrator or an AI."
)

DRAFT_SYSTEM = (
    "You write {{user}}'s next message in an ongoing roleplay with {{char}}. Write ONLY "
    "{{user}}'s single next reply, in {{user}}'s own voice — what {{user}} says, and briefly "
    "what {{user}} does — reacting to the latest moment. Never write anything {{char}} says, "
    "does, thinks or feels, and do not narrate the world. Write it the way {{user}}'s own "
    "earlier messages are written — same person, tense and style (first person if there are "
    "none) — and include something {{user}} says out loud. Keep it short: one to three "
    "sentences. No preamble, no name label, no quotation marks around the whole reply."
)


def speaker(m, char: str, user: str) -> str:
    return char if m.role == "char" else "Narrator" if m.role == "narrator" else user


def shown(m) -> str:
    """The text on screen: a reply's selected swipe, else the content."""
    return m.swipes[m.swipe_index] if (m.role == "char" and m.swipes) else m.content


def scene_transcript(session: ChatSession, char: str, user: str, turns: int = SCENE_TURNS) -> str:
    msgs = [m for m in session.messages[session.memory.last_summarized_index:] if m.role != "system"]
    return "\n\n".join(f"{speaker(m, char, user)}: {substitute_macros(shown(m), char, user)}"
                        for m in msgs[-turns:])


def _scene_context(card: CharacterCard, persona: Persona, session: ChatSession) -> str:
    char, user = card.name or "Companion", persona.name or "User"
    parts = []
    block = _char_block(card, char, user)
    if block:
        parts.append(f"About {char}:\n{block}")
    bits = [b for b in (getattr(persona, "title", ""), persona.description,
                        getattr(persona, "background", "")) if b and b.strip()]
    if bits:
        parts.append(f"About {user}: " + substitute_macros(". ".join(bits), char, user))
    if session.memory.summary:
        parts.append("Story so far:\n" + substitute_macros(session.memory.summary, char, user))
    scene = scene_transcript(session, char, user)
    parts.append("The scene, most recent last:\n" + (scene or "(nothing has happened yet)"))
    return "\n\n".join(parts)


def build_narrator_messages(card: CharacterCard, persona: Persona, session: ChatSession,
                            direction: str = "") -> List[Dict]:
    char, user = card.name or "Companion", persona.name or "User"
    what = (f"Narrate this happening next, in your own words: {direction.strip()}. "
            if direction.strip() else "Narrate the next event now. ")
    # Restated last, where it counts: with the scene transcript just above, the model
    # slid into writing the next lines for {{char}} — speech, gestures, shivers — and
    # ran for paragraphs. The system rules alone, many hundred tokens back, didn't hold.
    ask = (what + f"One paragraph, two to four sentences, about the world only: what "
           f"happens, what is seen or heard. Write nothing that {char} or {user} says, does, "
           f"thinks or feels — they react next, not you.")
    return [
        {"role": "system", "content": substitute_macros(NARRATOR_SYSTEM, char, user)},
        {"role": "user", "content": _scene_context(card, persona, session) + f"\n\n[{ask}]"},
    ]


def build_draft_messages(card: CharacterCard, persona: Persona, session: ChatSession,
                         direction: str = "") -> List[Dict]:
    char, user = card.name or "Companion", persona.name or "User"
    ask = (f"Write {user}'s next message. {user} wants to: {direction.strip()}"
           if direction.strip() else f"Write {user}'s next message.")
    return [
        {"role": "system", "content": substitute_macros(DRAFT_SYSTEM, char, user)},
        {"role": "user", "content": _scene_context(card, persona, session) + f"\n\n[{ask}]"},
    ]


def clean_piece(text: str, names: List[str]) -> str:
    """Drop a leading speaker label ("Narrator:", "Brian:") and wrapping quotes."""
    t = (text or "").strip()
    for n in names:
        # "Narrator: …", or inside the italics the whole piece is wrapped in: "*Narrator: …*"
        m = re.match(r"^(\*?)\s*" + re.escape(n) + r"\s*:\s*", t, re.I) if n else None
        if m:
            t = m.group(1) + t[m.end():]
            break
    if len(t) > 1 and t[0] == t[-1] == '"' and t.count('"') == 2:
        t = t[1:-1].strip()
    return t


# Orpheus voices perform these instead of reading them (services/orpheus.py). Offered
# only to cards whose voice is Orpheus; the chat view hides them, Kokoro strips them.
# A subset of what Orpheus knows: cough/sniffle/yawn read as noise in conversation.
VOICE_TAGS = ("laugh", "chuckle", "sigh", "gasp", "groan")


def voice_tag_note(card, assistant: bool) -> str:
    """Tell an Orpheus-voiced card it may write sound tags. Shown by example, like the
    expression beats: a small model copies a demonstrated shape far more reliably than
    it follows a described one. Trailing, after ASSISTANT_FORMAT_NOTE, because that note
    forbids stage directions and the later instruction is the one that wins."""
    if not str(getattr(card, "tts_voice", "") or "").startswith("orpheus:"):
        return ""
    where = ("where the sound happens, usually at the very start of the reply" if assistant else
             "INSIDE the quoted dialogue where the sound happens — only quoted speech is voiced")
    eg = ("<chuckle> That is exactly what I said. You just did not believe me."
          if assistant else
          '"<chuckle> That is exactly what I said. You just did not believe me."')
    return (
        "Voice tags: your replies are spoken by a voice that can really laugh, chuckle, "
        "sigh, gasp and groan. React with one when the moment calls for it — something "
        "funny or absurd: <laugh> or <chuckle>; a setback or something tiresome: <sigh> or "
        "<groan>; a real surprise: <gasp>. Write the tag " + where + ".\n"
        "Example: " + eg + "\n"
        "At most one per reply; plain questions and requests need none. Use only these five "
        "tags, written exactly so. They are the one allowed exception to having no stage "
        "directions; never describe a sound in words instead (no \"haha\", no *laughs*). "
        "They are performed, not shown, so never mention them."
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
    if mem.summary and not assistant:
        # Roleplay only. "Story so far" is scene framing, and an assistant has no
        # story — but the stronger reason is that `memory.summarize()` is the sole
        # writer of this field and, since the pipelines split, never runs for an
        # assistant. So a non-empty summary on an assistant session is roleplay-era
        # residue from before its card was switched: third-person narration, frozen,
        # because nothing will ever rewrite it. Injecting it under a friendlier
        # header still put a worked example of narrating above an assistant's
        # history, which is the exact leak the split exists to stop. Measured on a
        # live 51-turn session: 1851 chars of it, opening with a hardcoded date.
        # `assistant_memory.fold()` clears the stored copy once that material is
        # safely in directives/facts/notes; this skips it in the meantime.
        # Framed as finished events: the model is to build on this, not retell it.
        # The messages after the system block continue from its "Where things
        # stand" line (see memory.summarize).
        sys_parts.append(
            "Story so far (already happened — never retell it; the messages below "
            "continue from where it leaves off):\n" + sub(mem.summary))

    # Assistant cards get the DATE here, not the time. It changes once a day, so it
    # costs one cache miss a day instead of one a minute. The minute-accurate time
    # used to trail every prompt, and sitting last it was the loudest thing in the
    # prompt: replies volunteered the clock unasked ("Mirror." got an answer about
    # being synchronised to 11:57 AM; the time was written into the middle of a
    # generated system prompt). The exact time now trails only when asked for.
    if assistant:
        today = datetime.now().astimezone()
        sys_parts.append(
            f"Today is {today.strftime('%A, %B')} {today.day}, {today.year}. You are not "
            f"told the time of day, so never state or guess one. Mention the date only "
            f"when {user_name} asks for it.")

    messages: List[Dict] = [{"role": "system", "content": "\n\n".join(p for p in sys_parts if p.strip())}]

    # --- chat history (skip messages already folded into the summary) ---
    for m in session.messages[mem.last_summarized_index:]:
        if m.role == "system":
            continue
        if m.role == "narrator":
            # An event in the scene, from neither of them. As a user turn the model read
            # it as something {{user}} said or did; as a labelled system turn it's the world.
            messages.append({"role": "system", "content": NARRATION_LABEL + sub(m.content)})
            continue
        role = "assistant" if m.role == "char" else "user"
        content = m.swipes[m.swipe_index] if (m.role == "char" and m.swipes) else m.content
        messages.append({"role": role, "content": sub(content)})

    # --- post-history instructions (jailbreak / UJB) go after the history ---
    if card.post_history_instructions:
        messages.append({"role": "system", "content": sub(card.post_history_instructions)})

    # --- ONE trailing note: everything that must sit after the history ---
    # These used to be up to four separate system turns after {{user}}'s message
    # (time, expression rules, format reminder, boundary note), so the last thing
    # the model read before replying was a wall of rules rather than what was just
    # said — and it answered the rules: drifting off topic, re-describing the scene.
    # Merged into one message that ENDS by pointing back at the latest message.
    # The order inside is the old order, so the measured placements still hold:
    # expression beats trail (see expression_note), and the boundary note stays
    # after any UJB and as late as possible (see PROMPT_BUDGET.md).
    last_user = next((m.content for m in reversed(session.messages)
                      if m.role == "user" and m.content and m.content.strip()), "")
    tail: list[str] = []
    if assistant and _ASKS_TIME.search(last_user):
        now = datetime.now().astimezone()
        hour = now.strftime("%I").lstrip("0") or "12"
        tail.append(
            f"Current date and time: {now.strftime('%A, %B')} {now.day}, {now.year}, "
            f"{hour}:{now.strftime('%M %p')} {now.strftime('%Z')}. "
            f"This is authoritative — use it, never guess.")
    expr_hint = expression_note(expr_labels or [], assistant=assistant)
    if expr_hint:
        tail.append(expr_hint)
    if assistant:
        tail.append(ASSISTANT_FORMAT_NOTE.format(char=char_name, user=user_name))
    voice_hint = voice_tag_note(card, assistant)
    if voice_hint:
        tail.append(voice_hint)
    if boundary_note:
        tail.append(sub(boundary_note))
    latest = next((m for m in reversed(session.messages) if m.role != "system"), None)
    if not assistant and latest is not None and latest.role in ("narrator", "char"):
        # A turn {{user}} didn't take: reacting to a narrated event, or Continue.
        tail.append(roleplay_turn_note(latest, char_name, user_name, sub))
    elif last_user:
        tail.append(focus_note(sub(last_user), char_name, user_name, assistant))
    if tail:
        messages.append({"role": "system", "content": "\n\n".join(tail)})

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
