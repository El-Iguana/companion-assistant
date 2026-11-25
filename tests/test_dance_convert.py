"""#120: scripts/dance/convert.py turns a CMU-style clip into VRM humanoid rotations."""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "dance"))
import convert as cv  # noqa: E402

# A CMU skeleton in T-pose (the names and offsets of codepawl/cmu-mocap-glb's skeleton/ files,
# trimmed): parent, translation.
SKELETON = [
    ("root", -1, [0, 0.87, 0]),
    ("lhipjoint", 0, [0, 0, 0]), ("lfemur", 1, [0.09, -0.08, 0.05]), ("ltibia", 2, [0.12, -0.33, 0]),
    ("lfoot", 3, [0.13, -0.37, 0]), ("ltoes", 4, [0.01, -0.03, 0.1]),
    ("rhipjoint", 0, [0, 0, 0]), ("rfemur", 6, [-0.08, -0.08, 0.05]), ("rtibia", 7, [-0.12, -0.33, 0]),
    ("rfoot", 8, [-0.13, -0.36, 0]), ("rtoes", 9, [-0.01, -0.03, 0.1]),
    ("lowerback", 0, [0, 0, 0]), ("upperback", 11, [0, 0.11, -0.03]), ("thorax", 12, [0, 0.12, 0]),
    ("lowerneck", 13, [0, 0.12, 0.01]), ("upperneck", 14, [0, 0.08, 0.015]), ("head", 15, [0, 0.08, 0]),
    ("head_end", 16, [0, 0.08, 0]),
    ("lclavicle", 13, [0, 0.12, 0.01]), ("lhumerus", 18, [0.12, 0.08, -0.02]), ("lradius", 19, [0.28, 0, 0]),
    ("lwrist", 20, [0.19, 0, 0]), ("lhand", 21, [0.09, 0, 0]),
    ("rclavicle", 13, [0, 0.12, 0.01]), ("rhumerus", 23, [-0.12, 0.08, -0.02]), ("rradius", 24, [-0.28, 0, 0]),
    ("rwrist", 25, [-0.19, 0, 0]), ("rhand", 26, [-0.09, 0, 0]),
]


def axis_angle(axis, angle):
    axis = np.asarray(axis, float) / np.linalg.norm(axis)
    a = np.asarray(angle, float)[..., None] / 2
    return np.concatenate([axis * np.sin(a), np.cos(a)], axis=-1)


def write_glb(path: Path, seconds: float, rotations: dict, root_pos=None, fps: int = 60) -> Path:
    """A GLB with SKELETON and one animation: node name -> (F, 4) rotations."""
    n = int(seconds * fps)
    times = (np.arange(n) / fps).astype(np.float32)
    blobs, accessors, views, channels, samplers = [], [], [], [], []

    def add(arr, typ):
        arr = np.ascontiguousarray(arr, dtype=np.float32)
        off = sum(len(b) for b in blobs)
        blobs.append(arr.tobytes())
        views.append({"buffer": 0, "byteOffset": off, "byteLength": arr.nbytes})
        acc = {"bufferView": len(views) - 1, "componentType": 5126, "count": len(arr), "type": typ}
        if typ == "SCALAR":
            acc.update(min=[float(arr.min())], max=[float(arr.max())])
        accessors.append(acc)
        return len(accessors) - 1
    t_acc = add(times, "SCALAR")
    names = [s[0] for s in SKELETON]
    for name, q in rotations.items():
        samplers.append({"input": t_acc, "output": add(q, "VEC4"), "interpolation": "LINEAR"})
        channels.append({"sampler": len(samplers) - 1, "target": {"node": names.index(name), "path": "rotation"}})
    pos = root_pos if root_pos is not None else np.tile([0, 0.87, 0], (n, 1))
    samplers.append({"input": t_acc, "output": add(pos, "VEC3"), "interpolation": "LINEAR"})
    channels.append({"sampler": len(samplers) - 1, "target": {"node": 0, "path": "translation"}})
    nodes = [{"name": nm, "translation": t} for nm, _, t in SKELETON]
    for i, (_, p, _) in enumerate(SKELETON):
        if p >= 0:
            nodes[p].setdefault("children", []).append(i)
    binc = b"".join(blobs)
    binc += b"\0" * (-len(binc) % 4)
    gltf = {"asset": {"version": "2.0"}, "nodes": nodes, "scenes": [{"nodes": [0]}],
            "buffers": [{"byteLength": len(binc)}], "bufferViews": views, "accessors": accessors,
            "animations": [{"channels": channels, "samplers": samplers}]}
    js = json.dumps(gltf).encode()
    js += b" " * (-len(js) % 4)
    out = b"glTF" + struct.pack("<II", 2, 12 + 8 + len(js) + 8 + len(binc))
    out += struct.pack("<I", len(js)) + b"JSON" + js + struct.pack("<I", len(binc)) + b"BIN\0" + binc
    path.write_bytes(out)
    return path


