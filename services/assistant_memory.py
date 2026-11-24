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
    "Track where things stand between {char} and {user}: open threads, decisions taken, and the "
    "state {char} would need to pick the work back up. Something {user} still has to do, or is "
    "planning, or asked {char} to help with, IS a thread (\"buy pasta sheets on Friday\", "
    "\"switch the DNS\").\n\n"
    "The current threads are numbered below. Read the new activity and write ONLY what changes, "
    "one per line:\n"
    "  DONE <number>          a thread that is finished, answered, or no longer true\n"
    "  EDIT <number>: <text>  a thread that moved on (the new wording)\n"
    "  NEW: <text>            a thread the new activity opens\n"
    "Threads you don't mention stay as they are, so never repeat one. If nothing changes, "
    "write NONE.\n\n"
    "- Terse, one clause each. No preamble, no prose, no narration.\n"
    "- Not chit-chat or small talk.\n"
    "- DONE only when the new activity says the thread is finished; a different topic is not "
    "a reason.\n"
    "- Do NOT record standing instructions or facts about {user}; those are stored separately.\n"
    "- Never record what {char} said, offered, knows, or its own status.\n"
    "- Never record a time, a date, or a clock reading.\n\n"
    "Current threads:\n{notes}\n\nNew activity:\n{transcript}\n\nChanges:"
)
NOTE_STALE_DAYS = 14         # a thread untouched this long leaves the prompt (kept in the store)
_OP = re.compile(r"^(DONE|EDIT|NEW)\b\s*(\d+)?\s*:?\s*(.*)$", re.I)


def _clean(line: str, cap: int) -> str:
    line = line.strip().lstrip("-*•").strip()
    line = re.sub(r"^\d+[.)]\s*", "", line)
    return line[:cap]


# Lines the notes pass writes that are no note: placeholders, list headers, markup, and prose
# about the conversation itself. All four came back from Cydonia-24B on a transcript of pure
# small talk ("(empty)", "Updated list:", "</assink>", "The interaction shows Brian
# checking if Mira is awake"), and each would be injected into every later prompt.
_JUNK_NOTE = re.compile(
    r"^\(?\s*(?:empty|none|nothing|no notes?|n/?a)\s*\)?\.?$"          # placeholders
    r"|:\s*$"                                                       # a header
    r"|</?[a-z_]+>"                                                  # markup
    r"|^(?:the|this) (?:interaction|conversation|exchange|chat|new activity|current list|list)\b"
    r"|\b(?:updated|current) (?:list|notes)\b", re.I)


def _junk_notes(notes: list[str]) -> list[str]:
    return [n for n in notes if not _JUNK_NOTE.search(n)]


def apply_note_changes(mem: AssistantMemory, raw: str, char: str, user: str,
                       now: datetime | None = None) -> None:
    """Apply a fold's changes to the notes (Memory Phase 7, #17). A note the fold doesn't name
    stays as it is: folding chat B can no longer drop a thread only chat A knew about, which
    rewriting the whole list did. A plain line where a change was asked for is read as NEW
    unless it restates a note already there (a model falling back to rewriting the list)."""
    now_s = (now or datetime.now()).isoformat(timespec="seconds")
    notes = list(mem.notes)
    known = [d.text for d in mem.directives] + [f.text for f in mem.facts]
    done_idx: set[int] = set()
    edits: dict[int, str] = {}
    new: list[str] = []
    for line in raw.splitlines():
        line = _clean(line, NOTE_MAX_CHARS + 12)
        if not line or line.upper().startswith("NONE"):
            continue
        m = _OP.match(line)
        if m:
            op, num, text = m.group(1).upper(), m.group(2), m.group(3).strip()
            i = int(num) - 1 if num else -1
            if op == "DONE" and 0 <= i < len(notes):
                done_idx.add(i)
            elif op == "EDIT" and 0 <= i < len(notes) and text:
                edits[i] = text[:NOTE_MAX_CHARS]
            elif op == "NEW" and text:
                new.append(text[:NOTE_MAX_CHARS])
        else:
            new.append(line[:NOTE_MAX_CHARS])
    # Filters for what the model writes: status lines, junk, restated instructions or facts.
    def keep(t: str) -> bool:
        return bool(_junk_notes(_drop_status_notes([t], char, user))) and bool(_drop_restated([t], known))
    out, touched = [], dict(mem.notes_touched)
    for i, n in enumerate(notes):
        if i in done_idx:
            touched.pop(n, None)
            continue
        if i in edits and keep(edits[i]):
            touched.pop(n, None)
            n = edits[i]
            touched[n] = now_s
        out.append(n)
    for t in new:
        if not keep(t):
            continue
        if any(len(_content(t) & _content(x)) / max(1, min(len(_content(t)), len(_content(x)))) >= 0.6
               for x in out if _content(x)):
            continue                                  # a restatement of a thread already there
        out.append(t)
        touched[t] = now_s
    if len(out) > MAX_NOTES:                          # over the cap: the stalest go
        out = sorted(out, key=lambda n: touched.get(n, mem.updated_at))[-MAX_NOTES:]
    mem.notes = out
    mem.notes_touched = {n: touched.get(n, mem.updated_at) for n in out}


