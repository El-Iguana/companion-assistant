"""Score hands-free listening pipelines on the synthetic scenes (#89, #90).

    podman exec -w /app companion-assistant python -m eval.listen.run [energy] [silero] [--json out.json]

A pipeline gets a scene's audio and its "her" spans and returns the utterances it would send
to Whisper (start, end, the time it decided the utterance was over, strict). Each is then
transcribed by the same Whisper the app uses and put through the same wake gate
(voicecore.WakeGate, phrase mode, wake word "Mira").

Measured per pipeline:
- wake commands sent with the right words (recall), missed, garbled;
- false sends: messages sent that no wake command asked for;
- Whisper calls, and those on no speech at all (noise, music, knocks);
- delay: the end of the wake command to the message being decided (the segmenter's wait
  plus Whisper's time on this machine).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "appcode" / "ui"))
import voicecore  # noqa: E402

RATE = 16000
BLOCK = 1365                 # the pages' 4096 frames at 48 kHz, ~85 ms
HER_TURN_BEFORE_S = 1.0      # the reply streams a moment before her first line plays
HER_TURN_AFTER_S = 1.1       # the pages' 1.5 s after she stops (the 0.4 s hangover is in audible_until)


def load(name):
    with wave.open(str(HERE / "scenes" / f"{name}.wav")) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    return x, json.loads((HERE / "scenes" / f"{name}.json").read_text())


def her_flags(events):
    """(audible(t), her_turn(t)) from the scene's "her" events, as the pages set them."""
    spans = [(e["start"], e["audible_until"]) for e in events if e["kind"] == "her"]

    def audible(t):
        return any(a <= t <= b for a, b in spans)

    def her_turn(t):
        return any(a - HER_TURN_BEFORE_S <= t <= b + HER_TURN_AFTER_S for a, b in spans)
    return audible, her_turn


# --- pipelines -----------------------------------------------------------------------------------

def energy(x, events):
    """Today: voicecore.Segmenter on ~85 ms blocks (threshold 0.015, 800 ms of silence)."""
    seg = voicecore.Segmenter()
    audible, her_turn = her_flags(events)
    out, start = [], None
    for i in range(0, x.size - BLOCK + 1, BLOCK):
        b = x[i:i + BLOCK]
        t = (i + BLOCK) / RATE
        rms = float(np.sqrt(np.mean(b * b)))
        for act in seg.block(rms, b, t * 1000, audible(t), her_turn(t), False):
            if act["do"] == "listening" and act["on"]:
                start = t
            elif act["do"] == "utterance":
                u = act["utterance"]
                audio = np.concatenate(u.blocks)
                out.append({"start": start, "end": t, "decided": t, "audio": audio, "strict": u.strict})
    return out


PIPELINES = {"energy": energy}


def silero(x, events):          # filled in by V2 (#90): services/listen.py
    from services import listen
    return listen.eval_pipeline(x, events, her_flags(events))


PIPELINES["silero"] = silero


# --- scoring ---------------------------------------------------------------------------------------

def _words(t):
    return set(voicecore.norm_phrase(t).split())


def score(name, pipeline):
    from services import stt
    x, meta = load(name)
    events = meta["events"]
    gate = voicecore.WakeGate()
    gate.configure({"wake_word": "Mira", "wake_mode": "phrase"})
    utts = pipeline(x, events)
    rows = []
    for u in utts:
        audio = u.pop("audio")
        wav = voicecore.wav_bytes([audio], RATE, lambda b: b.tolist())
        t0 = time.monotonic()
        text = stt.transcribe(wav, strict=u["strict"])
        u["whisper_s"] = time.monotonic() - t0
        msg, _ = gate.apply(text, u["end"] * 1000)
        u.update(text=text, sent=msg)
        u["speech"] = any(e["kind"] in ("wake", "speech", "her") and e["start"] < u["end"] and u["start"] < e["end"]
                          for e in events)
        rows.append(u)
    res = {"scene": name, "wake": 0, "hit": 0, "garbled": 0, "missed": [], "false_sends": [],
           "whisper": len(rows), "whisper_no_speech": sum(1 for r in rows if not r["speech"]), "delays": []}
    used = set()
    for e in (e for e in events if e["kind"] == "wake"):
        res["wake"] += 1
        best = None
        for i, r in enumerate(rows):
            if i in used or not r["sent"]:
                continue
            if r["start"] - 0.5 <= e["end"] and r["end"] >= e["start"]:
                best = i
                break
        if best is None:
            res["missed"].append(e["command"])
            continue
        used.add(best)
        r = rows[best]
        want = _words(e["command"])
        if len(want & _words(r["sent"])) >= 0.6 * len(want):
            res["hit"] += 1
        else:
            res["garbled"] += 1
        res["delays"].append(r["decided"] - e["end"] + r["whisper_s"])
    res["false_sends"] = [r["sent"] for i, r in enumerate(rows) if r["sent"] and i not in used]
    res["rows"] = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in rows]
    return res


def summary(results):
    tot = {k: sum(r[k] for r in results) for k in ("wake", "hit", "garbled", "whisper", "whisper_no_speech")}
    tot["missed"] = sum(len(r["missed"]) for r in results)
    tot["false_sends"] = sum(len(r["false_sends"]) for r in results)
    d = [x for r in results for x in r["delays"]]
    tot["delay_median_s"] = round(statistics.median(d), 2) if d else None
    tot["delay_p90_s"] = round(sorted(d)[int(0.9 * (len(d) - 1))], 2) if d else None
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pipelines", nargs="*", default=["energy"])
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    names = sorted(p.stem for p in (HERE / "scenes").glob("*.wav"))
    report = {}
    for p in a.pipelines:
        results = [score(n, PIPELINES[p]) for n in names]
        report[p] = {"total": summary(results), "scenes": results}
        print(f"\n== {p} ==")
        print(f"{'scene':16s} wake hit garb miss false whisper(no-speech) delay")
        for r in results:
            d = statistics.median(r["delays"]) if r["delays"] else float("nan")
            print(f"{r['scene']:16s} {r['wake']:4d} {r['hit']:3d} {r['garbled']:4d} {len(r['missed']):4d} "
                  f"{len(r['false_sends']):5d} {r['whisper']:7d}({r['whisper_no_speech']:2d})  {d:5.2f}s")
        print("total:", json.dumps(report[p]["total"]))
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
