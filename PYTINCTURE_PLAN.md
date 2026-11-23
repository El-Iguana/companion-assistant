# Pantheon on pytincture, with Ember's UX — plan

> **Note (2026-11-23):** expression sprites and the ComfyUI dependency were removed from Cantinero.
> A card without a 3D avatar shows its portrait. The sprite parts of this plan are history.

Started 2026-10-26. The goal is a **UX change, not a feature change**: Pantheon keeps everything it
does (cards V2/V3, swipes, mood and expression sprites, the relationship system, both memory
pipelines, personas, presets, Kokoro/Piper TTS, faster-whisper STT, hands-free voice, the VRM avatar,
multi-user login). What changes is the frontend: from 2,561 lines of vanilla JS to a **pytincture +
wapyt** page (Python in the browser), laid out and styled after **Emberfolk**
(perchance.org/character-ai-ember).

## Decisions (2026-10-26, with the user)

| Question | Decision |
|---|---|
| Structure | **In place, phased.** Same repo, image, container, port (`companion`, 7861). The new page is served beside the old one until it reaches parity; then the old one is deleted. |
| Network | **Loopback first.** pytincture only allows login over plain HTTP on a loopback address. LAN access is a later phase behind a TLS proxy. |
| Voice + avatar | **Kept as JavaScript islands** (`vtuber.mjs`, the mic/VAD/lip-sync loop), driven from Python through events. They run at audio and frame rate, which Pyodide's FFI handles poorly. **Revised 2026-11-08 (#5):** measured, a call per capture block costs 0.003 ms (85 ms apart), so hands-free's *decisions* moved to Python (`ui/voicecore.py`); capture, playback and lip-sync stay JS. |
| Ember surfaces | Discover gallery home · suggestion chips · theme swatches · recent-chats rail. Plus what is assumed: Ember's layout, chat view, profile modal, studio page, mobile drawer. |
| Out of scope | Everything Emberfolk gets from Perchance's hosted services: Groups, Feed, Discuss, likes from others, publishing, the guided tour. Pantheon has no community; nothing here invents one. |

## Architecture

```
browser: pantheon.py (Pyodide + wapyt)  ──BFF──▶  services/*_bff.py  ──calls──▶  main.py handlers + services/*
                 │                         ──bff_stream──▶ chat tokens
                 └─ JS islands: vtuber.mjs, voice.js (mic, VAD, TTS playback, lip-sync)
```

