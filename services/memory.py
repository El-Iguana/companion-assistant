"""Long-term memory: rolling summarization of older turns.

When the running history grows past a threshold, the oldest un-summarized turns
are folded into `session.memory.summary` so the prompt stays within budget while
preserving continuity. Uses the same KoboldCPP backend for the summarization pass.
"""
from __future__ import annotations

import re
from datetime import datetime

from muninn import facts as _facts
from muninn import fold as _fold
from muninn import summary as _summary
from muninn.checks import clean_memory, keep_facts
# The fold budget and the recall index moved to Muninn (../muninn, MEMORY_CORE_PLAN.md);
# re-exported here under their old names, which main.py, assistant_memory and the
# evals use.
from muninn.fold import (FOLD_CHARS_PER_TOKEN, FOLD_CONTEXT_TOKENS,  # noqa: F401
                         FOLD_RESERVE_TOKENS, FOLD_TRANSCRIPT_CHARS, fold_budget)
from muninn.recall import (RECALL_MAX_CHARS, RECALL_MAX_VECTORS,  # noqa: F401
                           RECALL_MIN_INDEX, RECALL_MIN_MARGIN, RECALL_MIN_QUERY_WORDS,
                           RECALL_MIN_SCORE, RECALL_MIN_TEXT, RECALL_RESERVE_CHARS,
                           RECALL_TOP_K, _index_load, _index_save, index_add,
                           index_search, recall_block)

from models.schema import (ChatSession, CharacterCard, CharacterMemory,
                           MemoryFact, Persona)
from services.kobold import KoboldClient
from services.prompt_builder import substitute_macros, speaker

# Defaults for the rolling summarizer. Overridable per-user via AppSettings
# (summarize_after / keep_recent / summary_max_tokens); these constants remain
# the fallback when no settings are passed.
SUMMARIZE_AFTER = 24        # start summarizing once this many un-summarized messages accumulate
KEEP_RECENT = 12            # keep this many recent turns verbatim in the live prompt
SUMMARY_MAX_TOKENS = 400    # token budget for the summary pass


# Cross-session long-term memory (MEMORY_PLAN Phase 3).
LONGTERM_MAX_FACTS = 40       # hard cap on stored facts — this store must not grow forever
LONGTERM_FACT_MAX_CHARS = 200 # one fact must not be able to dominate the block
LONGTERM_BUDGET_CHARS = 2400  # ceiling on the injected block, at 8192 context
LONGTERM_SUMMARY_SHARE = 0.6  # of that budget, the share the cumulative history may take


async def _answer_full(kobold, prompt: str, params: dict) -> tuple[str, bool]:
    """(answer, hit the token cap). A client without generate_answer_full can't say,
    so it reports not-cut; the missing-stand-line repair still covers that case."""
    full = getattr(kobold, "generate_answer_full", None)
    if full is not None:
        return await full(prompt, params)
    return await kobold.generate_answer(prompt, params), False


def _log_dropped(where: str, lines: list[str]) -> None:
    if lines:
        print(f"[memory] {where}: dropped {len(lines)} non-memory line(s), e.g. "
              f"{lines[0][:80]!r}", flush=True)


def needs_summary(session: ChatSession, summarize_after: int = SUMMARIZE_AFTER) -> bool:
    unsummarized = len(session.messages) - session.memory.last_summarized_index
    return unsummarized >= summarize_after


def _transcript(messages, char_name: str, user_name: str) -> str:
    """Speaker-labelled transcript of `messages`, macros already substituted."""
    return "\n".join(
        f"{speaker(m, char_name, user_name)}: "
        f"{substitute_macros(m.content, char_name, user_name)}"
        for m in messages if m.role != "system"
    )


