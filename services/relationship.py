"""Relationship system — multi-axis, per (character, persona).

Axes (0-100): affection, trust, attraction, tension. Updated each reply from the
classified emotion + valence. Grows and can drop from interactions; no idle decay.
"""
from __future__ import annotations

import re
from datetime import datetime

from models.schema import Relationship
from services.expressions import valence_of

RATE = 5.0   # max points a single strong turn can move an axis (tuning knob)
ESC_RATE = 4.0  # willingness gained per turn from actually engaging in physical acts (escalation)

# Emotions that build attraction / trust specifically (beyond generic valence).
_ATTRACTION = {"love": 1.0, "desire": 1.0, "admiration": 0.5, "excitement": 0.3}
_TRUST_POS = {"gratitude": 0.8, "caring": 0.7, "approval": 0.7, "relief": 0.6,
              "admiration": 0.4, "love": 0.5}


# Physical-intimacy ladder (clinical, cumulative; weighted toward the explicit end). No hard cap.
PHYSICAL_TIERS = [
    "none", "proximity & affectionate touch", "kissing & making out",
    "sensual touch over clothing", "manual stimulation", "oral intimacy",
    "intercourse", "uninhibited intimacy",
]
_TIER_THRESHOLDS = [8, 22, 36, 50, 64, 78, 90]  # willingness at which tiers 1..7 unlock

EMOTIONAL_LEVELS = [(75, "deeply bonded"), (55, "vulnerable"), (35, "open"),
                    (15, "reserved"), (0, "guarded")]


def _clamp(x: float) -> float:
    return round(max(0.0, min(100.0, x)), 2)


def _tier_from_willingness(w: float) -> int:
    t = 0
    for i, thr in enumerate(_TIER_THRESHOLDS, start=1):
        if w >= thr:
            t = i
    return t


def _tension_cap(tension: float) -> int:
    if tension >= 80:
        return 0
    if tension >= 60:
        return 2
    if tension >= 40:
        return 4
    return 7


def physical_tier(rel: Relationship) -> tuple[int, str]:
    """Comfortable rung right now: willingness gates it, high tension caps it down."""
    idx = min(_tier_from_willingness(rel.physical_willingness), _tension_cap(rel.tension))
    return idx, PHYSICAL_TIERS[idx]


def emotional_level(rel: Relationship) -> str:
    for thr, label in EMOTIONAL_LEVELS:
        if rel.emotional_intimacy >= thr:
            return label
    return "guarded"


def update(rel: Relationship, emotion: str, power: float = 0.0, social: float = 50.0,
           valence: float | None = None) -> Relationship:
    """Apply one reply's emotional read to the relationship axes (mutates + returns rel).

    `power` (-100..100) and `social` (0..100) are the resolved context (card default or
    per-relationship override). They dampen physical willingness and add caution — never compliance.
    """
    e = emotion or "neutral"
    v = valence_of(e) if valence is None else valence   # -1..1

    # --- core axes ---
    rel.affection = _clamp(rel.affection + v * RATE)
    if e in _ATTRACTION:
        rel.attraction = _clamp(rel.attraction + _ATTRACTION[e] * RATE)
    # Trust: base from any positive interaction + a bonus for trust-building emotions; negatives erode.
    if v >= 0:
        rel.trust = _clamp(rel.trust + v * RATE * 0.5 + _TRUST_POS.get(e, 0.0) * RATE * 0.6)
    else:
        rel.trust = _clamp(rel.trust + v * RATE * 0.7)
    if v < 0:
        rel.tension = _clamp(rel.tension + (-v) * RATE)
    else:
        rel.tension = _clamp(rel.tension - v * RATE * 0.8)

    # --- emotional intimacy: grows with positive valence, capped by trust (vulnerability needs trust) ---
    rel.emotional_intimacy = _clamp(min(rel.emotional_intimacy + v * RATE * 0.9, rel.trust + 20))

    # --- physical willingness: needs a trust floor + attraction; scaled by acceptance,
    #     dampened by power imbalance; suppressed by tension. The character's OWN openness. ---
    social_factor = 0.4 + 0.6 * (social / 100.0)      # taboo pairings crawl
    power_stress = abs(power) / 100.0
    WRATE = 6.0
    if v > 0 and rel.trust >= 15:
        # Combined trust x attraction gate: accelerates as both build (slow start, faster once bonded).
        gate = max(0.0, min(1.0, (rel.trust / 100.0) * (rel.attraction / 100.0) * 2.2))
        rel.physical_willingness = _clamp(
            rel.physical_willingness + v * WRATE * gate * social_factor / (1 + power_stress))
    elif v < 0:
        rel.physical_willingness = _clamp(rel.physical_willingness + v * RATE * 0.5)
    if rel.tension >= 50:                             # discomfort pulls her back
        rel.physical_willingness = _clamp(rel.physical_willingness - (rel.tension - 50) / 10.0)

    rel.updated_at = datetime.now().isoformat()
    return rel


