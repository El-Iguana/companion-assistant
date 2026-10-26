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


def transcribe(audio: bytes, language: str | None = None) -> str:
    """Transcribe audio bytes (any ffmpeg-decodable container) to plain text.

    `vad_filter` trims leading/trailing silence so push-to-talk clips with a bit
    of dead air don't hallucinate; `beam_size=1` keeps it snappy.
    """
    samples = _wav_samples(audio)
    segments, _info = _whisper().transcribe(
        samples if samples is not None else io.BytesIO(audio),
        language=language,
        beam_size=1,
        vad_filter=True,
    )
    return " ".join(s.text.strip() for s in segments).strip()
