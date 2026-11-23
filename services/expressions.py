"""The assistant's emotions: the labels, their valence, and classifying a reply.

A reply's emotion comes from the model's inline [expr:x] beats, else from classify()
(one short KoboldCPP call). It drives the 3D avatar's face, gestures and arm poses and
the running mood. Expression sprites (generated with ComfyUI) were removed on
2026-11-23: a card without a 3D avatar shows its portrait.
"""
from __future__ import annotations

import re

from services.kobold import KoboldClient

# The roleplay label sets (8 core, 28 full) came with Muninn. They stay importable for
# old data (a stored message's label, sprites generated before), but nothing new uses
# them: an assistant doesn't fall in love or lose its temper.
from services.muninn.emotion import CORE_EMOTIONS, EMOTION_VALENCE, FULL_EMOTIONS  # noqa: E402,F401
from services.muninn.emotion import valence_of as _roleplay_valence  # noqa: E402

# What an assistant feels, and how each moves the running mood (-1..1). The voice
# prompt's [expr:x] beats, the classifier fallback, the VRM face, gestures and arm poses
# all use these (vtuber.mjs maps each one). No love, desire, anger, disgust, fear or grief.
ASSISTANT_EMOTIONS = [
    "neutral", "happy", "amused", "curious", "thoughtful", "surprised", "confused",
    "concerned", "apologetic", "grateful", "proud", "relieved", "sympathetic",
]
ASSISTANT_VALENCE = {
    "neutral": 0.0, "happy": 0.7, "amused": 0.6, "curious": 0.25, "thoughtful": 0.05,
    "surprised": 0.1, "confused": -0.1, "concerned": -0.2, "apologetic": -0.1,
    "grateful": 0.6, "proud": 0.5, "relieved": 0.4, "sympathetic": -0.15,
}

# Default label vocabulary used when a caller doesn't specify one.
EMOTIONS = ASSISTANT_EMOTIONS


def card_emotions(card) -> list[str]:
    """The labels a card's replies are tagged and classified with: the assistant set for
    a card showing a 3D avatar (its face, gestures and poses follow them), none for a
    card on a static portrait, where an emotion would change nothing on screen and the
    classifier call would be wasted. Sprite cards are portraits now (2026-11-23)."""
    return list(ASSISTANT_EMOTIONS) if (card.vrm and card.avatar_mode == "vrm") else []


def valence_of(emotion: str) -> float:
    """An assistant label's valence; an old roleplay label (stored data) keeps its own."""
    return ASSISTANT_VALENCE.get(emotion, _roleplay_valence(emotion))


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


# ---------------------------------------------------------------------------
# Emotion classification (KoboldCPP)
# ---------------------------------------------------------------------------
# Fortuna's confidence below which a reply's emotion reads as neutral. On four test replies it
# was 0.84 for a clear "thoughtful", 0.38-0.40 for clear sympathy and amusement, and 0.24
# for a flat "Playing … on Predator."
FORTUNA_EMOTION_MIN = 0.3


async def classify(kobold: KoboldClient, reply: str, labels: list[str] | None = None) -> str:
    """Return one label describing the emotion of `reply`.

    `labels` is the vocabulary to choose from (card_emotions); the assistant set by default.
    """
    labels = labels or ASSISTANT_EMOTIONS
    default = "neutral" if "neutral" in labels else labels[0]
    if not reply.strip():
        return default
    # Fortuna first: 0.23 s against ~7 s here. Under FORTUNA_EMOTION_MIN it isn't sure, and
    # an unsure face is a neutral one. Fortuna off or down: KoboldCPP, as before.
    from services import fortuna
    got = await fortuna.choose(reply[:1500], "Which emotion does the assistant's reply express?",
                               {e: e for e in labels})
    if got is not None:
        label, conf = got
        return label if conf >= FORTUNA_EMOTION_MIN else default
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
