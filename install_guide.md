# Pantheon Installation Guide

This guide walks you through installing and running **Pantheon**, a self-hosted, fully-local character-card AI companion, on your own Linux workstation. All inference runs on your machine — no cloud services, no external accounts, and no database. State is stored as plain JSON files under a local `data/` directory.

## What Pantheon needs

Pantheon runs as a **Podman container**. It does not perform text or image generation itself; it connects to separate backend services that you provide:

| Backend | Required? | Purpose | Default address |
|---|---|---|---|
| **KoboldCPP** | **Yes** | LLM text generation | `http://koboldcpp:5001` or `http://localhost:5001` |
| **ComfyUI** | Optional | Expression-sprite / portrait generation | `http://comfyui:8188` or `http://localhost:8188` |

Without KoboldCPP, Pantheon cannot chat. Without ComfyUI, everything except expression-sprite generation still works.

## Requirements

### Software

- **Podman** installed and working on your Linux workstation.
- **Git** and standard build tools.
- **KoboldCPP** installed and reachable over the network.
- **ComfyUI** installed and reachable, if you want expression sprites.
- A web browser for the frontend.

> The easiest way to satisfy the KoboldCPP + ComfyUI requirements is the [`ai-stack`](https://github.com/JotunheimPress/ai-stack) launcher, which installs and runs both backends together.

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

### 2. Start the backends

Start KoboldCPP, and ComfyUI if you want sprites. They should be on the `ai-net` Podman network (or reachable on `localhost:5001` / `localhost:8188`).

If you are using `ai-stack` but running Pantheon manually:

```bash
stack up
```

Otherwise start your backends however you normally start them, on the ports above.

## Backend setup: KoboldCPP

KoboldCPP is the LLM inference backend Pantheon uses for character replies. It must be running before Pantheon starts, or on the same `ai-net` Podman network if containerized.

### Installing KoboldCPP

1. Install system dependencies. On Fedora/RHEL:

   ```bash
   sudo dnf install git cmake ninja-build gcc g++ python3 python3-pip
   ```

   On Debian/Ubuntu:

   ```bash
   sudo apt update
   sudo apt install git cmake ninja-build build-essential python3 python3-pip
   ```

2. Clone KoboldCPP:

   ```bash
   git clone https://github.com/LostRuins/koboldcpp.git
   cd koboldcpp
   ```

3. Build the project. This compiles the Llama/ggml inference engine:

   ```bash
   make
   ```

   For CUDA acceleration, ensure you have the NVIDIA toolkit installed and use:

   ```bash
   make LLAMA_CUDA=1
   ```

   For Vulkan/ROCm builds, refer to KoboldCPP's repository README for the exact flags.

4. (Optional) Create a Python virtual environment for KoboldCPP and install its dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

### Obtaining models

KoboldCPP runs GGUF models. Download one or more compatible GGUF files, for example from:

- Hugging Face: `https://huggingface.co/models?sort=trending&search=gguf`
- TheBloke / lmstudio-community / bartowski quantized models

Recommended formats for consumer GPUs:

- `Q4_K_M` — best balance of quality and VRAM use
- `Q5_K_M` — slightly better quality, requires more VRAM
- `Q6_K` or `Q8_0` — if you have ample VRAM

Place models in a directory you will pass to KoboldCPP, for example:

```bash
mkdir -p ~/models
mv your-model-Q4_K_M.gguf ~/models/
```

### Running KoboldCPP

Start KoboldCPP from the project directory:

```bash
./koboldcpp.py --model ~/models/your-model-Q4_K_M.gguf \
  --port 5001 \
  --contextsize 4096 \
  --usemlock \
  --highpriority \
  --flashattention \
  --gpulayers 35
```

Key flags:

- `--model` — path to the GGUF file.
- `--port` — the API port. Use `5001` to match Pantheon defaults.
- `--contextsize` — max context length in tokens.
- `--gpulayers` — number of layers to offload to GPU; increase until you run out of VRAM, then lower.
- `--usemlock` — keeps weights in RAM, useful for CPU inference.
- `--highpriority` — raises process priority.
- `--flashattention` — faster attention on supported GPUs.

If running inside a container, bind to all interfaces:

```bash
--host 0.0.0.0
```

Verify it is serving:

```bash
curl http://localhost:5001/api/v1/model
```

You should see a JSON response describing the loaded model.

## Backend setup: ComfyUI

ComfyUI is optional. Pantheon uses it to generate character expression sprites and portraits. If you do not need generated images, you can skip ComfyUI.

### Installing ComfyUI

1. Install Python 3.10+ and a virtual environment tool.

2. Clone ComfyUI:

   ```bash
   git clone https://github.com/comfyanonymous/ComfyUI.git
   cd ComfyUI
   ```

3. Create and activate a virtual environment:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

4. Install PyTorch. For NVIDIA:

   ```bash
   pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
   ```

   For CPU-only:

   ```bash
   pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
   ```

5. Install ComfyUI dependencies:

   ```bash
   pip install -r requirements.txt
   ```

### Obtaining checkpoints and support files

Place model files under the corresponding directories inside `ComfyUI/models/`:

- `checkpoints/` — SD 1.5, SDXL, or Flux `.safetensors` checkpoints
- `vae/` — VAE files if not bundled in the checkpoint
- `clip/` — CLIP models required by Flux
- `unet/` — UNet/diffusion models for Flux or other non-standard pipelines
- `loras/`, `controlnet/`, etc. — optional enhancements

Example:

```bash
mkdir -p models/checkpoints
mv my-checkpoint.safetensors models/checkpoints/
```

For Flux-based workflows, you will typically need:

- A Flux checkpoint or UNET in `models/unet/` or `models/checkpoints/`
- A matching CLIP model in `models/clip/`
- A VAE in `models/vae/`

### Running ComfyUI

Start ComfyUI from its directory:

```bash
python main.py --listen 0.0.0.0 --port 8188
```

Verify it is serving:

```bash
curl http://localhost:8188/system_stats
```

You should see a JSON response.

## Networking Pantheon with the backends

Pantheon discovers KoboldCPP and ComfyUI using `KOBOLD_URL` and `COMFYUI_URL`. The `run-podman.sh` launcher auto-detects based on whether the `ai-net` network exists.

### Option A: Containerized backends on a shared network

Create the Podman network once:

```bash
podman network create ai-net
```

Run KoboldCPP on `ai-net`:

```bash
podman run -d --name koboldcpp --network ai-net \
  -p 5001:5001 \
  -v ~/models:/models:ro,z \
  localhost/koboldcpp:latest \
  --model /models/your-model-Q4_K_M.gguf --port 5001 --host 0.0.0.0
```

Run ComfyUI on `ai-net`:

```bash
podman run -d --name comfyui --network ai-net \
  -p 8188:8188 \
  -v ~/ComfyUI/models:/app/models:ro,z \
  localhost/comfyui:latest \
  python main.py --listen 0.0.0.0 --port 8188
```

Then run Pantheon:

```bash
./run-podman.sh
```

`run-podman.sh` will detect `ai-net` and use:

```bash
KOBOLD_URL=http://koboldcpp:5001
COMFYUI_URL=http://comfyui:8188
```

### Option B: Host-networked or bare-metal backends

If KoboldCPP and ComfyUI are running directly on the host without `ai-net`, start Pantheon with host defaults:

```bash
./run-podman.sh
```

It will fall back to:

```bash
KOBOLD_URL=http://localhost:5001
COMFYUI_URL=http://localhost:8188
```

### Option C: Manual override

When launching Pantheon with `podman run` directly:

```bash
podman run -d --name companion --network ai-net -p 7861:7861 \
  -e KOBOLD_URL=http://koboldcpp:5001 \
  -e COMFYUI_URL=http://comfyui:8188 \
  -v ./data:/app/data:z \
  localhost/companion:latest
```

### 3. Build and run Pantheon

From the project root:

```bash
./run-podman.sh
```

This will:

1. Build the container image `localhost/companion:latest` if it does not exist.
2. Create the `data/` directory tree.
3. Start the container named `companion` on port `7861`.

After the first build, the image layers are cached; later starts are fast. Use `--rebuild` after any code, template, or static-asset change, because those files are baked into the image:

```bash
./run-podman.sh --rebuild
```

Use a different host port if `7861` is taken:

```bash
./run-podman.sh --port 8861
```

### 4. Verify the container is running

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

   or, if on `ai-net`, test from inside the Pantheon container:

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

## Troubleshooting

### `KoboldCPP offline` status dot

- Check that KoboldCPP is running and reachable from inside the Pantheon container:

  ```bash
  podman exec -it companion curl -s http://localhost:5001/api/v1/model
  ```

  or, if on `ai-net`:

  ```bash
  podman exec -it companion curl -s http://koboldcpp:5001/api/v1/model
  ```

- Verify `KOBOLD_URL` is set correctly if you overrode it.

### ComfyUI unreachable when generating sprites

- ComfyUI is optional. If you do not need expression sprites, you can ignore this.
- If you want sprites, check that ComfyUI is reachable and that at least one checkpoint is available in its `models/checkpoints/` directory.

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
| Build KoboldCPP | `make` or `make LLAMA_CUDA=1` |
| Run KoboldCPP | `./koboldcpp.py --model MODEL.gguf --port 5001 --gpulayers N` |
| Install ComfyUI deps | `pip install -r requirements.txt` |
| Run ComfyUI | `python main.py --listen 0.0.0.0 --port 8188` |
| Create shared network | `podman network create ai-net` |
| Run Pantheon | `./run-podman.sh` or `./run-podman.sh --rebuild` |
| View logs | `podman logs -f companion` |
| Stop Pantheon | `podman stop companion` |
