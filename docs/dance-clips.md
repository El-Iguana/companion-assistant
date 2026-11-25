# Adding your own dances

Mira dances to music with clips from the CMU motion-capture library. You can add your own clips
too, for example club or hip-hop dances from [Mixamo](https://www.mixamo.com) (free with an
Adobe account). These clips stay in **your** library: Mixamo's terms don't allow sharing the
animation files, so they are never committed to the repo or built into the image.

## 1. Download from Mixamo

1. Pick an animation (search "dance", "hip hop", "samba", ...). Turn **In Place** on if it's offered.
2. Click **Download** and choose:
   - **Format:** FBX Binary (.fbx)
   - **Skin:** Without Skin
   - **Frames per second:** 30
   - **Keyframe Reduction:** none

## 2. Convert it

On the server (amd-halo), from the repo:

```bash
.venv/bin/python scripts/dance/convert.py ~/Downloads/"Hip Hop Dancing.fbx" \
    --name "Hip Hop" --source "Mixamo, Hip Hop Dancing"
```

This writes `data/dance/hip-hop.json` and prints the tempo it measured. The clip is marked
`"license": "local"` (the default).

Useful options:

| Option | What it does |
|--------|--------------|
| `--bpm 96` | sets the clip's tempo when the measured one is wrong (often double or half) |
| `--from 4 --to 20` | uses only that stretch of the take, in seconds |
| `--parts lower` | keeps only the hips and legs; her arms keep the simple bounce |
| `--keep-heading` | keeps the dancer's turns (by default she keeps facing you) |

## 3. Use it

Open her **Settings** window. The new dance is in the **Dance** list; pick it, and adjust **Tempo**
until her steps match a song you know. Auto mode also uses it for songs whose tempo fits.

## Removing a clip

Delete its file from `data/dance/`.
