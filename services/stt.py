"""Local speech-to-text via faster-whisper (CTranslate2).

CPU int8 by default — mirrors the Kokoro TTS choice (local, no torch, no cloud).
The model is baked into the image at build time (see Containerfile). For short
push-to-talk clips `base.en` on CPU is fast enough; if latency disappoints, move
STT to a GPU whisper.cpp container on ai-net (the original plan) and
point WHISPER_* at it instead.

`faster-whisper` bundles ffmpeg via PyAV, so it decodes the browser's WebM/Opus
recording directly — no system ffmpeg needed.
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


def transcribe(audio: bytes, language: str | None = None) -> str:
    """Transcribe audio bytes (any ffmpeg-decodable container) to plain text.

    `vad_filter` trims leading/trailing silence so push-to-talk clips with a bit
    of dead air don't hallucinate; `beam_size=1` keeps it snappy.
    """
    segments, _info = _whisper().transcribe(
        io.BytesIO(audio),
        language=language,
        beam_size=1,
        vad_filter=True,
    )
    return " ".join(s.text.strip() for s in segments).strip()
