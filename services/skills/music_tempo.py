"""Each music track's tempo, known before it plays (#114): what Mira dances to (#115, #116).

Music only, never spoken word: tracks are taken from the catalog's music albums
(``music_catalog.music_albums``), so an audiobook, a podcast or a cold read never gets a
tempo and she never dances to one.

Where a BPM comes from: the file's own BPM tag when it has one, else measured from the audio
(numpy only: a spectral-flux onset curve, its autocorrelation, a prior around 120). Either way
it is folded into a dancing range, ``LOW``-``HIGH``: a reading of 70 or 280 is 140, since an
analyser's commonest mistake is half or double time.

Measured on amd-halo, 2026-11-25: ~0.2 s to fetch an MP3 and ~0.2 s to measure 90 s of it;
"Get The Party Started" 129 (published ~129), "Back in Black" 94 (~94).

The cache is ``<library>/music_tempo.json`` ({"tracks": {"<item id>": {...}}}), filled in the
background a track at a time and kept across rebuilds; a track is measured once. A play never
waits for it: a track not measured yet simply has no ``bpm``.
"""
from __future__ import annotations

import asyncio
import io
import json
import time
from pathlib import Path

from services.skills import music_catalog
from services.skills import orpheus_media as om

FILE = "music_tempo.json"
LOW, HIGH = 80.0, 160.0          # the dancing range a reading is folded into
MAX_AGE_S = 6 * 3600             # look for new albums this often (only new tracks are measured)
MAX_BYTES = 200 * 1024 * 1024    # a bigger file is not fetched to be measured
SKIP_S, TAKE_S = 30.0, 90.0      # measure 90 s from 30 s in (past an intro)
SAVE_EVERY = 20
_building: set = set()


# --- one track -------------------------------------------------------------------------------

def fold(bpm: float | None) -> float | None:
    if not bpm or bpm <= 0:
        return None
    while bpm < LOW:
        bpm *= 2
    while bpm >= HIGH * 1.0001 and bpm / 2 >= LOW:
        bpm /= 2
    return round(bpm, 1)


def _samples(data: bytes):
    """Mono float samples to measure (``TAKE_S`` from ``SKIP_S`` in, or from the start of a
    short track) at about 11 kHz, and their rate; None for a track under 10 s. soundfile
    first; PyAV (faster-whisper's) for what libsndfile can't open: 42 of 917 MP3s on
    amd-halo, 2026-11-25."""
    import numpy as np
    import soundfile as sf

    try:
        with sf.SoundFile(io.BytesIO(data)) as f:
            sr = f.samplerate
            if f.frames < 10 * sr:
                return None
            f.seek(int(SKIP_S * sr) if f.frames > (SKIP_S + 20) * sr else 0)
            x = f.read(int(TAKE_S * sr), dtype="float32", always_2d=True).mean(axis=1)
        step = max(1, sr // 11025)                   # ~11 kHz: averaging is a rough low-pass
        return x[: len(x) // step * step].reshape(-1, step).mean(axis=1), sr / step
    except sf.LibsndfileError:
        pass
    import av

    sr = 11025
    with av.open(io.BytesIO(data)) as c:
        stream = c.streams.audio[0]
        length = float(stream.duration * stream.time_base) if stream.duration else (c.duration or 0) / 1e6
        if 0 < length < 10:
            return None
        if length > SKIP_S + 20:
            c.seek(int(SKIP_S / stream.time_base), stream=stream)
        resampler = av.AudioResampler(format="flt", layout="mono", rate=sr)
        parts, n = [], 0
        for frame in c.decode(stream):
            for out in resampler.resample(frame):
                parts.append(out.to_ndarray().reshape(-1))
                n += out.samples
            if n >= TAKE_S * sr:
                break
    if n < 10 * sr:
        return None
    return np.concatenate(parts)[: int(TAKE_S * sr)].astype("float32"), float(sr)


def estimate(data: bytes) -> float | None:
    """The tempo of an audio file's bytes, folded, or None (too short, silent, unreadable)."""
    import numpy as np

    got = _samples(data)
    if got is None:
        return None
    x, sr = got
    n, hop = 1024, 128
    if len(x) < n * 4:
        return None
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop] * np.hanning(n)
    mag = np.log1p(100 * np.abs(np.fft.rfft(frames, axis=1)))
    flux = np.maximum(0, np.diff(mag, axis=0)).sum(axis=1)
    flux = np.maximum(flux - np.convolve(flux, np.ones(32) / 32, mode="same"), 0)
    if not flux.any():
        return None
    fps = sr / hop
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    lags = np.arange(len(ac), dtype=float)
    with np.errstate(divide="ignore"):
        bpm = np.where(lags > 0, 60 * fps / np.maximum(lags, 1e-9), 0)
    ok = np.where((bpm >= 50) & (bpm <= 220))[0]
    if not len(ok):
        return None
    prior = np.exp(-0.5 * (np.log2(bpm[ok] / 120) / 0.9) ** 2)
    i = int(ok[np.argmax(ac[ok] * prior)])
    lag = float(i)
    if 0 < i < len(ac) - 1:                           # between two lags: a parabola's peak
        a, b, c = ac[i - 1], ac[i], ac[i + 1]
        den = a - 2 * b + c
        if den:
            lag = i + 0.5 * (a - c) / den
    return fold(60 * fps / lag)


def tag_bpm(data: bytes) -> float | None:
    """The file's own BPM tag (ID3 TBPM, a FLAC/Ogg BPM comment, MP4 tmpo), folded."""
    try:
        import mutagen
    except ImportError:
        return None
    try:
        f = mutagen.File(io.BytesIO(data))
    except Exception:                                 # noqa: BLE001 — any unreadable tag
        return None
    if f is None or not f.tags:
        return None
    for key in ("TBPM", "BPM", "bpm", "tmpo"):
        try:
            v = f.tags.get(key)
        except (KeyError, ValueError, TypeError):
            continue
        if v is None:
            continue
        v = getattr(v, "text", v)
        v = v[0] if isinstance(v, (list, tuple)) and v else v
        try:
            return fold(float(str(v).strip()))
        except ValueError:
            continue
    return None


def measure(data: bytes) -> dict:
    t = tag_bpm(data)
    if t:
        return {"bpm": t, "from": "tag"}
    try:
        b = estimate(data)
    except Exception as e:                            # noqa: BLE001 — a file it can't decode
        return {"bpm": None, "failed": type(e).__name__}
    return {"bpm": b, "from": "audio"} if b else {"bpm": None, "failed": "no beat"}


async def _fetch(st: dict, item_id: int) -> bytes | None:
    """The file as stored, served direct; None when Orpheus would transcode it (not worth a
    session) or it is too big."""
    import httpx

    d = await om._request("POST", f"{st['url']}/api/v1/items/{item_id}/playback", token=st["token"])
    if d.get("method") != "direct" or not d.get("url"):
        if d.get("session_id"):
            try:
                await om._request("DELETE", f"{st['url']}/api/v1/playback/{d['session_id']}", token=st["token"])
            except om.OrpheusError:
                pass
        return None
    if int(d.get("size") or 0) > MAX_BYTES:
        return None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=4.0)) as c:
            r = await c.get(st["url"] + d["url"])
    except httpx.HTTPError as e:
        raise om.OrpheusError("SERVER_UNREACHABLE", f"the music server didn't send it ({type(e).__name__})") from e
    return r.content if r.status_code == 200 else None


