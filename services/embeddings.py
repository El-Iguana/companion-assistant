"""Local sentence embeddings — BGE-small-en-v1.5, ONNX, CPU.

Runs on the onnxruntime already present for rembg/Kokoro/whisper, with the
`tokenizers` wheel that faster-whisper already pulls in: no torch, no
sentence-transformers, no cloud, and **zero VRAM** — the 4070 stays entirely
for KoboldCPP and ComfyUI (see MEMORY_PLAN's settled decisions).

The int8-quantized model is baked at /app/models/embed (33 MB). It was picked
over fp32 (127 MB) by measurement, not guesswork: on a companion-shaped corpus
both scored 5/5 top-1 on the same probe queries, with cosine scores within
0.02 of each other, so fp32 bought nothing but image size.

Everything here is best-effort by contract. `available()` is false when the
model isn't baked in, and `encode()` raises rather than returning junk —
callers in services/memory.py swallow both, because semantic recall going
missing must never take a chat turn with it.
"""
from __future__ import annotations

import os
import threading

import numpy as np

MODEL_PATH = os.environ.get("EMBED_MODEL", "/app/models/embed/model.onnx")
TOKENIZER_PATH = os.environ.get("EMBED_TOKENIZER", "/app/models/embed/tokenizer.json")
MAX_TOKENS = 512
DIM = 384

# BGE is trained asymmetrically: queries get this instruction prefix, stored
# passages do not. Skipping it measurably degrades ranking.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_lock = threading.Lock()
_session = None
_tokenizer = None
_inputs: set[str] = set()


def available() -> bool:
    return os.path.exists(MODEL_PATH) and os.path.exists(TOKENIZER_PATH)


def _load():
    """Build the session once, under a lock — chat turns arrive concurrently
    (each bridge/request runs in its own thread) and two racing first calls
    would otherwise each pay the load cost and leave one session orphaned."""
    global _session, _tokenizer, _inputs
    with _lock:
        if _session is None:
            import onnxruntime as ort
            from tokenizers import Tokenizer
            if not available():
                raise FileNotFoundError(f"embedding model not found at {MODEL_PATH}")
            tok = Tokenizer.from_file(TOKENIZER_PATH)
            tok.enable_truncation(MAX_TOKENS)
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = int(os.environ.get("EMBED_THREADS", "2"))
            _session = ort.InferenceSession(MODEL_PATH, opts,
                                            providers=["CPUExecutionProvider"])
            _inputs = {i.name for i in _session.get_inputs()}
            _tokenizer = tok
        return _session, _tokenizer


def encode(texts: list[str], is_query: bool = False) -> np.ndarray:
    """Embed `texts` -> (N, DIM) float32, L2-normalized so a dot product IS
    cosine similarity. Raises if the model is unavailable."""
    if not texts:
        return np.zeros((0, DIM), dtype=np.float32)
    session, tok = _load()
    if is_query:
        texts = [QUERY_PREFIX + t for t in texts]

    enc = [tok.encode(t) for t in texts]
    width = max(len(e.ids) for e in enc)
    ids = np.array([e.ids + [0] * (width - len(e.ids)) for e in enc], dtype=np.int64)
    mask = np.array([e.attention_mask + [0] * (width - len(e.attention_mask))
                     for e in enc], dtype=np.int64)
    feed = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in _inputs:
        feed["token_type_ids"] = np.zeros_like(ids)

    # BGE pools the CLS token (position 0), not the mean — mean pooling here
    # would quietly produce worse rankings rather than an obvious error.
    out = session.run(None, feed)[0][:, 0]
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return (out / np.maximum(norms, 1e-12)).astype(np.float32)