async def summarize(kobold: KoboldClient, session: ChatSession,
                    card: CharacterCard, persona: Persona,
                    summarize_after: int = SUMMARIZE_AFTER,
                    keep_recent: int = KEEP_RECENT,
                    max_tokens: int = SUMMARY_MAX_TOKENS,
                    context_tokens: int = FOLD_CONTEXT_TOKENS) -> None:
    """Fold older turns into session.memory.summary (mutates session).

    Thresholds/cap default to the module constants but can be overridden from
    per-user settings. `keep_recent` is capped below `summarize_after` so a fold
    always covers at least one turn."""
    keep_recent = min(keep_recent, summarize_after - 1)
    if not needs_summary(session, summarize_after):
        return

    char_name = card.name or "Companion"
    user_name = persona.name or "User"
    start = session.memory.last_summarized_index
    fold_end = len(session.messages) - keep_recent
    if fold_end <= start:
        return

    # Chunked for the same reason as the long-term fold (see _fold_chunks): this
    # window is bounded by turn COUNT and never by length — `summarize_after`
    # reaches 200 on the settings slider — so a long-turn roleplay renders a
    # transcript that overflows the context and, truncated from the front, takes
    # these instructions with it. Each pass folds into the summary the one before
    # it produced, which is what the threshold already does across turns.
    chunks = _fold_chunks(session.messages, start, fold_end, char_name, user_name,
                          fold_budget(context_tokens))
    if not chunks:
        # Nothing summarizable in the range (system-only turns) — still advance the
        # mark so it isn't rescanned on every later turn.
        session.memory.last_summarized_index = fold_end
        return

    # Mode-aware, because this block is injected into every later prompt and so
    # teaches a format by example. "Summarize the roleplay ... third person, present
    # tense" produced pages of narrative prose that then sat above an assistant's
    # history as a worked demonstration of narrating. Assistants get notes.
    #
    # UNREACHABLE since the pipelines split: `main._maintain_memory` takes the
    # assistant branch before this function's only call site, so an assistant never gets here. Kept
    # as a guard, not as a live path — if a future caller does reach it, it must
    # still not emit narration. The assistant equivalent is
    # `assistant_memory.NOTES_PROMPT`; change that one.
    instruction = (
        "Summarize the conversation so far into concise notes. Preserve decisions made, "
        "facts established, open questions, and anything asked to be remembered. Plain "
        "notes only: no scene, no narration, no description of anyone, and do not write "
        "it as a story."
        if getattr(card, "chat_mode", "roleplay") == "assistant" else
        # Notes in the past tense, not story prose. "A few paragraphs, third person,
        # present tense" produced a retelling of the story, which then sat in every
        # prompt as a worked example of recapping — and replies recapped. The
        # closing line gives the model a "you are here" for the moment the verbatim
        # messages pick up from, which nothing in the prompt carried before.
        f"Write the memory of this roleplay as compact notes in the past tense: key "
        f"events in order, facts established, relationship developments between "
        f"{char_name} and {user_name}, promises, and unresolved threads. Plain notes, "
        f"not a story: no scene-setting, no description for its own sake. End with ONE "
        f"line that starts \"Where things stand:\" saying where {char_name} and "
        f"{user_name} are, who is with them, and what is happening at the last moment "
        f"covered. If the existing memory already has that line, replace it — keep only "
        f"one, describing the newest moment."
    )

    roleplay = getattr(card, "chat_mode", "roleplay") != "assistant"
    for n, (end, transcript) in enumerate(chunks, start=1):
        prev = session.memory.summary
        prompt = (
            instruction + "\n\n"
            + (f"Existing memory:\n{prev}\n\n" if prev else "")
            + f"New events to fold in:\n{transcript}\n\nUpdated memory:"
        )

        try:
            text, truncated = await _answer_full(kobold, prompt, {
                "max_length": max_tokens, "temperature": 0.5, "top_p": 0.9, "rep_pen": 1.05})
        except Exception:
            return  # summarization is best-effort; never block the chat. Passes that
                    # already landed stand — last_summarized_index records exactly
                    # them, and the turns after it simply stay verbatim for now.

        # The closing "Where things stand:" line is the first thing the token cap cuts
        # (muninn.summary): drop a cut-off last line, keep one stand line, last, and
        # ask for it on its own when the pass didn't produce one. Only on the final
        # pass — an earlier pass's line is replaced by the next one anyway.
        notes, stand = _summary.finish(text, truncated)
        # Untagged reasoning, echoed rules and pasted transcript (muninn.checks).
        notes, junk = clean_memory(notes, transcript=transcript, prompt=instruction,
                                   speakers=[char_name, user_name])
        _log_dropped("summary", junk)
        if roleplay and stand is None and n == len(chunks):
            stand = await _summary.repair_stand(kobold.generate_answer, transcript,
                                                f"{char_name} and {user_name}")
        summary = _summary.join(notes, stand if roleplay else None)
        if not summary.strip():
            return

        session.memory.summary = summary.strip()
        session.memory.last_summarized_index = end


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

