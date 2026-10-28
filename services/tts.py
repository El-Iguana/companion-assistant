"""Local text-to-speech — Kokoro (default) + Piper (per-voice ONNX models) + Orpheus.

Both run on the onnxruntime already present for rembg — no torch, no cloud. Kokoro is
one model + a bank of style embeddings ("af_sarah"); Piper is one .onnx per voice, so
its voices are namespaced `piper:<name>` and loaded from /app/models/piper (see
Containerfile). `orpheus:<name>` is the expressive voice served by the stack's
orpheus-tts container (services/orpheus.py); it falls back to a Kokoro voice whenever
Orpheus can't speak. Anything without a prefix goes to Kokoro.
"""
from __future__ import annotations

import io
import os
import re
import wave

MODEL = os.environ.get("KOKORO_MODEL", "/app/models/kokoro/kokoro-v1.0.onnx")
VOICES = os.environ.get("KOKORO_VOICES", "/app/models/kokoro/voices-v1.0.bin")
PIPER_DIR = os.environ.get("PIPER_DIR", "/app/models/piper")
PIPER_PREFIX = "piper:"
# Piper's raw output is much quieter than Kokoro's; this gain matches the two so
# switching a character's voice doesn't jump in volume (see _piper_wav).
PIPER_GAIN = float(os.environ.get("PIPER_GAIN", "3.5"))

_engine = None
_piper: dict[str, object] = {}


def _kokoro():
    global _engine
    if _engine is None:
        from kokoro_onnx import Kokoro
        _engine = Kokoro(MODEL, VOICES)
    return _engine


def _piper_names() -> list[str]:
    """Voice names available in PIPER_DIR — a `<name>.onnx` with a sibling `<name>.onnx.json`."""
    try:
        entries = os.listdir(PIPER_DIR)
    except OSError:
        return []
    return sorted(f[:-5] for f in entries
                  if f.endswith(".onnx") and f"{f}.json" in entries)


def _piper_voice(name: str):
    """Load (and cache) one Piper voice. Raises if the model or the package is missing."""
    if name not in _piper:
        from piper import PiperVoice
        path = os.path.join(PIPER_DIR, f"{name}.onnx")
        if not os.path.exists(path):
            raise FileNotFoundError(f"unknown piper voice: {name}")
        _piper[name] = PiperVoice.load(path, config_path=f"{path}.json")
    return _piper[name]


def voices() -> list[str]:
    try:
        out = sorted(_kokoro().get_voices())
    except Exception:
        out = ["af_sarah"]
    out += [PIPER_PREFIX + n for n in _piper_names()]
    # Listed only when the service is there, so a compose install without it never
    # offers a voice that can only ever fall back.
    from services import orpheus
    if orpheus.available():
        out += [orpheus.PREFIX + n for n in orpheus.VOICES]
    return out


def _clean(text: str) -> str:
    """Strip *action* narration and quotes so she 'speaks' rather than reads the prose."""
    clean = re.sub(r"\*[^*]*\*", " ", text)     # drop *action* narration
    clean = re.sub(r'["_]', "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or "..."


def _piper_wav(text: str, name: str, speed: float) -> bytes:
    from piper import SynthesisConfig
    voice = _piper_voice(name)
    # Piper scales phoneme *length*, so it's the inverse of Kokoro's speed multiplier;
    # scale the voice's own tuned length_scale rather than replacing it.
    # normalize_audio would rescale EVERY chunk to full range — with the frontend's
    # sentence-chunked streaming that pumps the level per sentence, so use a fixed gain.
    syn = SynthesisConfig(
        length_scale=voice.config.length_scale / max(speed, 0.1),
        normalize_audio=False,
        volume=PIPER_GAIN,
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        voice.synthesize_wav(text, wav, syn_config=syn)
    return buf.getvalue()


def stream_pcm(text: str, voice: str):
    """Orpheus voices, streamed: raw PCM s16le 24 kHz mono chunks as they are generated
    (services/orpheus.stream_pcm). If Orpheus can't speak, the line is spoken whole in
    its Kokoro fallback voice, which is 24 kHz too, so the stream format never changes."""
    from services import orpheus
    clean = _clean(text)
    name = voice[len(orpheus.PREFIX):] if voice.startswith(orpheus.PREFIX) else ""
    started = False
    try:
        for chunk in orpheus.stream_pcm(clean, name):
            started = True
            yield chunk
        return
    except Exception as e:
        if started:                     # mid-sentence: nothing sensible to splice in
            import logging
            logging.getLogger("tts").warning("orpheus stream broke off: %s", e)
            return
        import logging
        logging.getLogger("tts").warning("orpheus failed, falling back to Kokoro: %s", e)
    wav = synth_wav(re.sub(r"\s*<(%s)>\s*" % "|".join(orpheus.TAGS), " ", clean),
                    orpheus.fallback_voice(name))
    with wave.open(io.BytesIO(wav)) as w:
        yield w.readframes(w.getnframes())


# Bounds for the speaking pace, matching CharacterCard.tts_speed. Clamped here as well
# because /api/tts takes a raw dict, not a validated model — a 0 or a negative divides
# through in _piper_wav and is meaningless to Kokoro.
SPEED_MIN, SPEED_MAX = 0.5, 1.5


def synth_wav(text: str, voice: str = "af_sarah", speed: float = 1.0) -> bytes:
    """Synthesize `text` to WAV bytes, routed by voice id (`piper:<name>` -> Piper)."""
    clean = _clean(text)
    speed = min(max(speed, SPEED_MIN), SPEED_MAX)
    from services import orpheus
    if voice.startswith(orpheus.PREFIX):
        name = voice[len(orpheus.PREFIX):]
        try:
            return orpheus.synth_wav(clean, name)
        except Exception as e:
            # Never silent: an unloaded/unreachable Orpheus speaks the line in Kokoro.
            import logging
            logging.getLogger("tts").warning("orpheus failed, falling back to Kokoro: %s", e)
            voice = orpheus.fallback_voice(name)
    # Kokoro and Piper would read a sound tag out loud ("less than laugh greater than").
    clean = re.sub(r"\s*<(%s)>\s*" % "|".join(orpheus.TAGS), " ", clean).strip() or "..."
    if voice.startswith(PIPER_PREFIX):
        return _piper_wav(clean, voice[len(PIPER_PREFIX):], speed)
    import soundfile as sf
    samples, sr = _kokoro().create(clean, voice=voice, speed=speed, lang="en-us")
    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV")
    return buf.getvalue()
