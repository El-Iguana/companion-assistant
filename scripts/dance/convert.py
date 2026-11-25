#!/usr/bin/env python3
"""Turn a motion-capture clip into a Cantinero dance clip (#120): the VRM humanoid's own
bone rotations, so the avatar page plays it on any model with no retargeting of its own.

    python scripts/dance/convert.py SRC.glb --out data/dance --name "Macarena" \\
        --source "CMU mocap, subject 143 trial 35" --license "CMU-Mocap" --credit "..."

Input: a glTF/GLB whose skeleton is in a T-pose at rest, facing +Z with its left on +X (the
VRM 1.0 normalized rig's own convention). The CMU database as converted by
codepawl/cmu-mocap-glb (its ``skeleton/`` files) is exactly that: every rest rotation is
zero, the arms lie along +-X. Other rigs (Mixamo, #121) need only their own bone map.

How a bone is moved: each source joint's world rotation change from its rest (``D``) is the
same change for the VRM bone, whose normalized rest is the identity with the same T-pose. A
VRM bone's local rotation is then its parent bone's ``D`` undone, times its own: joints the
VRM has no bone for (CMU's ``upperneck``, ``lhipjoint``) simply fold into the next one.

Also, per clip:
- **Heading.** The slow turning is taken out (a moving average of the hips' heading over
  ``HEADING_WINDOW_S``), so she keeps facing the viewer; a twist of the hips stays. The
  Macarena turns a quarter at the end of each verse.
- **Hips.** Height as a change from the clip's median, in leg lengths (the page scales it by
  the model's); side to side and back and forth only as sway about a moving centre, so she
  dances in place.
- **Tempo.** Measured from the motion (how fast the bones turn, autocorrelated, a gentle prior
  around 100), halved or doubled only outside 60-200, like the songs'
  (services/skills/music_tempo.py); or given.
- **Beat.** ``beat_frame``: where in each beat the body moves least, i.e. where a pose lands;
  the page keeps that on the song's beats (#124).
- **Loop.** Whole bars (8 or 16 beats) whose last pose is closest to the first.

Output: ``<out>/<id>.json``, ``{"format": "cantinero-dance/1", ...}``; see ``write``.
Plain numpy, no Blender: tests/test_dance_convert.py.
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

import numpy as np

FORMAT = "cantinero-dance/1"
FPS = 30
HEADING_WINDOW_S = 2.0
SWAY_WINDOW_S = 2.0
LOW, HIGH = 60.0, 200.0          # as the songs' tempos (services/skills/music_tempo.py, #124)
PRIOR_BPM, PRIOR_OCTAVES = 100.0, 1.0

# CMU joint -> VRM humanoid bone. Root first; every other bone's parent is the nearest mapped
# ancestor (VRM_PARENT), which is how unmapped joints fold in.
CMU_MAP = {
    "root": "hips",
    "lowerback": "spine", "upperback": "chest", "thorax": "upperChest",
    "lowerneck": "neck", "head": "head",
    "lclavicle": "leftShoulder", "lhumerus": "leftUpperArm", "lradius": "leftLowerArm", "lwrist": "leftHand",
    "rclavicle": "rightShoulder", "rhumerus": "rightUpperArm", "rradius": "rightLowerArm", "rwrist": "rightHand",
    "lfemur": "leftUpperLeg", "ltibia": "leftLowerLeg", "lfoot": "leftFoot", "ltoes": "leftToes",
    "rfemur": "rightUpperLeg", "rtibia": "rightLowerLeg", "rfoot": "rightFoot", "rtoes": "rightToes",
}
VRM_PARENT = {
    "hips": None, "spine": "hips", "chest": "spine", "upperChest": "chest", "neck": "upperChest",
    "head": "neck",
    "leftShoulder": "upperChest", "leftUpperArm": "leftShoulder", "leftLowerArm": "leftUpperArm",
    "leftHand": "leftLowerArm",
    "rightShoulder": "upperChest", "rightUpperArm": "rightShoulder", "rightLowerArm": "rightUpperArm",
    "rightHand": "rightLowerArm",
    "leftUpperLeg": "hips", "leftLowerLeg": "leftUpperLeg", "leftFoot": "leftLowerLeg", "leftToes": "leftFoot",
    "rightUpperLeg": "hips", "rightLowerLeg": "rightUpperLeg", "rightFoot": "rightLowerLeg",
    "rightToes": "rightFoot",
}


# --- quaternions, [x, y, z, w], any leading shape ----------------------------------------------

def qmul(a, b):
    ax, ay, az, aw = np.moveaxis(a, -1, 0)
    bx, by, bz, bw = np.moveaxis(b, -1, 0)
    return np.stack([aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw,
                     aw * bw - ax * bx - ay * by - az * bz], axis=-1)


def qinv(q):
    return q * np.array([-1.0, -1.0, -1.0, 1.0])


def qrot(q, v):
    """Rotate vectors v by q."""
    qv = np.concatenate([v, np.zeros(v.shape[:-1] + (1,))], axis=-1)
    return qmul(qmul(q, qv), qinv(q))[..., :3]


def qyaw(angle):
    a = np.asarray(angle, dtype=float) / 2
    return np.stack([np.zeros_like(a), np.sin(a), np.zeros_like(a), np.cos(a)], axis=-1)


def qangle(a, b):
    """The angle between two rotations (radians)."""
    d = np.abs(np.sum(a * b, axis=-1)).clip(0, 1)
    return 2 * np.arccos(d)


def qcontinuous(q):
    """Flip signs so consecutive keys take the short way (q and -q are one rotation)."""
    q = q.copy()
    for i in range(1, len(q)):
        if np.dot(q[i - 1], q[i]) < 0:
            q[i] = -q[i]
    return q


def nlerp(a, b, t):
    b = np.where((np.sum(a * b, axis=-1) < 0)[..., None], -b, b)
    q = a * (1 - t)[..., None] + b * t[..., None]
    return q / np.linalg.norm(q, axis=-1, keepdims=True)


# --- reading a glTF clip -----------------------------------------------------------------------

_COMP = {5126: np.float32, 5123: np.uint16, 5125: np.uint32, 5121: np.uint8, 5122: np.int16, 5120: np.int8}
_N = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def read_glb(path: Path) -> tuple[dict, bytes]:
    b = Path(path).read_bytes()
    if b[:4] != b"glTF":
        raise ValueError("not a GLB file")
    jlen, = struct.unpack("<I", b[12:16])
    gltf = json.loads(b[20:20 + jlen])
    off = 20 + jlen
    binc = b""
    if off + 8 <= len(b):
        blen, = struct.unpack("<I", b[off:off + 4])
        binc = b[off + 8: off + 8 + blen]
    return gltf, binc


def _accessor(gltf: dict, binc: bytes, i: int) -> np.ndarray:
    acc = gltf["accessors"][i]
    view = gltf["bufferViews"][acc["bufferView"]]
    dt, n = _COMP[acc["componentType"]], _N[acc["type"]]
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride") or np.dtype(dt).itemsize * n
    count = acc["count"]
    raw = np.frombuffer(binc, dtype=np.uint8, count=stride * (count - 1) + np.dtype(dt).itemsize * n, offset=start)
    rows = np.lib.stride_tricks.as_strided(raw, shape=(count, np.dtype(dt).itemsize * n), strides=(stride, 1))
    out = rows.copy().view(dt).reshape(count, n).astype(np.float64)
    if acc.get("normalized") and dt is not np.float32:
        out /= np.iinfo(dt).max
    return out if n > 1 else out[:, 0]


def load_clip(path: Path, fps: int = FPS) -> dict:
    """Nodes (name, parent, rest translation and rotation) and, resampled to ``fps``, every
    node's rotation and the root's translation."""
    gltf, binc = read_glb(path)
    nodes = gltf["nodes"]
    parent = [-1] * len(nodes)
    for i, n in enumerate(nodes):
        for c in n.get("children", []):
            parent[c] = i
    names = [n.get("name") or f"node{i}" for i, n in enumerate(nodes)]
    rest_t = np.array([n.get("translation", [0, 0, 0]) for n in nodes], dtype=float)
    rest_r = np.array([n.get("rotation", [0, 0, 0, 1]) for n in nodes], dtype=float)
    anim = gltf["animations"][0]
    tracks, t_end = {}, 0.0
    for ch in anim["channels"]:
        s = anim["samplers"][ch["sampler"]]
        times = _accessor(gltf, binc, s["input"])
        vals = _accessor(gltf, binc, s["output"])
        if s.get("interpolation") == "CUBICSPLINE":
            vals = vals[1::3]
        tracks[(ch["target"]["node"], ch["target"]["path"])] = (times, vals)
        t_end = max(t_end, float(times[-1]))
    frames = int(np.floor(t_end * fps)) + 1
    grid = np.arange(frames) / fps
    rot = np.repeat(rest_r[None], frames, axis=0)                # (F, N, 4)
    pos = np.repeat(rest_t[None], frames, axis=0)                # (F, N, 3)
    for (node, path_), (times, vals) in tracks.items():
        idx = np.clip(np.searchsorted(times, grid, side="right") - 1, 0, len(times) - 1)
        nxt = np.minimum(idx + 1, len(times) - 1)
        span = np.where(nxt > idx, times[nxt] - times[idx], 1.0)
        f = np.clip((grid - times[idx]) / span, 0, 1)
        if path_ == "rotation":
            rot[:, node] = nlerp(vals[idx], vals[nxt], f)
        elif path_ == "translation":
            pos[:, node] = vals[idx] + (vals[nxt] - vals[idx]) * f[:, None]
    return {"names": names, "parent": parent, "rest_t": rest_t, "rest_r": rest_r,
            "rot": rot, "pos": pos, "fps": fps}


# --- retargeting -------------------------------------------------------------------------------

def _world(parent: list, local: np.ndarray) -> np.ndarray:
    """World rotations from local ones, (..., N, 4)."""
    world = np.empty_like(local)
    for i in range(local.shape[-2]):                  # glTF lists parents before children
        p = parent[i]
        world[..., i, :] = local[..., i, :] if p < 0 else qmul(world[..., p, :], local[..., i, :])
    return world


def _order(parent: list) -> list:
    seen, out = set(), []

    def visit(i):
        if i in seen:
            return
        if parent[i] >= 0:
            visit(parent[i])
        seen.add(i)
        out.append(i)
    for i in range(len(parent)):
        visit(i)
    return out


def _moving_average(x: np.ndarray, n: int) -> np.ndarray:
    n = max(1, int(n)) | 1                              # odd, centred
    pad = n // 2
    xp = np.concatenate([np.repeat(x[:1], pad, axis=0), x, np.repeat(x[-1:], pad, axis=0)])
    c = np.cumsum(np.concatenate([np.zeros((1,) + x.shape[1:]), xp]), axis=0)
    return (c[n:] - c[:-n]) / n


def retarget(clip: dict, bone_map: dict = CMU_MAP, keep_heading: bool = False) -> dict:
    names, parent = clip["names"], clip["parent"]
    order = _order(parent)
    if order != list(range(len(parent))):                # make parents come first
        raise ValueError("node order with children before parents is not supported")
    world = _world(parent, clip["rot"])                  # (F, N, 4)
    world0 = _world(parent, clip["rest_r"][None])[0]     # (N, 4)
    delta = qmul(world, qinv(world0)[None])              # each joint's turn from its rest
    by_name = {n: i for i, n in enumerate(names)}
    missing = [j for j in bone_map if j not in by_name]
    if bone_map is CMU_MAP and missing:
        raise ValueError(f"not a CMU skeleton: no {', '.join(missing)}")
    joint_of = {v: by_name[j] for j, v in bone_map.items() if j in by_name}
    root = joint_of["hips"]
    fps = clip["fps"]

    # Heading: the hips' facing, its slow part taken out.
    fwd = qrot(delta[:, root], np.array([0.0, 0.0, 1.0])[None].repeat(len(delta), 0))
    heading = np.unwrap(np.arctan2(fwd[:, 0], fwd[:, 2]))
    slow = np.zeros_like(heading) if keep_heading else _moving_average(heading, HEADING_WINDOW_S * fps)
    unturn = qyaw(-slow)                                  # (F, 4)

    bones = {}
    for bone, j in joint_of.items():
        p = VRM_PARENT.get(bone)
        while p is not None and p not in joint_of:
            p = VRM_PARENT.get(p)
        if p is None:                                     # the hips: their world turn, unturned
            q = qmul(unturn, delta[:, j])
        else:
            q = qmul(qinv(delta[:, joint_of[p]]), delta[:, j])
        bones[bone] = qcontinuous(q / np.linalg.norm(q, axis=-1, keepdims=True))

    # Hips: in leg lengths, about the clip's median height and a moving centre.
    lt = [by_name[n] for n in ("lfemur", "ltibia", "lfoot", "ltoes") if n in by_name]
    leg = float(-sum(clip["rest_t"][i][1] for i in lt)) if lt else 0.9
    leg = leg if leg > 0.2 else 0.9
    p = clip["pos"][:, root].copy()
    centre = _moving_average(p[:, [0, 2]], SWAY_WINDOW_S * fps)
    sway = np.zeros_like(p)
    sway[:, [0, 2]] = p[:, [0, 2]] - centre
    sway = qrot(unturn, sway)
    sway[:, 1] = p[:, 1] - np.median(p[:, 1])
    # Each bone's rest direction (toward its first child joint), in the rest world frame: the
    # page turns its model's bones by the difference, since CMU stands with its legs ~20 deg
    # apart and a VRM with them straight (uncorrected, her legs crossed).
    world_pos = np.zeros((len(names), 3))
    for i in range(len(names)):
        pi = parent[i]
        world_pos[i] = clip["rest_t"][i] if pi < 0 else world_pos[pi] + qrot(world0[pi], clip["rest_t"][i])
    children = {i: [c for c in range(len(names)) if parent[c] == i] for i in range(len(names))}
    rest_dirs = {}
    for bone, j in joint_of.items():
        if bone == "hips" or not children[j]:
            continue
        d = world_pos[children[j][0]] - world_pos[j]
        if np.linalg.norm(d) > 1e-6:
            rest_dirs[bone] = d / np.linalg.norm(d)
    return {"bones": bones, "hips": sway / leg, "fps": fps, "frames": len(p), "leg_m": leg,
            "rest_dirs": rest_dirs}


# --- tempo and loop ----------------------------------------------------------------------------

def fold(bpm: float) -> float:
    while bpm < LOW:
        bpm *= 2
    while bpm >= HIGH * 1.0001 and bpm / 2 >= LOW:
        bpm /= 2
    return round(bpm, 1)


def motion_speed(bones: dict) -> np.ndarray:
    """How fast the body turns, frame to frame (radians per frame, summed over bones)."""
    tot = None
    for q in bones.values():
        s = qangle(q[1:], q[:-1])
        tot = s if tot is None else tot + s
    return np.concatenate([[0.0], tot])


def estimate_bpm(bones: dict, fps: int) -> float | None:
    x = motion_speed(bones)
    x = x - _moving_average(x, fps)                      # the moves, not the drift
    if len(x) < fps * 3 or not np.any(x):
        return None
    ac = np.correlate(x, x, mode="full")[len(x) - 1:]
    lags = np.arange(len(ac), dtype=float)
    bpm = np.where(lags > 0, 60 * fps / np.maximum(lags, 1e-9), 0)
    ok = np.where((bpm >= 50) & (bpm <= 220))[0]
    if not len(ok):
        return None
    prior = np.exp(-0.5 * (np.log2(bpm[ok] / PRIOR_BPM) / PRIOR_OCTAVES) ** 2)
    i = int(ok[np.argmax(ac[ok] * prior)])
    lag = float(i)
    if 0 < i < len(ac) - 1:
        a, b, c = ac[i - 1], ac[i], ac[i + 1]
        if a - 2 * b + c:
            lag = i + 0.5 * (a - c) / (a - 2 * b + c)
    return fold(60 * fps / lag)


def beat_frame(bones: dict, fps: int, bpm: float) -> int:
    """A frame where the clip's moves land on a beat (#124). Dancers hit a pose on the beat:
    the moment the motion stops, so the phase of the beat where the body slows down most, over
    the whole clip. (Not where it moves least: a pose held after the hit is stillest in its
    middle, half a hold late.)"""
    speed = _moving_average(motion_speed(bones)[:, None], 3)[:, 0]
    x = -np.maximum(0.0, -np.diff(speed, prepend=speed[0]))     # deceleration, as a minimum
    period = 60 * fps / bpm
    k = np.arange(int((len(x) - 1) / period))
    if len(k) < 2:
        return int(np.argmin(x))
    phases = np.arange(max(1, int(round(period))))
    idx = np.minimum(np.rint(phases[:, None] + k[None, :] * period).astype(int), len(x) - 1)
    return int(phases[int(np.argmin(x[idx].mean(axis=1)))])


def pick_loop(bones: dict, hips: np.ndarray, fps: int, bpm: float) -> tuple[int, int]:
    """Start and end frames of a loop of whole bars whose two ends look most alike."""
    frames = len(hips)
    beat = 60 * fps / bpm
    for beats in (16, 8, 4):
        n = int(round(beats * beat))
        if n < frames - 2:
            break
    else:
        return 0, frames - 1
    best, where = None, (0, n)
    first = int(0.3 * fps) if frames - n > int(0.3 * fps) else 0
    for s in range(first, frames - n):
        e = s + n
        d = sum(float(qangle(q[s], q[e])) for q in bones.values()) + 5 * float(np.linalg.norm(hips[s] - hips[e]))
        if best is None or d < best:
            best, where = d, (s, e)
    return where


# --- writing -----------------------------------------------------------------------------------

def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "clip"


def build(src: Path, name: str, source: str, license_: str, credit: str = "", bpm: float | None = None,
          keep_heading: bool = False, style: str = "") -> dict:
    clip = load_clip(src)
    r = retarget(clip, keep_heading=keep_heading)
    tempo = fold(bpm) if bpm else estimate_bpm(r["bones"], r["fps"])
    if not tempo:
        raise ValueError("no steady beat in this clip: give --bpm")
    start, end = pick_loop(r["bones"], r["hips"], r["fps"], tempo)
    speed = float(np.mean(motion_speed(r["bones"])[start:end])) * r["fps"]     # radians per second
    rnd = lambda a: [round(float(v), 4) for v in np.asarray(a).reshape(-1)]   # noqa: E731
    return {
        "format": FORMAT, "id": slug(name), "name": name, "style": style,
        "bpm": tempo, "energy": round(min(1.0, speed / 60.0), 2),    # the Twist ~0.96, the Macarena ~0.32
        "fps": r["fps"], "frames": r["frames"], "loop": [int(start), int(end)],
        "beat_frame": beat_frame(r["bones"], r["fps"], tempo),
        "source": source, "license": license_, "credit": credit,
        "bones": {b: rnd(q) for b, q in r["bones"].items()},
        "rest_dirs": {b: rnd(d) for b, d in r["rest_dirs"].items()},
        "hips": rnd(r["hips"]),
    }


def write(out_dir: Path, data: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{data['id']}.json"
    p.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("src", type=Path)
    ap.add_argument("--out", type=Path, default=Path("data/dance"))
    ap.add_argument("--name", required=True)
    ap.add_argument("--style", default="")
    ap.add_argument("--source", required=True)
    ap.add_argument("--license", required=True, help="e.g. CMU-Mocap, or local for a clip you may not share")
    ap.add_argument("--credit", default="")
    ap.add_argument("--bpm", type=float, help="the clip's tempo, when measuring it goes wrong")
    ap.add_argument("--keep-heading", action="store_true", help="keep the clip's turns")
    a = ap.parse_args(argv)
    data = build(a.src, a.name, a.source, a.license, a.credit, a.bpm, a.keep_heading, a.style)
    p = write(a.out, data)
    print(f"{p}: {data['bpm']} BPM, loop {data['loop'][0]}-{data['loop'][1]} of {data['frames']} frames "
          f"at {data['fps']} fps, energy {data['energy']}, {p.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
