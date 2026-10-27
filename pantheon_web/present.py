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


def portrait_b64(card) -> str | None:
    """The card's profile picture, per its ``portrait_source``. A chosen source that
    has nothing yet falls back to the automatic chain: portrait, neutral sprite, 3D still."""
    src = card.portrait_source
    if src == "vrm" and card.vrm_still:
        return card.vrm_still
    if src == "sprite" and card.expressions.get(card.portrait_sprite or "neutral"):
        return card.expressions[card.portrait_sprite or "neutral"]
    return card.image or card.expressions.get("neutral") or card.vrm_still


def has_portrait(card) -> bool:
    return bool(portrait_b64(card))


def starters(card) -> list:
    """Conversation starters for the "Try saying" chips. Kept inside the card spec's
    ``extensions`` so a V2/V3 export round-trips them; edited in the Studio (Phase 4)."""
    ext = (card.extensions or {}).get("pantheon") or {}
    items = ext.get("starters") if isinstance(ext, dict) else None
    return [str(s) for s in (items or []) if str(s).strip()][:6]


def normalise_portrait(b64: str) -> str:
    """Any uploaded image -> PNG, at most 1024 px tall. Stored cards stay a sane size and
    the export (a PNG card) always has a PNG to embed."""
    import base64
    import io

    from PIL import Image

    raw = base64.b64decode(b64.split(",", 1)[-1])
    im = Image.open(io.BytesIO(raw))
    im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
    if im.height > 1024:
        im = im.resize((round(im.width * 1024 / im.height), 1024), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return base64.b64encode(out.getvalue()).decode()