- *(Phases 0–6; Phase 7 made it one server — see its results.)* **One process, two servers during the transition.** `service.py` imports `main` (all of
  Pantheon's state and clients) and builds the pytincture app beside it. The old page keeps
  serving from `main.app` on 7861, exactly as today, including LAN access. The new page runs
  as a second uvicorn server in the same process on a **loopback-only** port (7866; the
  container publishes it as `127.0.0.1:7866`). Same process means same module state. It is
  not one app because pytincture's loopback login rejects any non-loopback Host, and its
  stricter headers would apply to the old page too. At switch-over the old server goes and
  7861 serves the new page.
- **No business logic moves.** Pantheon's handlers already take the user's context as a plain
  argument (`async def list_cards(cur: Ctx = Depends(get_ctx))`), so BFF classes call them directly
  with the signed-in user's `Ctx`. The memory, relationship, prompt and expression code is untouched.
- **Login = Pantheon's own accounts.** pytincture's `AUTH_USER_AUTHENTICATOR` hook checks the
  username and password against `data/auth/users` with the existing scrypt code; the session carries
  `user_id` and `role`. Roles and per-user isolation work exactly as today.
- **Chat streaming** uses `@bff_stream` (an async generator): tokens, `expr` beats and the final
  `done` event (mood, relationship, milestones) flow as items. The stream limits (300 s total, 30 s
  idle) are checked against a cold 8k-context prompt in Phase 0.
- **Binary routes stay plain**: portraits and sprites (`/api/cards/{id}/expression/…`), VRM files,
  card PNG export/import, TTS audio, STT upload. The BFF is JSON and capped at 2 MiB; these read the
  pytincture session instead of Pantheon's cookie.
- **The desktop avatar** (`/desktop`, loaded by WebKitGTK in `desktop/pantheon-desktop.py`) is
  **the old SPA** (`index.html` + `app.js`) with its chrome hidden. Its voice loop, TTS queue
  and avatar bridge all live in `app.js`. So it keeps the old page until Phase 6 extracts the
  voice loop into `voice.js`, and Phase 7 then gives `/desktop` a small page built on
  `voice.js` + `vtuber.mjs`. Until then switch-over must not delete `app.js`.

## Ember's UX, mapped onto Pantheon

Measured from Emberfolk's source (`styles.css`, `app.js`) and screenshots.

**Shell.**
- **Left rail, 292 px.** It holds the brand ("Pantheon"); a 3×2 tile nav (**Discover · Create · Chats · Personas · Presets · Settings** — "Chats" replaced a top-level
  Memory screen in Phase 1: memory belongs to a character+persona pair, so it lives in the chat's
  ⋮ menu, and a global screen would have been a new feature); **RECENT CHATS** (avatar, "Name · persona", last-line preview, hover ×); **MY CHARACTERS**; and at the foot: persona switcher, theme swatches, Import / Backup, and the user menu (with Users for admins).
- **Top bar.** Pill search ("Search characters, tags…"), backend status pill (KoboldCPP and ComfyUI), and **+ Create**.
- **View width.** Content up to 1180 px.
- **Mobile, ≤ 900 px.** The rail becomes an off-canvas drawer behind ☰, and in chat the top bar hides.

**Discover (home).**
- A hero line, then a **3:4 portrait card grid** of the user's characters: portrait, name over a gradient, tagline (the card's `creator_notes` or first line of `description`), tags, and chat count.
- Tag pills filter the grid.
- **Clicking a card opens the profile modal**: banner and avatar; **Start chat · Continue · Scenario… · Edit · Export · Delete**; persona text, greeting, "Try saying" chips, and the relationship stage with this persona.

**Chat.**
- A centred 860 px column over the character's portrait, blurred (70 px, 15 % opacity).
- **Header.** Avatar, name, subline `stage · mood · N memories`, and **⋮** with Profile · Memory · Relationship · Expressions · Voice · Export · Clear chat.
- **Mood meter and relationship strip** fold into the subline, with a popover for detail.
- **Expression portrait / VRM.** On wide screens it stays as Pantheon's portrait, docked beside the column. On narrow screens it moves into the header avatar.
- **Bubbles.**
  - Character: left-aligned, with an avatar, a "Name · time" label and a carded bubble (16 px radius, 6 px tail corner).
  - User: right-aligned, no avatar, accent-filled.
  - Narration (`*…*`) renders in serif italic.
  - Day dividers between days.
- **Per message, on hover** (always visible on touch):
  - **‹ 2/3 ›** swipes, where › past the end generates a new take. This is Pantheon's swipe system, restyled.
  - **⋯** menu: Read aloud · Edit · Copy · Regenerate · Delete.
  - Edits happen inline in the bubble.
- **Composer.**
  - Suggestion chips above it.
  - A rounded input bar: ☰ menu on the left (push-to-talk, hands-free, continue, narrate), then the textarea, then send. **Send becomes Stop while streaming**, which is Pantheon's abort.
  - The typing indicator reads "{Name} is thinking".
  - A "↓ Jump to latest" pill appears when scrolled up.

**Studio (create / edit, a full page).**
- **Left:** every card field in sections (Basics, Personality, Scenario & greetings, Lorebook, Appearance & expressions, Voice, Avatar, Advanced).
- **Right:** a sticky phone preview of the profile and greeting.

