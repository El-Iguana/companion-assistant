"""The hands-free microphone's decisions, in plain Python (#5).

No browser imports: the page runs this in Pyodide (``ui/mic.py`` connects it to
``static/js/voice.js``), the unit tests import it directly, and the server can use the
same rules later (#6). JavaScript keeps only what has to run on the audio thread or every
frame: capture, TTS playback, lip-sync. The desktop window has no Pyodide and still uses
voice.js's own copy of these rules.

Ported from voice.js with its measured reasons; times are milliseconds from the caller's
clock (``performance.now()`` for the segmenter and self-hearing, ``Date.now()`` for the
wake-word conversation, as in voice.js).
"""
from __future__ import annotations

import re
import struct
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Phrases: wake word, sleep phrase
# ---------------------------------------------------------------------------
# Compare on words alone: Whisper decides its own punctuation, and contractions are the
# transcriber's choice too ("that will" vs "that'll").
CONTRACTIONS = [
    (r"\bthat'?ll\b", "that will"), (r"\bthat'?s\b", "that is"),
    (r"\bit'?s\b", "it is"), (r"\bwe'?re\b", "we are"),
    (r"\bi'?m\b", "i am"), (r"\bdon'?t\b", "do not"),
    (r"\bwon'?t\b", "will not"), (r"\bcan'?t\b", "cannot"),
    (r"\byou'?re\b", "you are"), (r"\blet'?s\b", "let us"),
]


def norm_phrase(text: str) -> str:
    x = str(text or "").lower().replace("‘", "'").replace("’", "'")
    for pat, to in CONTRACTIONS:
        x = re.sub(pat, to, x)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", x)).strip()


def starts_with(text: str, phrase: str) -> bool:
    p = norm_phrase(phrase)
    if not p:
        return False
    t = norm_phrase(text)
    return t == p or t.startswith(p + " ")


def strip_prefix(text: str, phrase: str) -> str:
    """`text` without its first words matching `phrase`, keeping the rest's punctuation and
    capitals (the message as said)."""
    words = len([w for w in norm_phrase(phrase).split(" ") if w])
    t = re.sub(r"^[^\w]+", "", text.strip())
    for _ in range(words):
        t = re.sub(r"^[^\s]+\s*", "", t)
    return re.sub(r"^[\s,.:!?-]+", "", t).strip()


