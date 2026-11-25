"""#114: each music track's BPM, known ahead of time; never for spoken word."""
from __future__ import annotations

import asyncio
import io

import numpy as np
import pytest
import soundfile as sf

from services.skills import music_catalog, music_tempo as mt
from services.skills import orpheus_media as om

SR = 22050


def _clicks(bpm: float, seconds: float = 40.0, sr: int = SR) -> bytes:
    """A drum-like click track at ``bpm`` as WAV bytes: a short decaying noise burst per beat,
    a quieter one on the off-beat, over faint noise."""
    rng = np.random.default_rng(7)
    x = rng.normal(0, 0.01, int(seconds * sr)).astype("float32")
    burst = (rng.normal(0, 1, int(0.03 * sr)) * np.exp(-np.linspace(0, 8, int(0.03 * sr)))).astype("float32")
    period = 60.0 / bpm
    t = 0.0
    n = len(burst)
    while t + period < seconds:
        i, j = int(t * sr), int((t + period / 2) * sr)
        x[i:i + n] += 0.8 * burst
        x[j:j + n] += 0.25 * burst
        t += period
    buf = io.BytesIO()
    sf.write(buf, x, sr, format="WAV")
    return buf.getvalue()


@pytest.mark.parametrize("bpm", [90, 100, 120, 128, 145])
def test_estimate_finds_the_tempo(bpm):
    got = mt.estimate(_clicks(bpm))
    assert got is not None and abs(got - bpm) <= 2.5, got


def test_slow_and_fast_readings_fold_into_the_dancing_range():
    assert mt.fold(70) == 140 and mt.fold(40) == 80 and mt.fold(280) == 140 and mt.fold(200) == 100
    assert mt.fold(120) == 120 and mt.fold(160) == 160 and mt.fold(0) is None and mt.fold(None) is None
    assert abs(mt.estimate(_clicks(66)) - 132) <= 3.0          # a slow song dances at double time


def test_too_short_or_silent_has_no_tempo():
    assert mt.estimate(_clicks(120, seconds=5)) is None
    buf = io.BytesIO()
    sf.write(buf, np.zeros(SR * 30, dtype="float32"), SR, format="WAV")
    assert mt.estimate(buf.getvalue()) is None
    assert mt.measure(b"not audio at all")["bpm"] is None


def test_a_bpm_tag_wins_over_measuring(tmp_path):
    from mutagen.flac import FLAC

    x, sr = sf.read(io.BytesIO(_clicks(120)), dtype="float32")
    path = tmp_path / "t.flac"
    sf.write(path, x, sr, format="FLAC")
    f = FLAC(path)
    f["BPM"] = "174"
    f.save()
    data = path.read_bytes()
    assert mt.tag_bpm(data) == 87.0                            # the tag, folded
    assert mt.measure(data) == {"bpm": 87.0, "from": "tag"}
    assert mt.tag_bpm(_clicks(120)) is None
    rec = mt.measure(_clicks(120))
    assert rec["from"] == "audio" and abs(rec["bpm"] - 120) <= 2.5


def _library(tmp_path, monkeypatch, fetched):
    """Two albums: music (tracks 11, 12) and an audiobook (21), and the files behind them."""
    music_catalog._save(tmp_path, {"built_at": 1e12, "albums": [
        {"id": 1, "title": "Greatest Hits", "artist": "Pink", "spoken": False},
        {"id": 2, "title": "The Hobbit", "artist": "Tolkien", "spoken": True}]})
    tracks = {1: [11, 12], 2: [21]}

    async def expand(base, token, *, item_id=None, playlist_id=""):
        return [{"id": t, "title": f"t{t}"} for t in tracks[item_id]]

    async def fetch(st, item_id):
        fetched.append(item_id)
        return {11: _clicks(128), 12: b"broken"}.get(item_id)
    monkeypatch.setattr(om, "expand", expand)
    monkeypatch.setattr(mt, "_fetch", fetch)
    return tracks


def test_build_measures_music_only_once_and_never_spoken_word(tmp_path, monkeypatch):
    fetched = []
    tracks = _library(tmp_path, monkeypatch, fetched)
    st = {"url": "http://o", "token": "t"}
    asyncio.run(mt.build(tmp_path, st))
    assert sorted(fetched) == [11, 12]                         # the audiobook's track never fetched
    known = mt.bpm_of(tmp_path, [11, 12, 21])
    assert list(known) == [11] and abs(known[11] - 128) <= 2.5
    asyncio.run(mt.build(tmp_path, st))
    assert sorted(fetched) == [11, 12]                         # measured once, failures included
    tracks[1] = [12]                                           # 11 left the library
    asyncio.run(mt.build(tmp_path, st))
    assert "11" not in mt.load(tmp_path)["tracks"]


def test_an_album_marked_spoken_later_loses_its_tempos(tmp_path, monkeypatch):
    fetched = []
    _library(tmp_path, monkeypatch, fetched)
    st = {"url": "http://o", "token": "t"}
    asyncio.run(mt.build(tmp_path, st))
    cat = music_catalog.load(tmp_path)
    cat["albums"][0]["spoken"] = True
    music_catalog._save(tmp_path, cat)
    asyncio.run(mt.build(tmp_path, st))
    assert mt.bpm_of(tmp_path, [11]) == {}


def test_a_server_that_is_down_stops_the_build_without_marking_tracks(tmp_path, monkeypatch):
    fetched = []
    _library(tmp_path, monkeypatch, fetched)

    async def down(*a, **kw):
        raise om.OrpheusError("SERVER_UNREACHABLE", "down")
    monkeypatch.setattr(om, "expand", down)
    with pytest.raises(om.OrpheusError):
        asyncio.run(mt.build(tmp_path, {"url": "http://o", "token": "t"}))
    assert not (mt.load(tmp_path).get("tracks"))


def test_queue_entries_carry_the_bpm_where_known(tmp_path):
    mt._save(tmp_path, {"built_at": 1e12, "tracks": {"11": {"bpm": 128.0}, "12": {"bpm": None, "failed": "x"}}})
    q = mt.annotate(tmp_path, [{"id": 11}, {"id": 12}, {"id": 21}])
    assert q == [{"id": 11, "bpm": 128.0}, {"id": 12}, {"id": 21}]


def test_the_lookup_route_answers_known_tempos(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from pantheon_web import media as pmedia
    import service

    class Cur:
        class cards:
            parent = tmp_path
    monkeypatch.setattr(pmedia, "_ctx", lambda request, resident=False: Cur)
    mt._save(tmp_path, {"built_at": 1e12, "tracks": {"11": {"bpm": 128.0}}})
    c = TestClient(service.app, base_url="http://127.0.0.1:7867")
    r = c.get("/pmedia/music/tempo?ids=11,21,x")
    assert r.status_code == 200 and r.json() == {"tempo": {"11": 128.0}}


def test_what_libsndfile_cant_open_is_decoded_by_pyav(monkeypatch):
    import soundfile

    real, data = soundfile.SoundFile, _clicks(120)

    def refuse(*a, **kw):
        raise soundfile.LibsndfileError(0, "Format not recognised.")
    monkeypatch.setattr(soundfile, "SoundFile", refuse)
    got = mt.estimate(data)                                    # PyAV reads the WAV instead
    monkeypatch.setattr(soundfile, "SoundFile", real)
    assert got is not None and abs(got - 120) <= 2.5