**Look.**
- Dark only, five colourways from Ember's palette: Bone (default), Ember, Moss, Dusk, Harbor.
- Swapped with CSS variables on wapyt's dark theme.
- Serif for the brand, headings and narration.
- MDI icons, matching Ember's line icons; no emoji. The old UI's 🖼/🎭/📌 buttons become icons.

## Small backend additions the UX needs

These are UI affordances Ember has and Pantheon's API lacks. No new behaviour.

- **Edit one message** and **delete one message** (today there is only "clear"). Editing a user message offers "Save & resend": truncate after it and regenerate.
- **Conversation starters** for the chips: `card.extensions.pantheon.starters[]`, kept inside the card spec so V2/V3 export round-trips. Edited in the Studio. Without them, the chips are hidden.
- **Session preview** for the recent-chats rail: last message, character and persona in `list_sessions`.

## Phases

| # | Phase | Done when |
|---|---|---|
| 0 | **Spike.** pytincture inside Pantheon's process; login through Pantheon accounts; one async BFF calling a `main.py` handler; chat streamed with `@bff_stream` from a cold 8k prompt; `vtuber.mjs` running under pytincture's CSP. | Each risk proven or a fallback chosen. |
| 1 | **Shell.** Layout, rail, top bar, themes, mobile drawer, login page in Ember's style. | New page navigable; old page untouched. |
| 2 | **Discover + profile modal** + start / continue / scenario chat (wizard as a modal). | A chat can be started from the new page. |
| 3 | **Chat view.** Bubbles, streaming with Stop, swipes, ⋯ menu, inline edit, chips, mood and relationship, portrait and expressions. | Day-to-day chatting works without the old page. |
| 4 | **Studio.** All card fields, lorebook, expression generation and gallery, VRM, voice, import and export. | Cards can be made and edited without the old page. |
| 5 | **The rest.** Personas, presets, memory modal, relationship context, settings (voice, memory, thinking), users (admin), help. | Feature parity. |
| 6 | **Voice + avatar islands.** Read aloud, auto-voice, push-to-talk, hands-free, full duplex, VRM canvas. | Voice and avatar parity, re-checked against `MEMORY.md`/`CLAUDE.md`'s measured behaviour. |
| 7 | **Switch-over.** 7861 serves the new page; `/desktop` moves to a small `voice.js` + `vtuber.mjs` page; delete `templates/index.html`, `app.js`, `style.css` and the routes only they used; publish on 127.0.0.1; docs. | One UI. |
| 8 | LAN access — **done 2026-10-30**, plain HTTP: `allow_development_auth_origin` + `LanAsLoopback` in `service.py` (no TLS; an HTTPS proxy was built and dropped as needless for a local product). | Phones work, with login over HTTP. |

Each phase ends with the Playwright smoke test (every screen, zero console errors) and screenshots,
like `ai-stack/stack-ui/tests/smoke.py`.

## Risks to retire in Phase 0

- **Async handlers from BFF.** pytincture supports `async def` exports and async generators. Check that the KoboldCPP and ComfyUI clients (async, module-level) behave when called from them.
- **BFF modules are re-executed per call.** Pantheon's module-level state (embeddings model, TTS engines, caches) must stay in the plain modules the BFFs import, never in the BFF files.
- **Stream limits.** 30 s idle and 300 s total. First-token latency on a full context and long auto-continue replies need measuring; raise `BFF_STREAM_*` if needed.
- **CSP.** `vtuber.mjs` and three.js are vendored and same-origin, which is fine, but WebGL and WASM under pytincture's CSP need a live check. Mic access needs `PYTINCTURE_ALLOW_MICROPHONE=1`.
- **Loopback-only login.** Today Pantheon publishes 7861 on all interfaces. The new page will not log in from another device until Phase 8. The old page keeps LAN access until switch-over.

## Phase 0 results (2026-10-26) — all risks retired

