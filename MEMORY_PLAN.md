# Memory enhancement plan

Ideas for improving the companion's memory. See `services/memory.py`, `models/schema.py`
(`MemoryState`), and `prompt_builder.build_messages`.

Revised after reviewing Lumina's memory design (a third-party agentic assistant with a
"MemPalace" layered store) as a possible alternative. Conclusion: **keep this plan**, take two
ideas from it, and explicitly reject its centrepiece. See "Evaluated and rejected" at the bottom
so this doesn't get re-litigated later.

## Current state (what exists today)

Per-session memory (`ChatSession.memory` = `MemoryState{summary, pinned, last_summarized_index}`),
assembled into the prompt in three tiers:
1. **Verbatim recent turns** — `messages[last_summarized_index:]` go in word-for-word.
   (`session.messages` keeps the *full* history on disk regardless; the index only marks how much
   the summary covers. Nothing is destroyed by folding.)
2. **Rolling summary** ("Story so far:") — older turns folded into `memory.summary`.
   - Trigger: `summarize_after` (default 24) unsummarized turns; keep `keep_recent` (12) verbatim.
   - Cumulative, best-effort (never blocks chat), ~400 tokens, via `kobold.generate_answer`.
3. **Pinned facts** ("Important facts:") — `memory.pinned`, always-on, user-curated.

Scope + gaps:
- **Per-session only** — new chats start empty. (**Relationship** state is separately persistent per
  character×persona; the **lorebook** is authored keyword-triggered memory — both distinct from this.)
- **No embeddings / RAG** — pure summarization, so precise old details survive only if the summary kept them.
- Bounded by the 8192-token context; summaries are lossy by design.

## Settled design decisions

Recorded here because each was an open question and each now has a reason, not just an answer.

- **Long-term scope = per (character × persona).** Mirrors `Relationship` exactly — same granularity,
  same `{cid}__{pid}.json` file convention, same "different persona is genuinely a different history"
  logic. Reuses `_load_relationship`/`_save_relationship` as the pattern to copy.
- **Cross-session persistence ships before embeddings.** Cross-session is a *missing capability* (she
  forgets you on a new chat); embeddings are a *retrieval-quality optimization* on top of it. The
  long-term store can ship with recency + pinned retrieval and gain semantic search later without
  changing its shape. Lumina is the cautionary case: it built an elaborate layered store first and its
  recall is still `content LIKE '%query%'`.
- **Embedder runs on CPU via ONNX — zero VRAM.** `bge-small-en` / `all-MiniLM-L6` are ~90–130 MB and
  `onnxruntime` is already in the image for rembg, Kokoro, and faster-whisper. Follows the established
  baked-`models/<name>/` convention. This closes the "share the 4070 with KoboldCPP + ComfyUI" question
  without a trade-off.
- **Auto-written memory is tagged and revertible.** Every fact carries its origin (`user` | `auto`) so
  the 📌 Memory modal can filter and revert what she decided to remember on her own. Auto-extraction
  without an undo path is not shippable.
- **Injection is budget-capped and drops oldest — never truncates a block mid-way.** Lumina's injector
  `break`s on the first oversized entry, silently dropping every later layer with no error. Cap per
  entry *and* total, and drop from the oldest end.

## Phases

- **Phase 1 — Pinned-facts UI (quick win). ✅ DONE.** Scope: **per-session** (`MemoryState.pinned`).
  `GET/PUT /api/sessions/{id}/memory` read/write `pinned[]` + `summary`; frontend `📌 Memory` header
  button opens a `#memory` modal — add/remove pinned facts + edit the rolling "Story so far" summary.
  Injection was already wired in `prompt_builder` (Important facts / Story so far).

- **Phase 2 — Tunable + hardened summarizer. ✅ DONE.** `summarize_after` / `keep_recent` /
  `summary_max_tokens` are on `AppSettings` (bounded `Field`s) and exposed as sliders in the ⚙ Settings
  modal (MEMORY section). `memory.summarize()`/`needs_summary()` take them as params (module constants
  remain the fallback); the chat flow passes the per-user settings. `kobold.generate_answer` prefills an
  empty `<think></think>` so summaries don't come back as reasoning. `keep_recent` is clamped below
  `summarize_after` so a fold always covers ≥1 turn.

