#!/bin/sh
# Start KoboldCPP on the model named by KOBOLD_MODEL (a .gguf in /models).
# KOBOLD_GPU_LAYERS: layers to put on the GPU (-1 = all; lower it if VRAM runs
# out). KOBOLD_CONTEXT: context size in tokens. Extra flags: KOBOLD_ARGS.
set -eu

MODEL="${KOBOLD_MODEL:-}"
if [ -z "$MODEL" ]; then
    MODEL="$(ls /models/*.gguf 2>/dev/null | head -1 | xargs -r basename)"
fi
if [ -z "$MODEL" ] || [ ! -f "/models/$MODEL" ]; then
    echo "No model: set KOBOLD_MODEL in .env to one of the .gguf files in KOBOLD_MODELS_DIR." >&2
    echo "Found in /models:" >&2
    ls /models/*.gguf 2>/dev/null | xargs -r -n1 basename | sed 's/^/  /' >&2 || true
    exit 1
fi

echo "KoboldCPP: model $MODEL, gpu layers ${KOBOLD_GPU_LAYERS:--1}, context ${KOBOLD_CONTEXT:-8192}"
# shellcheck disable=SC2086
exec /usr/local/bin/koboldcpp \
    --host 0.0.0.0 --port 5001 \
    --usecublas \
    --gpulayers "${KOBOLD_GPU_LAYERS:--1}" \
    --contextsize "${KOBOLD_CONTEXT:-8192}" \
    --model "/models/$MODEL" \
    ${KOBOLD_ARGS:-}
