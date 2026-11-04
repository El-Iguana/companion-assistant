"""Expression sprites.

Two capabilities:
  1. classify(reply) -> emotion label, using KoboldCPP (no extra model/deps).
  2. generate_expressions(card) -> {emotion: base64 PNG}, using ComfyUI.

Sprites for a given card all share one deterministic seed (derived from the
card id) so composition/character stays consistent across emotions; only the
expression descriptor changes.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re

from models.schema import CharacterCard
from services.kobold import KoboldClient
from services.comfyui import ComfyUIClient, get_checkpoint_defaults

# The label sets and their valence live in Muninn (shared with Rhapsode).
from muninn.emotion import CORE_EMOTIONS, EMOTION_VALENCE, FULL_EMOTIONS, valence_of  # noqa: E402,F401

# Default label vocabulary used when a caller doesn't specify one.
EMOTIONS = CORE_EMOTIONS


def strip_bare_labels(text: str, labels) -> str:
    """Drop an expression label the model emitted WITHOUT its [expr:...] wrapper.

    Observed three times against the 9B: the model copies the label out of the
    example and leaves the brackets behind, so `_strip_expr` never sees a tag and
    the bare word lands in the transcript — displayed, stored, and spoken aloud.

    ONLY a label alone on its own line is removed. Every other shape is left
    alone on purpose: the vocabulary is ordinary English ("happy", "love",
    "pride", "relief", "fear"), so a rule that also caught them mid-prose would
    quietly eat real sentences — "Pride comes before a fall" is not a stray tag.
    A label stranded mid-paragraph is handled in the prompt instead, by stating
    that the brackets are part of the tag.
    """
    if not labels or not text:
        return text
    alt = "|".join(sorted((re.escape(l) for l in labels), key=len, reverse=True))
    # The `$` anchor is load-bearing: without it "Love is a strong word" matches at
    # the line start and loses its first word. The label must be the WHOLE line.
    text = re.sub(rf"(?im)^[ \t]*(?:{alt})[ \t]*$\r?\n?", "", text)
    # Removing the line leaves the blank lines that surrounded it back to back.
    return re.sub(r"\n{3,}", "\n\n", text).strip()

# Visual descriptor added to the appearance prompt per emotion (covers both sets;
# any label without an entry falls back to "<label> facial expression").
EMOTION_PROMPTS = {
    # core
    "neutral":     "neutral calm expression",
    "happy":       "smiling, happy, cheerful expression",
    "sad":         "sad, downcast eyes, sorrowful expression",
    "angry":       "angry, frowning, furious expression",
    "surprised":   "surprised, wide eyes, shocked expression, open mouth",
    "embarrassed": "embarrassed, blushing, shy bashful expression",
    "thoughtful":  "thoughtful, pensive, contemplative expression, looking aside",
    "love":        "affectionate, loving, tender expression, soft warm smile",
    # full (28)
    "admiration":     "admiring, impressed, gazing with admiration",
    "amusement":      "amused, playful grin, holding back a laugh",
    "anger":          "angry, furious, sharp frown",
    "annoyance":      "annoyed, irritated, mild frown, rolling eyes",
    "approval":       "approving, satisfied, gentle nod and smile",
    "caring":         "caring, warm, gentle compassionate expression",
    "confusion":      "confused, puzzled, tilted head, furrowed brow",
    "curiosity":      "curious, intrigued, raised eyebrow, interested",
    "desire":         "longing, yearning, soft sultry gaze",
    "disappointment": "disappointed, let down, downcast look",
    "disapproval":    "disapproving, stern, frowning",
    "disgust":        "disgusted, grimacing, wrinkled nose",
    "embarrassment":  "embarrassed, blushing, shy bashful expression",
    "excitement":     "excited, thrilled, wide eyes, big grin",
    "fear":           "fearful, scared, wide frightened eyes",
    "gratitude":      "grateful, thankful, warm soft smile",
    "grief":          "grief-stricken, deep sorrow, tears, anguished",
    "joy":            "joyful, bright cheerful smile, radiant",
    "nervousness":    "nervous, anxious, worried, uneasy",
    "optimism":       "optimistic, hopeful, bright confident smile",
    "pride":          "proud, confident, chin up, subtle smug smile",
    "realization":    "sudden realization, dawning understanding, wide-eyed",
    "relief":         "relieved, relaxed, soft exhale, gentle smile",
    "remorse":        "remorseful, regretful, guilty, apologetic",
    "sadness":        "sad, sorrowful, downcast eyes",
    "surprise":       "surprised, shocked, wide eyes, open mouth",
}

_NEG = ("lowres, worst quality, low quality, bad anatomy, bad hands, extra limbs, "
        "extra fingers, watermark, signature, text, deformed, jpeg artifacts")

def _seed_for(card: CharacterCard, emotion: str = "") -> int:
    # Per-emotion seed so each expression is a genuinely different image (not the
    # same face regenerated). The appearance prompt keeps the character recognizable.
    return int(hashlib.sha256((card.id + "|" + emotion).encode()).hexdigest()[:8], 16)


# ---------------------------------------------------------------------------
# Emotion classification (KoboldCPP)
# ---------------------------------------------------------------------------
async def classify(kobold: KoboldClient, reply: str, labels: list[str] | None = None) -> str:
    """Return one label describing the emotion of `reply`.

    `labels` is the vocabulary to choose from — pass the emotions a card actually
    has sprites for (so an 8-sprite card classifies into 8, a 28-sprite card into 28).
    """
    labels = labels or CORE_EMOTIONS
    default = "neutral" if "neutral" in labels else labels[0]
    if not reply.strip():
        return default
    options = ", ".join(labels)
    prompt = (
        "You are an emotion classifier. Read the character's reply and choose the single "
        f"emotion that best fits it from this list: {options}.\n"
        "Respond with ONLY that one word, nothing else.\n\n"
        f"Reply:\n{reply[:1500]}\n\nEmotion:"
    )
    try:
        out = await kobold.generate_answer(prompt, {
            "max_length": 16, "temperature": 0.0, "top_p": 1.0, "rep_pen": 1.0})
    except Exception:
        return default

    word = re.sub(r"[^a-z]", "", out.strip().lower().split()[0]) if out.strip() else ""
    if word in labels:
        return word
    for e in labels:                        # substring fallback ("very happy" -> happy)
        if e in out.lower():
            return e
    return default


# ---------------------------------------------------------------------------
# Sprite generation (ComfyUI)
# ---------------------------------------------------------------------------
def _appearance_prompt(card: CharacterCard) -> str:
    if card.appearance.strip():
        return card.appearance.strip()
    # Fall back to the card's own text description.
    base = card.description.strip() or card.personality.strip()
    return f"{card.name}, {base}" if base else card.name


def _uploaded_base(b64_img: str, w: int, h: int) -> str:
    """Turn the card's uploaded portrait into an img2img base: RGB, cropped to the
    sprite's portrait aspect (cover + center-crop), re-encoded PNG base64."""
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(base64.b64decode(b64_img))).convert("RGB")
    im = ImageOps.fit(im, (w, h), method=Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Background removal (rembg) — produces transparent sprites.
# Sessions are cached module-wide so the ONNX model loads only once.
# ---------------------------------------------------------------------------
_rembg_sessions: dict = {}


def _fit_to(b64_img: str, b64_like: str) -> str:
    """Resize an image to another's dimensions (Kontext returns its own trained size)."""
    from PIL import Image

    like = Image.open(io.BytesIO(base64.b64decode(b64_like)))
    im = Image.open(io.BytesIO(base64.b64decode(b64_img)))
    if im.size == like.size:
        return b64_img
    buf = io.BytesIO()
    im.resize(like.size, Image.LANCZOS).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _rembg_session(model: str):
    from rembg import new_session
    if model not in _rembg_sessions:
        _rembg_sessions[model] = new_session(model)
    return _rembg_sessions[model]


def remove_background(b64_png: str, style: str = "anime") -> str:
    """Return an RGBA PNG (base64) with the background removed.

    Uses isnet-anime for anime style (best for illustrated characters), u2net
    otherwise. Falls back to the original image on any failure so generation
    never breaks because of the cutout step.
    """
    try:
        from rembg import remove
        model = "isnet-anime" if style == "anime" else "u2net"
        raw = base64.b64decode(b64_png)
        cut = remove(raw, session=_rembg_session(model))  # bytes in -> RGBA PNG bytes out
        return base64.b64encode(cut).decode()
    except Exception:
        return b64_png


async def generate_expressions(
    comfy: ComfyUIClient,
    card: CharacterCard,
    checkpoint: str,
    style: str = "anime",
    emotions: list[str] | None = None,
    transparent: bool = True,
    from_upload: bool = False,
    denoise: float = 0.55,
    use_kontext: bool = False,
) -> dict[str, str]:
    """Generate a sprite per emotion. Returns {emotion: base64 PNG}.

    The BASE portrait every emotion is derived from (via img2img partial-denoise, so
    all sprites are the SAME person) is either:
      - `from_upload`: the card's uploaded portrait (`card.image`) — keeps the user's
        own character. Note: img2img still restyles it toward the checkpoint, so
        likeness drifts somewhat at the tuned denoise.
      - otherwise: a fresh txt2img portrait from the appearance prompt.

    Each emotion is then either an img2img pass over the base (`denoise`), or — with
    `use_kontext` — a Flux Kontext instruction edit of it ("change the expression to…"),
    which keeps face, hair, clothes and background and changes only the expression.
    img2img at a strength that keeps the likeness barely moves the face on FLUX (measured:
    0.55 left five emotions near-identical; at 0.7 angry and surprised both came out as
    grins); Kontext made angry and sad unmistakable with nothing else changed. ~52 s per
    sprite against ~15 s, so it is a choice, not a default everywhere.
    """
    emotions = emotions or EMOTIONS
    appearance = _appearance_prompt(card)
    seed = _seed_for(card)
    d = get_checkpoint_defaults(checkpoint)
    # Portrait aspect regardless of the model's square default.
    w, h = (768, 1024) if d["is_sdxl"] or d["is_flux"] else (512, 768)

    # The base is reproducible (fixed seed / fixed upload), so regenerating a single
    # emotion later still matches the rest.
    if from_upload and card.image:
        base = _uploaded_base(card.image, w, h)
    else:
        base_positive = (f"{appearance}, {EMOTION_PROMPTS['neutral']}, solo portrait, upper body, "
                         "looking at viewer, plain flat pastel background")
        base = await comfy.generate(
            positive=base_positive, negative=_NEG, checkpoint=checkpoint, style=style,
            width=w, height=h, steps=d["steps"], cfg=d["cfg"],
            sampler=d["sampler"], scheduler=d["scheduler"], seed=seed,
        )

    async def finish(b64: str) -> str:
        # rembg is CPU-bound and sync — offload so we don't block the loop.
        return await asyncio.to_thread(remove_background, b64, style) if transparent else b64

    out: dict[str, str] = {}
    for emo in emotions:
        if emo == "neutral":
            out[emo] = await finish(base)
            continue
        descriptor = EMOTION_PROMPTS.get(emo, f"{emo} facial expression")
        if d["is_flux"]:
            # FLUX's T5 encoder reads plain language and has no (text:1.3) weighting —
            # the numbers would be read as words. Lead with the expression instead.
            positive = (f"{descriptor}, clearly showing on the face. {appearance}, same person, "
                        "solo portrait, upper body, looking at viewer, plain flat pastel background")
        else:
            positive = (f"{appearance}, ({descriptor}:1.3), same person, expressive face, "
                        "solo portrait, upper body, looking at viewer, plain flat pastel background")
        if use_kontext:
            img = await comfy.generate_kontext(
                base, f"Change the facial expression to {descriptor}. Keep the person, hair, "
                "clothing, pose, framing and background exactly the same.", seed=seed)
            img = _fit_to(img, base)            # Kontext snaps its own size; match the set
        else:
            img = await comfy.generate_img2img(
                base, positive=positive, negative=_NEG, checkpoint=checkpoint, style=style,
                steps=d["steps"], cfg=d["cfg"], sampler=d["sampler"], scheduler=d["scheduler"],
                denoise=denoise, seed=seed,
            )
        out[emo] = await finish(img)
    return out
