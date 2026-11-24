"""The memory fold's decision, KoboldCPP against Fortuna (Memory Phase 7 #15, Fortuna part).

One question per user turn: what, if anything, should the assistant remember from it, as an
instruction, a fact, a time-bound event, or nothing. The fold answers it implicitly today, in
a KoboldCPP generation that extracts lines. Answered explicitly first, "nothing" could skip
that generation, and small talk could never invent a memory (this eval's predecessor found
exactly that: "Address him as Sir" mined from small talk).

    podman cp eval companion-assistant:/app/eval
    podman exec companion-assistant python3 /app/eval/memory_decisions/run.py [--runs 3]

Reports per engine: accuracy against labels that allow every fair answer, invented (said
something where only "nothing" is right: the costly error, since a memory goes into every
later prompt), missed (said nothing where something was there), and latency.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

HERE = Path(__file__).resolve().parent
LABELS = {
    "instruction": "a standing instruction for how the assistant should behave from now on",
    "fact": "a lasting fact about the user or their life",
    "event": "something time-bound about the user: a plan, appointment or visit",
    "nothing": "nothing worth remembering: small talk, a one-off request, a question, a passing mood",
}
QUESTION = "What, if anything, should the assistant remember long-term from this message from the user?"
KOBOLD_PROMPT = (
    "You decide what an assistant should remember long-term from one message the user said.\n"
    "Choose exactly one word:\n"
    + "".join(f"- {k}: {v}\n" for k, v in LABELS.items())
    + "\nMessage: {text}\n\nAnswer with only the word.\nAnswer:"
)


async def ask_kobold(kobold, text: str) -> tuple[str, float]:
    t0 = time.monotonic()
    # Room to write a short sentence ("This message is a fact."): with 6 tokens the model was
    # cut off before the label and the first run scored it on "the" and "this". The first
    # label it names is its answer, as a fair reading of what it said.
    out = await kobold.generate_answer(KOBOLD_PROMPT.replace("{text}", text),
                                       {"max_length": 40, "temperature": 0.0, "top_p": 1.0, "rep_pen": 1.0})
    low = (out or "").lower()
    hits = sorted((low.find(k), k) for k in LABELS if k in low)
    return (hits[0][1] if hits else "?" + low.strip()[:20]), time.monotonic() - t0


async def ask_fortuna(text: str) -> tuple[str, float, float]:
    from services import fortuna

    t0 = time.monotonic()
    got = await fortuna.choose(text, QUESTION, LABELS)
    if got is None:
        return "?down", 0.0, time.monotonic() - t0
    return got[0], got[1], time.monotonic() - t0


def score(rows: list[dict], key: str) -> dict:
    right = sum(r[key] in r["ok"] for r in rows)
    invented = sum(r["ok"] == ["nothing"] and r[key] != "nothing" for r in rows)
    missed = sum("nothing" not in r["ok"] and r[key] == "nothing" for r in rows)
    return {"right": right, "of": len(rows), "invented": invented, "missed": missed}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    a = ap.parse_args()
    from services.kobold import KoboldClient

    kobold = KoboldClient(os.environ.get("KOBOLD_URL", "http://koboldcpp:5001"))
    turns = [json.loads(line) for line in (HERE / "turns.jsonl").read_text().splitlines() if line.strip()]
    all_rows, kt, ft = [], [], []
    for run in range(a.runs):
        for t in turns:
            k, ks = await ask_kobold(kobold, t["text"])
            f, conf, fs = await ask_fortuna(t["text"])
            kt.append(ks)
            ft.append(fs)
            all_rows.append({**t, "run": run, "kobold": k, "fortuna": f, "conf": round(conf, 2)})
    for name in ("kobold", "fortuna"):
        s = score(all_rows, name)
        times = kt if name == "kobold" else ft
        print(f"{name:8} right {s['right']}/{s['of']}  invented {s['invented']}  missed {s['missed']}  "
              f"median {statistics.median(times) * 1000:.0f} ms  max {max(times) * 1000:.0f} ms")
    print("\nDisagreements and errors (first run):")
    for r in all_rows:
        if r["run"] == 0 and (r["kobold"] not in r["ok"] or r["fortuna"] not in r["ok"]):
            mark = lambda v: v + ("" if v in r["ok"] else " ✗")   # noqa: E731
            print(f"  {r['text'][:60]:60}  want {'/'.join(r['ok']):18} kobold {mark(r['kobold']):14} "
                  f"fortuna {mark(r['fortuna'])} ({r['conf']})")
    # The gate: skip the extraction call when Fortuna says "nothing" this sure.
    print("\nFortuna as a gate (skip the fold's extraction when it says nothing at this confidence):")
    for t in (0.0, 0.5, 0.7, 0.8, 0.9):
        skip = [r for r in all_rows if r["fortuna"] == "nothing" and r["conf"] >= t]
        lost = sum("nothing" not in r["ok"] for r in skip)
        print(f"  conf >= {t:.1f}: skips {len(skip)}/{len(all_rows)} turns, of which {lost} held something")
    (HERE / "last_run.json").write_text(json.dumps(all_rows, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