def angle_about(q, axis):
    """The signed angle of q about an axis (q assumed to turn about it)."""
    axis = np.asarray(axis, float)
    s = np.sum(q[..., :3] * axis, axis=-1)
    return 2 * np.arctan2(s, q[..., 3])


def test_quaternion_basics():
    a, b = axis_angle([0, 1, 0], 0.4), axis_angle([1, 0, 0], -0.7)
    assert np.allclose(cv.qmul(a, cv.qinv(a)), [0, 0, 0, 1], atol=1e-9)
    assert np.allclose(cv.qrot(axis_angle([0, 0, 1], np.pi / 2), np.array([1.0, 0, 0])), [0, 1, 0], atol=1e-9)
    assert np.isclose(cv.qangle(a, cv.qmul(a, b)), 0.7)


def test_an_arm_swing_lands_on_the_vrm_arm_at_its_tempo(tmp_path):
    secs, fps, bpm = 8.0, 60, 120
    t = np.arange(int(secs * fps)) / fps
    swing = 0.6 * np.sin(2 * np.pi * (bpm / 60) * t)           # one swing per beat
    glb = write_glb(tmp_path / "a.glb", secs, {"lhumerus": axis_angle([0, 0, 1], swing)})
    r = cv.retarget(cv.load_clip(glb))
    got = angle_about(r["bones"]["leftUpperArm"], [0, 0, 1])
    want = 0.6 * np.sin(2 * np.pi * (bpm / 60) * np.arange(r["frames"]) / cv.FPS)
    assert np.allclose(got, want, atol=0.02)
    assert np.allclose(cv.qangle(r["bones"]["rightUpperArm"], np.array([0, 0, 0, 1.0])), 0, atol=1e-6)
    assert abs(cv.estimate_bpm(r["bones"], r["fps"]) - 120) <= 3


def test_a_joint_the_vrm_lacks_folds_into_the_next():
    clip = {"names": [s[0] for s in SKELETON], "parent": [s[1] for s in SKELETON],
            "rest_t": np.array([s[2] for s in SKELETON], float), "rest_r": np.tile([0, 0, 0, 1.0], (len(SKELETON), 1)),
            "fps": 30}
    rot = np.tile([0, 0, 0, 1.0], (10, len(SKELETON), 1))
    rot[:, clip["names"].index("upperneck")] = axis_angle([1, 0, 0], 0.3)
    clip.update(rot=rot, pos=np.tile(clip["rest_t"], (10, 1, 1)))
    r = cv.retarget(clip, keep_heading=True)
    assert np.allclose(angle_about(r["bones"]["head"], [1, 0, 0]), 0.3, atol=1e-6)   # upperneck -> head
    assert np.allclose(cv.qangle(r["bones"]["neck"], np.array([0, 0, 0, 1.0])), 0, atol=1e-6)


def test_slow_turns_are_taken_out_and_a_twist_stays(tmp_path):
    secs, fps = 12.0, 60
    t = np.arange(int(secs * fps)) / fps
    turn = np.pi / 2 * np.clip((t - 4) / 4, 0, 1)              # a quarter turn over 4 s
    twist = 0.4 * np.sin(2 * np.pi * 2.0 * t)                   # hips twisting twice a second
    glb = write_glb(tmp_path / "t.glb", secs, {"root": axis_angle([0, 1, 0], turn + twist)})
    kept = cv.retarget(cv.load_clip(glb), keep_heading=True)
    flat = cv.retarget(cv.load_clip(glb))
    mid = slice(int(9 * cv.FPS), int(10 * cv.FPS))              # after the turn, away from the edge
    yaw_kept = angle_about(kept["bones"]["hips"], [0, 1, 0])[mid]
    yaw_flat = angle_about(flat["bones"]["hips"], [0, 1, 0])[mid]
    assert abs(np.mean(yaw_kept) - np.pi / 2) < 0.1             # kept: still facing the new way
    assert abs(np.mean(yaw_flat)) < 0.15                        # taken out: facing the viewer
    assert np.ptp(yaw_flat) > 0.6                               # the twist itself stays


