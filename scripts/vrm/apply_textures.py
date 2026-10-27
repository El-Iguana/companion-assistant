"""Swap edited textures into a VRM and export it.

Usage: flatpak run org.blender.Blender -b --python apply_textures.py -- IN.vrm OUT.vrm TEX_DIR [PREVIEW_DIR]
Every TEX_DIR/<name>.png replaces the model's image <name> (same size required).
Pixels are copied in and packed; reloading from a path does not work here, because
the images are packed inside the imported file and reload() reads the packed copy.
"""
import bpy, sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
args = sys.argv[sys.argv.index("--") + 1:]
SRC, OUT, TEX = args[:3]
PREV = args[3] if len(args) > 3 else None

for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
bpy.ops.import_scene.vrm(filepath=SRC)
arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
for path in sorted(glob.glob(f"{TEX}/*.png")):
    n = os.path.splitext(os.path.basename(path))[0]
    im = bpy.data.images.get(n)
    if im is None:
        print("no image named", n, "- skipped"); continue
    new = bpy.data.images.load(path)
    assert tuple(new.size) == tuple(im.size), f"{n}: size {tuple(new.size)} != {tuple(im.size)}"
    im.pixels.foreach_set(list(new.pixels)); im.update(); im.pack()
    bpy.data.images.remove(new)
    print("replaced", n)
arm.data.vrm_addon_extension.vrm1.meta.vrm_name = os.path.splitext(os.path.basename(OUT))[0]

keys = bpy.data.objects["Face"].data.shape_keys.key_blocks
if PREV:
    from preview import setup, shot
    setup()
    shot(PREV, "1_neutral", keys, {}, 0.19, 1.545)
    shot(PREV, "2_aa", keys, {"Fcl_MTH_A": 1}, 0.19, 1.545)
    shot(PREV, "3_happy", keys, {"Fcl_ALL_Joy": 1}, 0.19, 1.545)
    shot(PREV, "4_three_quarter", keys, {}, 0.30, 1.52, 35)
    shot(PREV, "5_back", keys, {}, 0.30, 1.52, 160)
for kb in keys:
    kb.value = 0
print("export:", bpy.ops.export_scene.vrm(filepath=OUT))
