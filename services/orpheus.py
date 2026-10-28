"""Orpheus TTS (Canopy Labs, 3B) — expressive voice, loaded only while voice is on.

The model runs in the stack's `orpheus-tts` container (KoboldCPP in admin mode, started
with no model, so it costs no VRAM until used). Pantheon asks it to load when a voice is
switched on and to unload when it is switched off (`warm` / `release`, driven by
voice.js); KoboldCPP also unloads by itself after an idle timeout as a backstop.

Orpheus is a Llama model whose output tokens are SNAC audio codes, 7 per frame. They
are decoded here on the CPU with the ONNX export of the SNAC 24 kHz decoder (baked into
the image), so Pantheon needs no torch and the decoder takes no VRAM.

Emotion comes from the wording and from inline tags the model voices instead of
reading: <laugh> <chuckle> <sigh> <gasp> <cough> <sniffle> <groan> <yawn>.

Format and sampling follow Canopy's own engine (orpheus_tts/engine_class.py, decoder.py).
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
import urllib.request
import wave

URL = os.environ.get("ORPHEUS_URL", "http://orpheus-tts:5005").rstrip("/")
DECODER = os.environ.get("SNAC_DECODER", "/app/models/snac/decoder_model.onnx")
CONFIG = "orpheus.kcpps"            # the .kcpps the orpheus-tts service exports
PREFIX = "orpheus:"
VOICES = ["tara", "leah", "jess", "mia", "zoe", "leo", "dan", "zac"]
MALE = {"leo", "dan", "zac"}
TAGS = ("laugh", "chuckle", "sigh", "gasp", "cough", "sniffle", "groan", "yawn")
SR = 24000
LOAD_TIMEOUT = 60                   # s; measured load is 3 s (file cached) to 15 s
FAIL_COOLDOWN = 60                  # s; after a failed load, fall back without retrying

log = logging.getLogger("orpheus")
_lock = threading.Lock()
_load_requested = 0.0               # monotonic time of the last load request
_failed_at = 0.0                    # monotonic time of the last failed load
_decoder = None


class OrpheusUnavailable(RuntimeError):
    pass


# ---- the orpheus-tts service ------------------------------------------------------
def _call(path: str, body: dict | None = None, timeout: float = 5.0):
    req = urllib.request.Request(
        URL + path, data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def available() -> bool:
    """The service is up and offers the Orpheus config."""
    try:
        return CONFIG in _call("/api/admin/list_options", timeout=1.5)
    except Exception:
        return False


def _probe() -> str:
    """'ready' (model loaded), 'idle' (server up, no model) or 'down' (not answering).

    A load restarts the server process, so it reads idle -> down -> ready. A load that
    fails (out of VRAM) comes back up with no model: idle -> down -> idle. That second
    idle is the only failure signal the admin API gives, and the code below keys on it.
    """
    try:
        r = str(_call("/api/v1/model", timeout=1.5).get("result", "")).lower()
    except Exception:
        return "down"
    return "ready" if "orpheus" in r else "idle"


def loaded() -> bool:
    return _probe() == "ready"


def warm(wait: bool = False) -> str:
    """Make sure the model is loaded (or loading). Returns 'ready' or 'loading'.

    One load request per LOAD_TIMEOUT window: a second reload_config while the first
    is still loading would restart it. With `wait`, blocks until it is ready and raises
    OrpheusUnavailable as soon as the load is seen to fail. After a failure, calls
    within FAIL_COOLDOWN raise at once, so each sentence falls back to Kokoro instead
    of retrying a load that cannot fit.
    """
    global _load_requested, _failed_at
    if loaded():
        return "ready"
    if time.monotonic() - _failed_at < FAIL_COOLDOWN:
        raise OrpheusUnavailable("orpheus failed to load recently; using the fallback voice")
    with _lock:
        if time.monotonic() - _load_requested > LOAD_TIMEOUT and not loaded():
            try:
                _call("/api/admin/reload_config", {"filename": CONFIG}, timeout=10)
            except Exception as e:
                raise OrpheusUnavailable(f"orpheus-tts not reachable at {URL}: {e}") from e
            _load_requested = time.monotonic()
            log.info("orpheus: load requested")
    if not wait:
        return "loading"

    def failed(why: str):
        global _load_requested, _failed_at
        _failed_at, _load_requested = time.monotonic(), 0.0
        log.warning("orpheus: %s", why)
        return OrpheusUnavailable(why)

    seen_down = False
    while time.monotonic() < _load_requested + LOAD_TIMEOUT:
        state = _probe()
        if state == "ready":
            return "ready"
        if state == "down":
            seen_down = True
        elif seen_down or time.monotonic() - _load_requested > 8:
            raise failed("orpheus model failed to load (not enough GPU memory?)")
        time.sleep(0.3)
    raise failed("orpheus model did not load in time")


def release() -> None:
    """Unload the model and give its VRAM back. Safe when it isn't loaded.

    Also clears a recent failure: switching voice off and on again is the user asking
    for another try (say, after closing whatever was holding the GPU)."""
    global _load_requested, _failed_at
    try:
        if loaded() or time.monotonic() - _load_requested < LOAD_TIMEOUT:
            _call("/api/admin/reload_config", {"filename": "unload_model"}, timeout=10)
            log.info("orpheus: unloaded")
    except Exception as e:
        log.info("orpheus: unload skipped: %s", e)
    _load_requested = _failed_at = 0.0


# ---- speech -----------------------------------------------------------------------
def _codes(text: str, voice: str) -> list[int]:
    prompt = (f"<custom_token_3><|begin_of_text|>{voice}: {text}<|eot_id|>"
              f"<custom_token_4><custom_token_5><custom_token_1>")
    # ~84 audio tokens per second of speech; the cap only stops a runaway, the real end
    # is the end-of-speech token (custom_token_2).
    cap = max(400, min(3600, 60 * len(text.split()) + 300))
    out = _call("/api/v1/generate", {
        "prompt": prompt, "max_length": cap, "max_context_length": 4096,
        "temperature": 0.6, "top_p": 0.8, "rep_pen": 1.3,
        "stop_sequence": ["<custom_token_2>"]}, timeout=120)
    return [int(n) for n in re.findall(r"<custom_token_(\d+)>", out["results"][0]["text"])]


def _decode(nums: list[int]):
    import numpy as np
    global _decoder
    if _decoder is None:
        import onnxruntime as ort
        _decoder = ort.InferenceSession(DECODER, providers=["CPUExecutionProvider"])
    ids = np.array([n - 10 - (i % 7) * 4096 for i, n in enumerate(nums)], dtype=np.int64)
    ids = ids[: len(ids) // 7 * 7].reshape(-1, 7)
    if not len(ids):
        raise OrpheusUnavailable("orpheus returned no audio")
    ids = ids.clip(0, 4095)
    feeds = {
        "audio_codes.0": ids[:, 0][None, :],
        "audio_codes.1": ids[:, [1, 4]].reshape(1, -1),
        "audio_codes.2": ids[:, [2, 3, 5, 6]].reshape(1, -1),
    }
    return _decoder.run(None, feeds)[0].reshape(-1)


def synth_wav(text: str, voice: str) -> bytes:
    """Speak `text` in Orpheus voice `voice` (e.g. 'tara'); WAV bytes, 24 kHz mono."""
    import numpy as np
    if voice not in VOICES:
        voice = VOICES[0]
    warm(wait=True)
    audio = _decode(_codes(text, voice))
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm)
    return buf.getvalue()


def fallback_voice(voice: str) -> str:
    """The Kokoro voice to use when Orpheus can't speak."""
    return "am_michael" if voice in MALE else "af_heart"
