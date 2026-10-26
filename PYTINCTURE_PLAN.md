# Pantheon on pytincture, with Ember's UX — plan

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
| Voice + avatar | **Kept as JavaScript islands** (`vtuber.mjs`, the mic/VAD/lip-sync loop), driven from Python through events. They run at audio and frame rate, which Pyodide's FFI handles poorly. |
| Ember surfaces | Discover gallery home · suggestion chips · theme swatches · recent-chats rail. Plus what is assumed: Ember's layout, chat view, profile modal, studio page, mobile drawer. |
| Out of scope | Everything Emberfolk gets from Perchance's hosted services: Groups, Feed, Discuss, likes from others, publishing, the guided tour. Pantheon has no community; nothing here invents one. |

## Architecture

```
browser: pantheon.py (Pyodide + wapyt)  ──BFF──▶  services/*_bff.py  ──calls──▶  main.py handlers + services/*
                 │                         ──bff_stream──▶ chat tokens
                 └─ JS islands: vtuber.mjs, voice.js (mic, VAD, TTS playback, lip-sync)
```

- **One process, two servers during the transition.** `service.py` imports `main` (all of
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
| 8 | *(later)* LAN access behind a TLS proxy. | Phones work, with login over HTTPS. |

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
