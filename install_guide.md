# Pantheon Installation Guide

This guide walks you through installing and running **Pantheon**, a self-hosted, fully-local character-card AI companion, on your own Linux workstation. All inference runs on your machine — no cloud services, no external accounts, and no database. State is stored as plain JSON files under a local `data/` directory.

This guide is written for **Linux** systems, with specific notes for **Bazzite** and other immutable Fedora derivatives. The recommended architecture on all supported systems is to run KoboldCPP and ComfyUI as **Podman containers** on a shared `ai-net` network, with Pantheon connecting to them by container name.

## What Pantheon needs

Pantheon runs as a **Podman container**. It does not perform text or image generation itself; it connects to separate backend services that you provide:

| Backend | Required? | Purpose | Default address |
|---|---|---|---|
| **KoboldCPP** | **Yes** | LLM text generation | `http://koboldcpp:5001` |
| **ComfyUI** | Optional | Expression-sprite / portrait generation | `http://comfyui:8188` |

Without KoboldCPP, Pantheon cannot chat. Without ComfyUI, everything except expression-sprite generation still works.

The recommended topology is:

1. Create a Podman network named `ai-net`.
2. Run KoboldCPP in a container named `koboldcpp` on `ai-net`.
3. Run ComfyUI in a container named `comfyui` on `ai-net` (optional).
4. Run Pantheon in a container named `companion` on `ai-net`.

Pantheon then resolves the backends by DNS name (`koboldcpp`, `comfyui`) rather than relying on host networking.

## Requirements

### Software

- **Podman** installed and working on your Linux workstation.
- **Git**.
- Container images for the backends, or the ability to build them.
  - KoboldCPP: use a prebuilt community image, or build your own.
  - ComfyUI: use the official image or build your own.
- A web browser for the frontend.

