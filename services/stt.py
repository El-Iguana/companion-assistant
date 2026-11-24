"""Local speech-to-text via faster-whisper (CTranslate2).

CPU int8 by default — mirrors the Kokoro TTS choice (local, no torch, no cloud).
The model is baked into the image at build time (see Containerfile). For short
push-to-talk clips `base.en` on CPU is fast enough; if latency disappoints, move
STT to a GPU whisper.cpp container on ai-net (the original plan) and
point WHISPER_* at it instead.

WAV (what both pages send) is decoded here with the stdlib; anything else goes to
faster-whisper's PyAV decoder — see ``_wav_samples`` for why.
"""
from __future__ import annotations

import io
import os

MODEL = os.environ.get("WHISPER_MODEL", "base.en")
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
CACHE = os.environ.get("WHISPER_CACHE", "/app/models/whisper")

_model = None


def _whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE,
                              download_root=CACHE)
    return _model


def _wav_samples(audio: bytes):
    """PCM WAV -> mono float32 at 16 kHz, or None for anything else.

    Both pages record PCM and send WAV. Decoding it here keeps PyAV out of the path:
    faster-whisper 1.2 calls ``av.open(..., metadata_errors=...)``, which PyAV 19 no
    longer accepts, so every transcription raised a TypeError (measured in the image:
    av 19.0.2 + faster-whisper 1.2.1). Other containers still go through PyAV.
    """
    import wave

    import numpy as np

    if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return None
    try:
        with wave.open(io.BytesIO(audio)) as w:
            if w.getsampwidth() != 2:
                return None
            rate, channels = w.getframerate(), w.getnchannels()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    except (wave.Error, EOFError):
        return None
    x = pcm.astype(np.float32) / 32768.0
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    if rate != 16000 and x.size:
        n = int(round(x.size * 16000 / rate))
        x = np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype(np.float32)
    return x


# --- hallucination guards -----------------------------------------------------
# Whisper invents speech from near-silence: faint echo of the character's own voice
# on an echo-cancelled mic became a 74-word "user" message ("Alright. Okay. Let's go
# down there...") out of 1.26 s of sound, and was sent as if the user had said it.
# Thresholds measured in the image (base.en, Kokoro speech at full/0.3/0.1 gain vs
# chopped faint echo and noise; 2026-10-28):
#   real speech:  no_speech_prob <= 0.34, avg_logprob >= -0.95, <= 3.1 words/s of VAD audio
#   faint echo:   no_speech_prob up to 0.75, avg_logprob down to -1.39
# Only a third of echo hallucinations are separable this way — the rest score like
# speech — so these catch the unconfident ones and the impossible runs, nothing more.
MAX_WORDS_PER_SEC = 6.0      # of speech left after VAD; real speech peaked at 3.1
NO_SPEECH_DROP = 0.5         # segment dropped when no_speech_prob is above this...
LOGPROB_DROP = -0.7          # ...and avg_logprob is below this (confident words stay)
COMPRESSION_DROP = 2.4       # Whisper's own repetition-loop threshold
# Strict: the clip overlapped the character speaking (voice.js herTurn), so echo of her
# voice is the likely source. Same measurements, applied per segment:
#   kept real speech 102/108 (losses mostly at 0.1 gain, i.e. very quiet);
#   faint-echo hallucinations getting through 11/19 -> 1/19.
STRICT_NO_SPEECH = 0.2       # dropped above this...
STRICT_LOGPROB = -0.9        # ...or below this


def _accept(segments, speech_s: float, strict: bool = False) -> str:
    """Join the segments that pass the guards; '' when the whole thing is implausible."""
    import logging
    log = logging.getLogger("stt")
    kept = []
    for s in segments:
        t = s.text.strip()
        if not t:
            continue
        if s.compression_ratio > COMPRESSION_DROP or (
                s.no_speech_prob > NO_SPEECH_DROP and s.avg_logprob < LOGPROB_DROP):
            log.info("stt: dropped segment (no_speech %.2f, logprob %.2f, compression %.2f): %r",
                     s.no_speech_prob, s.avg_logprob, s.compression_ratio, t[:120])
            continue
        if strict and (s.no_speech_prob > STRICT_NO_SPEECH or s.avg_logprob < STRICT_LOGPROB):
            log.info("stt: dropped segment, strict (no_speech %.2f, logprob %.2f): %r",
                     s.no_speech_prob, s.avg_logprob, t[:120])
            continue
        kept.append(t)
    text = " ".join(kept).strip()
    words = len(text.split())
    if words and words / max(speech_s, 0.5) > MAX_WORDS_PER_SEC:
        log.info("stt: rejected %d words from %.2f s of speech: %r", words, speech_s, text[:120])
        return ""
    return text


def transcribe(audio: bytes, language: str | None = None, strict: bool = False,
               wake_word: str = "") -> str:
    """Transcribe audio bytes (any ffmpeg-decodable container) to plain text.

    `vad_filter` trims leading/trailing silence so push-to-talk clips with a bit
    of dead air don't hallucinate; `beam_size=1` keeps it snappy. Not conditioning
    on previous text stops one invented segment from seeding the next, which is
    how a hallucination turns into a run. `_accept` drops what is left over.

    `wake_word`: named to Whisper in a sentence of context ("The assistant's name is Mira.").
    Without it base.en wrote "Mira" as "Mirror", "Miro" or "MIRO" and the exact wake-word match
    failed: 17 of 28 synthetic wake commands began with "Mira", 28 of 28 with it. faster-whisper's
    ``hotwords`` is the wrong tool: it reads as text already heard, so Whisper left the wake word
    out altogether (15 of 28). eval/listen, 2026-11-24.
    """
    samples = _wav_samples(audio)
    segments, info = _whisper().transcribe(
        samples if samples is not None else io.BytesIO(audio),
        language=language,
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt=(f"The assistant's name is {wake_word.strip()}." if wake_word.strip() else None),
    )
    return _accept(list(segments), info.duration_after_vad or info.duration or 0.0, strict)
