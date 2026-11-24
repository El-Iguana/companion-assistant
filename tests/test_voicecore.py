"""The hands-free microphone's rules (appcode/ui/voicecore.py, #5), ported from voice.js.
Plain Python, so they are tested here and run unchanged in the page (Pyodide).

    .venv/bin/python -m pytest -q tests/test_voicecore.py
"""
from __future__ import annotations

import importlib.util
import io
import wave
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "voicecore", Path(__file__).resolve().parents[1] / "appcode" / "ui" / "voicecore.py")
vc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vc)

LOUD, QUIET = 0.05, 0.001


def test_the_module_imports_nothing_from_the_browser():
    src = (Path(vc.__file__)).read_text()
    assert "import js" not in src and "pyodide" not in src.split('"""', 2)[2]


# ---- phrases ----------------------------------------------------------------------

def test_phrases_compare_on_words_and_contractions():
    assert vc.norm_phrase("That'll be ALL!") == "that will be all"
    assert vc.starts_with("Mira, what time is it?", "mira")
    assert not vc.starts_with("Miranda called", "mira")
    assert vc.starts_with("that's all, thanks", "that is all")
    assert vc.strip_prefix("Mira, what time is it?", "Mira") == "what time is it?"
    assert vc.strip_prefix("... Hey Mira! Lights off.", "hey mira") == "Lights off."


# ---- wake word ----------------------------------------------------------------------

def _gate(**cfg):
    g = vc.WakeGate()
    g.configure({"wake_word": "Mira", "sleep_phrase": "that will be all", "farewell": "Bye.",
                 "wake_mode": "phrase", "sleep_after_s": 180, **cfg})
    return g


def test_no_wake_word_is_an_open_mic():
    assert vc.WakeGate().apply("  hello there ", 0) == ("hello there", [])


def test_phrase_mode_needs_the_word_every_time():
    g = _gate()
    assert g.apply("Mira, lights on", 0) == ("lights on", [])
    assert g.apply("lights off", 1000) == ("", [])
    assert g.apply("Mira", 2000) == ("", [{"do": "summoned"}])   # her name alone is a summons


def test_continuous_mode_opens_and_closes_a_conversation():
    g = _gate(wake_mode="continuous")
    msg, acts = g.apply("Mira, what's on today?", 0)
    assert msg == "what's on today?" and g.awake
    assert {"do": "toast", "kind": "info", "msg": "Listening — say “that will be all” when done"} in acts
    assert g.apply("and tomorrow?", 1000) == ("and tomorrow?", [])      # no prefix needed
    msg, acts = g.apply("That'll be all.", 2000)
    assert msg == "" and not g.awake and {"do": "say", "text": "Bye."} in acts


def test_the_conversation_times_out_and_the_mic_off_says_nothing():
    g = _gate(wake_mode="continuous", sleep_after_s=10)
    g.apply("Mira hi", 0)
    assert g.tick(5000) == [] and g.awake
    assert {"do": "say", "text": "Bye."} in g.tick(10_001) and not g.awake
    g.apply("Mira hi", 20_000)
    acts = g.set_awake(False, "mic off", 21_000)
    assert not any(a["do"] == "say" for a in acts)
    never = _gate(wake_mode="continuous", sleep_after_s=0)
    never.apply("Mira hi", 0)
    assert never.tick(10 ** 12) == [] and never.awake


def test_speech_keeps_the_conversation_open():
    g = _gate(wake_mode="continuous", sleep_after_s=10)
    g.apply("Mira hi", 0)
    g.keep_open(9000)
    assert g.tick(15_000) == [] and g.awake


# ---- self-hearing -------------------------------------------------------------------

SPOKEN = [{"t": 10_000, "text": "the weather tomorrow looks sunny with light winds"}]


def test_self_hearing_needs_full_duplex_recent_speech_and_mostly_her_words():
    said = "the weather tomorrow looks sunny"
    assert vc.looks_self_heard(said, SPOKEN, 12_000, True)
    assert not vc.looks_self_heard(said, SPOKEN, 12_000, False)          # half duplex: never
    assert not vc.looks_self_heard(said, SPOKEN, 40_000, True)           # over 20 s ago
    assert not vc.looks_self_heard("what about the weather on Friday", SPOKEN, 12_000, True)
    assert not vc.looks_self_heard("sunny ok", SPOKEN, 12_000, True)     # too short to judge


