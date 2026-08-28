# Pantheon Installation Guide

This guide walks you through installing and running **Pantheon**, a self-hosted, fully-local character-card AI companion, on your own Linux workstation. All inference runs on your machine — no cloud services, no external accounts, and no database. State is stored as plain JSON files under a local `data/` directory.

This guide is written for **Linux** systems, with specific notes for **Bazzite** and other immutable Fedora derivatives. The recommended architecture on all supported systems is to run KoboldCPP and ComfyUI as **Podman containers** on a shared `ai-net` network, with Pantheon connecting to them by container name.

This guide uses `~/pantheon` as the runtime root for the entire AI ecosystem. All repositories and runtime state live there.

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
- **Git** and **curl** installed.
- **NVIDIA GPU** with working drivers and the **NVIDIA Container Toolkit**.
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
- For GPU acceleration inside containers, install the **NVIDIA Container Toolkit** via `rpm-ostree`:
  ```bash
  sudo rpm-ostree install nvidia-container-toolkit
  ```
  Then reboot. After that, pass `--device nvidia.com/gpu=all` to backend containers that need CUDA.

The Pantheon `./run-podman.sh` launcher and the backend `podman run` commands below are unchanged on Bazzite; only the way you enable GPU access differs.

## Check prerequisites

Before installing, verify that your system can run the stack:

```bash
podman --version
nvidia-smi
nvidia-ctk cdi list
git --version
curl --version
```

Expected results:

- `podman` is installed.
- `nvidia-smi` shows your GPU.
- `nvidia-ctk cdi list` shows `nvidia.com/gpu=all`.

If `nvidia-ctk cdi list` does not show `nvidia.com/gpu=all`, generate the CDI spec:

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
```

Then run `nvidia-ctk cdi list` again.

On Bazzite, if `nvidia-ctk` is not installed:

```bash
sudo rpm-ostree install nvidia-container-toolkit
```

Then reboot and verify again.

Optional but recommended for the control dashboard and boot auto-start:

```bash
systemctl --user enable --now podman.socket
loginctl enable-linger
```

## Install the AI ecosystem

`ai-stack` is a private repository under the `JotunheimPress` GitHub organization. You need a GitHub account with access to that repository and a registered SSH key to clone it.

### 1. Create the runtime root

All repositories and runtime state live under `~/pantheon`. Set `AI_ECOSYSTEM` explicitly so every `stack` command uses this location.

```bash
mkdir -p ~/pantheon
export AI_ECOSYSTEM="$HOME/pantheon"
```

Add the export to your shell profile if you want it to persist across sessions:

```bash
echo 'export AI_ECOSYSTEM="$HOME/pantheon"' >> ~/.bashrc
```

### 2. Clone the repositories

Clone `ai-stack` and the frontends you want as siblings under `~/pantheon`:

```bash
cd ~/pantheon

