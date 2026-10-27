"""Paint an older look onto a VRoid model's textures (no Blender needed).

Usage: python3 paint_aging.py TEX_DIR OUT_DIR
TEX_DIR holds the PNGs from dump_textures.py. Writes edited copies of:
  _04 face skin  - pencil mustache + toon age lines (coordinates are for the
                   VRoid face UV layout, 1024x1024)
  _07 brows      - grey, heavier
  _12 _18 hair   - silver
Texture names are the ones in Butler1/2; check another model's with
dump_textures.py first, VRoid numbers them per export.
"""
import os, sys, math
from PIL import Image, ImageDraw, ImageFilter, ImageChops
SRC, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)
SS = 4  # supersample for clean anti-aliased strokes

def silver(name, lo=150, hi=225):
    im = Image.open(f"{SRC}/{name}.png").convert("RGBA")
    r, g, b, a = im.split()
    L = Image.merge("RGB", (r, g, b)).convert("L")
    mn, mx = L.getextrema(); mx = max(mx, mn + 1)
    # stretch luminance into a silver band, keep strand variation; slight cool tint
    L = L.point(lambda v: int(lo + (v - mn) * (hi - lo) / (mx - mn)))
    out = Image.merge("RGBA", (L.point(lambda v: int(v * .97)), L.point(lambda v: int(v * .985)), L, a))
    out.save(f"{OUT}/{name}.png")

def brows():
    im = Image.open(f"{SRC}/_07.png").convert("RGBA")
    a = im.split()[3]
    a = a.filter(ImageFilter.MaxFilter(5))          # heavier
    a = a.point(lambda v: min(255, int(v * 1.25)))
    col = Image.new("RGBA", im.size, (112, 112, 118, 0)); col.putalpha(a)
    col.save(f"{OUT}/_07.png")

def stroke(d, pts, w0, w1, fill):
    """Tapered polyline: width goes w0 -> w1 along the points."""
    n = len(pts) - 1
    for i in range(n):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        w = (w0 + (w1 - w0) * i / n) * SS
        d.line([(x0 * SS, y0 * SS), (x1 * SS, y1 * SS)], fill=fill, width=max(1, int(w)))
        d.ellipse([x1 * SS - w / 2, y1 * SS - w / 2, x1 * SS + w / 2, y1 * SS + w / 2], fill=fill)

def curve(p0, p1, p2, steps=24):
    return [((1-t)**2*p0[0]+2*(1-t)*t*p1[0]+t*t*p2[0], (1-t)**2*p0[1]+2*(1-t)*t*p1[1]+t*t*p2[1])
            for t in (i/steps for i in range(steps+1))]

def mirror(pts): return [(1024 - x, y) for x, y in pts]

def face():
    base = Image.open(f"{SRC}/_04.png").convert("RGBA")
    W, H = base.size
    # --- age lines: soft, skin-toned, multiplied in
    lines = Image.new("L", (W * SS, H * SS), 0); d = ImageDraw.Draw(lines)
    for side in (lambda p: p, mirror):
        stroke(d, side(curve((466, 686), (410, 716), (414, 792))), 3.6, 1.0, 170)   # smile line
        stroke(d, side(curve((286, 606), (345, 636), (404, 606))), 3.0, 1.4, 190)   # under-eye bag
        for dy, ang in ((-14, -14), (2, 0), (18, 14)):                               # crow's feet
            x0, y0 = 262, 556 + dy
            x1, y1 = x0 - 34 * math.cos(math.radians(ang)), y0 + 34 * math.sin(math.radians(ang))
            stroke(d, side([(x0, y0), ((x0 + x1) / 2, (y0 + y1) / 2 + 1), (x1, y1)]), 3.0, 1.0, 200)
    lines = lines.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(1.1))
    shade = Image.new("RGBA", (W, H), (176, 112, 100, 255))
    face_rgb = base.convert("RGB")
    mult = ImageChops.multiply(face_rgb, shade.convert("RGB"))
    out = Image.composite(mult, face_rgb, lines.point(lambda v: int(v * .75)))
    # --- pencil mustache: two thin blades, gap at the philtrum, sharp ends
    m = Image.new("L", (W * SS, H * SS), 0); d = ImageDraw.Draw(m)
    for side in (lambda p: p, mirror):
        # upper edge and lower edge of one half, from center out to the tip
        top = curve((509, 735), (478, 731), (436, 752), 30)
        bot = curve((509, 746), (480, 745), (436, 752), 30)
        poly = side(top) + side(bot)[::-1]
        d.polygon([(x * SS, y * SS) for x, y in poly], fill=255)
    m = m.resize((W, H), Image.LANCZOS)
    stache = Image.new("RGB", (W, H), (58, 58, 64))
    out = Image.composite(stache, out, m)
    out.putalpha(base.split()[3]); out.save(f"{OUT}/_04.png")

silver("_18"); silver("_12"); brows(); face()
print("painted")
