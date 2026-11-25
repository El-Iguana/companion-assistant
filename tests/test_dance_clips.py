"""#120: the server lists and serves dance clips from data/dance (services/dance.py)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from services import dance


def _clip(**over):
    d = {"format": "cantinero-dance/1", "id": "x", "name": "X", "style": "party", "bpm": 100.0, "energy": 0.4,
         "fps": 30, "frames": 2, "loop": [0, 1], "license": "CMU-Mocap",
         "bones": {"hips": [0, 0, 0, 1, 0, 0, 0, 1]}, "hips": [0] * 6}
    d.update(over)
    return d


@pytest.fixture()
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(dance, "folder", lambda: tmp_path)
    (tmp_path / "macarena.json").write_text(json.dumps(_clip(name="Macarena", bpm=81.2)))
    (tmp_path / "the-twist.json").write_text(json.dumps(_clip(name="The Twist", bpm=89.7, energy=0.96)))
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "other.json").write_text(json.dumps({"format": "something-else"}))
    (tmp_path / "Bad Name.json").write_text(json.dumps(_clip()))
    return tmp_path


def test_only_good_clips_are_listed(folder):
    got = dance.clips()
    assert [(c["id"], c["name"], c["bpm"]) for c in got] == [("macarena", "Macarena", 81.2),
                                                             ("the-twist", "The Twist", 89.7)]
    assert got[1]["energy"] == 0.96 and got[0]["license"] == "CMU-Mocap"


def test_a_clip_is_found_only_by_a_safe_id(folder):
    assert dance.path_of("macarena") == folder / "macarena.json"
    assert dance.path_of("broken") is None and dance.path_of("other") is None
    for bad in ("../macarena", "Macarena", "", "a/b", "macarena.json"):
        assert dance.path_of(bad) is None


def test_no_folder_no_clips(tmp_path, monkeypatch):
    monkeypatch.setattr(dance, "folder", lambda: tmp_path / "missing")
    assert dance.clips() == []


def test_the_routes(folder, monkeypatch):
    from pantheon_web import media as pmedia
    import service

    monkeypatch.setattr(pmedia, "_ctx", lambda request, resident=False: object())
    c = TestClient(service.app, base_url="http://127.0.0.1:7867")
    assert [x["id"] for x in c.get("/pmedia/dance").json()["clips"]] == ["macarena", "the-twist"]
    r = c.get("/pmedia/dance/macarena.json")
    assert r.status_code == 200 and r.json()["name"] == "Macarena"
    assert c.get("/pmedia/dance/broken.json").status_code == 404
    assert c.get("/pmedia/dance/nope.json").status_code == 404


def test_a_tempo_set_by_hand_is_saved_in_the_clip(folder):
    import math

    # A clip whose arm swings and stops each beat at 120 BPM (15 frames), landing at frame 6.
    frames = 120
    q = []
    for t in range(frames):
        u = ((t - 6) % 15) / 15
        m = min(1.0, max(0.0, (u - 0.4) / 0.6))
        a = 0.5 * (1 - 2 * m * m * (3 - 2 * m)) * (1 if ((t - 6) // 15) % 2 == 0 else -1)
        q += [0.0, 0.0, math.sin(a / 2), math.cos(a / 2)]
    (folder / "swing.json").write_text(json.dumps(_clip(name="Swing", bpm=180.0, fps=30, frames=frames,
                                                        bones={"leftUpperArm": q}, beat_frame=0)))
    got = dance.set_tempo("swing", 120)
    assert got["bpm"] == 120.0
    d = json.loads((folder / "swing.json").read_text())
    assert d["bpm"] == 120.0 and d["bpm_from"] == "set by hand" and abs(d["beat_frame"] - 6) <= 1
    assert dance.set_tempo("swing", 999)["bpm"] == 240.0                # kept in range
    with pytest.raises(KeyError):
        dance.set_tempo("nope", 100)


def test_the_tempo_route(folder, monkeypatch):
    from pantheon_web import media as pmedia
    import service

    monkeypatch.setattr(pmedia, "_ctx", lambda request, resident=False: object())
    monkeypatch.setattr(pmedia, "_require_csrf", lambda request: None)
    c = TestClient(service.app, base_url="http://127.0.0.1:7867")
    r = c.post("/pmedia/dance/macarena/tempo", json={"bpm": 104})
    assert r.status_code == 200 and r.json()["clip"]["bpm"] == 104.0
    assert c.post("/pmedia/dance/macarena/tempo", json={}).status_code == 400
    assert c.post("/pmedia/dance/nope/tempo", json={"bpm": 100}).status_code == 404