> The easiest way to satisfy the KoboldCPP + ComfyUI requirements is the [`ai-stack`](https://github.com/JotunheimPress/ai-stack) launcher, which starts both backends as containers on `ai-net`.

### Hardware

- A modern multi-core CPU for the container's baked ONNX models (TTS, STT, embeddings, background removal). Pantheon itself uses **zero VRAM**.
- A CUDA-capable GPU is strongly recommended for KoboldCPP and ComfyUI. The reference setup uses a single RTX 4070 with 12 GB of VRAM shared between KoboldCPP and ComfyUI.
- At least **~2 GB of free disk space** for the container image on first build:
  - Kokoro TTS model: ~338 MB
  - rembg background-removal models: ~336 MB
  - faster-whisper `base.en`: ~141 MB
  - BGE-small embeddings: ~34 MB
- Additional disk space for your own KoboldCPP models, ComfyUI checkpoints, and Pantheon's `data/` library.

### Network

- Internet access is required **only during the first build**, to download the baked ONNX models listed above. After that, the container is self-contained.
- The backends must be reachable from the Pantheon container. The easiest topology is a shared Podman network named `ai-net`.

### Bazzite / immutable Fedora notes

Bazzite is an **immutable** Fedora-based distribution. The root filesystem is read-only, so you cannot use `dnf install` to add build tools system-wide. This guide avoids host-level compilation entirely by running the backends as containers.

On Bazzite you should:

- Use **Podman containers** for KoboldCPP, ComfyUI, and Pantheon.
- If you ever need a transient build environment, use **Distrobox** or **Toolbx** rather than layering packages with `rpm-ostree`.
- For GPU acceleration inside containers, install the **NVIDIA Container Toolkit** via `rpm-ostree`:
  ```bash
  sudo rpm-ostree install nvidia-container-toolkit
  ```
  Then reboot. After that, pass `--device nvidia.com/gpu=all` to backend containers that need CUDA.

The Pantheon `./run-podman.sh` launcher and the backend `podman run` commands below are unchanged on Bazzite; only the way you obtain backend images and enable GPU access differs.

## Quick start (recommended): use `ai-stack`

If you have [`ai-stack`](https://github.com/JotunheimPress/ai-stack) installed:

```bash
stack up          # start KoboldCPP + ComfyUI on ai-net
stack companion   # build Pantheon (first run) and start it on http://localhost:7861
```

That is the fastest path. Skip to [First login](#first-login).

## Manual installation

### 1. Clone the repository

```bash
git clone <repo-url>
cd companion   # or whatever the project directory is named
```

### 2. Create the shared Podman network

Create the network once. All three containers will attach to it.

```bash
podman network create ai-net
```

### 3. Obtain or build backend container images

You need images for KoboldCPP and (optionally) ComfyUI. You can build them yourself, pull community images, or use `ai-stack`.

#### Option A: Use `ai-stack` images

If you are using `ai-stack`:

```bash
stack up
```

This creates the `ai-net` network and starts the backend containers for you. Skip to [Run Pantheon](#4-run-pantheon).

#### Option B: Pull community images

For KoboldCPP, community images are available on Docker Hub and Quay. Choose one that matches your hardware (CUDA, Vulkan, or CPU). Example:

```bash
podman pull docker.io/kerast/koboldcpp:latest
```

For ComfyUI, the official image is on Docker Hub:

```bash
podman pull docker.io/comfyanonymous/comfyui:latest
```

> Verify the image tags and CUDA versions suit your GPU driver. These examples are illustrative; use the tag documented by the image maintainer.

#### Option C: Build your own backend images

If you prefer to build the backend images yourself, do it inside a **Distrobox** on Bazzite, or directly on a mutable Fedora/Debian host.

Build KoboldCPP image:

```bash
cd koboldcpp
podman build -t localhost/koboldcpp:latest .
```

Build ComfyUI image:

```bash
cd ComfyUI
podman build -t localhost/comfyui:latest .
```

### 4. Run KoboldCPP

Create a directory for your GGUF models:

```bash
mkdir -p ~/models
```

Place one or more GGUF files there. Recommended formats for consumer GPUs:

- `Q4_K_M` — best balance of quality and VRAM use
- `Q5_K_M` — slightly better quality, requires more VRAM
- `Q6_K` or `Q8_0` — if you have ample VRAM

Run the KoboldCPP container on `ai-net`:

```bash
podman run -d \
  --name koboldcpp \
  --network ai-net \
  -p 5001:5001 \
  --device nvidia.com/gpu=all \
  -v ~/models:/models:ro,z \
  localhost/koboldcpp:latest \
  --model /models/your-model-Q4_K_M.gguf \
  --port 5001 \
  --host 0.0.0.0 \
  --contextsize 4096 \
  --gpulayers 35 \
  --flashattention
```

Key flags:

- `--model` — path to the GGUF file inside the container (`/models/...`).
- `--port` — the API port. Use `5001` to match Pantheon defaults.
- `--host 0.0.0.0` — required so Pantheon can reach the container by DNS name.
- `--contextsize` — max context length in tokens.
- `--gpulayers` — number of layers to offload to GPU; increase until you run out of VRAM, then lower.
- `--flashattention` — faster attention on supported GPUs.

On CPU-only systems, omit `--device nvidia.com/gpu=all` and `--gpulayers`, and add `--usemlock` if desired.

Verify it is serving:

```bash
curl http://localhost:5001/api/v1/model
```

You should see a JSON response describing the loaded model. From inside the Pantheon container you will also be able to reach it as `http://koboldcpp:5001`.

### 5. Run ComfyUI (optional)

ComfyUI is only needed if you want Pantheon to generate character expression sprites and portraits.

Create a directory for ComfyUI models:

```bash
mkdir -p ~/comfyui-models/checkpoints
mkdir -p ~/comfyui-models/vae
mkdir -p ~/comfyui-models/clip
mkdir -p ~/comfyui-models/unet
```

Place model files under the corresponding directories:

- `checkpoints/` — SD 1.5, SDXL, or Flux `.safetensors` checkpoints
- `vae/` — VAE files if not bundled in the checkpoint
- `clip/` — CLIP models required by Flux
- `unet/` — UNet/diffusion models for Flux or other non-standard pipelines
- `loras/`, `controlnet/`, etc. — optional enhancements

For Flux-based workflows, you will typically need:

- A Flux checkpoint or UNET in `unet/` or `checkpoints/`
- A matching CLIP model in `clip/`
- A VAE in `vae/`

Run the ComfyUI container on `ai-net`:

```bash
podman run -d \
  --name comfyui \
  --network ai-net \
  -p 8188:8188 \
  --device nvidia.com/gpu=all \
  -v ~/comfyui-models:/app/models:ro,z \
  localhost/comfyui:latest \
  python main.py --listen 0.0.0.0 --port 8188
```

Verify it is serving:

```bash
curl http://localhost:8188/system_stats
```

You should see a JSON response.

### 6. Run Pantheon

From the project root:

```bash
./run-podman.sh
```

This will:

1. Build the container image `localhost/companion:latest` if it does not exist.
2. Create the `data/` directory tree.
3. Start the container named `companion` on `ai-net` and publish port `7861`.

Because `ai-net` exists, `run-podman.sh` automatically sets:

```bash
KOBOLD_URL=http://koboldcpp:5001
COMFYUI_URL=http://comfyui:8188
```

After the first build, the image layers are cached; later starts are fast. Use `--rebuild` after any code, template, or static-asset change, because those files are baked into the image:

```bash
./run-podman.sh --rebuild
```

Use a different host port if `7861` is taken:

```bash
./run-podman.sh --port 8861
```

### 7. Verify the container is running

```bash
podman ps
podman logs -f companion
```

You should see the startup banner, including the first-run admin password if this is the first time.

## First login

The first time Pantheon starts with an empty `data/auth/` directory, it creates a single `ADMIN` account. The password is **randomly generated** and printed in the container logs:

```bash
podman logs companion | head -20
```

It is also written to a file so you can find it later:

```bash
cat data/auth/INITIAL_PASSWORD
```

That file is created with `0600` permissions (owner-only).

1. Open `http://localhost:7861` in your browser.
2. Log in as `ADMIN` with the generated password.
3. **Change the password immediately** via the userbar → account settings.
4. Delete `data/auth/INITIAL_PASSWORD`.

### Set your own admin password before first run

If you prefer to choose the admin password, set it before the first start. It is only read when seeding the database:

```bash
COMPANION_ADMIN_PASSWORD='your-secure-password' ./run-podman.sh
```

> Do not commit a literal password to the repository. `COMPANION_ADMIN_PASSWORD` is intentionally an environment variable, not a constant in the code, because the container publishes port `7861` on all interfaces.

## Configuration

### Backend URLs

`run-podman.sh` auto-detects the backend layout:

- If the `ai-net` Podman network exists, it uses `http://koboldcpp:5001` and `http://comfyui:8188`.
- Otherwise it falls back to host networking and uses `http://localhost:5001` and `http://localhost:8188`.

This guide assumes you created `ai-net` and used the containerized backend approach, so Pantheon will use the container DNS names automatically.

You can override these with environment variables when running the container manually:

```bash
podman run -d \
  --name companion \
  --network ai-net \
  -p 7861:7861 \
  -e KOBOLD_URL=http://my-kobold-host:5001 \
  -e COMFYUI_URL=http://my-comfy-host:8188 \
  -v ./data:/app/data:z \
  localhost/companion:latest
```

These variables are read by `main.py` and passed to `KoboldClient` / `ComfyUIClient`.

### Timezone

By default the image is UTC. `run-podman.sh` derives the host timezone and passes it as `TZ`. You can override it:

```bash
TZ=Europe/London ./run-podman.sh
```

This affects timestamps in `created_at` fields, logs, and assistant-mode replies when asked for the time.

### Podman Compose

If you prefer `podman-compose`, a `compose.yaml` is provided:

```bash
podman-compose up -d
podman-compose logs -f
```

Review `compose.yaml` first: it assumes `ai-net` exists and unconditionally mounts `../jarvis` for Piper voices. If you do not have a `jarvis/` directory, comment out that volume line.

## Optional: Piper TTS voices

Pantheon includes **Kokoro** TTS by default (54 built-in voices). It also supports **Piper** voices if you mount them at run time.

Create a directory named `jarvis/` next to the repository, and place one or more Piper voice bundles there:

```
jarvis/
  jarvis-medium.onnx
  jarvis-medium.onnx.json
  ...
```

`run-podman.sh` will detect `jarvis/` and mount it read-only into `/app/models/piper`. In the character editor, choose a voice prefixed with `piper:`, e.g. `piper:jarvis-medium`.

No rebuild is needed when adding voices — just restart the container.

## Persistent data and backups

All Pantheon state is stored under `data/`:

```
data/
  auth/               # signing secret and user accounts
  auth/users/         # one JSON file per user
  u/
    <user-id>/
      characters/     # character cards
      personas/       # user personas
      sessions/       # chat sessions
      relationships/  # per-(character,persona) relationship state
      presets/        # reusable relationship presets
      settings/       # per-user app settings
      memories/       # cross-session memory + vector indexes
```

`data/` is a bind mount, so it survives image rebuilds and container restarts. **Back up `data/` regularly** — it is the only place your characters, chats, and memories exist.

## First-time sanity check

After logging in:

1. Import or create a character card.
   - Use **Import** in the sidebar to load a SillyTavern/Chub PNG or JSON card.
   - Or click **New character** to build one from scratch.
2. Create a chat session from the card.
3. Type a message and send it.
4. The header dot should show KoboldCPP as online; if it shows offline, check that KoboldCPP is reachable from the container.

## Backend verification

Before relying on Pantheon, confirm each backend is reachable:

1. KoboldCPP:

   ```bash
   curl http://localhost:5001/api/v1/model
   ```

   or, from inside the Pantheon container:

   ```bash
   podman exec -it companion curl -s http://koboldcpp:5001/api/v1/model
   ```

2. ComfyUI:

   ```bash
   curl http://localhost:8188/system_stats
   ```

3. Pantheon:

   ```bash
   curl http://localhost:7861/
   ```

## Alternative: host-level backend installation

If you are not on an immutable distribution and prefer to run KoboldCPP or ComfyUI directly on the host, you can still do so. Point Pantheon to them with host networking by **not** creating `ai-net`, or by setting `KOBOLD_URL` / `COMFYUI_URL` explicitly.

See the project wiki or upstream documentation for building KoboldCPP and ComfyUI from source. This is not recommended on Bazzite because the root filesystem is read-only.

## Troubleshooting

### `KoboldCPP offline` status dot

- Check that KoboldCPP is running and reachable from inside the Pantheon container:

  ```bash
  podman exec -it companion curl -s http://koboldcpp:5001/api/v1/model
  ```

- Verify that the `koboldcpp` container is on `ai-net`:

  ```bash
  podman inspect koboldcpp --format '{{range .NetworkSettings.Networks}}{{.NetworkID}}{{end}}'
  podman network inspect ai-net
  ```

- Verify `KOBOLD_URL` is set correctly if you overrode it.

### ComfyUI unreachable when generating sprites

- ComfyUI is optional. If you do not need expression sprites, you can ignore this.
- If you want sprites, check that ComfyUI is reachable and that at least one checkpoint is available in its `models/checkpoints/` directory.

### GPU not visible inside a backend container on Bazzite

- Ensure the NVIDIA Container Toolkit is installed:

  ```bash
  sudo rpm-ostree install nvidia-container-toolkit
  ```

  Then reboot.

- Pass `--device nvidia.com/gpu=all` to the backend container.
- Confirm your user is in the `render` and `video` groups if required by your driver setup.

### Cannot log in after first run

- If you did not set `COMPANION_ADMIN_PASSWORD`, read the generated password from `data/auth/INITIAL_PASSWORD` or the container logs.
- If you lost the password, an existing admin can reset it from the **Users** panel. There is no recovery backdoor.

### `run-podman.sh --rebuild` is slow

- The first build downloads ~850 MB of models. Subsequent rebuilds reuse cached layers and should be much faster.

### Port already in use

- Use `./run-podman.sh --port <port>` or change the host port in `compose.yaml`.

## Updating Pantheon

1. Pull or apply the code changes.
2. Rebuild the image:

   ```bash
   ./run-podman.sh --rebuild
   ```

3. Your `data/` persists automatically.

Do not edit code inside the running container expecting it to take effect — code is baked into the image.

## Quick command reference

| Task | Command |
|---|---|
| Create shared network | `podman network create ai-net` |
| Run KoboldCPP container | `podman run -d --name koboldcpp --network ai-net -p 5001:5001 --device nvidia.com/gpu=all -v ~/models:/models:ro,z IMAGE ...` |
| Run ComfyUI container | `podman run -d --name comfyui --network ai-net -p 8188:8188 --device nvidia.com/gpu=all -v ~/comfyui-models:/app/models:ro,z IMAGE ...` |
| Run Pantheon | `./run-podman.sh` or `./run-podman.sh --rebuild` |
| View logs | `podman logs -f companion` |
| Stop Pantheon | `podman stop companion` |
| Stop backends | `podman stop koboldcpp comfyui` |
| Install NVIDIA Container Toolkit on Bazzite | `sudo rpm-ostree install nvidia-container-toolkit` |
