"""Emotion labels and their valence.

The label sets are the 8 core and 28 SillyTavern / GoEmotions labels a reply is
classified into; the valence of that label is what moves mood and relationships.
Classifying (a model call) stays in each app; this is only the shared vocabulary.
Moved from Pantheon's services/expressions.py.
"""
from __future__ import annotations

import re

# Core emotion set (kept small to bound generation time). "neutral" is the default.
# Core set: small, fast, reliable (default).
CORE_EMOTIONS = [
    "neutral", "happy", "sad", "angry",
    "surprised", "embarrassed", "thoughtful", "love",
]

# Full set: the 28 SillyTavern / GoEmotions labels — compatible with existing
# SillyTavern sprite packs (same file names).
FULL_EMOTIONS = [
    "admiration", "amusement", "anger", "annoyance", "approval", "caring",
    "confusion", "curiosity", "desire", "disappointment", "disapproval", "disgust",
    "embarrassment", "excitement", "fear", "gratitude", "grief", "joy", "love",
    "nervousness", "neutral", "optimism", "pride", "realization", "relief",
    "remorse", "sadness", "surprise",
]

# Emotional valence per label, [-1, 1]. Drives the running mood/relationship state.
EMOTION_VALENCE = {
    # core
    "neutral": 0.0, "happy": 0.8, "sad": -0.7, "angry": -0.85,
    "surprised": 0.1, "embarrassed": -0.05, "thoughtful": 0.05, "love": 0.95,
    # full (28)
    "admiration": 0.7, "amusement": 0.6, "anger": -0.85, "annoyance": -0.4,
    "approval": 0.5, "caring": 0.65, "confusion": -0.15, "curiosity": 0.25,
    "desire": 0.6, "disappointment": -0.5, "disapproval": -0.5, "disgust": -0.7,
    "embarrassment": -0.05, "excitement": 0.75, "fear": -0.7, "gratitude": 0.7,
    "grief": -0.9, "joy": 0.85, "nervousness": -0.3, "optimism": 0.6,
    "pride": 0.5, "realization": 0.1, "relief": 0.4, "remorse": -0.55,
    "sadness": -0.7, "surprise": 0.1,
}


def valence_of(emotion: str) -> float:
    return EMOTION_VALENCE.get(emotion, 0.0)


# Words a classifier answers with that are not labels, mapped to the nearest one.
# Without this an off-list answer became "neutral": asked how a sneering lord feels,
# a model answered "contempt", and the strongest beat in the scene moved nothing.
# Only words with a clear direction are mapped; "glee" or "smug" toward someone's
# humiliation is not joy, so those stay unmapped and fall back to the default.
ALIASES = {
    # hostile / cold
    "contempt": "disgust", "contemptuous": "disgust", "scorn": "disgust", "scornful": "disgust",
    "disdain": "disgust", "disdainful": "disgust", "loathing": "disgust", "mockery": "disgust",
    "derision": "disgust", "disgusted": "disgust",
    "hostility": "anger", "hostile": "anger", "hatred": "anger", "hate": "anger", "rage": "anger",
    "fury": "anger", "furious": "anger", "resentment": "anger", "angry": "anger",
    "irritation": "annoyance", "irritated": "annoyance", "frustration": "annoyance",
    "frustrated": "annoyance", "impatience": "annoyance", "annoyed": "annoyance",
    "suspicion": "disapproval", "suspicious": "disapproval", "distrust": "disapproval",
    "mistrust": "disapproval", "disapproving": "disapproval",
    "wariness": "nervousness", "wary": "nervousness", "anxiety": "nervousness",
    "anxious": "nervousness", "worry": "nervousness", "worried": "nervousness",
    "unease": "nervousness", "uneasy": "nervousness", "nervous": "nervousness",
    "dread": "fear", "terror": "fear", "afraid": "fear", "scared": "fear", "frightened": "fear",
    "disappointed": "disappointment", "shame": "embarrassment", "ashamed": "embarrassment",
    "embarrassed": "embarrassment", "guilt": "remorse", "guilty": "remorse", "regret": "remorse",
    "sorrow": "sadness", "sad": "sadness", "melancholy": "sadness", "grieving": "grief",
    # warm
    "affection": "caring", "affectionate": "caring", "warmth": "caring", "warm": "caring",
    "fondness": "caring", "fond": "caring", "tenderness": "caring", "tender": "caring",
    "compassion": "caring", "sympathy": "caring", "concern": "caring", "concerned": "caring",
    "protectiveness": "caring", "protective": "caring", "kindness": "caring", "kind": "caring",
    "respect": "admiration", "respectful": "admiration", "awe": "admiration",
    "trust": "approval", "trusting": "approval", "acceptance": "approval",
    "happiness": "joy", "happy": "joy", "delight": "joy", "delighted": "joy",
    "contentment": "joy", "content": "joy", "loving": "love", "adoration": "love",
    "lust": "desire", "longing": "desire", "attraction": "desire", "attracted": "desire",
    "hope": "optimism", "hopeful": "optimism", "grateful": "gratitude",
    "gratefulness": "gratitude", "thankful": "gratitude", "thankfulness": "gratitude",
    "amused": "amusement", "excited": "excitement", "relieved": "relief", "proud": "pride",
    "curious": "curiosity", "interest": "curiosity", "interested": "curiosity",
    "intrigue": "curiosity", "intrigued": "curiosity", "confused": "confusion",
    "surprised": "surprise", "shock": "surprise", "shocked": "surprise", "amazement": "surprise",
    "indifference": "neutral", "indifferent": "neutral", "calm": "neutral",
}


def normalize(answer: str, labels, default: str = "neutral") -> str:
    """The label a classifier's free-text answer means, or `default`.

    The answer's first word if it is a label; else that word's alias, if the alias
    is one of `labels`; else the first label that appears anywhere in the answer
    ("very happy" -> happy). An alias is only used when its target is allowed, so a
    caller with a small label set never gets a label it didn't offer."""
    out = (answer or "").strip().lower()
    labels = list(labels)
    if default not in labels:
        default = "neutral" if "neutral" in labels else (labels[0] if labels else default)
    if not out:
        return default
    word = re.sub(r"[^a-z]", "", out.split()[0])
    if word in labels:
        return word
    if ALIASES.get(word) in labels:
        return ALIASES[word]
    for e in labels:
        if e in out:
            return e
    return default