git clone git@github.com:JotunheimPress/ai-stack.git
git clone git@github.com:JotunheimPress/companion.git
git clone git@github.com:JotunheimPress/ai-story-maker.git ai_story_maker
git clone git@github.com:JotunheimPress/iguana_studio.git
```

Note the directory names:

| Repository | Directory under `~/pantheon` | Product |
|---|---|---|
| `JotunheimPress/ai-stack` | `ai-stack` | Launcher and backends |
| `JotunheimPress/companion` | `companion` | Pantheon |
| `JotunheimPress/ai-story-maker` | `ai_story_maker` | Rhapsode |
| `JotunheimPress/iguana_studio` | `iguana_studio` | Iguana Studio |

`stack` looks for frontends by these exact directory names.

### 3. Run `stack install`

`stack install` is idempotent — safe to re-run.

```bash
cd ~/pantheon/ai-stack
./stack install
```

This will:

- Check prerequisites (Podman, NVIDIA driver, CDI device).
- Link `stack` onto your `PATH` at `~/.local/bin/stack`.
- Install a systemd user service for boot auto-start.
- Create the `ai-net` Podman network.
- Build backend images: `localhost/koboldcpp:latest`, `localhost/comfyui:latest`, `localhost/stackui:latest`.
- Lay out `~/pantheon/koboldcpp/` and `~/pantheon/ComfyUI/` with mutable state folders.
- Clone pinned ComfyUI custom nodes.

It will **not** download model weights. At the end it reports any missing models.

### 4. Verify the install

```bash
stack doctor
```

This checks prerequisites, images, network, service, and custom nodes without changing anything.

## Add models

The install created the model directories. You must supply the weights yourself.

### KoboldCPP models

Place GGUF files in:

```bash
~/pantheon/koboldcpp/models/
```

Recommended formats for consumer GPUs:

- `Q4_K_M` — best balance of quality and VRAM use
- `Q5_K_M` — slightly better quality, requires more VRAM
- `Q6_K` or `Q8_0` — if you have ample VRAM

Tell `ai-stack` which model to use by creating `~/pantheon/koboldcpp/model.env`:

```bash
cat > ~/pantheon/koboldcpp/model.env <<'EOF'
KOBOLDCPP_MODEL=your-model-Q4_K_M.gguf
KOBOLDCPP_GPU_LAYERS=33
EOF
```

Adjust `KOBOLDCPP_GPU_LAYERS` to fit your GPU. A larger model or heavier quant needs fewer layers.

### ComfyUI models

Place model files under:

```bash
~/pantheon/ComfyUI/models/checkpoints/
~/pantheon/ComfyUI/models/vae/
~/pantheon/ComfyUI/models/clip/
~/pantheon/ComfyUI/models/unet/
```

- `checkpoints/` — SD 1.5, SDXL, or Flux `.safetensors` checkpoints
- `vae/` — VAE files if not bundled in the checkpoint
- `clip/` — CLIP models required by Flux
- `unet/` — UNet/diffusion models for Flux or other non-standard pipelines
- `loras/`, `controlnet/`, etc. — optional enhancements

For Flux-based workflows, you will typically need:

- A Flux checkpoint or UNET in `unet/` or `checkpoints/`
- A matching CLIP model in `clip/`
- A VAE in `vae/`

A good first checkpoint is `absolutereality_v181` (~2 GB).

## Run the stack

### Start the backends

```bash
stack on
```

This starts KoboldCPP and ComfyUI now and enables them to start at boot. To free the GPU later:

```bash
stack off
```

### Start Pantheon

```bash
stack companion
```

This builds the Pantheon image on first run and starts it on `http://localhost:7861`.

### Start other frontends (optional)

```bash
stack story     # Rhapsode on http://localhost:7860
stack iguana    # Iguana Studio on http://localhost:7862
stack stackui   # AI Ecosystem dashboard on http://localhost:7863
```

## Verify everything is running

```bash
stack status
```

Check each backend and frontend:

```bash
curl http://localhost:5001/api/v1/model
curl http://localhost:8188/system_stats
curl http://localhost:7861/
```

From inside the Pantheon container:

```bash
podman exec -it companion curl -s http://koboldcpp:5001/api/v1/model
```

## First login

The first time Pantheon starts with an empty `data/auth/` directory, it creates a single `ADMIN` account. The password is **randomly generated** and printed in the container logs:

```bash
podman logs companion | head -20
```

It is also written to a file so you can find it later:

```bash
cat ~/pantheon/companion/data/auth/INITIAL_PASSWORD
```

That file is created with `0600` permissions (owner-only).

1. Open `http://localhost:7861` in your browser.
2. Log in as `ADMIN` with the generated password.
3. **Change the password immediately** via the userbar → account settings.
4. Delete `~/pantheon/companion/data/auth/INITIAL_PASSWORD`.

### Set your own admin password before first run

If you prefer to choose the admin password, set it before the first start. It is only read when seeding the database:

```bash
COMPANION_ADMIN_PASSWORD='your-secure-password' stack companion
```

> Do not commit a literal password to the repository. `COMPANION_ADMIN_PASSWORD` is intentionally an environment variable, not a constant in the code, because the container publishes port `7861` on all interfaces.

## Configuration

### Backend URLs

`run-podman.sh` auto-detects the backend layout:

- If the `ai-net` Podman network exists, it uses `http://koboldcpp:5001` and `http://comfyui:8188`.
- Otherwise it falls back to host networking and uses `http://localhost:5001` and `http://localhost:8188`.

