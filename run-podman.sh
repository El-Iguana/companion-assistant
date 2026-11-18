#!/usr/bin/env bash
# ============================================================
# Pantheon assistant — Podman run script
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="localhost/companion-assistant:latest"
CONTAINER="companion-assistant"
NETWORK="ai-net"
# Loopback only: the login (pytincture over plain HTTP) refuses anything else. Container and
# host use the same number because it is also the page's origin.
PORT="7867"

REBUILD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rebuild|-r) REBUILD=1; shift ;;
    --port|-p)    PORT="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: $0 [--rebuild] [--port PORT]"
      exit 0 ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

if [[ "$REBUILD" -eq 1 ]] || ! podman image exists "$IMAGE"; then
  echo "==> Building image $IMAGE …"
  # amd64 pinned HERE (the workstation's build), not in the Containerfile: compose installs
  # on other machines — Apple Silicon included — must keep building for their own arch. The
  # pin is what keeps a wrongly-cached base from slipping in: a python:3.12-slim pulled as
  # arm64 once had Iguana's container running under emulation (2026-10-27).
  # Muninn (the shared memory package) from the sibling checkout when there is one, so a
  # change there is in the next rebuild; otherwise the Containerfile's pinned commit.
  MUNINN_CTX=()
  if [[ -f "$SCRIPT_DIR/../muninn/pyproject.toml" ]]; then
    echo "==> Muninn from the local checkout $(cd "$SCRIPT_DIR/../muninn" && pwd)"
    MUNINN_CTX=(--build-context "muninn-src=$SCRIPT_DIR/../muninn")
  fi
  podman build --format docker --platform linux/amd64 "${MUNINN_CTX[@]}" \
    -t "$IMAGE" -f "$SCRIPT_DIR/Containerfile" "$SCRIPT_DIR"
fi

if podman container exists "$CONTAINER"; then
  echo "==> Removing existing container $CONTAINER …"
  podman rm -f "$CONTAINER"
fi

mkdir -p "$SCRIPT_DIR"/data/{auth,u,vrm}

# Both backend URLs have to move together. They used to not: only KOBOLD_URL was set
# here, so the fallback left COMFYUI_URL at its by-name default (main.py) and sprite
# generation died on DNS while chat worked — on exactly the path someone without the
# full ai-stack takes.
if podman network exists "$NETWORK"; then
  NET_ARG="--network=$NETWORK"
  KOBOLD_URL="http://koboldcpp:5001"
  COMFYUI_URL="http://comfyui:8188"
  echo "==> Joining network: $NETWORK (backends reachable by container name)"
else
  NET_ARG="--network=host"
  KOBOLD_URL="http://localhost:5001"
  COMFYUI_URL="http://localhost:8188"
  echo "==> Network '$NETWORK' not found; falling back to host networking"
fi

# First-run admin password. Forwarded only if you set it; otherwise the app mints a
# random one and prints it (podman logs companion-assistant) + writes data/auth/INITIAL_PASSWORD.
SEED_ARG=()
if [[ -n "${COMPANION_ADMIN_PASSWORD:-}" ]]; then
  SEED_ARG=(-e "COMPANION_ADMIN_PASSWORD=${COMPANION_ADMIN_PASSWORD}")
  echo "==> Seeding the admin password from COMPANION_ADMIN_PASSWORD (first run only)"
fi

# Timezone. The container clock is NOT wrong — it shares the host kernel, so the instant
# is identical. But the image ships no TZ and /etc/localtime -> Etc/UTC, so everything the
# app renders as local time came out in UTC: `created_at` on cards/sessions/memories, log
# lines, and what an assistant character answers when asked the time (5pm EDT -> "9pm").
# Derived from the host rather than hardcoded, so it follows the machine; override by
# exporting TZ. tzdata is present in the image, so the zone name resolves.
TZ_VAL="${TZ:-$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)}"
echo "==> Timezone: $TZ_VAL"

# Piper TTS voices (shared, read-only) — skipped if the dir isn't there.
PIPER_SRC="$(dirname "$SCRIPT_DIR")/jarvis"
PIPER_ARG=()
if [[ -d "$PIPER_SRC" ]]; then
  PIPER_ARG=(-v "$PIPER_SRC:/app/models/piper:ro,z")
  echo "==> Mounting Piper voices from $PIPER_SRC"
fi

echo "==> Starting $CONTAINER on port $PORT …"
podman run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  $NET_ARG \
  -p "${PORT}:${PORT}" \
  -e "PANTHEON_PORT=${PORT}" \
  -e "KOBOLD_URL=${KOBOLD_URL}" \
  -e "COMFYUI_URL=${COMFYUI_URL}" \
  -e "TZ=${TZ_VAL}" \
  -v "$SCRIPT_DIR/data:/app/data:z" \
  "${SEED_ARG[@]}" \
  "${PIPER_ARG[@]}" \
  "$IMAGE"

echo ""
echo "✓  Cantinero is running."
echo "   Open: http://127.0.0.1:${PORT}/   (other devices: http://$(hostname -s).local:${PORT}/)"
echo "   Logs: podman logs -f $CONTAINER"
echo "   Stop: podman stop $CONTAINER"
