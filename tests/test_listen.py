"""The streaming listener (services/listen.py, #90). Silero is faked: a frame's "speech
probability" is its loudness, so the tests are about the rules, not the model."""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from services import listen

F = listen.FRAME


class LoudVad:
    """Speech is a frame with samples over 0.1."""

    def __call__(self, frame):
        return 0.9 if np.abs(frame).max() > 0.1 else 0.05


def frames(*spec):
    """("speech"|"quiet", seconds), ... -> 32 ms frames."""
    out = []
    for kind, s in spec:
        n = int(round(s * listen.RATE / F))
        out += [np.full(F, 0.3 if kind == "speech" else 0.01, np.float32)] * n
    return out


def run(seg, fs, audible=lambda i: False, turn=lambda i: False):
    return [u for i, f in enumerate(fs) if (u := seg.frame(f, audible(i), turn(i)))]


def seg(**kw):
    return listen.Segmenter(vad=LoudVad(), **kw)


def test_an_utterance_ends_after_half_a_second_of_quiet_and_keeps_the_pre_roll():
    us = run(seg(), frames(("quiet", 1.0), ("speech", 1.5), ("quiet", 1.0)))
    assert len(us) == 1
    u = us[0]
    assert u.start == pytest.approx(1.0 - listen.PRE_ROLL_S + 2 * F / listen.RATE, abs=0.05)   # the wake word's lead-in
    assert u.end == pytest.approx(2.5, abs=0.05)
    assert u.decided - u.end == pytest.approx(listen.END_SILENCE_S, abs=0.05)


def test_a_click_is_not_speech():
    assert run(seg(), frames(("quiet", 1), ("speech", 0.032), ("quiet", 1))) == []


def test_a_short_gap_does_not_split_a_sentence():
    assert len(run(seg(), frames(("speech", 1), ("quiet", 0.3), ("speech", 1), ("quiet", 1)))) == 1


def test_endless_speech_is_cut_at_the_limit():
    us = run(seg(), frames(("speech", listen.MAX_UTTERANCE_S + 2), ("quiet", 1)))
    assert us[0].end - us[0].start == pytest.approx(listen.MAX_UTTERANCE_S, abs=0.1)


def test_half_duplex_is_deaf_while_she_is_audible_and_strict_in_her_turn():
    fs = frames(("speech", 1), ("quiet", 1))
    assert run(seg(), fs, audible=lambda i: True) == []
    assert run(seg(full_duplex=True), fs, audible=lambda i: True)[0].strict is False
    assert run(seg(), fs, turn=lambda i: True)[0].strict is True


def _pcm(fs):
    return b"".join((np.clip(f, -1, 1) * 32767).astype("<i2").tobytes() for f in fs)


def test_a_session_sends_listening_heard_and_the_wake_gates_message(monkeypatch):
    monkeypatch.setattr(listen, "Vad", LoudVad)
    said = iter(["Mira, what time is it?", "the meeting moved to Thursday"])
    seen_wake = []

    def transcribe(u, wake_word=""):
        seen_wake.append(wake_word)
        return next(said)
    monkeypatch.setattr(listen, "transcribe", transcribe)
    out = []

    async def send(m):
        out.append(m)

    async def go():
        s = listen.Session(send)
        await s.message({"config": {"wake_word": "Mira", "wake_mode": "phrase"}})
        pcm = _pcm(frames(("speech", 1), ("quiet", 1), ("speech", 1), ("quiet", 1)))
        for i in range(0, len(pcm), 3000):                  # any chunking
            await s.audio(pcm[i:i + 3000])
        await asyncio.gather(*s.tasks)
    asyncio.run(go())
    heard = [m for m in out if m["type"] == "heard"]
    assert [m["send"] for m in heard] == ["what time is it?", ""]       # no wake word: dropped
    assert [m["on"] for m in out if m["type"] == "listening"] == [True, False, True, False]
    assert seen_wake == ["Mira", "Mira"] and all(m["ms"] >= 0 for m in heard)


def test_the_resident_streams_the_microphone_on_loopback_only(monkeypatch):
    from fastapi.testclient import TestClient

    from resident import service
    monkeypatch.setattr(listen, "Vad", LoudVad)
    monkeypatch.setattr(listen, "transcribe", lambda u, wake_word="": "Mira, next song")
    c = TestClient(service.app, base_url="http://127.0.0.1:7867")
    with c.websocket_connect("ws://127.0.0.1:7867/pmedia/listen") as ws:   # a full URL: the Host header
        ws.send_text(json.dumps({"config": {"wake_word": "Mira"}}))
        ws.send_bytes(_pcm(frames(("speech", 1), ("quiet", 1))))
        got = [ws.receive_json() for _ in range(3)]
    assert got[-1]["type"] == "heard" and got[-1]["send"] == "next song"
    lan = TestClient(service.app, base_url="http://192.168.1.50:7867")
    with pytest.raises(Exception):
        with lan.websocket_connect("ws://192.168.1.50:7867/pmedia/listen") as ws:
            ws.receive_json()


def test_a_continuous_conversation_opens_and_closes_with_awake_events(monkeypatch):
    monkeypatch.setattr(listen, "Vad", LoudVad)
    said = iter(["Mira, hello", "what's on tonight", "that will be all"])
    monkeypatch.setattr(listen, "transcribe", lambda u, wake_word="": next(said))
    out = []

    async def send(m):
        out.append(m)

    async def go():
        s = listen.Session(send)
        await s.message({"config": {"wake_word": "Mira", "wake_mode": "continuous", "sleep_phrase": "that will be all"}})
        await s.audio(_pcm(frames(*[("speech", 1), ("quiet", 1)] * 3)))
        await asyncio.gather(*s.tasks)
    asyncio.run(go())
    assert [(m["on"], m["why"]) for m in out if m["type"] == "awake"] == [(True, "wake word"), (False, "sleep phrase")]
    assert [m["send"] for m in out if m["type"] == "heard"] == ["hello", "what's on tonight", ""]
