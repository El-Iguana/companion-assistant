"""Build the synthetic hands-free test set (#89): scenes of a room, each with its ground truth.

Run inside the image (it needs Kokoro, numpy, PyAV and the user's Orpheus pairing for music):

    podman cp eval companion-assistant:/app/eval
    podman exec -w /app companion-assistant python -m eval.listen.make_scenes [--user <uid>]

Writes eval/listen/scenes/<name>.wav (16 kHz mono float -> int16) and <name>.json (events).
Deterministic for a given library (seeded). Levels come from predator's own log
(2026-11-24): the room idles at peaks of 0.008-0.019 against the pages' 0.015 gate.

Events: {"kind": "wake"|"speech"|"her"|"noise", "start", "end" (s), "text", "command"
(what should be sent, for wake), "voice"}. "her" events carry "audible_until" (playback end +
the pages' 0.4 s hangover) and mark the span the pages treat as her turn.
"""
from __future__ import annotations

import argparse
import io
import json
import random
import wave
from pathlib import Path

import numpy as np

RATE = 16000
HERE = Path(__file__).resolve().parent
OUT = HERE / "scenes"
SEED = 2026

USER_VOICES = ["am_adam", "am_michael", "bm_george", "af_bella", "bf_emma", "am_eric"]
HER_VOICE = "af_sarah"                       # Mira's voice: her echo
WAKE = "Mira"
COMMANDS = [
    "what time is it", "play some blues", "pause the music", "turn it down a bit",
    "what's the weather like tomorrow", "remind me to call my mother at five", "next song",
    "play the album with Ants Marching", "how long until dinner is ready", "stop the music",
    "add milk to the shopping list", "who sings this song",
]
CHATTER = [
    "I think we should order pizza tonight.", "Did you feed the cat this morning?",
    "The meeting moved to Thursday afternoon.", "Can you pass me the remote?",
    "That movie was a lot longer than I expected.", "We're almost out of coffee again.",
]
HER_LINES = [
    "It's a quarter past three in the afternoon.", "Sure, I'll remind you at five o'clock.",
    "Tomorrow looks sunny with a high of seventy two degrees.",
    "I added milk to your shopping list.", "Dinner should be ready in about twenty minutes.",
    "That's Back in Black by AC/DC, from nineteen eighty.",
]

# Levels (RMS of the float signal, the pages' scale)
SPEECH_RMS = 0.06            # the user, a couple of metres from the mic
CHATTER_RMS = 0.04           # someone else in the room
HER_ECHO_RMS = 0.03          # her voice back through the room
QUIET_FLOOR = 0.004          # room tone; peaks reach ~0.012
NOISY_FLOOR = 0.008          # fan, traffic; peaks over the gate
MUSIC_RMS = 0.03             # music playing in the room


def _rms(x):
    return float(np.sqrt(np.mean(np.square(x)))) if x.size else 0.0


def _at(x, rms):
    r = _rms(x)
    return x * (rms / r) if r > 0 else x


def _wav_to_float(data: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(data)) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    return _resample(pcm, rate)


def _resample(x, rate):
    if rate == RATE or not x.size:
        return x.astype(np.float32)
    n = int(round(x.size * RATE / rate))
    return np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype(np.float32)


def say(text, voice):
    from services import tts
    return _wav_to_float(tts.synth_wav(text, voice=voice))


def room_tone(n, floor, rng):
    """Pink-ish noise with a slow wobble: room tone, a fan."""
    white = rng.standard_normal(n).astype(np.float32)
    pink = np.convolve(white, np.ones(16) / 16, mode="same")
    wobble = 1 + 0.4 * np.sin(np.linspace(0, rng.uniform(3, 9) * np.pi, n))
    return _at(pink * wobble, floor)


def knocks(n, rng, count, level=0.25):
    """Door knocks, a dropped spoon, typing bursts: short loud transients."""
    x = np.zeros(n, np.float32)
    for _ in range(count):
        at = rng.integers(0, max(1, n - RATE))
        dur = int(rng.uniform(0.02, 0.25) * RATE)
        burst = rng.standard_normal(dur).astype(np.float32) * np.exp(-np.linspace(0, 6, dur))
        x[at:at + dur] += burst * level
    return x


def reverb(x, rng, seconds=0.35):
    """A room: the dry sound plus a decaying noise tail."""
    k = int(seconds * RATE)
    ir = rng.standard_normal(k).astype(np.float32) * np.exp(-np.linspace(0, 7, k))
    ir[0] = 1.0
    y = np.convolve(x, ir)[: x.size + k]
    return y / (np.abs(ir).sum() ** 0.5)


def music(user_root: Path, n: int, which: int) -> np.ndarray:
    """A stretch of a real track from the user's Orpheus library, or synthetic chords if
    the library can't be reached."""
    try:
        import asyncio

        import av

        from services.skills import orpheus_media as om
        st = om.load(user_root)
        names = ["Back in Black", "Boom Boom", "September"]

        async def fetch():
            got = await om.resolve(st["url"], st["token"], names[which % 3], ["track"])
            item = (got.get("candidates") or [])[0]["item_id"]
            dec = await om._request("POST", f"{st['url']}/api/v1/items/{item}/playback", token=st["token"], json={})
            import httpx
            url = dec["url"] if dec["url"].startswith("http") else st["url"] + dec["url"]
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.get(url, headers={"Authorization": f"Bearer {st['token']}"})
                return r.content
        data = asyncio.run(fetch())
        frames = []
        with av.open(io.BytesIO(data)) as f:
            res = av.AudioResampler(format="flt", layout="mono", rate=RATE)
            for fr in f.decode(audio=0):
                for o in res.resample(fr):
                    frames.append(o.to_ndarray().reshape(-1))
                if sum(len(a) for a in frames) > RATE * 60 + n:
                    break
        x = np.concatenate(frames)[RATE * 30:]            # past the intro
        return np.resize(x, n).astype(np.float32)
    except Exception as e:                                # noqa: BLE001
        print(f"  (no library music: {e}; synthetic instead)")
        t = np.arange(n) / RATE
        chord = sum(np.sin(2 * np.pi * f * t) for f in (220, 277, 330))
        beat = (np.sin(2 * np.pi * 2 * t) > 0.95).astype(np.float32) * np.random.default_rng(which).standard_normal(n)
        return (chord * 0.2 + beat).astype(np.float32)


