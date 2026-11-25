"""#121: your own dance clips (Mixamo's FBX), converted locally and never committed or baked in."""
from __future__ import annotations

import struct
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "dance"))
import convert as cv  # noqa: E402
import fbx  # noqa: E402

# A Mixamo skeleton in its T-pose (centimetres; offsets from Mixamo's Y Bot, trimmed).
SKELETON = [
    ("Hips", None, [0, 99.7, 0]),
    ("Spine", "Hips", [0, 9.9, -1.2]), ("Spine1", "Spine", [0, 11.6, -1.4]), ("Spine2", "Spine1", [0, 13.4, -1.6]),
    ("Neck", "Spine2", [0, 15, 0.8]), ("Head", "Neck", [0, 10.3, 3.1]), ("HeadTop_End", "Head", [0, 18.5, 6.6]),
    ("LeftShoulder", "Spine2", [6.1, 9.1, 0.7]), ("LeftArm", "LeftShoulder", [12.7, -0.3, -2.6]),
    ("LeftForeArm", "LeftArm", [27.4, 0, 0]), ("LeftHand", "LeftForeArm", [27.6, 0, 0]),
    ("LeftHandMiddle1", "LeftHand", [12.8, 0, 0]),
    ("RightShoulder", "Spine2", [-6.1, 9.1, 0.7]), ("RightArm", "RightShoulder", [-12.7, -0.3, -2.6]),
    ("RightForeArm", "RightArm", [-27.4, 0, 0]), ("RightHand", "RightForeArm", [-27.6, 0, 0]),
    ("RightHandMiddle1", "RightHand", [-12.8, 0, 0]),
    ("LeftUpLeg", "Hips", [9.1, -6.7, -0.1]), ("LeftLeg", "LeftUpLeg", [0.2, -40.6, -0.5]),
    ("LeftFoot", "LeftLeg", [-0.2, -42, -2.1]), ("LeftToeBase", "LeftFoot", [0.4, -10.5, 12.6]),
    ("LeftToe_End", "LeftToeBase", [0, 0, 10]),
    ("RightUpLeg", "Hips", [-9.1, -6.7, -0.1]), ("RightLeg", "RightUpLeg", [-0.2, -40.6, -0.5]),
    ("RightFoot", "RightLeg", [0.2, -42, -2.1]), ("RightToeBase", "RightFoot", [-0.4, -10.5, 12.6]),
    ("RightToe_End", "RightToeBase", [0, 0, 10]),
]


# --- a binary FBX writer, just what fbx.py reads -------------------------------------------------

def _p(v) -> bytes:
    if isinstance(v, str):
        b = v.encode()
        return b"S" + struct.pack("<I", len(b)) + b
    if isinstance(v, bool):
        return b"C" + struct.pack("<?", v)
    if isinstance(v, int):
        return b"L" + struct.pack("<q", v)
    if isinstance(v, float):
        return b"D" + struct.pack("<d", v)
    if isinstance(v, np.ndarray):
        code, dt = {"f": (b"f", "<f4"), "d": (b"d", "<f8"), "l": (b"l", "<i8")}[v.dtype.kind if v.dtype.kind != "i" else "l"]
        raw = np.ascontiguousarray(v, dtype=dt).tobytes()
        packed = zlib.compress(raw)
        return code + struct.pack("<III", len(v), 1, len(packed)) + packed
    raise TypeError(type(v))


def _node(name: str, props=(), children=(), at: int = 0) -> bytes:
    pb = b"".join(_p(v) for v in props)
    head_len = 12 + 1 + len(name)
    body = b""
    for c in children:
        body += _node(*c, at=at + head_len + len(pb) + len(body))
    if children:
        body += b"\0" * 13
    end = at + head_len + len(pb) + len(body)
    return struct.pack("<III", end, len(props), len(pb)) + bytes([len(name)]) + name.encode() + pb + body


def _P(name, kind, *vals):
    return ("P", [name, kind, "", "A", *vals])