# ---------------------------------------------------------------------------
# Wake word + conversation state
# ---------------------------------------------------------------------------
class WakeGate:
    """Which transcripts are messages. With no wake word, every one is (open mic). With
    one, only a transcript that starts with it, minus the word. In "continuous" mode the
    wake word opens a conversation: follow-ups need no prefix until the sleep phrase or
    the idle timeout. The timeout is not a nicety: an open mic that never closes keeps
    transcribing the room after you walk away.

    Methods return actions for the page: ``{"do": "say", "text"}`` (the farewell),
    ``{"do": "toast", "msg", "kind"}``, ``{"do": "paint"}``."""

    def __init__(self) -> None:
        self.awake = False
        self.awake_until = 0.0
        self.wake_word = ""
        self.sleep_phrase = "that will be all"
        self.farewell = "Goodbye."
        self.mode = "phrase"
        self.sleep_after_s: float | None = 180

    def configure(self, cfg: dict) -> None:
        """The card's own phrases win over the global ones (the caller merges them)."""
        self.wake_word = str(cfg.get("wake_word") or "").strip()
        self.sleep_phrase = str(cfg.get("sleep_phrase") or "").strip()
        farewell = cfg.get("farewell")
        self.farewell = ("Goodbye." if farewell is None else str(farewell)).strip()
        self.mode = cfg.get("wake_mode") or "phrase"
        self.sleep_after_s = cfg.get("sleep_after_s", 180)

    def _timeout_ms(self) -> float:
        s = self.sleep_after_s
        return float("inf") if (s == 0 or s is None) else float(s) * 1000

    def set_awake(self, on: bool, why: str, now: float) -> list[dict]:
        if self.awake == on:
            if on:
                self.awake_until = now + self._timeout_ms()
            return []
        self.awake = on
        self.awake_until = now + self._timeout_ms() if on else 0.0
        out: list[dict] = []
        # Not when the mic is switched off: saying goodbye to someone who muted you is
        # talking to an empty room.
        if not on and why != "mic off" and self.farewell:
            out.append({"do": "say", "text": self.farewell})
        if on:
            out.append({"do": "toast", "kind": "info",
                        "msg": f"Listening — say “{self.sleep_phrase or 'stop'}” when done"})
        out.append({"do": "paint"})
        return out

    def keep_open(self, now: float) -> None:
        """Any speech keeps an open conversation open."""
        if self.awake:
            self.awake_until = now + self._timeout_ms()

    def tick(self, now: float) -> list[dict]:
        """Expire on a timer, not on the next utterance: an empty room never speaks."""
        if self.awake and now > self.awake_until:
            return self.set_awake(False, "idle timeout", now)
        return []

    def apply(self, text: str, now: float) -> tuple[str, list[dict]]:
        """(the message to send, or "" to ignore this utterance; actions)."""
        w, sleep = self.wake_word, self.sleep_phrase
        continuous = self.mode == "continuous"
        if not w:
            return text.strip(), []                       # pure open mic
        if continuous and self.awake and sleep:
            bare = strip_prefix(text, w) if starts_with(text, w) else text.strip()
            if starts_with(bare, sleep) or starts_with(text, sleep):
                return "", self.set_awake(False, "sleep phrase", now)
        if starts_with(text, w):
            rest = strip_prefix(text, w)
            acts = self.set_awake(True, "wake word", now) if continuous else []
            return rest, acts                             # her name alone is a summons, not a message
        if continuous and self.awake:
            if now > self.awake_until:
                return "", self.set_awake(False, "idle timeout", now)
            self.awake_until = now + self._timeout_ms()
            return text.strip(), []
        return "", []


# ---------------------------------------------------------------------------
# Self-hearing
# ---------------------------------------------------------------------------
SELF_HEARD_WINDOW_MS = 20000
SELF_HEARD_SHARE = 0.7


def looks_self_heard(text: str, spoken: list[dict], now: float, full_duplex: bool) -> bool:
    """Full duplex on speakers without an echo canceller makes her answer her own voice.
    Check the symptom rather than trust the setup: a transcript that is mostly words she
    said in the last 20 s. `spoken` is ``[{"t": ms, "text": lower-case}]``."""
    if not full_duplex:
        return False
    recent = [s for s in spoken if now - float(s.get("t", 0)) < SELF_HEARD_WINDOW_MS]
    if not recent:
        return False
    said = " ".join(str(s.get("text", "")) for s in recent)
    words = re.findall(r"[a-z']{3,}", str(text).lower())
    if len(words) < 3:
        return False
    return sum(1 for w in words if w in said) / len(words) > SELF_HEARD_SHARE


# ---------------------------------------------------------------------------
# Speech segmentation (energy gate)
# ---------------------------------------------------------------------------
PRE_ROLL_BLOCKS = 8            # ~0.7 s of 4096-frame blocks kept before the gate


class Utterance:
    """One finished utterance: its audio blocks (as the capture gave them), whether any
    of it overlapped her turn (`strict`), and whether a barge-in waits on its transcript."""

    def __init__(self, blocks: list, strict: bool, pending_barge_in: bool) -> None:
        self.blocks, self.strict, self.pending_barge_in = blocks, strict, pending_barge_in


