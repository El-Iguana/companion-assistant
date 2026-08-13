#!/usr/bin/env bash
# ============================================================
# La Cantina — Podman run script
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="localhost/companion:latest"
CONTAINER="companion"
NETWORK="ai-net"
PORT="7861"

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
  podman build -t "$IMAGE" -f "$SCRIPT_DIR/Containerfile" "$SCRIPT_DIR"
fi

if podman container exists "$CONTAINER"; then
  echo "==> Removing existing container $CONTAINER …"
  podman rm -f "$CONTAINER"
fi

mkdir -p "$SCRIPT_DIR"/data/{characters,personas,sessions,relationships,presets,expressions,settings}

if podman network exists "$NETWORK"; then
  NET_ARG="--network=$NETWORK"
  KOBOLD_URL="http://koboldcpp:5001"
  echo "==> Joining network: $NETWORK (KoboldCPP reachable by container name)"
else
  NET_ARG="--network=host"
  KOBOLD_URL="http://localhost:5001"
  echo "==> Network '$NETWORK' not found; falling back to host networking"
fi

echo "==> Starting $CONTAINER on port $PORT …"
podman run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  $NET_ARG \
  -p "${PORT}:7861" \
  -e "KOBOLD_URL=${KOBOLD_URL}" \
  -v "$SCRIPT_DIR/data:/app/data:z" \
  "$IMAGE"

echo ""
echo "✓  La Cantina is running."
echo "   Open: http://localhost:${PORT}"
echo "   Logs: podman logs -f $CONTAINER"
echo "   Stop: podman stop $CONTAINER"
