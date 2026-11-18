"""Cards saved with a removed Orpheus voice keep speaking, in a mapped Kokoro voice."""
from __future__ import annotations

import pytest

from models.schema import LEGACY_ORPHEUS_VOICES, CharacterCard, current_voice_id
from services import tts


@pytest.mark.parametrize("name, kokoro", sorted(LEGACY_ORPHEUS_VOICES.items()))
def test_saved_card_loads_with_its_mapped_voice(name, kokoro):
    assert CharacterCard(name="Mira", tts_voice=f"orpheus:{name}").tts_voice == kokoro


def test_each_old_voice_stays_distinct():
    assert len(set(LEGACY_ORPHEUS_VOICES.values())) == len(LEGACY_ORPHEUS_VOICES)


def test_unknown_old_voice_gets_the_default_and_others_are_untouched():
    assert current_voice_id("orpheus:nobody") == "af_heart"
    assert current_voice_id("af_sarah") == "af_sarah"
    assert current_voice_id("piper:jarvis-medium") == "piper:jarvis-medium"
    assert current_voice_id("") == ""


def test_speech_maps_the_voice_and_never_reads_a_sound_tag(monkeypatch):
    calls = []

    class Engine:
        def create(self, text, voice, speed, lang):
            calls.append((text, voice))
            return [0.0] * 240, 24000

    monkeypatch.setattr(tts, "_kokoro", lambda: Engine())
    assert tts.synth_wav("<laugh> That is exactly it.", "orpheus:zac")[:4] == b"RIFF"
    assert calls == [("That is exactly it.", "am_puck")]
