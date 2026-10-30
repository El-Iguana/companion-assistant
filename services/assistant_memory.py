"""Memory for task-driven assistants — isolated from services/memory.py on purpose.

`memory.py` serves a companion: a character with a history, a relationship, and a
narrative worth preserving. An assistant is a tool. It has no story, and the thing
it must not do is drift — a tool that misremembers your instructions is worse than
one that remembers nothing.

The two pipelines were one, with `if chat_mode == "assistant"` branches added as
each leak was found: the relationship descriptor, the narration example, then the
"summarize the roleplay so far" prompt whose narrative prose was injected into
every assistant turn as a worked example of narrating. Each fix was correct and
each left another branch for the next feature to forget. This module has no
branch: there is no roleplay framing here to leak.

What it keeps, and nothing else:
  directives  standing instructions to obey    ("address me as Sir")
  facts       things that are true             ("has two friends, Tom and Bill")
  notes       terse task state, rewritten whole ("waiting on the middleware build")
"""
from __future__ import annotations

import re
from datetime import datetime

from models.schema import (AssistantMemory, CharacterCard, ChatSession,
                           MemoryFact, Persona)
from services.memory import FOLD_CONTEXT_TOKENS, _fold_chunks, _transcript, fold_budget

MAX_DIRECTIVES = 20       # instructions are few and load-bearing; a long list is a smell
MAX_FACTS = 40
MAX_NOTES = 12
FACT_MAX_CHARS = 200
NOTE_MAX_CHARS = 160
BUDGET_CHARS = 2400       # same ceiling as the companion block, spent differently
MAX_NEW_PER_FOLD = 4      # across BOTH categories

# KNOWN, UNFIXED: work-in-progress still leaks into `facts`. Measured — "We still
# need to finish the middleware" was filed as a durable fact despite the rule
# against it, where it will outlive its truth. Two prompt revisions improved the
# instruction/fact split and the same-sentence carve-out; a third without a probe
# set would be guessing. This wants what eval/ wants in the Gnostic fork: a fixture
# transcript with labelled expected output, so a prompt change can be measured
# instead of eyeballed. Until then a stale fact is visible and deletable in the
# Memory modal, which is what `origin` is for.

# Split on purpose. Asked for "durable facts" as one bucket, the model returned
# "El Iguana prefers to be addressed as Sir" as a fact — true, but filed where
# nothing tells the assistant to DO it. An instruction that is merely known is an
# instruction that gets ignored.
EXTRACT_PROMPT = (
    "Read the conversation and pull out what {char} must carry into future "
    "conversations with {user}. Two kinds, and the difference matters:\n\n"
    "INSTRUCTION: something {user} told {char} to do, or how to behave. Forms of "
    "address, formats, units, defaults, what to do or never do.\n"
    "FACT: something that is true about {user} or their setup, which {char} should "
    "know but is not told to act on.\n\n"
    # TRIED AND REVERTED 2026-09-03: replacing the description below with a worked
    # two-line example ("Said: my sister Ana is flying in on Friday / FACT: Ana is
    # {user}'s sister") on the theory that a demonstrated shape gets copied more
    # reliably than a described rule — which is true of the expression beats. Here
    # it measured 0/6 against 1/9 for the description, i.e. no better and one more
    # thing in the prompt. The same-sentence split is simply not solved; do not
    # retry the example without a different idea behind it.
    "A sentence can carry both kinds at once, and the durable half still counts: "
    "\"my friends Tom and Bill are visiting next month\" contains the durable FACT "
    "that Tom and Bill are their friends, and a visit that is not durable. Record "
    "the durable half and drop the rest — do not discard the whole sentence.\n\n"
    "Do NOT record:\n"
    "- the scheduling itself — a date, or words like next, soon, about to, or "
    "working on. That part is not durable, even where the same sentence also "
    "carries something that is\n"
    "- anything true only today, or the state of the current task\n"
    "- anything {char} said, offered, or suggested\n"
    "- anything you inferred rather than were plainly told\n"
    "- anything already listed under Known below, in any wording\n\n"
    "One per line, each prefixed exactly INSTRUCTION: or FACT:. No bullets, no "
    "numbering, no commentary. Each must stand alone without the conversation, and "
    "must name things exactly as {user} named them.\n"
    "At most {max_new} lines total. Fewer is better, and NONE is a good answer — "
    "output exactly NONE if there is nothing durable.\n\n"
    "{known}"
    "Conversation:\n{transcript}\n\nDurable items:"
)

