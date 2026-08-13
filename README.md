# La Cantina

A self-hosted, fully-local **character-card AI companion**. FastAPI backend + a no-build vanilla-JS
single-page frontend. All inference runs on your own machine — no cloud, no external services, no
database (state is plain JSON files).

Built around the **Character Card V2/V3** spec (SillyTavern / Chub compatible), so cards import and
export losslessly.

## Features

- **Streaming chat** with swipes (alternate replies) and rolling long-term memory
- **Character Card V2/V3** import/export (PNG + JSON), full in-app card editor with lorebook
- **Personas** — your `{{user}}` card, injected into the prompt
- **Expression sprites** via ComfyUI — one base portrait → per-emotion sprites (same character),
  transparent cutouts, Core-8 / Full-28 sets, with a live mood-driven portrait
- **Relationship system** — multi-axis (affection / trust / attraction / tension), stages,
  intimacy/consent with agency-preserving behavior, milestones, stage-gated lorebook, scenario presets
- **Local voice** — Kokoro + Piper TTS (per-character voice) + push-to-talk & hands-free faster-whisper STT
- **Multi-user** — login with `admin`/`user` roles; each account has its own **private, isolated**
  library of characters, personas, and presets
- **Roleplay vs assistant** chat modes, custom system prompts, and a `/no_think` speed toggle

## Running

Part of the [`ai-stack`](https://github.com/El-Iguana/ai-stack) launcher:

```bash
stack up            # start the backends (KoboldCPP + ComfyUI)
stack companion     # build (first run) + start on http://localhost:7861
```

…or directly:

```bash
./run-podman.sh              # build + run on :7861
./run-podman.sh --rebuild    # rebuild after code/template/static changes
```

First run seeds an **ADMIN** account — log in, then change the password and add users from the
**Users** panel. In-app **?** button documents custom system prompts and curation.

## Architecture

Runs as a Podman container on the shared `ai-net` network.

| Component | Role |
|-----------|------|
| this app (`:7861`) | FastAPI + SPA |
| KoboldCPP (`koboldcpp:5001`) | LLM text generation |
| ComfyUI (`:8188`) | expression-sprite generation |

Data lives under `data/` (git-ignored): `data/auth/` (accounts + signing secret) and
`data/u/<user>/` (each user's isolated library). See [`CLAUDE.md`](CLAUDE.md) for the full internals.

## Note

Local, private, and uncensored by design (it runs an uncensored local model). All characters are
adult. Intended for personal, self-hosted use.