def test_build_writes_a_clip_the_page_can_play(tmp_path):
    secs, fps = 10.0, 60
    t = np.arange(int(secs * fps)) / fps
    swing = 0.5 * np.sin(2 * np.pi * (100 / 120) * t)            # a stop at each end: one move a beat
    pos = np.stack([0.05 * np.sin(2 * np.pi * 0.83 * t), 0.87 + 0.02 * np.sin(2 * np.pi * 1.67 * t), np.zeros_like(t)], 1)
    glb = write_glb(tmp_path / "b.glb", secs, {"lhumerus": axis_angle([0, 0, 1], swing),
                                               "rhumerus": axis_angle([0, 0, 1], -swing)}, root_pos=pos)
    out = cv.main([str(glb), "--out", str(tmp_path / "dance"), "--name", "Test Swing", "--source", "unit test",
                   "--license", "CC0-1.0", "--style", "party"])
    assert out == 0
    d = json.loads((tmp_path / "dance" / "test-swing.json").read_text())
    assert d["format"] == "cantinero-dance/1" and d["id"] == "test-swing" and d["license"] == "CC0-1.0"
    assert abs(d["bpm"] - 100) <= 3 and d["fps"] == 30
    assert set(d["bones"]) >= {"hips", "spine", "leftUpperArm", "rightUpperLeg", "head"}
    assert all(len(v) == 4 * d["frames"] for v in d["bones"].values()) and len(d["hips"]) == 3 * d["frames"]
    s, e = d["loop"]
    beats = (e - s) / d["fps"] * d["bpm"] / 60
    assert abs(beats - round(beats)) < 0.1 and round(beats) in (4, 8, 16)
    leg = d["rest_dirs"]["leftUpperLeg"]
    assert leg[1] < -0.9 and leg[0] > 0.3                       # CMU's legs stand apart
    assert max(abs(v) for v in d["hips"][1::3]) < 0.05          # height in leg lengths, small


def test_a_clip_with_no_beat_needs_one_given(tmp_path):
    glb = write_glb(tmp_path / "still.glb", 5.0, {"lhumerus": axis_angle([0, 0, 1], np.zeros(300))})
    with pytest.raises(ValueError, match="--bpm"):
        cv.build(glb, "Still", "unit test", "CC0-1.0")
    assert cv.build(glb, "Still", "unit test", "CC0-1.0", bpm=60)["bpm"] == 60     # 60-200: as given
    assert cv.build(glb, "Still", "unit test", "CC0-1.0", bpm=40)["bpm"] == 80     # outside: doubled


