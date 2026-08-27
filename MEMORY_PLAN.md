# Memory system — plan and decision log

Ideas and decisions for what the companion remembers. Revised after reviewing Lumina's memory design
(a third-party agentic assistant with a "MemPalace" layered store) as a possible alternative:
**keep this plan**, take two ideas from it, reject its centrepiece. See "Evaluated and rejected" at
the bottom so that doesn't get re-litigated later.

**Mechanics are not in this file.** The scopes, the fold triggers, the recall gate, auto-fact
extraction, the budgets and every constant live in `MEMORY.md`, which is the file to edit when
behavior changes. This one records the *reasons* — what was open, what was decided, and what shipped.

Code: `services/memory.py`, `services/embeddings.py`, `models/schema.py` (`MemoryState`,
`CharacterMemory`), `prompt_builder.build_messages`, and the chat flow in `main.py`.

Phases 1–5 have all shipped.

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

## Open decisions

- Whether long-term `facts` should ever expire/decay, or stay until the user removes them.
  (Lumina has no decay and its store grows unbounded — a caution, not a model to copy.)
- **Total prompt budget** — the long-term block is capped, but the prompt as a whole is not, and on
  overflow KoboldCPP truncates from the front, silently deleting the memory blocks. Measured and
  written up in `PROMPT_BUDGET.md`; the fix is proposed, not built.

### Closed since (recorded here so they don't re-open)

- ~~Idle threshold for the sweep trigger~~ — shipped as `SWEEP_IDLE_MS` = 5 min (`app.js`),
  deliberately **not** user-tunable: it's a safety net behind the threshold trigger, not a
  preference, and a wrong value degrades quietly rather than visibly.
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
- `Ctx` + `SUBDIRS` is how a new per-user data folder gets created and isolated.
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
