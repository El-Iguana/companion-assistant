"""strip_reasoning, on the shapes models actually produce."""
from services.muninn.text import strip_reasoning

BULLETS = "- Jonah works twelve-hour shifts on the night ward\n- He has a cat named Pepper"


def test_reasoning_then_think_close():
    raw = ("Let me list what Jonah said:\n1. Night ward\n2. Cat named Pepper\n\n"
           "I won't infer anything beyond what's said.\n\n</think>\n\n" + BULLETS)
    assert strip_reasoning(raw) == BULLETS


def test_reasoning_then_thought_close_then_meta_line():
    raw = ("Jonah is exhausted from a night shift. Mira offers tea.\n\n</thought>\n\n"
           "I need to create a concise memory bullet list based on Jonah's statements and actions:\n\n"
           + BULLETS)
    assert strip_reasoning(raw) == BULLETS


def test_answer_then_stray_end_of_turn_tag():
    raw = BULLETS + "\n\n</assistant>"
    assert strip_reasoning(raw) == BULLETS


def test_next_turn_after_end_of_turn_is_dropped():
    raw = BULLETS + "<|im_end|>\n<|im_start|>user\nThanks!"
    assert strip_reasoning(raw) == BULLETS


def test_prefilled_empty_think_and_matched_pair():
    assert strip_reasoning("<think>\n\n</think>\n\n" + BULLETS) == BULLETS
    assert strip_reasoning("<think>hmm, the user wants</think>joy") == "joy"


def test_unclosed_reasoning_leaves_nothing():
    # Better an empty answer (the fold treats it as a failed pass and retries
    # later) than storing half a reasoning block as memory.
    assert strip_reasoning("<think>First I should consider whether") == ""


def test_leading_wrapper():
    assert strip_reasoning("<response>NONE</response>") == "NONE"


def test_clean_answers_are_untouched():
    for clean in (BULLETS, "joy", "3", "NONE",
                  "Where things stand: Jonah is asleep on the sofa; Mira is reading.",
                  "I'll be honest — Jonah said he hates coriander."):  # no tag: not a meta line
        assert strip_reasoning(clean) == clean


def test_meta_line_kept_when_it_is_the_whole_answer():
    assert strip_reasoning("</think>\nI need to rest.") == "I need to rest."


def test_empty_and_none():
    assert strip_reasoning("") == ""
    assert strip_reasoning(None) == ""
