#!/usr/bin/env python3
"""Score assistant-mode REPLIES against patterns that must never appear.

The memory eval next door checks what gets stored. This checks what gets said,
because the failures are different: an assistant that invents a room it is
standing in, describes the temperature, offers further assistance at the end of
every turn, and asks after your day is not narrating — it is padding, and none of
the memory fixtures would notice.

Same discipline as the memory eval, for the same reason: each prompt is run
REPEATEDLY and reported as a rate. A prompt change that moves a rate is evidence;
one run that reads better is the mistake this directory exists to stop.

    podman cp eval companion:/app/eval
    podman exec -i companion python3 /app/eval/assistant_replies/run.py --runs 3
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

from models.schema import (AppSettings, CharacterCard, ChatMessage,  # noqa: E402
                           ChatSession, Persona)
from services import expressions, prompt_builder  # noqa: E402
from services.kobold import KoboldClient  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent


async def reply_to(kob, card, persona, settings, text: str, max_tokens: int) -> str:
    """Build the prompt exactly as chat() does for an assistant, then generate.

    No relationship note and no boundary note: those are gated off for assistant
    cards, and passing them here would measure a configuration nobody runs."""
    s = ChatSession(character_id=card.id, persona_id=persona.id)
    s.messages = [ChatMessage(role="user", content=text)]
    msgs = prompt_builder.build_messages(
        card, persona, s, expr_labels=sorted(card.expressions.keys()))
    if settings.disable_thinking:
        for m in reversed(msgs):
            if m["role"] == "user":
                m["content"] += " /no_think"
                break
    acc = ""
    async for tok in kob.chat_generate_stream(msgs, {
            "max_tokens": max_tokens, "temperature": 0.7, "top_p": 0.9, "rep_pen": 1.05}):
        if isinstance(tok, str):
            acc += tok
    # Mirror chat()'s post-processing, or the probe measures the raw model rather
    # than the reply a user actually sees.
    out = expressions.strip_bare_labels(acc.strip(), card.expressions.keys())
    return prompt_builder.strip_initiative_question(out)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=220)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    probe = json.loads((HERE / "probe.json").read_text())
    rules = [(f["id"], re.compile(f["pattern"], re.I)) for f in probe["forbid"]]
    cap = probe["max_chars"]

    root = glob.glob("/app/data/u/*")[0] if os.path.isdir("/app/data") else glob.glob("data/u/*")[0]
    card = next(CharacterCard.model_validate_json(open(p).read())
                for p in glob.glob(f"{root}/characters/*.json")
                if json.load(open(p)).get("chat_mode") == "assistant"
                and json.load(open(p)).get("expressions"))
    persona = Persona.model_validate_json(
        open(glob.glob(f"{root}/personas/*.json")[0]).read())
    settings = next((AppSettings.model_validate_json(open(p).read())
                     for p in glob.glob(f"{root}/settings/app.json")), AppSettings())
    kob = KoboldClient(os.environ.get("KOBOLD_URL", "http://koboldcpp:5001"))

    print(f"assistant-reply probe — {len(probe['prompts'])} prompts x {args.runs} runs, "
          f"card={card.name}\n")
    tally = {rid: 0 for rid, _ in rules}
    tally["over-length"] = 0
    total = clean = 0

    for prompt in probe["prompts"]:
        print(f"{'=' * 70}\n> {prompt}\n{'=' * 70}")
        for i in range(args.runs):
            out = await reply_to(kob, card, persona, settings, prompt, args.max_tokens)
            total += 1
            flags = [rid for rid, rx in rules if rx.search(out)]
            if len(out) > cap:
                flags.append("over-length")
            for f in flags:
                tally[f] += 1
            if not flags:
                clean += 1
            print(f"  run {i + 1}: {len(out):>3}ch  {', '.join(flags) or 'clean'}")
            if args.show or flags:
                print(f"      {out[:200]}")
        print()

    print(f"{'=' * 70}\nTOTAL over {total} replies")
    print(f"  clean                     {clean:>3}/{total}")
    for rid in tally:
        print(f"  {rid:<25} {tally[rid]:>3}/{total}")
    print("\n  Record this block with any prompt change.")
    await kob.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
