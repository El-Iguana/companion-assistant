# AI Companion — Animated Avatar Upgrade Plan (rev. 2026-08b)

Brief for building the VTuber layer *inside the existing companion app*. Fitted to the app's current
architecture (rich emotion/mood/relationship systems, transparent expression sprites) and the
Bazzite / RTX 4070, local-first environment.

> **rev. 2026-08b — scope change.** The deliverable is now a **fully animated 3D avatar**, not a
> voiced sprite. This moves the avatar from "Phase 3, visual upgrade" to **the spine of the plan**,
> splits it into four sub-phases, and adds a **parallel content-pipeline track** that must start
> immediately because it has the longest lead time. Phases 0 and 1 are unchanged and remain shipped.

## Goal
Turn the companion into a **live, voiced, fully animated presence**: a 3D avatar that speaks (TTS),
listens (STT), lip-syncs, gazes, gestures, and emotes continuously in real time — reusing the
emotion/mood/relationship engine already built.

**"Fully animated" means all six channels running together**, not just a face that changes:

| Channel | What it covers | Without it the avatar reads as |
|---|---|---|
| **Lip-sync** | jaw / visemes driven by speech | a mannequin that emits sound |
| **Idle life** | breathing, weight shift, blink, micro-motion | a frozen statue between lines |
| **Secondary motion** | spring-bone hair and cloth physics | a rigid doll |
| **Gaze** | eye + head aim, holds the viewer | vacant, looking through you |
| **Emotive face** | expression presets blended from mood + `[expr]` beats | flat affect |
| **Gesture** | arm/hand/posture clips on speech beats | a talking bust |

The existing sprite path is **retained as the fallback tier**, not the destination — see
*Model tiering* below.

**Two further goals now shape this**: a **desktop mode** (Phase 6) where she lives in the corner of the
screen as a tray-resident presence rather than a browser tab, and an **option on realistic, non-VRM
avatars** (see Component choices) which would also retire the viseme and expression-mapping limits.

## Current state (what we already have — reuse it)
- FastAPI backend, vanilla-JS frontend, KoboldCPP streaming (`chat_generate_stream`, SSE).
- **Emotion system**: post-reply classifier (Core-8 / Full-28), running **mood/valence**, per-swipe emotions.
- **Relationship system**: multi-axis + intimacy/consent, stage, milestones — drives behavior and affect.
- **Transparent expression sprites** (img2img, same-character) with a live-swapping portrait.
- A `<think>`-block stripper in `services/kobold.py` (the pattern we adapt for tag-stripping).
- **Shipped voice stack** — Kokoro + Piper TTS, faster-whisper STT, hands-free VAD loop (Phases 0–1).

### What the avatar is replacing
Today's "avatar" is a single `<img id="portrait">` (`templates/index.html:144`) swapped between static
PNGs, with `scaleY(1 + min(amp*0.5, 0.06))` as the mouth (`static/js/app.js:1006`) — a **6 % vertical
squash**. That is a placeholder, not animation. None of the six channels above exist yet.

## Design principles
1. **Fully local voice.** This app has intimate/adult content — no cloud STT/TTS. Everything on the 4070,
   containerized like `koboldcpp` (Containerfile + `ai-net`).
2. **Hybrid expression, not a rewrite.** Combine three signals instead of replacing what exists:
   - **Resting affect** from the running **mood + relationship** (baseline face/posture).
   - **Momentary beats** from inline **`[expr:x]` tags** the LLM emits (mid-sentence changes).
   - **Fallback** to the existing classifier when the model forgets to tag.