# Bullets, not prose, and every rule below is here because the model broke that
# way in testing against the 9B fine-tune:
#   - "a few paragraphs at most" produced 1800 characters of florid essay about a
#     dog and a bass ("immutable pillars", "a symbol of stability"), so the format
#     is now terse bullets and interpretation is banned outright;
#   - a bullet cap alone got padded to exactly the cap with invented traits
#     ("Testy prefers to conclude interactions with a casual 'talk later'"), so
#     the brevity instruction sits above the rule list, states that a short memory
#     is a GOOD memory, and inferring traits is forbidden;
#   - the model recorded its own replies as memories ("MemTest confirms retention
#     of all noted personal facts"), hence the explicit carve-out;
#   - folding a cancellation kept BOTH "plans to sail next month" and "trip is
#     off", i.e. a self-contradicting memory, hence the REPLACE rule.
# Measured across three successive folds: 113 -> 228 -> 322 chars, growing only
# with genuinely new content. Retune here rather than by lowering
# LONGTERM_BUDGET_CHARS — truncating mid-sentence is worse than generating short.
LONGTERM_MAX_BULLETS = 10

LONGTERM_PROMPT = (
    "You maintain {char}'s durable memory of {user} across all their conversations.\n\n"
    "Rewrite the memory below so it also covers the new events.\n\n"
    "Keep it SHORT. Most conversations add only one or two bullets. Never pad to reach "
    "the limit of {max_bullets}; a four-bullet memory is a good memory.\n\n"
    "- Terse bullets, one line each. No preamble, no closing sentence, no headings.\n"
    "- Record only what was explicitly said, decided, or promised, and only if it stays "
    "true after the conversation ends. Do not infer traits, preferences, or patterns.\n"
    "- Do NOT record {char}'s own replies, acknowledgements, or offers — that {char} "
    "noted, stored, remembered, agreed, or offered something is not a memory.\n"
    "- If the new events change, resolve, or contradict an existing bullet, REPLACE that "
    "bullet. Never keep both versions; drop anything finished or no longer true.\n"
    "- No interpretation, no significance, no scene description, no small talk.\n"
    "- Third person, present tense."
)


# Auto-fact extraction (MEMORY_PLAN Phase 5).
# A wrong auto-fact is worse than a missing one: the summary gets rewritten every
# fold, but a fact persists into every future conversation until someone notices
# and deletes it. So this is deliberately reluctant — NONE is a good answer, the
# per-fold cap is small, and near-duplicates are dropped in code rather than
# trusted to the prompt.
AUTO_FACT_MAX_NEW = 3          # per fold; small on purpose. Fallback only — the live
                               # value comes from AppSettings.auto_facts_max_new, since
                               # the right setting is a judgement call: low over-selects
                               # (a four-fact conversation loses one), high risks storing
                               # marginal facts that then persist into every future chat.
AUTO_FACT_DEDUPE_SIM = 0.86    # cosine above this against an existing fact -> drop

# Each Do-NOT below is a failure seen against the 9B fine-tune. The transient
# rule is emphatic ("even if it sits in the same sentence as a durable fact")
# because the first version happily extracted "Nadia is visiting at the end of
# summer" out of a sentence that also carried the durable "Nadia is a paramedic
# in Leeds". Sharpened, it kept the paramedic and dropped the visit.
AUTO_FACT_PROMPT = (
    "Extract durable facts about {user} from the conversation below.\n\n"
    "A durable fact stays true for months or years: names, relationships, possessions, "
    "where they live or work, allergies and health constraints, firm preferences and "
    "dislikes, skills, standing commitments.\n\n"
    "Do NOT extract:\n"
    "- anything scheduled, planned, or upcoming. If it has a date, a season, or words "
    "like next, soon, visiting, coming, or planning, it is NOT durable — skip it, even "
    "if it sits in the same sentence as a durable fact.\n"
    "- moods or one-off reactions\n"
    "- anything {char} said, offered, or promised\n"
    "- anything you inferred rather than were plainly told\n"
    "- anything already in Known facts below, in any wording\n\n"
    "One fact per line. No bullets, no numbering, no commentary. Each fact must stand "
    "alone without the conversation. Third person, using the name {user}.\n"
    "At most {max_new}. Fewer is better, and NONE is a good answer — output exactly "
    "NONE if there are no new durable facts."
)


