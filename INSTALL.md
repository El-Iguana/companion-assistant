# Installing Pantheon

Pantheon is a self-hosted character-card AI companion. It runs as a
container, with **Docker** or **Podman**, on Windows, macOS or Linux. Its own
models (voice, speech recognition, memory search, background removal) are
CPU-only and baked into the image, so **Pantheon itself needs no GPU**.

It does not generate text or images itself. It talks to two backends, which
you run next to it:

| Backend | Needed for | Required? | Pantheon's default address |
|---|---|---|---|
| **KoboldCPP** | chat | **yes** | `http://host.docker.internal:5001` |
| **ComfyUI** | expression sprites | no | `http://host.docker.internal:8188` |

`host.docker.internal` means "this computer, seen from inside the
container". Without ComfyUI everything except sprite generation works.

- **Disk:** about 2.1 GB for Pantheon's image, plus your models (a chat model
  is typically 3–10 GB). The optional KoboldCPP container is another 3 GB.
- **Network during the build:** github.com, huggingface.co, pypi.org and
  deb.debian.org. Afterwards Pantheon needs no internet.
- **Time:** the first build downloads about 1.5 GB and takes several minutes.

Every command below is shown for Docker. **For Podman, replace `docker` with
`podman`**: the compose files, flags and paths are the same, except for the
GPU file in [4b](#4b-koboldcpp-in-a-container-nvidia). Where Windows
PowerShell differs from macOS/Linux shells, both are given.

**Contents**

1. [Choose where the backends run](#1-choose-where-the-backends-run)
2. [Install a container engine](#2-install-a-container-engine)
3. [Get Pantheon and configure it](#3-get-pantheon-and-configure-it)
4. [Set up KoboldCPP](#4-set-up-koboldcpp): [natively](#4a-koboldcpp-natively-any-computer) · [in a container](#4b-koboldcpp-in-a-container-nvidia) · [with ai-stack](#4c-ai-stack-linux--nvidia)
5. [Optional: ComfyUI for sprites](#5-optional-comfyui-for-expression-sprites)
6. [Start Pantheon and sign in](#6-start-pantheon-and-sign-in)
7. [Everyday tasks](#7-everyday-tasks): stop, logs, update, back up, reset a password, voices, uninstall
8. [Without compose](#8-without-compose)
9. [Reaching Pantheon from other devices](#9-reaching-pantheon-from-other-devices)
10. [Troubleshooting](#10-troubleshooting)
11. [What was actually tested](#11-what-was-actually-tested)

---

## 1. Choose where the backends run

Pantheon's container is the same everywhere. What differs is KoboldCPP (and
ComfyUI), which want your GPU. Find your row:

| Your computer | KoboldCPP | Section |
|---|---|---|
| **Mac** (Apple Silicon) | the native macOS build, which uses Metal | [4a](#4a-koboldcpp-natively-any-computer) |
| **Windows**, NVIDIA GPU | the native `koboldcpp.exe` (simplest), or a container with Docker Desktop | [4a](#4a-koboldcpp-natively-any-computer) or [4b](#4b-koboldcpp-in-a-container-nvidia) |
| **Windows**, AMD or Intel GPU | the native build, with Vulkan | [4a](#4a-koboldcpp-natively-any-computer) |
| **Linux**, NVIDIA GPU | a container next to Pantheon | [4b](#4b-koboldcpp-in-a-container-nvidia) |
| **Linux** with the ai-stack launcher | ai-stack's own containers | [4c](#4c-ai-stack-linux--nvidia) |
| **Linux**, AMD or Intel GPU | the native Linux build, with Vulkan | [4a](#4a-koboldcpp-natively-any-computer) |
| Backends on **another computer** | whatever that computer runs | [4a](#4a-koboldcpp-natively-any-computer), last part |

Why not always a container? On a Mac, containers run in a Linux VM that
cannot use the GPU, so KoboldCPP in a container there would be CPU-only and
very slow. On Windows, the native build is a single `.exe` and is less work
than GPU passthrough.

**How big a model?** It has to fit in your GPU's memory (VRAM), or on a Mac
in about two thirds of your RAM. Roughly, a GGUF file of up to 70–80 % of
your VRAM leaves room for the conversation: a 4–8B model at `Q4_K_M` to
`Q8_0` on 8–12 GB, up to 13B at `Q4_K_M` on 12 GB. Pantheon was developed
with Qwen3-4B-Instruct-2507 at `Q8_0` (4.3 GB). Download GGUF models from
Hugging Face.

---

## 2. Install a container engine

Pick **one** engine. You also need its **compose** tool, which starts
Pantheon from `compose.yaml` with one command.

### Windows 10 (22H2 or later) and Windows 11

**Docker Desktop**

1. Install Docker Desktop from <https://www.docker.com/products/docker-desktop/>,
   or in PowerShell: `winget install Docker.DockerDesktop`.
2. It uses WSL 2. If the installer asks, let it enable WSL, or run
   `wsl --install` in an administrator PowerShell and restart.
3. Start Docker Desktop and wait until it says it is running.
4. Check, in a new PowerShell window:
   ```powershell
   docker version
   docker compose version
   ```

With an NVIDIA GPU, Docker Desktop can also give containers the GPU (for
[4b](#4b-koboldcpp-in-a-container-nvidia)). It needs only the normal NVIDIA
Windows driver, no toolkit inside WSL. Check with
`docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.

**Podman**

1. Install Podman Desktop from <https://podman-desktop.io/>, or in PowerShell:
   `winget install RedHat.Podman-Desktop` (the command-line only package is
   `RedHat.Podman`).
2. Create and start the Podman machine (a small Linux VM, using WSL 2):
   ```powershell
   podman machine init
   podman machine start
   ```
   Podman Desktop offers to do this on first launch.
3. Install a compose provider. Podman Desktop's **Compose** extension installs
   one; on the command line: `winget install Docker.DockerCompose`.
4. Check:
   ```powershell
   podman version
   podman compose version
   ```

With Podman on Windows, run KoboldCPP natively ([4a](#4a-koboldcpp-natively-any-computer)).
GPU access inside the Podman machine takes extra setup that this guide does
not cover.

**Git** (to clone): `winget install Git.Git`.

### macOS 13 or later

**Docker Desktop**: install it from <https://www.docker.com/products/docker-desktop/>,
then start it and check `docker version` and `docker compose version`.
Docker-compatible alternatives such as OrbStack or Colima work the same way,
since they provide the `docker` command.

**Podman**

```sh
brew install podman docker-compose      # docker-compose is podman compose's provider
podman machine init --memory 4096
podman machine start
podman compose version
```

Podman Desktop (<https://podman-desktop.io/>) is the graphical alternative.

Give the VM enough memory: Pantheon's speech and voice models want about
2 GB. For Docker Desktop, go to **Settings → Resources → Memory** and set at
least 4 GB. For an existing Podman machine, stop it and run
`podman machine set --memory 4096`.

On Apple Silicon the image builds as native `arm64`; nothing to configure.

### Linux

**Docker Engine**: follow <https://docs.docker.com/engine/install/> for your
distribution and install the Compose plugin (`docker-compose-plugin`). To use
`docker` without `sudo`, add yourself to the `docker` group and log in again:

```sh
sudo usermod -aG docker "$USER"
```

**Podman** (rootless by default; nothing needs `sudo` once it is installed):

```sh
sudo apt install podman                 # Debian, Ubuntu
sudo dnf install podman                 # Fedora, RHEL, CentOS Stream
```

Then a compose provider, either one:

```sh
sudo apt install podman-compose         # or: pipx install podman-compose
sudo dnf install podman-compose
# or Docker's compose plugin, which podman compose also uses
```

Check with `podman compose version`.

On an **immutable** distribution (Bazzite, Silverblue, Kinoite), Podman is
already installed; get `docker-compose` from Homebrew (`brew install
docker-compose`) instead of layering packages.

**NVIDIA GPU in containers** (only for [4b](#4b-koboldcpp-in-a-container-nvidia)):
install the NVIDIA Container Toolkit, following
<https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>.

- Docker: run `sudo nvidia-ctk runtime configure --runtime=docker` and
  restart Docker. Check with `docker run --rm --gpus all
  nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.
- Podman: generate the device list, then check:
  ```sh
  sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
  nvidia-ctk cdi list                   # must show nvidia.com/gpu=all
  podman run --rm --device nvidia.com/gpu=all --security-opt label=disable \
      docker.io/nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
  ```
  Bazzite's `-nvidia` images ship the toolkit already.

---

## 3. Get Pantheon and configure it

The repository is **private** (`JotunheimPress/companion`), so you need a
GitHub account that has been given access. Clone it with the GitHub CLI,
which asks you to sign in once:

Pantheon is built together with **Muninn**, its memory package, which is a
separate private repository (`JotunheimPress/muninn`). Clone both into the same
folder: the build looks for Muninn at `../muninn`, next to Pantheon.

```sh
gh auth login
gh repo clone JotunheimPress/muninn muninn
gh repo clone JotunheimPress/companion pantheon
cd pantheon
```

or with an SSH key registered on your GitHub account:

```sh
git clone git@github.com:JotunheimPress/muninn.git muninn
git clone git@github.com:JotunheimPress/companion.git pantheon
cd pantheon
```

(The folder is called `pantheon` here; the repository keeps its old name. The
Muninn folder must be called `muninn`.)

> **Windows:** the repository forces Unix line endings (`.gitattributes`), so
> Git's usual CRLF conversion does not touch it. Keep it that way. The
> KoboldCPP start script runs inside Linux, and a `\r` at the end of a
> setting in `.env` becomes part of the value.

Copy the example settings:

```sh
cp .env.example .env                    # macOS, Linux
```
```powershell
Copy-Item .env.example .env             # Windows PowerShell
```

Open `.env` in a text editor. The settings you are most likely to change:

| Setting | What it does |
|---|---|
| `TZ` | Your time zone, e.g. `Europe/Berlin`, `America/New_York`. Timestamps, and "what time is it?" in assistant mode, use it. Default `UTC`. |
| `PANTHEON_ADMIN_PASSWORD` | The first account's password, read **only** when the first account is created. Leave it empty to get a random one ([6](#6-start-pantheon-and-sign-in)). |
| `PANTHEON_PORT` | The port on this computer, default `7861`. |
| `KOBOLD_URL`, `COMFYUI_URL` | Where the backends are, if not on this computer's usual ports. |

The rest of `.env.example` is for the KoboldCPP container ([4b](#4b-koboldcpp-in-a-container-nvidia))
and is explained there.

> **Windows:** Notepad may save the file as `.env.txt` when file extensions are
> hidden. In File Explorer turn on **View → Show → File name extensions** and
> check that the file is called exactly `.env`.

---

## 4. Set up KoboldCPP

Do **one** of 4a, 4b or 4c.

### 4a. KoboldCPP natively (any computer)

Download KoboldCPP from its releases page,
<https://github.com/LostRuins/koboldcpp/releases>. Pick the file for your
computer:

| Computer | File | GPU option |
|---|---|---|
| Windows, NVIDIA | `koboldcpp.exe` | CUDA (`--usecuda`) |
| Windows, AMD or Intel | `koboldcpp-nocuda.exe` | Vulkan (`--usevulkan`) |
| macOS, Apple Silicon | `koboldcpp-mac-arm64` | Metal, automatic |
| Linux, NVIDIA | `koboldcpp-linux-x64` | CUDA (`--usecuda`) |
| Linux, AMD or Intel | `koboldcpp-linux-x64-nocuda` | Vulkan (`--usevulkan`) |

Start it with a model. On Windows you can simply double-click the `.exe`:
choose the model in its window, pick CUDA or Vulkan under **Presets**, and
click **Launch**. From a terminal:

```powershell
# Windows
.\koboldcpp.exe --model C:\models\my-model.gguf --usecuda --gpulayers -1 --contextsize 8192
```
```sh
# macOS (first allow it to run, see below)
chmod +x koboldcpp-mac-arm64
./koboldcpp-mac-arm64 --model ~/models/my-model.gguf --gpulayers -1 --contextsize 8192

# Linux
chmod +x koboldcpp-linux-x64
./koboldcpp-linux-x64 --model ~/models/my-model.gguf --usecuda --gpulayers -1 --contextsize 8192
```

`--gpulayers -1` puts the whole model on the GPU. If it does not fit, lower
it (e.g. `20`) and the rest runs on the CPU. On macOS the first launch is
blocked as an unidentified download: allow it in **System Settings → Privacy
& Security**, or run `xattr -d com.apple.quarantine koboldcpp-mac-arm64`.

KoboldCPP listens on port **5001**. Check it in a browser on the same
computer: <http://127.0.0.1:5001> shows KoboldCPP's own page.

**Then:**

- **Windows and macOS:** nothing to set. Pantheon's default
  `KOBOLD_URL=http://host.docker.internal:5001` reaches it.
- **Linux:** a container on Docker's or Podman's default network cannot reach
  services bound to this computer's `127.0.0.1`. KoboldCPP listens on all
  addresses unless told otherwise, so the default URL works. If you started it
  with `--host 127.0.0.1`, start Pantheon with `compose.host-network.yaml`
  instead ([6](#6-start-pantheon-and-sign-in)).

**KoboldCPP on another computer** (a gaming PC, say): start it there as above,
make sure its firewall allows port 5001, and set in `.env`:

```sh
KOBOLD_URL=http://192.168.1.20:5001
```

with that computer's address. The same goes for `COMFYUI_URL`.

### 4b. KoboldCPP in a container (NVIDIA)

For Linux with Docker or Podman, and Windows with Docker Desktop, with an
NVIDIA GPU set up as in [2](#2-install-a-container-engine). The container
runs the official KoboldCPP Linux build with CUDA; compose builds it from
`backends/koboldcpp/`.

1. Put one or more `.gguf` models in the `llm-models` folder inside
   `pantheon`, or point `KOBOLD_MODELS_DIR` at an existing folder.
2. In `.env`, name the model and choose the compose files. Uncomment the
   `COMPOSE_FILE` line for your engine:

   ```sh
   KOBOLD_MODEL=Qwen3-4B-Instruct-2507-Q8_0.gguf
   # Docker, Linux:
   COMPOSE_FILE=compose.yaml:compose.koboldcpp.yaml:compose.gpu.docker.yaml
   # Docker Desktop, Windows (";" separates the files there):
   COMPOSE_FILE=compose.yaml;compose.koboldcpp.yaml;compose.gpu.docker.yaml
   # Podman, Linux:
   COMPOSE_FILE=compose.yaml:compose.koboldcpp.yaml:compose.gpu.podman.yaml
   ```

   **The GPU files are not interchangeable.** Podman ignores the Docker form
   without an error, and KoboldCPP then runs on the CPU.

3. Optional, in `.env`: `KOBOLD_GPU_LAYERS` (`-1` = all, lower it if the model
   does not fit), `KOBOLD_CONTEXT` (default `8192`), and `KOBOLD_ARGS` for
   any other KoboldCPP flag (e.g. `--flashattention`).

When you start Pantheon ([6](#6-start-pantheon-and-sign-in)), the KoboldCPP
container starts with it, and Pantheon reaches it by the name `koboldcpp`.
Loading a model takes from a few seconds to several minutes. Check that it
found the GPU:

```sh
docker compose logs koboldcpp
```

Look for `found 1 CUDA devices` and your GPU's name. If there is no such
line, the container got no GPU: see [10](#10-troubleshooting).

The KoboldCPP version is pinned in `backends/koboldcpp/Containerfile`
(`KOBOLDCPP_VERSION`). To move to another release, change it and run
`docker compose build koboldcpp`.

### 4c. ai-stack (Linux + NVIDIA)

If you use the `ai-stack` launcher (also a private JotunheimPress
repository), its KoboldCPP and ComfyUI containers already run on a network
called `ai-net`. Either:

- use `stack companion`, which runs Pantheon through `run-podman.sh` in a
  container called `companion`, with its data in the `data/` folder. See
  ai-stack's own INSTALL.md; or
- use compose, joining the same network. In `.env`:

  ```sh
  COMPOSE_FILE=compose.yaml:compose.ai-stack.yaml
  PIPER_VOICES_DIR=../jarvis              # if you keep Piper voices there
  ```

  Start the backends first (`stack up`). This creates a container called
  `pantheon`, with its data in the `pantheon-data` volume.

Run one or the other, not both: they are separate installs with separate
data. To move between them, see *Move data between installs* in
[7](#7-everyday-tasks).

---

## 5. Optional: ComfyUI for expression sprites

Pantheon draws a character's portrait and per-emotion sprites with ComfyUI.
It uses only ComfyUI's built-in nodes, so **no custom nodes are needed**; one
checkpoint is enough. The model family (SD 1.5, SDXL or Flux) is guessed from
the checkpoint's file name. On a 12 GB card shared with KoboldCPP, an SD 1.5
checkpoint such as `absolutereality_v181` (about 2 GB) is the safe choice.

Install ComfyUI natively:

- **Windows:** the portable package from
  <https://github.com/comfyanonymous/ComfyUI/releases>. There are packages
  for NVIDIA, AMD and Intel (`ComfyUI_windows_portable_nvidia.7z` and so on).
  Extract it with 7-Zip and run the `run_…_gpu.bat` inside. It listens on
  port **8188**.
- **Windows or macOS:** ComfyUI Desktop, from <https://www.comfy.org/download>.
  It listens on port **8000** by default, so set
  `COMFYUI_URL=http://host.docker.internal:8000` in `.env`, or change its
  port in the app's settings.
- **Linux, or manual installs:** follow <https://github.com/comfyanonymous/ComfyUI#installing>
  and start it with `python main.py`.

Put the checkpoint in ComfyUI's `models/checkpoints/` folder and restart
ComfyUI.

ComfyUI listens on `127.0.0.1` by default.

- **Windows and macOS** (Docker Desktop, Podman machine): the container still
  reaches it through `host.docker.internal`.
- **Linux:** a bridged container cannot reach `127.0.0.1`. Either start
  ComfyUI with `--listen 0.0.0.0` (it is then also open to your network, so
  firewall port 8188), or run Pantheon with `compose.host-network.yaml`.

---

## 6. Start Pantheon and sign in

In the `pantheon` folder:

```sh
docker compose up -d
```

(Podman: `podman compose up -d`.) The first time, this builds the image,
which takes several minutes. With `COMPOSE_FILE` set in `.env`, the same
command starts everything you chose.

**Linux, backends on `127.0.0.1` only** (see 4a and 5): use host networking
instead. Pantheon then listens on this computer's `127.0.0.1:7861` directly:

```sh
docker compose -f compose.host-network.yaml up -d
```

(or put `COMPOSE_FILE=compose.host-network.yaml` in `.env`). This file
replaces `compose.yaml` rather than adding to it, and cannot be combined with
the KoboldCPP container. Its default backend addresses become `http://127.0.0.1:5001` and
`http://127.0.0.1:8188`.

Check that it is up. After about half a minute, `docker compose ps` shows
`healthy`. Then ask Pantheon whether it can reach its backends:

```sh
docker compose exec pantheon python3 manage.py backends
```
```
KoboldCPP http://host.docker.internal:5001: OK — model koboldcpp/Qwen3-4B-Instruct-2507-Q8_0
ComfyUI   http://host.docker.internal:8188: OK
```

A `NOT reachable` line comes with the next command to try. See
[10](#10-troubleshooting).

### First sign-in

Open **<http://127.0.0.1:7861>** (or the port you chose). Use `127.0.0.1`;
`localhost` redirects there.

The account is **`ADMIN`**. If you did not set `PANTHEON_ADMIN_PASSWORD`, its
password was generated on the first start. Read it from the log:

```sh
docker compose logs pantheon
```
```
==============================================================
  First run: created admin account ADMIN
  password: pWCSFDfKpqKx49R1
  ...
```

It is also kept in the data volume, in case the log is gone:

```sh
docker compose exec pantheon cat /app/data/auth/INITIAL_PASSWORD
```

Then:

1. Sign in as `ADMIN`.
2. Change the password: **Settings → Account**.
3. Delete the file: `docker compose exec pantheon rm /app/data/auth/INITIAL_PASSWORD`.
4. Add other people from **Settings → Users** (admins only). Each account gets
   its own private library of characters, personas and chats.

The status pill at the top right shows whether KoboldCPP is online (and which
model it has loaded). Import a character card (PNG or JSON, SillyTavern/Chub
format) or make one under **Create**, open it from **Discover**, start a chat,
and send a message.

---

## 7. Everyday tasks

With `COMPOSE_FILE` in `.env`, these commands apply to everything you
started. Without it, repeat the same `-f` options you started with.

**Stop, start, logs**

```sh
docker compose stop                     # stop; data is kept
docker compose start
docker compose logs -f pantheon         # follow the log; Ctrl+C to stop following
docker compose logs -f koboldcpp        # the KoboldCPP container, if you use it
```

Both restart by themselves after a reboot once the engine is running
(Docker Desktop: **Settings → General → Start Docker Desktop when you sign
in**). Rootless Podman on Linux does not restart containers at boot unless you
enable it: `systemctl --user enable podman-restart.service` and
`loginctl enable-linger "$USER"`.

**Update**

```sh
git pull
git -C ../muninn pull
docker compose up -d --build
```

The data volume is untouched. The models baked into the image are only
downloaded again if their build steps changed.

**Back up**

Everything lives in one volume, `pantheon-data`: accounts, characters,
personas, chats, relationships, memories and uploaded avatars. Copy it out:

```sh
docker compose cp pantheon:/app/data ./pantheon-backup
```

That works the same in PowerShell. Keep the copy somewhere else, and
remember it contains everyone's private chats.

**Restore** (into a new, empty install; with an existing one, first run
`docker compose down -v`, which **deletes** the current data):

```sh
docker compose create                   # the container, not started
docker compose cp ./pantheon-backup/. pantheon:/app/data/
docker compose up -d
```

Copy the backup in **before** the first start. Starting on an empty volume
creates a fresh `ADMIN` account, and a restore on top of it leaves two
accounts named `ADMIN`.

**Move data between installs**: to move from a `data/` folder (an older
install, or `stack companion` / `run-podman.sh`) into compose, do the restore
above with `./data/.` in place of `./pantheon-backup/.`. To go the other way,
copy `pantheon:/app/data/.` into `data/` while both containers are stopped.

**Reset a password** (for example, the only admin's):

```sh
docker compose exec -it pantheon python3 manage.py users
docker compose exec -it pantheon python3 manage.py reset-password ADMIN
```

It asks for the new password twice. `--create` makes a new administrator
instead, and `--password-stdin` reads the password from standard input, for
scripts. Browsers already signed in stay signed in.

**Piper voices.** Kokoro's 54 voices are built in. For Piper voices, put each
voice's `.onnx` and `.onnx.json` files in the `voices` folder (or set
`PIPER_VOICES_DIR`) and run `docker compose restart pantheon`. They appear in
the character editor as `piper:<name>`.

**Uninstall**

```sh
docker compose down                     # remove the containers; the volume stays
docker compose down -v                  # …and DELETE all data
docker image rm localhost/companion:latest localhost/pantheon-koboldcpp:latest
```

Then delete the `pantheon` folder. Your models are only in the folders you
put them in.

---

## 8. Without compose

The same thing with plain commands. For Podman, replace `docker` with
`podman`.

```sh
docker build -f Containerfile -t localhost/companion:latest .
docker volume create pantheon-data
docker run -d --name pantheon --restart unless-stopped \
  -p 127.0.0.1:7861:7861 \
  --add-host host.docker.internal:host-gateway \
  -e KOBOLD_URL=http://host.docker.internal:5001 \
  -e COMFYUI_URL=http://host.docker.internal:8188 \
  -e TZ=Europe/Berlin \
  -v pantheon-data:/app/data \
  localhost/companion:latest
```

- `-f Containerfile` is needed with Docker, which otherwise looks for a file
  called `Dockerfile`.
- In PowerShell, replace each `\` at the end of a line with a backtick, or
  put the command on one line.
- With Podman, add `--format docker` to the build if you want the image's own
  healthcheck (Podman drops it from its default image format).
- Add `-v ./voices:/app/models/piper:ro,z` for Piper voices.

The KoboldCPP container on its own, with Podman on Linux:

```sh
podman build -t localhost/pantheon-koboldcpp:latest backends/koboldcpp
podman run -d --name pantheon-koboldcpp -p 127.0.0.1:5001:5001 \
  --device nvidia.com/gpu=all --security-opt label=disable \
  -e KOBOLD_MODEL=my-model.gguf -v ./llm-models:/models:ro,z \
  localhost/pantheon-koboldcpp:latest
```

(With Docker, use `--gpus all` instead of the `--device` and `--security-opt`
options.) Published on `127.0.0.1` like this, it counts as a KoboldCPP on
`127.0.0.1` for [4a](#4a-koboldcpp-natively-any-computer)'s Linux note.

`run-podman.sh` is the Linux/Podman launcher that `stack companion` uses. It
keeps the data in the `data/` folder rather than a volume, and derives `TZ`
from the host.

---

## 9. Reaching Pantheon from other devices

**Not yet.** Pantheon answers on this computer only, at `http://127.0.0.1:7861`
(`localhost` redirects there). Its sign-in runs over plain HTTP, and it allows
that only on this computer's own address, so a phone or another PC gets an
error page instead of the sign-in form, and a reverse proxy in front does not
help yet: Pantheon would still refuse the proxy's host name.

Support for other devices is planned: Pantheon behind HTTPS, with a host name
you choose. Until then, use it on the computer that runs it.

Changing `PANTHEON_BIND` in `.env` no longer opens Pantheon to the network.
Older `.env` files that set `PANTHEON_BIND=0.0.0.0` are harmless; the setting
is ignored.

---

## 10. Troubleshooting

Start with:

```sh
docker compose ps                                       # running? healthy?
docker compose logs --tail 50 pantheon
docker compose exec pantheon python3 manage.py backends
```

**The status dot says KoboldCPP is offline / Pantheon cannot reach KoboldCPP**

`manage.py backends` names the address it tried. Then:

```sh
docker compose exec pantheon python3 manage.py probe host.docker.internal 5001
```

- *Connection refused*: the address is right, but nothing listens there **as
  seen from the container**. Either KoboldCPP is not running, or (on Linux)
  it listens on `127.0.0.1` only. Start it without `--host 127.0.0.1`, or use
  `compose.host-network.yaml` ([6](#6-start-pantheon-and-sign-in)).
- *Name does not resolve*: with `compose.koboldcpp.yaml` the name is
  `koboldcpp`, and that container is not running (`docker compose logs
  koboldcpp`). With `compose.ai-stack.yaml`, ai-stack's backends are not
  started (`stack up`).
- *Timed out*: a firewall, or the wrong address for another computer.
- **Windows or macOS**, and `host.docker.internal` does not reach a backend
  that works in the browser on the same computer: set `KOBOLD_URL` (or
  `COMFYUI_URL`) to the computer's own network address, e.g.
  `http://192.168.1.10:5001`. The backend must then listen on all addresses
  (ComfyUI: `--listen 0.0.0.0`).

**The KoboldCPP container exits with "No model"**: `KOBOLD_MODEL` is empty or
names a file that is not in `KOBOLD_MODELS_DIR`. The log lists the `.gguf`
files it found.

**The KoboldCPP container runs, but slowly, with no GPU**: the log has no
`found 1 CUDA devices` line.

- Podman: use `compose.gpu.podman.yaml`, not the Docker one, and check that
  `nvidia-ctk cdi list` shows `nvidia.com/gpu=all`.
- Docker: check `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.
  On Linux, run `sudo nvidia-ctk runtime configure --runtime=docker` and
  restart Docker.
- SELinux (Fedora, Bazzite, RHEL): the device is denied without
  `label=disable`, which the Podman GPU file sets.

**KoboldCPP runs out of memory** (`cudaMalloc failed`, or it exits while
loading): the model is too large for the GPU. Lower `KOBOLD_GPU_LAYERS` or
`KOBOLD_CONTEXT`, or pick a smaller quantisation. ComfyUI shares the same
VRAM.

**Replies get cut off, or the character forgets the persona in long chats**:
the conversation outgrew KoboldCPP's context size. Raise `--contextsize` /
`KOBOLD_CONTEXT` if the VRAM allows.

**Sprite generation fails**: ComfyUI is optional, so chat is unaffected.
Run `manage.py backends`, and check that ComfyUI has at least one checkpoint.
ComfyUI Desktop listens on port 8000, not 8188.

**I lost the ADMIN password**: `manage.py reset-password ADMIN` ([7](#7-everyday-tasks)).

**`PANTHEON_ADMIN_PASSWORD` was ignored**: it is read only when there are no
accounts yet. Use `reset-password`.

**Times are wrong**: set `TZ` in `.env` and run `docker compose up -d`.

**The port is taken** (`address already in use`): set another
`PANTHEON_PORT` in `.env`. On the ai-stack workstation, a `companion`
container from `stack companion` may already hold 7861.

**`docker build` fails with "failed to read dockerfile"**: add
`-f Containerfile`. Compose does this for you.

**Windows: the KoboldCPP container exits with `exec format error` or
`$'\r': command not found`**: the checkout has Windows line endings. Run
`git config --global core.autocrlf false`, delete the folder, clone again,
and rebuild with `docker compose build --no-cache koboldcpp`.

**Permission denied on a folder you mounted** (Linux with SELinux): the
compose files mount `voices` and `llm-models` with `:z`, which relabels
them. Point `PIPER_VOICES_DIR` / `KOBOLD_MODELS_DIR` only at folders used
for this, never at your whole home or Downloads folder.

---

## 11. What was actually tested

On Linux (Bazzite 44, x86_64, RTX 4070 12 GB), rootless **Podman 5 with
`podman compose`** (Docker Compose v5 as provider), from a clean export of
this repository on 2026-09-26:

- Both images built from the export. `docker compose config` accepts every
  combination of the compose files.
- `compose.yaml` + `compose.koboldcpp.yaml` + `compose.gpu.podman.yaml`:
  KoboldCPP found the RTX 4070 and put all of Qwen3-4B-Instruct-2507 `Q8_0`
  on CUDA. Both containers were healthy and `manage.py backends` reported OK.
  Then a sign-in, a new character, and a chat with a streamed reply from the
  model.
- Browser sign-in (Chromium) with no console errors. The generated
  password appeared in the log and in `INITIAL_PASSWORD`, and the new volume
  was seeded with the default avatar.
- `manage.py users` and `reset-password` (existing user, `--create`, too
  short, unknown user). Data kept across `down` and `up`. Backup with
  `compose cp`, and restore into a fresh volume via `create`. A restore after
  the first start produced the duplicate `ADMIN` described in
  [7](#7-everyday-tasks).
- A KoboldCPP on the host, seen from the default network: bound to
  `127.0.0.1` it was *connection refused*, and bound to all addresses it was
  reachable. `compose.host-network.yaml` reached the `127.0.0.1` one and
  listened on `127.0.0.1` only.
- `compose.ai-stack.yaml` reached a KoboldCPP on `ai-net` by name.
- The dev working tree still builds as `run-podman.sh` builds it.
- An `arm64` image (as on Apple Silicon) builds, under emulation, and starts:
  it serves the sign-in page and creates the first account.
- Under Podman, the Docker GPU form (`compose.gpu.docker.yaml`) gives the
  container **no** GPU (checked with `nvidia-smi`), hence the two files.

**Not tested:**

- Docker itself (the test machine's Docker daemon was not usable without root).
- Windows and macOS: Docker Desktop, and the Podman machine with its
  `host.containers.internal` forwarding.
- Native KoboldCPP and ComfyUI builds.
- ComfyUI sprite generation through `host.docker.internal`.
- The reverse-proxy recipes.
