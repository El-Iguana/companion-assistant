# syntax=docker/dockerfile:1
# Python 3.13: pytincture (the new page, PYTINCTURE_PLAN.md) needs 3.13+. Every other
# requirement was checked to install on it (2026-10-26).
ARG PYTHON_IMAGE=docker.io/library/python:3.13-slim

# ── wapyt, the new page's widgetset: not on PyPI, so built from GitHub at a pinned commit.
# Two wheels: 0.2.0 for the server's Python (pytincture's widgetset discovery reads it) and
# 99.99.99 for the BROWSER, with its asset manifest regenerated for that version
# (pytincture hash-checks every widget asset). Same recipe and commit as ai-stack/stack-ui
# and ~/Development/Pytinc/basilisk_wapyt. Local checkout instead:
#   podman build --build-context wapyt-src=../../Pytinc/wa_pytincture_widgetset .
FROM ${PYTHON_IMAGE} AS wapyt-git
ARG WAPYT_REPO=https://github.com/WAwesome-AI/wa_pytincture_widgetset.git
ARG WAPYT_REF=0457960bca1481ffee0c4e778128e5e4a4f295b5
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none "$WAPYT_REPO" /src \
    && git -C /src checkout --quiet "$WAPYT_REF" \
    && rm -rf /src/.git

FROM scratch AS wapyt-src
COPY --from=wapyt-git /src /

FROM ${PYTHON_IMAGE} AS wapyt-wheels
COPY --from=wapyt-src / /wapyt
RUN set -eu; \
    pip wheel --no-cache-dir --no-deps /wapyt -w /wheels/server; \
    cp -r /wapyt /dev-src; \
    sed -i 's/^version = ".*"$/version = "99.99.99"/' /dev-src/pyproject.toml; \
    grep -q "pytincture-assets.json" /dev-src/MANIFEST.in \
        || sed -i '1i include wapyt/pytincture-assets.json' /dev-src/MANIFEST.in; \
    python /wapyt/scripts/generate_assets_manifest.py /dev-src; \
    pip wheel --no-cache-dir --no-deps /dev-src -w /wheels/browser; \
    ls /wheels/server/wapyt-*.whl /wheels/browser/wapyt-99.99.99-py3-none-any.whl

# ── Pantheon ─────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="Pantheon"
LABEL org.opencontainers.image.description="Character-card AI companion (FastAPI + KoboldCPP)"

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libglib2.0-0 libgomp1 espeak-ng libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=wapyt-wheels /wheels/server/ /tmp/wapyt/
RUN pip install --no-cache-dir /tmp/wapyt/wapyt-*.whl && rm -rf /tmp/wapyt

# Bake the background-removal models into the image (no runtime download).
ENV U2NET_HOME=/app/models/rembg
RUN python3 -c "from rembg import new_session; new_session('isnet-anime'); new_session('u2net')"

# Bake the Kokoro TTS model (ONNX, CPU) into the image.
ENV KOKORO_MODEL=/app/models/kokoro/kokoro-v1.0.onnx
ENV KOKORO_VOICES=/app/models/kokoro/voices-v1.0.bin
RUN mkdir -p /app/models/kokoro && \
    curl -fsSL -o "$KOKORO_MODEL" https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx && \
    curl -fsSL -o "$KOKORO_VOICES" https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin

# Piper voices (one .onnx + .onnx.json per voice) are NOT baked — the shared
# ~/Development/ai_ecosystem/jarvis dir is mounted here read-only at run time.
ENV PIPER_DIR=/app/models/piper
RUN mkdir -p ${PIPER_DIR}

# Bake the embedding model for semantic recall (MEMORY_PLAN Phase 4).
# int8-quantized BGE-small-en-v1.5, 33 MB, CPU-only via the onnxruntime already
# here for rembg — deliberately NOT the 127 MB fp32 build: both scored 5/5 top-1
# on the same retrieval probe with cosine scores within 0.02, so fp32 bought
# nothing but image size. No VRAM is used; the GPU stays for KoboldCPP/ComfyUI.
ENV EMBED_DIR=/app/models/embed
ENV EMBED_MODEL=${EMBED_DIR}/model.onnx
ENV EMBED_TOKENIZER=${EMBED_DIR}/tokenizer.json
RUN mkdir -p ${EMBED_DIR} && \
    curl -fsSL -o "$EMBED_MODEL" https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/onnx/model_quantized.onnx && \
    curl -fsSL -o "$EMBED_TOKENIZER" https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/tokenizer.json

# Bake the faster-whisper STT model (CPU int8) into the image.
ENV WHISPER_MODEL=base.en
ENV WHISPER_CACHE=/app/models/whisper
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8', download_root='${WHISPER_CACHE}')"

COPY main.py run.py manage.py service.py ./
COPY models/ ./models/
COPY services/ ./services/
COPY templates/ ./templates/
COPY static/ ./static/
# The new (pytincture) page: browser code + BFF classes, and its server-side helpers.
COPY pantheon_web/ ./pantheon_web/
COPY appcode/ ./appcode/
COPY --from=wapyt-wheels /wheels/browser/wapyt-99.99.99-py3-none-any.whl ./appcode/

RUN mkdir -p /app/data/characters /app/data/personas /app/data/sessions /app/data/expressions /app/data/settings

# The redistributable default avatar. A new, empty named volume is filled from
# the image the first time it is mounted, so compose installs start with it; a
# bind-mounted data/ (run-podman.sh) hides this copy and has its own.
COPY data/vrm/baseline.vrm /app/data/vrm/baseline.vrm

# KoboldCPP URL — override with -e KOBOLD_URL=http://... at runtime.
ENV KOBOLD_URL=http://koboldcpp:5001

# The new page binds all interfaces INSIDE the container so a published port reaches it;
# run-podman.sh / compose publish it on 127.0.0.1 only, because its login is loopback-only.
# PANTHEON_WEB_PORT must equal the published host port: it is also the page's origin.
ENV PANTHEON_WEB_BIND=0.0.0.0 \
    PANTHEON_WEB_PORT=7866

EXPOSE 7861 7866

# HEALTHCHECK is a docker-format field with no OCI slot, so podman drops it unless the build
# passes --format docker (run-podman.sh does). Without that flag this block is silently inert.
#
# Probes `/`, not `/api/status`. Two reasons, both of which would have made this container
# report unhealthy while working fine: /api/status is behind `current_user`, so unauthenticated
# it answers 401 and `curl -f` treats that as failure; and it proxies KoboldCPP, so it would
# also fail whenever the *backend* is down. `/` renders the login page — it exercises uvicorn
# and Jinja without depending on either.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fs http://localhost:7861/ || exit 1

# One process, two servers: the classic page on 7861 and the new page on 7866, sharing
# Pantheon's state (service.py; PYTINCTURE_PLAN.md).
CMD ["python3", "service.py"]
