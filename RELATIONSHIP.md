# Relationship system — how it works today

What the character feels, how those feelings move, and how they reach her replies. This describes
the **shipped** system; `RELATIONSHIP_PLAN.md` is the design/decision log (phases 1-3, with one open
item), and `MEMORY.md` covers the parallel system that shares the same per-pairing shape.

Code: `services/relationship.py`, `models/schema.py` (`Relationship`, `RelationshipPreset`),
assembled in `services/prompt_builder.build_messages()`, driven from the chat flow in `main.py`.

## State

One record per (character, persona) at `data/u/<uid>/relationships/{cid}__{pid}.json` — a different
persona is genuinely a different history. Same convention and load/save/route trio as
`CharacterMemory`.

| Field | Range | What it is |
|---|---|---|
| `affection` | 0-100 | how warmly she feels |
| `trust` | 0-100 | how safe she feels; gates everything else |
| `attraction` | 0-100 | romantic/physical pull; gates romance stages |
| `tension` | 0-100 | friction; suppresses intimacy and caps the ladder |
| `emotional_intimacy` | 0-100 | openness/vulnerability, capped at `trust + 20` |
| `physical_willingness` | 0-100 | her own openness to physical intimacy -> ladder rung |
| `power_dynamic` | -100..100 | per-relationship override of the card default |
| `social_acceptance` | 0-100 | per-relationship override of the card default |
| `milestones` | ids | one-time crossings already fired |

Four axes rather than a single "affection" score, because a single number cannot express "trusts you
deeply but isn't attracted to you" or "attracted but wary" — the states that make behaviour injection
worth having.

## How the axes move — `update()`

Called once per generated reply, from the classified emotion and its valence, and **skipped on
re-rolls** so a swipe cannot double-count. Deltas scale with valence intensity x `RATE` (5.0, the most
a single strong turn can move an axis); a neutral emotion changes nothing. All values clamp to 0-100.

- **Affection** tracks positive valence directly, drops on negative.
- **Attraction** moves only on `_ATTRACTION` emotions — love, desire, admiration, excitement.
- **Trust** takes a base from any positive turn plus a bonus for `_TRUST_POS` emotions (gratitude,
  caring, approval, relief, admiration, love). Negatives erode it faster than positives build it.
- **Tension** rises on negative valence, eases on positive.
- **Emotional intimacy** grows with positive valence but is capped at `trust + 20` — vulnerability
  that outruns trust reads as false.
- **Physical willingness** needs a trust floor (>= 15) and passes through a combined trust x
  attraction gate, so it starts slow and accelerates once she is genuinely bonded. Scaled by
  `social_factor` (0.4-1.0: taboo pairings crawl), damped by power imbalance, and actively pulled
  back while tension >= 50.

**Interaction-driven, with no idle or time decay.** Tension eases on positive interaction, not on the
clock — time-based decay punishes the user for not playing, and there is no principled rate for it.

## Derived state

- **`stage_of()`** — from `bond = (affection + trust) / 2`: Stranger -> Acquaintance -> Friend ->
  Close -> Romantic -> Intimate, with romance gated on **attraction** rather than affection alone.
  Tension >= 60 appends "(Strained)".
- **`emotional_level()`** — guarded -> reserved -> open -> vulnerable -> deeply bonded.
- **`physical_tier()`** — `physical_willingness` maps to one of 7 cumulative rungs (`PHYSICAL_TIERS`,
  thresholds 8/22/36/50/64/78/90), from "none" to "uninhibited intimacy", then `_tension_cap()` caps
  the *comfortable* rung down regardless of accumulated willingness (tension >= 80 -> 0, >= 60 -> 2,
  >= 40 -> 4). Distress overrides history. The rung labels are clinical on purpose: this is abstract
  state the local model turns into prose, not prose itself.

## Escalation — `escalate()`

Once intimacy has begun (tier >= 1) and she is positively engaged, `classify_activity()` runs a
zero-temperature pass over the reply to read which rung it actually depicted, and willingness is
nudged toward the next one (`ESC_RATE` = 4.0). Acts at or above her current edge build the most
(`proximity` 1.0 vs 0.4), social acceptance modulates the pace, and tension >= 50 blocks escalation
entirely. So *doing* things gradually opens her further, instead of willingness only tracking
sentiment.

## Context: power and social acceptance

`power_dynamic` and `social_acceptance` default from the card and are overridable per relationship
(the gear on the rel-strip -> `PUT /api/relationship`; null clears back to the card default).

**They slow the pace and add caution — they never add compliance.** Low social acceptance makes her
more guarded, discreet, and conflicted. A large power dynamic in either direction makes her *more
careful and deliberate*, worded explicitly as "never more compliant" when `{{user}}` holds the power
and "stays firmly in control of her own boundaries" when she does. Writing it the other way would
make the power slider a coercion dial, which is not a feature this app will have.