Spike: `service.py` + `appcode/pantheon.py` + `appcode/bff/{library,chat}.py` +
`pantheon_web/auth.py`, run on the host (Python 3.13) against a scratch library
(`PANTHEON_DATA_DIR`) and the live KoboldCPP.

| Risk | Result |
|---|---|
| Login through Pantheon accounts | ✅ `AUTH_USER_AUTHENTICATOR` → `pantheon_web.auth.authenticate` (existing scrypt check); claims `user_id/username/role` reach every BFF call. |
| Async BFF → `main.py` handler | ✅ `await main.list_cards(cur=ctx)` from an `async def` export; default `event-loop` mode keeps Pantheon's async clients on the ASGI loop. Never set `BFF_ASYNC_EXECUTION_MODE=worker-thread` or `BFF_EXECUTION_MODE=isolated-process` — both break that (and isolated mode refuses streams). |
| Chat over `@bff_stream` | ✅ `Chat.send` relays `main.chat`'s SSE unchanged; browser reads `async for ev in Chat().send(...)`. First token 359–542 ms warm; the `done` event keeps `expression/mood/valence/relationship/message_id`. |
| Cold full-context prompt vs the 30 s idle limit | ✅ ~8,170-token prompt: first token **3.0 s**. No `BFF_STREAM_*` change needed. |
| VRM island under pytincture's CSP | ✅ CSP is `script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:`, so the inline import map works as-is. `vtuber.mjs` booted, WebGL rendered the placeholder (66 frames in 3 s). A real `.vrm` load needs the authenticated file route (Phase 6). |
| Python 3.13 | ✅ every requirement installs (rembg, kokoro-onnx, piper-tts, faster-whisper); the image moves from 3.12. |

Found along the way:

- **`allowed_hosts` must be literal IPs** with loopback login: `"localhost"` is refused at
  startup. The new page lives at **`http://127.0.0.1:7866/pantheon/`**, never `localhost`.
- **Async exports have no `_async` companion** — call `await Library().cards()`. Only sync
  exports get one.
- **pytincture's stock login form is `type="email"`**, so a username like `ADMIN` can't be
  typed into it. Phase 1 replaces the login page (Ember-styled) the way Basilisk's
  `login_page.py` does; the spike used an email-shaped test account.
- **`main.py` gained `PANTHEON_DATA_DIR`** so a dev instance never touches the real
  library.

## Phase 1 results (2026-10-26) — the shell

Built: `appcode/pantheon.py` (rail, top bar, hash routing, themes, persona menu, two-tap chat
delete, mobile drawer), `appcode/ui/{common,views}.py`, `appcode/bff/shell.py`,
`appcode/static/pantheon.css`, `pantheon_web/{login_page,media}.py`, `tests/smoke_web.py`.
Screens behind the tiles are placeholders naming their phase, with a link to the classic page.

- **Login page** is pytincture's form rewritten (username field, Ember "Bone" look) by
  `LoginPageMiddleware`; `check_at_startup()` logs if a pytincture upgrade stops emitting the
  anchored markup.
- **Images** come from `/pmedia/card/<id>.png`, `/pmedia/card/<id>/expr/<emotion>.png` and
  `/pmedia/persona/<id>.png`, which read pytincture's session (`request.session["user"]`), never
  a request-supplied path, with mtime ETags.
- **Theme** is per account (`AppSettings.ui_theme`, ignored by the classic page); localStorage
  only caches it so the right colours paint before the account's value arrives.
- **wapyt widgets follow the colourway** by pointing wapyt's `--wapyt-*-dark` tokens at ours,
  so menus, dialogs and toasts added later need no per-widget styling.
- **Container:** Python 3.13, wapyt wheels at the pinned commit, `CMD python3 service.py`,
  7866 published on 127.0.0.1 (`run-podman.sh`, `compose.yaml`); the host-network compose
  variant pins the new page to 127.0.0.1. Verified in a sandboxed run: both pages serve, the
  classic login still works, the new page logs in through the published port and reaches
  KoboldCPP + ComfyUI by name, smoke test 0 console errors.