async def classify_activity(kobold, reply: str) -> int:
    """Estimate the most explicit physical-intimacy tier (0-7) depicted in the reply."""
    if not reply.strip():
        return 0
    options = "; ".join(f"{i}={l}" for i, l in enumerate(PHYSICAL_TIERS))
    prompt = (
        "Read the passage and identify the MOST explicit physical intimacy actually depicted. "
        f"Pick the single number: {options}. Respond with ONLY that number.\n\n"
        f"Passage:\n{reply[:1500]}\n\nNumber:"
    )
    try:
        out = await kobold.generate_answer(prompt, {"max_length": 8, "temperature": 0.0, "top_p": 1.0})
    except Exception:
        return 0
    m = re.search(r"[0-7]", out or "")
    return int(m.group()) if m else 0


def escalate(rel: Relationship, act_tier: int, social: float) -> None:
    """Engaging in physical acts builds willingness toward the next rung (escalation).

    Only when she's comfortable and not tense; taboo/low social acceptance adds hesitancy
    (smaller gain). Acts at/above her current comfort edge build the most.
    """
    if act_tier < 1 or rel.tension >= 50:
        return
    social_factor = 0.4 + 0.6 * (social / 100.0)          # taboo -> slower, more hesitant
    cur = _tier_from_willingness(rel.physical_willingness)
    proximity = 1.0 if act_tier >= cur else 0.4           # at her edge builds most
    rel.physical_willingness = _clamp(rel.physical_willingness + ESC_RATE * social_factor * proximity)


def stage_of(rel: Relationship) -> str:
    a, t, at = rel.affection, rel.trust, rel.attraction
    bond = (a + t) / 2
    if at >= 70 and a >= 70 and t >= 60:
        base = "Intimate"
    elif at >= 40 and a >= 50 and bond >= 55:
        base = "Romantic"
    elif bond >= 65:
        base = "Close"
    elif bond >= 40:
        base = "Friend"
    elif bond >= 15:
        base = "Acquaintance"
    else:
        base = "Stranger"
    if rel.tension >= 60:
        base += " (Strained)"
    return base


_HINT = {
    "Stranger": "reserved and polite, still sizing {user} up",
    "Acquaintance": "friendly but a little guarded",
    "Friend": "warm, comfortable, and casual",
    "Close": "openly fond, trusting, and emotionally close",
    "Romantic": "flirtatious and romantically affectionate toward {user}",
    "Intimate": "deeply intimate, loving, and physically at ease with {user}",
}


def _level(x: float) -> str:
    return ("very high" if x >= 80 else "high" if x >= 60 else "moderate" if x >= 35
            else "low" if x >= 15 else "minimal")


