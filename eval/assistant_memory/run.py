#!/usr/bin/env python3
"""Score services/assistant_memory.py against labelled fixtures.

Why this exists. Every prompt in that module was tuned by reading one run and
deciding it looked better. That is guessing with extra steps: two consecutive
folds over the same transcript with the same code disagreed on the directives —
one found a plausible standing instruction, the next filed "Say that again." as
one. A single sample cannot tell a prompt improvement from the model's variance.

So each fixture is run REPEATEDLY and reported as a rate, not a verdict. A change
that moves a rate from 1/3 to 3/3 is evidence; a change that moves one run is not.

What is measured, in the order the failures actually cost you:

  MISFILED   a standing instruction stored as a fact. The worst outcome and the
             reason the pipeline is split: an instruction that is merely known is
             an instruction that gets ignored.
  FORBIDDEN  something that must never be stored — a clock reading, work in
             progress, the assistant's own status. These persist and are injected
             into every later prompt, so a false positive outlives its truth.
  MISSED     something durable that was not captured. Recoverable: the user can
             say it again, or add it in the Memory modal.

Run inside the container, which has the model:
    podman exec -i companion python3 /app/eval/assistant_memory/run.py --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, "/app" if os.path.isdir("/app/services") else
                str(pathlib.Path(__file__).resolve().parents[2]))

from models.schema import (AssistantMemory, CharacterCard, ChatMessage,  # noqa: E402
                           ChatSession, Persona)
from services import assistant_memory as am  # noqa: E402
from services.kobold import KoboldClient  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent


def matches(text: str, groups: list[list[str]]) -> bool:
    """Every group must contribute at least one synonym.

    Deliberately keyword-based, not embeddings: the harness must not depend on an
    optional model being present to say whether the pipeline works, and these
    strings are short and literal. The cost is that labels have to list the
    wordings a model plausibly uses — which is a fixture-authoring job, and an
    honest one.
    """
    low = text.lower()
    return all(any(s.lower() in low for s in group) for group in groups)


def found(items: list[str], spec: dict) -> bool:
    return any(matches(i, spec["all"]) for i in items)


def hits(items: list[str], pattern: str) -> list[str]:
    rx = re.compile(pattern, re.I)
    return [i for i in items if rx.search(i)]


async def one_run(kob, fixture: dict, card, persona) -> dict:
    s = ChatSession(character_id=card.id, persona_id=persona.id)
    for role, text in fixture["messages"]:
        s.messages.append(ChatMessage(role=role, content=text))
    mem = AssistantMemory(character_id=card.id, persona_id=persona.id)
    await am.fold(kob, mem, s, card, persona)
    return {"directives": [d.text for d in mem.directives],
            "facts": [f.text for f in mem.facts],
            "notes": list(mem.notes)}


def score(out: dict, fixture: dict) -> dict:
    exp, forbid, misfile = (fixture.get("expect") or {}, fixture.get("forbid") or {},
                            fixture.get("misfile") or {})
    every = out["directives"] + out["facts"] + out["notes"]
    r = {"missed": [], "forbidden": [], "misfiled": []}

    for bucket in ("directives", "facts", "notes"):
        for spec in exp.get(bucket, []):
            if not found(out[bucket], spec):
                r["missed"].append(f"{bucket}: {spec['label']}")
    for bucket, items in (("directives", out["directives"]), ("facts", out["facts"]),
                          ("notes", out["notes"]), ("any", every)):
        for spec in forbid.get(bucket, []):
            for bad in hits(items, spec["pattern"]):
                r["forbidden"].append(f"{bucket}: {spec['label']} -> {bad[:60]!r}")
    for spec in misfile.get("directives_as_facts", []):
        if found(out["facts"], spec):
            r["misfiled"].append(spec["label"])
    return r


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3, help="folds per fixture (variance is the point)")
    ap.add_argument("--fixture", default="", help="substring filter")
    ap.add_argument("--show", action="store_true", help="print what each run extracted")
    args = ap.parse_args()

    root = glob.glob("/app/data/u/*")[0] if os.path.isdir("/app/data") else glob.glob("data/u/*")[0]
    card = next(CharacterCard.model_validate_json(open(p).read())
                for p in sorted(glob.glob(f"{root}/characters/*.json")))
    persona = Persona.model_validate_json(
        open(glob.glob(f"{root}/personas/*.json")[0]).read())
    kob = KoboldClient(os.environ.get("KOBOLD_URL", "http://koboldcpp:5001"))

    files = sorted(HERE.glob("fixtures/*.json"))
    if args.fixture:
        files = [f for f in files if args.fixture in f.name]
    print(f"assistant-memory eval — {len(files)} fixtures x {args.runs} runs, "
          f"card={card.name}, persona={persona.name}\n")

    totals = {"missed": 0, "forbidden": 0, "misfiled": 0, "clean": 0, "n": 0}
    for path in files:
        fx = json.loads(path.read_text())
        print(f"{'=' * 70}\n{fx['name']}\n  {fx['why']}\n{'=' * 70}")
        per = []
        for i in range(args.runs):
            out = await one_run(kob, fx, card, persona)
            r = score(out, fx)
            per.append(r)
            totals["n"] += 1
            for k in ("missed", "forbidden", "misfiled"):
                totals[k] += bool(r[k])
            if not any(r.values()):
                totals["clean"] += 1
            flag = ("clean" if not any(r.values())
                    else " ".join(k.upper() for k in ("misfiled", "forbidden", "missed") if r[k]))
            print(f"  run {i + 1}: {flag}")
            if args.show:
                for b in ("directives", "facts", "notes"):
                    for it in out[b]:
                        print(f"      {b[:4]}: {it[:88]}")
            for k in ("misfiled", "forbidden", "missed"):
                for detail in r[k]:
                    print(f"      {k}: {detail}")
        clean = sum(1 for r in per if not any(r.values()))
        print(f"  -> clean {clean}/{args.runs}\n")

    n = totals["n"] or 1
    print(f"{'=' * 70}\nTOTAL over {n} runs")
    print(f"  clean          {totals['clean']:>3}/{n}")
    print(f"  misfiled       {totals['misfiled']:>3}/{n}   (instruction stored as a fact)")
    print(f"  forbidden      {totals['forbidden']:>3}/{n}   (stored something that must never persist)")
    print(f"  missed         {totals['missed']:>3}/{n}   (durable item not captured)")
    print("\n  Record this line with any prompt change. A rate that moves is evidence;\n"
          "  a single run that looks better is not.")
    await kob.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
