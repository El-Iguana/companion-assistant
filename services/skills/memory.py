"""Memory commands on the Menu (Memory Phase 7, #22): "remember…", "forget…" and "what do you
remember about me?" act at once, by tool calling, instead of waiting for the next fold.

The tools write the bartender's store directly (the same one the Memory modal edits), so a
change is there the next time the modal opens. What the user asks to remember is theirs:
``origin: user``, so no fold evicts it. What they ask to forget is removed and kept as a short
"forgotten" list the extractor treats as already known, so the next fold of the same chat
doesn't write it straight back.

Remember and forget answer with a fixed line (``final``): no second model pass, so a memory
command costs one pass like any other reply. "What do you remember" hands the entries to the
model, which answers in her own words.

The store lives in ``main`` (``_load_assistant_memory`` / ``_save_assistant_memory``); ``main``
binds it here at import, so this module needs no import of ``main``.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Callable

from models.schema import AssistantMemory, MemoryFact
from services import assistant_memory as am
from services.skills import Skill, SkillContext, Tool, register

# Bound by main: (ctx) -> the bartender's memory view, and (ctx, mem) -> save it.
_load: Callable[[SkillContext], AssistantMemory] | None = None
_save: Callable[[SkillContext, AssistantMemory], None] | None = None

FORGET_MAX = 3            # more matches than this is "be more specific", not a mass delete
FORGOTTEN_MAX = 30        # tombstones kept for the extractor
LIST_CHARS = 1500         # what "what do you remember" sends the model (RESULT_MAX_CHARS is 2000)


def bind(load: Callable[[SkillContext], AssistantMemory],
         save: Callable[[SkillContext, AssistantMemory], None]) -> None:
    global _load, _save
    _load, _save = load, save


def _key(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def _store(ctx: SkillContext) -> AssistantMemory:
    if _load is None or not ctx.card_id:
        raise RuntimeError("memory store not bound")
    return _load(ctx)


def _unforget(mem: AssistantMemory, text: str) -> None:
    k = _key(text)
    mem.forgotten = [t for t in mem.forgotten if _key(t) != k]


async def _remember(ctx: SkillContext, args: dict) -> dict:
    text = args["text"].strip().rstrip(".")[:am.FACT_MAX_CHARS]
    instruction = args.get("kind") == "instruction"
    everyone = bool(args.get("everyone")) and instruction
    mem = _store(ctx)
    bucket = mem.directives if instruction else mem.facts
    k = _key(text)
    same = next((x for x in bucket if _key(x.text) == k), None)
    _unforget(mem, text)
    if same is not None:
        # Already known: now it is the user's own, so no fold evicts it.
        same.origin = "user"
        if everyone:
            same.scope = "all"
        mem.updated_at = datetime.now().isoformat()
        _save(ctx, mem)
        return {"remembered": same.text, "already_known": True, "final": "I already have that."}
    item = MemoryFact(text=text, origin="user", scope="all" if everyone else "")
    if not instruction:
        item.kind, item.expires = await am.lifetime(text)
    bucket.append(item)
    cap = am.MAX_DIRECTIVES if instruction else am.MAX_FACTS
    while len(bucket) > cap:
        idx = next((i for i, f in enumerate(bucket) if f.origin == "auto"), None)
        if idx is None:
            bucket.pop()
            return {"ok": False, "error": "MEMORY_FULL",
                    "message": "Memory is full of things the user wrote; one has to go first (the Memory window)."}
        bucket.pop(idx)
    mem.updated_at = datetime.now().isoformat()
    _save(ctx, mem)
    line = f"Noted: {text}." + (" Every bartender will follow that." if everyone else "")
    return {"remembered": text, "kind": "instruction" if instruction else "fact",
            "for_every_bartender": everyone, "until": item.expires, "final": line}


def _matches(mem: AssistantMemory, about: str) -> list[tuple[str, object]]:
    """Entries ``about`` names: every word of it in the entry, or failing that, the entries
    sharing the most words with it (at least half of them)."""
    said = am._words(about)
    if not said:
        return []
    entries = ([("directives", d) for d in mem.directives] + [("facts", f) for f in mem.facts]
               + [("notes", n) for n in mem.notes])
    scored = []
    for field, e in entries:
        text = e if isinstance(e, str) else e.text
        common = len(said & am._words(text))
        if common:
            scored.append((common, field, e))
    if not scored:
        return []
    full = [(f, e) for c, f, e in scored if c == len(said)]
    if full:
        return full
    best = max(c for c, _, _ in scored)
    if best * 2 < len(said):
        return []
    return [(f, e) for c, f, e in scored if c == best]


async def _forget(ctx: SkillContext, args: dict) -> dict:
    about = args["about"]
    mem = _store(ctx)
    hits = _matches(mem, about)
    if not hits:
        return {"removed": [], "final": "I had nothing about that."}
    texts = [e if isinstance(e, str) else e.text for _, e in hits]
    if len(hits) > FORGET_MAX:
        listed = "; ".join(texts[:6])
        return {"removed": [], "matches": texts[:6],
                "final": f"That matches {len(hits)} things ({listed}). Which should I forget?"}
    for field, e in hits:
        if field == "notes":
            mem.notes = [n for n in mem.notes if n != e]
        else:
            setattr(mem, field, [x for x in getattr(mem, field) if x.id != e.id])
    mem.forgotten = ([t for t in mem.forgotten if _key(t) not in {_key(x) for x in texts}]
                     + texts)[-FORGOTTEN_MAX:]
    mem.updated_at = datetime.now().isoformat()
    _save(ctx, mem)
    said = texts[0] if len(texts) == 1 else "; ".join(texts)
    return {"removed": texts, "final": f"Forgotten: {said}."}


async def _list(ctx: SkillContext, args: dict) -> dict:
    mem = _store(ctx)
    now = datetime.now()
    about = str(args.get("about") or "")
    facts = [f for f in mem.facts if f.text.strip() and not am.expired(f, now)]
    if about and am._words(about):
        picked = am.recall(mem, about, now=now)
        facts = picked or facts
    out: dict = {"instructions": [], "facts": [], "threads": []}
    room = LIST_CHARS
    for field, items in (("instructions", [d.text for d in mem.directives]),
                         ("facts", [f.text for f in facts]),
                         ("threads", am.live_notes(mem, now))):
        for t in items:
            if len(t) + 4 > room:
                out["more"] = True
                break
            out[field].append(t)
            room -= len(t) + 4
    if not any(out[k] for k in ("instructions", "facts", "threads")):
        return {"empty": True, "final": "Nothing yet. Tell me what to remember and I will."}
    return out


REMEMBER = Tool(
    "memory_remember",
    "Remember something the user asked you to remember, at once. A fact about the user, or an "
    "instruction for how to behave from now on.",
    {"type": "object", "properties": {
        "text": {"type": "string", "maxLength": am.FACT_MAX_CHARS,
                 "description": "What to remember, as a statement that stands alone, naming things as the user did"},
        "kind": {"type": "string", "enum": ["fact", "instruction"]},
        "everyone": {"type": "boolean", "description": "An instruction for every bartender, not just you"},
    }, "required": ["text", "kind"]},
    _remember, status="Making a note…", icon="📝")

FORGET = Tool(
    "memory_forget",
    "Forget what the user asks you to forget: removes the matching memories.",
    {"type": "object", "properties": {
        "about": {"type": "string", "maxLength": 200, "description": "What to forget, in the user's words"},
    }, "required": ["about"]},
    _forget, status="Forgetting…", icon="📝")

LIST = Tool(
    "memory_list",
    "What you remember: the user's instructions, facts about them and open threads. Use when "
    "they ask what you remember or know about them.",
    {"type": "object", "properties": {
        "about": {"type": "string", "maxLength": 200, "description": "A topic, if they named one"},
    }},
    _list, status="Checking my notes…", icon="📝")


def _tools(ctx: SkillContext) -> list[Tool]:
    return [REMEMBER, FORGET, LIST] if _load is not None else []


def _note(ctx: SkillContext) -> str:
    return ("Memory: when the user asks you to remember or forget something, call memory_remember "
            "or memory_forget; never say you will remember without calling it. When they ask what "
            "you remember, call memory_list.")


register(Skill("memory", "Memory", "Remember, forget and say what she remembers, when you ask.",
               _tools, note=_note))
