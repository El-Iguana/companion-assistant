# Memory enhancement plan

Ideas for improving the companion's memory. Captured to work out later — not yet built.
See `services/memory.py`, `models/schema.py` (`MemoryState`), and `prompt_builder.build_messages`.

## Current state (what exists today)

Per-session memory (`ChatSession.memory` = `MemoryState{summary, pinned, last_summarized_index}`),
assembled into the prompt in three tiers:
1. **Verbatim recent turns** — `messages[last_summarized_index:]` go in word-for-word.
2. **Rolling summary** ("Story so far:") — older turns folded into `memory.summary`.
   - Trigger: `SUMMARIZE_AFTER = 24` unsummarized turns; keep `KEEP_RECENT = 12` verbatim.
   - Cumulative (folds new events into the existing summary), best-effort (never blocks chat),
     ~400 tokens, via raw `/api/v1/generate`.
3. **Pinned facts** ("Important facts:") — `memory.pinned`, always-on. **Latent: no API/UI to edit yet.**

Scope + gaps:
- **Per-session only** — new chats start empty. (The **relationship** state is separately persistent per
  character×persona; the **lorebook** is authored keyword-triggered memory — both distinct from this.)
- **No embeddings / RAG** — pure summarization, so precise old details survive only if the summary kept them.
- Bounded by the 8192-token context; summaries are lossy by design.

## Enhancements (rough phases — reorder as we like)

- **Phase 1 — Pinned-facts UI (quick win). ✅ DONE.** Scope chosen: **per-session** (`MemoryState.pinned`).
  `GET/PUT /api/sessions/{id}/memory` (main.py) read/write `pinned[]` + `summary`; frontend `📌 Memory`
  header button opens a `#memory` modal — add/remove pinned facts + edit the rolling "Story so far" summary.
  Injection was already wired in `prompt_builder` (Important facts / Story so far). Auto-pin by the model is
  deferred to the optional auto-fact-extraction item below.
- **Phase 2 — Tunable + hardened summarizer.** Expose `SUMMARIZE_AFTER` / `KEEP_RECENT` / summary token cap in
  ⚙ Settings. Harden the summary pass against the heavy-thinking model (append `/no_think`, maybe raise the
  cap) so summaries never come back empty/truncated. Consider summarizing via chat-completions for template
  correctness.
- **Phase 3 — Embedding / vector memory (RAG).** Semantic recall of old turns/facts on demand.
  - **Embed** what: individual turns, summary chunks, and/or pinned facts.
  - **Backend**: KoboldCPP can serve `/v1/embeddings` (needs an embeddings model loaded) — or a small local
    embedder. Decide GPU/VRAM cost vs. the chat model.
  - **Store**: per-user, per (character[, persona]) — JSON + numpy cosine for small N, or sqlite/FAISS if it grows.
  - **Retrieve**: top-k relevant chunks by the latest user turn, inject as a "Relevant memories:" block
    (budget-limited), alongside the rolling summary.
- **Phase 4 — Cross-session / long-term character memory (optional).** A persistent memory store per
  (character, persona) that carries across chats (like the relationship does), separate from the
  per-session summary. Ties naturally into Phase 3's store.
- **Auto-fact extraction (optional).** After replies, extract salient durable facts and auto-pin them
  (with the user able to review/remove) — reduces reliance on lossy summaries.

## Open decisions
- Pinned/long-term scope: **per-session** vs **per-character** vs **per (character×persona)**.
- Embedding backend + VRAM budget (share the 4070 with KoboldCPP + ComfyUI).
- Store format/tech as it scales (flat JSON → sqlite/FAISS).
- How aggressively to auto-extract vs. keep memory user-curated.

## Integration points (reuse, don't rebuild)
- `MemoryState` already has `pinned` + `summary` + `last_summarized_index` — extend, don't replace.
- `build_messages` already injects pinned/summary into the system block — add a "Relevant memories:" part.
- `memory.summarize()` is the place to also update embeddings / extract facts.
- Keep it **local-first** (no cloud), consistent with the rest of the stack.