# Rewritten whole rather than appended, so a finished item leaves. The companion
# summary grew because prose accretes; a list of task state should shrink when the
# task is done.
NOTES_PROMPT = (
    "Track where things stand between {char} and {user}. Rewrite the list below so "
    "it also covers the new activity.\n\n"
    "- Terse lines, one clause each. No preamble, no prose, no narration.\n"
    "- Only open threads, decisions taken, and state {char} would need to pick the "
    "work back up. Not chit-chat, not what was merely discussed.\n"
    "- Do NOT repeat standing instructions or facts about {user}. Those are stored "
    "separately and injected already; a line here that restates one is waste, and "
    "two copies of an instruction can drift apart.\n"
    "- DROP anything finished, answered, or no longer true. A shorter list is a "
    "better list, and an empty list is correct when nothing is outstanding.\n"
    "- Never record what {char} said, offered, knows, or its own status. "
    "\"{char} is operational\" and \"{char} knows X\" are not open threads.\n"
    "- Never record a time, a date, or a clock reading. This list is injected into "
    "every future conversation, where a timestamp from tonight is simply wrong.\n"
    "- At most {max_notes} lines.\n\n"
    "Current list:\n{notes}\n\nNew activity:\n{transcript}\n\nUpdated list:"
)


def _clean(line: str, cap: int) -> str:
    line = line.strip().lstrip("-*•").strip()
    line = re.sub(r"^\d+[.)]\s*", "", line)
    return line[:cap]


def _norm(t: str) -> str:
    return " ".join(t.lower().replace(".", "").split())


def _dedupe(candidates: list[str], existing: list[str]) -> list[str]:
    """String-level only. The companion pipeline compares embeddings here; an
    assistant's directives are short, literal and few, so exact-match is enough and
    avoids making the embedder a dependency of behaving correctly."""
    seen = {_norm(e) for e in existing}
    out = []
    for c in candidates:
        if _norm(c) and _norm(c) not in seen:
            out.append(c)
            seen.add(_norm(c))
    return out


_STOP = {"a", "an", "the", "to", "of", "in", "on", "as", "is", "are", "be", "and",
         "or", "for", "with", "all", "every", "me", "my", "i", "it", "that", "this",
         "should", "must", "will", "always", "never", "use", "give", "instead"}