- **Phase 3 — Cross-session long-term memory. ✅ DONE.** *(Was Phase 4; promoted — see settled
  decisions.)* A persistent store per (character × persona) carried across chats, alongside the
  per-session summary rather than replacing it.
  - **Models** (`models/schema.py`): `MemoryFact{id, text, origin: "user"|"auto", created_at}` and
    `CharacterMemory{character_id, persona_id, facts[], summary, swept_sessions, created_at, updated_at}`.
    `swept_sessions` (session_id → message index already folded) replaced the planned
    `last_swept_session_id`: a single id can't express "this session grew, fold the new part," which
    both triggers need in order to be idempotent *and* re-runnable.
  - **Store**: `data/u/<uid>/memories/{cid}__{pid}.json`, `memories` in `SUBDIRS` + `Ctx`.
    `_load_character_memory`/`_save_character_memory` mirror the relationship pair. `_user_root()`
    creates the dir for existing users on their next request — no migration needed.
  - **Write triggers** — both via `memory.fold_into_longterm()`:
    - *threshold* — the chat flow folds forward exactly what `summarize()` just folded out of the
      verbatim window (`upto=session.memory.last_summarized_index`), so nothing leaves the live prompt
      without being recorded durably first;
    - *idle* — `POST /api/sessions/{id}/sweep`, armed by the frontend `SWEEP_IDLE_MS` (5 min) timer
      after each completed turn. **Borrowed from Lumina's dream-sweep** and the better of the two: it
      captures a *finished* conversation instead of cutting mid-scene.
  - **Inject**: `memory.longterm_block()` → `build_messages(longterm_note=…)`, placed after pinned
    facts and before "Story so far". Budget-capped (`LONGTERM_BUDGET_CHARS`, header counted): the
    history is truncated to its share and facts drop oldest-first, so an oversized entry costs only
    itself. Facts are capped at `LONGTERM_MAX_FACTS` (40) on write so the store stays bounded.
  - **API**: `GET/PUT /api/memories?character_id=&persona_id=`, same shape as `/api/relationship`.
    Facts round-trip by `id` so editing through the modal can't relabel an `auto` fact as `user`.
  - **UI**: Memory modal gained a tab bar (This chat / Long-term); long-term facts carry an `auto`
    badge and are individually editable/removable. The tab is hidden for character-less and scenario
    sessions.
  - **Scenario sessions are excluded** from both injection and sweeping, exactly like `rel` in
    `chat()` — a scenario is a deliberate alternate premise, and the main timeline's remembered
    history would contradict the state the preset just seeded.

- **Phase 4 — Embedding / vector memory (RAG). ✅ DONE.** *(Was Phase 3.)* Semantic recall of past
  turns and facts, layered onto Phase 3's store.
  - **Encoder** (`services/embeddings.py`): int8 BGE-small-en-v1.5, ONNX, CPU, baked at
    `/app/models/embed` (33 MB). Chosen over fp32 (127 MB) by measurement — both scored 5/5 top-1 on
    the same probe with cosine within 0.02. Zero VRAM. CLS pooling + L2 norm; BGE's query prefix is
    applied to queries only. 143 ms cold load, ~2 ms per warm query.
  - **Index**: `data/u/<uid>/memories/{cid}__{pid}.vec.npz` (vectors + JSON meta in one file, non-
    `.json` suffix so no future `glob("*.json")` mistakes it for a model). Written inside
    `fold_into_longterm()` so both triggers stay in sync; facts re-indexed wholesale on
    `PUT /api/memories` (replace-by-kind). Capped at `RECALL_MAX_VECTORS` (2000), evicting oldest
    turns first — curated facts are never pushed out by conversation volume.
  - **USER turns only.** Her replies restate the user wrapped in personality; indexing them
    duplicated signal *and* dragged the median similarity up, defeating the gate below. Measured on a
    live index: with her turns in, 2 of 6 deliberately irrelevant queries slipped through; without,
    0 of 6, with all 5 relevant queries still correct.
  - **Relevance gate**: BGE cosine scores are not calibrated — "hello" scored 0.541 while the genuinely
    relevant "what can't I eat?" scored 0.523, so no fixed floor works. Gate is the margin between best
    and median hit (relevant ≥0.114, irrelevant ≤0.094 → threshold 0.10), plus a loose floor and a
    3-word minimum query. Calibrated on a small sample; a heuristic, not a validated threshold.
  - **Budget**: retrieval SHARES `LONGTERM_BUDGET_CHARS` with the Phase 3 block rather than adding to
    it, and drops any snippet whose text is already in that block.
  - **Settings**: `recall_enabled` / `recall_top_k` on `AppSettings`, with a toggle + slider in the ⚙
    Settings MEMORY section.
  - **Degrades to nothing**: no baked model, an onnxruntime failure, or a corrupt index all yield no
    recall block and leave the chat turn untouched. Verified for both failure modes.
  - **Caveat worth knowing**: with the Phase-3 summarizer tightened, a 7-topic conversation still fits
    in the 10-bullet summary, so recall added nothing at that size. Its value is in pairings that
    accumulate well past the bullet cap. Verified by emptying the summary and confirming a fresh
    session could still answer three specifics from the index alone.