def test_the_beat_frame_is_where_the_pose_lands():
    fps, bpm = 30, 120
    period = 60 * fps / bpm                                     # 15 frames a beat
    t = np.arange(300)
    # A move each beat that arrives at frame 6 of the beat (6, 21, 36, ...) and holds there:
    # moving for 9 frames, held for 6, to alternate ends.
    u = ((t - 6) % period) / period                              # 0 at the arrival
    moving = np.clip((u - 0.4) / 0.6, 0, 1)                      # the next move starts at 0.4
    ease = moving * moving * (3 - 2 * moving)
    side = np.where(((t - 6) // period) % 2 == 0, 1.0, -1.0)    # A to B, then B to A
    angle = 0.5 * side * (1 - 2 * ease)
    bones = {"leftUpperArm": axis_angle([0, 0, 1], angle), "hips": np.tile([0, 0, 0, 1.0], (300, 1))}
    assert abs(cv.beat_frame(bones, fps, bpm) - 6) <= 1


def test_a_clip_is_one_beat_per_move():
    # The Macarena's arms hit every ~0.37 s, right arm then left: the whole body repeats only
    # every two moves, which read 81 and played her in double time under a 92 BPM song.
    fps = 30
    t = np.arange(12 * fps) / fps
    beat = 60 / 160
    side = np.floor(t / beat) % 2                                # moves alternate arms
    u = (t % beat) / beat
    move = np.clip(u / 0.6, 0, 1)
    ease = move * move * (3 - 2 * move)                          # move, then hold, each beat
    n = np.floor(t / beat)
    left = 0.6 * ((np.floor((n + 1) / 2) % 2) * 2 - 1) * np.where(side == 0, ease, 1)
    right = 0.6 * ((np.floor(n / 2) % 2) * 2 - 1) * np.where(side == 1, ease, 1)
    bones = {"leftUpperArm": axis_angle([0, 0, 1], left), "rightUpperArm": axis_angle([0, 0, 1], right)}
    assert abs(cv.tempo_from_hits(bones, fps) - 160) <= 6
    assert cv.tempo_from_hits({"leftUpperArm": axis_angle([0, 0, 1], np.zeros(60))}, fps) is None


def test_not_a_cmu_skeleton_is_refused(tmp_path):
    clip = {"names": ["a", "b"], "parent": [-1, 0], "rest_t": np.zeros((2, 3)), "rest_r": np.tile([0, 0, 0, 1.0], (2, 1)),
            "rot": np.tile([0, 0, 0, 1.0], (5, 2, 1)), "pos": np.zeros((5, 2, 3)), "fps": 30}
    with pytest.raises(ValueError, match="not a CMU skeleton"):
        cv.retarget(clip)


def test_soften_keeps_the_axis_and_scales_the_angle():
    q = axis_angle([0, 1, 0], np.array([0.8, -0.4]))
    half = cv.soften(q, 0.5)
    assert np.allclose(angle_about(half, [0, 1, 0]), [0.4, -0.2], atol=1e-9)
    assert np.allclose(cv.soften(q, 0.0), [[0, 0, 0, 1.0]] * 2, atol=1e-9)
    assert np.allclose(cv.soften(np.array([[0, 0, 0, 1.0]]), 0.5), [[0, 0, 0, 1.0]])


def test_lower_parts_keep_the_hips_and_legs_and_soften_the_torso(tmp_path):
    secs, fps = 10.0, 60
    t = np.arange(int(secs * fps)) / fps
    swing = 0.5 * np.sin(2 * np.pi * (100 / 120) * t)
    glb = write_glb(tmp_path / "l.glb", secs, {"lhumerus": axis_angle([0, 0, 1], swing),
                                               "lowerback": axis_angle([0, 0, 1], 0.6 * np.ones_like(t)),
                                               "lfemur": axis_angle([1, 0, 0], 0.3 * np.ones_like(t))})
    full = cv.build(glb, "Full", "unit test", "CC0-1.0")
    low = cv.build(glb, "Low", "unit test", "CC0-1.0", parts="lower", torso=0.5)
    assert set(low["bones"]) == {"hips", "spine", "chest", "upperChest", "leftUpperLeg", "leftLowerLeg",
                                 "leftFoot", "leftToes", "rightUpperLeg", "rightLowerLeg", "rightFoot", "rightToes"}
    assert "leftUpperArm" in full["bones"] and "head" in full["bones"]
    # Timing from the whole dance: the arms' tempo, loop and beat, though the arms are gone.
    assert (low["bpm"], low["loop"], low["beat_frame"]) == (full["bpm"], full["loop"], full["beat_frame"])
    spine = np.array(low["bones"]["spine"]).reshape(-1, 4)
    assert np.allclose(angle_about(spine, [0, 0, 1]), 0.3, atol=1e-3)       # half of 0.6
    assert np.allclose(np.array(low["bones"]["leftUpperLeg"]), np.array(full["bones"]["leftUpperLeg"]))
    assert low["parts"] == "lower, torso 0.5" and set(low["rest_dirs"]) <= set(low["bones"])
    with pytest.raises(ValueError):
        cv.only_parts(full["bones"], "upper", 0.5)


def test_a_stretch_can_be_cut_out_of_a_long_take(tmp_path):
    secs, fps = 20.0, 60
    t = np.arange(int(secs * fps)) / fps
    swing = np.where((t >= 8) & (t < 16), 0.5 * np.sin(2 * np.pi * (100 / 120) * t), 0.0)   # a dance mid-take
    glb = write_glb(tmp_path / "long.glb", secs, {"lhumerus": axis_angle([0, 0, 1], swing)})
    whole = cv.build(glb, "Whole", "unit test", "CC0-1.0", bpm=100)
    cut = cv.build(glb, "Cut", "unit test", "CC0-1.0", start_s=8, end_s=16)
    assert whole["frames"] == 600 and cut["frames"] == 240               # 8 s at 30 fps
    assert abs(cut["bpm"] - 100) <= 3                                   # its own tempo, from the dance alone
    first = np.array(cut["bones"]["leftUpperArm"][:4])
    assert np.allclose(first, np.array(whole["bones"]["leftUpperArm"][240 * 4:241 * 4]), atol=1e-3)
    with pytest.raises(ValueError, match="too short"):
        cv.build(glb, "Blink", "unit test", "CC0-1.0", start_s=5, end_s=6)
