# Cantinero

<p align="center"><img src="docs/cantinero.jpg" alt="El Iguana in a fez behind the bar, holding out an old fashioned" width="420"></p>

*El Iguana's bartender — at your service.*

A self-hosted, fully-local **voice assistant built on character cards**. You talk to it, it talks
back, and it shows a face: expression sprites or a 3D VRM avatar, in the browser or in a corner of
your desktop. A FastAPI backend and a [pytincture](https://github.com/pytincture/pytincture) page
(Python in the browser). All inference runs on your own machine — no cloud, no external services,
no database (state is plain JSON files).

Split on 2026-11-07 from [El-Iguana/companion](https://github.com/El-Iguana/companion), which is now
the text-only roleplay chat. This repo keeps the assistant half: voice, avatars, the desktop shell
and assistant memory, with no roleplay, relationship system or narrator.

## Features

- **Local voice** — Kokoro and Piper, per-character voice
  and pace; push-to-talk and hands-free faster-whisper STT with a wake word, a sleep phrase and barge-in
- **A face** — ComfyUI expression sprites (one portrait → per-emotion sprites, live with the mood),
  or a **3D VRM avatar** with lip-sync, expressions and idle motion
- **Desktop mode** — the avatar in a transparent window in the corner of the screen (`desktop/`, Linux/KDE)
- **Assistant memory** — standing instructions it obeys, facts it knows, and "where things stand"
  task notes, per character and persona, reviewable and editable
- **Replies composed for the ear** — spoken prose, no markdown, no courtesy questions tacked on
- **Character Card V2/V3** import/export (PNG + JSON), a full card editor with lorebook
- **Personas** — who you are (`{{user}}`), injected into the prompt
- **Multi-user** — login with `admin`/`user` roles; each account has its own private library
- **Mira**, an example assistant, in every new library (wake word "Mira", Kokoro `af_sarah`, the
  bundled pixiv sample avatar `baseline.vrm`). Edit or delete her; she is not re-added.

## Installing

**[INSTALL.md](INSTALL.md)** covers Windows, macOS and Linux with Docker or Podman: the container
engine, KoboldCPP (natively, in a container next to Cantinero, or through ai-stack), optional
ComfyUI, first sign-in, backups, and troubleshooting. In short:

```sh
cp .env.example .env                     # time zone, port, backend addresses
docker compose up -d                     # or: podman compose up -d  →  http://127.0.0.1:7867
docker compose logs companion-assistant  # the first ADMIN password
```

- **KoboldCPP** is required (chat); **ComfyUI** is optional (expression sprites only). By default
  Cantinero looks for both on this computer. `compose.koboldcpp.yaml` runs KoboldCPP in a container
  beside it (NVIDIA; with `compose.gpu.docker.yaml` or `compose.gpu.podman.yaml`).
- Cantinero itself needs **no GPU**: its own models (TTS, STT, background removal) are CPU/ONNX,
  baked into the image. The first build downloads ~820 MB of them: Kokoro TTS (338 MB), rembg
  (336 MB) and faster-whisper `base.en` (141 MB).
- Everything a user makes lives in the `companion-assistant-data` volume (compose) or the `data/`
  folder (`run-podman.sh`). `manage.py` checks the backends and resets passwords.

With [`ai-stack`](https://github.com/JotunheimPress/ai-stack) on the Linux workstation, `stack up`
then `stack companion-assistant`. It runs `run-podman.sh`, which joins the `ai-net` network when it
exists (backends by name), falls back to host networking, keeps data in `data/`, mounts Piper
voices from `../jarvis`, and derives `TZ` from the host. It runs beside the roleplay app
(Whimsy, the `companion` repo, port 7861):

```bash
./run-podman.sh              # build + run on :7867
./run-podman.sh --rebuild    # rebuild after code/static changes
./run-podman.sh --port 8867  # publish on a different host port
COMPANION_ADMIN_PASSWORD='…' ./run-podman.sh   # choose the first ADMIN password
```

Open **`http://127.0.0.1:7867/`** (`localhost` redirects there). Both `run-podman.sh` and compose
publish on `127.0.0.1` only. The microphone needs a secure context, so voice input works on this
computer ([INSTALL.md, section 9](INSTALL.md#9-reaching-cantinero-from-other-devices)).

The in-app **Guide** (Settings → Guide) documents custom system prompts and memory.

## Architecture

Runs as a Podman container on the shared `ai-net` network.

| Component | Role |
|-----------|------|
| this app (`127.0.0.1:7867`, container `companion-assistant`) | FastAPI + the pytincture page; voice and 3D avatar as JS islands |
| KoboldCPP (`koboldcpp:5001`) | LLM text generation |
| ComfyUI (`comfyui:8188`) | expression-sprite generation (optional) |

Data lives under `data/` (git-ignored): `data/auth/` (accounts + session keys) and
`data/u/<user>/` (each user's isolated library). It's a bind mount, so it survives rebuilds — and
it's the thing to back up. Code is baked into the image, so rebuild after editing it. See
[`CLAUDE.md`](CLAUDE.md) for the full internals.

## Note

Local and private by design. Intended for personal, self-hosted use. The bundled `baseline.vrm` is
pixiv's redistributable sample model; models you import stay in your own `data/vrm/`.
