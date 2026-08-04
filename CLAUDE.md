# Companion — Project Context

A self-hosted **character-card AI companion**. FastAPI + uvicorn, runs as a Podman container,
talks to **KoboldCPP** (text) and **ComfyUI** (portraits/expressions) on the shared
`sillytavern-net` network. Everything local; no external services, no database.

Built around the **Character Card V2/V3 spec** (SillyTavern/Chub compatible). Scaffolded from
`~/ai_story_maker` (reused `services/kobold.py` + `services/comfyui.py`). Frontend is **vanilla JS,
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

Prereqs: `~/koboldcpp/start.sh` (text). For sprites: `~/bin/comfyui.sh` (ComfyUI in the `comfyui`
distrobox on :8188). They share VRAM — use SD 1.5 checkpoints (e.g. `absolutereality_v181`) when both
run. **Data (`data/…`) is a mounted volume**, so editing card/session/relationship JSON is live; **code
is baked in — rebuild after edits.** `podman exec` needs `-i` to receive a stdin heredoc.

## Container topology

| Container  | Network         | Address                         | Role         |
|------------|-----------------|---------------------------------|--------------|
| companion  | sillytavern-net | `0.0.0.0:7861`                  | this app     |
| koboldcpp  | sillytavern-net | `koboldcpp:5001`                | LLM text gen |
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
  prompt_builder.py     # macros, system prompt, lorebook scan, injects relationship_note
  memory.py             # rolling summarization of older turns
  expressions.py        # emotion classify + valence map + sprite gen (img2img) + rembg cutout
  relationship.py       # multi-axis relationship + stage + intimacy ladder + descriptor
  tts.py                # local Kokoro TTS (ONNX/CPU) — text -> WAV, strips *action* narration
