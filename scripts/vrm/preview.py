"""Shared preview rendering for the VRM scripts (runs inside Blender).

Camera numbers suit a VRoid model of Butler's height (head ~1.54 m)."""
import bpy, math, os


def setup(res=420):
    sc = bpy.context.scene
    for eng in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
        try:
            sc.render.engine = eng; break
        except TypeError:
            pass
    sc.render.resolution_x = sc.render.resolution_y = res
    w = bpy.data.worlds.new("preview"); sc.world = w; w.use_nodes = True
    bg = next(n for n in w.node_tree.nodes if n.type == "BACKGROUND")
    bg.inputs[0].default_value = (.8, .8, .8, 1); bg.inputs[1].default_value = 1.5
    cam = bpy.data.cameras.new("preview"); cam.type = "ORTHO"
    co = bpy.data.objects.new("preview", cam); sc.collection.objects.link(co); sc.camera = co


def shot(out_dir, tag, keys, values, scale, z, yaw=0):
    """Render one still with the given shape key values. yaw in degrees, 0 = front."""
    os.makedirs(out_dir, exist_ok=True)
    sc = bpy.context.scene; co = sc.camera
    for kb in keys:
        kb.value = 0
    for k, v in values.items():
        keys[k].value = v
    co.data.ortho_scale = scale; a = math.radians(yaw)
    co.location = (math.sin(a), -math.cos(a), z); co.rotation_euler = (math.radians(90), 0, a)
    sc.render.filepath = f"{out_dir}/{tag}.png"
    bpy.ops.render.render(write_still=True)