def descriptor(rel: Relationship, char: str, user: str,
               power: float = 0.0, social: float = 50.0) -> str:
    """Relationship summary + behavior/consent guidance, for the system prompt."""
    stage = stage_of(rel)
    base_stage = stage.replace(" (Strained)", "")
    hint = _HINT.get(base_stage, "").replace("{user}", user)
    parts = [f"Affection {_level(rel.affection)}", f"trust {_level(rel.trust)}",
             f"attraction {_level(rel.attraction)}"]
    if rel.tension >= 25:
        parts.append(f"tension {_level(rel.tension)}")
    strained = " There is real friction between you right now." if "(Strained)" in stage else ""

    _, tier = physical_tier(rel)
    intimacy = (f' Emotional openness: {emotional_level(rel)}. Physical comfort right now: she is '
                f'willing up to "{tier}" and no further — she engages warmly at or below this and '
                "gently declines or slows anything beyond it. Her consent is genuine, enthusiastic, "
                "and revocable at any time; her boundaries reflect her real feelings, never obligation.")
    ctx = ""
    if social < 40:
        ctx += (" This relationship carries social stigma and she is aware of it — she stays more "
                "guarded, discreet, and conflicted about it.")
    if abs(power) >= 40:
        if power < 0:
            ctx += (f" {user} holds real power or authority over {char}; this makes her more careful "
                    "and deliberate about intimacy, never more compliant.")
        else:
            ctx += (f" {char} holds real power or authority over {user}; she stays firmly in control "
                    "of her own boundaries.")

    return (f"Relationship between {char} and {user}: {stage}. "
            + ", ".join(parts) + ". "
            + f"{char} behaves accordingly — {hint}.{strained}"
            + intimacy + ctx
            + " Let the relationship evolve gradually and naturally; do not skip ahead of this level.")


# ---------------------------------------------------------------------------
# Milestones — one-time beats fired the first time a tier/stage is crossed.
# {char}/{user} are substituted by the caller. Order = display order.
# ---------------------------------------------------------------------------
_STAGE_ORDER = ["Stranger", "Acquaintance", "Friend", "Close", "Romantic", "Intimate"]


def _stage_rank(rel: Relationship) -> int:
    s = stage_of(rel).replace(" (Strained)", "")
    return _STAGE_ORDER.index(s) if s in _STAGE_ORDER else 0


MILESTONES = [
    {"id": "friends", "test": lambda r: _stage_rank(r) >= 2,
     "toast": "{char} considers you a real friend now.",
     "beat": "{char} and {user} have just become genuine, comfortable friends — let that settled warmth show."},
    {"id": "close", "test": lambda r: _stage_rank(r) >= 3,
     "toast": "You and {char} have grown close.",
     "beat": "{char} feels genuinely close to and trusting of {user} now — let a moment of that emotional closeness surface."},
    {"id": "romantic", "test": lambda r: _stage_rank(r) >= 4,
     "toast": "❤️ Things with {char} have turned romantic.",
     "beat": "The bond between {char} and {user} has just crossed into romance — if the moment is right, let that shift into mutual romantic feeling happen naturally."},
    {"id": "intimate", "test": lambda r: _stage_rank(r) >= 5,
     "toast": "You and {char} share a deep intimacy now.",
     "beat": "{char} and {user} share deep intimacy and trust now — let that closeness color the reply."},
    {"id": "first_kiss", "test": lambda r: physical_tier(r)[0] >= 2,
     "toast": "{char} is ready for a first kiss.",
     "beat": "{char} is comfortable kissing {user} for the first time — if the moment feels right, let a tender first kiss happen naturally and mutually."},
    {"id": "first_intimacy", "test": lambda r: physical_tier(r)[0] >= 6,
     "toast": "{char} is ready to be fully intimate with you.",
     "beat": "{char} is now comfortable being fully physically intimate with {user} for the first time — let it unfold naturally and consensually, only if the moment is right."},
]


def from_preset(preset, character_id: str, persona_id: str) -> Relationship:
    """Build a session-scoped Relationship seeded from a RelationshipPreset."""
    return Relationship(
        character_id=character_id, persona_id=persona_id,
        affection=preset.affection, trust=preset.trust, attraction=preset.attraction,
        tension=preset.tension, emotional_intimacy=preset.emotional_intimacy,
        physical_willingness=preset.physical_willingness,
        power_dynamic=preset.power_dynamic, social_acceptance=preset.social_acceptance,
    )


def newly_reached(rel: Relationship) -> list[dict]:
    """Milestones whose condition is now met but not yet recorded in rel.milestones."""
    return [m for m in MILESTONES if m["id"] not in rel.milestones and m["test"](rel)]


def summary(rel: Relationship) -> dict:
    idx, tier = physical_tier(rel)
    return {"affection": rel.affection, "trust": rel.trust, "attraction": rel.attraction,
            "tension": rel.tension, "stage": stage_of(rel),
            "emotional_intimacy": rel.emotional_intimacy, "emotional_level": emotional_level(rel),
            "physical_willingness": rel.physical_willingness,
            "physical_tier": idx, "physical_tier_label": tier}
