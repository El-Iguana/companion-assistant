"""Local text-to-speech via Kokoro (ONNX, CPU).

Reuses the onnxruntime already present for rembg — no torch, no cloud. The model
is baked into the image at /app/models/kokoro (see Containerfile).
"""
from __future__ import annotations

import io
import os
import re

MODEL = os.environ.get("KOKORO_MODEL", "/app/models/kokoro/kokoro-v1.0.onnx")
VOICES = os.environ.get("KOKORO_VOICES", "/app/models/kokoro/voices-v1.0.bin")

_engine = None


def _kokoro():
    global _engine
    if _engine is None:
        from kokoro_onnx import Kokoro
        _engine = Kokoro(MODEL, VOICES)
    return _engine


def voices() -> list[str]:
    try:
        return sorted(_kokoro().get_voices())
    except Exception:
        return ["af_sarah"]


def synth_wav(text: str, voice: str = "af_sarah", speed: float = 1.0) -> bytes:
    """Synthesize `text` to WAV bytes. Strips *action* narration and quotes so she 'speaks'."""
    import soundfile as sf
    clean = re.sub(r"\*[^*]*\*", " ", text)     # drop *action* narration
    clean = re.sub(r'["_]', "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        clean = "..."
    samples, sr = _kokoro().create(clean, voice=voice, speed=speed, lang="en-us")
    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV")
    return buf.getvalue()