# --- the cache -------------------------------------------------------------------------------

def load(root: Path) -> dict:
    try:
        d = json.loads((Path(root) / FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(root: Path, cache: dict) -> None:
    p = Path(root) / FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    tmp.replace(p)


def bpm_of(root: Path, ids) -> dict[int, float]:
    """The known BPMs among these track ids (music only; the rest are left out)."""
    tracks = load(root).get("tracks") or {}
    out = {}
    for i in ids:
        rec = tracks.get(str(i)) or {}
        if rec.get("bpm"):
            out[int(i)] = float(rec["bpm"])
    return out


def annotate(root: Path, queue: list[dict]) -> list[dict]:
    """Queue entries gain ``bpm`` where it's known."""
    known = bpm_of(root, [e.get("id") for e in queue if e.get("id") is not None])
    for e in queue:
        if e.get("id") in known:
            e["bpm"] = known[e["id"]]
    return queue


async def build(root: Path, st: dict) -> dict:
    """Measure every music track not measured yet; forget tracks no longer on a music album."""
    cat = music_catalog.load(root) or await music_catalog.build(root, st)
    cache = load(root)
    tracks: dict = dict(cache.get("tracks") or {})
    keep: set[str] = set()
    measured = 0
    for album in music_catalog.music_albums(cat):
        try:
            queue = await om.expand(st["url"], st["token"], item_id=int(album["id"]))
        except om.OrpheusError as e:
            if e.code == "SERVER_UNREACHABLE":
                raise
            continue
        for entry in queue:
            key = str(entry["id"])
            keep.add(key)
            if key in tracks:
                continue
            data = await _fetch(st, int(entry["id"]))
            tracks[key] = (await asyncio.to_thread(measure, data) if data
                           else {"bpm": None, "failed": "not fetched"})
            tracks[key]["album"] = int(album["id"])
            measured += 1
            if measured % SAVE_EVERY == 0:
                _save(root, {"built_at": cache.get("built_at", 0), "tracks": tracks})
    tracks = {k: v for k, v in tracks.items() if k in keep}      # an album now spoken, or gone
    out = {"built_at": time.time(), "tracks": tracks}
    _save(root, out)
    print(f"[music] tempo: {sum(1 for v in tracks.values() if v.get('bpm'))} of {len(tracks)} tracks "
          f"({measured} measured now)", flush=True)
    return out


def refresh_soon(root: Path, st: dict) -> None:
    """Fill in new tracks in the background when stale; one build per library at a time."""
    cache = load(root)
    if cache and time.time() - float(cache.get("built_at") or 0) < MAX_AGE_S:
        return
    key = str(root)
    if key in _building:
        return
    _building.add(key)

    async def run():
        try:
            await build(root, st)
        except Exception as e:                                   # noqa: BLE001 — never an error
            print(f"[music] tempo not measured: {e}", flush=True)
        finally:
            _building.discard(key)
    asyncio.ensure_future(run())
