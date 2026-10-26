"""Presentation helpers shared by the BFF classes (plain module — imported normally and
cached, unlike the BFF files pytincture re-executes per call)."""
from __future__ import annotations


def tagline(card) -> str:
    """One short line for a card: creator notes, else personality, else the description's
    first sentence. Ember shows one under every name."""
    for text in (card.creator_notes, card.personality, card.description):
        line = " ".join((text or "").split())
        if line:
            first = line.split(". ")[0].rstrip(".")
            return first[:90] + ("…" if len(first) > 90 else "")
    return ""


def has_portrait(card) -> bool:
    return bool(card.image or card.expressions.get("neutral"))


def starters(card) -> list:
    """Conversation starters for the "Try saying" chips. Kept inside the card spec's
    ``extensions`` so a V2/V3 export round-trips them; edited in the Studio (Phase 4)."""
    ext = (card.extensions or {}).get("pantheon") or {}
    items = ext.get("starters") if isinstance(ext, dict) else None
    return [str(s) for s in (items or []) if str(s).strip()][:6]
