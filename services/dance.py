"""Dance clips (#120): the motion the avatar dances to music with, in ``data/dance/``.

Each clip is one ``<id>.json`` made by ``scripts/dance/convert.py`` (format
``cantinero-dance/1``): VRM humanoid bone rotations, the hips' sway, the clip's own tempo and
a loop of whole bars. The page lists them (``GET /pmedia/dance``), fetches the ones it plays
(``GET /pmedia/dance/<id>.json``) and matches each to the song's tempo. With none here, she
dances the generated way (static/js/vtuber.mjs, #115).

The folder is the library's (the data mount, like ``data/vrm/``). Clips the user adds for
themselves (Mixamo, #121) live here too and are never in the repo; bundled ones (#122) carry
a licence that allows sharing them.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

FORMAT = "cantinero-dance/1"
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_cache: dict = {}                       # path -> (mtime, summary)


def folder() -> Path:
    import main

    return main.DATA / "dance"


def _summary(p: Path) -> dict | None:
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    hit = _cache.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("format") != FORMAT or not d.get("bpm") or not d.get("bones"):
        return None
    s = {"id": p.stem, "name": str(d.get("name") or p.stem), "style": str(d.get("style") or ""),
         "bpm": float(d["bpm"]), "energy": float(d.get("energy") or 0.5),
         "license": str(d.get("license") or ""), "bytes": p.stat().st_size}
    _cache[str(p)] = (mtime, s)
    return s


def clips() -> list[dict]:
    """Every usable clip, by name."""
    d = folder()
    if not d.is_dir():
        return []
    out = [s for p in sorted(d.glob("*.json")) if ID.match(p.stem) and (s := _summary(p))]
    return sorted(out, key=lambda s: s["name"].lower())


TEMPO_RANGE = (40.0, 240.0)


def _beat_frame(bones: dict, fps: int, bpm: float) -> int:
    """Where in each beat the clip's moves land at this tempo: as scripts/dance/convert.py's
    ``beat_frame`` (the phase where the body slows down most), from the stored rotations."""
    import numpy as np

    speed = None
    for flat in bones.values():
        q = np.asarray(flat, dtype=float).reshape(-1, 4)
        d = np.abs(np.sum(q[1:] * q[:-1], axis=1)).clip(0, 1)
        s = 2 * np.arccos(d)
        speed = s if speed is None else speed + s
    if speed is None or len(speed) < 4:
        return 0
    speed = np.convolve(np.concatenate([[speed[0]], speed]), np.ones(3) / 3, mode="same")
    x = -np.maximum(0.0, -np.diff(speed, prepend=speed[0]))
    period = 60 * fps / bpm
    k = np.arange(int((len(x) - 1) / period))
    if len(k) < 2:
        return int(np.argmin(x))
    phases = np.arange(max(1, int(round(period))))
    idx = np.minimum(np.rint(phases[:, None] + k[None, :] * period).astype(int), len(x) - 1)
    return int(phases[int(np.argmin(x[idx].mean(axis=1)))])


def set_tempo(clip_id: str, bpm: float) -> dict:
    """The user's tempo for a clip, set by eye in her Settings (#132): saved in the clip, so
    Auto fits it to the right songs from then on, with where its moves land found again."""
    p = path_of(clip_id)
    if p is None:
        raise KeyError(clip_id)
    bpm = round(max(TEMPO_RANGE[0], min(TEMPO_RANGE[1], float(bpm))), 1)
    d = json.loads(p.read_text(encoding="utf-8"))
    d["bpm"] = bpm
    d["bpm_from"] = "set by hand"
    d["beat_frame"] = _beat_frame(d.get("bones") or {}, int(d.get("fps") or 30), bpm)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
    tmp.replace(p)
    return _summary(p) or {}


def path_of(clip_id: str) -> Path | None:
    if not ID.match(clip_id or ""):
        return None
    p = folder() / f"{clip_id}.json"
    return p if _summary(p) else None