Because `stack on` creates `ai-net`, Pantheon will use the container DNS names automatically.

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
TZ=Europe/London stack companion
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

Create a directory named `~/pantheon/jarvis/` and place one or more Piper voice bundles there:

```
~/pantheon/jarvis/
  jarvis-medium.onnx
  jarvis-medium.onnx.json
  ...
```

`run-podman.sh` will detect `jarvis/` and mount it read-only into `/app/models/piper`. In the character editor, choose a voice prefixed with `piper:`, e.g. `piper:jarvis-medium`.

No rebuild is needed when adding voices — just restart the container.

## Persistent data and backups

All Pantheon state is stored under `~/pantheon/companion/data/`:

```
~/pantheon/companion/data/
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

`data/` is a bind mount, so it survives image rebuilds and container restarts. **Back up `~/pantheon/companion/data/` regularly** — it is the only place your characters, chats, and memories exist.

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

## Alternative: manual backend setup

If you prefer not to use `ai-stack`, you can run KoboldCPP and ComfyUI as containers yourself. This is not recommended on Bazzite because the root filesystem is read-only and building backend images manually is more fragile than using `stack install`.

See the earlier sections of this guide for the manual `podman run` equivalents, or refer to `ai-stack/koboldcpp/INSTALL.md` and `ai-stack/comfyui/INSTALL.md` for the canonical recipes.

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

- Verify `KOBOLDCPP_MODEL` is set in `~/pantheon/koboldcpp/model.env` and the file exists in `~/pantheon/koboldcpp/models/`.
- Verify `KOBOLD_URL` is set correctly if you overrode it.

### ComfyUI unreachable when generating sprites

- ComfyUI is optional. If you do not need expression sprites, you can ignore this.
- If you want sprites, check that ComfyUI is reachable and that at least one checkpoint is available in `~/pantheon/ComfyUI/models/checkpoints/`.

### GPU not visible inside a backend container on Bazzite

- Ensure the NVIDIA Container Toolkit is installed:

  ```bash
  sudo rpm-ostree install nvidia-container-toolkit
  ```

  Then reboot.

- Confirm the CDI spec exists:

  ```bash
  nvidia-ctk cdi list
  ```

- Pass `--device nvidia.com/gpu=all` to the backend container.
- Confirm your user is in the `render` and `video` groups if required by your driver setup.

### Cannot log in after first run

- If you did not set `COMPANION_ADMIN_PASSWORD`, read the generated password from `~/pantheon/companion/data/auth/INITIAL_PASSWORD` or the container logs.
- If you lost the password, an existing admin can reset it from the **Users** panel. There is no recovery backdoor.

### `stack install` or rebuild is slow

- The first build downloads several gigabytes of PyTorch, CUDA, and KoboldCPP binaries. Subsequent builds reuse cached layers.
- Pantheon's first build also downloads ~850 MB of ONNX models. Subsequent rebuilds are faster.

### Port already in use

- Use `./run-podman.sh --port <port>` inside `~/pantheon/companion`, or change the host port in `compose.yaml`.

## Updating Pantheon

1. Pull or apply the code changes in `~/pantheon/companion`.
2. Rebuild the image:

   ```bash
   cd ~/pantheon/companion
   ./run-podman.sh --rebuild
   ```

3. Your `data/` persists automatically.

Do not edit code inside the running container expecting it to take effect — code is baked into the image.

## Quick command reference

| Task | Command |
|---|---|
| Set ecosystem root | `export AI_ECOSYSTEM="$HOME/pantheon"` |
| Install / verify stack | `./stack install` / `stack doctor` |
| Start backends | `stack on` |
| Stop backends | `stack off` |
| Start Pantheon | `stack companion` |
| Start Rhapsode | `stack story` |
| Start Iguana Studio | `stack iguana` |
| Start dashboard | `stack stackui` |
| View status | `stack status` |
| View logs | `stack logs companion` |
| Stop an app | `stack down companion` |
| Rebuild Pantheon | `cd ~/pantheon/companion && ./run-podman.sh --rebuild` |
| Install NVIDIA toolkit on Bazzite | `sudo rpm-ostree install nvidia-container-toolkit` |