- `pytincture`'s peer-address loopback check only gates its password-less dev login and API
  clients, so real logins through a published port work — the same thing Basilisk's compose
  install relies on.

## Phase 2 results (2026-10-26) — Discover + profile

Built: `appcode/ui/discover.py`, `appcode/ui/profile.py` (profile modal + Scenario dialog),
`appcode/bff/library.py` (profile, start_chat, delete_card), `pantheon_web/present.py`,
`/pmedia/card/<id>/export.png?version=2|3`, `ui.common.Modal`, `scripts/web-dev.sh`.

- **Discover:** hero (counts, Browse / Create / Surprise me), a "Pick up where you left off"
  rail, tag pills, the 3:4 portrait grid. The top-bar search filters it live (name, tagline,
  tags; every word must match). The card's corner pill is **Continue →** the latest real
  conversation, or **Chat →** for someone new.
- **Profile** at `#/character/<id>` opens over Discover *in place* (the grid and its scroll
  survive; Esc/backdrop/× return to `#/discover`). Continue · Start chat (with the greeting
  picked in ‹ 1/3 ›, macros substituted for the active persona) · Scenario… (only when presets
  exist) · Edit (Studio, Phase 4) · Export (V2/V3) · Delete (confirmed; like the classic page
  it leaves chats/relationship/memories on disk). Roleplay cards show the relationship with the
  active persona (stage, the four axes, emotional level, comfort tier).
- **"Continue" = the latest chat with real conversation**, not the newest file: a chat that is
  only the greeting (started and left) must not hide an older real one. The grid pill falls
  back to a greeting-only chat rather than piling up new empty ones.
- `main.create_session` is unchanged; greeting index, persona and preset + mode pass straight
  through, so scenario chats behave exactly as from the classic wizard.

Known gap: pytincture's post-login redirect drops the URL fragment, so a deep link
(`#/character/…`) opened while signed out lands on Discover after signing in.

## Phase 3 results (2026-10-26) — the chat view

Built: `appcode/ui/chat.py`, `appcode/ui/chats.py` (the Chats tile: every chat, searchable),
`appcode/bff/chat.py` (open, send, swipe, edit, delete, truncate_after, clear, sweep),
`Shell.all_chats`, three endpoints in `main.py` (edit / delete / truncate one message),
`tests/test_messages.py` (9 cases), chat steps in `tests/smoke_web.py` against the live backend.

- **Layout:** Ember's centred 860 px column over the blurred portrait; carded replies with a
  small avatar and "Name · time · feeling", accent-filled messages of yours on the right, day
  dividers, `*actions*` in serif italic. The expression portrait docks beside the column above
  1480 px and follows the running mood and live `[expr:x]` beats; the mood pill shows only for
  cards with sprites, as on the classic page.
- **Per message on hover:** ‹ n/m › on the last reply (› past the end = another take, the
  classic ▶) and one ⋯ menu: Another take · Edit (yours: "Edit & resend") · Copy · Delete.
- **Inline edit**; "Save & resend" = edit + truncate after + the ordinary regenerate path
  (`main.chat` with `regenerate` answers a history that ends with a user message).
- **Streaming** through `@bff_stream`: typing dots, then text with a cursor; Send becomes a
  pulsing **Stop**, which cancels the task — the generated proxy aborts its fetch in a
  `finally`, so KoboldCPP stops as with the classic page.
- **A stopped turn is not saved** — true of the classic page too: `main.chat` saves the
  exchange once, at the end. The new view puts your text back in the box and says so,
  instead of letting it vanish.
