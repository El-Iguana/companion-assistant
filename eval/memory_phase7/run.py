"""Memory Phase 7 scenarios (#15): memory that belongs to the bartender, across chats and time.

Each scenario is a short story run through the app's own code on a scratch library:
- chats with one bartender or another, saved and folded the way a chat ending folds them
  (``main.sweep_session``, the real model);
- facts seeded straight into the store where a model fold would only add noise;
- checks of what a bartender's memory puts in her prompt (``main.memory_for_turn``) for a
  new chat, a question, and a day.

Every later Phase 7 step is measured here; the baseline is recorded before any of them.

    podman cp eval companion-assistant:/app/eval
    podman exec companion-assistant python3 /app/eval/memory_phase7/run.py [--runs 3]
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
# A scratch library: this must never touch the real one. Set before main is imported.
os.environ["PANTHEON_DATA_DIR"] = tempfile.mkdtemp(prefix="memory-phase7-")
# The code tree this runner sits in: /app in the image, or a copy staged beside it to measure
# a branch before it's merged.
sys.path.insert(0, str(HERE.parent.parent))

import main  # noqa: E402
from models.schema import CharacterCard, ChatMessage, ChatSession, MemoryFact, User  # noqa: E402
from services import auth  # noqa: E402

PERSONA = "Brian"


def _library():
    salt = auth.new_salt()
    u = User(username=f"eval{os.urandom(3).hex()}", role="user", salt=salt,
             password_hash=auth.hash_password("x", salt))
    main._save_user(u)
    cur = main._ctx_for(u)
    persona = main._default_persona(cur)
    persona.name = PERSONA
    main._save(persona, cur.personas)
    return cur, persona


def _card(cur, cards: dict, name: str) -> CharacterCard:
    if name not in cards:
        c = CharacterCard(name=name)
        main._save(c, cur.cards)
        cards[name] = c
    return cards[name]


async def run_scenario(sc: dict) -> list[tuple[str, bool, str]]:
    """-> [(check label, passed, why)]"""
    cur, persona = _library()
    cards: dict = {}
    out = []
    for step in sc["steps"]:
        if "chat" in step:
            card = _card(cur, cards, step["chat"])
            s = ChatSession(character_id=card.id, persona_id=persona.id,
                            messages=[ChatMessage(role=r, content=t) for r, t in step["messages"]])
            main._save(s, cur.sessions)
            if step.get("fold", True):
                await main.sweep_session(s.id, cur=cur)
        elif "seed" in step:
            card = _card(cur, cards, step["seed"]["bartender"])
            mem = main._load_assistant_memory(cur, card.id, persona.id)
            mem.facts += [MemoryFact(text=t, origin="user") for t in step["seed"]["facts"]]
            main._save_assistant_memory(cur, mem)
        elif "check" in step:
            ck = step["check"]
            card = _card(cur, cards, ck["bartender"])
            # A new chat whose only message is the question: what she knows comes from memory.
            s = ChatSession(character_id=card.id, persona_id=persona.id,
                            messages=[ChatMessage(role="user", content=ck["user_says"])])
            when = datetime.now() + timedelta(days=ck.get("at_days", 0))
            got = main.memory_for_turn(cur, card, persona, s, now=when)
            text = (got.get("system") or "") + "\n" + (got.get("trailing") or "")
            where = f"{ck['bartender']}, day {ck.get('at_days', 0)}"
            for e in ck.get("expect", []):
                ok = re.search(e["pattern"], text, re.I) is not None
                out.append((f"{where}: has {e['label']}", ok, "" if ok else "missing"))
            for f in ck.get("forbid", []):
                m = re.search(f["pattern"], text, re.I)
                out.append((f"{where}: not {f['label']}", m is None,
                            "" if m is None else f"found {text[max(0, m.start() - 30):m.end() + 30]!r}"))
    return out


async def main_() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    files = sorted(glob.glob(str(HERE / "scenarios" / "*.json")))
    total = passed = 0
    summary = []
    for f in files:
        sc = json.loads(Path(f).read_text())
        if a.only and a.only not in sc["name"]:
            continue
        print(f"\n{'=' * 70}\n{sc['name']} ({sc.get('issue', '')})\n  {sc['why']}\n{'=' * 70}")
        sc_pass = 0
        for run in range(a.runs):
            results = await run_scenario(sc)
            ok = all(r[1] for r in results)
            sc_pass += ok
            print(f"  run {run + 1}: {'pass' if ok else 'FAIL'}")
            for label, good, why in results:
                if not good:
                    print(f"      {label}: {why}")
        summary.append(f"{sc['name']} {sc_pass}/{a.runs}")
        total += a.runs
        passed += sc_pass
    print(f"\n{'=' * 70}\nTOTAL {passed}/{total}   " + " · ".join(summary))


if __name__ == "__main__":
    asyncio.run(main_())
