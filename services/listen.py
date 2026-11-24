"""The open microphone, decided where the audio is (#6, #90): Silero VAD, then Whisper.

A page streams 16 kHz mono PCM; this cuts it into utterances by a real speech detector instead
of a level threshold, transcribes only those, with the wake word as a hint, and puts each
through the same wake gate as the pages (``appcode/ui/voicecore.py``).

Why (eval/listen/RESULTS.md, baseline): over a level threshold, music never reads as quiet,
so an utterance never ended and 11 of 12 wake commands during music were lost; a third of the
Whisper calls were on knocks and room tone; and Whisper wrote "Mira" as "Mirror" or "Miro".

Silero is faster-whisper's own copy (``assets/silero_vad_v6.onnx``, already in the image),
run one 32 ms frame at a time with its state carried, so no new dependency.

Times are seconds on the stream's own clock (samples / 16000).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

RATE = 16000
FRAME = 512                  # Silero's frame at 16 kHz: 32 ms
CONTEXT = 64                 # samples of the previous frame it also reads
START_PROB = 0.5             # speech starts after START_FRAMES frames at or above this
START_FRAMES = 2             # 64 ms: a click is one frame
END_PROB = 0.35              # and ends after END_SILENCE_S below this
END_SILENCE_S = 0.5          # shorter than the pages' 0.8 s: Silero isn't fooled by steady noise
PRE_ROLL_S = 0.5             # kept from before the start: the wake word is the first thing said
MAX_UTTERANCE_S = 12.0       # sung vocals can read as speech; a wake command is short
MIN_UTTERANCE_S = 0.3        # shorter is a cough, not a command
# Her name heard while the utterance is still going (#106): the music ducks at once and the
# pause after the name may last this long, not END_SILENCE_S. Said with a pause ("Mira…
# play Pink") the request used to be cut off after the name, and over music the name was
# often lost (user, 2026-11-24: "I have to give the command too fast after the wake word…
# if music is playing it doesn't hear").
WAKE_HOLD_S = 2.0            # the pause after her name alone; once the request starts, END_SILENCE_S
CHECKING_HOLD_S = 1.2        # a pause while the look for her name is still running isn't an end yet
EARLY_AT_S = (1.2, 2.4)      # into an utterance (pre-roll included): looked for then, and at each of
EARLY_MAX = 3                #   its first pauses; at most this many looks per utterance


class Vad:
    """Silero, frame by frame."""

    _session = None

    def __init__(self) -> None:
        self.reset()

    @classmethod
    def _model(cls):
        if cls._session is None:
            import faster_whisper
            import onnxruntime
            opts = onnxruntime.SessionOptions()
            opts.inter_op_num_threads = opts.intra_op_num_threads = 1
            opts.log_severity_level = 4
            path = os.path.join(os.path.dirname(faster_whisper.__file__), "assets", "silero_vad_v6.onnx")
            cls._session = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"], sess_options=opts)
        return cls._session

    def reset(self) -> None:
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.context = np.zeros(CONTEXT, np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self.context, frame.astype(np.float32)])[None, :]
        prob, self.h, self.c = self._model().run(None, {"input": x, "h": self.h, "c": self.c})
        self.context = frame[-CONTEXT:].astype(np.float32)
        return float(np.asarray(prob).reshape(-1)[0])


@dataclass
class Utterance:
    audio: np.ndarray
    start: float
    end: float               # when the speech ended
    decided: float           # when the silence after it was long enough to say so
    strict: bool             # some of it overlapped her turn (echo is likely)
    n: int = 0               # which utterance of the stream (Segmenter.n)
    partial: bool = False    # the start of one still going (the early look for her name)


@dataclass
class Segmenter:
    """Frames in, utterances out. ``audible``: her voice is playing (or under 0.4 s ago);
    ``her_turn``: wider, as the pages define it. Half duplex unless ``full_duplex``."""

    full_duplex: bool = False
    vad: Vad = field(default_factory=lambda: Vad())       # resolved at creation (tests swap it)

    def __post_init__(self) -> None:
        self.t = 0.0
        self.pre: list[np.ndarray] = []
        self.buf: list[np.ndarray] = []
        self.speaking = False
        self.above = 0
        self.quiet_since: float | None = None
        self.start = 0.0
        self.strict = False
        self.n = 0                                       # utterances started so far
        self.end_silence = END_SILENCE_S                 # this utterance's; hold() changes it
        self.hold_to_speech = False                      # back to END_SILENCE_S once speech resumes

    def hold(self, n: int, seconds: float, until_speech: bool = False) -> bool:
        """How long utterance ``n`` may pause before it counts as over: WAKE_HOLD_S after her
        name alone (``until_speech``: only that pause, then the usual), the usual again when
        the look found no name. False when it has already ended."""
        if not self.speaking or self.n != n:
            return False
        self.end_silence = seconds
        self.hold_to_speech = until_speech
        return True

    @property
    def pausing(self) -> bool:
        return self.speaking and self.quiet_since is not None

    def so_far(self) -> np.ndarray:
        """The audio of the utterance in progress."""
        return np.concatenate(self.buf) if self.buf else np.zeros(0, np.float32)

    def frame(self, x: np.ndarray, audible: bool = False, her_turn: bool = False) -> Utterance | None:
        p = self.vad(x)
        self.t += FRAME / RATE
        out = None
        if not self.speaking:
            self.pre.append(x)
            if len(self.pre) > int(PRE_ROLL_S * RATE / FRAME):
                self.pre.pop(0)
            deaf = audible and not self.full_duplex
            self.above = self.above + 1 if (p >= START_PROB and not deaf) else 0
            if self.above >= START_FRAMES:
                self.speaking, self.quiet_since = True, None
                self.n += 1
                self.end_silence, self.hold_to_speech = END_SILENCE_S, False
                self.buf, self.pre = self.pre, []
                self.start = self.t - len(self.buf) * FRAME / RATE
                self.strict = her_turn
            return None
        self.buf.append(x)
        self.strict = self.strict or her_turn
        if p >= END_PROB:
            if self.quiet_since is not None and self.hold_to_speech:
                self.end_silence, self.hold_to_speech = END_SILENCE_S, False   # the request began
            self.quiet_since = None
        elif self.quiet_since is None:
            self.quiet_since = self.t - FRAME / RATE
        long_quiet = self.quiet_since is not None and self.t - self.quiet_since >= self.end_silence
        too_long = self.t - self.start >= MAX_UTTERANCE_S
        if long_quiet or too_long:
            end = self.quiet_since if long_quiet else self.t
            audio = np.concatenate(self.buf)
            if end - self.start >= MIN_UTTERANCE_S + PRE_ROLL_S * 0.5:
                out = Utterance(audio, self.start, end, self.t, self.strict, self.n)
            self.speaking, self.buf, self.above, self.quiet_since = False, [], 0, None
            self.strict = False
            self.end_silence, self.hold_to_speech = END_SILENCE_S, False
        return out


def transcribe(u: Utterance, wake_word: str = "") -> str:
    """Whisper on one utterance, told the wake word (stt.transcribe)."""
    from services import stt
    return stt.transcribe(_wav(u.audio), strict=u.strict, wake_word=wake_word)


def _wav(x: np.ndarray) -> bytes:
    import io
    import wave
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return b.getvalue()


def eval_pipeline(x: np.ndarray, events: list, flags) -> list[dict]:
    """eval/listen/run.py's view: the utterances this would send to Whisper on a scene."""
    audible, her_turn = flags
    seg, out = Segmenter(), []
    for i in range(0, x.size - FRAME + 1, FRAME):
        t = (i + FRAME) / RATE
        u = seg.frame(x[i:i + FRAME], audible(t), her_turn(t))
        if u:
            out.append({"start": u.start, "end": u.end, "decided": u.decided, "audio": u.audio,
                        "strict": u.strict, "wake_word": "Mira"})
    return out


