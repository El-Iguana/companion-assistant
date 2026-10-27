# Pantheon

A self-hosted, fully-local **character-card AI companion**. A FastAPI backend and a
[pytincture](https://github.com/pytincture/pytincture) page (Python in the browser), laid out after
Emberfolk: a Discover gallery, character profiles, a centred chat, and a Studio for cards. All inference runs on your own machine — no cloud, no external services, no
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
- **3D avatars** (VRM) with lip-sync, expressions and idle motion, in the chat or as a
  **desktop companion** in the corner of the screen (`desktop/`, Linux/KDE)
- **Roleplay vs assistant** chat modes, custom system prompts, and a `/no_think` speed toggle

## Installing

**[INSTALL.md](INSTALL.md)** covers Windows, macOS and Linux with Docker or Podman: the container
engine, KoboldCPP (natively, in a container next to Pantheon, or through ai-stack), optional
ComfyUI, first sign-in, backups, and troubleshooting. In short:

```sh
cp .env.example .env          # time zone, port, backend addresses
docker compose up -d          # or: podman compose up -d  →  http://127.0.0.1:7861
docker compose logs pantheon  # the first ADMIN password
```

- **KoboldCPP** is required (chat); **ComfyUI** is optional (expression sprites only). By default
  Pantheon looks for both on this computer. `compose.koboldcpp.yaml` runs KoboldCPP in a container
  beside it (NVIDIA; with `compose.gpu.docker.yaml` or `compose.gpu.podman.yaml`).
- Pantheon itself needs **no GPU**: its own models (TTS, STT, embeddings, background removal) are
  CPU/ONNX, baked into the image. The first build downloads ~850 MB of them: Kokoro TTS (338 MB),
  rembg (336 MB), faster-whisper `base.en` (141 MB) and BGE-small embeddings (34 MB).
- Everything a user makes lives in the `pantheon-data` volume (compose) or the `data/` folder
  (`run-podman.sh`). `manage.py` checks the backends and resets passwords.

With [`ai-stack`](https://github.com/JotunheimPress/ai-stack) on the Linux workstation, `stack up`
then `stack companion` still works as before. It runs `run-podman.sh`, which joins the `ai-net`
network when it exists (backends by name), falls back to host networking, keeps data in `data/`,
mounts Piper voices from `../jarvis`, and derives `TZ` from the host:

```bash
./run-podman.sh              # build + run on :7861
./run-podman.sh --rebuild    # rebuild after code/static changes
./run-podman.sh --port 8861  # publish on a different host port
COMPANION_ADMIN_PASSWORD='…' ./run-podman.sh   # choose the first ADMIN password
```

Open **`http://127.0.0.1:7861/`** (`localhost` redirects there). Both `run-podman.sh` and compose
publish on `127.0.0.1` only: sign-in over plain HTTP is allowed on this computer alone, so other
devices need HTTPS in front ([INSTALL.md, section 9](INSTALL.md#9-reaching-pantheon-from-other-devices)).

The in-app **Guide** (Settings → Guide) documents custom system prompts and curation.

## Architecture

Runs as a Podman container on the shared `ai-net` network.

| Component | Role |
|-----------|------|
| this app (`127.0.0.1:7861`) | FastAPI + the pytincture page; voice and 3D avatar as JS islands |
| KoboldCPP (`koboldcpp:5001`) | LLM text generation |
| ComfyUI (`comfyui:8188`) | expression-sprite generation |

Data lives under `data/` (git-ignored): `data/auth/` (accounts + session keys) and
`data/u/<user>/` (each user's isolated library). It's a bind mount, so it survives rebuilds — and
it's the thing to back up. Code is baked into the image, so rebuild after editing it. See
[`CLAUDE.md`](CLAUDE.md) for the full internals.

## Note

Local, private, and uncensored by design (it runs an uncensored local model). All characters are
adult. Intended for personal, self-hosted use.
