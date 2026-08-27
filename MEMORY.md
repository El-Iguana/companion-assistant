# Memory system — how it works today

What the companion actually remembers, where it lives, and why each piece is shaped the way it is.
This describes the **shipped** system; `MEMORY_PLAN.md` is the design/decision log (phases 1-5),
and `PROMPT_BUDGET.md` covers the one gap this system does not close.

Code: `services/memory.py`, `services/embeddings.py`, assembled in
`services/prompt_builder.build_messages()`, wired in `main.py` (`chat()` + `/api/memories` +
`/api/sessions/{id}/sweep`).

## The layers

| Layer | Scope | Storage | Injected as |
|---|---|---|---|
| Verbatim turns | session | `session.messages[last_summarized_index:]` | the history itself |
| Pinned facts | session | `MemoryState.pinned` | `Important facts:` |
| Rolling summary | session | `MemoryState.summary` | `Story so far:` |
| Long-term memory | character x persona | `data/u/<uid>/memories/{cid}__{pid}.json` | `What {{char}} remembers about {{user}}…` |
| Semantic recall | character x persona | `data/u/<uid>/memories/{cid}__{pid}.vec.npz` | `Things from earlier that may be relevant:` |

Order inside the system block: pinned facts -> `longterm_note` (long-term + recall, joined) ->
`Story so far` -> history -> `post_history_instructions` -> date/time -> boundary note.

## Per-session rolling summary — `summarize()`

Once `summarize_after` (24, tunable in the settings modal) un-summarized turns accumulate, everything
older than `keep_recent` (12) is folded into `memory.summary` by a KoboldCPP pass
(`summary_max_tokens`, 400). `keep_recent` is clamped below `summarize_after` so a fold always covers
at least one turn. Nothing is destroyed — `session.messages` keeps the full history on disk and the
index only marks how much the summary covers. Best-effort: an exception returns without touching the
session, and never blocks the chat.

## Cross-session long-term memory — `CharacterMemory`

Per (character, persona), the same granularity and `{cid}__{pid}` file convention as `Relationship`.
Holds `facts[]` (`MemoryFact{text, origin: user|auto}`), a cumulative `summary`, and
`swept_sessions` (session_id -> message index already folded), which is what makes folding
**idempotent and re-runnable** — a single "last swept session" id could not express "this session
grew, fold the new part".

Two write triggers, both through `fold_into_longterm()`:

- **threshold** — the chat flow folds forward exactly what `summarize()` just folded out of the
  verbatim window (`upto=session.memory.last_summarized_index`), so nothing leaves the live prompt
  without being recorded durably first;
- **idle** — `POST /api/sessions/{id}/sweep`, armed by the frontend `SWEEP_IDLE_MS` (5 min) timer, so
  a *finished* conversation gets captured instead of being cut at an arbitrary turn boundary.

The fold prompt (`LONGTERM_PROMPT`) is tuned against the 9B fine-tune and every rule in it is a
failure that was observed: terse bullets (`LONGTERM_MAX_BULLETS` = 10) because "a few paragraphs"
produced florid essays; "a four-bullet memory is a good memory" because a bare cap got padded with
invented traits; an explicit carve-out because the model recorded its own replies as memories; and a
REPLACE rule because folding a cancellation kept both "plans to sail next month" and "trip is off".
Retune the prompt rather than lowering the budget — truncating mid-sentence is worse than generating
short.

Injection (`longterm_block()`) is capped at `LONGTERM_BUDGET_CHARS` (2400, header counted): the
cumulative history is truncated to its share (`LONGTERM_SUMMARY_SHARE`, 0.6) and facts drop
**oldest-first**, so an oversized entry costs only itself instead of silently dropping everything
after it. Facts are hard-capped at `LONGTERM_MAX_FACTS` (40) on write.

**Scenario sessions are excluded** from both injection and sweeping, exactly like `rel` in `chat()` —
a scenario is a deliberate alternate premise, and the main timeline's remembered history would
contradict the state the preset just seeded.

## Semantic recall

`services/embeddings.py`: int8 BGE-small-en-v1.5, ONNX, CPU, baked at `/app/models/embed` (33 MB),
running on the onnxruntime already present for rembg/Kokoro/whisper — **zero VRAM**, so the 4070
stays entirely for KoboldCPP and ComfyUI. CLS pooling (not mean — BGE pools CLS), L2-normalized so a
dot product *is* cosine, and BGE's query prefix applied to queries only. int8 was picked over fp32
(127 MB) by measurement: both scored 5/5 top-1 on the same probe, cosines within 0.02. 143 ms cold
load, ~2 ms per warm query — cheap enough to run on every turn, and the search is offloaded with
`asyncio.to_thread` because onnxruntime is sync and would otherwise block the event loop, stalling
every other session's stream for the duration.