- **Idle sweep:** 5 min after a reply, per chat (the app owns the timers, so leaving the screen
  does not cancel them; the classic page's single timer was cancelled by the next chat).
- **Edit/delete/truncate keep memory consistent:** they move `last_summarized_index` and the
  pairing's `swept_sessions` mark (`_shift_swept`), and a truncate below the summary resets it
  as clearing does. Mood and relationship never change on these — as with re-rolls.
- **`overflow: clip`, not `hidden`, on fixed-height containers.** An `overflow:hidden` box can
  still be scrolled by `focus()`: opening the inline editor scrolled the chat header (and the
  whole shell) out of view. `clip` cannot scroll. The smoke test asserts the header stays put.

Until Phase 5/6 the chat header's ⋮ opens the classic page for memory, voice and the
relationship-context editor.

## Phase 4 results (2026-10-26) — the Studio

Built: `appcode/ui/studio.py`, `appcode/bff/studio.py`, `pantheon_web/jobs.py`, plain routes
`POST /pmedia/import`, `POST /pmedia/vrm`, `GET /pmedia/tts-sample.wav`, studio steps in the smoke test.

- **Layout:** Ember's studio — sections (Basics · Personality · Greetings · Relationship ·
  Lorebook · Expressions · 3D avatar · Voice · Advanced) with a jump bar, a sticky phone preview
  (header, greeting with `{{char}}`/`{{user}}` filled in, starter chips) updated as you type, and
  a sticky Save / Create / Create & chat bar.
- **Everything the classic editor did:** all card fields, alternate greetings as a repeater,
  starters (`extensions.pantheon.starters` — they survive a V2 PNG export/import, tested),
  lorebook entries (keys, always, before/after, stage gate; unknown entry fields are kept),
  power/social sliders (hidden for assistant cards), portrait (normalised to PNG ≤ 1024 px),
  sprites, VRM import with the audit (licence flags, rig problems), voice + pace + Test voice,
  the hands-free phrases, system prompt / post-history, creator/version; Import card,
  Duplicate, Delete.
- **Save takes a whitelist** (`studio.EDITABLE`) applied onto the stored card and validated by
  `CharacterCard`, so sprites, VRM assignment, V3 extras and other `extensions` survive.
- **Sprite generation is a background job** (`pantheon_web/jobs.py`, an asyncio task on the
  server loop; the page polls every 2 s). Measured: one sprite took **32 s** — already past the
  30 s BFF limit — so even single regenerations go through it. One job per card; you can keep
  editing or leave and come back (the Studio picks a running job up on load).
- **Fixed a lost-edit race in `main.py`:** generation saved the card copy it loaded minutes
  earlier, undoing any edit made meanwhile; it now merges the sprites onto a fresh read.
- **Drafts:** leaving with unsaved changes stores them in `localStorage`
  (`pantheon.draft.<id|new>`, portrait excluded); the next visit offers Restore / Discard.
- The segmented avatar switch keeps its radios focusable (visually hidden, not `display:none`).
- Correction to Phase 1/2 notes: pytincture's BFF limits are **8 MiB per request, 50 MiB per
  result** — the "2 MiB" was Monguana's own middleware. Uploads still use plain routes because
  a VRM is tens of MB and multipart.

## Phase 5 results (2026-10-26) — personas, presets, memory, settings, users, guide

Built: `appcode/ui/{people,settings,memory}.py`, `appcode/bff/{people,account,memory}.py`,
`GET /pmedia/help.html`, `templates/help.html`, phase-5 steps in the smoke test.

- **Personas** (grid, active badge, chat count; editor with portrait, name, role, description,
  background, make active) and **Presets** (list; editor with the eight relationship sliders,
  optional character/persona pairing, description, opening note).
- **Settings**, one page: account + password change (shared with the classic page), the
  thinking switch, the memory tuning (summarize after, keep recent, summary length, recall
  and its depth, automatic facts and their ceiling) and the voice / hands-free settings
  with the classic explanations. The voice settings are used today by the classic page and
  the desktop avatar; the **microphone picker** needs the browser's device list and comes with
  Phase 6's voice controls.
