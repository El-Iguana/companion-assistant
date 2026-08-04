"""Character Card V2/V3 import + export.

Cards are PNG files with the character JSON embedded in a tEXt chunk:
  - V2: keyword `chara`  -> base64( JSON {"spec":"chara_card_v2", "data":{...}} )
  - V3: keyword `ccv3`   -> base64( JSON {"spec":"chara_card_v3", "data":{...}} )

We also accept/emit plain `.json` cards. On import the PNG itself is kept as the
portrait (base64) so the card art is preserved.
"""
from __future__ import annotations

import base64
import io
import json
from typing import Any, Dict

from PIL import Image, PngImagePlugin

from models.schema import CharacterCard, CharacterBook, LorebookEntry


# ---------------------------------------------------------------------------
# spec dict  <->  CharacterCard
# ---------------------------------------------------------------------------
def _book_from_spec(d: Dict[str, Any] | None) -> CharacterBook | None:
    if not d:
        return None
    entries = []
    for e in d.get("entries", []) or []:
        entries.append(LorebookEntry(
            keys=e.get("keys", []) or [],
            content=e.get("content", "") or "",
            enabled=e.get("enabled", True),
            insertion_order=e.get("insertion_order", 100),
            case_sensitive=e.get("case_sensitive", False) or False,
            name=e.get("name", "") or e.get("comment", "") or "",
            priority=e.get("priority", 10) or 10,
            selective=e.get("selective", False) or False,
            secondary_keys=e.get("secondary_keys", []) or [],
            constant=e.get("constant", False) or False,
            position=e.get("position", "before_char") or "before_char",
            comment=e.get("comment", "") or "",
        ))
    return CharacterBook(
        name=d.get("name", "") or "",
        description=d.get("description", "") or "",
        scan_depth=d.get("scan_depth", 4) or 4,
        token_budget=d.get("token_budget", 512) or 512,
        recursive_scanning=d.get("recursive_scanning", False) or False,
        entries=entries,
        extensions=d.get("extensions", {}) or {},
    )


def card_from_spec(spec: Dict[str, Any], image_b64: str | None = None) -> CharacterCard:
    """Build a CharacterCard from a V2/V3 spec dict (or a bare data dict)."""
    data = spec.get("data", spec) if isinstance(spec, dict) else {}
    return CharacterCard(
        image=image_b64,
        name=data.get("name", "Companion") or "Companion",
        description=data.get("description", "") or "",
        personality=data.get("personality", "") or "",
        scenario=data.get("scenario", "") or "",
        first_mes=data.get("first_mes", "") or "",
        mes_example=data.get("mes_example", "") or "",
        creator_notes=data.get("creator_notes", "") or "",
        system_prompt=data.get("system_prompt", "") or "",
        post_history_instructions=data.get("post_history_instructions", "") or "",
        alternate_greetings=data.get("alternate_greetings", []) or [],
        tags=data.get("tags", []) or [],
        creator=data.get("creator", "") or "",
        character_version=data.get("character_version", "") or "",
        character_book=_book_from_spec(data.get("character_book")),
        extensions=data.get("extensions", {}) or {},
        nickname=data.get("nickname", "") or "",
        group_only_greetings=data.get("group_only_greetings", []) or [],
    )


def card_to_spec(card: CharacterCard, version: int = 2) -> Dict[str, Any]:
    """Serialize a CharacterCard to a V2 (default) or V3 spec dict."""
    data: Dict[str, Any] = {
        "name": card.name,
        "description": card.description,
        "personality": card.personality,
        "scenario": card.scenario,
        "first_mes": card.first_mes,
        "mes_example": card.mes_example,
        "creator_notes": card.creator_notes,
        "system_prompt": card.system_prompt,
        "post_history_instructions": card.post_history_instructions,
        "alternate_greetings": card.alternate_greetings,
        "tags": card.tags,
        "creator": card.creator,
        "character_version": card.character_version,
        "extensions": card.extensions,
    }
    if card.character_book:
        data["character_book"] = card.character_book.model_dump()
    if version >= 3:
        data["nickname"] = card.nickname
        data["group_only_greetings"] = card.group_only_greetings
        return {"spec": "chara_card_v3", "spec_version": "3.0", "data": data}
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


# ---------------------------------------------------------------------------
# PNG / JSON  <->  CharacterCard
# ---------------------------------------------------------------------------
def import_card_bytes(raw: bytes, filename: str = "") -> CharacterCard:
    """Import a card from raw file bytes (.png or .json)."""
    if filename.lower().endswith(".json") or raw.lstrip()[:1] in (b"{", b"["):
        return card_from_spec(json.loads(raw.decode("utf-8")))

    img = Image.open(io.BytesIO(raw))
    text = getattr(img, "text", {}) or {}
    payload = text.get("ccv3") or text.get("chara")
    if not payload:
        raise ValueError("No embedded character data ('chara'/'ccv3') found in PNG.")
    spec = json.loads(base64.b64decode(payload).decode("utf-8"))
    image_b64 = base64.b64encode(raw).decode("ascii")
    return card_from_spec(spec, image_b64=image_b64)


def export_card_png(card: CharacterCard, version: int = 2) -> bytes:
    """Render the card as a PNG with embedded character data. Returns PNG bytes."""
    # Base image: the stored portrait, else a blank canvas.
    if card.image:
        img = Image.open(io.BytesIO(base64.b64decode(card.image))).convert("RGBA")
    else:
        img = Image.new("RGBA", (400, 600), (32, 32, 40, 255))

    spec = card_to_spec(card, version=version)
    payload = base64.b64encode(json.dumps(spec).encode("utf-8")).decode("ascii")

    meta = PngImagePlugin.PngInfo()
    meta.add_text("chara", payload)              # V2 key — widely supported
    if version >= 3:
        meta.add_text("ccv3", payload)

    out = io.BytesIO()
    img.save(out, format="PNG", pnginfo=meta)
    return out.getvalue()