3. **The avatar is the deliverable; sprites are the fallback tier.** *(revised — was "sprite-first,
   VRM later".)* Voice shipped on sprites and that was the right call, but the 3D avatar is no longer an
   optional visual upgrade. It is staged (3a→3d) so each sub-phase is independently useful, and the
   sprite renderer stays live behind a capability check so a card with no `.vrm` still works.
4. **Latency/turn-taking is the core engineering problem**, not a step. Budget for it.
5. **Content lead time is the schedule risk, not code.** *(new)* Rigging a VRM and retargeting an
   animation set is slow, manual, and cannot be parallelised late. Track B starts now, in parallel with
   Phase 2, not after it.
6. **Animation is driven, never scripted.** *(new)* Every channel takes its input from state the app
   already computes — mood, relationship, `[expr]` beats, audio amplitude. No hand-timed cutscenes.

## Target architecture

```
Mic → local STT (whisper.cpp / faster-whisper, streaming) ─┐
                                                           ▼
        KoboldCPP (streaming) ── emits text + inline [expr:x] tags
                                                           │
   ┌────────────────────┬──────────────────────────────────┼──────────────────────┐
   ▼                    ▼                                  ▼                      ▼
strip tags →     parse [expr] events              resting affect from      relationship
sentence-chunk   → queued WITH the speech          mood + relationship      → posture bias
→ local TTS       segment they belong to
   │                    │                                  │                      │
   ▼                    └──────────────┬───────────────────┴──────────────────────┘
audio out                              ▼
   │                        ANIMATION MIXER  (three-vrm)
   ├─ amplitude ──────────→  jaw / viseme track
   │  (phoneme timings   )
   │  (upgrade to visemes)
   └─ playback clock ─────→  expression track   (cross-faded presets)
                             gesture track      (clip state machine)
                             idle track         (breath, sway, blink)  ← always on
                             gaze track         (VRM lookAt)           ← always on
                             spring bones       (hair/cloth physics)   ← always on
```

**Key ordering rule (new):** expression and gesture events fire **at audio playback time**, not at
parse time. `consumeSpeech()` queues and prefetches audio (`static/js/app.js:1049+`), so tokens run
ahead of sound — firing on parse makes the face change seconds before the line is heard. Events ride
attached to their speech-queue item and trigger in `playBuffer`.

## Two tracks

The plan now runs **two tracks in parallel**. Track A is code; Track B is content. Track B gates 3b–3d
and has the longest lead time, so it starts **now**, alongside Phase 2.

| | Track A — runtime (code) | Track B — content (art/rig) |
|---|---|---|
| Now | Phase 2 — expression channel | B1 model sourcing · B2 rig audit |
| Next | 3a renderer · 3b idle life | B3 animation set |
| Then | 3c speech · 3d emotion+gesture | B4 per-character coverage |

## Track A — Phasing
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
- ✅ **Continuous / hands-free listening (done)** — shipped as a fully-local, dependency-free first cut:
  - ✅ **Separate mic control** — a distinct **🎙 toggle** in the header, independent of the hold-to-talk
    🎤 (PTT). PTT stays for one-shot input; the toggle arms/disarms the open-mic loop. Toggle state is
    **persisted** (`AppSettings.hands_free`); re-engages on first user gesture after reload (mic needs a
    gesture).
  - ✅ **Local VAD** — in-browser **RMS energy gate** (`hfTick`, Web Audio `AnalyserNode`): speech start
    opens a `MediaRecorder`, trailing silence (`vad_silence_ms`) ends the utterance → POST to `/api/stt`
    → auto-send. No cloud, no model. *(Silero-VAD ONNX is the accuracy upgrade.)*
  - ✅ **Configurable wake word** — `AppSettings.wake_word` via the **⚙ voice-settings modal**; when set,
    `applyWakeWord()` only sends utterances that start with it (and strips the prefix); blank = pure
    open-mic. **Simplification:** this is **text-matched over the STT transcript**, not a dedicated
    always-on engine. *(openWakeWord — ONNX/CPU — is the upgrade for low-latency always-on detection and
    wake-word-**gated** barge-in.)*
  - ✅ **Ignore-herself — half-duplex** — while she's speaking (`ttsSource` active) or within a 400 ms
    tail, the hands-free mic **does not listen at all** (`hfTick` skips the start gate). This fully
    eliminates self-hearing. **Trade-off (accepted for now):** no *automatic* barge-in in hands-free mode
    — to interrupt her, use **push-to-talk 🎤** (explicit press still cuts TTS + aborts generation).
    On top of `getUserMedia` `echoCancellation`.
    - *Superseded:* an earlier echo-**ducking** attempt (raise the gate by `echo_suppress·ttsLevel`) still
      let her voice through at speaker volume, so we went full half-duplex. `echo_suppress` + `ttsLevel`
      remain in the code/settings but are **dormant** until interruption returns.
  - **Settings store** — new singleton `GET/PUT /api/settings` (`data/settings/app.json`, mounted so it
    survives rebuilds); also holds VAD tuning (mic threshold, end-of-speech silence, echo suppression).
  - ◻ **Interruption (deferred, by request)** — bring back auto barge-in without self-hearing.
    **Promoted out of this phase → see Phase 5.**

> **Deviation from the original plan:** this shipped as **in-process CPU faster-whisper**, not a GPU
> whisper.cpp container on ai-net. Reason: matches how Kokoro TTS actually shipped (in-process,
> CPU, baked), avoids VRAM contention with Kobold+ComfyUI, and `base.en` is fast enough for short clips.
> The GPU whisper.cpp container remains the upgrade path if latency/accuracy needs it — swap by pointing
> `WHISPER_*` / `services/stt.py` at the container.


✅ **Phase 2 — Expression channel (hybrid)** — *done, verified against the live 9B.*
> The model emits well-formed, in-set tags and the events reach the SSE wire. Getting there took two
> prompt fixes: the instruction sat in the **system block**, where the trailing boundary note
> out-weighted it (**1/6** replies tagged, and that label invented) — moved to a trailing system
> message before `boundary_note` it went to **3/6 with none invalid**; and the wording
> `emit a tag [expr:LABEL]` was **copied verbatim** by the model, so the shape is now taught by the
> example line alone with no placeholder anywhere. Tag rate is ~**4/6** and varies run to run; the
> classifier fallback still covers untagged replies. ◻ Out-of-set labels the model reaches for
> (`curious`, `playful`) are dropped silently — a small alias map would recover those beats.
- System-prompt the model to emit inline `[expr:x]` tags.
- **Parse and strip server-side**, in `services/kobold.py`, and emit expression events on the existing
  SSE envelope as `{"expr": "..."}` alongside `{"token": ...}` (`main.py:984`) — no protocol change needed.
  - *This is a sibling of `_strip_think`, not a reuse of it.* `_strip_think` matches two **fixed literals**
    and **discards** the body; `[expr:label]` needs a prefix match, a scan to `]`, a **variable body**, and
    must **emit** rather than drop — so it carries a third state (accumulating a label) beyond the current
    `(buf, in_think)` pair. The split-across-tokens tail trick is what transfers.
- Frontend attaches each `expr` event to the speech-queue item in flight, so it fires **on playback**
  (see the ordering rule above).
- Add a `[` strip rule to `cleanForTTS` (`static/js/app.js:1026`) as belt-and-braces. It currently
  strips code fences, tables, headings, bullets, `*actions*` and backticks but has **no bracket rule** —
  a tag that ever reaches it gets synthesized and spoken aloud as "expr happy".
- Map tags to the existing emotion vocabulary; blend with mood/relationship resting affect; classifier fallback.
- ✅ Shipped: `_strip_expr`/`_flush_expr` + `ExprEvent` (`services/kobold.py`), `{"expr":...}` SSE and
  tag-wins-over-classifier (`main.py`), `pendingExpr`/`applyExpr`/`flushExpr` + playback-time firing
  and the `cleanForTTS` bracket rule (`static/js/app.js`), `expression_note()` (`services/prompt_builder.py`),
  and `expression_labels` on `/api/cards` — the client could not otherwise tell which beats it has a
  sprite for. ◻ Remaining: confirm real-model tagging behaviour.

### Phase 3 — The animated avatar

Split into four sub-phases. Each is independently shippable and each adds one or more of the six
channels. **3a and 3b together are what make the avatar read as alive** — 3b is not polish.

✅ **Phase 3a — Renderer bring-up (done)** *(no animation yet)*
> Shipped: `static/js/vtuber.mjs` module island (three.js + `@pixiv/three-vrm`, **vendored** under
> `static/vendor/` — not a CDN, so the app stays offline-after-build), four-event bridge to the
> classic-script `app.js` (`avatar:card` / `:speak` / `:expr` / `:active`), `CharacterCard.vrm`
> tiering with `GET /api/cards/{id}/avatar.vrm`, WebGL capability check, 30 fps cap, hidden-tab
> pause, and sprite fallback. Render loop, canvas sizing and framing **verified in-browser** via
> `static/avatar-test.html`. ◻ The real `.vrm` load path is still untested — Track B has produced
> no model, so only the opt-in placeholder has been driven.
- Add a **module island**: `static/js/vtuber.mjs` loaded as `type="module"` with an ESM import-map for
  `three` + `@pixiv/three-vrm`. `static/js/app.js` is a single 1589-line classic script and stays that
  way — the island talks to it through a small event bridge, not by refactoring it.
- Load a `.vrm`, render loop, camera framing, transparent or scene background, resize handling.
- Capability check + graceful fallback to the existing `<img id="portrait">` sprite path.
- **Exit criteria:** the model stands on screen in the chat view at a stable frame rate, and a card
  without a `.vrm` still renders sprites exactly as it does today. — **met** for the renderer (harness)
  and the sprite tier (`has_vrm` false → no canvas, no takeover); the VRM branch awaits a model.

✅ **Phase 3b — Idle life (done)** *(the "alive" phase)*
> Shipped: **rest pose out of T** (a VRM carries a rig but no animation, so nothing lowers the arms
> unless we do — this was missing from the original phase description and is why the first cut still
> read as a mannequin), breathing, weight shift on two incommensurable periods, randomised blink with
> double-blinks, `VRMLookAt` gaze that holds the viewer and glances away, and the model's spring bones
> which finally have body motion to react to. Arm-rotation signs are **measured, not derived** — the
> axis derivation gave the opposite; a VRM 1.0 model may want the sign flipped, so `armDeg` stays
> tunable. Tuned by eye on the test model: gain 2, arms 72°.
- **Spring bones** — `VRMSpringBoneManager`, hair and cloth secondary motion. Cheapest single win.
- **Breathing + weight shift** — subtle continuous body motion, never a static T-rest.
- **Blink** — randomised interval with natural double-blinks.
- **Gaze** — `VRMLookAt` aimed at the camera, drifting to break the stare; optional cursor follow.
- **Exit criteria:** with **zero** chat activity, the avatar looks like a person waiting, not a paused
  model. Judge this with the sound off and no messages sent.

✅ **Phase 3c — Speech motion (done, jaw only)**
> Shipped: jaw driven by the Phase 0 RMS amplitude (`aa` 85% + `ih` 15%; pure `aa` reads as a gape),
> asymmetric attack/release so it neither chatters on consonants nor hangs open, a 150 ms timeout
> because silence is the *absence* of events rather than a zero, and prosody head motion keyed to a
> slow energy envelope so the head moves with phrasing not syllables. **Verified with live Kokoro
> audio in a chat**, not just the harness simulator — the jaw tracks real speech.
> ◻ **Visemes remain blocked** — one scalar cannot say which mouth shape belongs to which instant.
- **Jaw from amplitude** — port the working Phase 0 analyser path (`static/js/app.js:996`) onto the
  jaw bone / `aa` blendshape. Works today with no new data.
- **Visemes — blocked on phoneme timing.** *(corrected)* Amplitude yields **one scalar**; it cannot
  produce visemes. Neither backend currently exposes timing — `services/tts.py` has no phoneme,
  duration, or alignment output (the `phoneme` mention at line 75 is a Piper *length-scale* comment).
  Either extend a backend to emit phoneme durations (**Piper is the likelier source**) or keep jaw-only.
  Do not schedule visemes until that lands.
- **Prosody head motion** — small nods/tilts keyed off amplitude envelope and sentence boundaries.
- **Exit criteria:** the mouth tracks speech convincingly at conversational distance; head is not rigid.

✅ **Phase 3d — Emotion and gesture (done)**
> Shipped: expression track consuming the Phase 2 beats (weight carries intensity, so `annoyance`
> and `anger` differ rather than collapsing; cross-faded, rising faster than falling; presets
> **detected per model** so VRM 0.x's missing `surprised` falls back instead of no-op), a
> **procedural** gesture track (nod/tilt/leanIn/recoil/shrug on an envelope, with a cooldown —
> clip retargeting is Track B3 and blocking on it was the worse trade), and posture bias from
> relationship stage fed by a new `avatar:mood` event.
> Also fixed the pose-composition bug this phase exposed: idle **set** bones while gesture/speech/
> posture **added**, and shoulders were reset by neither — posture integrated a delta every frame
> and the arms wandered off. Rest now covers every animated bone, `resetPose()` runs first, and all
> channels are additive.
- **Expression track** — Phase 2 `[expr]` beats + mood/relationship resting affect → VRM presets,
  **cross-faded**, never snapped.
  - *Mapping is lossy and needs a decision.* VRM 1.0 ships ~5 emotive presets
    (happy/angry/sad/relaxed/surprised) against our **Core-8 / Full-28** vocabulary. Options: collapse to
    the 5, or author **custom blendshapes** per model to preserve more — which pushes work into Track B.
    Version the label→preset map so it can grow without breaking saved content.
- **Gesture track** — a small clip library (idle-talk, emphasis, shrug, lean-in, recoil) on a state
  machine with cross-fade blending, triggered on speech beats and `[expr]` changes.
- **Posture bias** — relationship stage shifts resting posture (guarded/closed → open/leaning-in).
- **Exit criteria:** all six channels run together without fighting each other; blends look continuous.
  — **met**, and "without fighting each other" turned out to be the whole difficulty: the mixed
  absolute/additive convention was exactly that fight.

**Phase 4 — Polish**
- Transition smoothing everywhere, idle micro-motion variety, camera framing per scene.
- Failure fallbacks (what the avatar does if STT/TTS/LLM stalls — it must keep breathing).
- Interaction: click/touch response, look-at-cursor tuning.

✅ **Phase 5 — Interruption (done)**
She can now be interrupted mid-sentence in hands-free mode. Barge-in is symmetric with push-to-talk:
speaking over her calls `stopSpeaking()` + `abortChat()`, the same pair the 🎤 button uses.

**The measurement that settled the design.** On the reference box (Logitech C920e webcam mic, line-out
speakers), during her speech:

| Capture path | Echo relative to the noise floor |
|---|---|
| Raw microphone | **+26.3 dB** |
| PipeWire WebRTC AEC source | **−39.2 dB** (below the floor) |

That ~65 dB gap is the whole story, and it explains the *superseded* attempt recorded in Phase 1: ducking
the start gate by `echo_suppress × ttsLevel` could never have worked, because **no level threshold
separates a signal that dominates by 26 dB.** So the fix is a hard switch plus a real canceller, never
another knob — `echo_suppress` is now removed from the UI rather than left as a control that cannot work.

- **`AppSettings.full_duplex`** — lifts the half-duplex gate. Correct *by construction* on headphones
  (no acoustic path); needs an echo canceller over speakers. Off by default: wrong on speakers means she
  hears herself and answers herself.
- **`AppSettings.mic_device_id`** — pick an echo-cancelled capture source; falls back to the browser
  default if the saved device disappears.
- **`scripts/setup-aec.sh`** — loads PipeWire's `module-echo-cancel` (`aec_source` + `aec_sink`), with
  `--default`, `--status`, `--unload`. Runtime only, so a reboot clears it.
- **Both halves are required over speakers**: selecting `aec_source` while playing to the normal sink
  does nothing, because the canceller needs the playback reference to subtract. The in-app help says so.

✅ **Persistence + a visible failure mode (done).** The canceller was runtime-only, so a reboot dropped
hands-free silently back to half-duplex — a confusing way to lose a feature. `setup-aec.sh --persist`
now installs a **PipeWire drop-in** (`~/.config/pipewire/pipewire.conf.d/`) that loads the native
`libpipewire-module-echo-cancel` at start; `--unpersist` removes it. Verified: `aec_source`/`aec_sink`
return after a PipeWire restart with no `pactl` call, and still cancel.

Two guards, because the failure is otherwise silent:
- **At configuration time** — ⚙ Settings warns if interruption is on while the chosen mic is not
  echo-cancelled (fine on headphones, wrong on speakers).
- **At runtime** — a *symptom* check rather than a trust check: what she just said is remembered, and a
  transcript that is >70 % her own recent words while full-duplex is on is discarded. Twice in a row is
  a configuration fault, not luck, so interruption switches itself off with a toast explaining the fix.
  Unit-tested against echoes, partial echoes, genuine questions and genuine interruptions.

◻ Remaining polish: openWakeWord (ONNX/CPU) and Silero-VAD are still the accuracy upgrades for the VAD
and wake word.

### Phase 6 — Desktop mode *(new goal)*

Her as a **resident desktop presence** rather than a browser tab: a tray service that keeps running,
with the avatar occupying the lower-right corner of the desktop and surfacing when addressed.

**This is a repackaging, not a rebuild.** The backend already is the microservice — chat, TTS, STT,
settings and the avatar are all HTTP on `:7861`. Desktop mode adds exactly two things:

1. **`/desktop`** — an avatar-only view: no chat pane, no sidebar, transparent background. Same
   `vtuber.mjs`, same `avatar:*` bridge, same `/api/tts` + `/api/stt` the SPA uses. The chat transcript
   becomes optional rather than the primary surface.
2. **A thin native shell** that hosts that view in a borderless, transparent, always-on-top window and
   owns the tray icon. It holds no logic of its own; the container stays the brain.

#### Platform findings (measured on this box, 2026-08-31)

| | Result |
|---|---|
| Session | Wayland, KDE, `kwin_wayland` with XWayland up on `:0` |
| `zwlr_layer_shell_v1` | **advertised, version 5** |
| `org_kde_plasma_shell` | advertised, version 8 |
| Tray | StatusNotifierItem via `kded6` on the session bus |

Layer-shell being present is the important one, and it was not a safe assumption — it is the clean way
to anchor an overlay to a screen corner on Wayland, choose its layer, and set an **input region** so
clicks pass through to the desktop everywhere except the avatar itself. Without it the fallbacks are
XWayland positioning or asking the user to write a KWin window rule.

#### Shell options

| Option | Layer-shell | Notes |
|---|---|---|
| **GTK4 + WebKitGTK + gtk4-layer-shell** | native | Python/PyGObject, fits the existing stack, no Electron or Rust. **Risk:** WebKitGTK's `getUserMedia` and WebGL need verifying before committing. |
| **Electron / Tauri** | no native support | Would run under XWayland, where positioning and always-on-top work. Heavier; Electron especially. |
| **Ordinary window + KWin rule** | n/a | Zero shell work, but pushes setup onto the user. |

#### Requirements the browser version never had

- **Click-through** — the avatar must not eat clicks meant for the desktop behind it. Input region via
  layer-shell, or `setIgnoreMouseEvents` with forwarding on Electron/Tauri.
- **Summoning** — a global hotkey (KDE can bind one to a D-Bus call) and/or the wake word already built
  in Phase 1b. "Pop up on a decided interaction" is the wake-word path.
- **Idle behaviour** — she is on screen all the time now, so the render loop's cost and the
  hidden-tab pause both need rethinking for a window that is always visible but rarely looked at.
- **Multi-monitor** — which output, and what happens when it changes.
- **Autostart** — systemd user unit, matching how `stack` already installs one.

#### Dependency worth stating plainly

**Phase 5 (interruption) was the gate here, and it is now done.** Half-duplex would have been the
defining flaw in a desktop assistant that talks at you and cannot be cut off. Desktop mode inherits the
full-duplex path — and a headset, which is the likely desktop setup anyway, needs no echo canceller at all.

## Track B — Content pipeline *(new; starts now, in parallel with Phase 2)*

**This is the schedule risk.** Code for 3a–3d is bounded and well-trodden. Rigging a VRM and
retargeting an animation set is manual, slow, and cannot be compressed by starting late.

> **B1 is now the single blocker for 3b.** Everything in Track A buildable without a model is built.
> Sources: **Booth.pm** (largest marketplace), **VRoid Hub** (free, per-model download gating),
> **VRoid Studio under Proton** (no native Linux build, but this is a Bazzite box — worth trying
> before Blender), or **Blender + VRM Add-on** (native, full control, needed anyway for hero-tier
> custom blendshapes). Check each model's **VRM meta license flags** — they carry machine-readable
> allow/deny for sexual content, violence, commercial use and redistribution, and many Booth /
> VRoid Hub models are marked sexual-use-prohibited by their authors.

**B1 — Model sourcing.** VRoid Studio has **no native Linux build** → use the **VRM Add-on for Blender**
(native, and fits the game-dev plan) or pull from **VRoidHub**. Produce one reference `.vrm` early, even
a rough one, so 3a is unblocked.

**B2 — Rig audit.** Per model, verify: complete humanoid bone map, blendshape inventory against the
label→preset map, spring-bone setup on hair/cloth, and sane scale/origin. A model that loads but lacks
spring bones or presets silently kills channels — audit before building on it.

**B3 — Animation set.** Idle set (breath, weight shift, blink) + gesture clips (idle-talk, emphasis,
shrug, lean-in, recoil). **Mixamo retarget** (fast, generic, needs a humanoid-bone mapping pass) or
**hand-authored in Blender** (slower, on-model). Recommend Mixamo for the first pass to prove the state
machine, hand-authored later for the hero character.

**B4 — Per-character coverage → the tiering decision below.**

### Model tiering — the biggest open question in this plan

The app is **card-based and multi-character**, and sprites are **generated automatically per card**
(`services/expressions.py:201`, ComfyUI img2img + rembg). That pipeline scales to unlimited characters
at zero manual cost.

**VRM avatars do not scale that way.** Each is hand-authored content: source or model, rig, audit,
retarget, per-model blendshapes. There is no img2img equivalent. A "fully animated avatar for *the*
character" is achievable; a fully animated avatar for *every card a user makes* is not, without a
generative 3D pipeline that does not exist here.

**✅ DECIDED (2026-08-29) — three tiers, chosen per card:**

| Tier | Content cost | What the card gets |
|---|---|---|
| **Hero** | hand-authored VRM + custom blendshapes + authored clips | all six channels |
| **Standard** | stock/VRoidHub VRM + retargeted Mixamo clips | all six, generic gestures |
| **Sprite** | today's automatic ComfyUI pipeline (**unchanged**) | expression swap + amplitude mouth |

Add an optional `CharacterCard.vrm` (path/asset ref, null = sprite tier). The renderer picks the tier
from that field. Sprite tier is the default and stays exactly as it works today, so nothing regresses
and the multi-character system keeps its zero-cost scaling.

**Decided**: the avatar is a per-card feature. `CharacterCard.vrm` (null = sprite tier) is the
switch; sprite tier stays the default so nothing regresses. 3a is unblocked on this question and now
waits only on a loadable `.vrm` from Track B.

## Component choices (local-first)
- **TTS**: **Kokoro** shipped (fast, 54 voices) and **Piper** shipped as a second backend (voice ids
  `piper:<name>`, one ONNX per voice mounted ro from `../jarvis`; first voice `jarvis-medium`, en_GB RP).
  Same `/api/tts` path, so lip-sync and streaming are unchanged. **XTTS-v2 remains open** for
  voice-cloning. Piper is also the likeliest source of **phoneme timings** for visemes (Phase 3c).
- **STT**: shipped as in-process **faster-whisper** (`base.en`, CPU int8). GPU whisper.cpp remains the
  upgrade path.
- **VRM in browser**: `three.js` + `@pixiv/three-vrm`, ESM import-map, loaded as a **module island**
  beside the classic-script SPA.
- **Animation sourcing**: **Mixamo** retarget for the generic set; **Blender + VRM Add-on** for
  hand-authored hero clips.
- **VRM sourcing on Linux**: **VRM Add-on for Blender** (native) or **VRoidHub**. VRoid Studio has no
  Linux build (worth trying under Proton on this box before falling back to Blender).
- ◻ **OPTION — realistic (non-VRM) avatars.** VRM is an anime-first format: MToon toon shading, and at
  most **5** emotive presets (4 on the 0.x models we have). The realistic ecosystems ship plain glTF with
  **ARKit blendshapes — 52 of them**, including real mouth shapes. That is not just a look: it would
  retire two limits this plan currently carries, the lossy 28→5 expression collapse and the
  viseme block, since ARKit shapes are what viseme drivers target.
  | Source | Style | Notes |
  |---|---|---|
  | **Ready Player Me** | stylized-realistic | glTF+ARKit, free, embeddable creator; best effort/reward |
  | **Avaturn** | near-photoreal from a selfie | GLB+ARKit, commercial SDK |
  | **Character Creator 4** | genuinely realistic | GLB/FBX, paid desktop authoring, **no service dependency** |
  | ~~MetaHuman~~ | photoreal | Unreal only — not a browser format, ruled out |
  - **Cost:** the 3b/3c/3d channels are VRM-coupled (`humanoid.getNormalizedBoneNode`,
    `expressionManager`, `VRMLookAt`, VRM spring bones). The refactor is bounded though — everything
    already funnels through seven bone handles and `setValue`, so a **rig adapter** of about two methods
    (`bone(name)`, `setExpression(name, weight)`) covers it, with a VRM impl and a glTF/ARKit impl. The
    channels themselves would not change. **Spring bones are the real loss**: VRM declares them in the
    file, a plain glTF has none.
  - **Counter-argument, taken seriously:** near-photoreal amplifies every animation flaw. At 30 fps with
    procedural idle and a five-clip gesture library, a photoreal face will read *worse* than the current
    anime model, not better. Stylized-realistic is the safer target; going photoreal makes
    **NVIDIA Audio2Face / ACE** (local, runs on the 4070, generates blendshape animation from audio)
    effectively mandatory rather than optional.
  - **Cheap way to decide:** export one RPM or CC4 avatar as GLB and load it in the harness. It will
    fail for want of VRM extensions, and the failure names exactly what the adapter needs — while
    showing the realism against our real lighting and bust framing.
  - Hosted creators (RPM, Avaturn) carry usage terms worth reading for this app's content; CC4 and
    Blender avoid that entirely and fit the local-first principle better.

## Performance budget *(new)*
One RTX 4070, 12 GB, already shared by KoboldCPP and ComfyUI. The avatar adds a **continuous** WebGL
render loop — unlike sprites, it never idles.
- Renderer runs **while Kobold is generating**; expect contention on both GPU and frame pacing.
- Budget a frame cap (30 fps is plenty for a bust) and pause/throttle the loop when the tab is hidden.
- Spring-bone and blendshape work is CPU-side per frame — watch it against the STT/TTS ONNX threads.
- **Measure before 3d.** If the budget does not hold, the lever is the gesture track, not idle life.

## Integration points (reuse, don't rebuild)
- `kobold.chat_generate_stream` for the token stream; add the tag parser alongside the `<think>` stripper.
- Emotion set (`expressions.CORE/FULL_EMOTIONS`) as the tag vocabulary; classifier as fallback.
- `session.mood` + `relationship.summary()` for resting affect and posture bias.
- Existing sprites remain the **sprite tier**, not a temporary stand-in — keep the SPA, add the avatar
  as a module island behind a per-card capability check.
- Voice I/O is done and reusable as-is: the amplitude analyser (`static/js/app.js:996`) feeds the jaw
  track unchanged.

## Expression tag protocol (keep simple + versioned)
- `[expr:label]` inline, label ∈ the emotion set. Parser is stream-safe (handles tags split across tokens).
- Stripped server-side; delivered as `{"expr": ...}` SSE events.
- Version the label→VRM-preset map so it can grow without breaking saved content.

## Open decisions
*(Resolved and removed: TTS engine — Kokoro **and** Piper shipped, only XTTS open. STT trigger — PTT
**and** hands-free VAD both shipped.)*

- **Emotion mapping** — collapse Core-8/Full-28 to the ~5 VRM presets, or author custom blendshapes?
  (cost lands in Track B)
- **Framing** — bust or full body? Determines whether the gesture track is worth building at all.
  Desktop mode leans this way: a corner overlay is a bust, and a full body would be a tall thin window.
- **Realistic vs VRM avatars** — see the option under Component choices. Decide before investing in
  hero-tier VRM content, since the rig adapter and the content pipeline both fork on it.
- **Desktop shell** — GTK4 + WebKitGTK (native layer-shell, needs a `getUserMedia`/WebGL check) vs
  Electron/Tauri under XWayland vs an ordinary window plus a KWin rule.
- **Desktop summoning** — global hotkey, wake word, or both.
- In-app avatar view vs separate `/vtuber` page (three.js is heavy for the no-build SPA).
- VRM sourcing: Blender VRM addon vs VRoidHub.
- Animation set: Mixamo retarget vs hand-authored.
- Phase-0 sprite mouth: swap vs overlay/mask — still relevant for the **sprite tier**.
- Visemes: extend Piper for phoneme timings, or ship jaw-only?

## Notes for Claude Code
- Reuse the streaming/backend + emotion/mood/relationship systems — only the tag layer, the avatar
  renderer, and the animation mixer are new.
- Local voice is a hard requirement (privacy). Watch VRAM: Kobold + (ComfyUI) + TTS/STT + **a continuous
  WebGL loop** on one 4070.
- **Do not skip Phase 3b.** Idle life is what separates "a 3D model is displayed" from "someone is there."
- **Do not refactor `static/js/app.js` into modules** to accommodate three.js. Add a module island and
  bridge it.
- Start Track B (a loadable `.vrm`) before writing 3a — the renderer is untestable without one.
