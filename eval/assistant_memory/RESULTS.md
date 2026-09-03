# assistant-memory eval — baseline

**2026-09-02 · 5 fixtures × 3 runs · Qwen3.8-9B-distill-heretic**
`podman exec -i companion python3 /app/eval/assistant_memory/run.py --runs 3`

```
clean            7/15
misfiled         0/15   (instruction stored as a fact)
forbidden        7/15   (stored something that must never persist)
missed           4/15   (durable item not captured)
```

Per fixture: directive-vs-fact 2/3 · same-sentence 0/3 · work-in-progress 3/3 ·
self-status-and-clocks 2/3 · nothing-durable 0/3.

## What the harness caught that reading single runs did not

**1. It invents standing instructions out of nothing.** The `nothing-durable`
fixture is pure small talk — "morning", "just checking you're awake", "ok carry
on". All three runs stored items anyway, including:

```
directive: Address Jared Revan as "Sir".
fact:      Jared Revan's name is Jared Revan.
note:      Jarvis is fully awake and operational.
```

Nobody asked to be called Sir in that transcript. The *assistant* says "Sir" in
its own replies, and the extractor turned its own politeness into a standing
instruction — despite a rule forbidding exactly that. This is the worst failure
the pipeline has: an invented directive is injected into every later prompt, and
it is also the likely origin of Mira's "Say that again." directive.

**2. My two eyeball fixes were wrong in both directions.** The same-sentence
carve-out I added and declared fixed scores **0/3**. The work-in-progress leak I
recorded as *unfixed* scores **3/3**. One run told me the opposite of what three
runs say, in both cases.

**3. The instruction/fact split itself is solid.** `misfiled 0/15` — no standing
instruction was ever filed as a fact. That was the reason for splitting the
pipeline, and it holds.

## How to use this

Record the TOTAL line with any prompt change. A rate that moves is evidence; a
single run that looks better is not — that is the mistake this file exists to stop
being repeated.

`eval/` is deliberately not baked into the image (`Containerfile` copies only the
app). Copy it in for a run: `podman cp eval companion:/app/eval`.

## Next

1. Confabulated directives (`nothing-durable`, 0/3) — worst first.
2. The same-sentence carve-out (0/3), which does not work despite appearing to.
3. More fixtures. Five is enough to catch these; it is not enough to tune against.
