"""Each music track's tempo, known before it plays (#114): what Mira dances to (#115, #116).

Music only, never spoken word: tracks are taken from the catalog's music albums
(``music_catalog.music_albums``), so an audiobook, a podcast or a cold read never gets a
tempo and she never dances to one.

Where a BPM comes from: the file's own BPM tag when it has one, else measured from the audio
(numpy only: a spectral-flux onset curve over the whole track, its autocorrelation, a gentle
prior around ``PRIOR_BPM``). Only a reading outside ``LOW``-``HIGH`` is halved or doubled.
The first version folded everything into 80-160 with a prior at 120: "Cry Freedom" (slow, 78)
came out 156 and she danced at double speed (user, 2026-11-25). Its audio's strongest beat is
78.3 (0.41) against 156.6 (0.33); "Get The Party Started" is 64.6 (0.78) against 129.2 (0.74),
and the prior has to pick 78 for one and 129 for the other, which 100 +- 1 octave does.

**Where the beats fall** (``beats``, #124): every beat's time, by dynamic programming over the
onset curve (Ellis 2007: each beat as strong an onset as can be found about one period after
the last), so the grid follows a band that speeds up or slows down. One tempo and one phase
weren't enough: "September" measured 124.3 against ~126, and that drifts several beats over a
song. The player sends where it is between two beats, and her moves land on them.

Measured on amd-halo, 2026-11-25: ~0.2 s to fetch an MP3, ~0.5 s to measure it.

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
LOW, HIGH = 60.0, 200.0          # only a reading outside this is halved or doubled
PRIOR_BPM, PRIOR_OCTAVES = 100.0, 1.0
VERSION = 2                      # 2: the tempo prior moved, and beats; older entries are measured again
MAX_AGE_S = 6 * 3600             # look for new albums this often (only new tracks are measured)
MAX_BYTES = 200 * 1024 * 1024    # a bigger file is not fetched to be measured
SKIP_S, TAKE_S = 0.0, 600.0      # the whole track (up to 10 min): beat times are from its start
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


def _envelope(x, sr):
    """The onset curve (positive spectral change) and its frame rate. Its frame i covers the
    audio from i * hop; a sound shows when it enters the window, ``ONSET_LAG_S`` before it is
    heard (measured on clicks: beats came out ~88 ms early without it)."""
    import numpy as np

    n, hop = 1024, 128
    if len(x) < n * 4:
        return None, 0.0
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop] * np.hanning(n)
    mag = np.log1p(100 * np.abs(np.fft.rfft(frames, axis=1)))
    flux = np.maximum(0, np.diff(mag, axis=0)).sum(axis=1)
    flux = np.maximum(flux - np.convolve(flux, np.ones(32) / 32, mode="same"), 0)
    return (flux if flux.any() else None), sr / hop


def autocorr(x):
    """The autocorrelation for lags 0..len(x)-1, by FFT. Not np.correlate: that makes one dot
    product per lag, each spread over every core by the BLAS, and on a whole song (~23,000
    points) it burned 5 h 40 min of CPU in 22 min and never finished, in the server's own
    background job too ("What About Us", 2026-11-25)."""
    import numpy as np

    n = len(x)
    f = np.fft.rfft(x, 2 * n)
    return np.fft.irfft(f * np.conj(f))[:n]


def tempo_of(flux, fps: float) -> float | None:
    """The tempo the onset curve repeats at most, with a gentle prior, folded only if extreme."""
    import numpy as np

    ac = autocorr(flux)
    lags = np.arange(len(ac), dtype=float)
    with np.errstate(divide="ignore"):
        bpm = np.where(lags > 0, 60 * fps / np.maximum(lags, 1e-9), 0)
    ok = np.where((bpm >= 45) & (bpm <= 220))[0]
    if not len(ok):
        return None
    prior = np.exp(-0.5 * (np.log2(bpm[ok] / PRIOR_BPM) / PRIOR_OCTAVES) ** 2)
    i = int(ok[np.argmax(ac[ok] * prior)])
    lag = float(i)
    if 0 < i < len(ac) - 1:                           # between two lags: a parabola's peak
        a, b, c = ac[i - 1], ac[i], ac[i + 1]
        den = a - 2 * b + c
        if den:
            lag = i + 0.5 * (a - c) / den
    return 60 * fps / lag


TIGHTNESS = 100.0                # how much a beat may stray from one period after the last
ONSET_LAG_S = (1024 - 64) / 11025   # the onset curve's window, less half a hop (see _envelope)


def track_beats(flux, fps: float, bpm: float) -> list[float]:
    """Every beat's time (s): the path through the onsets that best keeps one period apart."""
    import numpy as np

    o = flux / (flux.std() or 1.0)
    period = 60 * fps / bpm
    lo, hi = int(round(period / 2)), int(round(period * 2))
    lags = np.arange(lo, hi + 1)
    penalty = -TIGHTNESS * np.log(lags / period) ** 2
    score = o.astype(float).copy()
    back = np.full(len(o), -1)
    for t in range(lo, len(o)):
        prev = t - lags
        ok = prev >= 0
        cand = score[prev[ok]] + penalty[ok]
        j = int(np.argmax(cand))
        if cand[j] > 0:
            score[t] = o[t] + cand[j]
            back[t] = prev[ok][j]
    # End on the best-scoring frame of the last period, then follow the path back.
    t = len(o) - hi + int(np.argmax(score[-hi:])) if len(o) > hi else int(np.argmax(score))
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    return [round(b / fps + ONSET_LAG_S, 3) for b in reversed(beats)]