## Injection

Two places, both required:

- **`descriptor()`** goes into the system block: stage, axis levels, behaviour hint, emotional
  openness, the physical clause, the consent statement (genuine, enthusiastic, revocable, boundaries
  reflecting real feelings and never obligation — injected on every turn, not only at low tiers), and
  any context caveats.
- **`boundary_note()`** restates the same physical limit as a trailing system turn **after** the
  history, placed after `post_history_instructions` so a card's UJB cannot bury it.
  `kobold.chat_generate_stream` re-appends it on every auto-continue round, since the continuation
  appends `"Continue."` after it and would otherwise demote it out of last position.

### Boundary phrasing is load-bearing

Two live-observed failures against an anti-refusal fine-tune, both fixed in `88701cf`:

1. **The abstract label was a null instruction.** The old wording — `willing up to "none" … engages
   at or below this` — names no act to decline at tier 0, and at every other tier never says what the
   refused thing *is*. `_TIER_ACTS` now names concrete acts per rung and `_physical_clause()` states
   both what she is open to **and the first rung she refuses**, so a 9B model has something to act on.
   `_label_with_acts()` drops the parenthetical where it would merely restate the label.
2. **Placement.** The system message is many turns back by mid-conversation and small models weight
   recent tokens far more heavily, hence the trailing re-injection above.

`boundary_note()` also sanctions refusal as a valid beat — turning something down, hesitating, or
changing the subject moves the scene forward. Without that, the single-beat and anti-repetition rules
in the system prompt push the model to treat a refusal as stalling and write straight past it.

**Do not "simplify" either of these back.** The concrete-acts wording looks redundant next to the
tier label and the re-injection looks like duplicated prompt text; both are load-bearing.

**This is prompt steering, not a hard gate.** No code path blocks generation, and with the default
`…-Uncensored-…-Aggressive` fine-tune the model still complies sometimes. Anything built on top of
this should assume that rather than assume the ladder is enforced.

## Milestones

`MILESTONES` + `newly_reached()`: the first time a stage or physical rung is crossed, the chat SSE
`done` event carries a toast and a one-time beat is queued in `session.pending_milestone`, appended to
the relationship descriptor on the next reply. The id lands in `rel.milestones` so it never fires
twice. Six of them: friends, close, romantic, intimate, first_kiss (tier >= 2), first_intimacy
(tier >= 6).

## Stage gating

`_stage_rank()` is the gate function for anything stage-conditional. Today that is the lorebook:
`LorebookEntry.min_stage` is filtered in `prompt_builder.select_lorebook`, with a dropdown in the card
editor's lore repeater. Stage-tagged greetings are the remaining unbuilt item.

## Scenario sessions

A session created in `scenario` mode with a `RelationshipPreset` carries its own **session-scoped**
`ChatSession.relationship`, seeded by `from_preset()` and persisted with the session — the shared
per-pairing file is never touched, and cross-session memory is skipped for the same reason. A preset's
slider values apply in scenario mode **only**; `continue` ignores them and uses the persistent
relationship.

## API and UI

```
GET  /api/relationship?character_id=&persona_id=   summary + effective power/social + context_overridden
PUT  /api/relationship                             override context; null clears -> card default
GET/POST /api/presets ; GET/PUT/DELETE /api/presets/{id}    reusable scenario seeds
POST /api/chat                                     done event carries {relationship, milestones}
```

The chat header's rel-strip shows the stage badge, four axis bars, and the Emotional/Comfort readout,
with a gear for the per-relationship context override.

## Tuning knobs

`RATE` (5.0) · `WRATE` (6.0, local to `update()`) · `ESC_RATE` (4.0) · `_TIER_THRESHOLDS` ·
`_tension_cap()` bands · the stage thresholds in `stage_of()` · the attraction gate for romance ·
the `_ATTRACTION` / `_TRUST_POS` weights. All code-only today.

## Known gaps

- **Stage-tagged greetings** are unbuilt — session creation still picks from
  `[first_mes] + alternate_greetings` by index, and the alt-greetings editor is a newline-joined
  textarea rather than a repeater with a per-greeting `min_stage`.
- `classify_activity()` costs an extra LLM round-trip on every reply once intimacy has begun, and is
  not gated behind a setting the way `recall_enabled` is.
- `RATE` changes pacing more than any other knob and is editable only in code.
- The relationship descriptor and the memory blocks share one context window and **nothing budgets
  the total**. On overflow KoboldCPP truncates from the front, silently deleting the whole system
  block. Measured in `PROMPT_BUDGET.md`; the fix is proposed, not built.