def live_notes(mem: AssistantMemory, now: datetime | None = None) -> list[str]:
    """The notes for the prompt: those touched in the last NOTE_STALE_DAYS."""
    now = now or datetime.now()
    out = []
    for n in mem.notes:
        try:
            t = datetime.fromisoformat(mem.notes_touched.get(n) or mem.updated_at)
        except ValueError:
            t = now
        if t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        if (now - t).days < NOTE_STALE_DAYS:
            out.append(n)
    return out


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


# Fortuna's "nothing to remember" at this confidence or more leaves a user turn out of the
# extraction (eval/memory_decisions, 2026-11-23: 31 % of turns, none wrongly; the two real
# instructions it doubted scored 0.63 and 0.83, so they still reach the extractor).
GATE_MIN = 0.9
_GATE_LABELS = {
    "instruction": "a standing instruction for how the assistant should behave from now on",
    "fact": "a lasting fact about the user or their life",
    "event": "something time-bound about the user: a plan, appointment or visit",
    "nothing": "nothing worth remembering: small talk, a one-off request, a question, a passing mood",
}


async def worth_reading(user_turns: list) -> list:
    """The user turns the extractor should read: all of them, less those Fortuna is sure hold
    nothing. KoboldCPP invented 21 standing instructions from 48 such turns × 3 ("Play some
    jazz", "Good night Mira"); a turn it never sees, it cannot mine. Fortuna off or down:
    every turn, as before."""
    from services import fortuna

    keep = []
    for m in user_turns:
        got = await fortuna.choose(m.content, "What, if anything, should the assistant remember "
                                   "long-term from this message from the user?", _GATE_LABELS)
        if got is not None and got[0] == "nothing" and got[1] >= GATE_MIN:
            continue
        keep.append(m)
    if len(keep) < len(user_turns):
        print(f"[assistant-memory] gate: {len(user_turns) - len(keep)} of {len(user_turns)} "
              "user turn(s) hold nothing to remember", flush=True)
    return keep


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
        users = [m for m in session.messages[lo:end] if m.role == "user"]
        turns = await worth_reading(users) if extract_items else users
        if users and not turns:
            # Fortuna is sure no user turn here holds anything: small talk. The notes stand
            # as they are and no model time is spent (a fold here took ~50 s and returned
            # junk notes). Fortuna down or unsure keeps every turn, so this never fires.
            done = lo = end
            continue
        prompt = NOTES_PROMPT.format(char=char, user=user,
                                     notes="\n".join(f"{i}. {n}" for i, n in enumerate(mem.notes, 1)) or "(none)",
                                     transcript=transcript)
        try:
            raw = await kobold.generate_answer(prompt, {
                "max_length": max_tokens, "temperature": 0.3, "top_p": 0.9, "rep_pen": 1.05})
        except Exception:
            break
        apply_note_changes(mem, raw or "", char, user)
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
            said = _transcript(turns, char, user)
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
          budget_chars: int = BUDGET_CHARS, now: datetime | None = None) -> str:
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
    notes = [n.strip() for n in live_notes(mem, now) if n.strip()]
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
