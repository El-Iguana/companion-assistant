# Pantheon — Project Context

The product name is **Pantheon** (formerly *La Cantina* — renamed 2026-08-27; the old name may still
appear in older commits and notes). The project directory, image, container, and `stack` command all
remain `companion` (renaming them was deliberately deferred).

A self-hosted **character-card AI companion**. FastAPI + uvicorn, runs as a Podman container,
talks to **KoboldCPP** (text) and **ComfyUI** (portraits/expressions) on the shared
`ai-net` network. Everything local; no external services, no database.

Built around the **Character Card V2/V3 spec** (SillyTavern/Chub compatible). Scaffolded from
`~/Development/ai_ecosystem/ai_story_maker` (reused `services/kobold.py` + `services/comfyui.py`). Frontend is **vanilla JS,
no framework, no build step**.

Companion features layered on top: streaming chat, swipes, transparent expression sprites with a
live-swapping portrait, a running **mood/valence**, and a **relationship system** (multi-axis +
intimacy/consent). Relationship design detail in `RELATIONSHIP_PLAN.md`.

## Running

```bash
./run-podman.sh              # build (first run) + start on :7861
./run-podman.sh --rebuild    # REBUILD after ANY code/template/static change (baked into image)
python run.py                # local dev w/ autoreload (needs deps on host)
# open http://localhost:7861 ; logs: podman logs -f companion ; stop: podman stop companion
```

Prereqs: `~/Development/ai_ecosystem/koboldcpp/start.sh` (text). For sprites: `~/bin/comfyui.sh` (ComfyUI in the `comfyui`
distrobox on :8188). They share VRAM — use SD 1.5 checkpoints (e.g. `absolutereality_v181`) when both
run. **Data (`data/…`) is a mounted volume**, so editing card/session/relationship JSON is live; **code
is baked in — rebuild after edits.** `podman exec` needs `-i` to receive a stdin heredoc.

## Container topology

| Container  | Network         | Address                         | Role         |
|------------|-----------------|---------------------------------|--------------|
| companion  | ai-net | `0.0.0.0:7861`                  | this app     |
| koboldcpp  | ai-net | `koboldcpp:5001`                | LLM text gen |
| comfyui    | host            | `host.containers.internal:8188` | image gen    |

## File structure

```
main.py                 # FastAPI app — ALL routes + JSON file store (_save/_load/_list, relationship helpers)
run.py                  # dev launcher (:7861)
models/schema.py        # Pydantic models (below)
services/
  kobold.py             # KoboldCPP client. chat_generate_stream() = OpenAI SSE + auto-continue (2 rounds)
  comfyui.py            # ComfyUI client: generate() txt2img + generate_img2img() + upload_image()
  card_io.py            # V2/V3 PNG (tEXt chara/ccv3) + JSON import/export
  prompt_builder.py     # macros, system prompt, lorebook scan, injects relationship_note + date/time
  memory.py             # per-session summary + cross-session long-term memory + recall + auto-facts
  embeddings.py         # BGE-small ONNX/CPU sentence embeddings (baked, 33MB, zero VRAM)
  expressions.py        # emotion classify + valence map + sprite gen (img2img) + rembg cutout
  relationship.py       # multi-axis relationship + stage + intimacy ladder + descriptor
  tts.py                # local TTS (ONNX/CPU) — Kokoro + Piper (`piper:<name>`), text -> WAV, strips *action* narration
  stt.py                # local faster-whisper STT (base.en, CPU int8) — audio bytes -> text
  auth.py               # scrypt password hash + HMAC-signed session token (stdlib only)
templates/index.html · static/css/style.css · static/js/app.js
data/auth/  secret · users/<uid>.json                    # accounts + signing secret (global)
data/u/<uid>/  characters/ personas/ sessions/ relationships/ presets/ settings/ memories/  # per-user isolated library
models/rembg/           # baked: isnet-anime.onnx + u2net.onnx (background removal)
models/kokoro/          # baked: kokoro-v1.0.onnx + voices-v1.0.bin (TTS)
models/piper/           # MOUNTED ro from ../jarvis — one .onnx + .onnx.json per Piper voice
models/whisper/         # baked: faster-whisper base.en (CTranslate2) — STT
models/embed/           # baked: bge-small-en-v1.5 int8 ONNX + tokenizer — semantic recall
RELATIONSHIP.md         # relationship system reference — axes, ladder, boundary injection
RELATIONSHIP_PLAN.md    # relationship system design + decision log (phases 1-3 done)
MEMORY.md               # memory system reference — the four scopes, recall gate, auto-facts
MEMORY_PLAN.md          # memory design + decision log (phases 1-5 done)
PROMPT_BUDGET.md        # shared: what's in a prompt, overflow behavior, eviction plan (NOT built)
SKILLS_PLAN.md          # assistant skills via tool calling: System Info + DJ/NAS music (Phase 0 done)
```

