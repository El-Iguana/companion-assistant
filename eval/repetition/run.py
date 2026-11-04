#!/usr/bin/env python3
"""Does a character repeat herself across a chat? Measured, with and without the DRY
sampler.

A companion chat replays its own earlier replies as context, and the user often says
similar things ("Morning", "How's business?"). Pantheon's guards were a prompt rule
("do not repeat phrasing") and rep_pen 1.1, which works on single tokens, not on
reusing whole phrases from earlier turns. KoboldCPP's DRY sampler penalises extending
any sequence that already appears in the context.

Each run plays a scripted 12-turn chat through the real prompt builder and measures,
per reply against all her earlier replies in that chat:
  overlap   share of the reply's 4-word sequences already used in an earlier reply
  repeats   sentences (5+ words) she had already said, word for word
  openings  replies starting with the same first four words as an earlier one
  near-dups replies taking NEAR_DUP or more of their 4-word sequences from ONE earlier
            reply: the "same response again" a user notices, which an average hides

Host-side, against the live backend:
    KOBOLD_URL=http://localhost:5001 .venv/bin/python eval/repetition/run.py --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import re
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
os.environ.setdefault("PANTHEON_DATA_DIR", "/tmp/pantheon-eval-repetition")

from models.schema import CharacterCard, ChatMessage, ChatSession, Persona  # noqa: E402
from services import prompt_builder  # noqa: E402
from services.kobold import KoboldClient  # noqa: E402

SCRIPT = ["Morning, Mira.", "How's business?", "Tell me about the regulars.",
          "Any news from the road?", "I'll have the usual.", "Thanks, Mira.",
          "It's quiet today.", "Morning again, Mira.", "How's business today?",
          "Same as yesterday, then?", "I'll have the usual.", "Thanks, Mira."]

CARD = CharacterCard(
    name="Mira", description="Mira runs the Gull and Lantern, a harbour inn. Warm, quick-witted, "
    "a little nosy, fond of her regulars.", personality="warm, teasing, practical",
    scenario="Eli is a regular at the inn.", first_mes="*Mira looks up from the bar.* \"Back again?\"")
PERSONA = Persona(name="Eli")

_WORDS = re.compile(r"[a-z']+")


def _grams(text, n=4):
    w = _WORDS.findall(text.lower())
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def _sentences(text):
    return {" ".join(_WORDS.findall(s.lower())) for s in re.split(r"(?<=[.!?])\s+|\n", text)
            if len(_WORDS.findall(s)) >= 5}


NEAR_DUP = 0.25


def score(replies):
    overlaps, repeats, openings = [], 0, 0
    near = sum(1 for i, r in enumerate(replies) if _grams(r) and any(
        len(_grams(r) & _grams(p)) / len(_grams(r)) >= NEAR_DUP for p in replies[:i]))
    seen_g, seen_s, seen_o = set(), set(), set()
    for r in replies:
        g = _grams(r)
        if seen_g and g:
            overlaps.append(len(g & seen_g) / len(g))
        repeats += len(_sentences(r) & seen_s)
        opening = tuple(_WORDS.findall(r.lower())[:4])
        openings += opening in seen_o
        seen_g |= g
        seen_s |= _sentences(r)
        seen_o.add(opening)
    return {"overlap": statistics.mean(overlaps) if overlaps else 0.0,
            "repeats": repeats, "openings": openings, "near": near}


async def play(kob, extra: dict) -> list[str]:
    s = ChatSession(character_id=CARD.id, persona_id=PERSONA.id)
    s.messages.append(ChatMessage(role="char", content=CARD.first_mes))
    replies = []
    for line in SCRIPT:
        s.messages.append(ChatMessage(role="user", content=line))
        msgs = prompt_builder.build_messages(CARD, PERSONA, s)
        params = {**prompt_builder.kobold_params(s), **extra}
        text = "".join([t async for t in kob.chat_generate_stream(msgs, params)]).strip()
        s.messages.append(ChatMessage(role="char", content=text))
        replies.append(text)
    return replies


# Re-roll: "Another take" drops the reply being replaced from the history, so the model
# has nothing to differ from. Measured as how much take 2 reuses take 1.
HISTORY = [("user", "Morning, Mira."),
           ("char", "*Mira looks up from polishing a tankard and grins.* \"Eli! You're up early. Kettle's on.\""),
           ("user", "How's business?"),
           ("char", "*She sets the tankard down.* \"Slow, but the fishing boats are due back tonight. Then it'll be chaos.\""),
           ("user", "Tell me about the regulars.")]
RETAKE = {
    "baseline": ({}, False),
    "note": ({}, True),
    "note+dry": ({"dry_multiplier": 0.8, "dry_base": 1.75, "dry_allowed_length": 2,
                  "dry_penalty_last_n": 0}, True),
}


async def retake(kob, extra: dict, note: bool) -> tuple[str, str]:
    s = ChatSession(character_id=CARD.id, persona_id=PERSONA.id)
    s.messages = [ChatMessage(role=r, content=c) for r, c in HISTORY]
    params = {**prompt_builder.kobold_params(s), **extra}
    first = "".join([t async for t in kob.chat_generate_stream(
        prompt_builder.build_messages(CARD, PERSONA, s), params)]).strip()
    msgs = prompt_builder.build_messages(CARD, PERSONA, s, retake_of=[first] if note else None)
    second = "".join([t async for t in kob.chat_generate_stream(msgs, params)]).strip()
    return first, second


CONDITIONS = {
    "baseline": {},
    "dry": {"dry_multiplier": 0.8, "dry_base": 1.75, "dry_allowed_length": 2,
            "dry_penalty_last_n": 0},
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", default="", help="condition name")
    ap.add_argument("--part", default="all", choices=["all", "chat", "retake"],
                    help="chat = repetition across a chat, retake = Another take")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    kob = KoboldClient(os.environ.get("KOBOLD_URL", "http://localhost:5001"))
    print(f"repetition eval — {len(SCRIPT)} turns x {args.runs} runs per condition\n")
    totals = {}
    for name, extra in CONDITIONS.items():
        if args.only and name != args.only or args.part == "retake":
            continue
        runs = []
        for i in range(args.runs):
            replies = await play(kob, extra)
            sc = score(replies)
            runs.append(sc)
            print(f"  {name:<9} run {i + 1}: overlap {sc['overlap']:.2f}  repeated sentences "
                  f"{sc['repeats']:>2}  repeated openings {sc['openings']:>2}  near-dups {sc['near']:>2}",
                  flush=True)
            if args.show:
                for r in replies:
                    print("      | " + " ".join(r.split())[:140])
        totals[name] = runs
    takes = {}
    for name, (extra, note) in RETAKE.items():
        if args.only and name != args.only or args.part == "chat":
            continue
        rows = []
        for i in range(args.runs):
            a, b = await retake(kob, extra, note)
            ga, gb = _grams(a), _grams(b)
            ov = len(ga & gb) / len(gb) if gb else 0.0
            same_open = _WORDS.findall(a.lower())[:4] == _WORDS.findall(b.lower())[:4]
            rows.append((ov, same_open))
            print(f"  retake {name:<9} run {i + 1}: take-2 reuses {ov:.2f} of take 1"
                  f"{'  SAME OPENING' if same_open else ''}", flush=True)
            if args.show:
                print("      1| " + " ".join(a.split())[:140] + "\n      2| " + " ".join(b.split())[:140])
        takes[name] = rows
    print("\nTOTAL (mean over runs)")
    for name, runs in totals.items():
        print(f"  {name:<9} overlap {statistics.mean(r['overlap'] for r in runs):.2f}  "
              f"repeated sentences {statistics.mean(r['repeats'] for r in runs):.1f}  "
              f"repeated openings {statistics.mean(r['openings'] for r in runs):.1f}  "
              f"near-dups {statistics.mean(r['near'] for r in runs):.1f}")
    for name, rows in takes.items():
        print(f"  retake {name:<9} take 2 reuses {statistics.mean(r[0] for r in rows):.2f} of take 1, "
              f"same opening {sum(r[1] for r in rows)}/{len(rows)}")
    await kob.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
