"""The library's albums, kept for vague plays (#80): "play something upbeat", "play some blues".

Not her memory and never in a prompt. Fortuna picks from it: each album goes in as
"Album — Artist (year)" and Fortuna's own knowledge of the artist does the rest. Orpheus has
no genres for this library (0 of 46 albums, 2026-11-24), so there is nothing better to send.

Measured on amd-halo, 2026-11-24 (46 albums):
- "upbeat" -> Maroon 5, "blues" -> John Lee Hooker, "party" -> Earth Wind & Fire,
  "guitar rock" -> AC/DC; a pick over 41 albums took 1.7 s, so a pick is made over a random
  ``PICK_FROM`` of them (0.74 s for 25 labels), which also varies what plays;
- the music library holds audiobooks too; one Fortuna pass per album (7 s for 46, in the
  background) marks them spoken, and a vague play never lands on one.

The cache is ``<library>/music_catalog.json``, rebuilt when older than ``MAX_AGE_S`` in the
background, so a play never waits for it once there is one.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path

from services.skills import orpheus_media as om

FILE = "music_catalog.json"
MAX_AGE_S = 6 * 3600
PICK_FROM = 24
SPOKEN_MIN = 0.6                 # "Melodysheep" (music) scored 0.51 spoken; the audiobooks 0.66+
_building: set = set()


def label(album: dict) -> str:
    art = album.get("artist") or ""
    yr = f" ({album['year']})" if album.get("year") else ""
    return f"{album['title']} — {art}{yr}" if art else f"{album['title']}{yr}"


def music_albums(catalog: dict) -> list[dict]:
    return [a for a in catalog.get("albums") or [] if not a.get("spoken")]


def sample(albums: list[dict], n: int = PICK_FROM, rng: random.Random | None = None) -> dict[str, dict]:
    """Up to n albums, in random order, by their label (labels kept unique)."""
    pool = list(albums)
    (rng or random).shuffle(pool)
    out: dict[str, dict] = {}
    for a in pool:
        k = label(a)
        if k not in out:
            out[k] = a
        if len(out) >= n:
            break
    return out


def load(root: Path) -> dict:
    try:
        return json.loads((Path(root) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(root: Path, catalog: dict) -> None:
    p = Path(root) / FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(catalog), encoding="utf-8")
    tmp.replace(p)


async def build(root: Path, st: dict) -> dict:
    """Read every album from Orpheus and mark the spoken ones (Fortuna). ~8 s for 46."""
    from services import fortuna

    base, tok = st["url"], st["token"]
    items, cursor = [], ""
    while True:
        page = await om._request("GET", f"{base}/api/v1/items?kind=album&limit=200"
                                        + (f"&cursor={cursor}" if cursor else ""), token=tok)
        items += page.get("items") or []
        cursor = page.get("next_cursor") or ""
        if not cursor or len(items) >= 2000:
            break
    full = await asyncio.gather(*[om._request("GET", f"{base}/api/v1/items/{a['id']}", token=tok)
                                  for a in items], return_exceptions=True)
    old = {a["id"]: a for a in load(root).get("albums") or []}
    albums = []
    for a, d in zip(items, full):
        if isinstance(d, Exception) or not a.get("playable", True):
            continue
        rec = {"id": int(a["id"]), "title": a.get("title") or "", "year": a.get("year"),
               "artist": ((d.get("album") or {}).get("album_artist") or {}).get("title", "")}
        prev = old.get(rec["id"])
        if prev and prev.get("title") == rec["title"] and "spoken" in prev:
            rec["spoken"] = prev["spoken"]                   # asked before: not again
        else:
            got = await fortuna.choose(label(rec), "Is this a music album, or spoken word (an audiobook, "
                                       "a podcast, a drama)?",
                                       {"music": "a music album",
                                        "spoken": "spoken word: an audiobook, a podcast or an audio drama"})
            if got is not None:
                rec["spoken"] = got[0] == "spoken" and got[1] >= SPOKEN_MIN
        albums.append(rec)
    catalog = {"built_at": time.time(), "albums": albums}
    _save(root, catalog)
    print(f"[music] catalog: {len(albums)} albums, {sum(1 for a in albums if a.get('spoken'))} spoken word",
          flush=True)
    return catalog


def refresh_soon(root: Path, st: dict) -> None:
    """Rebuild in the background when missing or stale; one build per library at a time."""
    cat = load(root)
    if cat and time.time() - float(cat.get("built_at") or 0) < MAX_AGE_S:
        return
    key = str(root)
    if key in _building:
        return
    _building.add(key)

    async def run():
        try:
            await build(root, st)
        except Exception as e:                                # noqa: BLE001 — never an error
            print(f"[music] catalog not rebuilt: {e}", flush=True)
        finally:
            _building.discard(key)
    asyncio.ensure_future(run())


async def pick(root: Path, st: dict, request: str) -> dict | None:
    """The album Fortuna thinks fits a described request, or None (no catalog yet, Fortuna
    down): the caller hands the request to her."""
    from services import fortuna

    refresh_soon(root, st)
    albums = music_albums(load(root))
    if not albums:
        return None
    options = sample(albums)
    got = await fortuna.choose(request, "Which album fits this request best?",
                               {k: f"the album {k}" for k in options})
    return options.get(got[0]) if got else None
