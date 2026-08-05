# AI Companion — VTuber Upgrade Plan (rev. 2026-08)

Starting brief for building the VTuber layer *inside the existing companion app*. Revised to fit the
app's current architecture (rich emotion/mood/relationship systems, transparent expression sprites)
and the Bazzite / RTX 4070, local-first environment.

## Goal
Turn the companion into a **live, voiced, animated presence**: she speaks (TTS), listens (STT),
lip-syncs, and emotes in real time — reusing the emotion/mood/relationship engine already built.

## Current state (what we already have — reuse it)
- FastAPI backend, vanilla-JS frontend, KoboldCPP streaming (`chat_generate_stream`, SSE).
- **Emotion system**: post-reply classifier (Core-8 / Full-28), running **mood/valence**, per-swipe emotions.
- **Relationship system**: multi-axis + intimacy/consent, stage, milestones — drives behavior and affect.
- **Transparent expression sprites** (img2img, same-character) with a live-swapping portrait.
- A `<think>`-block stripper in `kobold.py` (the pattern we reuse for tag-stripping).

## Design principles
1. **Fully local voice.** This app has intimate/adult content — no cloud STT/TTS. Everything on the 4070,
   containerized like `koboldcpp` (Containerfile + `sillytavern-net`).
2. **Hybrid expression, not a rewrite.** Combine three signals instead of replacing what exists:
   - **Resting affect** from the running **mood + relationship** (baseline face/posture).
   - **Momentary beats** from inline **`[expr:x]` tags** the LLM emits (mid-sentence changes).
   - **Fallback** to the existing classifier when the model forgets to tag.
3. **Sprite-first, VRM later.** Get "alive" fast on the existing sprites (voice + mouth flap), then add a
   3D VRM avatar as a visual upgrade — don't front-load the riskiest piece.
4. **Latency/turn-taking is the core engineering problem**, not a step. Budget for it.

## Target architecture

```
Mic → local STT (whisper.cpp, streaming) ─┐
                                          ▼
        KoboldCPP (streaming) ── emits text + inline [expr:x] tags
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              ▼                           ▼                            ▼
   strip tags → sentence-chunk    parse [expr] events        resting affect from
        → local TTS (streaming)   → avatar expression         mood + relationship
              │                     (blendshape/sprite)
              ▼
   audio out + amplitude → mouth blendshape (lip-sync)   ← barge-in cuts playback
```

## Phasing (revised)

✅ **Phase 0 (done) — Voice + lip-sync on the EXISTING sprites**
- Local **Kokoro TTS** (ONNX/CPU, reuses onnxruntime; 54 voices; model baked at `/app/models/kokoro`).
  `POST /api/tts` (text→WAV, strips `*action*` narration) + `GET /api/tts/voices` (`services/tts.py`).
- Frontend: `🔇/🔊` toggle; **mid-stream** streaming queue that speaks **only quoted dialogue** (skips
  narration + `*actions*`; falls back to the whole reply if it has no quotes), enqueued + prefetched +
  played via Web Audio *as the reply generates*; **amplitude → portrait scaleY** mouth movement;
  **barge-in** (new message stops current speech). ~0.46 s/line synth.
- ✅ **Per-character voice** (`CharacterCard.tts_voice`) — picked in the character editor (54 voices +
  ▶ test); chat uses the current card's voice (fallback `af_sarah`); header ▶ auditions it.
- TODO next: better mouth (open-mouth sprite variant vs the pulse).

**Phase 1 — Voice input + barge-in** (hands-free loop)
- ✅ **Push-to-talk STT (done)** — local **faster-whisper** (`base.en`, CPU int8, baked into the
  companion image at `/app/models/whisper`; in-process like Kokoro, *not* a separate GPU container —
  see deviation note below). `services/stt.py` + `POST /api/stt` (multipart audio → `{text}`,
  `vad_filter` on). Frontend: hold-to-talk 🎤 in the composer (pointer events, MediaRecorder →
  WebM/Opus → `/api/stt`), transcript auto-sends. Verified via a TTS→STT round-trip (WAV + WebM/Opus,
  exact transcript, ~0.7 s).
- ✅ **Barge-in (done)** — pressing 🎤 calls `stopSpeaking()` (cuts TTS) **and** `abortChat()`
  (AbortController aborts the in-flight `/api/chat` stream; server generator cancels best-effort).
