# Pantheon — Project Context

The product name is **Pantheon** (formerly *La Cantina* — renamed 2026-08-27; the old name may still
appear in older commits and notes). The project directory, image, container, and `stack` command all
remain `companion` (renaming them was deliberately deferred).

A self-hosted **character-card AI companion**. FastAPI + uvicorn, runs as a Podman container,
talks to **KoboldCPP** (text) and **ComfyUI** (portraits/expressions) on the shared
`ai-net` network. Everything local; no external services, no database.

Built around the **Character Card V2/V3 spec** (SillyTavern/Chub compatible). Scaffolded from
`~/Development/ai_ecosystem/ai_story_maker` (reused `services/kobold.py` + `services/comfyui.py`).
The page is **pytincture + wapyt** (Python in the browser, `appcode/`), laid out after
Emberfolk (perchance.org/character-ai-ember); voice and the 3D avatar are two JavaScript islands
(`static/js/voice.js`, `static/js/vtuber.mjs`). It replaced the vanilla-JS page on 2026-10-26 —
plan and decision log in **`PYTINCTURE_PLAN.md`**.

Companion features layered on top: streaming chat, swipes, transparent expression sprites with a
live-swapping portrait, a running **mood/valence**, and a **relationship system** (multi-axis +
intimacy/consent). Relationship design detail in `RELATIONSHIP_PLAN.md`.

## Running

```bash
./run-podman.sh              # build (first run) + start: http://127.0.0.1:7861/
./run-podman.sh --rebuild    # REBUILD after ANY code/static change (baked into the image)
# logs: podman logs -f companion ; stop: podman stop companion
```

**`http://127.0.0.1:7861/`, this computer only.** pytincture signs users in over plain HTTP only
on a literal loopback Host, so the container publishes `127.0.0.1:7861` and `localhost` is
redirected to `127.0.0.1` (`service.LocalhostRedirect`). Phones and other machines need TLS in
front — PYTINCTURE_PLAN.md Phase 8. Accounts are Pantheon's own (`pantheon_web/auth.py` checks
them with `services.auth`); the session cookie key is `data/auth/pytincture.key`.

The container runs **`service.py`**: `main` (Pantheon itself — accounts, data, clients, every
handler) plus the pytincture app, whose BFF classes call `main`'s handlers directly. `main.app`'s
route table is **no longer served**; it stays as the handlers' contract and for the unit tests.
`/desktop` is the desktop avatar's page (`pantheon_web/desktop.py`, below).

```bash
# host-side dev against a THROWAWAY library — never the real data/:
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt pip pytest \
    ~/Development/Pytinc/wa_pytincture_widgetset
PYTHON=$PWD/.venv/bin/python ~/Development/Pytinc/wa_pytincture_widgetset/scripts/dev_wheel.sh appcode
PANTHEON_DATA_DIR=/tmp/pantheon-dev scripts/web-dev.sh start   # :7871 (PANTHEON_DEV_PORT), pidfile;
                                                                # stop|restart|log; refuses the real data/
PANTHEON_URL=http://127.0.0.1:7871 PANTHEON_USER=… PANTHEON_PASS=… \
  CHROME=~/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome \
  uv run --with playwright python tests/smoke_web.py      # every screen, fails on any console error
.venv/bin/python -m pytest -q tests/test_messages.py tests/test_chat_stream.py
```

| var | default | what |
|---|---|---|
| `PANTHEON_PORT` | 7861 | listen port **and** the port in the address bar — publish it on the same number |
| `PANTHEON_BIND` | 127.0.0.1 | listen address; the image sets 0.0.0.0 and publishes on 127.0.0.1 only |
| `PANTHEON_DATA_DIR` | `./data` | another library (dev/test); read by `main.py` |
| `KOBOLD_URL` / `COMFYUI_URL` | `http://koboldcpp:5001` / `http://comfyui:8188` | backends |
| `ORPHEUS_URL` | `http://orpheus-tts:5005` | the stack's on-demand Orpheus TTS (expressive voice); absent → those voices aren't offered |

Health: `GET /api/health` (public, `{"ok": true}`) — what `stack status` and the compose
healthchecks probe. `stack companion` opens `http://localhost:7861/`, which redirects.

Prereqs: `~/Development/ai_ecosystem/koboldcpp/start.sh` (text). For sprites: `~/bin/comfyui.sh` (ComfyUI in the `comfyui`
distrobox on :8188). They share VRAM — use SD 1.5 checkpoints (e.g. `absolutereality_v181`) when both
run. **Data (`data/…`) is a mounted volume**, so editing card/session/relationship JSON is live; **code
is baked in — rebuild after edits.** `podman exec` needs `-i` to receive a stdin heredoc.

### Installing elsewhere: compose (2026-09-26)

**INSTALL.md** is the user guide: Windows/macOS/Linux, Docker or Podman. `run-podman.sh` (what
`stack companion` runs) is unchanged and still the workstation path. Compose is for everyone else:

| file | what |
|---|---|
| `compose.yaml` | container `pantheon`, `127.0.0.1:${PANTHEON_PORT:-7861}`, named volume `pantheon-data`, backends at `host.docker.internal` |
| `compose.koboldcpp.yaml` | KoboldCPP container (`backends/koboldcpp/`, official CUDA binary) + `KOBOLD_URL=http://koboldcpp:5001` |
| `compose.gpu.docker.yaml` / `compose.gpu.podman.yaml` | its GPU: `deploy…devices` vs CDI `nvidia.com/gpu=all` + `label=disable` |
| `compose.ai-stack.yaml` | join external `ai-net`, backends by name |
| `compose.host-network.yaml` | standalone (not an override): Linux, backends on 127.0.0.1; uvicorn binds 127.0.0.1 itself |

`manage.py`: `backends`, `probe HOST PORT`, `users`, `reset-password [--create] [--password-stdin]`.
It writes `data/auth/users/<id>.json` directly with `services.auth`, so keep it in step with
`_save_user` and the scrypt params.

Traps, all measured:

- **Podman ignores Docker's GPU form.** `deploy.resources.reservations.devices` gives the container
  no GPU and no error (`nvidia-smi` not found), so there are two GPU files.
- **From a bridge network, `host.*.internal` does not reach 127.0.0.1-only host services**
  (connection refused; an all-interfaces port is reachable). ComfyUI binds 127.0.0.1 by default.
  Hence `compose.host-network.yaml` and `manage.py probe`.
- **Restoring into a volume after the first start duplicates ADMIN.** The first start seeds an
  account; the backup brings another with the same name. Restore = `compose create`, `compose cp`,
  then `up`.
- **A new named volume is filled from the image**, which is how `data/vrm/baseline.vrm` reaches
  compose installs (`COPY` in the Containerfile, re-included in `.containerignore`). A bind-mounted
  `data/` hides it.
- The old compose healthcheck curled `/api/status`, which is behind login (401) and so was always
  unhealthy; both now probe the public `/api/health`. Podman drops the Containerfile `HEALTHCHECK` without
  `--format docker`, so compose carries its own.
- `llm-models/` and `voices/` are mounted `:z`; only ever point those at dedicated folders.
- **amd64 is pinned in `run-podman.sh`, never in the Containerfile.** The workstation build
  passes `--platform linux/amd64` so a wrongly-cached base can't slip in (a `python:3.12-slim`
  cached as arm64 had Iguana running under emulation until 2026-10-27). Compose installs build
  for their own machine — Apple Silicon included — so the Containerfile must stay unpinned.

## Container topology

