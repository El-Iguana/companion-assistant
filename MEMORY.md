# Memory system — how it works today

What the app actually remembers, where it lives, and why each piece is shaped the way it is.
This describes the **shipped** system; `MEMORY_PLAN.md` is the design/decision log (phases 1-6),
and `PROMPT_BUDGET.md` covers the one gap this system does not close.

Code: `services/memory.py`, `services/assistant_memory.py`, and the shared parts in Muninn
(`../muninn`: `muninn.fold`, `muninn.recall`, `muninn.facts`, `muninn.embeddings`), assembled in
`services/prompt_builder.build_messages()`, wired in `main.py` (`chat()` + `/api/memories` +
`/api/assistant-memory` + `/api/sessions/{id}/sweep`).

## Two pipelines

`card.chat_mode` picks one, once per turn, and they share no code path that could leak one into
the other. A companion remembers a *history*; an assistant must remember *how to behave* and
*what is true*, and above all must not drift.

| | Companion (`roleplay`) | Assistant (`assistant`) |
|---|---|---|
| Module | `services/memory.py` | `services/assistant_memory.py` |
| Store | `CharacterMemory` — facts + a cumulative narrative summary | `AssistantMemory` — directives + facts + task notes |
| File | `data/u/<uid>/memories/{cid}__{pid}.json` | `data/u/<uid>/assistant/{cid}__{pid}.json` |
| Per-session summary | rolling `Story so far` | none — `session.memory.summary` stays empty, and a roleplay-era one is skipped on read and cleared on the next fold |
| Semantic recall | yes (`.vec.npz` sibling) | no |
| Route | `GET/PUT /api/memories` | `GET/PUT /api/assistant-memory` |

