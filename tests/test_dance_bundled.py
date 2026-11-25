"""#122: the dance clips that ship with Cantinero: CMU mocap, credited, copied in once."""
from __future__ import annotations

import json

import pytest

import main
from services import dance

CREDIT = ("The data used in this project was obtained from mocap.cs.cmu.edu. "
          "The database was created with funding from NSF EIA-0196217.")


def test_there_is_a_starter_set():
    names = {p.stem for p in dance.bundled()}
    assert {"the-twist", "cabbage-patch"} <= names and len(names) >= 3


@pytest.mark.parametrize("path", dance.bundled(), ids=lambda p: p.stem)
def test_every_bundled_clip_may_be_shared_and_is_credited(path):
    # CMU: "free for all uses ... you may include this data in commercially-sold products".
    # Anything else (Mixamo's, "local") must never be in the bundle (#121).
    d = json.loads(path.read_text(encoding="utf-8"))
    assert d["format"] == dance.FORMAT and d["id"] == path.stem and dance.ID.match(path.stem)
    assert d["license"] == "CMU-Mocap"
    assert d["credit"] == CREDIT
    assert d["source"].startswith("CMU mocap, ")
    assert d["bpm"] and d["bones"] and d["loop"][1] > d["loop"][0]


def test_bundled_clips_are_copied_in_once(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path)
    d = dance.folder()
    ids = {c["id"] for c in dance.clips()}
    assert {p.stem for p in dance.bundled()} <= ids
    twist = d / "the-twist.json"
    dance.set_tempo("the-twist", 101)                           # the user's tempo stays theirs
    (d / "cabbage-patch.json").unlink()                         # and a deleted one stays deleted
    dance.folder()
    assert json.loads(twist.read_text())["bpm"] == 101.0
    assert not (d / "cabbage-patch.json").exists()


def test_a_clip_added_to_the_bundle_later_reaches_an_existing_library(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", tmp_path / "data")
    seed = tmp_path / "seed"
    (seed / "dance").mkdir(parents=True)
    monkeypatch.setattr(main, "SEED", seed)
    dance.folder()
    assert not list((tmp_path / "data" / "dance").glob("*.json"))       # an empty bundle: nothing
    src = next(iter(sorted((main.BASE / "seed" / "dance").glob("*.json"))))
    (seed / "dance" / src.name).write_bytes(src.read_bytes())
    dance.folder()
    assert (tmp_path / "data" / "dance" / src.name).is_file()
