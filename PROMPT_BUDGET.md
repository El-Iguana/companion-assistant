# Prompt budget — shared decision doc

Owned jointly by `RELATIONSHIP_PLAN.md` and `MEMORY_PLAN.md`. Both systems inject into the same
context window, both are capped only in isolation, and **nothing accounts for the total**. This file
records what actually goes into a prompt, what happens when it overflows, and what to do about it.

Status: **steps 1 and 3 of the proposed design are built (2026-10-30)**: the app reads the real
limit, Settings can choose a smaller standard window, and history is trimmed by tokens to fit
(`services/context.py`). Steps 2 (priority fill), 4–5 (eviction order and caps for the system
block), 6 (UI warning) and 7 (slider bounds) are still open.

## The context window is the backend default, not a choice

`koboldcpp/start.sh` passes no `--contextsize`. The live backend reports:

```
GET /api/extra/true_max_context_length -> 8192
```

So the "8192 context" both plans assume is a **coincidence of the KoboldCPP default** for the current
build, not a configured value the app knows. The app never reads it, never sends a context length,
and has no constant for it. Swapping models, upgrading KoboldCPP, or adding `--contextsize` changes
the real limit and nothing in the app notices.

**Decided and built (2026-10-30):** `kobold.context_limit()` reads `true_max_context_length`
(cached 30 s, so a backend restart with a new `--contextsize` is seen within that; the last good
value is kept while the backend is down, 8192 if it was never seen). ⚙ Settings → **Context
window** offers Auto or 4096 / 6144 / 8192 / 12288 / 16384 / 24576 / 32768, clamped to the
backend's number. The backend's own size is `KOBOLDCPP_CONTEXT` in ai-stack's `koboldcpp/model.env`.

## What overflow actually does — measured, and it's bad

Tested against the live backend on the same endpoint the app uses (`/v1/chat/completions`, the path
behind `kobold.chat_generate_stream`). A system message with a unique marker at the **front**
(`SECRET-A is MERIDIAN`) and another at the **end** (`SECRET-B is ZEPHYR`), padded with filler:

| System block size | "What is SECRET-A?" (front) | "What is SECRET-B?" (end) |
|---|---|---|
| 2,678 tok (fits) | MERIDIAN ✅ | ZEPHYR ✅ |
| 9,818 tok (overflows) | **ZEPHYR ❌ — front is gone** | ZEPHYR ✅ |

**KoboldCPP truncates from the front and keeps the most recent tokens.** There is no error, no
warning, no log line, and nothing in the SSE stream signals it. The app cannot currently tell the
difference between a healthy prompt and one that has had its head cut off.

What this destroys, in the order it gets destroyed — the entire system block goes first:

