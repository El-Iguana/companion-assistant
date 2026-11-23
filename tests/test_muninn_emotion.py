"""normalize: a classifier's answer to a label, with synonyms mapped, not dropped."""
from services.muninn.emotion import ALIASES, FULL_EMOTIONS, CORE_EMOTIONS, normalize


def test_labels_pass_through():
    for e in FULL_EMOTIONS:
        assert normalize(e, FULL_EMOTIONS) == e
        assert normalize(e.capitalize() + ".", FULL_EMOTIONS) == e


def test_off_list_answers_map_to_the_nearest_label():
    assert normalize("contempt", FULL_EMOTIONS) == "disgust"
    assert normalize("Hostile.", FULL_EMOTIONS) == "anger"
    assert normalize("affection", FULL_EMOTIONS) == "caring"
    assert normalize("suspicion", FULL_EMOTIONS) == "disapproval"


def test_alias_only_to_an_allowed_label():
    # "contempt" -> disgust, which the core set doesn't have: fall back, never invent.
    assert normalize("contempt", CORE_EMOTIONS) == "neutral"
    assert normalize("happiness", CORE_EMOTIONS) == "neutral"   # joy isn't core either
    assert normalize("angry", CORE_EMOTIONS) == "angry"          # a core label as-is


def test_substring_and_default():
    assert normalize("I'd say very caring overall", FULL_EMOTIONS) == "caring"
    assert normalize("", FULL_EMOTIONS) == "neutral"
    assert normalize("glee", FULL_EMOTIONS) == "neutral"         # deliberately unmapped
    assert normalize("xyz", ["happy", "sad"]) == "happy"         # no neutral: first label


def test_every_alias_targets_a_label():
    assert set(ALIASES.values()) <= set(FULL_EMOTIONS)