The index is written inside `fold_into_longterm()` so the two triggers cannot drift apart on what got
indexed; facts are re-indexed wholesale on `PUT /api/memories` (replace-by-kind, since facts are
editable and deletable). Capped at `RECALL_MAX_VECTORS` (2000), evicting oldest **turns** first —
curated facts are never pushed out by conversation volume.

**User turns only.** Her replies restate the user wrapped in personality: indexing them duplicated the
signal and dragged the median similarity up, which is exactly what the gate is measured against. On a
live index, with her turns in, 2 of 6 deliberately irrelevant queries slipped through; with user turns
only, 0 of 6 did, and all 5 relevant queries still retrieved the right turn.

**The relevance gate** is a margin, not a floor. BGE's absolute cosines are not calibrated — "hello"
scored 0.541 while the genuinely relevant "what can't I eat?" scored 0.523, so any fixed floor either
lets greetings through or drops real hits. What separates them is the gap between the best hit and the
median: relevant queries scored >= 0.114 above median, irrelevant ones <= 0.094, hence
`RECALL_MIN_MARGIN` = 0.10, with `RECALL_MIN_SCORE` (0.45) kept only as a loose sanity check plus a
3-word minimum query. Calibrated on 8 documents and 13 queries — a heuristic, not a validated
threshold.

Retrieval **shares** `LONGTERM_BUDGET_CHARS` with the long-term block rather than adding to it, and
drops any snippet whose text already appears in that block. Tunable via `recall_enabled` /
`recall_top_k`.

## Auto-fact extraction

After each fold, durable facts are pulled out of the same transcript and stored with `origin="auto"`,
so the stable atoms survive the summary being rewritten wholesale on every fold.

Deliberately reluctant, because a wrong auto-fact outlives the summary that produced it: at most
`auto_facts_max_new` (default 3, 1-10) per fold, temperature 0.2, "NONE is a good answer" in the
prompt, and it only ever **adds** — user-written facts are never edited or deleted.

Transient-vs-durable was the hard part. The first prompt extracted "Nadia is visiting at the end of
summer" out of a sentence that also carried the durable "Nadia is a paramedic in Leeds"; the sharpened
rule (a date, a season, or words like next/soon/visiting/coming means NOT durable, even in the same
sentence as a durable fact) scored 4/4 on the probe set.

Dedupe is in **code**, not the prompt (`_dedupe_facts`). Told to skip known facts, the model re-emitted
two of them verbatim. Cosine against existing facts (`AUTO_FACT_DEDUPE_SIM` = 0.86) catches that and
the rewordings a string match misses ("owns a dog named Biscuit" vs "has a dog called Biscuit"), and
falls back to normalized string equality when the embedder is unavailable. Over the 40-fact cap, the
oldest **auto** facts are evicted first — a curated fact is never pushed out by one the app invented.

## Failure posture

Every layer degrades to nothing. No baked embedding model, an onnxruntime failure, a corrupt or torn
index, a summarizer exception — all yield no block and leave the chat turn untouched. Nothing in the
memory path is allowed to raise into the hot path or fail a reply.

## API and UI

```
GET/PUT /api/memories?character_id=&persona_id=   long-term store; facts round-trip BY ID, so editing
                                                  through the modal can't relabel an auto fact as user
POST /api/sessions/{id}/sweep                     idle trigger; idempotent
GET/PUT /api/settings                             summarize_after · keep_recent · summary_max_tokens ·
                                                  recall_enabled · recall_top_k ·
                                                  auto_facts_enabled · auto_facts_max_new
```

The Memory modal has two tabs (This chat / Long-term). Long-term facts carry an `AUTO` badge and are
individually editable and deletable — the review surface auto-extraction was gated on. The long-term
tab is hidden for character-less and scenario sessions.

## Known gaps

- Long-term `facts` never expire; they stay until the user removes them. Whether they should decay is
  still open.
- **The prompt as a whole is not budgeted.** Everything here has to fit the 8192-token context
  alongside the card, persona and relationship blocks. The memory blocks are individually capped, but
  on total overflow KoboldCPP truncates from the front and silently deletes them — measured and written up in
  `PROMPT_BUDGET.md`, where the fix is proposed but not built.
- With the fold prompt tightened, a 7-topic conversation still fits inside the 10-bullet summary, so
  recall added nothing at that size. Its value is in pairings that accumulate well past the bullet cap
  (verified by emptying the summary and confirming a fresh session could still answer three specifics
  from the index alone).