def _dedupe_facts(candidates: list[str], existing: list[str]) -> list[str]:
    """Drop candidates that restate something already stored (muninn.facts)."""
    return _facts.dedupe_facts(candidates, existing, AUTO_FACT_DEDUPE_SIM)


async def extract_auto_facts(kobold: KoboldClient, mem: CharacterMemory,
                             transcript: str, char_name: str, user_name: str,
                             max_new: int = AUTO_FACT_MAX_NEW) -> list[str]:
    """Durable facts stated in `transcript` and not already stored. [] on any
    problem — extraction failing must never fail the fold that called it."""
    if not transcript.strip():
        return []
    known = [f.text for f in mem.facts if f.text.strip()]
    prompt = (
        AUTO_FACT_PROMPT.format(char=char_name, user=user_name, max_new=max_new) + "\n\n"
        + ("Known facts:\n" + "\n".join(f"- {k}" for k in known) + "\n\n" if known else "")
        + f"Conversation:\n{transcript}\n\nNew durable facts:"
    )
    try:
        raw = await kobold.generate_answer(prompt, {
            "max_length": 220, "temperature": 0.2, "top_p": 0.9, "rep_pen": 1.05})
    except Exception:
        return []

    candidates = []
    for line in (raw or "").splitlines():
        line = line.strip().lstrip("-*•").strip()
        line = re.sub(r"^\d+[.)]\s*", "", line)
        if not line or line.upper().startswith("NONE") or len(line) < 8:
            continue
        candidates.append(line[:LONGTERM_FACT_MAX_CHARS])
    # "No new durable facts.", "I'll list them as requested:", the prompt's own rules:
    # dropped before the per-fold cap, so they can't take a real fact's slot.
    kept = keep_facts(candidates, prompt=AUTO_FACT_PROMPT.format(
        char=char_name, user=user_name, max_new=max_new))
    _log_dropped("auto-facts", [c for c in candidates if c not in kept])
    return _dedupe_facts(kept[:max_new], known)


def _fold_chunks(messages, start: int, upto: int, char_name: str, user_name: str,
                 budget: int = FOLD_TRANSCRIPT_CHARS) -> list[tuple[int, str]]:
    """Split [start, upto) into (end_index, transcript) passes that each fit `budget`.

    muninn.fold.fold_chunks, with this app's speaker-labelled transcript. Why the
    range is split into passes rather than truncated is documented there."""
    return _fold.fold_chunks(messages, start, upto,
                             lambda ms: _transcript(ms, char_name, user_name), budget)