# --- a page's open microphone, live (/pmedia/listen) ---------------------------------------------

def _voicecore():
    """The pages' wake gate (appcode/ui/voicecore.py), loaded by path: appcode is the
    browser's package, not the server's."""
    import importlib.util
    import sys
    from pathlib import Path
    if "voicecore" in sys.modules:
        return sys.modules["voicecore"]
    path = Path(__file__).resolve().parents[1] / "appcode" / "ui" / "voicecore.py"
    spec = importlib.util.spec_from_file_location("voicecore", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["voicecore"] = mod
    spec.loader.exec_module(mod)
    return mod


class Session:
    """One page's microphone. In: 16 kHz mono int16 PCM (binary messages, any length) and
    JSON: ``{"config": {...the card's merged voice settings}}``, ``{"her": {"audible", "turn"}}``.
    Out, through ``send``: ``{"type": "listening", "on"}`` when speech starts and stops,
    ``{"type": "heard", "text", "send", "strict", "ms"}`` per utterance (``send`` is the message,
    "" when the wake gate drops it; ``ms`` from the end of speech to now), and the gate's own
    actions as ``{"type": "action", ...}``, and ``{"type": "awake", "on", "why"}`` when a
    continuous conversation opens or closes (the page waves, says the farewell, ducks the music
    and saves the chat on it)."""

    def __init__(self, send) -> None:
        import asyncio
        vc = _voicecore()
        self.send = send
        self.seg = Segmenter()
        self.gate = vc.WakeGate()
        self.wake_word = ""
        self.audible = self.turn = False
        self.pending = np.zeros(0, np.float32)
        self.lock = asyncio.Lock()
        self.tasks: set = set()
        self.early_next = 0                  # index into EARLY_AT_S for the utterance in progress
        self.early_looks = 0                 # looks taken in it
        self.was_pausing = False
        self.early_busy = False
        self.named: set[int] = set()         # utterances whose start was heard to be her name

    def configure(self, cfg: dict) -> None:
        self.gate.configure(cfg)
        self.wake_word = self.gate.wake_word
        self.seg.full_duplex = bool(cfg.get("full_duplex"))

    async def message(self, msg: dict) -> None:
        if isinstance(msg.get("config"), dict):
            self.configure(msg["config"])
        her = msg.get("her")
        if isinstance(her, dict):
            self.audible, self.turn = bool(her.get("audible")), bool(her.get("turn"))

    async def audio(self, data: bytes) -> None:
        import asyncio
        x = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
        self.pending = np.concatenate([self.pending, x])
        while self.pending.size >= FRAME:
            frame, self.pending = self.pending[:FRAME], self.pending[FRAME:]
            was = self.seg.speaking
            u = self.seg.frame(frame, self.audible, self.turn)
            if self.seg.speaking != was:
                await self.send({"type": "listening", "on": self.seg.speaking})
                self.early_next = self.early_looks = 0
                self.was_pausing = False
            pause_began = self.seg.pausing and not self.was_pausing
            self.was_pausing = self.seg.pausing
            if self.seg.speaking and self._early_due(pause_began):
                self.early_looks += 1
                while self.early_next < len(EARLY_AT_S) and self.seg.t - self.seg.start >= EARLY_AT_S[self.early_next]:
                    self.early_next += 1
                if pause_began:
                    self.seg.hold(self.seg.n, CHECKING_HOLD_S)    # don't end while we look
                self.early_busy = True
                t = asyncio.ensure_future(self._early(self.seg.n, self.seg.so_far(), self.seg.strict))
                self.tasks.add(t)
                t.add_done_callback(self.tasks.discard)
            if u is not None:
                t = asyncio.ensure_future(self._heard(u))
                self.tasks.add(t)
                t.add_done_callback(self.tasks.discard)

    def _early_due(self, pause_began: bool = False) -> bool:
        """Look for her name in the utterance still going? Only in phrase mode with a wake
        word, outside a summons (the next thing said is the request anyway), one look at a
        time, not once it was found: at a checkpoint, or the moment a pause begins."""
        import time
        g = self.gate
        if not self.wake_word or self.early_busy or self.seg.n in self.named:
            return False
        if g.mode == "continuous" and g.awake:
            return False
        if g.summoned_until and time.time() * 1000 <= g.summoned_until:
            return False
        if self.early_looks >= EARLY_MAX:
            return False
        at_mark = self.early_next < len(EARLY_AT_S) and self.seg.t - self.seg.start >= EARLY_AT_S[self.early_next]
        return pause_began or at_mark

    async def _early(self, n: int, audio: np.ndarray, strict: bool) -> None:
        """Her name at the start of what is still being said: duck the music and wait up to
        WAKE_HOLD_S for the rest."""
        import asyncio
        try:
            if audio.size < int(0.3 * RATE):
                return
            u = Utterance(audio, self.seg.start, self.seg.t, self.seg.t, strict, n, partial=True)
            async with self.lock:
                text = await asyncio.to_thread(transcribe, u, self.wake_word)
            vc = _voicecore()
            if not vc.starts_with(text, self.wake_word):
                self.seg.hold(n, END_SILENCE_S)          # not her name: end as usual
                return
            self.named.add(n)
            if vc.strip_prefix(text, self.wake_word):
                self.seg.hold(n, END_SILENCE_S)          # the request is under way: end as usual
            else:
                self.seg.hold(n, WAKE_HOLD_S, until_speech=True)   # her name alone: wait for it
            await self.send({"type": "action", "do": "wake_heard"})
        finally:
            self.early_busy = False

    async def _heard(self, u: Utterance) -> None:
        import asyncio
        import time
        async with self.lock:                      # one Whisper at a time per microphone
            text = await asyncio.to_thread(transcribe, u, self.wake_word)
            if u.n in self.named:
                self.named.discard(u.n)
                vc = _voicecore()
                if text and not vc.starts_with(text, self.wake_word):
                    # The early look heard her name; over music the whole utterance may not
                    # (the name was said before the duck). It was addressed to her.
                    text = f"{self.wake_word}, {text}"
            was = self.gate.awake
            msg, acts = self.gate.apply(text, time.time() * 1000)
            if text and self.gate.awake:
                self.gate.keep_open(time.time() * 1000)  # any speech keeps a conversation open
            ms = int((self.seg.t - u.end) * 1000)      # stream time: the end of speech to now
            await self.send({"type": "heard", "text": text, "send": msg, "strict": u.strict, "ms": ms})
            for a in acts:
                await self.send(dict(a, type="action"))
            if self.gate.awake != was:
                await self.send({"type": "awake", "on": self.gate.awake,
                                 "why": "wake word" if self.gate.awake else "sleep phrase"})

    async def tick(self) -> None:
        import time
        was = self.gate.awake
        for a in self.gate.tick(time.time() * 1000):
            await self.send(dict(a, type="action"))
        if was and not self.gate.awake:
            await self.send({"type": "awake", "on": False, "why": "idle timeout"})

    async def close(self) -> None:
        for t in list(self.tasks):
            t.cancel()
