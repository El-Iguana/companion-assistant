#!/usr/bin/env python3
"""Score the COMPANION memory prompts in services/memory.py against labelled fixtures.

Why this exists. LONGTERM_PROMPT, AUTO_FACT_PROMPT, the summarize() instruction and
the recall gate were all tuned by reading single runs. eval/assistant_memory showed
what that is worth in this codebase: one run gave the OPPOSITE verdict to three, in
both directions (a fix declared working scored 0/3; a leak recorded as unfixed
scored 3/3). The companion pipeline is about to be moved into a shared package and
then restructured, so it needs a measured baseline first — otherwise an improvement,
a regression and the model's variance all look the same.

So each LLM fixture is run REPEATEDLY and reported as a rate, not a verdict.

What is measured:

  fold       fold_into_longterm(extract_facts=True) — LONGTERM_PROMPT + AUTO_FACT_PROMPT.
             Stages are separate sessions folded into the same CharacterMemory, so a
             later stage tests the rewrite (cancellation, erosion).
  summarize  summarize() — the per-session rolling summary. Stages append to the same
             session and fold again (existing memory + new events).
  recall     index_add()/index_search() in a temp dir. Deterministic given the
             embedder, so it runs once whatever --runs says.

Counters for the LLM fixtures, in the order the failures cost you:

  FORBIDDEN  stored something that must never persist: a character's own
             acknowledgement, an inferred trait, interpretation, a scheduled plan as a
             durable fact, a plan kept after its cancellation, small talk. Persists and
             is injected into every later prompt.
  FORMAT     the shape the prompt asks for was not produced: more bullets than the cap
             (LONGTERM_MAX_BULLETS, or a smaller fixture cap — padding), prose/heading
             lines in a bullet memory, a summary without exactly one final "Where
             things stand:" line, paragraph-length story prose.
  MISSED     something durable that is in neither the summary nor the facts.
             Recoverable: the user can say it again or add it in the Memory modal.

Every fold fixture also gets GLOBAL checks (below: GLOBAL_FOLD_FORBID etc.), because
each rule in those prompts is a failure that was once observed and can come back in
any transcript, not only the one written to provoke it.

Fixtures marked "informational" measure capabilities the code does not have by
design yet (the character's own canon, relationship grounding). They are reported
as capture rates on their own and never counted in the TOTAL.

Matching is keyword groups and regexes, not embeddings — see matches().

Run inside the container, which has the model and the embedder:
    podman cp eval companion:/app/eval
    podman exec -i companion python3 /app/eval/companion_memory/run.py --runs 3
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import time
import urllib.request

import numpy as np

sys.path.insert(0, "/app" if os.path.isdir("/app/services") else
                str(pathlib.Path(__file__).resolve().parents[2]))

from models.schema import (CharacterCard, CharacterMemory, ChatMessage,  # noqa: E402
                           ChatSession, Persona)
from services import memory  # noqa: E402

try:                                    # pre-Muninn layout (the baseline image)
    from services import embeddings  # noqa: E402
except ImportError:                     # after Phase 1: the embedder moved into muninn
    from muninn import embeddings  # noqa: E402
from services.kobold import KoboldClient  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
KOBOLD_URL = os.environ.get("KOBOLD_URL", "http://koboldcpp:5001")

# Not in the original failure list: added after the first run of this harness.
# generate_answer() prefills an empty Qwen-style
# <think></think>; a model that does not use that convention reasons in plain
# text, closes with a literal "</think>", and EVERYTHING is stored — the reasoning
# in the summary, and lines like "No new durable facts." or "Output: NONE" as
# auto-facts (the parser only skips lines that START with NONE). Applied to both
# the fold and the session summary.
META_FORBID = {
    "label": "reasoning/meta text stored as memory", "in": "any",
    "pattern": r"</?(think|thought|assistant|response|user|answer)\b|\b(I|I'll|I'm|I've) (need|should|will|"
               r"must|have to|make sure|keep|can|update|write|won't)\b|\blet me\b|updated memory|"
               r"(previous|existing) memory|as instructed|as requested|"
               r"^\W*(here is|here's) (the|my)\b|^\W*new durable facts"}
# The auto-fact parser only skips a line that STARTS with "NONE"; anything else the
# extractor says instead of a fact ("No new durable facts.", "Output: NONE",
# "The conversation contains no durable facts…") is stored as a fact.
NON_FACT_FORBID = {
    "label": "non-fact line stored as a fact", "in": "facts",
    "pattern": r"^\W*(output:|no new|no durable|none\b|there are no|the conversation|this conversation|"
               r"these (details|facts)|anything )"}

# --- global checks -----------------------------------------------------------
# Applied to every stage of every counted fold fixture. {char}/{user} are filled in.
GLOBAL_FOLD_FORBID = [
    # "1800 characters of florid essay ... 'immutable pillars', 'a symbol of stability'"
    {"label": "interpretation", "in": "any",
     "pattern": r"\b(symbol\w*|immutabl\w*|pillars?|testament|embod\w*|signific\w*|profound\w*|"
                r"unwavering|steadfast|beacon|cornerstone|tapestry|essence)\b"},
    # "MemTest confirms retention of all noted personal facts"
    {"label": "character's own reply recorded", "in": "any",
     "pattern": r"\b{char}\b[^\n]*\b(not(ed|es)|stor(ed|es)|remembers?|remembered|will remember|"
                r"acknowledg\w*|confirm(s|ed)|retain\w*|retention)\b"},
    # "Nadia is visiting at the end of summer"
    {"label": "scheduled/transient item as a durable fact", "in": "facts",
     "pattern": r"\b(next (week|month|year|spring|summer|autumn|fall|winter)|tomorrow|tonight|"
                r"this (week|weekend|evening|month)|soon|visiting|coming (over|to|up|round)|"
                r"planning|plans to|is going to|later today)\b"},
    META_FORBID,
    NON_FACT_FORBID,
    # Also first seen in this harness: the transcript itself pasted in as "memory"
    # ("Jonah: I hate coriander", "Mira: *waves* \"Hey yourself.\"").
    {"label": "transcript copied as memory", "in": "summary",
     "pattern": r"^\W*({char}|{user})\s*:"},
    # "Do NOT extract: anything {char} said, offered, or promised"
    {"label": "auto-fact about the character, not the user", "in": "facts",
     "pattern": r"^(?!.*\b{user}\b).*\b{char}\b"},
]
GLOBAL_SUMMARY_FORBID = [
    META_FORBID,
    # "A few paragraphs, third person, present tense" -> a retelling to imitate.
    {"label": "present-tense story narration", "in": "notes",
     "pattern": r"\b(she|he|they|{char}|{user}) (smiles|grins|leans|whispers|walks|looks|says|nods|"
                r"laughs|sighs|turns|steps|reaches|takes|asks|tells|gazes|glances|murmurs|"
                r"pulls|holds|squeezes)\b"},
]
PROSE_LINE_CHARS = 300   # a "note" longer than this is a paragraph of story prose
BULLET = re.compile(r"^\s*([-*•]|\d+[.)])\s+")
WHERE = re.compile(r"where things stand", re.I)


def matches(text: str, groups: list[list[str]]) -> bool:
    """Every group must contribute at least one synonym.

    Deliberately keyword-based, not embeddings (same choice as the assistant eval):
    the harness must not depend on an optional model to say whether the pipeline
    works, and the labels are short and literal. The cost is that labels must list
    the wordings a model plausibly uses — a fixture-authoring job, done in the open.
    """
    low = text.lower()
    return all(any(s.lower() in low for s in group) for group in groups)


def fill(pattern: str, char: str, user: str) -> str:
    return pattern.replace("{char}", re.escape(char)).replace("{user}", re.escape(user))


# --- running -----------------------------------------------------------------
def wait_for_kobold(max_wait: float = 900) -> str:
    """Block until KoboldCPP answers with a loaded model; return its name."""
    t0 = time.time()
    while True:
        try:
            with urllib.request.urlopen(f"{KOBOLD_URL}/api/v1/model", timeout=10) as r:
                name = json.load(r).get("result", "")
            if name and name.lower() not in ("inactive", "", "none"):
                return name
        except Exception:
            pass
        if time.time() - t0 > max_wait:
            raise SystemExit(f"KoboldCPP at {KOBOLD_URL} not ready after {max_wait:.0f}s")
        print("  (waiting for KoboldCPP …)", flush=True)
        time.sleep(15)


def _session(card, persona, messages) -> ChatSession:
    s = ChatSession(character_id=card.id, persona_id=persona.id)
    for role, text in messages:
        s.messages.append(ChatMessage(role=role, content=text))
    return s


CONSOLIDATE = False


async def run_llm(kob, fx: dict) -> list[dict]:
    """Run every stage; return one snapshot per stage."""
    card = CharacterCard(name=fx.get("char", "Mira"))          # chat_mode default: roleplay
    persona = Persona(name=fx.get("user", "Jonah"))
    snaps = []
    if fx["kind"] == "fold":
        mem = CharacterMemory(character_id=card.id, persona_id=persona.id)
        for st in fx["stages"]:
            s = _session(card, persona, st["messages"])     # each stage = a new chat
            with contextlib.redirect_stdout(io.StringIO()):  # "[auto-facts] +2 for …"
                ok = await memory.fold_into_longterm(
                    kob, mem, s, card, persona, extract_facts=True,
                    max_new_facts=memory.AUTO_FACT_MAX_NEW, consolidate=CONSOLIDATE)
                if CONSOLIDATE and ok:
                    await memory.review_longterm(kob, mem, card, persona)
            snaps.append({"summary": mem.summary, "facts": [f.text for f in mem.facts],
                          "error": not ok})
    else:
        s = _session(card, persona, [])
        for st in fx["stages"]:
            for role, text in st["messages"]:
                s.messages.append(ChatMessage(role=role, content=text))
            before = s.memory.last_summarized_index
            # summarize_after=1, keep_recent=0: fold everything in the stage, so the
            # stage boundary is exactly the fold boundary.
            await memory.summarize(kob, s, card, persona, summarize_after=1, keep_recent=0)
            snaps.append({"summary": s.memory.summary, "facts": [],
                          "error": s.memory.last_summarized_index == before})
    return snaps


# --- scoring -----------------------------------------------------------------
def fields(snap: dict) -> dict:
    lines = [l.strip() for l in (snap["summary"] or "").splitlines() if l.strip()]
    where = [l for l in lines if WHERE.search(l)]
    notes = [l for l in lines if not WHERE.search(l)]
    return {"summary": lines, "facts": snap["facts"], "any": lines + snap["facts"],
            "where": where, "notes": notes}


def check(snap: dict, spec: dict, char: str, user: str, kind: str,
          max_bullets: int, use_globals: bool) -> dict:
    f = fields(snap)
    r = {"forbidden": [], "format": [], "missed": [], "error": []}
    if snap.get("error"):
        # Not a prompt verdict: fold_into_longterm/summarize swallow backend errors
        # (by design — memory never fails a turn), so a model swap or a dead
        # KoboldCPP looks exactly like an empty generation. Counted separately and
        # excluded from the rates, so infrastructure can't masquerade as a result.
        r["error"].append("fold produced nothing (backend error or empty output)")
        return r

    if use_globals and kind == "fold":
        bullets = [l for l in f["summary"] if BULLET.match(l)]
        if len(bullets) > max_bullets:
            r["format"].append(f"{len(bullets)} bullets > cap {max_bullets} (padding)")
        for l in f["summary"]:
            if not BULLET.match(l):
                r["format"].append(f"non-bullet line -> {l[:60]!r}")
    if use_globals and kind == "summarize":
        n = len(f["where"])
        if n != 1:
            r["format"].append(f"{n} 'Where things stand' lines (want exactly 1)")
        elif f["summary"] and not WHERE.search(f["summary"][-1]):
            r["format"].append("'Where things stand' line is not last")
        for l in f["summary"]:
            if len(l) > PROSE_LINE_CHARS:
                r["format"].append(f"paragraph-length line ({len(l)} chars)")

    forbids = list(spec.get("forbid", []))
    if use_globals:
        forbids += GLOBAL_FOLD_FORBID if kind == "fold" else GLOBAL_SUMMARY_FORBID
    for fb in forbids:
        rx = re.compile(fill(fb["pattern"], char, user), re.I)
        unless = re.compile(fill(fb["unless"], char, user), re.I) if fb.get("unless") else None
        for item in f[fb.get("in", "any")]:
            if rx.search(item) and not (unless and unless.search(item)):
                r["forbidden"].append(f"{fb['label']} -> {item[:70]!r}")
    for ex in spec.get("expect", []):
        if not any(matches(i, ex["all"]) for i in f[ex.get("in", "any")]):
            r["missed"].append(ex["label"])
    return r


def score(fx: dict, snaps: list[dict]) -> dict:
    char, user = fx.get("char", "Mira"), fx.get("user", "Jonah")
    cap = min(fx.get("max_bullets", memory.LONGTERM_MAX_BULLETS), memory.LONGTERM_MAX_BULLETS)
    if CONSOLIDATE and fx["kind"] == "fold":
        # The list grows across chats by design; a fixture's own cap still holds
        # (padding), the rewrite's 10-bullet ceiling doesn't.
        cap = fx.get("max_bullets", memory.LONGTERM_KEEP_BULLETS)
    counted = not fx.get("informational")
    total = {"forbidden": [], "format": [], "missed": [], "error": []}
    for i, (st, snap) in enumerate(zip(fx["stages"], snaps)):
        last = i == len(snaps) - 1
        spec = {"forbid": list(st.get("checks", {}).get("forbid", [])),
                "expect": list(st.get("checks", {}).get("expect", []))}
        if last:
            spec["forbid"] += fx.get("checks", {}).get("forbid", [])
            spec["expect"] += fx.get("checks", {}).get("expect", [])
        r = check(snap, spec, char, user, fx["kind"], cap, use_globals=counted)
        tag = f"s{i + 1}: " if len(snaps) > 1 else ""
        for k in total:
            total[k] += [tag + d for d in r[k]]
    return total


# --- recall ------------------------------------------------------------------
def run_recall(fx: dict) -> dict:
    """Both corpus variants: durable turns only, and with small-talk turns mixed in."""
    if not embeddings.available():
        return {"skipped": "embedder not available"}
    variants = [("durable turns only", fx["docs"])]
    if fx.get("noise_docs"):
        variants.append(("with small-talk turns", fx["docs"] + fx["noise_docs"]))
    return {"variants": [(name, _recall_variant(fx, docs)) for name, docs in variants]}


def _recall_variant(fx: dict, docs: list[str]) -> dict:
    user = fx.get("user", "Jonah")
    out = {"docs": len(docs), "rel": 0, "rel_hit": 0, "rel_top1": 0, "rel_wrong": 0,
           "rel_none": 0, "irr": 0, "irr_leak": 0, "rows": [], "margins_rel": [], "margins_irr": []}
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "recall.vec.npz"
        # Same shape fold_into_longterm writes: "{user}: {turn}", kind="turn".
        memory.index_add(path, [{"text": f"{user}: {t}", "kind": "turn"} for t in docs])
        vectors, _ = memory._index_load(path)
        for q in fx["queries"]:
            hits = memory.index_search(path, q["q"])
            qv = embeddings.encode([q["q"]], is_query=True)[0]
            sims = vectors @ qv
            best = float(sims.max())
            margin = best - float(np.median(sims))       # exactly what the gate computes
            texts = [h["text"] for h in hits]
            if q["expect"]:
                out["rel"] += 1
                out["margins_rel"].append(margin)
                ok = [t for t in texts if any(e.lower() in t.lower() for e in q["expect"])]
                if not hits:
                    verdict = "MISS (gated out)"; out["rel_none"] += 1
                elif ok:
                    out["rel_hit"] += 1
                    top1 = any(e.lower() in texts[0].lower() for e in q["expect"])
                    out["rel_top1"] += top1
                    verdict = "hit@1" if top1 else "hit (not top-1)"
                else:
                    verdict = "WRONG (retrieved, none right)"; out["rel_wrong"] += 1
            else:
                out["irr"] += 1
                out["margins_irr"].append(margin)
                verdict = f"LEAK ({len(hits)})" if hits else "ok (nothing)"
                out["irr_leak"] += bool(hits)
            out["rows"].append((q["q"], "rel" if q["expect"] else "irr", best, margin, verdict,
                                texts[0][:50] if texts else ""))
    return out


# --- main --------------------------------------------------------------------
async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3, help="runs per LLM fixture (variance is the point)")
    ap.add_argument("--fixture", default="", help="substring filter on fixture file names")
    ap.add_argument("--show", action="store_true", help="print what each run stored")
    ap.add_argument("--consolidate", action="store_true",
                    help="fold fixtures use consolidation: append-only folds, then the review "
                         "after each stage (each stage is a finished chat, i.e. an idle sweep)")
    ap.add_argument("--dump", default="", help="write every run's raw output to this JSON file")
    ap.add_argument("--rescore", default="", help="score a previous --dump instead of calling the model "
                    "(for label fixes: the outputs stay fixed, only the checks change)")
    args = ap.parse_args()
    global CONSOLIDATE
    CONSOLIDATE = args.consolidate

    files = sorted(HERE.glob("fixtures/*.json"))
    if args.fixture:
        files = [f for f in files if args.fixture in f.name]
    fxs = [(p, json.loads(p.read_text())) for p in files]
    saved = None
    if args.rescore:
        saved = json.loads(pathlib.Path(args.rescore).read_text())
        if isinstance(saved, list):                 # first-version dumps: runs only
            saved = {"model": "unknown model", "runs": saved}
        model = saved["model"] + " (rescored from dump)"
        saved_runs = collections.defaultdict(list)
        for row in saved["runs"]:
            saved_runs[row["fixture"]].append(row["stages"])
        args.runs = max((len(v) for v in saved_runs.values()), default=0)
    else:
        model = wait_for_kobold() if any(fx["kind"] != "recall" for _, fx in fxs) else "-"
    kob = KoboldClient(KOBOLD_URL)
    print(f"companion-memory eval — {len(fxs)} fixtures x {args.runs} runs, model={model}"
          f"{', CONSOLIDATE' if CONSOLIDATE else ''}\n")

    totals = collections.Counter()
    per_fixture, info, recall, dump = [], [], None, []
    for path, fx in fxs:
        print(f"{'=' * 72}\n{fx['name']}  [{fx['kind']}{', informational' if fx.get('informational') else ''}]"
              f"\n  {fx['why']}\n{'=' * 72}")
        if fx["kind"] == "recall":
            recall = run_recall(fx)
            if "skipped" in recall:
                print(f"  skipped: {recall['skipped']}\n")
                continue
            for vname, v in recall["variants"]:
                print(f"  -- {vname} ({v['docs']} indexed turns)")
                for q, k, best, margin, verdict, top in v["rows"]:
                    print(f"  {k} best={best:.3f} margin={margin:+.3f}  {verdict:<26} {q[:40]!r}"
                          + (f" -> {top!r}" if top else ""))
            print()
            continue

        counted = not fx.get("informational")
        stats = collections.Counter()
        labels = collections.Counter()
        for i in range(args.runs):
            try:
                if saved is not None:
                    if i >= len(saved_runs[fx["name"]]):
                        break
                    snaps = saved_runs[fx["name"]][i]
                else:
                    snaps = await run_llm(kob, fx)
            except Exception as e:          # never seen; keep the run honest if it happens
                snaps = [{"summary": "", "facts": [], "error": True}] * len(fx["stages"])
                print(f"  run {i + 1}: exception {e!r}")
            dump.append({"fixture": fx["name"], "run": i + 1, "stages": snaps})
            if fx["kind"] == "fold" and counted:
                totals["fold_runs"] += 1
                totals["auto_facts"] += len(snaps[-1]["facts"])
                totals["fold_runs_no_facts"] += not snaps[-1]["facts"]
            r = score(fx, snaps)
            if r["error"]:
                stats["error"] += 1
                print(f"  run {i + 1}: ERROR — {'; '.join(r['error'])} (excluded from rates)")
                continue
            stats["n"] += 1
            if counted:
                for k in ("forbidden", "format", "missed"):
                    stats[k] += bool(r[k])
                stats["clean"] += not any(r.values())
                flag = ("clean" if not any(r.values())
                        else " ".join(k.upper() for k in ("forbidden", "format", "missed") if r[k]))
                print(f"  run {i + 1}: {flag}")
                for k in ("forbidden", "format", "missed"):
                    for detail in r[k]:
                        print(f"      {k}: {detail}")
                        labels[re.sub(r" -> .*| \(\d+ chars\)|^\d+ ", "", detail)] += 1
            else:
                exp = fx.get("checks", {}).get("expect", [])
                missed = set(r["missed"])
                for e in exp:
                    stats["cap:" + e["label"]] += e["label"] not in missed
                print(f"  run {i + 1}: captured {len(exp) - len(missed)}/{len(exp)}"
                      + (f"  (missing: {', '.join(sorted(missed))})" if missed else ""))
            if args.show:
                for j, s in enumerate(snaps):
                    print(f"      --- stage {j + 1} summary ({len(s['summary'])} chars):")
                    for line in (s["summary"] or "").splitlines():
                        print(f"        | {line[:110]}")
                    for t in s["facts"]:
                        print(f"        fact: {t[:100]}")
        n = stats["n"]
        if counted:
            for k in ("clean", "forbidden", "format", "missed", "n", "error"):
                totals[k] += stats[k]
            print(f"  -> clean {stats['clean']}/{n} · forbidden {stats['forbidden']}/{n} · "
                  f"format {stats['format']}/{n} · missed {stats['missed']}/{n}")
            for lab, c in labels.most_common():
                print(f"     {c}x {lab}")
            per_fixture.append((fx["name"], stats["clean"], n))
        else:
            caps = [(k[4:], v) for k, v in stats.items() if k.startswith("cap:")]
            print("  -> " + " · ".join(f"{lab} {v}/{n}" for lab, v in caps))
            info.append((fx["name"], caps, n))
        print()

    await kob.close()
    end_model = wait_for_kobold(60) if model != "-" and saved is None else model
    if end_model != model:
        print(f"\n!! MODEL CHANGED during the run: {model} -> {end_model}. "
              "Rates from this run mix two models; do not record them.")
    if args.dump:
        pathlib.Path(args.dump).write_text(json.dumps({"model": model, "runs": dump},
                                                      indent=1, ensure_ascii=False))

    n = totals["n"] or 1
    print(f"{'=' * 72}\nTOTAL over {totals['n']} runs (counted LLM fixtures) · model={model}")
    print(f"  clean          {totals['clean']:>3}/{n}")
    print(f"  forbidden      {totals['forbidden']:>3}/{n}   (stored something that must never persist)")
    print(f"  format         {totals['format']:>3}/{n}   (padding, prose, Where-line count)")
    print(f"  missed         {totals['missed']:>3}/{n}   (durable item / key event not captured)")
    print(f"  auto-facts:    {totals['auto_facts']} stored over {totals['fold_runs']} fold runs "
          f"({totals['fold_runs_no_facts']} runs stored none) — yield, not a verdict")
    if totals["error"]:
        print(f"  !! {totals['error']} run(s) produced no output (backend error) and are excluded")
    print("  per fixture:   " + " · ".join(f"{nm} {c}/{k}" for nm, c, k in per_fixture))
    if recall and "skipped" not in recall:
        for vname, v in recall["variants"]:
            mr, mi = v["margins_rel"], v["margins_irr"]
            print(f"  recall gate, {vname} ({v['docs']} turns, {v['rel'] + v['irr']} queries):")
            print(f"      relevant hit {v['rel_hit']}/{v['rel']} (top-1 {v['rel_top1']}, "
                  f"gated out {v['rel_none']}, wrong {v['rel_wrong']}) · "
                  f"irrelevant leaked {v['irr_leak']}/{v['irr']}")
            print(f"      margin: relevant min {min(mr):.3f} median {float(np.median(mr)):.3f}"
                  f" · irrelevant max {max(mi):.3f} median {float(np.median(mi)):.3f}"
                  f" (gate {memory.RECALL_MIN_MARGIN})")
    if info:
        print("INFORMATIONAL (not counted — capabilities a later phase may add)")
        for nm, caps, k in info:
            print(f"  {nm}: " + " · ".join(f"{lab} {v}/{k}" for lab, v in caps))
    print("\n  Record this block with any prompt change. A rate that moves is evidence;\n"
          "  a single run that looks better is not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