- **Phase 5 — Auto-fact extraction. ✅ DONE.** After a fold or idle sweep, durable facts are pulled
  out of the conversation and stored with `origin="auto"`, so the stable atoms survive the summary
  being rewritten wholesale on every fold.
  - **Reluctant by construction**, because a wrong auto-fact outlives the summary that produced it:
    at most `AUTO_FACT_MAX_NEW` (3) per fold, "NONE is a good answer" in the prompt, temperature 0.2,
    and it only ever ADDS — user-written facts are never edited or deleted.
  - **Transient-vs-durable is the hard part.** The first prompt extracted "Nadia is visiting at the
    end of summer" out of a sentence that also carried the durable "Nadia is a paramedic in Leeds".
    The sharpened rule ("if it has a date, a season, or words like next/soon/visiting/coming, it is
    NOT durable — even in the same sentence as a durable fact") scored 4/4 on the probe set:
    extracted 3 from a fact-rich exchange, NONE from transient-only, NONE from small talk, NONE from
    her own offers, and exactly the 2 durable halves of a mixed sentence.
  - **Dedupe is in code, not the prompt** (`_dedupe_facts`). Told to skip known facts, the model
    re-emitted two of them verbatim. Cosine against existing facts (>= 0.86) catches that AND the
    rewordings a string match misses — verified live: restating dog/allergy/sister in fresh wording
    added nothing, while a genuinely new fact in the same sweep was added. Falls back to normalized
    string equality without the embedder.
  - **Eviction protects curation**: over `LONGTERM_MAX_FACTS`, the oldest AUTO facts go first; a
    user-written fact is never pushed out by one the app invented.
  - **Settings**: `auto_facts_enabled` (default on) with a toggle in ⚙ Settings → MEMORY. Verified
    that turning it off stops extraction entirely.
  - **Review surface**: facts land in 📌 Memory → Long-term with an AUTO badge, individually editable
    and deletable — the precondition this phase was gated on.

## Open decisions

- Idle threshold for the sweep trigger (minutes) and whether it's user-tunable in ⚙ Settings.
- Token budget split between the long-term block and the session summary at 8192 context.
- Whether long-term `facts` should ever expire/decay, or stay until the user removes them.
  (Lumina has no decay and its store grows unbounded — a caution, not a model to copy.)
- How aggressively Phase 5 extracts vs. keeping memory user-curated.

## Integration points (reuse, don't rebuild)

- `MemoryState` already has `pinned` + `summary` + `last_summarized_index` — extend, don't replace.
  Phase 3 adds a *sibling* model for cross-session state; it does not fold into `MemoryState`.
- `build_messages` already injects pinned/summary into the system block — add parts, keep the order.
- `memory.summarize()` is the place to also update long-term memory / embeddings / extract facts.
- `_load_relationship`/`_save_relationship` + `GET/PUT /api/relationship` are the template for the
  per-pairing load/save/route trio.
- `Ctx` + `SUBDIRS` is how a new per-user data folder gets created and isolated.
- Keep it **local-first** (no cloud), consistent with the rest of the stack.

## Evaluated and rejected

From Lumina's MemPalace design, after reading the source:

- **Lexical "AAAK" compression** (abbreviation map + filler-word stripping before storage). Measured on
  ordinary sentences: **29% size reduction**, against a claimed "~30x". It is also lossy in ways that
  change meaning — the abbreviation pass isn't word-bounded (`Sanders` → `S+ers`, `command` → `comm+`)
  and the filler strip removes `is/are/was/the/a/an/that`, which strands negation: *"she was not
  comfortable"* → *"she not comfortable"*. For this app that is disqualifying: the relationship system's
  whole thesis is that boundary phrasing is load-bearing and small models comply with anything vague.
  An LLM writing clean prose (`memory.summarize`) compresses harder *and* preserves meaning.
- **A deep storage taxonomy** (Wings → Rooms → Closets → Drawers, plus parallel "Halls", with the model
  choosing placement per write). Bought nothing there, because retrieval is substring `LIKE`. Structure
  should follow retrieval, not precede it — hence Phase 4 defining how recall works before any
  hierarchy gets added.
- **A second parallel store** (Lumina keeps a flat table *and* the palace, with write-through and a
  fallback search path). One store.