| Container  | Network         | Address                         | Role         |
|------------|-----------------|---------------------------------|--------------|
| companion  | ai-net | `127.0.0.1:7861`                | this app      |
| koboldcpp  | ai-net | `koboldcpp:5001`                | LLM text gen |
| comfyui    | host            | `host.containers.internal:8188` | image gen    |
| orpheus-tts | ai-net         | `orpheus-tts:5005`              | voice (on demand) |

## File structure

```
main.py                 # Pantheon: every handler + JSON file store (_save/_load/_list, relationship helpers);
                        #   its FastAPI route table is not served (service.py is) — the BFFs call the handlers
service.py              # the server: pytincture app + /pmedia + /desktop + /api/health; localhost -> 127.0.0.1
run.py                  # dev launcher (service:app, autoreload, :7861)
models/schema.py        # Pydantic models (below)
services/
  kobold.py             # KoboldCPP client. chat_generate_stream() = OpenAI SSE + auto-continue (2 rounds)
  comfyui.py            # ComfyUI client: generate() txt2img + generate_img2img() + upload_image()
  card_io.py            # V2/V3 PNG (tEXt chara/ccv3) + JSON import/export
  prompt_builder.py     # macros, system prompt, lorebook scan, injects relationship_note + date/time
  memory.py             # COMPANION memory: session summary + cross-session store + recall + auto-facts
  assistant_memory.py   # ASSISTANT memory: directives + facts + task notes. Separate module, not a branch
  embeddings.py         # BGE-small ONNX/CPU sentence embeddings (baked, 33MB, zero VRAM)
  expressions.py        # emotion classify + valence map + sprite gen (img2img) + rembg cutout
  relationship.py       # multi-axis relationship + stage + intimacy ladder + descriptor
  tts.py                # local TTS (ONNX/CPU) — Kokoro + Piper (`piper:<name>`) + Orpheus (`orpheus:<name>`), text -> WAV, strips *action* narration
  orpheus.py            # Orpheus TTS client: load/release on the orpheus-tts service, prompt, SNAC ONNX decode (CPU)
  stt.py                # local faster-whisper STT (base.en, CPU int8) — audio bytes -> text; WAV decoded
                        #   in-house (PyAV 19 breaks faster-whisper 1.2's decoder — see _wav_samples)
  auth.py               # scrypt password hash + HMAC-signed session token (stdlib only)
appcode/                # pytincture modules_path: pantheon.py (the page), ui/ (screens), bff/ (BFF classes),
                        #   static/pantheon.css (served at /pstatic), wapyt-99.99.99-*.whl (git-ignored)
pantheon_web/           # server-side plain modules (never shipped to the browser): auth (login hook) ·
                        #   login_page · media (/pmedia: images, export, import, VRM list/file/upload, tts, stt,
                        #   avatar.vrm, help.html) · desktop (/desktop + /pdesk/*) · jobs (background sprite
                        #   generation) · present (shared formatting) · assets/help.html (the in-app guide)
static/js/voice.js      # voice island: read-aloud + lip-sync, PTT, hands-free/VAD/wake word, barge-in; loads vtuber.mjs
static/js/vtuber.mjs    # 3D avatar renderer (three.js + three-vrm, vendored in static/vendor/), avatar:* events
static/js/desktop.js · static/css/desktop.css   # the desktop avatar's page, on voice.js
static/avatar-test.html # avatar tuning harness (reads /pmedia/vrm; sign in first)
desktop/                # the native shell: transparent GTK4 + WebKitGTK window on /desktop, tray (desktop/README.md)
tests/smoke_web.py      # Playwright smoke test (WRITES: run on a scratch library); fake mic;
                        #   PANTHEON_VOICE=1 (+ PANTHEON_FAKE_AUDIO=speech.wav) adds TTS/STT steps — container only
tests/test_messages.py  # message edit/delete/truncate vs. the summary and sweep marks
tests/test_chat_stream.py # chat stream keep-alive: a slow finish still reaches done and saves
scripts/web-dev.sh      # run Pantheon host-side against a scratch library
scripts/vrm/            # Blender + VRM Add-on: custom expressions, texture repaint (built Butler2/3) — README.md
data/auth/  secret · users/<uid>.json                    # accounts + signing secret (global)
data/u/<uid>/  characters/ personas/ sessions/ relationships/ presets/ settings/ memories/ assistant/  # per-user isolated library
models/rembg/           # baked: isnet-anime.onnx + u2net.onnx (background removal)
models/kokoro/          # baked: kokoro-v1.0.onnx + voices-v1.0.bin (TTS)
models/piper/           # MOUNTED ro from ../jarvis — one .onnx + .onnx.json per Piper voice
models/whisper/         # baked: faster-whisper base.en (CTranslate2) — STT
models/embed/           # baked: bge-small-en-v1.5 int8 ONNX + tokenizer — semantic recall
RELATIONSHIP.md         # relationship system reference — axes, ladder, boundary injection
RELATIONSHIP_PLAN.md    # relationship system design + decision log (phases 1-3 done)
MEMORY.md               # memory reference — BOTH pipelines, the layers, recall gate, auto-facts, the eval
MEMORY_PLAN.md          # memory design + decision log (phases 1-6 done) + open questions
PROMPT_BUDGET.md        # shared: what's in a prompt, overflow behavior, eviction plan (NOT built)
SKILLS_PLAN.md          # assistant skills via tool calling: System Info + Media via Orpheus (Phase 0 done)
eval/                   # NOT baked into the image — `podman cp eval companion:/app/eval` to run
  assistant_memory/     # 5 labelled fixtures + runner + RESULTS.md (see MEMORY.md § The eval)
  assistant_replies/    # probe set for assistant reply style (narration, closing offers)
```

## Auth / multi-user (privacy)

Login-gated, role-based, per-user isolated libraries.
- **Accounts** (`models.schema.User`): `data/auth/users/<uid>.json`; password = **scrypt** hash + per-user
  salt (`services/auth.py`, stdlib — no bcrypt dep). Roles: **`admin`** | **`user`**.
