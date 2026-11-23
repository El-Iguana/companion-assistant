# Cantinero — Project Context

**Cantinero** — *El Iguana's bartender, at your service.* A self-hosted **voice assistant on
character cards**: TTS (Kokoro / Piper) + STT (faster-whisper), hands-free with wake
word and barge-in, a face from **expression sprites or a VRM avatar**, a **desktop shell**, and
**assistant memory** (standing instructions, facts, task notes). FastAPI + uvicorn in a Podman
container, talking to **KoboldCPP** (text) and **ComfyUI** (sprites) on the shared `ai-net`
network. Everything local; no external services, no database.

**Split from El-Iguana/companion on 2026-11-07.** This repo (`El-Iguana/companion-assistant`) was
cloned with companion's full history and trimmed to the assistant half. The sibling `../companion`
is the **RPG chat** (roleplay, relationships, narrator, story memory; no voice, VRM or desktop).
The two are separate products now: fixes move between them by cherry-pick, if at all. Removed here:
the `chat_mode` switch (every card is an assistant), the relationship system and presets/scenarios,
the narrator / Continue / Write for me, the companion memory pipeline (rolling story summary,
`CharacterMemory`, semantic recall, story memory) and the baked embedding model. JSON copied over
from companion still loads (pydantic ignores the dropped fields; `narrator` messages are never
prompted; a session's old `memory.summary` is never injected and the next fold clears it).

**Names.** The product name the user sees is **Cantinero** (it was Pantheon, and *La Cantina*
before that). Repo, image, container and `stack` name are **`companion-assistant`**, port **7867**.
**Internal identifiers still say pantheon** — `PANTHEON_*` env vars, the `pantheon_web` package,
`appcode/pantheon.py` and its `Pantheon` class / `APP_ENTRYPOINT`, the `/pantheon` URL path,
`/pmedia` `/pdesk` `/pstatic`, `window.PantheonVoice`, the compose `pantheon-koboldcpp` image,
`COMPANION_ADMIN_PASSWORD`, `pantheon.css`, the desktop shell's file names and D-Bus ids.
Renaming them is deliberately deferred; don't half-rename.

### Vocabulary (user-facing copy)

| the UI and help say | the code says |
|---|---|
| **the Staff** — the library of assistant cards; each card is a **bartender**. Creating one is **Hire** | characters, `CharacterCard`, `cards` |
| **on shift** — the bartender you are talking to: the desktop's `desktop_card_id`, else whoever you talked to last, else Mira (`bff/shell.py` `on_shift`) | the active / most recent card |
| **the Menu** — what the staff can do beyond talk; a skill is **on the menu**, enabling one is **adding it to the menu**. The DJ (Media via Orpheus) is on the menu (planned) | skills, `SKILLS_PLAN.md` |
| **standing orders** (help text) | directives (`AssistantMemory.directives`) |

Taglines: *"Pull up a stool. What'll it be?"* where the user is invited in (login, the Staff
page's hero, the rail); *"El Iguana's bartender — at your service."* where the app is described
(frontend.json, the image label, README, the Guide). Account and user management stay plain.
Model-facing prompts and tool definitions don't use this vocabulary.

**The brand image** is El Iguana behind the bar (fez, cigar, old fashioned). The original is the user's
`~/Pictures/eliguana/caninero.png` (1254², not committed). Everything below is cut from it: the head
crop `static/brand/cantinero-logo.png` (256², the rail logo and the sign-in mark), `favicon.png` (64²)
and `apple-touch-icon.png` (180²), the tray icon `desktop/icons/cantinero.png` (128²), and the README
banner `docs/cantinero.jpg` (900², not in the image). `static/` is public, which is why they live there:
the sign-in page shows the logo before anyone is signed in. pytincture owns the page's `<head>`, so the
favicon is added by `appcode/pantheon.py` `_set_favicon()`, and by `login_page._HEAD` on the sign-in page.

**Mira** is the example bartender: `seed/characters/mira.json` (fixed id `mira`, Kokoro
`af_sarah`, wake word "Mira", avatar `baseline.vrm`, the redistributable pixiv sample in
`data/vrm/`). `main._seed_library()` copies her into a library only when it is created (first-run
ADMIN, `create_user`), never again, so deleting her sticks. `_vrm_dir()` copies the bundled
baseline into any library's `data/vrm/` that lacks it (once, `.baseline-seeded` marker). Her
original model (Vita) is "Redistribution_Prohibited" and must never be bundled.

The page is **pytincture + wapyt** (Python in the browser, `appcode/`), laid out after Emberfolk;
voice and the 3D avatar are two JavaScript islands (`static/js/voice.js`, `static/js/vtuber.mjs`).
Plan and decision log in **`PYTINCTURE_PLAN.md`**.

## Running

```bash
./run-podman.sh              # build (first run) + start: http://127.0.0.1:7867/
./run-podman.sh --rebuild    # REBUILD after ANY code/static change (baked into the image)
# logs: podman logs -f companion-assistant ; stop: podman stop companion-assistant
```

**`http://<host>:7867/`, any device on the LAN, plain HTTP.** Cantinero is a local product for
whoever installs it; security against the LAN is not a goal. pytincture's
`allow_development_auth_origin` mode signs users in over HTTP on whatever address the browser used,
but answers 403 to non-loopback clients, so `service.LanAsLoopback` (outermost) presents every
client as 127.0.0.1 — the spike. The container publishes `0.0.0.0:7867`; `localhost` is redirected
to `127.0.0.1` (`service.LocalhostRedirect`) so this computer keeps one sign-in. The microphone
needs a secure context, so voice input works on this computer only, and only at `127.0.0.1`
or `localhost`: opened by its network name or IP (`http://amd-halo.local:7867`), even here,
the page has no `navigator.mediaDevices`. voice.js `micBlocked()` says so in the mic toasts and
Settings' "Find microphones" (it used to be a bare "TypeError").
**HTTPS for other devices' microphones (#7, 2026-11-08):** `run-podman.sh` also serves the app over
HTTPS on **7868** (`PANTHEON_HTTPS_PORT`; 0/unset = off, so compose installs stay plain), one process
with two uvicorn listeners (`service.py __main__`; the HTTPS one has `lifespan="off"`). The
certificate comes from Cantinero's own CA in `data/tls/` (`pantheon_web/tls.py`, `openssl` CLI): a
10-year P-256 CA, **name-constrained** to `localhost`, `.local`, the single-label host name it was
made with and private IPv4 nets, so a copy of its key can't impersonate a real site (a forged
`www.bank.com` cert fails to verify, tested). The server cert (397 days) covers
`PANTHEON_TLS_NAMES` (run-podman.sh: `hostname -s`, `NAME.local`, `hostname -I`), keeping only names the
CA may vouch for (public IPv6 is dropped and logged; one outside name would void the whole cert), and
is remade on start when the names change (DHCP) or <30 days are left; the CA is never remade.
Devices trust the CA once from **`http://NAME.local:7867/ca`** (`pantheon_web/tls_page.py`: public,
plain HTTP, steps for Android/iOS/Windows/Mac/Linux, SHA-256 fingerprint, `/ca.crt`), then use
`https://NAME.local:7868/` (or the IP: Android often can't resolve `.local`). The mic message points
there. Tested in a test build: curl verifies the chain by name and IP with the CA and refuses
without it; a browser signed in over HTTPS by IP, had a secure context and sent a hands-free
utterance. Not yet tried from a real phone. Tests: `tests/test_tls.py`.
`frontend.json` declares `"https_port": 7868, "https_setup": "/ca"`, so the ai-stack dashboard
links Cantinero over HTTPS (at the LAN address even when the dashboard is opened as localhost)
with a "Set up a device for HTTPS" link to `/ca`. Accounts are the app's own (`pantheon_web/auth.py` checks
them with `services.auth`); the session cookie key is `data/auth/pytincture.key`.

The container runs **`service.py`**: `main` (the app itself — accounts, data, clients, every
handler) plus the pytincture app, whose BFF classes call `main`'s handlers directly. `main.app`'s
route table is **no longer served**; it stays as the handlers' contract and for the unit tests.
`/desktop` is the desktop avatar's page (`pantheon_web/desktop.py`, below).

```bash
# host-side dev against a THROWAWAY library — never the real data/:
uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -r requirements.txt pip pytest \
    ~/Development/Pytinc/wa_pytincture_widgetset   # or git+https://github.com/WAwesome-AI/wa_pytincture_widgetset.git@<WAPYT_REF>
PYTHON=$PWD/.venv/bin/python ~/Development/Pytinc/wa_pytincture_widgetset/scripts/dev_wheel.sh appcode
PANTHEON_DATA_DIR=/tmp/cantinero-dev scripts/web-dev.sh start  # :7877 (PANTHEON_DEV_PORT), pidfile;
                                                                # stop|restart|log; refuses the real data/
PANTHEON_URL=http://127.0.0.1:7877 PANTHEON_USER=… PANTHEON_PASS=… \
  CHROME=~/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome \
  uv run --with playwright python tests/smoke_web.py      # every screen, fails on any console error
.venv/bin/python -m pytest -q          # 63 tests; or e.g. tests/test_chat_stream.py tests/test_seed_and_legacy.py
```

| var | default | what |
|---|---|---|
| `PANTHEON_PORT` | 7867 | listen port **and** the port in the address bar — publish it on the same number |
| `PANTHEON_BIND` | 127.0.0.1 | listen address; the image sets 0.0.0.0 and publishes on 127.0.0.1 only |
| `PANTHEON_DATA_DIR` | `./data` | another library (dev/test); read by `main.py` |
| `KOBOLD_URL` / `COMFYUI_URL` | `http://koboldcpp:5001` / `http://comfyui:8188` | backends |

Health: `GET /api/health` (public, `{"ok": true}`) — what `stack status` and the compose
healthchecks probe. `stack companion-assistant` opens `http://localhost:7867/`, which redirects.

Prereqs: `~/Development/ai_ecosystem/koboldcpp/start.sh` (text). For sprites: `~/bin/comfyui.sh` (ComfyUI in the `comfyui`
distrobox on :8188). They share VRAM — use SD 1.5 checkpoints (e.g. `absolutereality_v181`) when both
run. **Data (`data/…`) is a mounted volume**, so editing card/session/memory JSON is live; **code
is baked in — rebuild after edits.** It runs beside the RPG chat (`companion`, :7861); both share
the backends. `podman exec` needs `-i` to receive a stdin heredoc.

### Installing elsewhere: compose (2026-09-26)

**INSTALL.md** is the user guide: Windows/macOS/Linux, Docker or Podman. `run-podman.sh` (what
`stack companion-assistant` runs) is unchanged and still the workstation path. Compose is for everyone else:

| file | what |
|---|---|
| `compose.yaml` | service + container `companion-assistant`, `127.0.0.1:${PANTHEON_PORT:-7867}`, named volume `companion-assistant-data`, backends at `host.docker.internal` |
| `compose.koboldcpp.yaml` | KoboldCPP container `companion-assistant-koboldcpp` (`backends/koboldcpp/`, official CUDA binary) + `KOBOLD_URL=http://koboldcpp:5001` |
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
| companion-assistant | ai-net | `127.0.0.1:7867`     | this app      |
| koboldcpp  | ai-net | `koboldcpp:5001`                | LLM text gen |
| comfyui    | host            | `host.containers.internal:8188` | image gen    |

## File structure

```
main.py                 # every handler + JSON file store (_save/_load/_list, memory helpers, _seed_library);
                        #   its FastAPI route table is not served (service.py is) — the BFFs call the handlers
service.py              # the server: pytincture app + /pmedia + /desktop + /api/health; localhost -> 127.0.0.1
run.py                  # dev launcher (service:app, autoreload, :7867)
models/schema.py        # Pydantic models (below)
services/
  kobold.py             # KoboldCPP client. chat_generate_stream() = OpenAI SSE + auto-continue (2 rounds)
  comfyui.py            # ComfyUI client: generate() txt2img + generate_img2img() + upload_image()
  card_io.py            # V2/V3 PNG (tEXt chara/ccv3) + JSON import/export
  prompt_builder.py     # macros, DEFAULT_ASSISTANT, lorebook scan, memory + date/time, the trailing note
  context.py            # the context window: services/muninn/context.py + this app's CONTEXT_SIZES
  memory.py             # what the assistant fold shares: needs_summary, the transcript, fold_chunks/budget
  assistant_memory.py   # assistant memory: directives + facts + task notes (fold, extract, block)
  expressions.py        # emotion classify + valence map + sprite gen (img2img) + rembg cutout
  tts.py                # local TTS (ONNX/CPU) — Kokoro + Piper (`piper:<name>`), text -> WAV, strips *action* narration
  stt.py                # local faster-whisper STT (base.en, CPU int8) — audio bytes -> text; WAV decoded
                        #   in-house (PyAV 19 breaks faster-whisper 1.2's decoder — see _wav_samples)
  auth.py               # scrypt password hash + HMAC-signed session token (stdlib only)
  muninn/              # COPIED from Muninn (JotunheimPress/muninn, private) at 0b61f3d on 2026-11-23, so a
                        #   public Cantinero builds on its own: context, fold, emotion, text. Not synced
                        #   either way; fix it here. Tests: tests/test_muninn_*.py (copied with it)
appcode/                # pytincture modules_path: pantheon.py (the page), ui/ (screens), bff/ (BFF classes),
                        #   static/pantheon.css (served at /pstatic), wapyt-99.99.99-*.whl (git-ignored)
pantheon_web/           # server-side plain modules (never shipped to the browser): auth (login hook) ·
                        #   tls + tls_page (HTTPS for other devices: local CA, /ca, /ca.crt) ·
                        #   login_page · media (/pmedia: images, export, import, VRM list/file/upload, tts, stt,
                        #   avatar.vrm, help.html) · desktop (/desktop + /pdesk/*) · jobs (background sprite
                        #   generation) · present (shared formatting) · assets/help.html (the in-app guide)
static/js/voice.js      # voice island: read-aloud + lip-sync, PTT, mic capture, barge-in; loads vtuber.mjs.
                        #   Its hands-free decisions run in Python when the page plugs in a brain (below);
                        #   the desktop window (no Pyodide) still uses voice.js's own copy of the rules
appcode/ui/voicecore.py # hands-free rules in plain Python (no browser imports, unit-tested, #5): energy
                        #   segmenter + pre-roll, half duplex / strict / deferred barge-in, wake word +
                        #   continuous conversation, self-hearing check, WAV
appcode/ui/mic.py       # plugs voicecore into voice.js (PantheonVoice.useBrain / brainOps), page-wide
static/js/vtuber.mjs    # 3D avatar renderer (three.js + three-vrm, vendored in static/vendor/), avatar:* events
static/js/desktop.js · static/css/desktop.css   # the desktop avatar's page, on voice.js
static/avatar-test.html # avatar tuning harness (reads /pmedia/vrm; sign in first)
desktop/                # the native shell: transparent GTK4 + WebKitGTK window on /desktop, tray (desktop/README.md)
tests/smoke_web.py      # Playwright smoke test (WRITES: run on a scratch library); fake mic;
                        #   PANTHEON_VOICE=1 (+ PANTHEON_FAKE_AUDIO=speech.wav) adds TTS/STT steps — container only
tests/test_messages.py  # message edit/delete/truncate vs. the summary and sweep marks
tests/test_chat_stream.py # chat stream keep-alive + the after-done memory job vs. concurrent turns/edits
tests/test_context_budget.py # context window: backend probe, Settings clamp, history trim, fold budget
tests/conftest.py       # stubs kobold.context_limit (8192) so no test probes a real backend
scripts/web-dev.sh      # run the app host-side against a scratch library (:7877)
scripts/vrm/            # Blender + VRM Add-on: custom expressions, texture repaint (built Butler2/3) — README.md
data/auth/  secret · users/<uid>.json                    # accounts + signing secret (global)
data/u/<uid>/  characters/ personas/ sessions/ settings/ assistant/  # per-user isolated library
data/vrm/               # avatar models, shared by all users; baseline.vrm is tracked (Mira's)
seed/characters/        # example cards put into every NEW library (mira.json); baked into the image
models/rembg/           # baked: isnet-anime.onnx + u2net.onnx (background removal)
models/kokoro/          # baked: kokoro-v1.0.onnx + voices-v1.0.bin (TTS)
models/piper/           # MOUNTED ro from ../jarvis — one .onnx + .onnx.json per Piper voice
models/whisper/         # baked: faster-whisper base.en (CTranslate2) — STT
MEMORY.md               # memory reference — the assistant pipeline, its fold, injection, the eval
MEMORY_PLAN.md          # memory design + decision log from before the split (companion phases are history)
PROMPT_BUDGET.md        # shared: what's in a prompt, overflow behavior, eviction plan (steps 1+3 built)
SKILLS_PLAN.md          # the Menu (skills) via tool calling: System Info + Media via Orpheus (Phase 0 done)
eval/                   # NOT baked into the image — `podman cp eval companion-assistant:/app/eval` to run
  assistant_memory/     # 5 labelled fixtures + runner + RESULTS.md (see MEMORY.md § The eval)
  assistant_replies/    # probe set for assistant reply style (narration, closing offers)
```

## Auth / multi-user (privacy)

Login-gated, role-based, per-user isolated libraries.
- **Accounts** (`models.schema.User`): `data/auth/users/<uid>.json`; password = **scrypt** hash + per-user
  salt (`services/auth.py`, stdlib — no bcrypt dep). Roles: **`admin`** | **`user`**.
- **Session**: pytincture's signed session cookie (key `data/auth/pytincture.key`), claims
  `user_id/username/role` (`AUTH_SESSION_CLAIM_KEYS`). Sign-in is pytincture's form (restyled by
  `pantheon_web/login_page.py`) checking the app's accounts through `AUTH_USER_AUTHENTICATOR =
  pantheon_web.auth.authenticate`. **`ctx_for(claims)` re-reads the account on every call**, so a
  deleted or demoted user loses access at once. BFF calls carry pytincture's CSRF check; the plain
  `/pmedia` and `/pdesk` POSTs mirror it (`_require_csrf`, `X-CSRF-Token` from the CSRF cookie).
  (`services/auth.py`'s HMAC token, `data/auth/secret` and main's `current_user` belonged to the
  classic page's cookie and are unused by the page now.)
- **Isolation**: every handler resolves a per-request **`Ctx`** → `data/u/<uid>/{characters,personas,
  sessions,settings,assistant}`. Users never see each other's data. (`_load/_save/_list` take
  the Ctx's folder; settings/memory/persona helpers take `cur`.)
- **Admin-only**: user CRUD + password reset (Settings → Users; `bff/account.py`); guards against deleting yourself or the last admin
  (deleting a user removes their whole `data/u/<uid>` library). Normal users manage only their own libraries.
- **Seed + migrate (first run)**: `_seed_and_migrate()` creates **ADMIN** (with Mira, `_seed_library`) and moves any legacy flat
  `data/<subdir>/*` into the admin's library so nothing is orphaned. The password comes from
  `COMPANION_ADMIN_PASSWORD` if set, else `_seed_password()` mints a random one and
  `_announce_seed_password()` prints a startup banner + writes `data/auth/INITIAL_PASSWORD` (0600) —
  the container runs detached, so stdout alone isn't findable enough. **Never reintroduce a constant
  here:** this repo is shared, and a literal would be the known default admin credential of every
  install (and the app answers on the whole LAN). Seeding only runs when no users exist.
- **Frontend**: signed out, every page route goes to `/pantheon/login` (and `/desktop` returns to
  itself after sign-in via `session["return_to"]`); a BFF answer with `signed_out` sends the page
  there too (`ui.common.failed`). The rail foot shows the account and Sign out.
- **Volume**: the whole `data/` is now a single mount (bind mount in `run-podman.sh`, the `companion-assistant-data` volume in `compose.yaml`) so `data/u`,
  `data/auth`, and per-user subdirs all persist — no more per-subdir mount list to keep in sync.

## Data model (models/schema.py)

- **CharacterCard** — V2/V3 spec fields (`description`, `personality`, `scenario` — shown and
  injected as "Background", `first_mes`, `mes_example`, `alternate_greetings`, `system_prompt`,
  `post_history_instructions`, `character_book`, `tags`, `nickname`, …). Local: `image` (base64
  portrait), `appearance` (sprite prompt), `expressions` (emotion→base64),
  **`tts_voice`** (Kokoro id or `piper:<name>`), **`tts_speed`** (0.5–1.5 speaking
  pace, 1.0 = the voice's own tempo; Kokoro multiplier / inverse Piper `length_scale`),
  **`wake_word` / `sleep_phrase` / `farewell`** (per card; blank = the global setting),
  **`vrm` + `avatar_mode`** (`sprite` | `vrm`; a model stays assigned while showing sprites),
  **`portrait_source`** (`""` auto = portrait else neutral sprite | `image` | `sprite` + `portrait_sprite`
  | `vrm` = `vrm_still`, a PNG of the model rendered in the browser by `vtuber.mjs` `captureStill()`,
  taken from `vrm_still_of`) — the Studio's "Profile picture"; `present.portrait_b64()` is the one
  place that picks, so `/pmedia/card/{id}.png` and `has_portrait` follow it everywhere. A missing
  source falls back to auto. The Studio retakes the still on save when the assigned model changed.
  Every card gets **`DEFAULT_ASSISTANT`** (no invented setting; a custom `system_prompt` replaces it)
  and a trailing **`ASSISTANT_FORMAT_NOTE`** after the history — the system
  block loses to the transcript, and a session whose history already contains narrated replies keeps
  producing them however firmly the system prompt forbids it (measured on a live 139-message session:
  3/6 replies narrated without the note, 0/6 with). It names the transcript's style as a habit to
  ignore, which is the part that works.
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
  words — because replies are spoken. A custom `system_prompt` drops that too; say it there.
  Lorebook entries are kept (keyed injection is useful for an assistant's reference notes); their
  old `min_stage` relationship gate is gone and the Studio drops it on save.
- **Persona** — the `{{user}}` card (`name`, `title`/role, `description`, `background`, `image`, `is_default`);
  role+background injected as "About {{user}}". Sidebar has a personas list + editor; one active (default) at a time.
- **ChatMessage** — `role`, `content`, `expression`, `swipes[]`, `swipe_index`, `swipe_expressions[]`.
- **MoodState** — running `valence` (−1…1) + `mood` label (session-level, short-term).
- **AssistantMemory** — the memory store, `data/u/<uid>/assistant/{cid}__{pid}.json`.
  `directives[]` (standing instructions, OBEYED), `facts[]` (things known), `notes[]` (terse task
  state, rewritten whole each fold), `swept_sessions`. **No summary, no relationship, no scene.**
  Served by `services/assistant_memory.py`. Injection puts directives FIRST and never truncates
  them. **→ `MEMORY.md` for the mechanics, `MEMORY_PLAN.md` Phase 6 for why.**
- **ChatSession** — `messages`, `memory` (MemoryState: `pinned` facts, `last_summarized_index` =
  how far the fold has read; `summary` only survives from companion-era sessions), `mood`
  (MoodState), `generation` (GenerationConfig, `max_tokens=220`), `character_id`, `persona_id`.

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
POST /api/users/{id}/copy                  ADMIN: clone characters/personas from your library into theirs
                                           (new ids; sprites/portrait/voice/VRM ride along)
GET  /api/status                           KoboldCPP {online, version, model, context}
POST /api/tts ; GET /api/tts/voices        local TTS (text→WAV) / 54 Kokoro voices + any `piper:<name>`
POST /api/stt                              local faster-whisper STT (multipart audio → {text})
GET/PUT /api/settings                      per-user app settings (hands_free, wake_word, VAD tuning, disable_thinking, context_limit, memory fold)
# cards
GET/POST /api/cards                        list (trimmed, +has_expressions) / create
POST /api/cards/import                     import PNG/JSON card (multipart)
GET/PUT/DELETE /api/cards/{id}             full card ops
GET  /api/cards/{id}/export?version=2|3    download PNG card
# expressions / ComfyUI
GET  /api/comfyui/status | /checkpoints
GET  /api/expressions/emotions             {core:[8], full:[28]}
POST /api/cards/{id}/expressions           generate set — REPLACES all sprites (source portrait|prompt, method kontext|img2img, checkpoint, style, preset core|full, transparent, denoise)
GET  /api/cards/{id}/expressions           labels the card has sprites for
GET/POST/DELETE /api/cards/{id}/expression/{emotion}   serve PNG / regen one / delete one
# personas
GET/POST /api/personas ; PUT/DELETE /api/personas/{id}
# sessions + chat
GET/PUT /api/sessions/{id}/memory          this chat's pinned facts
GET/PUT /api/assistant-memory?character_id=&persona_id=   assistant store (directives[] + facts[] + notes[]); round-trip by id
POST /api/sessions/{id}/sweep              a chat ended: fold all of it into assistant memory (idempotent)
GET  /api/sessions ; POST /api/sessions    list / create ({character_id, persona_id?, greeting_index?})
GET/DELETE /api/sessions/{id} ; DELETE /api/sessions/{id}/messages
PUT  /api/sessions/{id}/swipe              select swipe + reclassify emotion -> {expression}
PUT    /api/sessions/{id}/messages/{mid}            edit one message (a reply: the swipe shown)
DELETE /api/sessions/{id}/messages/{mid}            delete one (not the greeting)
POST   /api/sessions/{id}/messages/{mid}/truncate   drop everything after it ("Save & resend")
       — all three keep memory.last_summarized_index and the pairing's swept_sessions mark
         consistent; a truncate below the fold mark resets it, like clearing (tests/test_messages.py)
POST /api/chat                             SSE stream; done carries {message_id, expression, mood, valence}
```

## How it works

- **Chat:** `build_messages()` assembles system (card system prompt or `DEFAULT_ASSISTANT` + persona
  + lorebook + character block + pinned facts + assistant memory + today's date) → history (`char`
  and `user` messages only) → `post_history_instructions` → **one trailing note** (exact time when
  asked, expression beats, `ASSISTANT_FORMAT_NOTE`, the retake note, then
  `focus_note`); streams via `kobold.chat_generate_stream()`. A stop sequence on the user's turn is
  added. After the reply: strip bare labels, the courtesy question and the closing offer; pick the
  expression (inline tag, else classify when the card has sprites); update mood; save; send `done`
  — then the memory fold in the background (`_memory_job`, see Conventions).
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
  **Sprites from the portrait (2026-11-06):** the Studio asks two things explicitly — *Make
  sprites from* the portrait or the appearance prompt, and *Change the expression with* Flux
  Kontext or a model (img2img, with the strength slider) — sent as `source` / `method` to
  `generate_expressions`. Portrait + Kontext needs no model. A choice that can't be honoured
  is a 400, never a fallback: a portrait request on a card with no saved portrait used to
  come back as a new character drawn from the prompt, and the portrait option only appeared
  after a reload (it read `has_portrait` once, at load). Generating from a portrait picked but
  not saved saves first. Audio checkpoints (ACE-Step, Stable Audio) are left out of the model
  list. `remove_background` keeps the uncut image when the cutout leaves under 5% of the
  pixels (it erased a flat drawing entirely). `/pmedia/card/{id}.png?upload=1` serves the
  uploaded portrait itself. Tests: `tests/test_sprites_from_portrait.py`.
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
  Driven by the sprite label (or an inline `[expr:x]` tag), so a card without sprites keeps a
  neutral mood. No header meter: the mood picks the sprite and the avatar's resting face.
- **Memory: `MEMORY.md` is the reference.** One pipeline: `AssistantMemory` — directives, facts,
  task notes, per (character, persona); plus this chat's pinned facts. When `summarize_after`
  un-folded messages pile up, `_maintain_memory` folds all but `keep_recent` of them
  (`assistant_memory.fold`: notes rewritten whole, directives/facts extracted from the user's turns
  only) and moves `last_summarized_index`, so those turns leave the prompt. **Ending a chat saves
  it** (`POST /sessions/{id}/sweep` folds everything, recent turns included): leaving it, a new
  chat, the hands-free conversation ending, the page or desktop window closing; the server also
  sweeps 5 min after the last reply and catches up at startup (`MEMORY.md` → *When a sweep runs*).
  No summary, no recall.
  Budget-capped, oldest auto entries evicted first, editable in the chat's ⋮ → Memory modal.
  Deleting a character or persona deletes its pairings' memory (`_forget_pairings`).
- **Storage:** plain JSON under `data/`, UUID ids.

## Frontend map (appcode/ + the islands)

Python in the browser (Pyodide + wapyt). **Gotchas that cost time:** BFF modules are re-executed per
call (no state; import plain modules inside methods; never import one BFF from another — shared
helpers go in `pantheon_web/present.py`); async exports are awaited directly, sync ones get `_async`;
never destroy a proxy inside its own callback; `overflow: clip`, not `hidden`, on fixed-height boxes
(`focus()` scrolls `hidden` ones).

| file | what |
|---|---|
| `pantheon.py` | the shell: 292 px rail (tile nav Staff/Hire/Chats/Personas/Settings, Recent chats, Staff with who is on shift, persona menu, theme swatches, sign out), top bar (search, KoboldCPP status, Hire), hash routing, mobile drawer, `end_chat` (save a chat to memory when it ends); loads `pantheon.css` and `voice.js` |
| `ui/discover.py` · `ui/profile.py` | the Staff page (route `discover`: hero with who is on shift, search, tags) · profile modal (greetings, start / continue, export, copy to user) |
| `ui/chat.py` | the chat: bubbles, streaming + Stop, swipes, ⋯ per message (another take, edit / save & resend, read aloud, copy, delete), starter chips, persona + memory count in the header, the **stage** (`.pt-stage`: its own right-hand column, `--pt-stage-w` 36% of the chat area, min 260 px, gone below 900 px; holds the sprite or the 3D avatar slot, only for cards with either — `vtuber.mjs` `frameBust()` fits the camera to its shape, gaze aimed at eye height), speaker / hands-free / push-to-talk; drives `voice.js` |
| `ui/chats.py` | all chats |
| `ui/studio.py` | the card editor ("Staff studio", route `create`): every V2/V3 field, greetings, starters, lorebook, voice + test, expression generation + gallery + preview, VRM import/assign/audit, import / export / duplicate, local drafts |
| `ui/people.py` | personas |
| `ui/settings.py` | settings (model, memory, voice + microphone picker), password, users (admin), the guide |
| `ui/memory.py` | the chat's Memory modal (pinned facts; instructions, facts, where things stand) |
| `ui/common.py` | spawn, Listeners, Modal, toast/failed, csrf_token, avatar/portrait helpers |
| `bff/*.py` | `shell`, `library`, `chat` (incl. the `@bff_stream` relay of `main.chat`), `studio`, `people`, `account`, `memory` |
| `static/js/voice.js` | `window.PantheonVoice` + `pvoice:*` events (PYTINCTURE_PLAN.md, Phase 6) |
| `ui/voicecore.py` · `ui/mic.py` | hands-free decisions in Python (#5): voice.js measures each 4096-sample capture block's level and calls `brain.block(rms, block, now, audible, herTurn, busy)`; the brain answers through `brainOps()` (transcribe, send, abortNow/abortAndWait, say, toast, persist, flags). Measured (Pyodide 0.29.3): 0.003 ms a block with the level from JS, 0.31 ms if Python measured it, against 85 ms between blocks. Browser-checked with a fake mic in the image: wake word sends the rest, no wake word sends nothing, speaking over a streaming reply stops it at speech onset and sends the transcript (strict) |
| `static/js/vtuber.mjs` | `avatar:card/speak/expr/mood` in, `avatar:active/error` out |

## Feature set (all built + verified)

✅ Card V2/V3 import/export · ✅ streaming chat · ✅ swipes + per-swipe emotions · ✅ **assistant
memory** (directives/facts/notes, per char+persona, saved when a chat ends, curated + revertible) · ✅ persona ·
✅ card editor (fields, portrait, lorebook, Duplicate) · ✅ ComfyUI expression sprites (same-character,
transparent via rembg, Core-8/Full-28, gallery w/ per-sprite regen/delete, Flux Kontext) · ✅ live
portrait swap · ✅ running mood/valence · ✅ local **TTS + STT** (Kokoro/Piper,
faster-whisper), hands-free, wake word, barge-in · ✅ **VRM avatar** (idle life, lip-sync, expressions,
gestures) · ✅ **desktop mode** · ✅ multi-user · ✅ Mira seeded into new libraries.

## Conventions & gotchas

- Rebuild after code/template/static edits (`./run-podman.sh --rebuild`); `podman exec -i` for heredocs.
- **The chat stream sends `{"ping": true}` every `STREAM_KEEPALIVE_S` (10 s) of silence** (`main.chat`
  `gen()` wraps `_turn()`). pytincture closes a BFF stream after `BFF_STREAM_IDLE_TIMEOUT_SECONDS`
  (30 s) without an item, and that once cancelled turns before `_save`: the reply streamed in full,
  then vanished on the reload with no error (2026-10-29: 5 of 24 replies in a long chat, while the
  memory step still ran before `done`). Every client ignores unknown events. Stop / a closed tab
  still cancels the turn unsaved.
- **A BFF argument that may be `None` must be annotated `X | None`.** pytincture checks every
  BFF argument against its annotation, as written, before the method runs; `long: dict = None`
  answered 400 to the `None` the page sends, so the Memory modal's Save wrote nothing for
  chats without long-term memory (2026-11-04). `tests/test_bff_signatures.py` scans every
  BFF method for it.
- **Nothing that waits on the model may save a stale copy.** `_save_turn` always keeps the
  stored `memory` (a turn never changes it; the job and the Memory modal do), and a fold,
  threshold or idle sweep, goes through `_store_fold`: only what the fold changed (entries
  added or evicted by id, the notes if nobody edited them, swept marks) is applied
  to a fresh read. The sweep moves only the session's index, on a fresh read.
  Tests: `tests/test_chat_stream.py`, `tests/test_fold_merge.py`.
- **Repetition guards** (measured in companion's `eval/repetition`, not carried here): the DRY sampler is on by default
  (`GenerationConfig.dry_*`, sent by `kobold_params`, so the auto-continue gets it too). It
  penalises reusing any word sequence already in the context, which `rep_pen` (single tokens)
  doesn't. "Another take" quotes the take being replaced (`prompt_builder.retake_note`, in the
  trailing note): the old take used to be dropped from the prompt, so the model had nothing
  to differ from and often opened the same way. Tests: `tests/test_repetition_guards.py`.
- **Memory maintenance runs after `done`** (`_memory_job`, 2026-10-29). A turn classifies, saves
  (`_save_turn`) and sends `done`; the assistant fold then runs in a background task, one per
  session at a time (`_memory_locks`), starting from a fresh read. It can overlap the next turn, an
  edit or a truncate, so it writes only `memory.last_summarized_index` (and the pairing's store via
  `_store_fold`), onto a fresh read, and only if the index didn't move and the folded turns are
  unchanged (`_apply_memory`). A dropped or lost job (restart) is harmless — the next turn re-checks
  the threshold. Tests: `tests/test_chat_stream.py`, `tests/test_fold_merge.py`.
  **A reply cancels a running job** (`_yield_memory_jobs`): KoboldCPP does one generation at a
  time, so a message sent while the summary ran waited behind it (first token at 52 s). The job's
  calls carry a genkey; the reply aborts it (`kobold.abort`), the job stores nothing and runs again
  after a later turn. `_MemoryYielded` is a `BaseException` so the memory steps' best-effort
  `except Exception` can't swallow it; after `MEMORY_MAX_YIELDS` (2) in a row a job is left to
  finish. Tests: `tests/test_memory_yields.py`.
- **Tests never touch a real library or backend** (`tests/conftest.py`): the throwaway
  `PANTHEON_DATA_DIR` is set before any import, and a model call a test didn't stub raises.
- **TTS backends** (`services/tts.py`): voice ids prefixed **`piper:`** route to Piper (one ONNX per voice,
  loaded from `PIPER_DIR`, cached; its espeak-ng data ships in the `piper-tts` wheel), everything else to
  Kokoro. Piper voices are **mounted, not baked** (`../jarvis` → `/app/models/piper:ro,z`) — dropping a new
  `<name>.onnx` + `<name>.onnx.json` pair in there needs only a container restart, no rebuild. Piper honours
  each voice's own `inference` block; `speed` scales its `length_scale` inversely. Note Piper voices carry
  their own sample rate (jarvis = 22.05 kHz vs Kokoro's 24 kHz).
- **Speech runs in this container** (Kokoro, or Piper when its voices are mounted). `voice.js`
  fetches one WAV per sentence from `/pmedia/tts` and prefetches the next while one plays. A long
  opening sentence is split at an early clause break, and a mid-sentence ellipsis does not end a piece.
- **Orpheus voices were removed on 2026-11-18**, with the ai-stack backend. `CharacterCard` maps a
  saved `orpheus:<name>` voice to Kokoro when it loads (`schema.current_voice_id`), and the next save
  stores the new id. Older replies can still contain sound tags such as `<laugh>`: they stay hidden
  in the chat view, chat-list preview and desktop caption, and are stripped before speech.
- **What gets spoken** (`feedSpeech` in `static/js/voice.js`): **everything**, flushed a sentence at
  a time as it streams. (The roleplay app speaks only quoted dialogue; here quotes mark a term or a
  citation, and feeding only the quoted spans spoke the citations and dropped the answer.)
  `cleanForTTS`  citations and dropped the answer. `cleanForTTS` then unwraps any markdown that got through —
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
  `Current date and time: …` line in the trailing note of `build_messages` when the user asks
  about the time, placed **after the history** — it changes every minute, and in the system block
  that would invalidate KoboldCPP's cached prefix and re-process the whole history every turn. The
  system block carries only the date.
- **Prompt overflow is silent and truncates from the FRONT** (measured — `PROMPT_BUDGET.md`), so
  the app now fits the prompt itself (`services/context.py`, 2026-10-30). The window is
  `context.effective()`: ⚙ Settings → Model → **Context window** (`AppSettings.context_limit`, 0 =
  Auto or one of `CONTEXT_SIZES` 4096…131072, 48K+ for the AMD box), never above what KoboldCPP reports at
  `/api/extra/true_max_context_length` (`kobold.context_limit()`, cached 30 s, last good value kept
  while it's down, 8192 if never seen). `build_messages(context_tokens=…)` drops the **oldest
  verbatim turns** until system block + history + trailing notes + reply reserve fit (chars/4.0,
  deliberately low); the newest message always stays, the system block is never touched, and a
  trim is logged `[context] dropped N`. The memory folds size their transcript passes from the
  same window (`memory.fold_budget`). Raising the window past 8192 needs the backend too:
  `KOBOLDCPP_CONTEXT` in `koboldcpp/model.env` (ai-stack). Not built yet from PROMPT_BUDGET.md:
  per-part caps and the eviction order for an oversized *system* block. The trailing note is
  the most overflow-resistant part; keep the format rules there.
- CSS (`appcode/static/pantheon.css`): an element with its own `display` beats the `hidden`
  attribute, so each such rule carries a `[hidden] { display: none }` partner; accessible hidden
  radios use `opacity: 0`, not `display: none`.
- `GET /api/cards` strips `expressions` (adds `has_expressions`); fetch the full card before editing.
- **Sprite generation re-reads the card before saving** (`generate_expressions` /
  `regenerate_expression`). It takes minutes; saving the copy loaded at the start silently
  undid any edit made meanwhile, in either page. Only the sprite fields are written onto a fresh read.
- Mood updates on generation only (not re-rolls). Sprites are same-character via img2img from a
  reproducible base (fixed per-card seed) — single-emotion regen stays consistent.
- The whole `data/` is one bind mount now, so new subdirs persist automatically (no per-subdir mount list).
  Historically `run-podman.sh`/`compose.yaml` mounted each subdir and a missed one wiped state on rebuild.
- `podman exec` heredocs need `-i`. Auth: never log/return password hashes; keep the scrypt params in
  `services/auth.py` in sync with stored hashes (changing them invalidates existing passwords).

## Roadmap / next

- **pytincture page** (`PYTINCTURE_PLAN.md`): ✅ Phases 0–7 — the page replaced the classic one on
  2026-10-26. ✅ **Phase 8: other devices** (2026-10-30) — plain
  HTTP on the LAN through `LanAsLoopback`, no TLS (an HTTPS proxy was built and dropped the same day).
- ✅ **Personas as cards** — name/role/description/background/portrait, rail list + editor,
  single-active enforced, role+background feed the prompt.
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
  gesture track (nod/tilt/leanIn/recoil/shrug; clips are Track B3) and a posture bias channel
  (`avatar:mood`'s `stage`; this app always sends "", so the posture stays neutral). **Pose composition rule: `resetPose()` returns every animated
  bone to rest each frame and all channels are purely ADDITIVE** — mixing absolute assignment with `+=`
  is what sent the arms wandering, and shoulders must stay in the rest set.
  **All six channels of "fully animated" are now live.** ✅ **3e arm poses** (2026-11-23):
  held poses so a quiet avatar doesn't stand with her arms hanging: `foldedArms`, `handOnHipL`/`R`,
  `handsOnHips`, `handsClasped`, `thinking` (hand to chin). `POSES` in `vtuber.mjs` are
  **directions in the body's frame** (`[out, up, forward]` per segment, the same numbers for both
  arms), and `targetArms` turns each bone so its child points that way on *this* model. Euler
  angles were tried first and failed: the two test models (`baseline.vrm`, armSign −1, and a VRoid
  VRM0, +1) need opposite rotations, and folding an arm is too large a move to add angles. Arm bones
  are set absolutely each frame (slerp from rest, 0.9 s smoothstep), so they can't drift; shoulders
  stay additive. The scheduler alternates rest (6–14 s) and a weighted random pose (7–16 s), never
  re-posing while she talks; an emotion beat can ask for a fitting one (`POSE_FOR`: anger → folded
  arms, thoughtful → thinking, pride → hands on hips…, 20 s cooldown). `/static/avatar-test.html`
  has a pose picker and a turn slider (`stats.pose`, `stats.turn`); `window.__avatarPoses` is
  editable live. Picked against screenshots of both models, front and side. **Fingers curl** with
  the pose (`curl: [fingers, thumb]`, 0 straight to 1 a loose fist; rest keeps a slight 0.18): the
  normalized rig's reference pose has the palms down, so every hand's palm side is local −Y and
  each joint turns about (its direction × palm); the thumb aims between the palm and the little
  finger so it closes across. Checked in close-ups on both models. ✅ **Phase 5 interruption done** — barge-in works in hands-free mode; speaking over her calls
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
- **The Menu (skills) for the staff** — plan in `SKILLS_PLAN.md`; users see it as "the Menu" (Vocabulary). ✅ **Phase 0 done** — host-derived
  `TZ` + assistant-mode date/time injection (she was inventing dates; the container clock was never
  the problem). ◻ Next: skill scaffolding (`CharacterCard.skills`, registry, two-pass tool loop).
  **Verified live:** KoboldCPP + this Qwen3.5 fine-tune support OpenAI `tools` **including over
  streaming SSE**, with correct arguments, no spurious calls, and no loss of character voice — so
  skills use real tool calling, not `[tag]` scraping. Then ◻ System Info (`get_system_status`:
  uptime/disk/**GPU VRAM**/services) and ◻ **Media** — find and play music/video through **Orpheus**,
  our own media server (on Homelab) + Flutter player, with Cantinero paired as a `controller` device.
  Replaced the Navidrome/DJ plan on 2026-09-15; design in `../orpheus/docs/architecture.md`. NAS: UGREEN
  NASync DH2300 @ `192.168.1.219` (SMB 445), mounted by the Orpheus server, not by Cantinero. Hard part
  is still audio coexistence: music breaks the half-duplex mic gate in both directions.
- **Memory Phase 7** — memory belongs to the bartender, not the chat: shared basics about the
  user plus per-bartender instructions and notes, folds per episode (voice-friendly), tiered
  per-turn recall, memory commands. Plan in `MEMORY_PLAN.md` → Phase 7, issues #15–#23 (#15, the
  eval fixtures, first).
- **Sprite-pack import** — drop Full-28-named PNGs straight into `card.expressions`.
- ◻ Group chats · ◻ faster inference backend (still KoboldCPP `:5001`).
- Tuning knobs: `expressions` img2img denoise, `GenerationConfig`, `AppSettings.summarize_after/keep_recent`.