- ◻ **Next — continuous / hands-free listening** (currently push-to-talk only). Requirements:
  - **Separate mic control**: a distinct **toggle** for continuous mode (e.g. 🎙 "hands-free"),
    *separate from* the existing hold-to-talk 🎤 (push-to-talk). Two independent controls — PTT stays
    for one-shot input; the toggle arms/disarms the open-mic loop. Persist the toggle state.
  - **Local VAD** to segment utterances hands-free (no button): detect speech start/end in-browser
    (e.g. Web Audio energy/RMS gate or a small local VAD like Silero-VAD ONNX), POST each segment to
    `/api/stt`, auto-send. Keep it fully local (no cloud), CPU-friendly like the rest of the voice stack.
  - **Configurable wake word**: an optional, user-set phrase that arms listening / triggers barge-in
    while she's speaking. Must use a **local** wake-word engine (e.g. **openWakeWord** — open, ONNX,
    CPU; fits the in-process precedent). Wake word + on/off are a **user setting** (persisted; likely a
    small settings store + editor UI). When set, continuous mode only acts on utterances after the wake
    word; when blank, the toggle behaves as pure open-mic.
  - **Auto barge-in on detected speech** during TTS playback (reuse `stopSpeaking()` + `abortChat()`),
    gated by the wake word if one is configured (avoid the avatar interrupting herself / room noise).

> **Deviation from the original plan:** this shipped as **in-process CPU faster-whisper**, not a GPU
> whisper.cpp container on sillytavern-net. Reason: matches how Kokoro TTS actually shipped (in-process,
> CPU, baked), avoids VRAM contention with Kobold+ComfyUI, and `base.en` is fast enough for short clips.
> The GPU whisper.cpp container remains the upgrade path if latency/accuracy needs it — swap by pointing
> `WHISPER_*` / `services/stt.py` at the container.

**Phase 2 — Expression channel (hybrid)**
- System-prompt the model to emit inline `[expr:x]` tags; add a streaming tag-parser (reuse the
  `<think>`-stripper approach) that strips tags from display/TTS and emits expression events.
- Map tags to the existing emotion vocabulary; blend with mood/relationship resting affect; classifier fallback.

**Phase 3 — VRM avatar** (visual upgrade)
- Load a `.vrm` with `@pixiv/three-vrm` + `three.js` (via ESM import-map; likely a separate `/vtuber` view).
- Map emotion set → the ~5–6 VRM expression presets (neutral/happy/angry/sad/relaxed/surprised) + visemes.
- Amplitude → viseme/jaw blendshape for lip-sync; idle blink + sway.

**Phase 4 — Polish**
- Expression transition smoothing (no instant snaps), idle micro-motion, camera framing.
- Failure fallbacks (what the avatar does if STT/TTS/LLM stalls).

## Component choices (local-first)
- **TTS**: **Kokoro** (fast, good quality) or **XTTS-v2** (expressive, voice-cloneable) — start Kokoro for
  latency, offer XTTS for quality. Piper as a lightweight fallback. Streaming/sentence-chunked.
- **STT**: **whisper.cpp** (CUDA) — `base`/`small` for latency, streaming partials.
- **VRM in browser**: `three.js` + `@pixiv/three-vrm`.
- **VRM sourcing on Linux**: VRoid Studio has **no native Linux build** → use the **VRM Add-on for Blender**
  (native; fits the game-dev plan) or grab models from **VRoidHub**. (Optionally generate art → mesh → Blender → VRM.)

## Integration points (reuse, don't rebuild)
- `kobold.chat_generate_stream` for the token stream; add a tag-parsing wrapper alongside the `<think>` stripper.
- Emotion set (`expressions.CORE/FULL_EMOTIONS`) as the tag vocabulary; classifier as fallback.
- `session.mood` + `relationship.summary()` for resting affect / posture.
- Existing sprites for Phase 0/2 avatar; keep the SPA — add a togglable VTuber view.
- New TTS/STT as containers on `sillytavern-net` (koboldcpp pattern); mind VRAM sharing with Kobold+ComfyUI.

## Expression tag protocol (keep simple + versioned)
- `[expr:label]` inline, label ∈ the emotion set. Parser is stream-safe (handles tags split across tokens).
- Version the label→VRM-preset map so it can grow without breaking saved content.

## Open decisions
- Phase-0 avatar: sprite mouth-swap vs overlay/mask lip-sync.
- TTS engine: Kokoro (speed) vs XTTS (expressive) vs Piper.
- In-app VTuber view vs separate `/vtuber` page (three.js is heavy for the no-build SPA).
- VRM sourcing: Blender VRM addon vs VRoidHub.
- STT trigger: push-to-talk vs continuous VAD.

## Notes for Claude Code
- Reuse the streaming/backend + emotion/mood/relationship systems — only voice I/O, the tag layer, and the
  avatar renderer are new.
- Local voice is a hard requirement (privacy). Watch VRAM: Kobold + (ComfyUI) + TTS/STT on one 4070.
- Start Phase 0 end-to-end (voice + lip-sync on sprites) before touching VRM.