- **Session**: pytincture's signed session cookie (key `data/auth/pytincture.key`), claims
  `user_id/username/role` (`AUTH_SESSION_CLAIM_KEYS`). Sign-in is pytincture's form (restyled by
  `pantheon_web/login_page.py`) checking Pantheon's accounts through `AUTH_USER_AUTHENTICATOR =
  pantheon_web.auth.authenticate`. **`ctx_for(claims)` re-reads the account on every call**, so a
  deleted or demoted user loses access at once. BFF calls carry pytincture's CSRF check; the plain
  `/pmedia` and `/pdesk` POSTs mirror it (`_require_csrf`, `X-CSRF-Token` from the CSRF cookie).
  (`services/auth.py`'s HMAC token, `data/auth/secret` and main's `current_user` belonged to the
  classic page's cookie and are unused by the page now.)
- **Isolation**: every handler resolves a per-request **`Ctx`** → `data/u/<uid>/{characters,personas,
  sessions,relationships,presets,settings}`. Users never see each other's data. (`_load/_save/_list` take
  the Ctx's folder; relationship/settings/persona helpers take `cur`.)
- **Admin-only**: user CRUD + password reset (Settings → Users; `bff/account.py`); guards against deleting yourself or the last admin
  (deleting a user removes their whole `data/u/<uid>` library). Normal users manage only their own libraries.
- **Seed + migrate (first run)**: `_seed_and_migrate()` creates **ADMIN** and moves any legacy flat
  `data/<subdir>/*` into the admin's library so nothing is orphaned. The password comes from
  `COMPANION_ADMIN_PASSWORD` if set, else `_seed_password()` mints a random one and
  `_announce_seed_password()` prints a startup banner + writes `data/auth/INITIAL_PASSWORD` (0600) —
  the container runs detached, so stdout alone isn't findable enough. **Never reintroduce a constant
  here:** this repo is shared, and a literal would be the known default admin credential of every
  install (and of every LAN exposure once Phase 8 puts TLS in front). Seeding only runs when no users exist.
- **Frontend**: signed out, every page route goes to `/pantheon/login` (and `/desktop` returns to
  itself after sign-in via `session["return_to"]`); a BFF answer with `signed_out` sends the page
  there too (`ui.common.failed`). The rail foot shows the account and Sign out.
- **Volume**: the whole `data/` is now a single mount (bind mount in `run-podman.sh`, the `pantheon-data` volume in `compose.yaml`) so `data/u`,
  `data/auth`, and per-user subdirs all persist — no more per-subdir mount list to keep in sync.

## Data model (models/schema.py)

- **CharacterCard** — V2/V3 spec fields (`description`, `personality`, `scenario`, `first_mes`,
  `mes_example`, `alternate_greetings`, `system_prompt`, `post_history_instructions`,
  `character_book`, `tags`, `nickname`, …). Local: `image` (base64 portrait), `appearance` (sprite
  prompt), `expressions` (emotion→base64), **`power_dynamic`** (−100…100), **`social_acceptance`** (0…100),
  **`tts_voice`** (per-character voice — Kokoro id or `piper:<name>`), **`tts_speed`** (0.5–1.5 speaking
  pace, 1.0 = the voice's own tempo; Kokoro multiplier / inverse Piper `length_scale`),
  **`portrait_source`** (`""` auto = portrait else neutral sprite | `image` | `sprite` + `portrait_sprite`
  | `vrm` = `vrm_still`, a PNG of the model rendered in the browser by `vtuber.mjs` `captureStill()`,
  taken from `vrm_still_of`) — the Studio's "Profile picture"; `present.portrait_b64()` is the one
  place that picks, so `/pmedia/card/{id}.png` and `has_portrait` follow it everywhere. A missing
  source falls back to auto. The Studio retakes the still on save when the assigned model changed.
  **`chat_mode`** (`roleplay`|`assistant`) — picks the built-in
  system prompt (`prompt_builder.DEFAULT_ROLEPLAY` scene/world vs `DEFAULT_ASSISTANT` personality-only, no
  invented setting); a custom `system_prompt` overrides both. Editor: "Chat mode" select.
  **`pronouns`** (`""`|`she/her`|`he/him`|`they/them`|`it/its`|custom `subject/object/possessive`) — used
  where the app writes *about* the character: the relationship descriptor and boundary note. Parsed by
  `relationship.Pronouns`, which also carries verb agreement (`they are`/`they turn`, not `they is`).
  Blank = she/her, which is what every card written before the field existed was silently getting.
  Assistant cards also get a trailing **`ASSISTANT_FORMAT_NOTE`** after the history — the system
  block loses to the transcript, and a session whose history already contains narrated replies keeps
  producing them however firmly the system prompt forbids it (measured on a live 139-message session:
  3/6 replies narrated without the note, 0/6 with). It names the transcript's style as a habit to
  ignore, which is the part that works. `memory.summarize()` is mode-aware for the same reason — the
  roleplay prompt's "third person, present tense" prose was being injected as "Story so far:" and read
  as a worked example of narrating; assistants get notes, labelled "Notes from earlier in this
  conversation". Roleplay summaries are past-tense notes too (2026-10-28): the prose version read as
  a recap to imitate. They end in a `Where things stand:` line, and the card's scenario is labelled
  "Opening premise", so long chats stop getting pulled back to the first scene.
  **Everything after the history is one system message ending in `focus_note()`**, which quotes the
  latest user message back. Four separate trailing blocks used to sit between the user's line and the
  reply, and the model answered them instead (measured: unprompted date/time in 7/15 assistant
  replies before, 1/15 after). The exact time trails only when asked; the date sits in the system block.
  `kobold.chat_generate_stream` continues a cut-off reply by **assistant prefill**, never a fake
  "Continue." user turn (that produced a recap glued onto the reply, 3/5 cut-off replies).
  The same note also bans closing offers of help, initiative questions, and any mention of a body,
  senses, hardware or location. `prompt_builder.strip_initiative_question()` then removes a
  trailing courtesy question in code — measured, the prompt rule alone moved it 9/15 -> 8/15 and
  the strip took it to 1/15 (`eval/assistant_replies/`). Clarification questions survive; only
  "anything else?" forms are removed.
  **`DEFAULT_ASSISTANT` composes for the ear** — spoken prose, no markdown/tables/lists, numbers as
  words — because assistant replies are spoken. A custom `system_prompt` drops that too; say it there.
- **Persona** — the `{{user}}` card (`name`, `title`/role, `description`, `background`, `image`, `is_default`);
  role+background injected as "About {{user}}". Sidebar has a personas list + editor; one active (default) at a time.
- **ChatMessage** — `role`, `content`, `expression`, `swipes[]`, `swipe_index`, `swipe_expressions[]`.
- **MoodState** — running `valence` (−1…1) + `mood` label (session-level, short-term).
- **Relationship** — persistent per (character, persona): axes `affection/trust/attraction/tension`,
  `emotional_intimacy`, `physical_willingness`, optional `power_dynamic`/`social_acceptance` overrides,
  `milestones`.
- **AssistantMemory** — the assistant-mode store, `data/u/<uid>/assistant/{cid}__{pid}.json`.
  `directives[]` (standing instructions, OBEYED), `facts[]` (things known), `notes[]` (terse task
  state, rewritten whole each fold), `swept_sessions`. **No summary, no relationship, no scene.**
  Served by `services/assistant_memory.py`, a separate module and not a branch in `memory.py` — every
  leak found so far was roleplay framing reaching an assistant through a shared path that forgot to
  branch, and a branch is a place the next feature forgets. Injection puts directives FIRST and never
  truncates them. **→ `MEMORY.md` for the mechanics, `MEMORY_PLAN.md` Phase 6 for why.**
- **CharacterMemory** — persistent per (character, persona), like Relationship: `facts[]`
  (`MemoryFact{text, origin: user|auto}`), `summary` (cumulative across every chat in the pairing),
  `swept_sessions` (session_id -> message index already folded, so sweeps are idempotent). Injected as
  "What {{char}} remembers about {{user}}"; **not** used in scenario sessions. **→ `MEMORY.md`.**
- **ChatSession** — `messages`, `memory` (MemoryState), `mood` (MoodState), `generation`
  (GenerationConfig, `max_tokens=220`), `pending_milestone`, optional session-scoped `relationship`
  (scenario mode), `character_id`, `persona_id`.
  (Lorebook entries carry `min_stage` for stage-gating.)

## What the server answers

```
/pantheon/…                                the page (pytincture): login, the app, its BFF calls
/                                          -> /pantheon (pytincture's own redirect)
GET  /api/health                           {"ok": true} — public; stack + compose healthchecks
/pmedia/…                                  pantheon_web/media.py: card/expression/persona PNGs, card export,
                                           import, VRM list/file/upload/avatar.vrm, tts, tts-sample.wav, stt, help.html
/desktop  ·  /pdesk/{state,open,chat,sweep,settings}    pantheon_web/desktop.py (the desktop avatar)
/static/… · /pstatic/…                     islands + vendored three.js · the page's stylesheet
```

Everything but `/api/health`, `/static` and the login page needs the session; POSTs need the CSRF
header. Any other Host than `127.0.0.1` gets a 400 from pytincture (`localhost` is redirected first).

## Handlers (main.py — not served; the BFFs call them)

The paths are the handlers' historical routes, kept because they name each handler and the unit
tests use them; nothing listens on them. User management requires admin (403).
```
POST /api/me/password                      change own password (needs current)
GET/POST /api/users ; DELETE /api/users/{id} ; POST /api/users/{id}/password   ADMIN only
GET  /api/status                           KoboldCPP {online, version, model}
POST /api/tts ; GET /api/tts/voices        local TTS (text→WAV) / 54 Kokoro voices + any `piper:<name>`
POST /api/stt                              local faster-whisper STT (multipart audio → {text})
GET/PUT /api/settings                      per-user app settings (hands_free, wake_word, VAD tuning, disable_thinking, memory + recall)
# cards
GET/POST /api/cards                        list (trimmed, +has_expressions) / create
POST /api/cards/import                     import PNG/JSON card (multipart)
GET/PUT/DELETE /api/cards/{id}             full card ops
GET  /api/cards/{id}/export?version=2|3    download PNG card
# expressions / ComfyUI
GET  /api/comfyui/status | /checkpoints
GET  /api/expressions/emotions             {core:[8], full:[28]}
POST /api/cards/{id}/expressions           generate set — REPLACES all sprites (checkpoint, style, preset core|full, transparent, from_upload, denoise)
GET  /api/cards/{id}/expressions           labels the card has sprites for
GET/POST/DELETE /api/cards/{id}/expression/{emotion}   serve PNG / regen one / delete one
# personas
GET/POST /api/personas ; PUT/DELETE /api/personas/{id}
GET/POST /api/presets ; GET/PUT/DELETE /api/presets/{id}   relationship presets (scenario seeds)
# relationship (per character+persona)
GET  /api/relationship?character_id=&persona_id=     summary (axes, stage, intimacy) + effective power/social + context_overridden
PUT  /api/relationship                     override context {power_dynamic, social_acceptance} (null clears -> card default)
# sessions + chat
GET/PUT /api/memories?character_id=&persona_id=   cross-session memory (facts[] + history); facts round-trip by id
GET/PUT /api/assistant-memory?character_id=&persona_id=   assistant store (directives[] + facts[] + notes[]); round-trip by id
POST /api/sessions/{id}/sweep              idle trigger: fold a finished chat into long-term memory (idempotent)
GET  /api/sessions ; POST /api/sessions    list / create ({character_id, persona_id?, greeting_index?, preset_id?, mode?})
GET/DELETE /api/sessions/{id} ; DELETE /api/sessions/{id}/messages
PUT  /api/sessions/{id}/swipe              select swipe + reclassify emotion -> {expression}
PUT    /api/sessions/{id}/messages/{mid}            edit one message (a reply: the swipe shown)
DELETE /api/sessions/{id}/messages/{mid}            delete one (not the greeting)
POST   /api/sessions/{id}/messages/{mid}/truncate   drop everything after it ("Save & resend")
       — all three keep memory.last_summarized_index and the pairing's swept_sessions mark
         consistent; a truncate below the summary resets it, like clearing (tests/test_messages.py)
POST /api/chat                             SSE stream; done carries {expression, mood, valence, relationship, milestones}
```

## How it works

- **Chat:** `build_messages()` assembles system (default/card system prompt + persona + **relationship
  descriptor** + lorebook + memory) → history → `post_history_instructions` → date/time (assistant
  mode only) → **boundary note** (always last); streams via
  `kobold.chat_generate_stream()`. System prompt enforces a **single concrete beat**, no repetition, no
  writing for {{user}}; a stop-sequence on the user's turn is added. After the reply: classify emotion,
  update mood + relationship (skipped on re-rolls), save.
- **Swipes:** alternates per message; each swipe's emotion cached in `swipe_expressions`. `▶` past the end regenerates.
- **Cards:** `card_io.py` reads/writes the PNG `chara`(V2)/`ccv3`(V3) tEXt chunk.
- **Expressions:** `expressions.py` — `classify()` picks a label (from the card's own set) via KoboldCPP;
  `generate_expressions()` makes ONE base portrait then derives each emotion via **img2img** (denoise
  adjustable via the panel slider, default 0.55, persisted `card.expr_denoise`; lower = closer to the
  base/likeness) so all sprites are the same character; `rembg` cuts a transparent background. The base is either
  a fresh **txt2img** from the appearance prompt (default) or the card's **uploaded portrait**
  (`from_upload` → `_uploaded_base`, cover-cropped to portrait dims) — chosen via the "Use uploaded
  portrait as base" checkbox, persisted on `card.expr_from_upload` so single-sprite regen matches the
  batch. (img2img still restyles the upload toward the checkpoint, so likeness drifts somewhat; the
  likeness-preserving upgrade is LivePortrait — see `vtuber-upgrade-plan.md`/models present.)
  Sets: **Core 8** / **Full 28** (SillyTavern-compatible). Portrait reflects the running **mood**.
  **FLUX (2026-10-27):** `flux1-schnell-Q4_K_S.gguf` (models/unet) is listed by
  `comfy.list_checkpoints()` and loads split (`_flux_loaders`: UnetLoaderGGUF + DualCLIPLoader
  clip_l/t5xxl + VAELoader ae — an fp8 all-in-one is ~12 GB, the whole card); img2img has its own
  FLUX graph (`_flux_img2img`) and FLUX prompts drop the `(x:1.3)` weighting T5 can't read. The
  Studio defaults the checkpoint to `card.expr_checkpoint` (what the set was made with, so a
  single regen matches), else FLUX. **Emotions use Flux Kontext** (`generate_kontext`,
  `card.expr_kontext`, Studio checkbox, on when `flux1-kontext-dev-Q4_K_M.gguf` is installed):
  an instruction edit of the base ("change the facial expression to …, keep everything else"),
  resized back to the base's size. Measured on the same base: img2img at 0.55 left five emotions
  near-identical, at 0.7 angry/surprised were both grins; Kontext gave eight distinct emotions with
  face, clothes and background unchanged. Cost: ~50 s per sprite at 12 steps (20 looked no better
  at ~85 s) — Core 8 in 402 s, Full 28 ≈ 25 min — vs ~15 s for schnell img2img. Runs beside
  KoboldCPP (9.5 GB loaded) without OOM.
- **Mood/valence:** per reply, `EMOTION_VALENCE` → running `session.mood` via EMA; **neutral holds**.
  Header mood meter shows it.
- **Relationship (`relationship.py`):** per (character, persona), stored `data/relationships/{cid}__{pid}.json`.
  Emotions move axes each reply (`RATE`); grows/drops, **no idle decay**. Stage derived (Stranger→Intimate,
  romance gated on attraction; "(Strained)" at high tension). **Intimacy/consent:** `emotional_intimacy`
  (capped by trust) + `physical_willingness` (gated by trust×attraction, `WRATE`, suppressed by tension) →
  a **7-rung clinical ladder** (`PHYSICAL_TIERS`, cumulative, no cap). **Context** (`power_dynamic`,
  `social_acceptance`; card default + per-relationship override) **slows pace & adds caution — never
  compliance**; agency-preserving, consent enthusiastic/revocable. `descriptor()` injects all this into the character's prompt, in their own `pronouns`.
  **Assistant-mode cards are outside the whole system** — `chat()` passes empty
  `rel_note`/`boundary_note` (scene-and-feelings text under a "there is no scene" system prompt is
  what had assistants narrating thoughts, gestures and self-description), skips `update()`/
  `escalate()`/milestones entirely, and omits `relationship` from the done event; the frontend hides
  the rel-strip for them. Their stored `{cid}__{pid}.json` is left intact, inert until the card is
  switched back to roleplay.
  **Boundary phrasing is load-bearing** — `_physical_clause()`/`boundary_note()` name the concrete acts
  (`_TIER_ACTS`) and the FIRST refused rung, never just the abstract label. The old wording (`willing up
  to "none" … engages at or below this`) was a null instruction at tier 0 and small models complied with
  anything. `boundary_note()` is also re-injected as a **trailing system message after the history**
  (`build_messages(boundary_note=…)`, placed after `post_history_instructions` so a card's UJB can't bury
  it), and `kobold.chat_generate_stream` re-appends that trailing system turn on each auto-continue round
  so `"Continue."` can't demote it. This is prompt steering, not a hard gate — an anti-refusal fine-tune
  (the default `…-Uncensored-…-Aggressive` GGUF) still complies sometimes.
  **Escalation:** once intimacy has begun and she's positively engaged, `classify_activity()` reads the act
  depicted and `escalate()` nudges willingness toward the next rung (`ESC_RATE`; social acceptance modulates
  pace, tension blocks it) — so doing acts gradually opens her further.
  **Milestones** (`MILESTONES`/`newly_reached`): crossing a stage/tier fires a one-time toast + a one-time
  beat injected next turn (`session.pending_milestone`), recorded so it never repeats. Lorebook entries can
  be **stage-gated** (`LorebookEntry.min_stage`, filtered by `stage_rank`); context is **overridable per
  relationship** (⚙ on rel-strip → `PUT /api/relationship`).
- **Memory: `MEMORY.md` is the reference and `MEMORY_PLAN.md` the decision log — edit those, not this
  bullet, when memory behaviour changes.** In outline: **two pipelines**, picked once per turn by
  `chat_mode`, sharing no code path. *Companion* — per-session pinned facts + a rolling summary (folds
  once ~24 turns accumulate, tunable in ⚙ Settings), cross-session `CharacterMemory` per (character,
  persona) written by two triggers (the chat flow folds forward what the summary just absorbed;
  `POST /sessions/{id}/sweep` on a 5-min idle timer folds a finished chat), plus margin-gated semantic
  recall over an int8 BGE index. *Assistant* — `AssistantMemory`: directives, facts, task notes; no
  summary, no recall. Both are budget-capped and drop oldest rather than truncating, both evict
  auto-written entries before curated ones, and both are editable in the 📌 Memory modal, which
  renders the assistant's different shape rather than a relabelled one.
- **Storage:** plain JSON under `data/`, UUID ids.

## Frontend map (appcode/ + the islands)

Python in the browser (Pyodide + wapyt). **Gotchas that cost time:** BFF modules are re-executed per
call (no state; import plain modules inside methods; never import one BFF from another — shared
helpers go in `pantheon_web/present.py`); async exports are awaited directly, sync ones get `_async`;
never destroy a proxy inside its own callback; `overflow: clip`, not `hidden`, on fixed-height boxes
(`focus()` scrolls `hidden` ones).

| file | what |
|---|---|
| `pantheon.py` | the shell: 292 px rail (tile nav, Recent chats, Your characters, persona menu, theme swatches, sign out), top bar (search, KoboldCPP status, Create), hash routing, mobile drawer, the per-chat idle-sweep timers; loads `pantheon.css` and `voice.js` |
| `ui/discover.py` · `ui/profile.py` | gallery home (search, tags, sort) · character profile modal (greetings, start / continue / scenario wizard) |
| `ui/chat.py` | the chat: bubbles, streaming + Stop, swipes, ⋯ per message (another take, edit / save & resend, read aloud, copy, delete), starter chips, mood + relationship in the header, the **stage** (`.pt-stage`: its own right-hand column, `--pt-stage-w` 36% of the chat area, min 260 px, gone below 900 px; holds the sprite or the 3D avatar slot, only for cards with either — `vtuber.mjs` `frameBust()` fits the camera to its shape, gaze aimed at eye height), speaker / hands-free / push-to-talk; drives `voice.js` |
| `ui/chats.py` | all chats |
| `ui/studio.py` | the card editor: every V2/V3 field, greetings, starters, lorebook, power/social, voice + test, expression generation + gallery + preview, VRM import/assign/audit, import / export / duplicate, local drafts |
| `ui/people.py` | personas + presets |
| `ui/settings.py` | settings (model, memory, voice + microphone picker), password, users (admin), the guide |
| `ui/memory.py` | the chat's Memory and Relationship-context modals |
| `ui/common.py` | spawn, Listeners, Modal, toast/failed, csrf_token, avatar/portrait helpers |
| `bff/*.py` | `shell`, `library`, `chat` (incl. the `@bff_stream` relay of `main.chat`), `studio`, `people`, `account`, `memory` |
| `static/js/voice.js` | `window.PantheonVoice` + `pvoice:*` events (PYTINCTURE_PLAN.md, Phase 6) |
| `static/js/vtuber.mjs` | `avatar:card/speak/expr/mood` in, `avatar:active/error` out |

## Feature set (all built + verified)

✅ Card V2/V3 import/export · ✅ streaming chat (single-beat, non-repetitive) · ✅ swipes + per-swipe
emotions · ✅ rolling memory · ✅ **cross-session long-term memory** (per char+persona, idle sweep,
curated + revertible facts) · ✅ persona · ✅ character editor (fields, portrait, **lorebook**, Duplicate,
power/social) · ✅ ComfyUI expression sprites (img2img same-character, transparent via rembg, Core-8/Full-28,
**gallery** w/ per-sprite regen/delete) · ✅ live portrait swap · ✅ running **mood/valence** · ✅ empty-greeting
fallback · ✅ delete chats/characters · ✅ **relationship system** (multi-axis, per char+persona, behavior
injection, rel-strip) · ✅ **intimacy/consent** (split willingness, 7-rung ladder, power/social context,
agency-preserving) · ✅ **milestones** (toast + injected beat) · ✅ **stage-gated lorebook** · ✅
per-relationship **context override** (⚙) · ✅ **persona editor modal** · ✅ **semantic recall**
(int8 BGE, CPU) · ✅ local **TTS + STT** (Kokoro/Piper, faster-whisper) · ✅ **assistant memory**
(directives/facts/notes, separate pipeline).

## Conventions & gotchas

- Rebuild after code/template/static edits (`./run-podman.sh --rebuild`); `podman exec -i` for heredocs.
- **The chat stream sends `{"ping": true}` every `STREAM_KEEPALIVE_S` (10 s) of silence** (`main.chat`
  `gen()` wraps `_turn()`). After the last token a turn still makes model calls — expression,
  relationship activity, memory summary + long-term fold — before it saves and sends `done`.
  pytincture closes a BFF stream after `BFF_STREAM_IDLE_TIMEOUT_SECONDS` (30 s) without an item,
  and that cancelled the turn before `_save`: the reply streamed in full, then vanished on the
  reload with no error (2026-10-29: 5 of 24 replies in a long chat). Every client ignores unknown
  events. Stop / a closed tab still cancels the turn unsaved. Keep anything slow inside `_turn()`.
- **TTS backends** (`services/tts.py`): voice ids prefixed **`piper:`** route to Piper (one ONNX per voice,
  loaded from `PIPER_DIR`, cached; its espeak-ng data ships in the `piper-tts` wheel), everything else to
  Kokoro. Piper voices are **mounted, not baked** (`../jarvis` → `/app/models/piper:ro,z`) — dropping a new
  `<name>.onnx` + `<name>.onnx.json` pair in there needs only a container restart, no rebuild. Piper honours
  each voice's own `inference` block; `speed` scales its `length_scale` inversely. Note Piper voices carry
  their own sample rate (jarvis = 22.05 kHz vs Kokoro's 24 kHz).
- **Orpheus voices** (`orpheus:tara` … `services/orpheus.py`, 2026-10-28): the expressive voice, a 3B model
  in the stack's `orpheus-tts` container (`ai-stack/orpheus-tts/CLAUDE.md`). It costs ~3 GB of VRAM, so it
  is **loaded only while voice is on**: `voice.js syncEngine()` (run on every repaint) posts
  `/pmedia/tts/engine {on}` when the speaker or hands-free is on for a card with an `orpheus:` voice, and
  `{on:false}` 20 s after that stops (a quick toggle or chat switch doesn't reload 3 GB). The service also
  unloads itself after 10 min idle (closed tab), and `koboldcpp/start.sh` unloads it before a model swap.
  A load that fails (out of VRAM) is seen in ~5 s from the admin API's idle → down → idle and toasts once;
  every line then falls back to Kokoro (`fallback_voice`: af_heart / am_michael, tags stripped) — for 60 s
  without retrying, or until voice is toggled. Model output is SNAC codes, decoded on the CPU by the
  baked ONNX decoder (`SNAC_DECODER`, 52 MB) — no torch, no VRAM. Measured: ~1.5× real time per
  sentence alone, 0.65–1.0× while the chat model is still writing; load 3 s cached / 15 s cold. The
  voices are listed only when the service answers.
- **Orpheus is streamed** (2026-10-28): voice.js fetches `/pmedia/tts/stream` for `orpheus:` voices —
  raw PCM s16le 24 kHz (`X-Sample-Rate`) as it is generated (`orpheus.stream_pcm`: KoboldCPP
  `/api/extra/generate/stream`, SNAC decoded in sliding 4-frame windows like Canopy's decoder, but the
  first frame and the last two are kept and each join is crossfaded over 256 samples — extra
  discontinuity at joins ≤0.007 vs the whole-sentence decode, correlation 0.995+). The browser reads
  each stream into memory as it arrives (a prefetched sentence keeps generating) and schedules the
  chunks back to back. **Start rule (`readyToPlay`)**: play once the buffer covers the rest of the
  sentence at the measured arrival rate (est. 0.42 s/word) — a fixed 0.3 s buffer stalled up to 48
  times per sentence under GPU contention; this rule had 0 stalls in 6 contended runs. **First
  piece kept short** (`enqueueSpeech`/`clauseCut`): an opening sentence >14 words splits at its first
  clause break after 6+ words, and while streaming a clause break after 8+ words is spoken before the
  full stop. Measured server-side, send → first audio: 3.2–13.4 s before, 1.8–2.3 s after (idle GPU),
  ~1–3 s under contention. A failed Orpheus streams its Kokoro fallback (also 24 kHz). Kokoro/Piper
  voices still use whole WAVs via `/pmedia/tts`.
- **Pauses between sentences** (2026-10-28, measured on a real 195-word Mira reply):
  - **The SNAC decoder is capped at 2 CPU threads** (`SNAC_THREADS`). onnxruntime's default (all 24)
    starved the chat model, which runs 8 of its 41 layers on the CPU: Rocinante took 55–60 s for 250
    tokens beside Orpheus vs 13.6 s alone, and that long overlap was what slowed Orpheus to
    0.65–0.9× and made her pause mid-reply. Capped: 18–19 s, Orpheus 0.85× during the overlap, 1.4×
    after. The decoder itself is faster on 2 threads (32 windows/s) than on all (27/s); real time
    needs ~12/s.
  - **Generation is pipelined** (`queueGeneration`): each Orpheus sentence starts generating as soon
    as its text exists and the previous one has finished generating, never waiting on playback.
    Fetching one sentence ahead of playback left 1.3–1.9 s of silence in a long reply; pipelined, 0.
  - **KoboldCPP's idle timer counts from the last generate request, not from a load**, so a server
    idle past `--adminunloadtimeout` unloaded the model ~1 s after loading it ("Inactive for over
    871 s"); `warm()` then read the idle model as a failed load and the whole reply fell back to
    Kokoro. `warm()` now sends a 1-token request before the load (any generate resets the timer,
    even one that errors with no model) and after it (`_settle_ready`, which also absorbs the ~7 s
    first-request warm-up), and tracks `_inflight` so "loaded, then unloaded" just loads again.
    `voice.js beginReply` re-posts `/tts/engine {on}` so an unload is repaired while the first
    sentence is being written.
  - **A mid-sentence ellipsis no longer ends a piece** (`feedSpeechWhole`): "yes... there's" stays one
    utterance; Orpheus voices the "..." pause itself.
  - **CUDA MPS was tried and dropped**: with the thread cap in place it gave Orpheus 0.93× instead of
    0.85× during the overlap and nothing for the chat model, at the cost of a host daemon at every
    boot — and quitting it crashed both attached containers.
  Inline tags it voices instead of reading: `<laugh> <chuckle> <sigh> <gasp> <cough> <sniffle> <groan> <yawn>`.
  **Cards with an `orpheus:` voice are taught five of them** (`prompt_builder.voice_tag_note`, trailing,
  after `ASSISTANT_FORMAT_NOTE` since that note bans stage directions and the later one wins; roleplay
  cards are told to put them inside quotes, the only part spoken). The note maps moments to tags (funny →
  laugh/chuckle, setback → sigh/groan, surprise → gasp) and leads its example with one — the first,
  permissive wording got 1/12 on inviting messages; this one 9/12 inviting, 2/12 neutral, 0 invented,
  1 double (Mira's live history, 2026-10-28). Tags are hidden in the chat view (`md()`), the chat-list
  preview (`shell._preview`) and the desktop caption, and stripped before Kokoro/Piper speak.
- **What gets spoken** (`feedSpeech` in `static/js/voice.js`): roleplay cards speak only the **quoted dialogue**,
  skipping narration and `*actions*` (a reply with no quotes at all falls back to the whole thing).
  **Assistant cards speak everything**, flushed a sentence at a time as it streams — they have no
  scene, so quotes there mark a term or a citation, and feeding only the quoted spans spoke the
  citations and dropped the answer. `cleanForTTS` then unwraps any markdown that got through —
  table rows become their cells, heading/list markers go while their text stays, fenced code is
  dropped, `~150` becomes "about 150" — since the prompt rule above is steering, not a gate.
  **Loudness:** Piper runs with `normalize_audio=False` + a fixed `PIPER_GAIN` (3.5) instead of its default
  per-chunk normalisation — normalising would pin every *sentence* to full scale, which pumps the level in
  the frontend's chunked streaming queue and skews the amplitude lip-sync. 3.5 was measured to match
  Kokoro (rms 0.104 on both) without clipping; retune it per voice via the `PIPER_GAIN` env var.
- **Disable-thinking** (`AppSettings.disable_thinking`, ⚙ Settings modal): `chat()` appends `/no_think` to
  the **last user turn** (this Qwen3 fine-tune ignores it in the system prompt). The model then emits an
  empty `<think></think>` which `kobold._strip_think` removes — so the full token budget goes to the reply.
  Mutates only the transient generation copy, not stored history.
- **Container TZ + the model knowing the time are two separate things.** The container clock is
  correct (it shares the host kernel); only the *zone* was wrong, so `run-podman.sh` derives `TZ`
  from the host (`compose.yaml` defaults it — compose can't shell out). That alone does NOT make her
  know the time: nothing put the date in the prompt, so asked the time she invented one. Fixed by a
  trailing `Current date and time: …` system message in `build_messages`, **assistant-mode only**
  (roleplay cards must not announce the real date) and placed **after the history** — it changes
  every minute, and in the system block that would invalidate KoboldCPP's cached prefix and
  re-process the whole history every turn. It sits just before `boundary_note`, which stays last.
- **Prompt overflow is silent and truncates from the FRONT** (measured — `PROMPT_BUDGET.md`). Context
  is KoboldCPP's default 8192 (`start.sh` sets no `--contextsize`; the app never reads it). Nothing
  budgets the total prompt, and history is bounded by turn *count*, not tokens — so on overflow the
  system block goes first: persona, relationship descriptor, and memory vanish with no error. The
  `boundary_note` trailing placement makes it the most overflow-resistant part; don't move it.
- CSS (`appcode/static/pantheon.css`): an element with its own `display` beats the `hidden`
  attribute, so each such rule carries a `[hidden] { display: none }` partner; accessible hidden
  radios use `opacity: 0`, not `display: none`.
- `GET /api/cards` strips `expressions` (adds `has_expressions`); fetch the full card before editing.
- **Sprite generation re-reads the card before saving** (`generate_expressions` /
  `regenerate_expression`). It takes minutes; saving the copy loaded at the start silently
  undid any edit made meanwhile, in either page. Only the sprite fields are written onto a fresh read.
- Relationship/mood update on generation only (not re-rolls). Sprites are same-character via img2img from a
  reproducible base (fixed per-card seed) — single-emotion regen stays consistent.
- All characters adult; intimacy code is abstract state (tiers/thresholds) — the local model writes prose.
- The whole `data/` is one bind mount now, so new subdirs persist automatically (no per-subdir mount list).
  Historically `run-podman.sh`/`compose.yaml` mounted each subdir and a missed one wiped state on rebuild.
- `podman exec` heredocs need `-i`. Auth: never log/return password hashes; keep the scrypt params in
  `services/auth.py` in sync with stored hashes (changing them invalidates existing passwords).

## Roadmap / next

- **pytincture page** (`PYTINCTURE_PLAN.md`): ✅ Phases 0–7 — the page replaced the classic one on
  2026-10-26. ◻ **Phase 8: other devices** — TLS in front (pytincture only signs in over plain HTTP on
  loopback), then phones work again.
- ✅ **Relationship Phase 2 (done)** — milestones: `relationship.MILESTONES` + `newly_reached()`; crossing a
  stage/tier fires a one-time toast (chat `done.milestones`) and injects a one-time beat via
  `session.pending_milestone` into the next reply; recorded in `Relationship.milestones`.
- ✅ **Relationship Phase 3 (mostly done)** — **lorebook stage-gating** (`LorebookEntry.min_stage`,
  filtered in `prompt_builder.select_lorebook` via `stage_rank`; editor dropdown) + **per-relationship
  context override UI** (⚙ on rel-strip → `PUT /api/relationship`; `GET` returns effective values).
  ◻ Remaining: **stage-tagged greetings** (needs the alt-greetings editor reworked into a repeater +
  auto-pick a stage-appropriate opening at session creation).
- **Personas as cards + relationship presets + scenario new-chat** (design confirmed):
  - ✅ **Phase A** — persona cards (name/role/description/background/portrait), sidebar list + editor,
    single-active enforced, role+background feed the prompt.
  - ✅ **Phase B** — reusable **preset library** (`RelationshipPreset` in `data/presets/`; `/api/presets`
    CRUD; sidebar list + editor: name, optional character+persona pairing, scenario note, full metric
    bundle power/social + axes + emotional/physical willingness).
  - ✅ **Phase C** — **scenario new-chat wizard**: `POST /api/sessions` accepts `preset_id` + `mode`
    (continue|scenario). Fresh scenario seeds a **session-scoped** `ChatSession.relationship` from the
    preset (+ `scenario_note` into the opening); `chat()` uses it when present, else the persistent file.
    `GET /api/sessions/{id}` returns `relationship_summary` for scenario sessions; the character list opens
    the wizard (`openNewChat`). **A preset's slider values only apply in `scenario` mode** — picking a
    preset now defaults the wizard to `scenario` (`ncPresetChanged`); `continue` intentionally ignores the
    preset numbers and uses the persistent relationship.
- **VTuber upgrade** (voiced, animated avatar) — plan in `vtuber-upgrade-plan.md`.
  ✅ **Phase 0 done** — local Kokoro TTS + amplitude lip-sync + barge-in on existing sprites.
  ✅ **Phase 1 (push-to-talk) done** — local **faster-whisper** STT (`base.en`, CPU int8, in-process,
  baked at `models/whisper/`); `services/stt.py` + `POST /api/stt`; hold-to-talk 🎤 in the composer
  (MediaRecorder → WebM/Opus), transcript auto-sends; **barge-in** cuts TTS (`stopSpeaking`) and aborts
  generation (`abortChat`/AbortController).
  ✅ **Phase 1b (continuous / hands-free) done** — a **separate 🎙 toggle** (distinct from push-to-talk)
  arms an open-mic loop: in-browser **RMS energy VAD** (`hfTick`) segments utterances, transcribes each
  via `/api/stt`. **Configurable wake word** (`applyWakeWord`) — when set, only speech starting with it is
  sent (+strips the prefix); blank = pure open-mic.
  **Ignore-herself = half-duplex:** while she's speaking (`ttsSource`) or within a 400 ms tail, the
  hands-free mic doesn't listen at all (`hfTick` skips the start gate) — no self-hearing. Trade-off: no
  *auto* barge-in in hands-free mode (use push-to-talk 🎤 to interrupt). `echo_suppress`/`ttsLevel` (an
  earlier ducking approach) remain but are **dormant** until interruption returns. Tuning (wake word, mic
  threshold, silence, `echo_suppress`) in the **⚙ voice-settings modal**, persisted to the new
  **`GET/PUT /api/settings`** store (`AppSettings` → `data/settings/app.json`); plus `getUserMedia`
  `echoCancellation`. ◻ **Upgrades:** true wake-word engine (**openWakeWord**,
  ONNX/CPU) + **Silero-VAD** for accuracy; wake-word-gated barge-in.
  ✅ **Phase 2 done** — inline `[expr:x]` beats: streaming parser (`_strip_expr`, sibling of the
  `<think>` stripper), `{"expr":...}` SSE events, beats fire at TTS *playback* not parse, tag wins over
  the classifier. **Verified against the live 9B**: it emits well-formed in-set tags and the events
  reach the wire. Two prompt bugs had to be fixed first — the instruction sat in the system block where
  the trailing boundary note out-weighted it (1/6 replies tagged; 3/6 once moved to a trailing system
  message before `boundary_note`), and the wording `emit a tag [expr:LABEL]` was copied verbatim, so the
  shape is now taught by the example line alone. Tag rate is ~4/6 and varies; the classifier still
  covers untagged replies. Occasional out-of-set labels (`curious`, `playful`) are dropped silently —
  an alias map would recover them. **A label emitted WITHOUT its brackets** never reaches the
  tag parser, so `expressions.strip_bare_labels()` drops one that stands alone on its own line
  before the reply is stored (observed 3x; it was displayed and spoken aloud). Own-line only,
  deliberately: the vocabulary is ordinary English, so a mid-prose rule would eat "Pride comes
  before a fall". That case is handled in the prompt, which now states the brackets are part
  of the tag.
  ✅ **Phase 3a done** — avatar renderer: `static/js/vtuber.mjs` **module island** (three.js +
  `@pixiv/three-vrm`, vendored in `static/vendor/`, no CDN) talking to the page's voice code (then the
  classic `app.js`, now `voice.js`) over
  four CustomEvents (`avatar:card`/`:speak`/`:expr`/`:active`); `CharacterCard.vrm` picks the tier
  (`""` = sprite tier, unchanged) and `GET /pmedia/card/{id}/avatar.vrm` serves models from `data/vrm/`.
  Renderer verified in-browser via `/static/avatar-test.html`; the real `.vrm` branch is untested for
  want of a model. ✅ **3b idle life done** — rest pose out of T (a VRM has a rig but no animation, so nothing lowers
  the arms unless we do), breathing, weight shift, randomised blink, `VRMLookAt` gaze, and the model's
  spring bones now reacting to that motion. Arm signs are measured not derived; `armDeg` stays tunable
  because a VRM 1.0 model may want them flipped. ✅ **3c speech motion done (jaw only)** — Phase 0's RMS
  amplitude drives `aa`+`ih` with asymmetric attack/release and a silence timeout, plus prosody head
  motion off a slow energy envelope; verified against live Kokoro audio. Visemes stay blocked on phoneme
  timings no TTS backend exposes.
  Tuning lives in `static/js/vtuber.mjs` (`idleGain` 2, `armRestDeg` 72, `mouthGain` 6) and is
  adjustable live from `/static/avatar-test.html`. ✅ **3d emotion+gesture done** — the Phase 2
  `avatar:expr` beats now drive the face: Core-8/Full-28 → VRM presets with weight as intensity,
  cross-faded, presets detected per model (VRM 0.x has no `surprised`, so it falls back). **Custom
  expressions win:** a VRM 1.0 custom expression named after an emotion label (`fear`, `pride`,
  `embarrassed`…) is used at full weight instead of the preset mapping (`resolveLabel`, `CUSTOM_ALIAS`;
  shown as `stats.custom`). `data/vrm/Butler2.vrm` = Butler1 + 12 such faces mixed from VRoid's `Fcl_*`
  shape keys in Blender (VRM Add-on). Procedural
  gesture track (nod/tilt/leanIn/recoil/shrug; clips are Track B3) and posture bias from relationship
  stage via a new `avatar:mood` event. **Pose composition rule: `resetPose()` returns every animated
  bone to rest each frame and all channels are purely ADDITIVE** — mixing absolute assignment with `+=`
  is what sent the arms wandering, and shoulders must stay in the rest set.
  **All six channels of "fully animated" are now live.** ✅ **Phase 5 interruption done** — barge-in works in hands-free mode; speaking over her calls
  `stopSpeaking()`+`abortChat()`. `AppSettings.full_duplex` lifts the half-duplex gate (safe on
  headphones; needs an echo canceller on speakers) and `mic_device_id` selects the capture source.
  **While she is audible in full duplex the abort is DEFERRED** (`hfPendingBargeIn`): energy alone
  cannot tell your voice from hers returning through the speakers, and the only check that can
  (`looksSelfHeard`) needs a transcript — so the abort used to fire an STT round-trip before the
  evidence arrived, and any echo cut her off mid-reply. It now runs in `hfOnUtterance()` once the
  transcript clears both the self-hearing check and the wake word, then waits for `state.streaming`
  to fall (`abortChat()` only signals the fetch). Costs one STT round-trip of barge-in latency, and
  only where echo is possible; when she is silent it still cuts instantly.
  **AEC needs BOTH halves** — capturing from `aec_source` while playback goes anywhere but
  `aec_sink` leaves the canceller nothing to subtract. The app cannot see playback routing, so its
  duplex sanity check passes in that state (observed live: Brave recording from `aec_source`, playing
  to the raw analog sink). `scripts/setup-aec.sh --default`, or `pactl move-sink-input <id> aec_sink`
  for one app, is the other half.
  `scripts/setup-aec.sh` loads PipeWire echo-cancel → `aec_source`/`aec_sink`; **`--persist`** installs a
  drop-in in `~/.config/pipewire/pipewire.conf.d/` so it survives reboot (`--unpersist` removes it).
  Because the failure is silent, two guards: Settings warns if full-duplex is on with a non-cancelled
  mic, and at runtime a transcript that is >70% her own recent words is discarded — twice in a row
  disables interruption with a toast.
  **Echo residue also makes Whisper INVENT speech** that shares none of her words, which that guard
  cannot see (2026-10-28: 1.26 s of AEC leftovers became a 74-word "user" message). `stt._accept`
  drops impossible transcripts (>6 words/s of VAD speech) and unconfident segments; clips that
  overlapped her turn (`voice.js herTurn`: streaming, line queued/playing, or <1.5 s after) are sent
  with `strict=1` and held to tighter limits (`STRICT_NO_SPEECH` / `STRICT_LOGPROB`). Measured on
  synthetic speech vs faint echo: real speech kept 108/108 normal, 102/108 strict; echo
  hallucinations through 19/30 unguarded, 11/30 normal, 1/30 strict.
  **Measured, and it explains the failed earlier attempt:** during her speech the raw mic hears her at
  **+26 dB** over the noise floor, the AEC source at **−39 dB** — no level threshold can bridge that, so
  `echo_suppress` was a dead end and is gone from the UI. Over speakers BOTH halves are needed: pick
  `aec_source` *and* play into `aec_sink`, or there is no reference to subtract. Next: ◻
  ✅ **Phase 6 desktop mode** — `desktop/` (transparent GTK4 + WebKitGTK window, tray over D-Bus,
  click-through strip, drag to position; `desktop/README.md`) on **`/desktop`**, which since the
  2026-10-26 switch-over is its own small page (`pantheon_web/desktop.py` + `static/js/desktop.js`
  on `voice.js`), no longer the classic SPA in disguise. · ◻ **Option: realistic non-VRM avatars** (Ready Player Me /
  Avaturn / CC4 — glTF + 52 ARKit blendshapes, which would retire both the 28→5 expression collapse and
  the viseme block; costs a rig-adapter refactor and loses VRM spring bones) · ◻ Track B3 real animation
  clips · ◻ visemes (blocked on phoneme timings unless the ARKit route is taken). **Track B (sourcing a `.vrm`) now blocks 3b** —
  Booth.pm, VRoid Hub, VRoid Studio under Proton, or Blender + VRM Add-on; mind the license flags.
  Local-first voice throughout. Sprites remain the fallback tier for cards with no `.vrm`.
- **Skills for assistant characters** — plan in `SKILLS_PLAN.md`. ✅ **Phase 0 done** — host-derived
  `TZ` + assistant-mode date/time injection (she was inventing dates; the container clock was never
  the problem). ◻ Next: skill scaffolding (`CharacterCard.skills`, registry, two-pass tool loop).
  **Verified live:** KoboldCPP + this Qwen3.5 fine-tune support OpenAI `tools` **including over
  streaming SSE**, with correct arguments, no spurious calls, and no loss of character voice — so
  skills use real tool calling, not `[tag]` scraping. Then ◻ System Info (`get_system_status`:
  uptime/disk/**GPU VRAM**/services) and ◻ **Media** — find and play music/video through **Orpheus**,
  our own media server (on Homelab) + Flutter player, with Pantheon paired as a `controller` device.
  Replaced the Navidrome/DJ plan on 2026-09-15; design in `../orpheus/docs/architecture.md`. NAS: UGREEN
  NASync DH2300 @ `192.168.1.219` (SMB 445), mounted by the Orpheus server, not by Pantheon. Hard part
  is still audio coexistence: music breaks the half-duplex mic gate in both directions.
- **Sprite-pack import** — drop Full-28-named PNGs straight into `card.expressions`.
- ◻ Group chats · ◻ faster inference backend (still KoboldCPP `:5001`).
- Tuning knobs: `relationship.RATE/WRATE/ESC_RATE/_TIER_THRESHOLDS`, `expressions` img2img denoise, `GenerationConfig`.