## Auth / multi-user (privacy)

Login-gated, role-based, per-user isolated libraries.
- **Accounts** (`models.schema.User`): `data/auth/users/<uid>.json`; password = **scrypt** hash + per-user
  salt (`services/auth.py`, stdlib — no bcrypt dep). Roles: **`admin`** | **`user`**.
- **Session**: HMAC-signed stateless token (`uid.exp.sig`) in an **HttpOnly cookie**, signed with
  `data/auth/secret` (persisted). `current_user`/`require_admin`/`get_ctx` FastAPI dependencies enforce it.
- **Isolation**: every data route resolves a per-request **`Ctx`** → `data/u/<uid>/{characters,personas,
  sessions,relationships,presets,settings}`. Users never see each other's data. (`_load/_save/_list` take
  the Ctx's folder; relationship/settings/persona helpers take `cur`.)
- **Admin-only**: `/api/users` CRUD + password reset; guards against deleting yourself or the last admin
  (deleting a user removes their whole `data/u/<uid>` library). Normal users manage only their own libraries.
- **Seed + migrate (first run)**: `_seed_and_migrate()` creates **ADMIN** and moves any legacy flat
  `data/<subdir>/*` into the admin's library so nothing is orphaned. The password comes from
  `COMPANION_ADMIN_PASSWORD` if set, else `_seed_password()` mints a random one and
  `_announce_seed_password()` prints a startup banner + writes `data/auth/INITIAL_PASSWORD` (0600) —
  the container runs detached, so stdout alone isn't findable enough. **Never reintroduce a constant
  here:** this repo is shared, and the app publishes 7861 on all interfaces, so a literal would be
  the known default admin credential of every install. Seeding only runs when no users exist.
- **Frontend**: full-screen `#login` overlay; `boot()` checks `/api/me` and gates `init()`; `api()` shows
  login on any 401; sidebar `#userbar` (name·role, Logout, admin-only **Users** modal).
- **Volume**: the whole `data/` is now a single bind mount (`run-podman.sh`/`compose.yaml`) so `data/u`,
  `data/auth`, and per-user subdirs all persist — no more per-subdir mount list to keep in sync.

## Data model (models/schema.py)

