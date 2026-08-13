# syntax=docker/dockerfile:1
FROM python:3.12-slim

LABEL org.opencontainers.image.title="La Cantina"
LABEL org.opencontainers.image.description="Character-card AI companion (FastAPI + KoboldCPP)"

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libglib2.0-0 libgomp1 espeak-ng libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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

COPY main.py run.py ./
COPY models/ ./models/
COPY services/ ./services/
COPY templates/ ./templates/
COPY static/ ./static/

RUN mkdir -p /app/data/characters /app/data/personas /app/data/sessions /app/data/expressions /app/data/settings

# KoboldCPP URL — override with -e KOBOLD_URL=http://... at runtime.
ENV KOBOLD_URL=http://koboldcpp:5001

EXPOSE 7861

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fs http://localhost:7861/api/status || exit 1

CMD ["python3", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7861"]
