"""List a VRM's materials and their textures, and save every texture as PNG.

Usage: flatpak run org.blender.Blender -b --python dump_textures.py -- MODEL.vrm OUT_DIR
Needs the "VRM format" add-on enabled in Blender.
"""
import bpy, os, sys
SRC, OUT = sys.argv[sys.argv.index("--") + 1:][:2]
os.makedirs(OUT, exist_ok=True)
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)
bpy.ops.import_scene.vrm(filepath=SRC)
for ob in bpy.data.objects:
    if ob.type != "MESH":
        continue
    for ms in ob.material_slots:
        m = ms.material
        src = m.vrm_addon_extension.mtoon1.pbr_metallic_roughness.base_color_texture.index.source
        print(f"{ob.name:6} {m.name:45} base texture: {getattr(src, 'name', None)}")
for im in bpy.data.images:
    if im.size[0] > 16:
        im.filepath_raw = f"{OUT}/{im.name}.png"; im.file_format = "PNG"; im.save()
print("saved textures to", OUT)
