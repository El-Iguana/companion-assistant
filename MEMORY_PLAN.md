# Memory system — plan and decision log

Ideas and decisions for what the app remembers — the companion in phases 1-5, and the assistant
it grew a second pipeline for in phase 6. Revised after reviewing Lumina's memory design
(a third-party agentic assistant with a "MemPalace" layered store) as a possible alternative:
**keep this plan**, take two ideas from it, reject its centrepiece. See "Evaluated and rejected" at
the bottom so that doesn't get re-litigated later.

**Mechanics are not in this file.** The scopes, the fold triggers, the recall gate, auto-fact
extraction, the budgets and every constant live in `MEMORY.md`, which is the file to edit when
behavior changes. This one records the *reasons* — what was open, what was decided, and what shipped.

Code: `services/memory.py`, `services/assistant_memory.py`, Muninn (`services/muninn`, a copy of its context, fold, emotion and text modules),
`models/schema.py` (`MemoryState`, `CharacterMemory`, `AssistantMemory`),
`prompt_builder.build_messages`, and the chat flow in `main.py`.

Phases 1–6 have all shipped. Phases 1–5 built the companion's memory; Phase 6 split a second
pipeline out of it for assistant-mode cards. **Phase 7 is planned**: memory that belongs to the
bartender rather than to a chat, folded per episode and recalled per turn (issues #15–#23).

## Settled design decisions

Recorded here because each was an open question and each now has a reason, not just an answer.

- **Long-term scope = per (character × persona).** Mirrors `Relationship` exactly — same granularity,
  same `{cid}__{pid}.json` convention, same "a different persona is genuinely a different history"
  logic, reusing `_load_relationship`/`_save_relationship` as the pattern to copy.
- **Cross-session persistence shipped before embeddings.** Cross-session is a *missing capability*
  (she forgets you on a new chat); embeddings are a *retrieval-quality optimization* on top of it.
  The store could ship with recency + pinned retrieval and gain semantic search later without
  changing shape. Lumina is the cautionary case: it built an elaborate layered store first and its
  recall is still `content LIKE '%query%'`.
- **The embedder runs on CPU via ONNX — zero VRAM.** `onnxruntime` was already in the image for
  rembg, Kokoro and faster-whisper, and the baked-`models/<name>/` convention was already
  established. That closes the "share the 4070 with KoboldCPP + ComfyUI" question without a
  trade-off. int8 over fp32 was decided by measurement, not size preference.
- **Auto-written memory is tagged and revertible.** Every fact carries its origin (`user` | `auto`)
  so the 📌 Memory modal can filter and revert what she decided to remember on her own.
  Auto-extraction without an undo path is not shippable — Phase 5 was gated on that review surface.
- **Auto-extraction is reluctant by construction.** A wrong auto-fact outlives the summary that
  produced it: the summary is rewritten on every fold, but a fact persists into every future
  conversation until someone notices. Hence a small per-fold cap, a low temperature, "NONE is a good
  answer", and add-only behavior that never edits or deletes a user-written fact.
- **Dedupe belongs in code, not the prompt.** Told to skip known facts, the model re-emitted them
  verbatim. Semantic comparison also catches the rewordings a string match misses.
- **The index holds user turns only.** Her replies restate the user wrapped in personality: indexing
  them duplicated the signal *and* dragged the median similarity up, which is what the relevance gate
  is measured against. Measured both ways before deciding.
- **Recall is gated on a margin, not a floor.** BGE's absolute cosines are not calibrated — a
  greeting outscored a genuinely relevant question — so any fixed floor either lets greetings through
  or drops real hits. The gate is the distance between the best hit and the median. Calibrated on a
  small sample: a heuristic, not a validated threshold.
- **Retrieval shares the long-term block's budget rather than adding to it**, so turning recall on
  can't silently double the memory footprint of every prompt.
- **Injection is budget-capped and drops oldest — never truncates a block mid-way.** Lumina's
  injector `break`s on the first oversized entry, silently dropping every later layer with no error.
  Cap per entry *and* total, and drop from the oldest end, so an oversized entry costs only itself.
- **Eviction protects curation.** Over the caps, the oldest *auto* facts and the oldest *turns* go
  first; a user-written fact is never pushed out by one the app invented or by conversation volume.
- **Scenario sessions are excluded** from both injection and sweeping, exactly like `rel` in
  `chat()` — a scenario is a deliberate alternate premise, and the main timeline's remembered
  history would contradict the state the preset just seeded.
- **Everything is best-effort.** A missing model, an onnxruntime failure, a corrupt index or a failed
  summarization degrade to "no block" and leave the chat turn untouched. Nothing in the memory path
  may raise into the hot path.

Added with Phase 6, for the assistant pipeline:

- **A separate module, not a branch.** The two pipelines *were* one, with `if chat_mode ==
  "assistant"` added as each leak was found: the relationship descriptor, then the narration example,
  then the "summarize the roleplay so far" prompt whose narrative prose was injected into every
  assistant turn as a worked example of narrating. Each fix was correct and each left another branch
  for the next feature to forget. A module with no roleplay framing in it has nothing to leak. The
  cost is deliberate duplication of the fold bookkeeping; that is the cheaper half.
- **An assistant has no story, so it gets no summary.** `notes` replace the rolling narrative
  entirely and are rewritten whole on each fold, so a finished item leaves. The companion summary may
  accrete because prose does; task state should shrink as work completes, and an empty list is the
  goal state, not a failed pass.
- **Instructions and facts are stored apart.** Asked for "durable facts" as one bucket, the model
  filed a form-of-address preference as a fact — true, and stored where nothing tells the assistant
  to *do* it. An instruction that is merely known is an instruction that gets ignored. An unprefixed
  extraction line is dropped rather than guessed at, since guessing reintroduces exactly this.
- **Directives are never dropped to fit a budget.** Facts and notes yield; instructions do not.
  Silently dropping one changes behaviour with no error and nothing in the transcript to explain it.
- **Assistant dedupe is string-level, not embeddings.** Directives are short, literal and few, and an
  assistant behaving correctly must not depend on an optional model being loaded. The companion
  pipeline compares embeddings because its facts are prose and get reworded.
- **Prompt rules that don't hold get enforced in code — now the default expectation.** Extraction
  reads user turns only; status notes and restated notes are filtered deterministically. That is five
  occasions in this codebase where a rule was present in a prompt, ignored, and had to be moved into
  code. Write the rule, measure it, and budget for enforcing it.
- **Tuning is measured, not eyeballed.** `eval/assistant_memory` exists because reading single runs
  gave the opposite verdict to three runs, in both directions. A rate that moves is evidence; a run
  that looks better is not.

## The fold prompt is tuned against observed failures

Every rule in `LONGTERM_PROMPT` is there because the 9B fine-tune broke that way in testing: "a few
paragraphs" produced florid essays; a bare bullet cap got padded to exactly the cap with invented
traits; the model recorded its own replies as memories; and folding a cancellation kept both "plans
to sail next month" and "trip is off" in one self-contradicting memory.

The same applies to auto-facts, where transient-vs-durable was the hard part: the first prompt pulled
"visiting at the end of summer" out of a sentence that also carried a durable fact, and only an
emphatic rule about dates and seasons separated them.

**Retune the prompts rather than lowering the budgets** — truncating mid-sentence is worse than
generating short.

Everything above was tuned by reading runs, which is how it was done before there was a harness. It
is also how two assistant-side fixes came to be recorded backwards (Phase 6). The companion prompts
have no fixture set yet, and the same risk applies to them: `eval/assistant_memory` is the pattern to
copy when one of them next needs changing.

## Phases

- **Phase 1 — Pinned-facts UI (quick win). ✅ DONE.** Per-session `MemoryState.pinned`, read/written
  through `GET/PUT /api/sessions/{id}/memory` and edited in the 📌 Memory modal. Injection was
  already wired in `prompt_builder`.

- **Phase 2 — Tunable + hardened summarizer. ✅ DONE.** The rolling summarizer's thresholds moved
  onto `AppSettings` as bounded fields with sliders in ⚙ Settings; the module constants remain the
  fallback. `kobold.generate_answer` prefills an empty `<think></think>` so summaries don't come back
  as reasoning, and `keep_recent` is clamped below `summarize_after` so a fold always covers ≥1 turn.

- **Phase 3 — Cross-session long-term memory. ✅ DONE.** *(Was Phase 4; promoted — see settled
  decisions.)* `CharacterMemory` per (character × persona), alongside the per-session summary rather
  than replacing it, with two write triggers and a budget-capped injected block.
  - `swept_sessions` (session_id → index already folded) replaced the planned `last_swept_session_id`:
    a single id can't express "this session grew, fold the new part", which both triggers need in
    order to be idempotent *and* re-runnable.
  - The **idle sweep** is **borrowed from Lumina's dream-sweep** and is the better of the two
    triggers: it captures a *finished* conversation instead of cutting mid-scene.
  - Facts round-trip **by id** through the API so editing in the modal can't relabel an `auto` fact
    as `user`.
  - `_user_root()` creates the new `memories/` dir for existing users on their next request, so no
    migration was needed.

- **Phase 4 — Embedding / vector memory (RAG). ✅ DONE.** *(Was Phase 3.)* Semantic recall layered
  onto Phase 3's store: int8 BGE-small-en-v1.5 on CPU, an index written inside the fold so both
  triggers stay in sync, and a margin-gated search.
  - The index file is `.vec.npz`, deliberately not `.json`, so no future `glob("*.json")` mistakes it
    for a model.
  - Facts are re-indexed **wholesale** on save (replace-by-kind) — they're editable and deletable, so
    that is both simpler and more correct than diffing.
  - **Caveat worth knowing:** with the Phase-3 summarizer tightened, a 7-topic conversation still
    fits in the bullet cap, so recall added nothing at that size. Its value is in pairings that
    accumulate well past it — verified by emptying the summary and confirming a fresh session could
    still answer three specifics from the index alone.

- **Phase 5 — Auto-fact extraction. ✅ DONE.** Durable facts pulled out after a fold or sweep and
  stored with `origin="auto"`, so the stable atoms survive the summary being rewritten wholesale on
  every fold. Reluctant by construction, deduped in code, evicted auto-first, tunable and switchable
  off in ⚙ Settings, and reviewable in the Memory modal — all as decided above.

- **Phase 6 — The assistant pipeline. ✅ DONE.** *(Not planned as a phase; forced by a bug class.)*
  `AssistantMemory` + `services/assistant_memory.py`, chosen by `card.chat_mode` in `chat()` and in
  the sweep endpoint. Directives, facts and task notes; no summary, no relationship, no scene, no
  recall. Mechanics in `MEMORY.md`.
  - **It started as branches and stopped being them.** Three leaks were fixed one at a time inside
    the shared path before it was clear the shared path *was* the bug. Splitting the module was the
    fourth fix and the only one that removes the class.
  - **`d4a51f3` shipped it, `70b9827` migrated Mira onto it.** The old companion store for a
    reclassified card is left on disk, intact and inert — reversible, and no migration to get wrong.
  - **The fold bookkeeping is reused, the output is not.** `_fold_chunks` and the high-water mark are
    domain-independent and already paid for; what comes out of a pass is directives, facts and notes,
    never a narrative. `session.memory.summary` is left empty on purpose so there is no prose to
    inject even by accident, while `last_summarized_index` still advances so the prompt stays bounded.
  - **The eval came before the last two fixes, and immediately contradicted me** (`9baf953`). Five
    labelled fixtures × 3 runs: `clean 7/15 · misfiled 0/15 · forbidden 7/15 · missed 4/15`. It found
    that the extractor **invents standing instructions out of nothing** — on pure small talk it stored
    "Address El Iguana as Sir" in all three runs, mined from the assistant's own politeness — and that
    of my two eyeball fixes the one I declared fixed scored 0/3 while the one I recorded as unfixed
    scored 3/3.
  - **Both repairs were deterministic, and both were rules the prompt already contained**
    (`2772fbc`): extraction reads user turns only, and status notes are dropped in code. Result
    `clean 12/15 · forbidden 1/15`. A third pass (`127690d`) added the subjectless status filter
    (kept, 3/3, and unit-tested so its correctness doesn't rest on the eval) and **reverted** a worked
    example that measured 0/6 against 1/9 for the described rule — recorded in the module so it is
    not retried on the same reasoning.
  - **Splitting the pipeline did not clean up the data it left behind.** A card switched to
    assistant mode keeps its sessions' roleplay summaries, and `summarize()` — their only writer —
    no longer runs, so the prose froze and kept being injected under a friendlier header. Two
    changes, deliberately at both ends: the read path skips it (immediate, and immune to however a
    summary got there) and the fold clears it (so it cannot return), gated on the fold having
    covered what the summary covered. The general lesson is the one this phase keeps teaching:
    changing which code runs does not change what is already on disk.
  - **One fixture was corrected, which is worth saying out loud.** `same-sentence` forbade a dated
    visit in *any* bucket, but a dated commitment is exactly what `notes` are for; it is now forbidden
    as a durable *fact* only. Moving a label to make a failure disappear is how an eval stops being
    worth running, so the test was that the fact-side expectation was untouched and the fixture still
    fails. It does.

- **Phase 7 — Memory that belongs to the bartender. ◻ PLANNED (2026-11-23).** Cantinero is a voice
  assistant, and its memory should follow the character, not the chat session. The long-term store
  is already per character, but the session still decides when a fold runs, what it reads, and
  what is in the prompt.

  **Why — where the session still decides (found reading the code, 2026-11-23):**
  - Folds fire on session events: `summarize_after` messages in one session, or a 5-minute idle
    sweep per session. Voice arrives as short bursts that may never reach the threshold, and the
    desktop window reopens the bartender's latest session (`pantheon_web/desktop.py`), so that
    session grows without end.
  - `fold()` rewrites `notes` whole from one session's transcript. The stored notes are in the
    prompt, but nothing stops a fold of chat B from dropping a thread only chat A knew about.
  - The store is keyed (character, persona): switching persona hides the bartender's standing
    instructions.
  - Pinned facts are per session, so the one thing the user curates doesn't carry over.
  - `block()` injects in list order and skips what doesn't fit 2,400 characters. Facts are
    appended, so once the store is full the **newest** facts are the ones dropped, silently.

  **Decided: shared basics.** Facts about the user are shared by every bartender in a library (name,
  time zone, family); standing instructions and task notes stay per bartender, and an instruction
  can be marked for all bartenders. Persona becomes a tag on an entry. Strictly per-character facts
  were rejected: telling each bartender your name separately is a chore, not a feature.

  **Kept from Phase 6, unchanged:** the instruction/fact split, extraction from user turns only, the
  deterministic note filters, string-level dedupe, directives never truncated, every step
  degrading to nothing, and the Memory modal as the correction surface.

  **Steps, in order** (each a GitHub issue; mechanics go in `MEMORY.md` as each ships):
  1. ✅ **#15 — eval fixtures first** (2026-11-23). Done as `eval/memory_phase7/`: five scenarios
     run through `main.memory_for_turn` on a scratch library. Baseline **2/15**. The Fortuna part,
     `eval/memory_decisions`, is #52 and #53.
  2. ✅ **#16 — the bartender owns memory** (2026-11-23; scenarios 2/15 → 5/15, shared basics 3/3). Shared user facts plus per-character instructions and
     notes; persona as a tag; pinned facts move into the store; a one-time merge of the
     `{cid}__{pid}.json` files, old files kept until checked.
  3. ✅ **#17 — notes as items** (2026-11-23; scenarios 5/15 → 9/15, cross-session thread 3/3). `id`, `status`, `created`, `touched`; a fold returns changes by id
     instead of a fresh list; notes untouched for about two weeks leave the prompt.
  4. ✅ **#18 — "lately with this bartender"** (2026-11-23; scenarios 9/15 → 12/15). The last few turns of her most recent conversation,
     whichever window it was in, so a new chat isn't a blank start.
  5. **#19 — fold per episode.** *(The end-of-chat triggers and the server-side idle sweep
     shipped first, on 2026-11-23: see "Closed since".)* An episode ends on the sleep phrase or a farewell, a quiet gap
     (about 10 minutes), the desktop window closing or the page hidden, or a new chat with her.
     One high-water mark per bartender, across sessions. The message-count trigger becomes context
     pressure: fold when `build_messages` is about to drop turns.
  6. **#20 — fact lifetimes.** `kind` lasting or time-bound with `expires`; `created`,
     `last_used`, `uses`. Answers the open decay question below.
  7. **#21 — tiered recall.** Always: directives plus a small core of user-written and most-used
     facts, in the system block. Per turn: other facts and notes scored against the latest user
     turn (keywords plus recency, with a minimum score), in the trailing note **after the
     history**, because a system block that changes every turn invalidates KoboldCPP's cached
     prefix. No model needed; embeddings only if keywords measure poorly.
  8. **#22 — memory commands.** "Remember…", "forget…", "what do you remember about me?" act at
     once through tool calling (verified over streaming in `SKILLS_PLAN.md`); the first item on the
     Menu.
  9. **#23 — Fortuna gate (optional).** A yes/no from Fortuna on whether an episode holds anything
     lasting, to skip extraction on small talk. Only if episode folds turn out to cost too much.

## Open decisions

- Whether long-term `facts` should ever expire/decay, or stay until the user removes them.
  (Lumina has no decay and its store grows unbounded — a caution, not a model to copy.)
  **Planned answer:** Phase 7's fact lifetimes (#20): time-bound facts expire, lasting ones stay.
- **Total prompt budget** — the long-term block is capped, but the prompt as a whole is not, and on
  overflow KoboldCPP truncates from the front, silently deleting the memory blocks. Measured and
  written up in `PROMPT_BUDGET.md`; the fix is proposed, not built. Directives make this sharper: the
  injector never truncates them, but nothing stops the *context* from doing it.
- **How to catch a stale assistant fact.** Work in progress still lands in `facts` ("we still need to
  finish the middleware"), where it outlives its truth. Two prompt revisions improved the split and a
  third without a new idea would be guessing; the alternatives are a durability re-check on fold, an
  age-out for `auto` entries, or accepting the modal as the correction surface. Related to the decay
  question above, and probably the same answer. **Planned:** the same answer (#20).
- **Whether the companion prompts get fixtures too.** `eval/assistant_memory` demonstrated that
  reading runs gives wrong answers; `LONGTERM_PROMPT`, `AUTO_FACT_PROMPT` and the recall gate
  (calibrated on 8 documents and 13 queries) were all tuned that way.
- **Runs per verdict.** Five fixtures × 3 runs separates a broken pipeline from a working one but not
  a prompt change from noise — `same-sentence` alone has scored 0/3, 0/3, 1/3, 0/6 and 2/3 on
  essentially the same code. More fixtures, and more runs for the unstable ones.

### Closed since (recorded here so they don't re-open)

- ~~Idle threshold for the sweep trigger~~ — shipped as `SWEEP_IDLE_MS` = 5 min (`app.js`),
  deliberately **not** user-tunable: it's a safety net behind the threshold trigger, not a
  preference, and a wrong value degrades quietly rather than visibly. **Moved to the server on
  2026-11-23** (`SWEEP_IDLE_S`): a browser timer died with the tab, so a chat closed before it
  fired lost its recent turns. Ending a chat now saves it at once, and the server timer plus a
  startup catch-up are the net (`MEMORY.md` → *When a sweep runs*). Part of Phase 7 step 5 (#19).
- ~~Token budget split between the long-term block and the session summary~~ — the long-term block
  has its own character budget, Phase 4 recall **shares** it, and the session summary is bounded
  separately by `summary_max_tokens`. The remaining gap is the *total*, above.
- ~~How aggressively Phase 5 extracts~~ — capped per fold, and since `157ae8d` user-tunable as
  `AppSettings.auto_facts_max_new` with a slider in ⚙ Settings → MEMORY. The right value is a
  judgement call: low over-selects, high risks storing marginal facts that then persist forever.

## Integration points (reuse, don't rebuild)

- `MemoryState` already has `pinned` + `summary` + `last_summarized_index` — extend, don't replace.
  Phase 3 added a *sibling* model for cross-session state; it does not fold into `MemoryState`.
- `build_messages` already injects pinned/summary into the system block — add parts, keep the order.
- `memory.summarize()` is the place to also update long-term memory / embeddings / extract facts.
- `_load_relationship`/`_save_relationship` + `GET/PUT /api/relationship` are the template for the
  per-pairing load/save/route trio.
- `Ctx` + `SUBDIRS` is how a new per-user data folder gets created and isolated (`memories`,
  then `assistant`, both added this way — an existing user gets the dir on their next request).
- `services/assistant_memory.py` reuses `_fold_chunks`/`_transcript` from `memory.py` and nothing
  else. Keep it that way: the import direction is one-way on purpose, and anything pulled the other
  way is a path a roleplay concept can travel down.
- `eval/assistant_memory/{fixtures,run.py,RESULTS.md}` is the template for measuring any prompt in
  this subsystem. `eval/` is deliberately not baked into the image — `podman cp eval companion:/app/eval`.
- Keep it **local-first** (no cloud), consistent with the rest of the stack.

## Evaluated and rejected

From Lumina's MemPalace design, after reading the source:

- **Lexical "AAAK" compression** (abbreviation map + filler-word stripping before storage). Measured
  on ordinary sentences: **29% size reduction**, against a claimed "~30x". It is also lossy in ways
  that change meaning — the abbreviation pass isn't word-bounded (`Sanders` → `S+ers`, `command` →
  `comm+`) and the filler strip removes `is/are/was/the/a/an/that`, which strands negation: *"she was
  not comfortable"* → *"she not comfortable"*. For this app that is disqualifying: the relationship
  system's whole thesis is that boundary phrasing is load-bearing and small models comply with
  anything vague. An LLM writing clean prose compresses harder *and* preserves meaning.
- **A deep storage taxonomy** (Wings → Rooms → Closets → Drawers, plus parallel "Halls", with the
  model choosing placement per write). Bought nothing there, because retrieval is substring `LIKE`.
  Structure should follow retrieval, not precede it — hence Phase 4 defining how recall works before
  any hierarchy gets added.
- **A second parallel store** (Lumina keeps a flat table *and* the palace, with write-through and a
  fallback search path). One store.
