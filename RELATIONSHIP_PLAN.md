# Relationship system — plan

Built on the running mood/valence (`ChatSession.mood`, `expressions.EMOTION_VALENCE`).

## Decisions (confirmed)
- **Scope:** per **character + persona**. One record per (character_id, persona_id).
- **Structure:** **multi-axis** meters.
- **Effects:** shape her behavior (prompt injection) + gate greetings/content + milestone moments.
- **Dynamics:** grows and can drop from interactions; **no idle/time decay**.

## Axes (0–100)
- **Affection** — warmth, closeness, care
- **Trust** — comfort, safety, openness
- **Attraction** — romantic/physical charge
- **Tension** — friction/conflict (high = strained)

## Movement (per reply, from classified emotion + valence)
Deltas scaled by valence intensity × slow rate (~1–3 pts/turn). Neutral = no change.
- Affection tracks positive valence; drops on negative.
- Attraction: love/desire/admiration/excitement.
- Trust: gratitude/caring/approval/relief/admiration; negatives reduce it.
- Tension: rises on negative valence, **eases on positive** (interaction-based, not time).
- All clamped 0–100.

## Stages (derived)
`Stranger → Acquaintance → Friend → Close → Romantic → Intimate`.
Romantic/Intimate **require Attraction over a threshold** (not just Affection). High Tension → "(Strained)".

## Effects
1. **Behavior** — inject a relationship descriptor into her system prompt each turn.
2. **Gating** — greetings/lorebook tagged with a min-stage; stage-appropriate opening; intimate content unlocks by stage. *(Phase 3)*
3. **Milestones** — first crossing of a tier fires a one-time beat + UI toast; recorded so it never repeats. *(Phase 2)*

## UI
Relationship strip: stage badge + 4 axis bars, persistent across chats. (Header mood meter stays for short-term mood.)

## Build surfaces
`schema.py` Relationship · `services/relationship.py` (update/stage/descriptor/milestones) ·
`main.py` (load+update in chat, `GET /api/relationship`, done event) · `prompt_builder.py` (inject) ·
frontend widget + toast.

## Phases
1. **Axes + stage + behavior injection + widget** ← building now
2. Milestones
3. Content gating (stage-tagged greetings/lorebook; card-editor fields)

## Tuning knobs
per-turn RATE, emotion→axis weights, stage thresholds, attraction gate for romance.
