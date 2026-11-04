# Relationship system — plan and decision log

Persistent, multi-axis relationship state per (character × persona), driving her behavior through
prompt injection. Built on the running mood/valence (`ChatSession.mood`, `expressions.EMOTION_VALENCE`).

**Mechanics are not in this file.** What the axes are, how they move, how the stage and the physical
ladder are derived, where everything is injected, and which knobs tune it all live in
`RELATIONSHIP.md`, which is the file to edit when behavior changes. This one records the *reasons* —
the questions that were open, what was decided, and what has and hasn't shipped. Those are the parts
most likely to be re-litigated or accidentally undone, and the only parts the code doesn't already
state.

Code: `muninn.relationship` and `muninn.models` (`Relationship`, `RelationshipPreset`, re-exported by
`models/schema.py`) in the shared Muninn package (`../muninn`),
`prompt_builder.build_messages`, and the chat flow in `main.py`.

Phases 1–3 have shipped, with one open item in Phase 3.

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
- **Emotional intimacy and physical willingness are separate meters**, because emotional closeness
  and physical willingness genuinely dissociate. Emotional intimacy is capped by trust —
  vulnerability that outruns trust reads as false. Physical willingness has a trust floor plus a
  combined trust × attraction gate, so it starts slow and accelerates once she is genuinely bonded.
- **The ladder's rung labels are clinical on purpose.** This is abstract state the local model turns
  into prose, not prose itself.
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

## Boundary phrasing (the load-bearing finding)

Two failures, both live-observed against an anti-refusal fine-tune, fixed in `88701cf`:

1. **The abstract label was a null instruction.** The old wording — `willing up to "none" … engages
   at or below this` — names no act to decline at tier 0, and at every other tier never says what
   the refused thing *is*. The fix names concrete acts per rung and states both what she is open to
   and the FIRST rung she refuses, so a 9B model has something to act on.
2. **Placement.** The full descriptor sits in the system message, which is many turns back by
   mid-conversation, and small models weight recent tokens far more heavily. The same limit is now
   restated as a trailing system turn after the history — deliberately after
   `post_history_instructions`, so a card's UJB can't bury it — and re-appended on every
   auto-continue round, since the continuation appends `"Continue."` after it and would otherwise
   demote it out of last position.

**Do not "simplify" either of these back.** The concrete-acts wording looks redundant next to the
tier label, and the re-injection looks like duplicated prompt text; both are load-bearing.

## Phases

- **Phase 1 — Axes + stage + behavior injection + widget. ✅ DONE.** Four axes, derived stage,
  `descriptor()` into the system prompt, rel-strip in the chat header (stage badge + 4 axis bars +
  Emotional/Comfort readout). Intimacy and consent landed alongside it.

- **Phase 2 — Milestones. ✅ DONE.** One-time toast + one-time injected beat the first time a stage
  or physical rung is crossed, recorded so it never repeats. Six milestones: friends, close,
  romantic, intimate, first_kiss, first_intimacy.

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
- Whether `RATE` should be user-tunable. It's currently the one knob that changes pacing most
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
