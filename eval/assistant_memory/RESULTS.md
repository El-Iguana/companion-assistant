# assistant-memory eval

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


---

# Run 2 — after the confabulation fix

**2026-09-02 · same 5 fixtures × 3 runs**

```
                baseline   run 2
clean              7/15    12/15
misfiled           0/15     0/15
forbidden          7/15     1/15
missed             4/15     2/15
```

Per fixture: directive-vs-fact 3/3 (was 2/3) · same-sentence 1/3 (was 0/3) ·
work-in-progress 3/3 · self-status-and-clocks 2/3 · **nothing-durable 3/3 (was 0/3)**.

## Both fixes were deterministic, because the prompt rules were already there

**Extraction reads USER turns only.** The invented "Address El Iguana as Sir" came
from the assistant's own replies — it says "Sir" out of politeness and the
extractor mined that into an instruction to itself. The prompt already forbade
recording what the assistant said and did not comply. Removing the material is
not a rule it can ignore: it cannot mine what it cannot see. This is the same
finding the companion pipeline recorded when it stopped indexing her turns.

**Status notes are dropped in code.** What survived the first fix was notes like
"Jarvis is fully awake and operational" and "El Iguana has issued the command to
proceed" — a status line and a transcript entry, neither an open thread. Matching
is anchored to the subject: the assistant's name plus a state verb, or the user's
name plus a speech verb. Real threads about the assistant survive — "Jarvis needs
the API key" and "Jarvis has not finished the migration" are both kept.

That is now three separate occasions in this codebase where a prompt rule was
present, ignored, and had to be enforced in code.

## One fixture was corrected, and that deserves saying out loud

`same-sentence` forbade the scheduled visit in `any`, which includes notes. But
"Tom and Bill visit next month" IS an open thread — a dated commitment is exactly
what notes are for. It is now forbidden as a durable FACT only, where it would
outlive the visit.

Moving a label to make a failure disappear is how an eval stops being worth
running. The test: the fact-side expectation was not touched, and the fixture
still fails — "Tom and Bill are friends" is missed in 2 of 3 runs. The correction
removed a wrong penalty, not a real one.

## What remains

- `same-sentence` 1/3 — the durable half of "my two friends Tom and Bill are
  coming next month" is still lost 2 runs in 3. A MISSED: recoverable, the user
  can say it again or add it in the modal.
- `self-status` 2/3 — "System rebooted successfully." has no subject, so a
  subject-anchored filter does not see it. An honest limit of the approach.
