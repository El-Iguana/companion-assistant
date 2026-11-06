"""Sprites from the portrait: the Studio's explicit source + method choices.

main.generate_expressions takes `source` ("portrait" | "prompt") and `method`
("kontext" | "img2img"). A choice it can't honour is an error, never a quiet fallback:
a portrait request on a card with no saved portrait used to come back as a new
character drawn from the appearance prompt. Portrait + Kontext needs no model.

    .venv/bin/python -m pytest -q tests/test_sprites_from_portrait.py

No ComfyUI: the generator and the Kontext probe are stubbed.
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PANTHEON_DATA_DIR", tempfile.mkdtemp(prefix="pantheon-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import main  # noqa: E402
from models.schema import CharacterCard, User  # noqa: E402
from services import auth  # noqa: E402


def _png() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 60), "teal").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture()
def cur():
    salt = auth.new_salt()
    u = User(username=f"t{os.urandom(4).hex()}", role="user", salt=salt,
             password_hash=auth.hash_password("x", salt))
    main._save_user(u)
    return main._ctx_for(u)


@pytest.fixture()
def gen(monkeypatch):
    """Records each call to the generator; Kontext installed unless a test says not."""
    state = {"calls": [], "kontext": True}

    async def fake(comfy, card, checkpoint, style="anime", emotions=None, transparent=True,
                   from_upload=False, denoise=0.55, use_kontext=False):
        state["calls"].append({"checkpoint": checkpoint, "from_upload": from_upload,
                               "use_kontext": use_kontext, "emotions": list(emotions or [])})
        return {e: "x" for e in emotions or []}

    async def has_kontext():
        return state["kontext"]

    monkeypatch.setattr(main.expressions, "generate_expressions", fake)
    monkeypatch.setattr(main.comfy, "has_kontext", has_kontext)
    return state


def _card(cur, portrait=True) -> CharacterCard:
    c = CharacterCard(name="Mira", image=_png() if portrait else None)
    main._save(c, cur.cards)
    return c


def _gen(cur, card, **body):
    return asyncio.run(main.generate_expressions(card.id, body, cur=cur))


def test_portrait_and_kontext_need_no_model(cur, gen):
    card = _card(cur)
    _gen(cur, card, source="portrait", method="kontext", checkpoint="")
    call = gen["calls"][-1]
    assert call["from_upload"] and call["use_kontext"] and call["checkpoint"] == ""
    saved = main._load(cur.cards, card.id, CharacterCard)
    assert saved.expr_from_upload and saved.expr_kontext and len(saved.expressions) == 8


def test_portrait_with_img2img_uses_the_chosen_model(cur, gen):
    card = _card(cur)
    _gen(cur, card, source="portrait", method="img2img", checkpoint="flux1-schnell-Q4_K_S.gguf")
    call = gen["calls"][-1]
    assert call["from_upload"] and not call["use_kontext"]
    assert call["checkpoint"] == "flux1-schnell-Q4_K_S.gguf"
    assert main._load(cur.cards, card.id, CharacterCard).expr_checkpoint == "flux1-schnell-Q4_K_S.gguf"


def test_no_saved_portrait_is_an_error_not_a_new_character(cur, gen):
    card = _card(cur, portrait=False)
    with pytest.raises(HTTPException) as e:
        _gen(cur, card, source="portrait", method="kontext")
    assert e.value.status_code == 400 and "portrait" in e.value.detail
    assert gen["calls"] == []


def test_kontext_asked_for_but_missing_is_an_error(cur, gen):
    gen["kontext"] = False
    card = _card(cur)
    with pytest.raises(HTTPException) as e:
        _gen(cur, card, source="portrait", method="kontext")
    assert "Kontext" in e.value.detail and gen["calls"] == []


def test_the_prompt_source_still_needs_a_model(cur, gen):
    card = _card(cur)
    with pytest.raises(HTTPException):
        _gen(cur, card, source="prompt", method="kontext", checkpoint="")
    _gen(cur, card, source="prompt", method="kontext", checkpoint="sd_xl_base_1.0.safetensors")
    assert not gen["calls"][-1]["from_upload"] and gen["calls"][-1]["use_kontext"]


def test_old_flags_still_work(cur, gen):
    gen["kontext"] = False                       # the old checkbox meant "when installed"
    card = _card(cur)
    _gen(cur, card, from_upload=True, kontext=True, checkpoint="v1-5-pruned-emaonly-fp16.safetensors")
    call = gen["calls"][-1]
    assert call["from_upload"] and not call["use_kontext"]


def test_single_sprite_regen_matches_a_portrait_kontext_set_without_a_model(cur, gen):
    card = _card(cur)
    _gen(cur, card, source="portrait", method="kontext")
    asyncio.run(main.regenerate_expression(card.id, "anger", {}, cur=cur))
    call = gen["calls"][-1]
    assert call["emotions"] == ["anger"] and call["from_upload"] and call["use_kontext"]


def test_studio_lists_no_audio_models():
    from appcode.bff import studio

    names = ["flux1-schnell-Q4_K_S.gguf", "ace_step_v1_3.5b.safetensors",
             "stable_audio_3_small_sfx.safetensors", "sd_xl_base_1.0.safetensors"]
    assert [n for n in names if not studio.AUDIO_MODEL.search(n)] == \
        ["flux1-schnell-Q4_K_S.gguf", "sd_xl_base_1.0.safetensors"]


def test_expressions_refuse_a_prompt_base_without_a_model():
    from services import expressions

    with pytest.raises(ValueError):
        asyncio.run(expressions.generate_expressions(None, CharacterCard(name="x"), checkpoint="",
                                                     emotions=["joy"]))


def test_a_cutout_that_removes_the_character_keeps_the_original(monkeypatch):
    from PIL import Image

    from services import expressions

    def cut_to(share):
        def fake_remove(raw, session=None):
            im = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
            for i in range(int(share * 10000)):
                im.putpixel((i % 100, i // 100), (200, 100, 50, 255))
            buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()
        return fake_remove

    import rembg
    original = _png()
    monkeypatch.setattr(expressions, "_rembg_session", lambda m: None)
    monkeypatch.setattr(rembg, "remove", cut_to(0.01))
    assert expressions.remove_background(original) == original          # 1% left: keep the original
    monkeypatch.setattr(rembg, "remove", cut_to(0.40))
    assert expressions.remove_background(original) != original          # a real cutout is kept
