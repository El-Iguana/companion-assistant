"""Add custom VRM 1.0 expressions (recipes.py) to a VRoid model and export it.

Usage: flatpak run org.blender.Blender -b --python add_expressions.py -- IN.vrm OUT.vrm [PREVIEW_DIR]
Each custom expression is named after a Pantheon emotion label, which is what
static/js/vtuber.mjs looks for. With PREVIEW_DIR, renders a front face shot per
expression so the faces can be checked by eye. Needs the "VRM format" add-on.
"""
import bpy, sys, os, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recipes import RECIPES, BLINK_BLEND
args = sys.argv[sys.argv.index("--") + 1:]
SRC, OUT = args[:2]
PREV = args[2] if len(args) > 2 else None

for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
bpy.ops.import_scene.vrm(filepath=SRC)
arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
face = bpy.data.objects["Face"]
keys = face.data.shape_keys.key_blocks
ex = arm.data.vrm_addon_extension.vrm1.expressions
existing = {c.custom_name for c in ex.custom}
for name, r in RECIPES.items():
    if name in existing:
        print("skip, already present:", name); continue
    c = ex.custom.add(); c.custom_name = name
    for k, w in r.items():
        assert "Fcl_" + k in keys, f"model has no shape key Fcl_{k}"
        b = c.morph_target_binds.add(); b.node.mesh_object_name = "Face"; b.index = "Fcl_" + k; b.weight = w
    if name in BLINK_BLEND:
        c.override_blink = "blend"
arm.data.vrm_addon_extension.vrm1.meta.vrm_name = os.path.splitext(os.path.basename(OUT))[0]
print("custom expressions:", [c.custom_name for c in ex.custom])

if PREV:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from preview import setup, shot
    setup()
    shot(PREV, "00_neutral", keys, {}, 0.19, 1.545)
    for n, r in RECIPES.items():
        shot(PREV, n, keys, {"Fcl_" + k: v for k, v in r.items()}, 0.19, 1.545)
for kb in keys:
    kb.value = 0
print("export:", bpy.ops.export_scene.vrm(filepath=OUT))