- **CharacterCard** — V2/V3 spec fields (`description`, `personality`, `scenario`, `first_mes`,
  `mes_example`, `alternate_greetings`, `system_prompt`, `post_history_instructions`,
  `character_book`, `tags`, `nickname`, …). Local: `image` (base64 portrait), `appearance` (sprite
  prompt), `expressions` (emotion→base64), **`power_dynamic`** (−100…100), **`social_acceptance`** (0…100),
  **`tts_voice`** (per-character voice — Kokoro id or `piper:<name>`), **`tts_speed`** (0.5–1.5 speaking
  pace, 1.0 = the voice's own tempo; Kokoro multiplier / inverse Piper `length_scale`),
  **`chat_mode`** (`roleplay`|`assistant`) — picks the built-in
  system prompt (`prompt_builder.DEFAULT_ROLEPLAY` scene/world vs `DEFAULT_ASSISTANT` personality-only, no
  invented setting); a custom `system_prompt` overrides both. Editor: "Chat mode" select.
  **`DEFAULT_ASSISTANT` composes for the ear** — spoken prose, no markdown/tables/lists, numbers as
  words — because assistant replies are spoken. A custom `system_prompt` drops that too; say it there.
- **Persona** — the `{{user}}` card (`name`, `title`/role, `description`, `background`, `image`, `is_default`);
  role+background injected as "About {{user}}". Sidebar has a personas list + editor; one active (default) at a time.
- **ChatMessage** — `role`, `content`, `expression`, `swipes[]`, `swipe_index`, `swipe_expressions[]`.
- **MoodState** — running `valence` (−1…1) + `mood` label (session-level, short-term).
- **Relationship** — persistent per (character, persona): axes `affection/trust/attraction/tension`,
  `emotional_intimacy`, `physical_willingness`, optional `power_dynamic`/`social_acceptance` overrides,
  `milestones`.
- **CharacterMemory** — persistent per (character, persona), like Relationship: `facts[]`
  (`MemoryFact{text, origin: user|auto}`), `summary` (cumulative across every chat in the pairing),
  `swept_sessions` (session_id -> message index already folded, so sweeps are idempotent). Injected as
  "What {{char}} remembers about {{user}}"; **not** used in scenario sessions.
- **ChatSession** — `messages`, `memory` (MemoryState), `mood` (MoodState), `generation`
  (GenerationConfig, `max_tokens=220`), `pending_milestone`, optional session-scoped `relationship`
  (scenario mode), `character_id`, `persona_id`.
  (Lorebook entries carry `min_stage` for stage-gating.)

## API endpoints (main.py)

All `/api/*` except `login`/`me` require auth (401 otherwise); user-mgmt requires admin (403).
```
GET  /                                     SPA (public; the SPA gates itself via /api/me)
# auth
POST /api/login ; POST /api/logout         set / clear the signed session cookie
GET  /api/me                               {user:{username,role}} | {user:null}  (public)
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
POST /api/sessions/{id}/sweep              idle trigger: fold a finished chat into long-term memory (idempotent)
GET  /api/sessions ; POST /api/sessions    list / create ({character_id, persona_id?, greeting_index?, preset_id?, mode?})
GET/DELETE /api/sessions/{id} ; DELETE /api/sessions/{id}/messages
PUT  /api/sessions/{id}/swipe              select swipe + reclassify emotion -> {expression}
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
- **Mood/valence:** per reply, `EMOTION_VALENCE` → running `session.mood` via EMA; **neutral holds**.
  Header mood meter shows it.
- **Relationship (`relationship.py`):** per (character, persona), stored `data/relationships/{cid}__{pid}.json`.
  Emotions move axes each reply (`RATE`); grows/drops, **no idle decay**. Stage derived (Stranger→Intimate,
  romance gated on attraction; "(Strained)" at high tension). **Intimacy/consent:** `emotional_intimacy`
  (capped by trust) + `physical_willingness` (gated by trust×attraction, `WRATE`, suppressed by tension) →
  a **7-rung clinical ladder** (`PHYSICAL_TIERS`, cumulative, no cap). **Context** (`power_dynamic`,
  `social_acceptance`; card default + per-relationship override) **slows pace & adds caution — never
  compliance**; agency-preserving, consent enthusiastic/revocable. `descriptor()` injects all this into her prompt.
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
- **Memory:** three scopes. Per-session pinned facts + a rolling summary (older turns fold in once
  ~24 accumulate, tunable in ⚙ Settings). Across sessions, `CharacterMemory` per (character, persona):
  written by two triggers — the chat flow folds forward whatever the session summary just absorbed, and
  `POST /sessions/{id}/sweep` (frontend 5-min idle timer) folds a finished chat. Injected between the
  pinned facts and "Story so far", budget-capped (history truncated to its share, facts dropped
  oldest-first — an oversized entry costs only itself). Facts capped at 40. Both scopes are editable in
  the 📌 Memory modal's two tabs; auto-written facts carry an `auto` badge and can be reverted.
- **Storage:** plain JSON under `data/`, UUID ids.

## Frontend map (static/js/app.js)

`state = {cards, sessions, persona, session, streaming}`. Chat pane: portrait overlay (mood-driven),
header **mood meter** + **relationship strip** (stage, 4 axis bars, Emotional/Comfort readout), 🖼 Gallery,
🎭 Expressions. Modals: card editor (all fields + portrait upload + **lorebook** + **power/social sliders**
+ Duplicate), expression gallery (grid, per-sprite regen/delete), **help** (`?` in the sidebar header →
in-app guide on custom system prompts / curation with copy-paste examples). The 🎭 Expressions panel also has a
**preview stage** (`refreshExprPreview/showExprPreview`, ‹ › to cycle) that renders each sprite at the
real in-chat `#portrait` framing (bottom-anchored, height-driven, over a transparency checker) + shows
its natural WxH — for checking sizing/framing consistency across emotions. Key fns: `renderSession→
renderMessages→updatePortrait/updateMood`, `renderRelationship/loadRelationship`, `send` (SSE),
`gotoSwipe`, editor (`buildCardPayload/renderLore`), gallery (`refreshGallery/regenOne`).

## Feature set (all built + verified)

✅ Card V2/V3 import/export · ✅ streaming chat (single-beat, non-repetitive) · ✅ swipes + per-swipe
emotions · ✅ rolling memory · ✅ **cross-session long-term memory** (per char+persona, idle sweep,
curated + revertible facts) · ✅ persona · ✅ character editor (fields, portrait, **lorebook**, Duplicate,
power/social) · ✅ ComfyUI expression sprites (img2img same-character, transparent via rembg, Core-8/Full-28,
**gallery** w/ per-sprite regen/delete) · ✅ live portrait swap · ✅ running **mood/valence** · ✅ empty-greeting
fallback · ✅ delete chats/characters · ✅ **relationship system** (multi-axis, per char+persona, behavior
injection, rel-strip) · ✅ **intimacy/consent** (split willingness, 7-rung ladder, power/social context,
agency-preserving) · ✅ **milestones** (toast + injected beat) · ✅ **stage-gated lorebook** · ✅
per-relationship **context override** (⚙).

## Conventions & gotchas

- Rebuild after code/template/static edits (`./run-podman.sh --rebuild`); `podman exec -i` for heredocs.
- **TTS backends** (`services/tts.py`): voice ids prefixed **`piper:`** route to Piper (one ONNX per voice,
  loaded from `PIPER_DIR`, cached; its espeak-ng data ships in the `piper-tts` wheel), everything else to
  Kokoro. Piper voices are **mounted, not baked** (`../jarvis` → `/app/models/piper:ro,z`) — dropping a new
  `<name>.onnx` + `<name>.onnx.json` pair in there needs only a container restart, no rebuild. Piper honours
  each voice's own `inference` block; `speed` scales its `length_scale` inversely. Note Piper voices carry
  their own sample rate (jarvis = 22.05 kHz vs Kokoro's 24 kHz).
- **What gets spoken** (`feedSpeech` in `app.js`): roleplay cards speak only the **quoted dialogue**,
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
- CSS: `[hidden] { display:none !important }` at top of style.css so the HTML `hidden` attr wins over
  element display rules; toggle via `.hidden`.
- Starlette: `TemplateResponse(request, "index.html")` (request first).
- `GET /api/cards` strips `expressions` (adds `has_expressions`); fetch the full card before editing.
- Relationship/mood update on generation only (not re-rolls). Sprites are same-character via img2img from a
  reproducible base (fixed per-card seed) — single-emotion regen stays consistent.
- All characters adult; intimacy code is abstract state (tiers/thresholds) — the local model writes prose.
- The whole `data/` is one bind mount now, so new subdirs persist automatically (no per-subdir mount list).
  Historically `run-podman.sh`/`compose.yaml` mounted each subdir and a missed one wiped state on rebuild.
- `podman exec` heredocs need `-i`. Auth: never log/return password hashes; keep the scrypt params in
  `services/auth.py` in sync with stored hashes (changing them invalidates existing passwords).

## Roadmap / next

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
  Next phases: ◻ Phase 2 inline `[expr]` tags (hybrid w/ mood/relationship) · ◻ Phase 3 **animated
  VRM avatar** (`@pixiv/three-vrm`), split 3a renderer / 3b idle life / 3c speech / 3d emotion+gesture ·
  ◻ Phase 5 interruption. Runs alongside **Track B**, the VRM/animation content pipeline. Local-first
  voice throughout. Sprites remain the fallback tier for cards with no `.vrm`.
- **Skills for assistant characters** — plan in `SKILLS_PLAN.md`. ✅ **Phase 0 done** — host-derived
  `TZ` + assistant-mode date/time injection (she was inventing dates; the container clock was never
  the problem). ◻ Next: skill scaffolding (`CharacterCard.skills`, registry, two-pass tool loop).
  **Verified live:** KoboldCPP + this Qwen3.5 fine-tune support OpenAI `tools` **including over
  streaming SSE**, with correct arguments, no spurious calls, and no loss of character voice — so
  skills use real tool calling, not `[tag]` scraping. Then ◻ System Info (`get_system_status`:
  uptime/disk/**GPU VRAM**/services) and ◻ **DJ** (NAS music). NAS identified: UGREEN NASync DH2300
  @ `192.168.1.219` (UGOS Pro `:9999`, SMB 445) — **no music API is listening yet**; plan starts by
  standing up Navidrome (Subsonic API) there. Hard part is audio coexistence: music breaks the
  half-duplex mic gate in both directions.
- **Sprite-pack import** — drop Full-28-named PNGs straight into `card.expressions`.
- Group chats · persona editor modal · embedding memory · TTS · faster inference backend.
- Tuning knobs: `relationship.RATE/WRATE/ESC_RATE/_TIER_THRESHOLDS`, `expressions` img2img denoise, `GenerationConfig`.
```
