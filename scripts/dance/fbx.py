"""A binary FBX reader, just enough for a skeleton's animation (#121): Mixamo's downloads.

Plain Python and numpy, no Blender or FBX SDK. Reads the node tree, the models (bones) with
their rest transform, and the animation curves on their translation and rotation; gives the
same clip as ``convert.load_clip`` does for a GLB:

    names, parent, rest_t, rest_r, rot (F, N, 4), pos (F, N, 3), fps

Only what a mocap skeleton uses: Euler rotation orders, pre- and post-rotation, and the file's
unit (Mixamo's centimetres stay centimetres: the converter measures the hips in leg lengths).
ASCII FBX is refused; Mixamo's "FBX Binary" is the default.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

KTIME_PER_S = 46186158000
MAGIC = b"Kaydara FBX Binary  \x00"


# --- the node tree -----------------------------------------------------------------------------

class Node:
    __slots__ = ("name", "props", "children")

    def __init__(self, name, props, children):
        self.name, self.props, self.children = name, props, children

    def find(self, name):
        return next((c for c in self.children if c.name == name), None)

    def all(self, name):
        return [c for c in self.children if c.name == name]

    def __repr__(self):
        return f"Node({self.name!r}, {self.props[:3]!r}, {len(self.children)} children)"


_ARRAY = {b"f": "<f4", b"d": "<f8", b"l": "<i8", b"i": "<i4", b"b": "<u1"}
_SCALAR = {b"Y": "<h", b"C": "<?", b"I": "<i", b"F": "<f", b"D": "<d", b"L": "<q"}


def _prop(b: bytes, o: int):
    t = b[o:o + 1]
    o += 1
    if t in _SCALAR:
        fmt = _SCALAR[t]
        size = struct.calcsize(fmt)
        return struct.unpack_from(fmt, b, o)[0], o + size
    if t in _ARRAY:
        n, enc, clen = struct.unpack_from("<III", b, o)
        o += 12
        raw = b[o:o + clen]
        if enc == 1:
            raw = zlib.decompress(raw)
        return np.frombuffer(raw, dtype=_ARRAY[t], count=n).copy(), o + clen
    if t in (b"S", b"R"):
        n, = struct.unpack_from("<I", b, o)
        o += 4
        v = b[o:o + n]
        return (v.decode("utf-8", "replace") if t == b"S" else v), o + n
    raise ValueError(f"FBX: unknown property type {t!r} at {o - 1}")


def _node(b: bytes, o: int, wide: bool):
    if wide:
        end, nprops, _plen = struct.unpack_from("<QQQ", b, o)
        o += 24
    else:
        end, nprops, _plen = struct.unpack_from("<III", b, o)
        o += 12
    nlen = b[o]
    o += 1
    if end == 0:                                       # the null record ending a list
        return None, o + nlen
    name = b[o:o + nlen].decode("ascii", "replace")
    o += nlen
    props = []
    for _ in range(nprops):
        v, o = _prop(b, o)
        props.append(v)
    children = []
    while o < end:
        c, o2 = _node(b, o, wide)
        if c is None:
            o = o2
            break
        children.append(c)
        o = o2
    return Node(name, props, children), end


def parse(data: bytes) -> Node:
    if not data.startswith(MAGIC):
        if data[:5] == b"; FBX":
            raise ValueError("an ASCII FBX: download it as \"FBX Binary\"")
        raise ValueError("not a binary FBX file")
    version, = struct.unpack_from("<I", data, 23)
    wide = version >= 7500
    o, top = 27, []
    while o < len(data):
        n, o = _node(data, o, wide)
        if n is None:
            break
        top.append(n)
    return Node("", [version], top)


def properties(node: Node) -> dict:
    """A Properties70 block as {name: values}."""
    p70 = node.find("Properties70")
    out = {}
    for p in (p70.all("P") if p70 else []):
        out[p.props[0]] = p.props[4:]
    return out


# --- rotations ---------------------------------------------------------------------------------

def _axis_q(axis: int, deg: np.ndarray) -> np.ndarray:
    h = np.radians(np.asarray(deg, dtype=float)) / 2
    q = np.zeros(h.shape + (4,))
    q[..., axis] = np.sin(h)
    q[..., 3] = np.cos(h)
    return q


def _qmul(a, b):
    ax, ay, az, aw = np.moveaxis(a, -1, 0)
    bx, by, bz, bw = np.moveaxis(b, -1, 0)
    return np.stack([aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw,
                     aw * bw - ax * bx - ay * by - az * bz], axis=-1)


# FBX RotationOrder: eEulerXYZ = 0 ... eEulerZYX = 5; "XYZ" turns about X first (R = Rz Ry Rx).
_ORDERS = ["XYZ", "XZY", "YZX", "YXZ", "ZXY", "ZYX"]


def euler_q(deg: np.ndarray, order: int = 0) -> np.ndarray:
    """(..., 3) Euler degrees (x, y, z) -> (..., 4) quaternions, in FBX's rotation order."""
    deg = np.asarray(deg, dtype=float)
    q = None
    for ax in _ORDERS[order]:                          # first axis applied first: leftmost-last
        i = "XYZ".index(ax)
        r = _axis_q(i, deg[..., i])
        q = r if q is None else _qmul(r, q)
    return q


# --- the clip ----------------------------------------------------------------------------------

