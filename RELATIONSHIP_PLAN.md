# Relationship system — plan

Persistent, multi-axis relationship state per (character × persona), driving her behavior through
prompt injection. Built on the running mood/valence (`ChatSession.mood`, `expressions.EMOTION_VALENCE`).

See `services/relationship.py`, `models/schema.py` (`Relationship`, `RelationshipPreset`),
`prompt_builder.build_messages`, and the chat flow in `main.py`.

Phases 1–3 have shipped. This document is now primarily a **decision log** — the mechanics are in
the code, but the reasons behind the consent/boundary design are not, and they are the parts most
likely to be re-litigated or accidentally undone.

## Current state (what exists today)

Per (character × persona), stored at `data/u/<uid>/relationships/{cid}__{pid}.json`:

- **Four axes** (0–100): `affection`, `trust`, `attraction`, `tension`. Updated from the classified
  emotion + valence after every generated reply (skipped on re-rolls, so a swipe can't double-count).
- **Two intimacy meters**: `emotional_intimacy` (capped by trust) and `physical_willingness`
  (gated by trust × attraction) → a 7-rung physical ladder.
- **Context**: `power_dynamic` (−100…100) and `social_acceptance` (0…100), defaulting from the card,
  overridable per relationship via ⚙ on the rel-strip.
- **Derived**: `stage_of()` (Stranger → Intimate, "(Strained)" at high tension), `physical_tier()`,
  `emotional_level()`.
- **Milestones**: `rel.milestones` records each one-time crossing so it never fires twice.
- **Injection**: `descriptor()` into the system block, `boundary_note()` as a trailing system turn
  after the history.

Scenario sessions carry their own **session-scoped** `ChatSession.relationship` (seeded from a
`RelationshipPreset`) and never touch the persistent file.

## Settled design decisions

Recorded here because each was an open question and each now has a reason, not just an answer.

- **Scope = per (character × persona).** One record per pairing; a different persona is genuinely a
  different history. `CharacterMemory` was later built to mirror this exactly — same granularity,
  same `{cid}__{pid}.json` convention, same load/save/route trio. See `MEMORY_PLAN.md`.
- **Multi-axis, not a single "affection" score.** A single number can't express "trusts you deeply
  but isn't attracted to you" or "attracted but wary", which are the states that make the behavior
  injection worth having at all.
- **Interaction-driven, no idle/time decay.** Tension eases on *positive interaction*, not on the
  clock. Time-based decay punishes the user for not playing, and there is no principled rate for it.
- **Emotional intimacy is capped by trust** (`min(…, rel.trust + 20)`). Vulnerability that outruns
  trust reads as false. Physical willingness has a trust *floor* (≥15) and a combined trust ×
  attraction gate — slow start, faster once genuinely bonded.
- **Context slows the pace and adds caution — it never adds compliance.** Low `social_acceptance`
  makes her more guarded, discreet, and conflicted; a large `power_dynamic` in either direction makes
  her *more careful and deliberate*, explicitly "never more compliant". Writing it the other way
  would make the power slider a coercion dial, which is not a feature this app will have.
- **Consent is stated as genuine, enthusiastic, and revocable**, with boundaries that "reflect her
  real feelings, never obligation" — injected on every turn, not just at low tiers.
- **Boundary phrasing is load-bearing — name concrete acts, never only the abstract label.**
  The single most important finding in this system; details below.
- **Refusal must be sanctioned as a valid beat.** The `boundary_note` explicitly says that turning
  something down, hesitating, or changing the subject moves the scene forward. Without it, the
  single-beat / anti-repetition rules in the system prompt push the model to treat a refusal as
  stalling and write past it.
- **This is prompt steering, not a hard gate.** No code path blocks generation. With the default
  anti-refusal fine-tune (`…-Uncensored-…-Aggressive`) the model still complies sometimes. Anything
  built on top of this should assume that, not assume the ladder is enforced.

## Axes and movement

Deltas are scaled by valence intensity × `RATE` (5.0 = the most a single strong turn can move an
axis). Neutral emotion = no change.

- **Affection** tracks positive valence directly; drops on negative.
- **Attraction** moves on `_ATTRACTION` emotions (love, desire, admiration, excitement) only.
- **Trust** gains a base from any positive turn plus a bonus for `_TRUST_POS` emotions (gratitude,
  caring, approval, relief, admiration, love); negatives erode it faster than positives build it.
- **Tension** rises on negative valence, eases on positive.
- All clamped 0–100.

**Stages** are derived from `bond = (affection + trust) / 2`, with romance gated on **attraction**
rather than affection alone: `Stranger → Acquaintance → Friend → Close → Romantic → Intimate`.
Tension ≥ 60 appends "(Strained)".

## Intimacy and consent

The part with the most subtlety and the most risk. Two independent meters, because emotional
closeness and physical willingness genuinely dissociate.

### Emotional intimacy
`emotional_intimacy` → `EMOTIONAL_LEVELS`: guarded → reserved → open → vulnerable → deeply bonded.
Capped at `trust + 20`.

### The physical ladder
`physical_willingness` → one of 7 cumulative rungs (`PHYSICAL_TIERS`, thresholds `_TIER_THRESHOLDS`
= 8/22/36/50/64/78/90), from "none" through "uninhibited intimacy". Clinical labels on purpose — this
is abstract state the local model turns into prose, not prose itself.

Three forces move it:
- **`update()`** — positive valence raises it through a trust × attraction gate, scaled by
  `social_factor` (taboo pairings crawl: 0.4–1.0) and damped by power imbalance. Negative valence
  lowers it. Tension ≥ 50 actively pulls it back.
- **`escalate()`** — once intimacy has begun and she's positively engaged, `classify_activity()`
  reads which rung the reply actually depicted and nudges willingness toward the next one
  (`ESC_RATE` = 4.0). Acts at or above her current edge build the most (`proximity` 1.0 vs 0.4);
  tension ≥ 50 blocks escalation entirely. So *doing* things gradually opens her further, rather
  than willingness only tracking sentiment.
- **`_tension_cap()`** — high tension caps the *comfortable* rung down regardless of accumulated
  willingness (≥80 → 0, ≥60 → 2, ≥40 → 4). Distress overrides history.

### Boundary phrasing (the load-bearing finding)

Two failures, both live-observed against an anti-refusal fine-tune, fixed in `88701cf`:

1. **The abstract label was a null instruction.** The old wording — `willing up to "none" … engages
   at or below this` — names no act to decline at tier 0, and at every other tier never says what
   the refused thing *is*. `_TIER_ACTS` now names concrete acts per rung, and `_physical_clause()`
   states both what she is open to **and the FIRST rung she refuses**, so a 9B model has something
   to act on. `_label_with_acts()` drops the parenthetical where it would merely restate the label.
2. **Placement.** The full `descriptor()` sits in the system message, which is many turns back by
   mid-conversation, and small models weight recent tokens far more heavily. `boundary_note()`
   restates the same limit as a trailing system turn **after** the history — deliberately after
   `post_history_instructions`, so a card's UJB can't bury it. `kobold.chat_generate_stream`
   re-appends that trailing turn on every auto-continue round, since the continuation appends
   `"Continue."` after it and would otherwise demote it out of last position.

**Do not "simplify" either of these back.** The concrete-acts wording looks redundant next to the
tier label, and the re-injection looks like duplicated prompt text; both are load-bearing.

## Phases

- **Phase 1 — Axes + stage + behavior injection + widget. ✅ DONE.** Four axes, derived stage,
  `descriptor()` into the system prompt, rel-strip in the chat header (stage badge + 4 axis bars +
  Emotional/Comfort readout). Intimacy/consent (above) landed alongside it.

- **Phase 2 — Milestones. ✅ DONE.** `MILESTONES` + `newly_reached()`: crossing a stage or a
  physical rung for the first time fires a one-time toast (`done.milestones` on the chat SSE) and
  injects a one-time beat into the next reply via `session.pending_milestone`; the id is appended to
  `rel.milestones` so it never repeats. Six milestones: friends, close, romantic, intimate,
  first_kiss (tier ≥ 2), first_intimacy (tier ≥ 6).

- **Phase 3 — Content gating. ◻ MOSTLY DONE.**
  - ✅ **Stage-gated lorebook** — `LorebookEntry.min_stage`, filtered in
    `prompt_builder.select_lorebook` via `stage_rank`; dropdown in the card editor's lore repeater.
  - ✅ **Per-relationship context override** — ⚙ on the rel-strip → `PUT /api/relationship`
    (`power_dynamic` / `social_acceptance`; null clears back to the card default). `GET` returns the
    effective values plus `context_overridden`.
  - ◻ **Stage-tagged greetings** — the only open item in this plan. Needs the alt-greetings editor
    reworked from the current newline-joined textarea (`app.js:701`, `#ce-greetings`) into a
    repeater with a per-greeting `min_stage`, plus auto-picking a stage-appropriate opening at
    session creation (`main.py:789` currently takes `[first_mes] + alternate_greetings` by index).

### Adjacent, shipped separately
**Personas as cards + relationship presets + scenario new-chat** (Phases A/B/C in `CLAUDE.md`) all
landed. Note the deliberate rule: a preset's slider values apply in **`scenario` mode only** —
`continue` ignores the preset numbers and uses the persistent relationship.

## Open decisions

- Stage-tagged greetings: does a greeting get a single `min_stage`, or a range (so an early-stage
  opening isn't still eligible at Intimate)?
- Whether `classify_activity()`'s extra LLM round-trip per reply is worth its latency, or should be
  gated behind a setting the way `recall_enabled` is.
- Whether `RATE` (5.0) should be user-tunable. It's currently the one knob that changes pacing most
  and is only editable in code.

## Integration points (reuse, don't rebuild)

- `_load_relationship`/`_save_relationship` + `GET/PUT /api/relationship` are the template for any
  new per-pairing state — `CharacterMemory` already copies it exactly.
- `descriptor()` is the single place behavior guidance enters the system prompt; add to it rather
  than injecting a second parallel block.
- `boundary_note()`'s trailing-system-turn placement is shared machinery with
  `kobold.chat_generate_stream`'s auto-continue — changing either needs the other checked.
- `_stage_rank()` is the gate function for anything stage-conditional (lorebook today, greetings next).
- Relationship + memory both inject into the same context window and **nothing budgets the total**.
  On overflow KoboldCPP truncates from the front, silently deleting the relationship descriptor
  along with the rest of the system block. See `PROMPT_BUDGET.md`.

## Tuning knobs

`RATE` (5.0) · `WRATE` (6.0, local to `update()`) · `ESC_RATE` (4.0) · `_TIER_THRESHOLDS` ·
`_tension_cap()` bands · stage thresholds in `stage_of()` · the attraction gate for romance ·
`_ATTRACTION` / `_TRUST_POS` weights.