`chat()` and `POST /sessions/{id}/sweep` each choose a pipeline and never run both (the chat
flow's `_maintain_memory` branches once on `chat_mode`). A card switched between modes leaves the other store on disk, intact
and inert; nothing migrates between them.

Why a separate module and not a branch is in `MEMORY_PLAN.md` (Phase 6) — the short version is that
every leak found so far was roleplay framing reaching an assistant through a shared path that forgot
to branch, and a branch is a place the next feature forgets.

## Companion memory — the layers

| Layer | Scope | Storage | Injected as |
|---|---|---|---|
| Verbatim turns | session | `session.messages[last_summarized_index:]` | the history itself |
| Pinned facts | session | `MemoryState.pinned` | `Important facts:` |
| Rolling summary | session | `MemoryState.summary` | `Story so far (already happened — never retell it…):` — past-tense notes ending in a `Where things stand:` line |
| Long-term memory | character x persona | `data/u/<uid>/memories/{cid}__{pid}.json` | `What {{char}} remembers about {{user}}…` |
| Semantic recall | character x persona | `data/u/<uid>/memories/{cid}__{pid}.vec.npz` | `Things from earlier that may be relevant:` |

Order inside the system block: pinned facts -> `longterm_note` (long-term + recall, joined) ->
`Story so far` -> (assistant: today's date) -> history -> `post_history_instructions` -> ONE trailing
note: [time, only when asked] + expression beats + assistant format note + boundary note + `focus_note`
(quotes the latest user message; always last).

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
  without being recorded durably first. **It runs after the reply** (2026-10-29): the turn saves and
  sends `done`, then `main._memory_job` does the summary + fold in the background, one job per
  session at a time. The job writes `summary`/`last_summarized_index` onto a fresh read only if
  neither moved and the folded turns are unchanged (else a later turn redoes it), and
  `_save_turn` never writes back an older summary than the one on disk;
- **idle** — `POST /api/sessions/{id}/sweep`, armed by the frontend `SWEEP_IDLE_MS` (5 min) timer, so
  a *finished* conversation gets captured instead of being cut at an arbitrary turn boundary.

A fold is **one pass per context-sized slice** of the range, not one pass overall (`_fold_chunks`,
`fold_budget(context_tokens)` — sized from the effective context window, `muninn.context` via `services/context.py`;
`FOLD_TRANSCRIPT_CHARS` is the 8192 default). A backlog — the first idle sweep of a long finished chat — renders a
transcript that can exceed the whole context window, and KoboldCPP truncates from the front with no
error, so the casualty would be `LONGTERM_PROMPT` itself: the fold running with none of its rules.
Successive passes keep every rule in every pass and lose nothing. Both pipelines use it.

The fold prompt (`LONGTERM_PROMPT`) is tuned against the 9B fine-tune and every rule in it is a
failure that was observed: terse bullets (`LONGTERM_MAX_BULLETS` = 10) because "a few paragraphs"
produced florid essays; "a four-bullet memory is a good memory" because a bare cap got padded with
invented traits; an explicit carve-out because the model recorded its own replies as memories; and a
REPLACE rule because folding a cancellation kept both "plans to sail next month" and "trip is off".
Retune the prompt rather than lowering the budget — truncating mid-sentence is worse than generating
short.

### Consolidation (setting, off by default)

`AppSettings.longterm_consolidate` (Settings → Memory → "Consolidate long-term memory") swaps
the rewrite for `muninn.consolidate`: a fold asks only for **new** bullets
(`LONGTERM_NEW_PROMPT`) and appends them, deduped in code; existing text is never regenerated.
The idle sweep then runs `review_longterm()` once per state of the memory
(`CharacterMemory.reviewed_summary`): the model names, by number, bullets a later one made untrue
or that repeat another, and code deletes exactly those (never more than half the list in one
pass). Past `LONGTERM_KEEP_BULLETS` (16) the oldest bullets go into the recall index as
`kind="memory"`.

**Measured on the eval, it is not better yet** (Cydonia-24B, 33 runs, against the rewrite's last
run): clean 10 vs 9, forbidden 14 vs 20, format 4 vs 13, but **missed 10 vs 6**, and the
erosion fixture it was built for is 0/3 clean either way. Two reasons, read from the outputs:
asked for "only new bullets", the model often answers NONE for chats with plain durable facts
(the first prompt said "often none" and missed 17/33); and append-only makes whatever junk gets
through permanent, where the rewrite tends to drop it next fold — reasoning written as bullets
("Here are Priya's statements…"), a stray `</assinking>`, once a whole invented dialogue. The
review also keeps a cancelled plan's details (its date, who packs what) about half the time.
Before turning it on: a stronger line filter on the new-bullets pass, and a reason for the NONE.

Injection (`longterm_block()`) is capped at `LONGTERM_BUDGET_CHARS` (2400, header counted): the
cumulative history is cut to its share (`LONGTERM_SUMMARY_SHARE`, 0.6) by dropping whole lines
**oldest first** (only a single over-long line is truncated), and facts drop
**oldest-first**, so an oversized entry costs only itself instead of silently dropping everything
after it. Facts are hard-capped at `LONGTERM_MAX_FACTS` (40) on write.

**Scenario sessions are excluded** from both injection and sweeping, exactly like `rel` in `chat()` —
a scenario is a deliberate alternate premise, and the main timeline's remembered history would
contradict the state the preset just seeded.

## Semantic recall

`muninn.embeddings` (index and gate: `muninn.recall`): int8 BGE-small-en-v1.5, ONNX, CPU, baked at `/app/models/embed` (33 MB),
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

## Assistant memory — `AssistantMemory`

`services/assistant_memory.py`, stored per (character, persona) at
`data/u/<uid>/assistant/{cid}__{pid}.json`. Three buckets and nothing else:

| Bucket | What it is | Cap |
|---|---|---|
| `directives[]` | standing instructions to **obey** — "address me as Sir", "times in Eastern" | 20 |
| `facts[]` | things that are **true** about the user, known but not acted on | 40 |
| `notes[]` | terse task state — open threads, decisions taken | 12 |

`directives`/`facts` are `MemoryFact{text, origin: user|auto}`, same as the companion store, so
everything auto-written is badged and revertible. `notes` are plain strings and are **rewritten
whole** on each fold rather than appended: the companion summary may accrete because prose does, but
a list of task state should *shrink* when the task is done, and an empty list is the correct answer
when nothing is outstanding.

**The instruction/fact split is the reason the pipeline exists.** Asked for "durable facts" as one
bucket, the model filed *"El Iguana prefers to be addressed as Sir"* as a fact — true, and stored
where nothing tells the assistant to do it. An instruction that is merely known is an instruction
that gets ignored. `extract()` requires each line to be prefixed exactly `INSTRUCTION:` or `FACT:`
and **drops an unprefixed line** rather than guessing, because guessing reintroduces the failure the
split exists to stop. It holds: `misfiled 0/15` across the eval.

**Injection (`block()`) puts directives FIRST and never truncates them.** Facts and notes yield to
the budget (`BUDGET_CHARS` 2400, same ceiling as the companion block, spent differently); directives
do not, because dropping an instruction to fit a budget changes behaviour with no error and nothing
in the transcript to explain it.

### Folding

`fold()` reuses the companion fold's high-water-mark bookkeeping and chunked passes — that part is
domain-independent and already paid for — but writes directives, facts and notes, never a narrative.
The same two triggers fire it: the chat flow at the `summarize_after` threshold (advancing
`session.memory.last_summarized_index` so the prompt cannot grow without bound, while
`session.memory.summary` stays **empty** — there must be no prose to inject) and the idle sweep.
`max_new` (`auto_facts_max_new`) is spent across **both** categories together.

**A session migrated from roleplay arrives carrying a summary, and it is dealt with in two places.**
`memory.summarize()` is the only writer of that field and never runs for an assistant, so the text is
*frozen* at whatever the roleplay summarizer last produced — third-person narration that nothing will
ever rewrite, injected into every turn. Found on a live 51-turn session: 1851 characters of it,
opening with a hardcoded date and time, which is exactly what `NOTES_PROMPT` forbids `notes` from
containing. So `build_messages` **skips `mem.summary` entirely for an assistant** (it no longer has a
gentler header — there is no case where injecting it is right), and `fold()` **clears the stored
copy**, but only once the fold has passed the point the summary covered, so nothing is discarded that
was not just read into directives, facts and notes. `session.messages` is untouched either way: this
deletes a recap, not history.

Unlike the companion summary, an **empty result is valid** and does not stop the fold; only a hard
failure does.

### Three enforcements in code, because the prompt rule did not hold

Each of these is a rule that is present in the prompt, was ignored, and had to be made
deterministic. Measured with `eval/assistant_memory` — see below.

- **Extraction reads USER turns only.** On a transcript of pure small talk the extractor stored
  *"Address El Iguana as Sir"* as a standing instruction in **all three runs**. Nobody said it: the
  assistant says "Sir" out of politeness in its own replies, and its own manners were mined into an
  instruction to itself, which then injects into every later prompt. The prompt already forbade
  recording what the assistant said. Removing the material is not a rule it can ignore — it cannot
  mine what it cannot see. Same shape, and the same reason, as the companion index holding user
  turns only.
- **Status notes are dropped (`_drop_status_notes`).** Notes are open threads; *"Jarvis is fully
  awake and operational"* is a status line that would still be sitting in the prompt next week, and
  *"El Iguana has issued the command to proceed"* is a transcript entry wearing a note's clothes.
  Matching is anchored to the **subject** — the assistant's name plus a state verb, or the user's
  name plus a speech verb — so real threads survive (`Jarvis needs the API key`, `Jarvis has not
  finished the migration`). A third pattern catches the subjectless case (*"System rebooted
  successfully."*), with the verb list kept tight so *"System migration still pending"* lives.
- **Notes that restate a directive or fact are dropped (`_drop_restated`).** Told not to repeat
  them, the model re-emitted *"Address Jared Revan as Sir"* and *"Use Eastern time instead of UTC"*
  as notes while both were already stored as directives. Overlap is scored against the **shorter**
  of the two strings, so a terse directive still matches a wordier restatement.

Dedupe here is **string-level, not embeddings** (`_dedupe`), unlike the companion pipeline:
directives are short, literal and few, and an assistant behaving correctly must not depend on an
optional model being loaded.

### The eval

`eval/assistant_memory/` — five labelled fixture transcripts, a runner, and `RESULTS.md` recording
every run. This exists because eyeballing single runs gave the **opposite** verdict to three runs, in
both directions: a fix declared working scored 0/3, and a leak recorded as unfixed scored 3/3.

```
podman cp eval companion:/app/eval          # eval/ is deliberately not baked into the image
podman exec -i companion python3 /app/eval/assistant_memory/run.py --runs 3
```

Four counters: `clean` / `misfiled` (instruction stored as a fact) / `forbidden` (stored something
that must never persist) / `missed`. Record the totals with any prompt change — a rate that moves is
evidence, a single better-looking run is not.

Current: `clean 13/15 · misfiled 0/15 · forbidden 1/15 · missed 1/15` (run 3, from `7/15 · 0/15 ·
7/15 · 4/15` at baseline).

## Failure posture

Every layer of both pipelines degrades to nothing. No baked embedding model, an onnxruntime failure,
a corrupt or torn index, a summarizer exception, an unreadable store — all yield no block and leave
the chat turn untouched. Nothing in the memory path is allowed to raise into the hot path or fail a
reply. An `AssistantMemory` file that fails to parse starts fresh rather than 500-ing a chat turn.

## API and UI

```
GET/PUT /api/memories?character_id=&persona_id=   companion store; facts round-trip BY ID, so editing
                                                  through the modal can't relabel an auto fact as user
GET/PUT /api/assistant-memory?character_id=&persona_id=
                                                  assistant store — directives[] + facts[] + notes[];
                                                  directives/facts round-trip by id, notes as lines
POST /api/sessions/{id}/sweep                     idle trigger; idempotent; branches on chat_mode and
                                                  reports which pipeline ran
GET/PUT /api/settings                             summarize_after · keep_recent · summary_max_tokens ·
                                                  recall_enabled · recall_top_k ·
                                                  auto_facts_enabled · auto_facts_max_new
```

`auto_facts_enabled` / `auto_facts_max_new` govern **both** pipelines; `summarize_after` /
`keep_recent` / `summary_max_tokens` set the assistant's fold window and budget too, even though no
summary comes out of it. The recall settings are companion-only.

The Memory modal has two tabs (This chat / Long-term). Long-term facts carry an `AUTO` badge and are
individually editable and deletable — the review surface auto-extraction was gated on. The long-term
tab is hidden for character-less and scenario sessions.

For an assistant card the same tab renders the **other shape, not a relabelled one** (`ltIsAssistant()`
in `app.js`): a Standing-instructions list appears above the facts, "Remembered facts" becomes "Things
it knows about you", and the free-text summary box becomes "Where things stand" — one note per line,
with the placeholder saying that empty is correct. The modal is also the correction surface for
everything the extractor gets wrong, which is what `origin` is for.

## Known gaps

- Long-term `facts` never expire; they stay until the user removes them. Whether they should decay is
  still open.
- **The prompt is budgeted by dropping old turns only.** Everything here has to fit the context
  window (Settings → Context window, capped at the backend's real limit) alongside the card, persona
  and relationship blocks. Since 2026-10-30 `build_messages` drops the oldest verbatim turns to fit,
  so history overflow no longer deletes the memory blocks. A *system block* too big on its own still
  overflows and still loses its front — the eviction order for that is in `PROMPT_BUDGET.md`, not built.
- With the fold prompt tightened, a 7-topic conversation still fits inside the 10-bullet summary, so
  recall added nothing at that size. Its value is in pairings that accumulate well past the bullet cap
  (verified by emptying the summary and confirming a fresh session could still answer three specifics
  from the index alone).
- **Assistant: work in progress still leaks into `facts`.** Measured — "We still need to finish the
  middleware" was filed as a durable fact despite a rule against it, where it will outlive its truth.
  Visible and deletable in the Memory modal; that is the current mitigation.
- **Assistant: the same-sentence split is unsolved, and its fixture is noisy.** The durable half of
  "my friends Tom and Bill are visiting next month" is lost more often than not, and the fixture has
  scored 0/3, 0/3, 1/3, 0/6 and 2/3 on essentially the same code. It needs more runs per verdict than
  the rest of the suite, and any conclusion from three runs of it is worth very little. A described
  rule beat a worked example here (1/9 vs 0/6) — the opposite of the expression beats, and recorded
  in the module so it is not retried on the same reasoning.
- **The assistant pipeline has no semantic recall.** Its store is small, literal and capped, so
  retrieval has nothing to add yet; if `facts` grows to its cap in practice this is where to look.