async def fold_into_longterm(kobold: KoboldClient, mem: CharacterMemory,
                             session: ChatSession, card: CharacterCard, persona: Persona,
                             upto: int | None = None,
                             max_tokens: int = 500,
                             index_path=None,
                             extract_facts: bool = False,
                             max_new_facts: int = AUTO_FACT_MAX_NEW,
                             context_tokens: int = FOLD_CONTEXT_TOKENS) -> bool:
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
    # One pass per context-sized slice of the range, oldest first, each folded into
    # the memory the pass before it produced. A single pass is still the normal
    # case; more than one only happens on a backlog that could not have fitted.
    chunks = _fold_chunks(session.messages, start, upto, char_name, user_name,
                          fold_budget(context_tokens))
    if not chunks:
        # Nothing foldable in the range (system-only turns) — still advance the
        # mark so the same empty range isn't rescanned on every later sweep.
        mem.swept_sessions[session.id] = upto
        mem.updated_at = datetime.now().isoformat()
        return True

    done = start                  # how far the folding actually got
    facts_left = max_new_facts    # the per-fold cap is for the WHOLE range, not per pass
    for end, transcript in chunks:
        prompt = (
            LONGTERM_PROMPT.format(char=char_name, user=user_name,
                                   max_bullets=LONGTERM_MAX_BULLETS) + "\n\n"
            + (f"Existing memory:\n{mem.summary}\n\n" if mem.summary else "")
            + f"New events:\n{transcript}\n\nUpdated memory:"
        )

        try:
            summary = await kobold.generate_answer(prompt, {
                "max_length": max_tokens, "temperature": 0.5, "top_p": 0.9, "rep_pen": 1.05})
        except Exception:
            break     # best-effort; never block or fail the chat turn. Keep the
                      # passes that did land — `done` records exactly those, so
                      # the next sweep resumes from there instead of redoing them.
        # Untagged reasoning, echoed rules and pasted transcript (muninn.checks). A pass
        # that was nothing else comes back empty and counts as a failed pass.
        summary, junk = clean_memory(
            summary, transcript=transcript,
            prompt=LONGTERM_PROMPT.format(char=char_name, user=user_name,
                                          max_bullets=LONGTERM_MAX_BULLETS),
            speakers=[char_name, user_name])
        _log_dropped("long-term", junk)
        if not summary.strip():
            break

        mem.summary = summary.strip()
        done = end

        # Auto-facts (Phase 5) — promote the stable, atomic things out of the
        # summary, which is rewritten wholesale on every fold and can quietly drop
        # them. Only ever ADDS, and only with origin="auto"; user-written facts are
        # never touched.
        if extract_facts and facts_left > 0:
            new_facts = await extract_auto_facts(kobold, mem, transcript, char_name,
                                                 user_name, max_new=facts_left)
            for text in new_facts:
                mem.facts.append(MemoryFact(text=text, origin="auto"))
            if new_facts:
                facts_left -= len(new_facts)
                print(f"[auto-facts] +{len(new_facts)} for {mem.character_id[:8]}", flush=True)
            # Evict oldest AUTO facts if over the cap — a curated fact must never be
            # pushed out by something the app decided to remember on its own.
            if len(mem.facts) > LONGTERM_MAX_FACTS:
                over = len(mem.facts) - LONGTERM_MAX_FACTS
                drop = {i for i, f in enumerate(mem.facts) if f.origin == "auto"}
                drop = set(sorted(drop)[:over])
                mem.facts = ([f for i, f in enumerate(mem.facts) if i not in drop]
                             if drop else mem.facts[-LONGTERM_MAX_FACTS:])
            if new_facts and index_path is not None:
                # Index what SURVIVED the eviction above, not what was extracted. A
                # fact dropped at the cap would otherwise stay in the index as
                # kind="fact" and keep being recalled into prompts, while the Memory
                # modal shows no such fact for the user to correct or delete —
                # invisible memory is exactly what `origin` exists to prevent.
                alive = {f.text for f in mem.facts if f.origin == "auto"}
                indexable = [t for t in new_facts if t in alive]
                if indexable:
                    index_add(index_path, [{"text": t, "kind": "fact"} for t in indexable])

    if done <= start:
        return False
    mem.swept_sessions[session.id] = done
    mem.updated_at = datetime.now().isoformat()

    # Index the folded turns for semantic recall (Phase 4). Done HERE, inside the
    # fold, so the threshold and idle triggers can't drift apart on what got
    # indexed; best-effort, and index_add() swallows its own failures.
    #
    # USER turns only. Her own replies restate what the user just said, wrapped in
    # personality — indexing them duplicates the signal and, worse, drags the
    # median similarity up, which is exactly what index_search's margin gate is
    # measured against. Observed on a live index: with her turns included, 2 of 6
    # deliberately irrelevant queries ("tell me a joke" -> "RagTest: Hey.") slipped
    # through the gate; with user turns only, 0 of 6 did, and all 5 relevant
    # queries still retrieved the right turn.
    if index_path is not None:
        index_add(index_path, [
            {"text": f"{user_name}: {m.content}", "kind": "turn"}
            for m in session.messages[start:done]
            if m.role == "user" and m.content
            and len(m.content.strip()) >= RECALL_MIN_TEXT
        ])
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
            continue                              # skip THIS one, don't stop: a single
        kept.append(line)                         # oversized fact must cost only itself
        remaining -= len(line) + 1
    kept.reverse()                                # back to the order the user wrote them

    parts = [header]
    if kept:
        parts.append("\n".join(kept))
    if summary:
        parts.append(f"Earlier history:\n{summary}")
    return "\n".join(parts) if (kept or summary) else ""


# ---------------------------------------------------------------------------
# Semantic recall (MEMORY_PLAN Phase 4) lives in muninn.recall: index_add,
# index_search, recall_block and the RECALL_* gate constants, imported above.
# ---------------------------------------------------------------------------