- **Users** (admin): add, reset password, delete. Admin calls check the role on the **freshly
  read** account (`ctx_for` re-reads it per call), so a demoted or deleted admin loses them at
  once; `main`'s own rules (not yourself, not the last admin, ≥ 4 characters) still apply.
- **Chat ⋮ → Memory…**: "This chat" (pinned facts, story so far) and "Long-term" shaped by the
  card (roleplay: facts + history across chats; assistant: standing instructions, facts,
  where things stand). Long-term rows keep their ids so AUTO facts are not relabelled; it is
  only written back if it was loaded. Scenario chats have no long-term tab, as before.
- **Chat ⋮ → Relationship context…** (roleplay, non-scenario): power / social overrides for
  this relationship only, "Use card default" clears them.
- **The guide** is `templates/help.html`, included by the classic page and served to the new
  one. **Fixed on the classic page:** its help text and the card editor's hint were rendered
  through Jinja, which read `{{char}}`/`{{user}}` as template variables and printed nothing —
  the copy-paste example prompts came out as "You are  in an ongoing story with ." They are in
  `{% raw %}` now; the new page's smoke test checks the macros survive.

## Phase 6 results (2026-10-26) — voice and the 3D avatar

Built: `static/js/voice.js` (the island), `POST /pmedia/tts`, `POST /pmedia/stt`,
`GET /pmedia/card/<id>/avatar.vrm`, chat-view wiring, the Settings microphone picker,
phase-6 steps in the smoke test.

- **`voice.js` is a port, not a refactor.** The classic page and `/desktop` keep `app.js`
  untouched; the voice loop was copied with its measured behaviour and the reasons beside it:
  quoted-dialogue speech for roleplay and sentence-flushed whole replies for assistants,
  `cleanForTTS`, prefetching speech queue, amplitude lip-sync, `[expr:x]` beats applied at
  playback, PCM push-to-talk (WebKitGTK's MediaRecorder emits nothing), RMS VAD with 8-block
  pre-roll, wake word with phrase/continuous modes, sleep phrase, farewell and idle timeout,
  half-duplex by default, full duplex with the **deferred** barge-in, the self-hearing guard,
  and mic choice by id → label → auto AEC.
- **The seam.** Python calls `window.PantheonVoice` (`attach`, `feed`, `expr`, `setStreaming`,
  `readAloud`, `speaker`, `pttStart/Stop`, `handsFree`, `mood`, `mics` …); the island answers
  with `pvoice:*` window events (`send`, `abort`, `expr`, `state`, `persist`, `toast`). It never
  reads the page's state. Settings it changes itself (`hands_free`, `full_duplex` after the
  self-hearing guard trips) go back through `Account.save_settings`.
- **UI.** Header speaker toggle (read replies aloud — a per-browser preference, kept in
  localStorage); composer ear (hands-free: on / listening / awake states) and microphone
  (hold to talk); "Read aloud" in a reply's ⋯ menu; chat ⋮ → Voice settings…. Leaving the chat
  closes the mic (the saved setting stays on and the next chat reopens it): an open mic with
  nothing to send to is only a privacy cost.
- **3D avatar.** `vtuber.mjs` unchanged, imported on first need (only 3D-tier cards pay for
  three.js); the import map is added before that import. Its one canvas moves into the open
  chat's slot. Same corner as the sprite on wide screens, smaller and above the composer below
  1480 px, hidden on phones like the sprite.
- **CSP.** GLTFLoader loads a VRM's embedded textures from `blob:` URLs (Chrome via `fetch`,
  others via `<img>`); pytincture allows `blob:` only for scripts and workers, with no setting,
  so the model rendered untextured with 13 page errors. `service.BlobImagesMiddleware` adds
  `blob:` to `img-src` and `connect-src` — page-minted URLs only, no new origin.
  `allow_microphone=True` sets `Permissions-Policy: microphone=(self)`.
