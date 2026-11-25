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


def path_of(clip_id: str) -> Path | None:
    if not ID.match(clip_id or ""):
        return None
    p = folder() / f"{clip_id}.json"
    return p if _summary(p) else None
