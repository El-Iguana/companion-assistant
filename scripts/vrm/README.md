# VRM scripts

Blender (flatpak, with the **VRM format** add-on) scripts that turn a VRoid Studio
export into a Pantheon avatar. They made `data/vrm/Butler2.vrm` and `Butler3.vrm`
from `Butler1.vrm`. The `.vrm` files are git-ignored; these scripts are how to rebuild them.

| script | runs in | what |
|---|---|---|
| `recipes.py` | — | custom expressions: Pantheon emotion label → VRoid `Fcl_*` shape key weights |
| `add_expressions.py` | Blender | adds the recipes as VRM 1.0 custom expressions, optional face previews |
| `dump_textures.py` | Blender | lists materials → texture names, saves every texture as PNG |
| `paint_aging.py` | python3 + Pillow | Butler's older look: silver hair, grey brows, age lines, pencil mustache |
| `apply_textures.py` | Blender | swaps edited PNGs back into the model, optional previews |
| `preview.py` | Blender | shared camera/render helpers |

```bash
B="flatpak run org.blender.Blender -b --python"
cd scripts/vrm
$B add_expressions.py -- ../../data/vrm/Butler1.vrm /tmp/vrm/Butler2.vrm /tmp/vrm/prev_expr
$B dump_textures.py   -- /tmp/vrm/Butler2.vrm /tmp/vrm/tex
python3 paint_aging.py   /tmp/vrm/tex /tmp/vrm/tex_aged
$B apply_textures.py  -- /tmp/vrm/Butler2.vrm /tmp/vrm/Butler3.vrm /tmp/vrm/tex_aged /tmp/vrm/prev_age
```

If Blender's flatpak can't see a path (e.g. under `/tmp`), add `--filesystem=<dir>` after `flatpak run`.

Notes:
- Custom expression names must match emotion labels; `static/js/vtuber.mjs` uses a
  matching custom expression at full weight before falling back to the 5 presets.
- Texture numbers (`_04`, `_18`…) are per export. Run `dump_textures.py` on a new
  model and adjust `paint_aging.py` before painting.
- `paint_aging.py` coordinates are for VRoid's 1024² face UV layout (mouth line at y≈772).
- Texture swaps copy pixels in, because reloading a packed image from a path silently
  keeps the old pixels.
- Check the previews: VRoid's male mouth has little range, so faces lean on brows and eyes.