class Segmenter:
    """Cuts the open mic into utterances by level, one capture block at a time.

    `block()` returns actions: ``{"do": "listening", "on": bool}`` for the mic button,
    ``{"do": "abort"}`` to cut her off now, and ``{"do": "utterance", "utterance": U}``
    when speech has ended."""

    def __init__(self, threshold: float = 0.015, silence_ms: float = 800,
                 full_duplex: bool = False) -> None:
        self.threshold, self.silence_ms, self.full_duplex = threshold, silence_ms, full_duplex
        self.reset()

    def configure(self, cfg: dict) -> None:
        self.threshold = float(cfg.get("vad_threshold") if cfg.get("vad_threshold") is not None else 0.015)
        self.silence_ms = float(cfg.get("vad_silence_ms") if cfg.get("vad_silence_ms") is not None else 800)
        self.full_duplex = bool(cfg.get("full_duplex"))

    def reset(self) -> None:
        self.speaking = False
        self.silence_start = 0.0
        self.pre: list = []
        self.pcm: list = []
        self.strict = False
        self.pending_barge_in = False

    def block(self, rms: float, block: Any, now: float, audible: bool, her_turn: bool,
              busy: bool) -> list[dict]:
        """`audible`: her voice is playing or stopped under 0.4 s ago. `her_turn`: wider —
        the reply is streaming, a line is queued or playing, or she stopped under 1.5 s
        ago, so a clip that opens between two lines still counts. `busy`: a line is
        playing or the reply is streaming (an interruption has something to cut)."""
        out: list[dict] = []
        if not self.speaking:
            # Half duplex (default) does not listen while she speaks. Full duplex lifts
            # that — only right when her voice can't reach the mic (headphones, or an AEC
            # source): the raw mic hears her at +26 dB over the floor, the AEC source at
            # -39 dB, so no threshold separates them.
            self_active = audible and not self.full_duplex
            if not self_active and rms > self.threshold:
                # Keep what was said just before the gate opened: the gate opens once
                # speech is already loud, and the wake word is the very first thing said.
                self.pcm = self.pre[:]
                self.speaking, self.silence_start = True, 0.0
                self.strict = her_turn
                out.append({"do": "listening", "on": True})
                # Energy can't tell your voice from her echo, or from a cough, and only the
                # transcript can — so the abort always waits for it (one STT round-trip).
                # Cutting at the gate let any noise cancel a reply she was still thinking
                # about (predator, 2026-11-24: two "what time is it?" turns lost unsaved).
                if busy or (audible and self.full_duplex):
                    self.pending_barge_in = True
        else:
            if her_turn:
                self.strict = True
            if rms > self.threshold:
                self.silence_start = 0.0
            elif not self.silence_start:
                self.silence_start = now
            elif now - self.silence_start > self.silence_ms:
                self.speaking, self.silence_start = False, 0.0
                self.pcm.append(block)
                u = Utterance(self.pcm, self.strict, self.pending_barge_in)
                self.pcm, self.pre, self.strict, self.pending_barge_in = [], [], False, False
                out += [{"do": "listening", "on": False}, {"do": "utterance", "utterance": u}]
                return out
        if self.speaking:
            self.pcm.append(block)
        else:
            self.pre.append(block)
            if len(self.pre) > PRE_ROLL_BLOCKS:
                self.pre.pop(0)
        return out


# ---------------------------------------------------------------------------
# WAV
# ---------------------------------------------------------------------------
def wav_bytes(blocks: list, rate: int, to_floats: Callable[[Any], Any] = lambda b: b) -> bytes:
    """16-bit mono PCM WAV of the float blocks, as voice.js's wav() writes it (the server's
    stt.py decodes exactly this). `to_floats` turns one captured block into a float
    sequence (in the page, a JS Float32Array's memory)."""
    samples = []
    for b in blocks:
        for x in to_floats(b):
            x = -1.0 if x < -1.0 else 1.0 if x > 1.0 else x
            samples.append(int(x * 0x8000) if x < 0 else int(x * 0x7FFF))
    n = len(samples)
    head = (b"RIFF" + struct.pack("<I", 36 + n * 2) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", n * 2))
    return head + struct.pack(f"<{n}h", *samples)