1. The system prompt (the single-beat / stay-in-character / don't-write-for-`{{user}}` rules)
2. "About `{{user}}`" — the persona
3. The **relationship descriptor** (stage, axes, consent framing, the physical clause)
4. Lorebook entries, character definition, `mes_example`
5. Pinned facts → **long-term memory block** → "Story so far"

In other words: overflow silently deletes precisely the state that `RELATIONSHIP_PLAN.md` and
`MEMORY_PLAN.md` exist to maintain, and does it without telling anyone. She stops being the
character, forgets the relationship, and forgets everything remembered about the user — all while
appearing to work.

**One accidental saving grace:** `boundary_note()` is injected last, after `post_history_instructions`
(a placement chosen so a card's UJB couldn't bury it — see `RELATIONSHIP_PLAN.md`). Front-truncation
means that decision *also* makes the physical boundary the most overflow-resistant thing in the
prompt. Do not move it.

## What's in a prompt, and what's capped

Measured on live data (`podman exec` against the running container), with token counts from the
backend's own tokenizer via `/api/extra/tokencount`. Calibration: **~5.0 chars/token** for this
model on this content — useful because every cap in the codebase is written in *characters*.

Current real session (assistant-type card, no lorebook, 13 turns): **2,793 tok of 8,192** — healthy.
That's the point: nothing is broken today, and nothing warns you as you approach the edge.

| Part | Cap | Where |
|---|---|---|
| Long-term memory block | **2400 ch ≈ 480 tok** (shared with recall) | `LONGTERM_BUDGET_CHARS` |
| Session summary | **400 tok** (settings: 100–1200) | `summary_max_tokens` |
| Relationship descriptor | bounded by construction, ~149 tok measured | `descriptor()` |
| Boundary note | bounded by construction, ~84 tok measured | `boundary_note()` |
| Built-in system prompt | fixed, ~130 tok | `DEFAULT_ROLEPLAY` |
| Reply | 220 tok, ×2 on auto-continue | `GenerationConfig.max_tokens` |
| — | — | — |
| Tool schemas, if skills ship | **none** — +51 (System Info) / +218 (DJ) / +256 (both), every turn | `SKILLS_PLAN.md` |
| Custom `system_prompt` | **none** | card |
| Persona (title + description + background) | **none** | card |
| Character def (description + personality + scenario) | **none** | card |
| `mes_example` | **none** | card |
| Lorebook — *all* triggered entries | **none** | `select_lorebook()` |
| `post_history_instructions` | **none** | card |
| Pinned facts | **none** | `MemoryState.pinned` |
| **Verbatim history** | **turn count only, never tokens** | `summarize_after` |

### History is the dominant term and it's bounded by the wrong unit

`build_messages` takes `session.messages[last_summarized_index:]`, and `summarize()` runs *after*
the reply — so the window oscillates between `keep_recent` (12) and `summarize_after` (24) **turns**,
with no token ceiling. Measured on the live session: ~517 ch ≈ 103 tok per message, but that's a
terse assistant chat. A roleplay card generating to the 220-token cap (440 with auto-continue) makes
char turns 300–440 tok.

Worked example — content-rich RP card, default settings, full verbatim window:

```
system prompt        130      char def            700
persona              100      mes_example         500
rel descriptor       149      lorebook (6 fired)  480
longterm block       480      summary             400
boundary note         84      history (24 turns) 4920
                                          total ~7943 tok
                              + reply 220        = 8163  of 8192
```

That is inside the window by ~0.4%. Any one of a longer custom system prompt, a few more pinned
facts, two more lorebook triggers, or raising `summarize_after` tips it over — silently.

And the settings sliders reach a **guaranteed** overflow: `summarize_after` maxes at **200** turns
(≈20,000 tok of history alone) and `summary_max_tokens` at 1200. Nothing warns the user that these
combinations cannot fit.

## Proposed design (not built)

1. **Learn the real limit.** Read `true_max_context_length` at startup alongside the existing
   `/api/status` model probe; fall back to 8192. Everything below budgets against that number.
2. **Reserve, then fill.** Reserve `max_tokens × 2` (auto-continue) plus a safety margin for the
   reply. Fill the rest in priority order rather than assembling blindly.
3. **Trim history by tokens, not turns.** The verbatim window should be capped by *estimated tokens*
   (chars ÷ 5.0 is good enough; `/api/extra/tokencount` if exactness is ever needed) and drop whole
   oldest turns until it fits. `summarize_after` stays as the *fold* trigger; this is a separate
   safety valve for when a fold hasn't happened yet or turns are unusually long.
4. **Priority order for eviction**, coarsest first — deliberately the inverse of what front-truncation
   does today:
   `mes_example` → lorebook (lowest `insertion_order` first) → session summary → long-term block →
   older history → char def. **Never** evict: the system prompt, persona, relationship descriptor,
   or boundary note.
5. **Cap what's uncapped.** Per-part ceilings for `mes_example`, total lorebook, and pinned facts,
   following the pattern `longterm_block()` already established (cap per entry *and* total, drop from
   the oldest/lowest-priority end, never truncate a block mid-way).
6. **Make it visible.** Log the assembled prompt's token estimate per turn; surface a warning in the
   UI when a session is within ~10% of the limit. The current failure is invisible, which is what
   makes it dangerous.
7. **Bound the settings sliders** to combinations that can actually fit, or warn in the ⚙ modal.

### Why not just raise `--contextsize`?

It defers the problem instead of fixing it, and it isn't free: KV cache grows with context and this
box shares 12 GB of VRAM between KoboldCPP and ComfyUI (see the root `CLAUDE.md` VRAM note). Worth
doing *as well* — `--contextsize 16384` on a 9B Q4_K_M is affordable — but the eviction order is the
part that determines whether a full session degrades gracefully or silently loses its memory.

## Verification notes

- Truncation direction and the 8192 limit were tested live against the running `koboldcpp` container
  on 2026-08-17, model `Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M`.
- Per-part sizes came from building a real prompt with `prompt_builder.build_messages` inside the
  running `companion` container against live user data.
- The 5.0 ch/tok ratio is model- and content-specific. Re-measure before relying on it for a
  different model.
