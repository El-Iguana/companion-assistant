# Cantinero

<p align="center"><img src="docs/cantinero.jpg" alt="El Iguana in a fez behind the bar, holding out an old fashioned" width="420"></p>

*El Iguana's bartender — at your service.*

> **Alpha.** Cantinero works day to day on the author's machine, but it is early software:
> expect rough edges, changes to settings and stored data between releases, and install
> paths that have seen little testing outside Linux with an NVIDIA GPU. Bug reports are
> welcome in [Issues](https://github.com/El-Iguana/companion-assistant/issues).

**New here? Start with the [wiki](https://github.com/El-Iguana/companion-assistant/wiki):** installing,
meeting Mira, talking to her hands-free, music, memory, making your own bartenders, and troubleshooting.

A self-hosted, fully-local **voice assistant built on character cards**. You talk to it, it talks
back, and it shows a face: a 3D VRM avatar (or a portrait), in the browser or in a corner of your
desktop. A FastAPI backend and a [pytincture](https://github.com/pytincture/pytincture) page
(Python in the browser). All inference runs on your own machine — no cloud, no external services,
no database (state is plain JSON files).

Split on 2026-11-07 from companion, which is now Whimsy, the text-only roleplay chat. This repo
keeps the assistant half: voice, avatars, the desktop shell and assistant memory, with no
roleplay, relationship system or narrator.

## Features

- **Local voice** — Kokoro and Piper, per-character voice
  and pace; push-to-talk and hands-free faster-whisper STT with a wake word, a sleep phrase and barge-in
- **A face** — a **3D VRM avatar** with lip-sync, expressions that follow the reply's feeling,
  gestures and idle poses (or simply a portrait)
- **Desktop mode** — the avatar in a transparent window in the corner of the screen (`desktop/`, Linux/KDE)
- **Assistant memory** — standing instructions it obeys, facts it knows, and "where things stand"
  task notes, per character and persona, reviewable and editable
- **Replies composed for the ear** — spoken prose, no markdown, no courtesy questions tacked on
- **Character Card V2/V3** import/export (PNG + JSON), a full card editor with lorebook
- **Personas** — who you are (`{{user}}`), injected into the prompt
- **Multi-user** — login with `admin`/`user` roles; each account has its own private library
- **Mira**, an example assistant, in every new library: wake word "Mira", Kokoro `af_heart`, the DJ and
  Memory on her Menu, and her own 3D avatar, **Pix** (`pix.vrm`). Edit or delete her; she is not re-added.

## Installing

**[INSTALL.md](INSTALL.md)** covers Windows, macOS and Linux with Docker or Podman: the container
engine, KoboldCPP (natively, in a container next to Cantinero, or through ai-stack), first
sign-in, backups, and troubleshooting. In short:

```sh
cp .env.example .env                     # time zone, port, backend addresses
docker compose up -d                     # or: podman compose up -d  →  http://127.0.0.1:7867
docker compose logs companion-assistant  # the first ADMIN password
```

- **KoboldCPP** is the one backend (chat). By default Cantinero looks for it on this computer. `compose.koboldcpp.yaml` runs KoboldCPP in a container
  beside it (NVIDIA; with `compose.gpu.docker.yaml` or `compose.gpu.podman.yaml`).
- Cantinero itself needs **no GPU**: its own models (TTS, STT) are CPU/ONNX, baked into the image.
  The first build downloads ~480 MB of them: Kokoro TTS (338 MB) and faster-whisper `base.en`
  (141 MB).
- Everything a user makes lives in the `companion-assistant-data` volume (compose) or the `data/`
  folder (`run-podman.sh`). `manage.py` checks the backends and resets passwords.

With [AI Ecosystem](https://github.com/JotunheimPress/ai_ecosystem) (`ai-stack`) on the Linux workstation, `stack up`
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
computer ([INSTALL.md, section 8](INSTALL.md#8-reaching-cantinero-from-other-devices)).
With `run-podman.sh`, other devices can talk to it too: open `http://<this computer>.local:7867/ca`
on the device, trust Cantinero's local certificate once (the page walks through it), then use
`https://<this computer>.local:7868/`.

The [wiki](https://github.com/El-Iguana/companion-assistant/wiki) is the user guide. The in-app **Guide**
(Settings → Guide) documents custom system prompts and memory.

## Architecture

Runs as a Podman container on the shared `ai-net` network.

| Component | Role |
|-----------|------|
| this app (`127.0.0.1:7867`, container `companion-assistant`) | FastAPI + the pytincture page; voice and 3D avatar as JS islands |
| KoboldCPP (`koboldcpp:5001`) | LLM text generation |

Data lives under `data/` (git-ignored): `data/auth/` (accounts + session keys) and
`data/u/<user>/` (each user's isolated library). It's a bind mount, so it survives rebuilds — and
it's the thing to back up. Code is baked into the image, so rebuild after editing it. See
[`CLAUDE.md`](CLAUDE.md) for the full internals.

## Note

Local and private by design. Intended for personal, self-hosted use. The bundled avatars are Pix (Mira's)
and pixiv's redistributable sample `baseline.vrm`; models you import stay in your own `data/vrm/`.

## License

[MIT](LICENSE). The bundled avatars keep the terms in their own VRM metadata:
`data/vrm/pix.vrm` (Pix, Mira's avatar, © 2026 El-Iguana) allows redistribution, modification and commercial
use; `data/vrm/baseline.vrm` is pixiv's redistributable VRM sample under pixiv's terms. The models the image downloads when it builds (Kokoro, faster-whisper)
keep their own licenses as well.