# ---- segmentation ---------------------------------------------------------------------

def _feed(seg, levels, start=0, step=85, audible=False, her_turn=False, busy=False):
    acts = []
    for i, rms in enumerate(levels):
        acts += [dict(a, at=i) for a in seg.block(rms, f"b{start + i}", start + i * step,
                                                  audible, her_turn, busy)]
    return acts


def test_an_utterance_keeps_its_pre_roll_and_ends_after_the_silence():
    seg = vc.Segmenter(threshold=0.015, silence_ms=800)
    acts = _feed(seg, [QUIET] * 10 + [LOUD] * 5 + [QUIET] * 12)
    starts = [a for a in acts if a["do"] == "listening" and a["on"]]
    utt = [a["utterance"] for a in acts if a["do"] == "utterance"]
    assert len(starts) == 1 and starts[0]["at"] == 10 and len(utt) == 1
    blocks = utt[0].blocks
    assert blocks[:8] == [f"b{i}" for i in range(2, 10)]       # 8 blocks of pre-roll
    assert blocks[8] == "b10" and not utt[0].strict and not utt[0].pending_barge_in
    # ends once silence has lasted over 800 ms: it started at block 15 (1275 ms) -> block 25
    assert [a["at"] for a in acts if a["do"] == "utterance"] == [25]


def test_half_duplex_does_not_listen_while_she_is_audible():
    seg = vc.Segmenter()
    assert _feed(seg, [LOUD] * 5, audible=True) == []


def test_a_sound_while_she_thinks_waits_for_the_transcript():
    # A cough while the reply streams used to cancel it at the gate, before the transcript
    # could say it wasn't speech (predator, 2026-11-24). Now only words interrupt.
    seg = vc.Segmenter()
    acts = _feed(seg, [LOUD], busy=True, her_turn=True)
    assert not any(a["do"] == "abort" for a in acts)
    utt = [a["utterance"] for a in _feed(seg, [QUIET] * 12, start=1) if a["do"] == "utterance"][0]
    assert utt.pending_barge_in and utt.strict                 # overlapped her turn


def test_full_duplex_waits_for_the_transcript_before_interrupting():
    seg = vc.Segmenter(full_duplex=True)
    acts = _feed(seg, [LOUD], audible=True, busy=True, her_turn=True)
    assert not any(a["do"] == "abort" for a in acts)
    utt = [a["utterance"] for a in _feed(seg, [QUIET] * 12, start=1) if a["do"] == "utterance"][0]
    assert utt.pending_barge_in and utt.strict


def test_configure_reads_the_settings():
    seg = vc.Segmenter()
    seg.configure({"vad_threshold": 0.03, "vad_silence_ms": 500, "full_duplex": True})
    assert (seg.threshold, seg.silence_ms, seg.full_duplex) == (0.03, 500.0, True)
    seg.configure({})
    assert (seg.threshold, seg.silence_ms, seg.full_duplex) == (0.015, 800.0, False)


# ---- WAV ---------------------------------------------------------------------------

def test_wav_is_what_the_server_decodes():
    data = vc.wav_bytes([[0.0, 0.5, -0.5], [1.5, -2.0]], 48000)
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 48000, 5)
        import struct
        assert struct.unpack("<5h", w.readframes(5)) == (0, 16383, -16384, 32767, -32768)
    from services import stt
    x = stt._wav_samples(data)
    assert x is not None and len(x) == 2                       # 5 samples at 48 kHz -> 16 kHz



def test_her_name_then_a_pause_then_the_request():
    """"Mira." ... "play Stevie Nicks": two utterances, one request (predator, 2026-11-24)."""
    g = vc.WakeGate()
    g.configure({"wake_word": "Mira"})
    assert g.apply("Mira.", 1000) == ("", [{"do": "summoned"}])
    assert g.apply("Play Stevie Nicks.", 2500) == ("Play Stevie Nicks.", [])
    assert g.apply("and the weather", 3000) == ("", [])                 # once, not an open mic
    g.apply("Mira.", 10000)
    assert g.apply("play some blues", 10000 + 8001) == ("", [])         # too late
    g.apply("Mira.", 20000)
    assert g.apply("Mira, next song", 21000)[0] == "next song"        # said again in full: fine
    assert g.apply("hello there", 22000) == ("", [])