def analyse(data: bytes, bpm: float | None = None) -> dict | None:
    """``{"bpm", "beats"}`` for an audio file's bytes (``bpm`` given, e.g. from a tag: the
    beats are found at it), or None (too short, silent, unreadable)."""
    import numpy as np

    got = _samples(data)
    if got is None:
        return None
    flux, fps = _envelope(*got)
    if flux is None:
        return None
    raw = fold(bpm or tempo_of(flux, fps))
    if not raw:
        return None
    beats = track_beats(flux, fps, raw)
    if len(beats) < 8:
        return None
    # The tempo is what the beats say (the middle half, past an intro and an ending).
    gaps = np.diff(beats)
    mid = gaps[len(gaps) // 4: max(len(gaps) // 4 + 1, 3 * len(gaps) // 4)]
    return {"bpm": round(60 / float(np.median(mid)), 2), "beats": beats}


def estimate(data: bytes) -> float | None:
    """The tempo of an audio file's bytes, or None (too short, silent, unreadable)."""
    got = analyse(data)
    return round(got["bpm"], 1) if got else None


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
    try:
        got = analyse(data, bpm=t)
    except Exception as e:                            # noqa: BLE001 — a file it can't decode
        got, err = None, type(e).__name__
    else:
        err = "no beat"
    if got:
        return {**got, "from": "tag" if t else "audio", "v": VERSION}
    if t:                                             # a tag, but audio it can't read: no beats
        return {"bpm": t, "from": "tag", "v": VERSION}
    return {"bpm": None, "failed": err, "v": VERSION}


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

_loaded: dict = {}                # path -> (mtime, data): the file holds every beat (~2 MB)


def load(root: Path) -> dict:
    p = Path(root) / FILE
    try:
        mtime = p.stat().st_mtime
        hit = _loaded.get(str(p))
        if hit and hit[0] == mtime:
            return hit[1]
        d = json.loads(p.read_text(encoding="utf-8"))
        d = d if isinstance(d, dict) else {}
        _loaded[str(p)] = (mtime, d)
        return d
    except (OSError, ValueError):
        return {}


def _save(root: Path, cache: dict) -> None:
    p = Path(root) / FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    tmp.replace(p)


def beats_of(root: Path, ids) -> dict[int, list]:
    """The beat times of these tracks, where they were found (the player asks for the one it
    starts; they are too long to ride along in every queue entry)."""
    tracks = load(root).get("tracks") or {}
    return {int(i): (tracks.get(str(i)) or {})["beats"] for i in ids
            if (tracks.get(str(i)) or {}).get("beats")}


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
            if key in tracks and tracks[key].get("v") == VERSION:
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
    stale = any(v.get("v") != VERSION for v in (cache.get("tracks") or {}).values())
    if cache and not stale and time.time() - float(cache.get("built_at") or 0) < MAX_AGE_S:
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
