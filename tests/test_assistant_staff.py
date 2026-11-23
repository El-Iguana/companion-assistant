"""The Staff studio's assistant fields: voice & tone, no roleplay fields in the prompt,
and the assistant emotion set.

    .venv/bin/python -m pytest -q tests/test_assistant_staff.py
"""
from __future__ import annotations

from models.schema import CharacterCard, ChatMessage, ChatSession, Persona, Tone
from services import expressions, prompt_builder


def _prompt(card: CharacterCard, **kw) -> str:
    s = ChatSession(character_id=card.id)
    s.messages = [ChatMessage(role="user", content="hello")]
    return "\n".join(m["content"] for m in prompt_builder.build_messages(card, Persona(name="Eli"), s, **kw))


# --- voice & tone ---------------------------------------------------------------

def test_tone_clamps_and_never_breaks_a_card():
    t = Tone(warmth=3, formality="junk", detail=-2)
    assert (t.warmth, t.formality, t.detail) == (1.0, 0.0, -1.0)
    old = CharacterCard(name="Iris").model_dump()
    old.pop("tone")
    assert CharacterCard(**old).tone == Tone()          # a card from before the sliders


def test_a_centred_tone_says_nothing():
    assert prompt_builder.tone_note(Tone(), "Iris") == ""
    assert prompt_builder.tone_note(Tone(warmth=0.1), "Iris") == ""


def test_tone_words_follow_direction_and_strength():
    note = prompt_builder.tone_note(Tone(warmth=0.8, formality=-0.4, detail=0.2, femininity=0.5), "Iris")
    assert note.startswith("Voice and tone: Iris is ")
    assert "very warm and personable" in note
    assert "casual and relaxed in wording" in note and "very casual" not in note
    assert "slightly thorough" in note
    assert "feminine in speech style" in note
    assert "masculine in speech style" in prompt_builder.tone_note(Tone(femininity=-0.6), "Iris")


def test_the_tone_line_reaches_the_prompt():
    card = CharacterCard(name="Iris", personality="observant", tone=Tone(playfulness=0.7))
    assert "very playful, with light humour" in _prompt(card)


# --- roleplay fields are kept on the card but not prompted ----------------------

def test_roleplay_fields_stay_out_of_the_prompt():
    card = CharacterCard(name="Iris", description="A research assistant.",
                         scenario="SCENE-XYZ in a tavern", mes_example="<START> EXAMPLE-XYZ",
                         post_history_instructions="UJB-XYZ")
    p = _prompt(card)
    assert "A research assistant." in p
    for marker in ("SCENE-XYZ", "EXAMPLE-XYZ", "UJB-XYZ"):
        assert marker not in p
    # Still on the card, so an export round-trips.
    assert card.scenario and card.mes_example and card.post_history_instructions


# --- the assistant emotion set ----------------------------------------------------

def test_no_roleplay_emotions_in_the_assistant_set():
    for gone in ("love", "desire", "anger", "angry", "annoyance", "disgust", "fear", "grief",
                 "nervousness"):
        assert gone not in expressions.ASSISTANT_EMOTIONS
    assert set(expressions.ASSISTANT_VALENCE) == set(expressions.ASSISTANT_EMOTIONS)


def test_only_a_card_with_a_3d_face_gets_emotions():
    vrm = CharacterCard(name="Iris", vrm="baseline.vrm", avatar_mode="vrm")
    portrait = CharacterCard(name="Iris", expressions={"happy": "x"}, avatar_mode="sprite")
    assert expressions.card_emotions(vrm) == expressions.ASSISTANT_EMOTIONS
    assert expressions.card_emotions(portrait) == []      # old sprites no longer drive a face


def test_the_beat_instruction_lists_the_assistant_set():
    card = CharacterCard(name="Iris", vrm="baseline.vrm", avatar_mode="vrm")
    p = _prompt(card, expr_labels=expressions.card_emotions(card))
    assert "apologetic" in p and "sympathetic" in p and "love" not in p.split("Valid labels")[1][:200]


def test_old_labels_keep_their_valence():
    assert expressions.valence_of("grateful") == expressions.ASSISTANT_VALENCE["grateful"]
    assert expressions.valence_of("love") > 0.5             # a mood stored in an old chat