templates/index.html · static/css/style.css · static/js/app.js
data/  characters/ personas/ sessions/ relationships/ presets/ expressions/   # one JSON per item
models/rembg/           # baked: isnet-anime.onnx + u2net.onnx (background removal)
models/kokoro/          # baked: kokoro-v1.0.onnx + voices-v1.0.bin (TTS)
RELATIONSHIP_PLAN.md    # relationship system design + phases
```

## Data model (models/schema.py)

- **CharacterCard** — V2/V3 spec fields (`description`, `personality`, `scenario`, `first_mes`,
  `mes_example`, `alternate_greetings`, `system_prompt`, `post_history_instructions`,
  `character_book`, `tags`, `nickname`, …). Local: `image` (base64 portrait), `appearance` (sprite
  prompt), `expressions` (emotion→base64), **`power_dynamic`** (−100…100), **`social_acceptance`** (0…100),
  **`tts_voice`** (per-character Kokoro voice).
- **Persona** — the `{{user}}` card (`name`, `title`/role, `description`, `background`, `image`, `is_default`);
  role+background injected as "About {{user}}". Sidebar has a personas list + editor; one active (default) at a time.
- **ChatMessage** — `role`, `content`, `expression`, `swipes[]`, `swipe_index`, `swipe_expressions[]`.
- **MoodState** — running `valence` (−1…1) + `mood` label (session-level, short-term).
- **Relationship** — persistent per (character, persona): axes `affection/trust/attraction/tension`,
  `emotional_intimacy`, `physical_willingness`, optional `power_dynamic`/`social_acceptance` overrides,
  `milestones`.
- **ChatSession** — `messages`, `memory` (MemoryState), `mood` (MoodState), `generation`
  (GenerationConfig, `max_tokens=220`), `pending_milestone`, optional session-scoped `relationship`
  (scenario mode), `character_id`, `persona_id`.
  (Lorebook entries carry `min_stage` for stage-gating.)

## API endpoints (main.py)

```
GET  /                                     SPA
GET  /api/status                           KoboldCPP {online, version, model}
POST /api/tts ; GET /api/tts/voices        local Kokoro TTS (text→WAV) / 54 voices
# cards
GET/POST /api/cards                        list (trimmed, +has_expressions) / create
POST /api/cards/import                     import PNG/JSON card (multipart)
GET/PUT/DELETE /api/cards/{id}             full card ops
GET  /api/cards/{id}/export?version=2|3    download PNG card
# expressions / ComfyUI
GET  /api/comfyui/status | /checkpoints
GET  /api/expressions/emotions             {core:[8], full:[28]}
POST /api/cards/{id}/expressions           generate set (checkpoint, style, preset core|full, transparent)
GET  /api/cards/{id}/expressions           labels the card has sprites for
GET/POST/DELETE /api/cards/{id}/expression/{emotion}   serve PNG / regen one / delete one
# personas
GET/POST /api/personas ; PUT/DELETE /api/personas/{id}
GET/POST /api/presets ; GET/PUT/DELETE /api/presets/{id}   relationship presets (scenario seeds)
# relationship (per character+persona)
GET  /api/relationship?character_id=&persona_id=     summary (axes, stage, intimacy) + effective power/social + context_overridden
PUT  /api/relationship                     override context {power_dynamic, social_acceptance} (null clears -> card default)
# sessions + chat
GET  /api/sessions ; POST /api/sessions    list / create ({character_id, persona_id?, greeting_index?, preset_id?, mode?})
GET/DELETE /api/sessions/{id} ; DELETE /api/sessions/{id}/messages
PUT  /api/sessions/{id}/swipe              select swipe + reclassify emotion -> {expression}
POST /api/chat                             SSE stream; done carries {expression, mood, valence, relationship, milestones}
```

## How it works

- **Chat:** `build_messages()` assembles system (default/card system prompt + persona + **relationship
  descriptor** + lorebook + memory) → history → `post_history_instructions`; streams via
  `kobold.chat_generate_stream()`. System prompt enforces a **single concrete beat**, no repetition, no
  writing for {{user}}; a stop-sequence on the user's turn is added. After the reply: classify emotion,
  update mood + relationship (skipped on re-rolls), save.
- **Swipes:** alternates per message; each swipe's emotion cached in `swipe_expressions`. `▶` past the end regenerates.
- **Cards:** `card_io.py` reads/writes the PNG `chara`(V2)/`ccv3`(V3) tEXt chunk.
- **Expressions:** `expressions.py` — `classify()` picks a label (from the card's own set) via KoboldCPP;
  `generate_expressions()` makes ONE base portrait (txt2img, fixed seed) then derives each emotion via
  **img2img** (denoise 0.55) so all sprites are the same character; `rembg` cuts a transparent background.
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
  **Escalation:** once intimacy has begun and she's positively engaged, `classify_activity()` reads the act
  depicted and `escalate()` nudges willingness toward the next rung (`ESC_RATE`; social acceptance modulates
  pace, tension blocks it) — so doing acts gradually opens her further.
  **Milestones** (`MILESTONES`/`newly_reached`): crossing a stage/tier fires a one-time toast + a one-time
  beat injected next turn (`session.pending_milestone`), recorded so it never repeats. Lorebook entries can
  be **stage-gated** (`LorebookEntry.min_stage`, filtered by `stage_rank`); context is **overridable per
  relationship** (⚙ on rel-strip → `PUT /api/relationship`).
- **Memory:** older turns fold into a rolling summary once ~24 accumulate.
- **Storage:** plain JSON under `data/`, UUID ids.

## Frontend map (static/js/app.js)

`state = {cards, sessions, persona, session, streaming}`. Chat pane: portrait overlay (mood-driven),
header **mood meter** + **relationship strip** (stage, 4 axis bars, Emotional/Comfort readout), 🖼 Gallery,
🎭 Expressions. Modals: card editor (all fields + portrait upload + **lorebook** + **power/social sliders**
+ Duplicate), expression gallery (grid, per-sprite regen/delete). Key fns: `renderSession→renderMessages→
updatePortrait/updateMood`, `renderRelationship/loadRelationship`, `send` (SSE), `gotoSwipe`, editor
(`buildCardPayload/renderLore`), gallery (`refreshGallery/regenOne`).

## Feature set (all built + verified)

✅ Card V2/V3 import/export · ✅ streaming chat (single-beat, non-repetitive) · ✅ swipes + per-swipe
emotions · ✅ rolling memory · ✅ persona · ✅ character editor (fields, portrait, **lorebook**, Duplicate,
power/social) · ✅ ComfyUI expression sprites (img2img same-character, transparent via rembg, Core-8/Full-28,
**gallery** w/ per-sprite regen/delete) · ✅ live portrait swap · ✅ running **mood/valence** · ✅ empty-greeting
fallback · ✅ delete chats/characters · ✅ **relationship system** (multi-axis, per char+persona, behavior
injection, rel-strip) · ✅ **intimacy/consent** (split willingness, 7-rung ladder, power/social context,
agency-preserving) · ✅ **milestones** (toast + injected beat) · ✅ **stage-gated lorebook** · ✅
per-relationship **context override** (⚙).

## Conventions & gotchas

- Rebuild after code/template/static edits (`./run-podman.sh --rebuild`); `podman exec -i` for heredocs.
- CSS: `[hidden] { display:none !important }` at top of style.css so the HTML `hidden` attr wins over
  element display rules; toggle via `.hidden`.
- Starlette: `TemplateResponse(request, "index.html")` (request first).
- `GET /api/cards` strips `expressions` (adds `has_expressions`); fetch the full card before editing.
- Relationship/mood update on generation only (not re-rolls). Sprites are same-character via img2img from a
  reproducible base (fixed per-card seed) — single-emotion regen stays consistent.
- All characters adult; intimacy code is abstract state (tiers/thresholds) — the local model writes prose.
- New `data/` subdirs MUST be added to the volume mounts in `run-podman.sh` + `compose.yaml`
  (relationships/ was missed once → state wiped on every rebuild until fixed).

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
    the wizard (`openNewChat`).
- **VTuber upgrade** (voiced, animated avatar) — plan in `vtuber-upgrade-plan.md`.
  ✅ **Phase 0 done** — local Kokoro TTS + amplitude lip-sync + barge-in on existing sprites.
  Next: ◻ Phase 1 STT (whisper.cpp) + barge-in loop · ◻ Phase 2 inline `[expr]` tags (hybrid w/ mood/relationship)
  · ◻ Phase 3 VRM (`@pixiv/three-vrm`). Local-first voice throughout.
- **Sprite-pack import** — drop Full-28-named PNGs straight into `card.expressions`.
- Group chats · persona editor modal · embedding memory · TTS · faster inference backend.
- Tuning knobs: `relationship.RATE/WRATE/ESC_RATE/_TIER_THRESHOLDS`, `expressions` img2img denoise, `GenerationConfig`.
```