def _content(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']{2,}", text.lower()) if w not in _STOP}


def _drop_status_notes(notes: list[str], char: str, user: str) -> list[str]:
    """Drop notes that report the assistant's own state, or log what was said.

    Notes are OPEN THREADS. "Jarvis is fully awake and operational" is not one; it
    is a status line that will still be sitting in the prompt next week. Neither is
    "El Iguana has issued the command to proceed", which is a transcript entry
    wearing a note's clothes — the exact shape the companion summary produced.

    The prompt forbids both and does not comply, which is now the expected result;
    this is the deterministic half. Matching is anchored to the SUBJECT: a note
    that opens with the assistant's name plus a state verb, or the user's name plus
    a speech verb. That leaves real threads about the assistant alone — "Jarvis
    needs the API key" and "Jarvis has not finished the migration" both survive,
    because `needs` and `has not finished` are not states of being.
    """
    c, u = re.escape(char), re.escape(user)
    status = re.compile(rf"^\s*(?:{c}|it|he|she)\s+(?:is|was|are|were|remains|stands|"
                        rf"feels|has been|had been|will be)\b", re.I)
    logged = re.compile(rf"^\s*{u}\s+(?:said|asked|confirmed|issued|gave|told|"
                        rf"requested|stated|mentioned|greeted|has (?:issued|said|asked|"
                        rf"confirmed|given|told|requested))\b", re.I)
    # "System rebooted successfully." has no subject, so the rules above cannot see
    # it — measured, it survived into notes. Verb list kept tight so a real thread
    # starting with the same noun lives: "System migration still pending" is not
    # matched, because `migration` is not a state.
    bare = re.compile(r"^\s*(?:all\s+)?(?:system|systems|everything|diagnostics?)\s+"
                      r"(?:is|are|was|were|rebooted|restarted|online|operational|"
                      r"nominal|stable|ready|green|normal|functioning)\b", re.I)
    return [n for n in notes
            if not status.match(n) and not logged.match(n) and not bare.match(n)]


def _drop_restated(notes: list[str], entries: list[str]) -> list[str]:
    """Remove notes that restate a directive or a fact.

    The prompt is TOLD not to repeat them and does not comply — measured: it
    re-emitted "Address Jared Revan as Sir" and "Use Eastern time instead of UTC"
    as task notes while both were already stored as directives. Exactly the
    finding the companion pipeline recorded about known facts, so the answer is
    the same: dedupe in code.

    Overlap is scored against the SHORTER of the two, so a terse directive still
    matches the wordier restatement of it. Deliberately not embeddings — an
    assistant behaving correctly must not depend on an optional model being
    present, and these strings are short and literal.
    """
    keep = []
    for note in notes:
        n = _content(note)
        if not n:
            continue
        if any((lambda e: len(n & e) / max(1, min(len(n), len(e))) >= 0.6)(_content(x))
               for x in entries if _content(x)):
            continue
        keep.append(note)
    return keep


async def extract(kobold, mem: AssistantMemory, transcript: str,
                  char: str, user: str, max_new: int = MAX_NEW_PER_FOLD
                  ) -> tuple[list[str], list[str]]:
    """-> (new directives, new facts). ([], []) on any problem."""
    if not transcript.strip():
        return [], []
    known_lines = ([f"- (instruction) {d.text}" for d in mem.directives]
                   + [f"- (fact) {f.text}" for f in mem.facts])
    known = ("Known:\n" + "\n".join(known_lines) + "\n\n") if known_lines else ""
    prompt = EXTRACT_PROMPT.format(char=char, user=user, max_new=max_new,
                                   known=known, transcript=transcript)
    try:
        raw = await kobold.generate_answer(prompt, {
            "max_length": 260, "temperature": 0.2, "top_p": 0.9, "rep_pen": 1.05})
    except Exception:
        return [], []

    directives, facts = [], []
    for line in (raw or "").splitlines():
        line = _clean(line, FACT_MAX_CHARS + 16)
        if not line or line.upper().startswith("NONE"):
            continue
        low = line.lower()
        if low.startswith("instruction:"):
            directives.append(line.split(":", 1)[1].strip()[:FACT_MAX_CHARS])
        elif low.startswith("fact:"):
            facts.append(line.split(":", 1)[1].strip()[:FACT_MAX_CHARS])
        # An unprefixed line is dropped rather than guessed at: filing an
        # instruction as a fact is exactly the failure this split exists to stop.
    directives = _dedupe([d for d in directives if d], [d.text for d in mem.directives])
    facts = _dedupe([f for f in facts if f], [f.text for f in mem.facts])
    return directives[:max_new], facts[:max_new]


async def fold(kobold, mem: AssistantMemory, session: ChatSession,
               card: CharacterCard, persona: Persona, upto: int | None = None,
               max_tokens: int = 400, extract_items: bool = True,
               max_new: int = MAX_NEW_PER_FOLD,
               context_tokens: int = FOLD_CONTEXT_TOKENS) -> bool:
    """Fold this session's turns into `mem`. True if it changed. Never raises.

    Same high-water-mark bookkeeping and chunked passes as the companion fold —
    that part is domain-independent and already paid for — but what comes out is
    directives, facts and task notes, never a narrative."""
    start = mem.swept_sessions.get(session.id, 0)
    upto = len(session.messages) if upto is None else upto
    if upto <= start:
        return False

    char, user = card.name or "Assistant", persona.name or "User"
    chunks = _fold_chunks(session.messages, start, upto, char, user,
                          fold_budget(context_tokens))
    if not chunks:
        mem.swept_sessions[session.id] = upto
        mem.updated_at = datetime.now().isoformat()
        return True

    done, left, lo = start, max_new, start
    for end, transcript in chunks:
        prompt = NOTES_PROMPT.format(char=char, user=user, max_notes=MAX_NOTES,
                                     notes="\n".join(f"- {n}" for n in mem.notes) or "(empty)",
                                     transcript=transcript)
        try:
            raw = await kobold.generate_answer(prompt, {
                "max_length": max_tokens, "temperature": 0.3, "top_p": 0.9, "rep_pen": 1.05})
        except Exception:
            break
        # An empty list IS a valid answer here — unlike the companion summary,
        # where empty meant the pass failed. "Nothing outstanding" is the goal
        # state for a task assistant, so only a hard failure stops the fold.
        notes = [_clean(l, NOTE_MAX_CHARS) for l in (raw or "").splitlines()]
        notes = [n for n in notes if n and not n.upper().startswith("NONE")]
        # Extraction runs after this, so the check is against what is already
        # stored; a directive learned in this same pass is caught on the next fold.
        notes = _drop_status_notes(notes, char, user)
        mem.notes = _drop_restated(
            notes, [d.text for d in mem.directives] + [f.text for f in mem.facts])[:MAX_NOTES]
        done = end

        if extract_items and left > 0:
            # USER turns only, and this is the fix for the worst failure the eval
            # found: on a transcript of pure small talk ("morning" / "just checking
            # you're awake" / "ok carry on") the extractor stored "Address El Iguana
            # as Sir" as a standing instruction in ALL THREE runs. Nobody said it —
            # the assistant says "Sir" in its own replies, and its own politeness got
            # mined into an instruction to itself. An invented directive is then
            # injected into every later prompt.
            #
            # The prompt already forbids recording what the assistant said and did
            # not comply, which by now is the expected outcome. Removing the material
            # is deterministic: it cannot mine what it cannot see. Same shape as the
            # companion pipeline indexing user turns only, for the same reason —
            # the assistant's replies restate the user wrapped in personality.
            said = _transcript([m for m in session.messages[lo:end] if m.role == "user"],
                               char, user)
            new_d, new_f = ((await extract(kobold, mem, said, char, user, max_new=left))
                            if said.strip() else ([], []))
            for t in new_d:
                mem.directives.append(MemoryFact(text=t, origin="auto"))
            for t in new_f:
                mem.facts.append(MemoryFact(text=t, origin="auto"))
            left -= len(new_d) + len(new_f)
            if new_d or new_f:
                print(f"[assistant-memory] +{len(new_d)} directive(s) +{len(new_f)} fact(s)",
                      flush=True)
            # Evict oldest AUTO entries only; a hand-written one is never pushed
            # out by something the app decided to remember on its own.
            for bucket, cap in ((mem.directives, MAX_DIRECTIVES), (mem.facts, MAX_FACTS)):
                while len(bucket) > cap:
                    idx = next((i for i, f in enumerate(bucket) if f.origin == "auto"), 0)
                    bucket.pop(idx)
        lo = end

    if done <= start:
        return False
    mem.swept_sessions[session.id] = done
    mem.updated_at = datetime.now().isoformat()

    # A session migrated from roleplay arrives carrying `session.memory.summary` —
    # narrative prose that `memory.summarize()` wrote and, since the pipelines
    # split, will never rewrite: it is frozen at whatever the roleplay summarizer
    # last produced, timestamps and all. `build_messages` no longer injects it for
    # an assistant, and this drops the stored copy so it cannot come back.
    #
    # Cleared HERE, and only once the fold has passed the point the summary covered,
    # so nothing is discarded that was not recorded first: those same turns have
    # just been read into directives, facts and notes. `session.messages` is
    # untouched either way — this deletes a recap, not history.
    if session.memory.summary and done >= session.memory.last_summarized_index:
        session.memory.summary = ""
    return True


def block(mem: AssistantMemory | None, char: str, user: str,
          budget_chars: int = BUDGET_CHARS) -> str:
    """Render for the system prompt, or "" if there is nothing to say.

    Directives are laid out FIRST and are never truncated. They are instructions:
    dropping one to fit a budget silently changes how the assistant behaves, with
    no error and nothing in the transcript to explain it. Facts and notes are only
    known, so they yield instead.
    """
    if mem is None:
        return ""
    directives = [d.text.strip() for d in mem.directives if d.text.strip()]
    facts = [f.text.strip() for f in mem.facts if f.text.strip()]
    notes = [n.strip() for n in mem.notes if n.strip()]
    if not (directives or facts or notes):
        return ""

    parts: list[str] = []
    if directives:
        parts.append(f"Standing instructions from {user} — follow these in every reply:\n"
                     + "\n".join(f"- {d}" for d in directives))
    remaining = budget_chars - len(parts[0] if parts else "")

    for header, items in ((f"What {char} knows about {user}:", facts),
                          ("Where things stand:", notes)):
        if not items or remaining <= len(header):
            continue
        room = remaining - len(header) - 1
        kept = []
        for line in (f"- {i}" for i in items):
            if len(line) + 1 > room:
                continue                       # skip this one, not the rest
            kept.append(line)
            room -= len(line) + 1
        if kept:
            block_text = header + "\n" + "\n".join(kept)
            parts.append(block_text)
            remaining -= len(block_text) + 2
    return "\n\n".join(parts)