- **Fixed for both pages — speech-to-text was broken in the image.** faster-whisper 1.2.1 calls
  `av.open(..., metadata_errors=…)`, which PyAV 19.0.2 rejects, so every `/api/stt` raised a
  TypeError (500). The deployed image has the same pair, so push-to-talk and hands-free on the
  classic page were dead too. `services/stt.py` now decodes WAV (what both pages send) itself
  and hands faster-whisper float32 samples at 16 kHz; other formats still go through PyAV.
  Measured in the container: the Kokoro line "Hello there. What is your favourite colour?"
  comes back as "Hello there, what is your favorite color?".
- **Tests.** Chromium runs with a fake microphone. The host steps cover the speaker, hands-free
  opening and closing a device, the wake-word gate (phrase, continuous, sleep phrase with a
  contraction), `cleanForTTS`, the avatar rendering in chat and parking on leave, and the mic
  picker. `PANTHEON_VOICE=1` (container only: the models are baked into the image) adds TTS,
  the CSRF check, Read aloud starting playback, and — with `PANTHEON_FAKE_AUDIO=<speech.wav>` —
  push-to-talk end to end, from the fake mic to a sent message.

## Phase 7 results (2026-10-26) — the switch-over

One server, one page. `http://127.0.0.1:7861/` is the pytincture page; the classic page is gone.

- **`service.py` serves one app** on `PANTHEON_PORT` (7861), bound by `PANTHEON_BIND` (image:
  0.0.0.0, published `127.0.0.1:7861:7861`). `PANTHEON_SERVE`, `PANTHEON_WEB_*`,
  `PANTHEON_CLASSIC_*` and port 7866 are gone. `main.app`'s route table is no longer served;
  `main` stays the home of every handler, and its shutdown (closing the KoboldCPP/ComfyUI
  clients) now runs from the served app's lifespan.
- **`localhost` -> `127.0.0.1`** (`LocalhostRedirect`, 307, path and query kept), outermost, so
  bookmarks, `stack companion`, and the desktop shell's old URL land on the address pytincture
  will sign in on. `GET /api/health` (public) is what `stack status` and both compose
  healthchecks probe now.
- **`/desktop` is its own small page** (`pantheon_web/desktop.py` + `static/js/desktop.js` +
  `static/css/desktop.css`) on `voice.js` and `vtuber.mjs`: no Pyodide in the shell's
  WebKitGTK window, and no forked voice code. Five JSON routes behind the session (`/pdesk/state`,
  `open`, `chat` — `main.chat`'s SSE unchanged — `sweep`, `settings`). It keeps the classic
  behaviour: `?card=` → the desktop-mode character → the last chat; continue her latest chat;
  voice on by default; caption of her latest line; the idle memory fold; the four
  `window.__desktop*` hooks the tray uses. Two traps: pytincture's `/{application}` catch-all
  read `/desktop` as an app named "desktop" (its routes are now inserted ahead), and the login's
  `return_to` must carry the query or `?card=` is lost.
- **Deleted:** `templates/index.html`, `templates/help.html` (the guide moved to
  `pantheon_web/assets/help.html`, without its Jinja guards), `static/js/app.js`,
  `static/css/style.css`, `appcode/ui/views.py`, main's `/` and `/desktop` page routes, the cookie
  login (`/api/login`, `/api/logout`, `/api/me`), the Jinja and static-mount setup, and the
  `jinja2` requirement. The avatar harness reads models from `/pmedia/vrm` now.
- **Elsewhere:** `run-podman.sh` (publish + env; host-network fallback binds loopback), both
  compose files, `.env.example`, INSTALL.md (section 9 now says other devices are not supported
  yet — the old proxy recipe would be refused by the host check), README, `desktop/` (URL +
  README), `ai-stack/stack` (the companion probe).
- **Lost until Phase 8:** access from other devices. The classic page answered on every interface
  over plain HTTP; nothing does now.