def _vec(props: dict, name: str, default=(0.0, 0.0, 0.0)) -> np.ndarray:
    v = props.get(name)
    return np.array(v[:3] if v else default, dtype=float)


def _curve_values(curve: Node, grid_s: np.ndarray, default: float) -> np.ndarray:
    times = curve.find("KeyTime")
    vals = curve.find("KeyValueFloat")
    if times is None or vals is None or not len(times.props[0]):
        return np.full(len(grid_s), default)
    t = np.asarray(times.props[0], dtype=np.float64) / KTIME_PER_S
    v = np.asarray(vals.props[0], dtype=np.float64)
    return np.interp(grid_s, t, v)                      # linear; Mixamo keys every frame


def load(path: Path, fps: int = 30) -> dict:
    """The skeleton (every LimbNode/Root/Null model under the scene, parents first) and its
    first animation, resampled to ``fps``."""
    root = parse(Path(path).read_bytes())
    objects = root.find("Objects")
    conns = root.find("Connections")
    if objects is None or conns is None:
        raise ValueError("FBX: no objects")
    by_id = {}
    for n in objects.children:
        if n.props and isinstance(n.props[0], int):
            by_id[n.props[0]] = n
    models = {i: n for i, n in by_id.items() if n.name == "Model"
              and len(n.props) > 2 and n.props[2] in ("LimbNode", "Root", "Null")}
    parent_of, curve_nodes, curves = {}, {}, {}
    for c in conns.all("C"):
        kind, child, par = c.props[0], c.props[1], c.props[2]
        if kind == "OO" and child in models and par in models:
            parent_of[child] = par
        elif kind == "OP" and child in by_id and by_id[child].name == "AnimationCurveNode" and par in models:
            curve_nodes[child] = (par, c.props[3])          # "Lcl Rotation" / "Lcl Translation"
        elif kind == "OP" and child in by_id and by_id[child].name == "AnimationCurve":
            curves.setdefault(par, {})[c.props[3]] = by_id[child]   # "d|X" ...
    # Keep only bones: models with a LimbNode below or above them (drops a stray Null).
    ids = list(models)
    order, seen = [], set()

    def visit(i):
        if i in seen:
            return
        p = parent_of.get(i)
        if p is not None:
            visit(p)
        seen.add(i)
        order.append(i)
    for i in ids:
        visit(i)
    index = {m: k for k, m in enumerate(order)}

    def name_of(m):
        return models[m].props[1].split("\x00")[0].removeprefix("Model::")

    # Time span.
    t_end = 0.0
    for per in curves.values():
        for cv in per.values():
            kt = cv.find("KeyTime")
            if kt is not None and len(kt.props[0]):
                t_end = max(t_end, float(kt.props[0][-1]) / KTIME_PER_S)
    if t_end <= 0:
        raise ValueError("FBX: no animation")
    frames = int(np.floor(t_end * fps)) + 1
    grid = np.arange(frames) / fps

    n = len(order)
    names = [name_of(m) for m in order]
    parent = [index[parent_of[m]] if m in parent_of else -1 for m in order]
    rest_t, rest_r = np.zeros((n, 3)), np.zeros((n, 4))
    pos, rot = np.zeros((frames, n, 3)), np.zeros((frames, n, 4))
    pre_q, post_qi, rorder = [], [], []
    eul = np.zeros((frames, n, 3))
    for k, m in enumerate(order):
        p = properties(models[m])
        ro = int(p.get("RotationOrder", [0])[0]) if p.get("RotationOrder") else 0
        pre = euler_q(_vec(p, "PreRotation")) if p.get("RotationActive", [1])[0] else euler_q(np.zeros(3))
        post = euler_q(_vec(p, "PostRotation")) if p.get("RotationActive", [1])[0] else euler_q(np.zeros(3))
        post_i = post * np.array([-1, -1, -1, 1.0])
        lt, lr = _vec(p, "Lcl Translation"), _vec(p, "Lcl Rotation")
        rest_t[k] = lt
        rest_r[k] = _qmul(_qmul(pre, euler_q(lr, ro)), post_i)
        pos[:, k] = lt
        eul[:, k] = lr
        pre_q.append(pre)
        post_qi.append(post_i)
        rorder.append(ro)
    for cn, (m, what) in curve_nodes.items():
        if m not in index:
            continue
        k = index[m]
        per = curves.get(cn, {})
        defaults = properties(by_id[cn])
        for a, axis in enumerate("XYZ"):
            cv = per.get(f"d|{axis}")
            d = defaults.get(f"d|{axis}")
            if what == "Lcl Rotation":
                eul[:, k, a] = _curve_values(cv, grid, d[0] if d else eul[0, k, a]) if cv else eul[:, k, a]
            elif what == "Lcl Translation":
                pos[:, k, a] = _curve_values(cv, grid, d[0] if d else pos[0, k, a]) if cv else pos[:, k, a]
    for k in range(n):
        rot[:, k] = _qmul(_qmul(np.broadcast_to(pre_q[k], (frames, 4)), euler_q(eul[:, k], rorder[k])),
                          np.broadcast_to(post_qi[k], (frames, 4)))
    return {"names": names, "parent": parent, "rest_t": rest_t, "rest_r": rest_r,
            "rot": rot, "pos": pos, "fps": fps}
