"""Long-term memory: rolling summarization of older turns.

When the running history grows past a threshold, the oldest un-summarized turns
are folded into `session.memory.summary` so the prompt stays within budget while
preserving continuity. Uses the same KoboldCPP backend for the summarization pass.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

import numpy as np

from models.schema import (ChatSession, CharacterCard, CharacterMemory,
                           MemoryFact, Persona)
from services import embeddings
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
LONGTERM_FACT_MAX_CHARS = 200 # one fact must not be able to dominate the block
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
AUTO_FACT_MAX_NEW = 3          # per fold; small on purpose
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
    """Drop candidates that restate something already stored.

    The prompt is TOLD to skip known facts and does not reliably comply — given
    two known facts and a conversation restating them, it re-emitted both
    verbatim. Semantic comparison catches that, and also catches the rewordings
    a string match would miss ("Eli owns a dog named Biscuit" vs "Eli has a dog
    called Biscuit"). Falls back to normalized string equality when the embedder
    isn't available.
    """
    out: list[str] = []
    if not candidates:
        return out
    norm = lambda t: " ".join(t.lower().replace(".", "").split())
    seen = {norm(e) for e in existing}

    vectors = None
    if existing and embeddings.available():
        try:
            vectors = embeddings.encode(existing)
        except Exception:
            vectors = None

    for c in candidates:
        if norm(c) in seen:
            continue
        if vectors is not None:
            try:
                if float((vectors @ embeddings.encode([c])[0]).max()) >= AUTO_FACT_DEDUPE_SIM:
                    continue
            except Exception:
                pass
        out.append(c)
        seen.add(norm(c))
    return out


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
    return _dedupe_facts(candidates[:max_new], known)


async def fold_into_longterm(kobold: KoboldClient, mem: CharacterMemory,
                             session: ChatSession, card: CharacterCard, persona: Persona,
                             upto: int | None = None,
                             max_tokens: int = 500,
                             index_path=None,
                             extract_facts: bool = False) -> bool:
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
        LONGTERM_PROMPT.format(char=char_name, user=user_name,
                               max_bullets=LONGTERM_MAX_BULLETS) + "\n\n"
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

    # Index the same turns for semantic recall (Phase 4). Done HERE, inside the
    # fold, so the threshold and idle triggers can't drift apart on what got
    # indexed; best-effort, and index_add() swallows its own failures.
    # Auto-facts (Phase 5) — promote the stable, atomic things out of the summary,
    # which is rewritten wholesale on every fold and can quietly drop them. Only
    # ever ADDS, and only with origin="auto"; user-written facts are never touched.
    if extract_facts:
        new_facts = await extract_auto_facts(kobold, mem, transcript, char_name, user_name)
        for text in new_facts:
            mem.facts.append(MemoryFact(text=text, origin="auto"))
        if new_facts:
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
            index_add(index_path, [{"text": t, "kind": "fact"} for t in new_facts])

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
            for m in session.messages[start:upto]
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


# ---------------------------------------------------------------------------
# Semantic recall (MEMORY_PLAN Phase 4)
# ---------------------------------------------------------------------------
# The long-term summary above is lossy by design — it keeps what matters in
# general and drops specifics. This recovers the specifics: past turns and facts
# are embedded once, and the turn the user just typed retrieves the few that
# actually relate to it.
#
# Layered ON TOP of Phase 3 rather than replacing it, and sharing its character
# budget rather than adding to it, so enabling recall can't silently double the
# memory footprint of every prompt.
#
# Everything here is best-effort: no embedding model baked in, an onnxruntime
# failure, a corrupt index — all degrade to "no recall block" and leave the chat
# turn untouched. Never raise into the hot path.

RECALL_TOP_K = 4
RECALL_MAX_VECTORS = 2000      # per pairing; ~3 MB at 384 dims, oldest turns evicted first
RECALL_MAX_CHARS = 240         # per stored entry, so one long turn can't dominate

# Relevance gate. BGE's absolute cosine scores are NOT calibrated — measured on a
# companion-shaped corpus, "hello" scored 0.541 while the genuinely relevant
# "what can't I eat?" scored 0.523, so any fixed floor either lets greetings
# through or drops real hits. What DOES separate them is the margin between the
# best hit and the median hit: relevant queries scored >= 0.114 above median,
# irrelevant ones <= 0.094. Hence a margin gate, with the raw floor kept only as
# a loose sanity check.
#
# Calibrated on 8 documents and 13 queries — a heuristic, not a validated
# threshold. Retune here if recall starts surfacing noise.
RECALL_MIN_MARGIN = 0.10
RECALL_MIN_SCORE = 0.45
RECALL_MIN_INDEX = 3           # below this the median is too noisy to gate on
RECALL_MIN_QUERY_WORDS = 3     # "hello" / "ok" / "thanks" retrieve nothing
RECALL_MIN_TEXT = 20           # skip trivially short turns ("ok", "sure", "Hey.")


def _index_load(path) -> tuple:
    """Return (vectors (N,D) float32 | None, meta list). Any problem -> empty."""
    try:
        if not path.exists():
            return None, []
        with np.load(path, allow_pickle=False) as z:
            vectors = z["vectors"]
            meta = json.loads(str(z["meta"]))
        if len(meta) != len(vectors):
            return None, []          # torn write -> rebuild from scratch
        return vectors, meta
    except Exception:
        return None, []


def _index_save(path, vectors, meta) -> None:
    tmp = path.with_suffix(".tmp.npz")
    np.savez(tmp, vectors=vectors.astype(np.float32), meta=np.array(json.dumps(meta)))
    os.replace(tmp, path)


def index_add(path, entries: list[dict], replace_kind: str = None) -> bool:
    """Embed `entries` ([{text, kind}]) and append them to the index at `path`.

    replace_kind: drop every existing entry of that kind first. Facts are
    editable and deletable, so re-indexing them wholesale on save is both
    simpler and more correct than trying to diff them.

    Returns True if the index was written. Never raises.
    """
    try:
        if not embeddings.available():
            return False
        entries = [e for e in entries if (e.get("text") or "").strip()]
        vectors, meta = _index_load(path)

        if replace_kind and meta:
            keep = [i for i, m in enumerate(meta) if m.get("kind") != replace_kind]
            meta = [meta[i] for i in keep]
            vectors = vectors[keep] if vectors is not None and keep else None
        if not entries and replace_kind is None:
            return False

        new_meta, new_text = [], []
        for e in entries:
            text = " ".join((e["text"] or "").split())[:RECALL_MAX_CHARS]
            new_meta.append({"text": text, "kind": e.get("kind", "turn"),
                             "created_at": datetime.now().isoformat()})
            new_text.append(text)

        if new_text:
            fresh = embeddings.encode(new_text)
            vectors = fresh if vectors is None or len(vectors) == 0 else np.vstack([vectors, fresh])
            meta = meta + new_meta

        if vectors is None or len(vectors) == 0:
            path.unlink(missing_ok=True)
            return True

        # Evict oldest TURNS first; curated facts are small and user-owned, so
        # they should never be pushed out by conversation volume.
        if len(meta) > RECALL_MAX_VECTORS:
            over = len(meta) - RECALL_MAX_VECTORS
            drop = [i for i, m in enumerate(meta) if m.get("kind") == "turn"][:over]
            if drop:
                keep = [i for i in range(len(meta)) if i not in set(drop)]
                meta = [meta[i] for i in keep]
                vectors = vectors[keep]

        _index_save(path, vectors, meta)
        return True
    except Exception as e:
        print(f"[recall] index_add skipped: {e}", flush=True)
        return False


def index_search(path, query: str, top_k: int = RECALL_TOP_K) -> list[dict]:
    """Top-k entries related to `query`, or [] if recall isn't applicable.

    Applicability is deliberately conservative — see RECALL_MIN_MARGIN. A block
    of loosely-related memories injected into every turn would degrade her
    replies, so the default when unsure is to inject nothing.
    """
    try:
        if not embeddings.available():
            return []
        if len((query or "").split()) < RECALL_MIN_QUERY_WORDS:
            return []
        vectors, meta = _index_load(path)
        if vectors is None or len(meta) < RECALL_MIN_INDEX:
            return []

        qv = embeddings.encode([query], is_query=True)[0]
        sims = vectors @ qv
        best = float(sims.max())
        if best < RECALL_MIN_SCORE or best - float(np.median(sims)) < RECALL_MIN_MARGIN:
            return []

        order = np.argsort(-sims)[:top_k]
        return [{"text": meta[i]["text"], "kind": meta[i].get("kind", "turn"),
                 "score": float(sims[i])} for i in order
                if float(sims[i]) >= RECALL_MIN_SCORE]
    except Exception as e:
        print(f"[recall] index_search skipped: {e}", flush=True)
        return []


def recall_block(hits: list[dict], budget_chars: int, already_shown: str = "") -> str:
    """Render retrieved memories as a system-prompt block, or "" if nothing fits.

    `already_shown` is the long-term block that will also be injected this turn;
    anything whose text is already in it is dropped rather than repeated — the
    curated facts are always present, so without this the same sentence would
    appear twice in one prompt.
    """
    if not hits or budget_chars <= 0:
        return ""
    header = "Things from earlier that may be relevant:"
    budget = budget_chars - len(header)
    lines = []
    for h in hits:
        text = h["text"].strip()
        if not text or (already_shown and text in already_shown):
            continue
        line = f"- {text}"
        if len(line) + 1 > budget:
            break
        lines.append(line)
        budget -= len(line) + 1
    return header + "\n" + "\n".join(lines) if lines else ""
