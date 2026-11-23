"""VRM model inspection.

A .vrm is a .glb (binary glTF) carrying VRM extensions, so the whole audit is a
matter of reading the JSON chunk out of the container -- no 3D library, no
dependency. That matters here: this runs in the API process, on a file someone
just uploaded, and it must not need the GPU or a parser we do not control.

What it reports is what actually decides whether a model is usable as an avatar:
the spec version, the author's own license flags, whether the humanoid rig is
complete, which expression presets exist, and whether there are spring bones.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

GLB_MAGIC = 0x46546C67
CHUNK_JSON = 0x4E4F534A

# The humanoid bones an avatar needs before any of the animation channels work.
REQUIRED_BONES = [
    "hips", "spine", "chest", "neck", "head",
    "leftUpperArm", "leftLowerArm", "leftHand",
    "rightUpperArm", "rightLowerArm", "rightHand",
    "leftUpperLeg", "leftLowerLeg", "leftFoot",
    "rightUpperLeg", "rightLowerLeg", "rightFoot",
]
# VRM 1.0 preset names. 0.x uses its own vocabulary, normalised below, because
# three-vrm presents both to the renderer under the 1.0 names.
EMOTIVE = ["happy", "angry", "sad", "relaxed", "surprised"]
VISEMES = ["aa", "ih", "ou", "ee", "oh"]
_V0_EMOTIVE = {"joy": "happy", "angry": "angry", "sorrow": "sad", "fun": "relaxed"}
_V0_VISEME = {"a": "aa", "i": "ih", "u": "ou", "e": "ee", "o": "oh"}

MAX_BYTES = 120 * 1024 * 1024      # generous; real models run 10-60 MB (service.py raises
                                    # pytincture's request cap to match)
MAX_JSON_BYTES = 32 * 1024 * 1024  # a glTF JSON chunk this big is not a model we want


class NotAVrm(ValueError):
    """The file is not a glb container, or carries no VRM extension."""


def _gltf_json(data: bytes) -> dict:
    if len(data) < 12:
        raise NotAVrm("file is too short to be a glb")
    magic, _version, _length = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC:
        raise NotAVrm("not a glb/vrm container (bad magic)")
    off = 12
    while off + 8 <= len(data):
        clen, ctype = struct.unpack_from("<II", data, off)
        off += 8
        if ctype == CHUNK_JSON:
            try:
                return json.loads(data[off:off + clen].decode("utf-8"))
            except Exception as e:                       # noqa: BLE001
                raise NotAVrm(f"JSON chunk is unreadable: {e}") from e
        off += clen
    raise NotAVrm("no JSON chunk in the container")


def _v1(vrm: dict, ext: dict) -> dict:
    meta = vrm.get("meta") or {}
    exprs = vrm.get("expressions") or {}
    names = set(exprs.get("preset") or {}) | set(exprs.get("custom") or {})
    springs = len((ext.get("VRMC_springBone") or {}).get("springs") or [])
    return {
        "spec": "1.0",
        "title": meta.get("name") or "",
        "author": ", ".join(meta.get("authors") or []),
        "license": {
            "redistribution": meta.get("allowRedistribution"),
            "modification": meta.get("modification"),
            "allowed_user": meta.get("avatarPermission"),
            "sexual": meta.get("allowExcessivelySexualUsage"),
            "violent": meta.get("allowExcessivelyViolentUsage"),
            "commercial": meta.get("commercialUsage"),
            "credit": meta.get("creditNotation"),
            "url": meta.get("licenseUrl") or meta.get("otherLicenseUrl") or "",
        },
        "bones": set((vrm.get("humanoid") or {}).get("humanBones") or {}),
        "expressions": names,
        "emotive": [e for e in EMOTIVE if e in names],
        "visemes": [v for v in VISEMES if v in names],
        "blink": any("blink" in n.lower() for n in names),
        "look_at": bool(vrm.get("lookAt")),
        "springs": springs,
    }


def _v0(vrm: dict) -> dict:
    meta = vrm.get("meta") or {}
    groups = (vrm.get("blendShapeMaster") or {}).get("blendShapeGroups") or []
    raw = {(g.get("presetName") or g.get("name") or "").lower() for g in groups}
    raw.discard("")
    names = {_V0_EMOTIVE.get(n, _V0_VISEME.get(n, n)) for n in raw}
    sec = vrm.get("secondaryAnimation") or {}
    # 0.x spells several of these "Ussage"; that is the spec's own typo, not ours.
    def m(*keys):
        for k in keys:
            if meta.get(k) is not None:
                return meta[k]
        return None
    return {
        "spec": "0.x",
        "title": meta.get("title") or "",
        "author": meta.get("author") or "",
        "license": {
            "redistribution": m("licenseName"),
            "modification": m("licenseName"),
            "allowed_user": m("allowedUserName"),
            "sexual": m("sexualUssageName", "sexualUsageName"),
            "violent": m("violentUssageName", "violentUsageName"),
            "commercial": m("commercialUssageName", "commercialUsageName"),
            "credit": None,
            "url": meta.get("otherLicenseUrl") or "",
        },
        "bones": {b.get("bone") for b in ((vrm.get("humanoid") or {}).get("humanBones") or [])
                  if b.get("bone")},
        "expressions": names,
        "emotive": [e for e in EMOTIVE if e in names],
        "visemes": [v for v in VISEMES if v in names],
        "blink": any("blink" in n for n in raw),
        "look_at": bool(vrm.get("firstPerson")),
        "springs": len(sec.get("boneGroups") or []),
    }


def audit(data: bytes) -> dict:
    """Inspect VRM bytes. Raises NotAVrm if it is not one."""
    g = _gltf_json(data)
    ext = g.get("extensions") or {}
    if "VRMC_vrm" in ext:
        out = _v1(ext["VRMC_vrm"], ext)
    elif "VRM" in ext:
        out = _v0(ext["VRM"])
    else:
        raise NotAVrm("glb has no VRM extension (a plain glTF, not an avatar)")

    bones = out.pop("bones")
    out["missing_bones"] = [b for b in REQUIRED_BONES if b not in bones]
    out["rig_complete"] = not out["missing_bones"]
    out["expressions"] = sorted(out["expressions"])

    accessors = g.get("accessors") or []
    tris = 0
    for mesh in g.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            idx = prim.get("indices")
            if idx is not None and idx < len(accessors):
                tris += (accessors[idx].get("count") or 0) // 3
    out["triangles"] = tris
    out["materials"] = len(g.get("materials") or [])
    out["size"] = len(data)

    # Usable at all? These are the things the renderer genuinely needs; anything
    # else is a quality note rather than a blocker.
    problems = []
    if out["missing_bones"]:
        problems.append(f"incomplete rig, missing {len(out['missing_bones'])} required bones")
    if not out["emotive"]:
        problems.append("no emotive expression presets")
    if not out["visemes"]:
        problems.append("no visemes — the mouth cannot be driven")
    out["problems"] = problems
    out["usable"] = not out["missing_bones"] and bool(out["visemes"])

    # Notes, not judgements: the flags are the author's declaration and it is the
    # operator who decides what to do about them.
    notes = []
    if not out["springs"]:
        notes.append("no spring bones — hair and cloth will not move")
    if not out["blink"]:
        notes.append("no blink shape")
    if len(out["emotive"]) < len(EMOTIVE):
        notes.append("missing presets: " + ", ".join(e for e in EMOTIVE if e not in out["emotive"]))
    out["notes"] = notes
    return out


def _head(path: Path) -> bytes:
    """The bytes audit() needs from a file: the 12-byte glb header and the chunks up to and
    including the JSON one (which glb puts first). A 60 MB model's JSON is a few hundred KB,
    so the textures and meshes are never read into memory."""
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12:
            return head
        out = bytearray(head)
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                return bytes(out)
            clen, ctype = struct.unpack("<II", ch)
            out += ch
            if ctype == CHUNK_JSON:
                if clen > MAX_JSON_BYTES:
                    raise NotAVrm("the model's JSON chunk is implausibly large")
                out += f.read(clen)
                return bytes(out)
            f.seek(clen, 1)                     # skip a non-JSON chunk (none, in practice)


def audit_file(path: str | Path) -> dict:
    p = Path(path)
    report = audit(_head(p))
    report["size"] = p.stat().st_size           # audit() saw only the head
    return {"name": p.name, **report}
