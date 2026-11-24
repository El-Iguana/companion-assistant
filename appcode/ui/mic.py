"""The hands-free microphone's brain in the page (#5): ``ui/voicecore.py``'s rules, plugged
into ``static/js/voice.js``.

voice.js still captures the microphone (the audio thread is no place for Python), plays
her voice and moves her mouth. With a brain plugged in (``PantheonVoice.useBrain``), each
capture block — its level, measured in JS — comes here, and the decisions are made here:
when speech starts and stops, interrupting her, the strict flag, the wake word and the
conversation, the self-hearing check, the WAV sent to Whisper. voice.js gives the brain a
small set of operations back (``ops``): transcribe, send, abort, say, toast, persist,
flags. Measured 2026-11-08 (Pyodide 0.29.3): 0.003 ms a block with the level from JS,
against 85 ms between blocks.

The desktop window has no Pyodide; there voice.js keeps its own copy of these rules.
"""
from __future__ import annotations

import asyncio
import json

import js
from pyodide.ffi import create_proxy, to_js

from ui.voicecore import Segmenter, WakeGate, looks_self_heard, wav_bytes


def _floats(block):
    """A JS Float32Array's samples as one memoryview of floats (one copy, not one FFI
    call per sample)."""
    mv = block.to_py()
    return mv if mv.format == "f" else mv.cast("B").cast("f")


class Brain:
    def __init__(self, ops) -> None:
        self.ops = ops                  # voice.js's operations (a JS object)
        self.seg = Segmenter()
        self.gate = WakeGate()
        self.full_duplex = False
        self.self_heard = 0
        self.rate = 48000

    # --- called by voice.js -------------------------------------------------------
    def configure(self, cfg_json: str) -> None:
        cfg = json.loads(str(cfg_json or "{}"))
        self.seg.configure(cfg)
        self.gate.configure(cfg)
        self.full_duplex = bool(cfg.get("full_duplex"))
        self.rate = int(cfg.get("sample_rate") or self.rate)

    def block(self, rms, block, now, audible, her_turn, busy) -> None:
        for act in self.seg.block(float(rms), block, float(now), bool(audible),
                                  bool(her_turn), bool(busy)):
            self._do(act)

    def tick(self, now) -> None:
        for act in self.gate.tick(float(now)):
            self._do(act)

    def reset(self) -> None:
        """The mic went off: forget the utterance in progress and close the conversation
        (no farewell — that would be talking to an empty room)."""
        self.seg.reset()
        for act in self.gate.set_awake(False, "mic off", float(js.Date.now())):
            self._do(act)
        self._flags(False)

    def is_awake(self) -> bool:
        return self.gate.awake

    # --- doing what the rules decided ----------------------------------------------
    def _flags(self, listening: bool | None = None) -> None:
        self.ops.flags(self.gate.awake, bool(self.seg.speaking if listening is None else listening))

    def _do(self, act: dict) -> None:
        kind = act["do"]
        if kind == "listening":
            self._flags(act["on"])
        elif kind == "abort":
            self.ops.abortNow()
        elif kind == "say":
            self.ops.say(act["text"])
        elif kind == "toast":
            self.ops.toast(act["msg"], act.get("kind", "info"))
        elif kind == "paint":
            self._flags()
        elif kind == "summoned":
            self.ops.summoned()
        elif kind == "utterance":
            asyncio.ensure_future(self._utterance(act["utterance"]))

    async def _utterance(self, u) -> None:
        try:
            await self._handle(u)
        except Exception as exc:  # noqa: BLE001 - one lost utterance, never a dead mic
            js.console.error(f"[mic] utterance failed: {exc}")

    async def _handle(self, u) -> None:
        if not u.blocks or not self.ops.active():
            return
        audio = wav_bytes(u.blocks, self.rate, _floats)
        raw = str(await self.ops.transcribe(to_js(audio), u.strict) or "").strip()
        js.console.log("[mic] transcript" + (" (strict)" if u.strict else "") + ": " + json.dumps(raw))
        self.gate.keep_open(float(js.Date.now()))
        if not raw:
            return
        spoken = json.loads(str(self.ops.spoken()))
        if looks_self_heard(raw, spoken, float(js.performance.now()), self.full_duplex):
            self.self_heard += 1                # one may be coincidence; two is a fault
            if self.self_heard >= 2:
                self.self_heard = 0
                self.full_duplex = self.seg.full_duplex = False
                self.ops.persist(json.dumps({"full_duplex": False}))
                self.ops.toast("She was hearing herself — interruption turned off. Use headphones, "
                               "or run scripts/setup-aec.sh --persist and pick aec_source in "
                               "Settings.", "warning")
            return                              # never answer her own voice
        self.self_heard = 0
        msg, acts = self.gate.apply(raw, float(js.Date.now()))
        for act in acts:
            self._do(act)
        if not msg:
            return
        # Held back at the energy gate; the transcript cleared both checks, so this is a
        # real interruption.
        if u.pending_barge_in:
            await self.ops.abortAndWait()
        if not self.ops.streaming():
            self.ops.send(msg)


_keep: list = []                                # proxies kept alive for the page's lifetime
_plugged: list = []


def install() -> None:
    """Plug the brain into voice.js once it has loaded (page-wide, like voice.js)."""
    def plug(*_a) -> None:
        voice = getattr(js.window, "PantheonVoice", None)
        if voice is None or _plugged or not hasattr(voice, "useBrain"):
            return
        brain = Brain(voice.brainOps())
        proxy = create_proxy(brain)
        _keep.append(proxy)
        _plugged.append(brain)
        voice.useBrain(proxy)
        js.console.log("[mic] hands-free decisions run in Python (ui/voicecore.py)")

    if getattr(js.window, "PantheonVoice", None) is not None:
        plug()
    else:
        listener = create_proxy(plug)
        _keep.append(listener)
        js.window.addEventListener("pvoice:ready", listener)
