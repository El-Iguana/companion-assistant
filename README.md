# La Cantina

A self-hosted, fully-local **character-card AI companion**. FastAPI backend + a no-build vanilla-JS
single-page frontend. All inference runs on your own machine — no cloud, no external services, no
database (state is plain JSON files).

Built around the **Character Card V2/V3** spec (SillyTavern / Chub compatible), so cards import and
export losslessly.

## Features

- **Streaming chat** with swipes (alternate replies) and rolling long-term memory
- **Character Card V2/V3** import/export (PNG + JSON), full in-app card editor with lorebook
- **Personas** — your `{{user}}` card, injected into the prompt
- **Expression sprites** via ComfyUI — one base portrait → per-emotion sprites (same character),
  transparent cutouts, Core-8 / Full-28 sets, with a live mood-driven portrait
- **Relationship system** — multi-axis (affection / trust / attraction / tension), stages,
  intimacy/consent with agency-preserving behavior, milestones, stage-gated lorebook, scenario presets
- **Local voice** — Kokoro + Piper TTS (per-character voice) + push-to-talk & hands-free faster-whisper STT
- **Multi-user** — login with `admin`/`user` roles; each account has its own **private, isolated**
  library of characters, personas, and presets
- **Roleplay vs assistant** chat modes, custom system prompts, and a `/no_think` speed toggle

## Requirements

- **Podman**, and network access for the first build.
- **KoboldCPP** reachable — required; without it there's no text generation.
- **ComfyUI** reachable — optional; only expression sprites need it. Everything else works without.

Both backends come from the [`ai-stack`](https://github.com/El-Iguana/ai-stack) launcher, which
installs and runs them. This app itself needs no GPU: its own models (TTS, STT, embeddings,
background removal) are CPU/ONNX, so the GPU stays free for KoboldCPP and ComfyUI.

## Running

With [`ai-stack`](https://github.com/El-Iguana/ai-stack):

```bash
stack up            # start the backends (KoboldCPP + ComfyUI)
stack companion     # build (first run) + start on http://localhost:7861
```

…or directly, from a clone of this repo:

```bash
./run-podman.sh              # build + run on :7861
./run-podman.sh --rebuild    # rebuild after code/template/static changes
./run-podman.sh --port 8861  # publish on a different host port
```

`run-podman.sh` joins the `ai-net` Podman network when it exists and reaches the backends by name
(`koboldcpp:5001`, `comfyui:8188`). If that network isn't there it falls back to host networking and
points both at `localhost` instead — so a standalone install works, as long as the backends are
listening on the usual ports. Somewhere else entirely? The app reads `KOBOLD_URL` and `COMFYUI_URL`
from its environment, so set them with `-e` on a manual `podman run` (or in `compose.yaml`).

> **The first build takes a while and downloads ~850 MB of models**, baked into the image so nothing
> is fetched at runtime: Kokoro TTS (338 MB), rembg background removal (336 MB), faster-whisper
> `base.en` (141 MB), and a BGE-small embedding model (34 MB). Later builds reuse the layers — only
> `--rebuild` after a code change, which is fast.

### First login

The first run seeds a single admin account:

```
username: ADMIN
password: ***REMOVED***
```

**Change it immediately** (userbar → your account), then add users from the admin-only **Users**
panel. The seed password is a constant in the source, so it's the same on every install — treat any
instance you haven't changed it on as open, especially since the container publishes port 7861 on
all interfaces, not just localhost.

The in-app **?** button documents custom system prompts and curation.

### Optional: Piper voices

Piper TTS voices are **mounted, not baked**: `run-podman.sh` looks for a `jarvis/` directory beside
this repo and mounts any `<name>.onnx` + `<name>.onnx.json` pairs in it read-only. Missing directory
is fine — it's skipped, and the 54 built-in Kokoro voices still work. Adding a voice needs only a
container restart, no rebuild.

`compose.yaml` (an alternative to `run-podman.sh` for `podman-compose` users) mounts that directory
**unconditionally**, so comment the line out if you don't have one.

## Architecture

Runs as a Podman container on the shared `ai-net` network.

| Component | Role |
|-----------|------|
| this app (`:7861`) | FastAPI + SPA |
| KoboldCPP (`koboldcpp:5001`) | LLM text generation |
| ComfyUI (`comfyui:8188`) | expression-sprite generation |

Data lives under `data/` (git-ignored): `data/auth/` (accounts + signing secret) and
`data/u/<user>/` (each user's isolated library). It's a bind mount, so it survives rebuilds — and
it's the thing to back up. Code is baked into the image, so rebuild after editing it. See
[`CLAUDE.md`](CLAUDE.md) for the full internals.

## Note

Local, private, and uncensored by design (it runs an uncensored local model). All characters are
adult. Intended for personal, self-hosted use.