class Scene:
    def __init__(self, seconds, rng):
        self.x = np.zeros(int(seconds * RATE), np.float32)
        self.events: list[dict] = []
        self.rng = rng

    def add(self, sig, at_s, kind, **meta):
        a = int(at_s * RATE)
        b = min(self.x.size, a + sig.size)
        self.x[a:b] += sig[: b - a]
        voiced = np.where(np.abs(sig[: b - a]) > 0.02 * np.abs(sig).max())[0]
        start = at_s + (voiced[0] / RATE if voiced.size else 0)
        end = at_s + (voiced[-1] / RATE if voiced.size else (b - a) / RATE)
        ev = dict(kind=kind, start=round(start, 3), end=round(end, 3), **meta)
        if kind == "her":
            ev["audible_until"] = round(at_s + (b - a) / RATE + 0.4, 3)
        self.events.append(ev)

    def save(self, name):
        OUT.mkdir(parents=True, exist_ok=True)
        pcm = (np.clip(self.x, -1, 1) * 32767).astype("<i2")
        with wave.open(str(OUT / f"{name}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm.tobytes())
        (OUT / f"{name}.json").write_text(json.dumps({"seconds": self.x.size / RATE, "events": self.events}, indent=1))
        print(f"  {name}: {self.x.size / RATE:.0f} s, {len(self.events)} events")


def build(user_root: Path):
    rng = np.random.default_rng(SEED)
    pick = random.Random(SEED)
    cmds = COMMANDS[:]
    pick.shuffle(cmds)
    cmd_iter = iter(cmds * 4)

    def wake(scene, at, voice):
        c = next(cmd_iter)
        sig = _at(say(f"{WAKE}, {c}.", voice), SPEECH_RMS)
        scene.add(sig, at, "wake", text=f"{WAKE}, {c}.", command=c, voice=voice)
        return at + sig.size / RATE

    def chatter(scene, at, voice):
        line = pick.choice(CHATTER)
        sig = _at(say(line, voice), CHATTER_RMS)
        scene.add(sig, at, "speech", text=line, voice=voice)
        return at + sig.size / RATE

    def her(scene, at):
        line = pick.choice(HER_LINES)
        sig = _at(reverb(say(line, HER_VOICE), rng), HER_ECHO_RMS)
        scene.add(sig, at, "her", text=line, voice=HER_VOICE)
        return at + sig.size / RATE

    def bed(scene, floor, knock_count=0, mus=None):
        n = scene.x.size
        scene.x += room_tone(n, floor, rng)
        if knock_count:
            scene.x += knocks(n, rng, knock_count)
        if mus is not None:
            scene.x += _at(mus, MUSIC_RMS)

    print("building scenes ->", OUT)
    # 1-2. A quiet and a noisy room, people talking, a few wake commands.
    for name, floor, k in (("quiet_room", QUIET_FLOOR, 2), ("noisy_room", NOISY_FLOOR, 10)):
        s = Scene(75, rng)
        bed(s, floor, k)
        t = 3.0
        for i in range(5):
            t = wake(s, t, USER_VOICES[i % len(USER_VOICES)]) + pick.uniform(4, 7)
            t = chatter(s, t, USER_VOICES[(i + 3) % len(USER_VOICES)]) + pick.uniform(3, 6)
        s.save(name)
    # 3-5. Music playing in the room.
    for i in range(3):
        s = Scene(60, rng)
        bed(s, QUIET_FLOOR, 2, music(user_root, s.x.size, i))
        t = 4.0
        for j in range(4):
            t = wake(s, t, USER_VOICES[(i + j) % len(USER_VOICES)]) + pick.uniform(7, 10)
        s.save(f"music_{i}")
    # 6. A conversation with her: commands, and her replies coming back through the room.
    s = Scene(90, rng)
    bed(s, QUIET_FLOOR, 2)
    t = 3.0
    for i in range(6):
        t = wake(s, t, USER_VOICES[i % len(USER_VOICES)]) + pick.uniform(0.8, 1.5)
        t = her(s, t) + pick.uniform(3, 5)
    s.save("her_replies")
    # 7-8. Nobody talking to her: noise and knocks; music alone. Every send here is false.
    s = Scene(90, rng)
    bed(s, NOISY_FLOOR, 25)
    t = 5.0
    for i in range(6):
        t = chatter(s, t, USER_VOICES[i % len(USER_VOICES)]) + pick.uniform(8, 12)
    s.save("no_wake_noise")
    s = Scene(90, rng)
    bed(s, QUIET_FLOOR, 3, music(user_root, s.x.size, 0))
    s.save("no_wake_music")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="", help="library id (data/u/<id>) for music; default: the only one")
    a = ap.parse_args()
    users = sorted((Path("/app/data/u")).glob("*"))
    root = Path("/app/data/u") / a.user if a.user else (users[0] if users else Path("."))
    build(root)