def write_fbx(path: Path, seconds: float, euler: dict, hips_pos=None, fps: int = 30, pre: dict | None = None,
              prefix: str = "mixamorig:") -> Path:
    """A Mixamo-style "without skin" FBX: SKELETON and one take; ``euler`` is bone -> (F, 3)
    degrees (XYZ order), ``pre`` bone -> PreRotation degrees."""
    n = int(seconds * fps)
    kt = (np.arange(n) * (fbx.KTIME_PER_S // fps)).astype(np.int64)
    objects, conns, uid = [], [], [1000]

    def new():
        uid[0] += 1
        return uid[0]
    ids = {}
    for name, parent, t in SKELETON:
        i = ids[name] = new()
        props = [_P("Lcl Translation", "Lcl Translation", *map(float, t))]
        if pre and name in pre:
            props.append(_P("PreRotation", "Vector3D", *map(float, pre[name])))
        objects.append(("Model", [i, f"Model::{prefix}{name}\x00\x01Model", "LimbNode"],
                        [("Version", [232]), ("Properties70", [], props)]))
        conns.append(("C", ["OO", i, ids[parent] if parent else 0]))

    def animate(bone, what, values):
        cn = new()
        objects.append(("AnimationCurveNode", [cn, "AnimCurveNode::" + what[4], ""],
                        [("Properties70", [], [_P(f"d|{a}", "Number", float(values[0, k])) for k, a in enumerate("XYZ")])]))
        conns.append(("C", ["OP", cn, ids[bone], what]))
        for k, a in enumerate("XYZ"):
            c = new()
            objects.append(("AnimationCurve", [c, "AnimCurve::", ""],
                            [("KeyTime", [kt]), ("KeyValueFloat", [np.asarray(values[:, k], np.float32)])]))
            conns.append(("C", ["OP", c, cn, f"d|{a}"]))
    for bone, e in euler.items():
        animate(bone, "Lcl Rotation", np.asarray(e, float))
    if hips_pos is not None:
        animate("Hips", "Lcl Translation", np.asarray(hips_pos, float))
    head = fbx.MAGIC + b"\x1a\x00" + struct.pack("<I", 7400)
    body, at = b"", len(head)
    for top in [("FBXHeaderExtension", [], [("FBXVersion", [7400])]), ("Objects", [], objects), ("Connections", [], conns)]:
        b = _node(*top, at=at)
        body += b
        at += len(b)
    path.write_bytes(head + body + b"\0" * 13)
    return path


def axis_angle(axis, angle):
    axis = np.asarray(axis, float) / np.linalg.norm(axis)
    a = np.asarray(angle, float)[..., None] / 2
    return np.concatenate([axis * np.sin(a), np.cos(a)], axis=-1)


def angle_about(q, axis):
    s = np.sum(q[..., :3] * np.asarray(axis, float), axis=-1)
    return 2 * np.arctan2(s, q[..., 3])


# --- reading -----------------------------------------------------------------------------------

def test_euler_order_is_fbx_s():
    # "XYZ": about X first, then Y, then Z (R = Rz Ry Rx), as Maya and Mixamo mean it.
    q = fbx.euler_q(np.array([90.0, 90.0, 0.0]))
    want = cv.qmul(axis_angle([0, 1, 0], np.pi / 2), axis_angle([1, 0, 0], np.pi / 2))
    assert np.allclose(np.abs(np.sum(q * want)), 1, atol=1e-9)
    q_zyx = fbx.euler_q(np.array([90.0, 90.0, 0.0]), order=5)
    want_zyx = cv.qmul(axis_angle([1, 0, 0], np.pi / 2), axis_angle([0, 1, 0], np.pi / 2))
    assert np.allclose(np.abs(np.sum(q_zyx * want_zyx)), 1, atol=1e-9)


def test_the_skeleton_and_its_keys_are_read(tmp_path):
    secs, fps = 2.0, 30
    t = np.arange(int(secs * fps)) / fps
    arm = np.stack([np.zeros_like(t), np.zeros_like(t), 40 * np.sin(2 * np.pi * t)], axis=1)
    hips = np.stack([np.zeros_like(t), 99.7 + 3 * np.sin(4 * np.pi * t), np.zeros_like(t)], axis=1)
    clip = fbx.load(write_fbx(tmp_path / "a.fbx", secs, {"LeftArm": arm}, hips), fps=30)
    names = clip["names"]
    assert names[0] == "mixamorig:Hips" and len(names) == len(SKELETON)
    assert names[clip["parent"][names.index("mixamorig:LeftForeArm")]] == "mixamorig:LeftArm"
    assert np.allclose(clip["rest_t"][names.index("mixamorig:LeftForeArm")], [27.4, 0, 0])
    i = names.index("mixamorig:LeftArm")
    assert np.allclose(np.degrees(angle_about(clip["rot"][:59, i], [0, 0, 1])), arm[:59, 2], atol=0.01)
    assert np.allclose(clip["pos"][:59, 0, 1], hips[:59, 1], atol=1e-3)


def test_a_pre_rotation_comes_before_the_keys(tmp_path):
    clip = fbx.load(write_fbx(tmp_path / "p.fbx", 1.0, {"LeftArm": np.tile([0, 0, 30.0], (30, 1))},
                              pre={"LeftArm": [0, 0, 10]}))
    i = clip["names"].index("mixamorig:LeftArm")
    assert np.allclose(np.degrees(angle_about(clip["rot"][:, i], [0, 0, 1])), 40, atol=1e-6)
    assert np.allclose(np.degrees(angle_about(clip["rest_r"][i], [0, 0, 1])), 10, atol=1e-6)


def test_ascii_or_other_files_are_refused(tmp_path):
    with pytest.raises(ValueError, match="FBX Binary"):
        fbx.parse(b"; FBX 7.4.0 project file\n")
    with pytest.raises(ValueError, match="not a binary FBX"):
        fbx.parse(b"glTF....")


# --- converting --------------------------------------------------------------------------------

@pytest.mark.parametrize("prefix", ["mixamorig:", "mixamorig1:", ""])
def test_a_mixamo_dance_becomes_a_clip(tmp_path, prefix):
    secs, fps, bpm = 12.0, 30, 120
    t = np.arange(int(secs * fps)) / fps
    swing = 35 * np.sin(2 * np.pi * (bpm / 60) * t)                 # one swing per beat, degrees
    z = np.zeros_like(t)
    arms = {"LeftArm": np.stack([z, z, swing], 1), "RightArm": np.stack([z, z, -swing], 1)}
    bob = np.stack([z, 99.7 - 5 * np.abs(np.sin(np.pi * (bpm / 60) * t)), z], 1)    # 5 cm dips
    src = write_fbx(tmp_path / "club.fbx", secs, arms, bob, prefix=prefix)
    assert cv.skeleton_of(cv.load_clip(src)["names"])[0] == "Mixamo"
    data = cv.build(src, "Club Bounce", "Mixamo, Hip Hop Dancing", "local")
    assert data["license"] == "local" and data["id"] == "club-bounce"
    assert abs(data["bpm"] - bpm) <= 4
    left = np.array(data["bones"]["leftUpperArm"]).reshape(-1, 4)
    assert np.allclose(np.degrees(angle_about(left, [0, 0, 1]))[:300], swing[:300], atol=0.5)
    hips_y = np.array(data["hips"]).reshape(-1, 3)[:, 1]
    assert 0.03 < hips_y.max() - hips_y.min() < 0.07                # 5 cm of a ~1 m leg, not 5 "metres"
    assert np.allclose(data["rest_dirs"]["leftUpperArm"], [1, 0, 0], atol=0.01)
    assert data["rest_dirs"]["leftLowerLeg"][1] < -0.99


def test_the_cli_marks_a_clip_local_unless_told(tmp_path):
    t = np.arange(240) / 30
    z = np.zeros_like(t)
    src = write_fbx(tmp_path / "c.fbx", 8, {"LeftArm": np.stack([z, z, 30 * np.sin(4 * np.pi * t)], 1)})
    assert cv.main([str(src), "--out", str(tmp_path / "out"), "--name", "Mine", "--source", "Mixamo"]) == 0
    import json
    assert json.loads((tmp_path / "out" / "mine.json").read_text())["license"] == "local"


def test_a_skeleton_missing_bones_says_which(tmp_path):
    clip = {"names": ["mixamorig:Hips", "mixamorig:Spine"], "parent": [-1, 0], "rest_t": np.zeros((2, 3)),
            "rest_r": np.tile([0, 0, 0, 1.0], (2, 1)), "rot": np.tile([0, 0, 0, 1.0], (5, 2, 1)),
            "pos": np.zeros((5, 2, 3)), "fps": 30}
    with pytest.raises(ValueError, match="LeftArm"):
        cv.retarget(clip)


# --- never committed, never baked in -----------------------------------------------------------

def test_local_clips_cannot_be_committed_or_built_into_the_image():
    ignored = subprocess.run(["git", "check-ignore", "-q", "data/dance/club-bounce.json"], cwd=ROOT)
    assert ignored.returncode == 0                                      # git won't add it
    lines = [ln.strip() for ln in (ROOT / ".containerignore").read_text().splitlines()]
    assert "data/" in lines                                             # the build context leaves data/ out
    assert not any(ln.startswith("!data/dance") for ln in lines)
