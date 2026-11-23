# The Menu (skills) — plan

> **Vocabulary.** In Cantinero's UI and help, skills are **the Menu**: a skill is *on the menu*,
> and enabling one for a bartender is *adding it to the menu*. Assistant cards are **the staff**.
> This plan keeps the engineering word "skill" for the code (`CharacterCard.skills`,
> `services/skills/`); anything a user reads says "the Menu". See CLAUDE.md, Vocabulary.
>
> **Since the split (2026-11-07)** this repo has no roleplay mode, relationship system or boundary
> note. Mentions of them below are history from El-Iguana/companion; the constraints that cited
> them now apply to the trailing system note (`ASSISTANT_FORMAT_NOTE` + `focus_note`) instead.

Give the staff (assistant characters) real capabilities: read the host system, and run the media.
A skill is a set of tool definitions plus the code behind them, attached to a character card.

First two on the menu: **System Info** (host date/time + machine status) and **Media** (the Orpheus
media server — find music and video by voice and play them on any Orpheus player; the DJ). *Media was the "DJ"
skill, built on Navidrome, until 2026-09-15 — see Skill 2.*

> **Status 2026-11-23 — Phase 0b built, DJ read-only.** Built against
> `AGENTIC_SKILLS_MEDIA_ARCHITECTURE.md` (the general architecture brief), reconciled with this plan:
> - **Runtime in-process** (`services/skills/__init__.py`), not a separate service: registry,
>   strict argument validation (unknown keys refused), 20 s timeout, typed errors
>   (`UNKNOWN_TOOL`, `BAD_ARGUMENTS`, `TIMEOUT`, `TOOL_FAILED`, the skill's own), operation ids and
>   a per-user audit log (`data/u/<uid>/skills/audit.jsonl`). MCP adapters: later.
> - **Tool loop** in `main.chat()`: `kobold.chat_generate_stream` yields a `ToolCallsEvent`; up to
>   3 tool rounds; results go back as OpenAI `tool` messages *before* the trailing note, which
>   stays last; a `skill` SSE event shows "🎵 Searching the library…" on the page and desktop;
>   schema tokens come off the context window; a tool turn keeps its clarifying question.
> - **`CharacterCard.skills`** and the Studio's **Menu** section.
> - **The DJ** (`services/skills/media.py`, client `orpheus_media.py`) against the live Orpheus on
>   Homelab: `media_connect` (LAN discovery via `ORPHEUS_MEDIA_SCAN_CIDR`, or `ORPHEUS_MEDIA_URL`;
>   pairing as a controller, approved on the server), then `media_find` + `media_status`.
> - **Playback (2026-11-23, on Orpheus Phase 5):** `media_play` takes what the user said and plays
>   it when the match is clear (one tool pass), or returns up to three choices, kept in
>   `media/pending.json` for 5 min and named in the next turn's note so `media_play {id}` picks one.
>   `media_control` (pause/resume/stop/next/previous/seek/volume). Playlists resolve too. The target
>   player (RESIDENT_PLAN.md R-6): the one named, else a resident's own player for a request from
>   that resident, else the default player (Settings → Music), else the only one online; several →
>   `WHICH_PLAYER`; a chosen player that's closed → `PLAYER_OFFLINE`, never another room. (#36's
>   "desktop shell launches the player" was removed in R1: the resident's player is always on.)
>   **Fast path:**
>   `media.fast_command` recognises whole plain utterances ("pause the music", "next song", "turn it
>   down", "volume 40") and `main.chat` does them without the model ("Paused."), for a card with
>   the DJ. **Ducking:** voice.js posts `/pmedia/music/duck` while she speaks or is awake after
>   the wake word (unduck after 1.5 s quiet); the server sends `duck 25`/`unduck` to that resident's
>   player, or the default one from the web page. Tests: `tests/test_dj_playback.py`.
> - **Model re-verified 2026-11-23:** Cydonia-24B (Mistral-based) made 21/21 correct streaming tool
>   calls (right tool, valid arguments, none for non-music questions), and KoboldCPP accepts the
>   `tool` result messages for the second pass. **Latency is the cost: ~12–14 s per pass, ~25 s for
>   a tool turn**, which is why the deterministic fast path (pause/skip/volume without the model,
>   from the brief) is planned with playback, and the tool list stays small.
> - From the brief, deliberately not taken: a new media server or API (Orpheus is it), playback in
>   Cantinero's page (the Orpheus player window stays the target), PostgreSQL, 11 dotted tool names
>   (OpenAI names can't contain dots; tokens are per tool), queue edits and room transfer (Orpheus
>   Phase 5b and later).

Status before 2026-11-23: **mostly not built.** The date/time half of System Info shipped early (2026-08-17) without
needing any of the skill machinery — see below, it changed what the skill is for. Everything marked
✅ VERIFIED was measured against the live stack on 2026-08-17; everything else is design.

## Why this is feasible now — verified, not assumed

The blocking question was how a 9B local model invokes anything at all. Tested live against
`koboldcpp` / `Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q4_K_M`:

| Test | Result |
|---|---|
| OpenAI `tools` + `tool_choice` on `/v1/chat/completions` | ✅ returns a proper `tool_calls` message, `finish_reason: "tool_calls"` |
| Tool calls over **streaming SSE** (the app's actual path) | ✅ `tool_calls` arrive as deltas, assembled across chunks |
| Argument quality | ✅ asked "What time is it?", it inferred `{"tz": "America/New_York"}` from context |
| No spurious calls when no tool is needed | ✅ "tell me a joke" → normal in-character text, no tool fired |
| Character voice with tools present | ✅ stayed fully in character alongside a roleplay/relationship system prompt |

**So: native tool calling, not `[skill:…]` tag scraping.** This is the single most important decision
here. The vtuber plan's `[expr:x]` tag approach exists because expressions must interleave *mid-
sentence* with prose; skills don't — they're a discrete request/response, which is exactly what the
tool-call protocol is for. Parsing skills out of prose would mean re-solving streaming, escaping, and
malformed-argument handling that the API already handles.

## Settled design decisions

- **Skills are opt-in per character**, via a `skills: List[str]` field on `CharacterCard`. Not global.
  A roleplay character in a medieval setting should not be able to query disk usage, and every
  enabled skill costs prompt tokens on every turn (below).
- **Assistant-mode first.** `chat_mode == "assistant"` (the Jarvis case) is the natural home. Nothing
  blocks a roleplay card from enabling one, but the defaults and the UI should steer to assistant.
- **Tool schemas cost context and must be budgeted.** ✅ Measured `prompt_tokens` deltas:
  | Payload | Added tokens (every turn) |
  |---|---|
  | System Info (1 tool) | **+51** |
  | DJ (5 tools; the Media skill keeps the same count) | **+218** |
  | Both (6 tools) | **+256** |

  For scale, the relationship descriptor is ~149 tok and the boundary note ~84. So enabling both
  skills costs about as much as the entire relationship system. This lands directly on the problem in
  `PROMPT_BUDGET.md` — the prompt has no total budget, overflow truncates from the front silently, and
  a content-rich card at default settings already sits ~0.4% inside the window. **Skills must be
  counted in that budget before they ship**, and tool schemas belong near the top of the eviction
  priority list (they're regenerable; the persona is not).
- **Prompt injection beats a tool call for small, always-relevant facts.** Learned by shipping:
  `get_time` was designed as a tool and turned out not to need one — the answer is ~25 tokens and
  always applicable, so it just goes in the prompt, with no round-trip and no two-pass latency.
  Reserve tools for what is expensive, large, parameterized, or has side effects. This trims the
  measured cost below: System Info's `+51` was `get_time`'s schema and no longer applies.
- **No arbitrary execution, ever.** Fixed, parameterized tools only — never a `run_command(cmd)` or
  `read_file(path)` escape hatch. See Security below; this is not negotiable given the app's exposure.
- **Local-first, like everything else in this stack.** No cloud services for either skill.
- **A failed skill degrades to conversation.** If a tool errors, times out, or the NAS is down, she
  gets a plain error string back and says so in character. A skill failure must never break the chat
  turn — same contract as recall in `MEMORY_PLAN.md` Phase 4 ("degrades to nothing").

## Chat-flow change (the shared machinery)

Today `chat()` builds messages → streams → post-processes (emotion, mood, relationship, memory).
Tool calling makes a turn potentially **two-pass**:

```
build_messages(+ tools)  →  stream
   ├─ finish_reason "stop"       → today's path, unchanged
   └─ finish_reason "tool_calls" → execute tool(s) locally
                                 → append assistant(tool_calls) + tool(result) messages
                                 → stream again → final in-character reply
```

Constraints this must respect, all of which already have hard-won reasons:

- **The trailing system note stays last.** It is deliberately appended after
  `post_history_instructions`, and `kobold.chat_generate_stream` re-appends the trailing system turn
  on each auto-continue round. The second pass is another round — it must re-append too, or the
  reply-format rules silently drop out of exactly the turns that ran a skill.
- **Only the final visible reply** gets classified for emotion / mood / memory. Tool
  round-trips are not conversation and must not move the relationship axes or land in memory.
- **The frontend must not render the intermediate pass.** The SSE stream should emit a distinct event
  (e.g. `skill`) so the UI can show "🎵 searching the library…" instead of leaking a tool-call blob
  into the message bubble.
- **Latency.** Two passes ≈ double time-to-first-token on skill turns. With TTS this is audible.
  Consider speaking a short acknowledgement from the first pass, or a UI affordance.

## Skill 1 — System Info

### The premise is wrong in a useful way ✅ VERIFIED

The brief says the skill should "read the system date and time from the host system, and not the
container." **The container's clock is not wrong.** Measured simultaneously:

```
host:      Mon Aug 17 05:27:27 PM EDT 2026   (America/New_York, -0400)
container: Mon Aug 17 21:27:27 UTC 2026
```

Same instant — 21:27:27 UTC both sides. Containers share the host kernel clock; there is nothing to
fetch. The only difference is **timezone**: the container's `/etc/localtime` → `Etc/UTC` and `TZ` is
unset, so `datetime.now()` returns UTC and she'd say "it's 9pm" at 5pm.

**Fix: configuration, not architecture.** ✅ **DONE.** `run-podman.sh` derives the zone from the host
(`timedatectl` → `/etc/timezone` → `UTC`, overridable by exporting `TZ`) and passes `-e TZ=`;
`compose.yaml` uses `TZ=${TZ:-America/New_York}` since compose can't shell out. tzdata is already in
the image, so the zone name resolves. Verified: container shell and Python both moved from
`21:27 UTC` to `17:39 EDT`, matching the host. This fixed local time for *everything* in the app —
`created_at` fields, log lines — not just the skill.

### …but the TZ fix alone did not make her know the time ✅ VERIFIED

Immediately after the TZ fix, asked "what time is it?", Jarvis answered **"Monday, October 24. The
current time is 9:15 AM"** — pure invention, against a real time of Monday, August 17 2026, 5:45 PM.

Cause: **nothing ever put the date in the prompt.** Confirmed by assembling a real prompt through
`build_messages` and searching it — no date, no time, anywhere. The container clock being right is
invisible to the model; the two problems were independent and the second was the one that mattered.

**Shipped fix (no tool calling required):** `build_messages` appends a
`Current date and time: …` system message. Two decisions:

- **Gated on `chat_mode == "assistant"`.** Verified: Jarvis (assistant) gets it, Errata (roleplay)
  does not. A roleplay character announcing the real-world date breaks the scene.
- **Placed after the history, not in the system block.** The string changes every minute and
  KoboldCPP reuses its cached prefix only up to the first changed token — in the system block that
  would re-process the entire history every turn. Trailing also puts it where small models weight it
  most, the same finding behind `boundary_note`'s placement. It goes immediately *before*
  `boundary_note`, which stays last (verified for both cards).

Result: "Monday, August 17 5:51 PM" against a host truth of 5:51 PM EDT.

**What this changes about the skill.** `get_time` as a *tool* is now unnecessary — the answer is
always in the prompt, at ~25 tokens/turn, with no round-trip and no two-pass latency. The lesson
generalizes: **a fact that's small, always relevant, and cheap to render belongs in the prompt, not
behind a tool call.** Tools are for things that are expensive, large, parameterized, or have side
effects. Apply this test before adding any future skill.

### What genuinely needs host access

These are real host facts a container cannot see, and they're the actual justification for the skill:

- uptime / load average
- disk free on `/var/home` (the container sees its own filesystem)
- GPU: VRAM used/free, temperature — the 4070 is shared between KoboldCPP and ComfyUI, so "can I
  generate images right now?" is a genuinely useful question to ask her
- which stack services are up (`koboldcpp`, `comfyui`, `companion`, …)

### Design

`services/skills/system_info.py`, exposing:

- ~~`get_time(tz?)`~~ — **dropped.** Shipped as prompt injection instead (above). Only worth
  reviving as a tool if arbitrary-timezone questions ("what time is it in Tokyo?") turn out to
  matter; the model already produced a correct IANA `tz` argument in testing, so it would work.
- `get_system_status(what?)` → uptime / disk / gpu / services. **This is now the whole skill**, and
  it's the part that genuinely can't be answered from the prompt: the values change, they're too
  large to inject every turn, and they need host access.

For the host facts, in preference order:
1. **Read-only bind mounts** of `/proc/uptime`, `/proc/loadavg`, and a host-written status file.
   Simplest, no new service, no network surface.
2. **A tiny host-side status endpoint** on `ai-net` (the `stack` CLI is the natural owner — it already
   knows what should be running). Needed for `nvidia-smi` and `podman ps`, which a bind mount can't
   provide. Read-only, no parameters that reach a shell.

Option 2 is more capable and is where GPU/service status has to live; it's also a new daemon. **Start
with option 1 plus the TZ fix**, and only add the endpoint when GPU/service status is actually wanted.

## Skill 2 — Media (Orpheus)

> **Plan changed 2026-09-15.** This skill was "DJ", built on Navidrome. It now targets **Orpheus**, our
> own self-hosted media server plus a Flutter player. System design: `../orpheus/docs/architecture.md`;
> server Phase 1 spec: `../orpheus/docs/phase1-server-spec.md`. The network survey below is kept because it's still
> true; the Navidrome plan after it is superseded.

### Plan of record, 2026-11-13

Supersedes the details below where they disagree. Orpheus is now deployed on **Homelab**; Cantinero and
the speakers are on **predator**. The server side is `../orpheus/docs/phase5-remote-control-spec.md`.

**The flow.** "Play Sigur Rós" → `media_find` → Orpheus `resolve` (albums, artists, tracks, *and the
user's playlists*) → if the match is clear she plays it at once; if `needs_disambiguation` is set she
offers the top two or three and plays the one chosen → `media_play` → if no Orpheus player is online on
predator, the desktop shell opens it and the skill waits for it to connect → Orpheus sends `play` to the
player → music. **Confirm only when ambiguous** (user decision, 2026-11-13).

**Decisions:**

- **Player:** Orpheus's browser player at `http://<homelab>:8096/player/`, in the Chromium app window
  that `../orpheus/player/desktop/install.sh` installs as `orpheus-player.desktop` — not the Flutter/Flatpak
  player below, which was superseded on 2026-10-09. Installed with the autoplay switch (Orpheus Phase 5
  AD-7), because nobody clicks a window the assistant opened.
- **Launching it:** the desktop shell (`desktop/pantheon-desktop.py`) is the only process on the host, so
  it gains a **page → shell channel** (a WebKit script-message handler) with one fixed message,
  `open_orpheus_player`, which runs `gtk-launch orpheus-player`. Fixed and parameterless: the page can ask
  for that one launch, never a command line. The skill's server side can't reach the desktop, so the
  launch is requested through the page: `media_play` returns `player_offline`, the turn's `skill` SSE event
  carries `{"launch": "orpheus_player"}`, the page relays it to the shell, and the skill retries `play` once
  the player shows online (poll `GET /api/v1/devices`, ~10 s). Without the desktop shell (a plain browser
  tab), she says to open the player.
- **Profile:** Cantinero's controller device is **bound to the user's Orpheus profile** at setup
  (`PUT /api/v1/devices/{id}/profile`), so "my road-trip playlist" resolves.
- **Pending choice survives the turn.** Tool results aren't kept in history, so when she asks "the album
  or the live recording?", the skill stores the candidates per user (`data/u/<uid>/media/pending.json`,
  expires after 5 min) and the next turn's prompt carries a one-line note ("Waiting on: 1) Ágætis
  byrjun — album, 2) …"). `strip_initiative_question` must not strip that question: skip it when a
  `skill` event fired this turn.
- **Names:** `services/orpheus.py` and `ORPHEUS_URL` already mean the **Orpheus TTS voice**. The media
  skill is `services/skills/media.py` with its client in `services/skills/orpheus_media.py`, configured by
  `ORPHEUS_MEDIA_URL` (the saved server, optional) and `ORPHEUS_MEDIA_SCAN_CIDR`.
- **Token storage:** its own file, `data/u/<uid>/media/orpheus.json` (`server_id`, URL, device id, token,
  default player name) — **not** `AppSettings`, which `Account.settings()` sends to the browser whole.
- **Discovery:** the server is on another host now, so the subnet scan crosses a real LAN; the
  single-host `host.containers.internal` workaround in the Orpheus docs no longer applies.

**Build order** (each step usable on its own):

1. **Orpheus Phase 5** — control connection, command relay, playlists in `resolve`, the player obeying,
   autoplay. Spec: `../orpheus/docs/phase5-remote-control-spec.md`.
2. **Phase 0b scaffolding here** — `kobold.chat_generate_stream` reads `delta.tool_calls` and stops on
   `finish_reason == "tool_calls"` (today it treats that as a natural stop and returns an empty reply);
   `kobold_params` carries `tools`; the two-pass loop in `main.chat()`; `CharacterCard.skills`; the
   registry; the `skill` SSE event in both consumers (`appcode/ui/chat.py`, `static/js/desktop.js`); tool
   schemas counted in `fit_history`.
3. **Connect** — discovery, pairing as a `controller` (settings button first; the `media_connect` tool
   after), profile binding, token file.
4. **Find and play** — `media_find`, `media_play`, `media_control`, `media_status`, the pending choice.
5. **Launch** — the desktop shell's page → shell channel and the retry.
6. **Coexistence** — `duck`/`unduck` around TTS through a server-side route (the browser knows when she
   speaks, the token must stay server-side), then vocabulary biasing for STT (`stt.transcribe` gains an
   `initial_prompt`).

### What's actually on the network ✅ VERIFIED, and it isn't what we assumed

Discovery (mDNS + port probe, 2026-08-17):

- **NAS: `DH2300-D030` @ `192.168.1.219`** — a **UGREEN NASync DH2300 running UGOS Pro**
  (`http://192.168.1.219:9999/` → `/desktop/?os=ugospro`). SMB on **445**.
- **No music API is listening.** Jellyfin (8096/8920), Navidrome (4533), Plex (32400), Subsonic/
  Airsonic (4040) — **all closed**. Only 80/443 (→ the UGOS web UI) and 445 are open.
- Nothing is mounted on this workstation: no NFS/CIFS mounts, nothing in `/etc/fstab`.

So the chosen approach — *media server with an API, reached over HTTP, no host mount* — is the right
target but **does not exist yet**. UGOS Pro's own Music/Media apps are behind the authenticated web
UI and expose no documented open API worth building against.

### Why Orpheus instead of Navidrome

The Navidrome plan was sound for *music over an API*. Three requirements outgrew it:

- **Video as well as music**, from the same library and the same voice commands.
- **Playback on real devices** — a Shield, a phone, this workstation — instead of the SPA's audio
  graph. The old plan's deferred `RemoteSink` (DLNA/MPD/Snapcast) becomes Orpheus's device registry:
  every paired Orpheus player is a target.
- **A resolver built for Whisper output.** Subsonic search is substring matching. "sigur rose" and
  "churches" need phonetic matching, aliases, and ranked candidates with a disambiguation flag. The
  Orpheus spec defines that retrieval contract, and its acceptance test, before any storage — the
  `MEMORY_PLAN.md` lesson applied.

What it costs, recorded honestly: we build and run a server ourselves (on Homelab, which mounts the NAS
over SMB; Pantheon itself still needs no mount). And the free Subsonic-client ecosystem (car and phone
apps) is gone, unless Orpheus later grows a Subsonic-compatible endpoint.

### Design

`services/skills/orpheus.py` holds the tool definitions; `services/orpheus_client.py` holds the HTTP
client.

**Connect** (once per Pantheon user), from a settings button or the `media_connect` tool:
1. **Find the server:** saved URL (checked against the saved `server_id`), then a subnet scan of
   `GET /api/v1/server` across `ORPHEUS_SCAN_CIDR`. mDNS is skipped: its multicast doesn't reach this
   container on the `ai-net` bridge. The container can't see the LAN's CIDR either, so `run-podman.sh`
   derives it from the host, the same way it derives `TZ`.
2. **Pair** as a `controller` device named after this host. The server returns a 6-digit code, the
   assistant tells the user to approve it on the server, and Pantheon polls until it's approved.
3. **Store** `server_id`, URL, and the device token in the per-user settings store. The token never
   appears in a prompt or a tool result.

**Tools.** While unpaired, only `media_connect` is injected. After pairing it is swapped for the five
control tools — the same count as the measured DJ set:

| Tool | Orpheus call |
|---|---|
| `media_connect` *(unpaired only)* | Discovery + `POST /api/v1/pair`; returns the code to read out |
| `media_find {query, kinds?}` | `POST /api/v1/resolve` — ranked candidates + `needs_disambiguation`; when set, she asks |
| `media_play {item_id, device?}` | `play` command to a player |
| `media_control {action, device?, position_ms?}` | pause / resume / stop / next / previous / seek |
| `media_volume {level, device?}` | `volume` command |
| `media_status {device?}` | Registered players and what each is playing |

`device` is a player *name*, validated against the server's registry. It defaults to the Orpheus player
on this workstation if one is paired, otherwise a per-user default. When she mishears something and the
user corrects her, the skill may teach the server an alias (`POST /api/v1/aliases`).

**Speech biasing.** Periodically fetch `GET /api/v1/vocabulary` and pass it to faster-whisper as
`initial_prompt`, so library names are recognized before they need resolving.

**Playback happens on an Orpheus player, not in the SPA.** `BrowserSink` is dropped. To listen at this
desk, run the Orpheus player (Flatpak) on this workstation — it's just another registered device.

### The hard problem: music audio vs. the voice loop

> **Changed by the move to Orpheus.** Music now plays in the Orpheus player, outside Pantheon's audio
> graph. Ducking under TTS gets *easier*: Pantheon sends the player `duck` when she starts speaking and
> `unduck` after. The open-mic half gets *harder*: option 2's "raise the VAD threshold by the music's
> own amplitude" relied on the music being a signal we generate in the browser, and it no longer is.
> Headphones or push-to-talk remain the answer. The analysis below is otherwise unchanged.

This is the real engineering risk, and it's new — the vtuber plan never had a second continuous audio
source. Today's hands-free loop is **half-duplex**: `hfTick` skips the mic entirely while `ttsSource`
is active plus a 400 ms tail. That was chosen after echo-*ducking* failed (her voice still came
through at speaker volume — `echo_suppress`/`ttsLevel` remain in the code but are dormant).

Music breaks that design in both directions:

- Music is **continuous**, unlike her speech. If music counts as "she's speaking," the open mic is
  gated off forever and hands-free dies. If it doesn't, the RMS gate trips on the music itself and
  every song gets transcribed as garbage and auto-sent.
- She must be able to **talk over the music** — which means ducking the music under TTS, i.e. exactly
  the mixing the current design gave up on.

Options, in order of preference:

1. **Headphones path** — trivially correct, zero DSP, and the honest answer for hands-free + music.
   Should be the documented recommended setup.
2. **Revive ducking for music only** — music is a *known signal we generate*, so we can subtract it:
   duck the music gain under TTS, and raise the VAD threshold by the music's own amplitude (the
   dormant `echo_suppress`/`ttsLevel` machinery, applied to a signal we control). Better odds than the
   original attempt, because we know the music's envelope exactly.
3. **Proper WebRTC AEC** — the real fix, also the one the vtuber plan already deferred once.
4. **Wake-word-gated mic while music plays** — only interrupt on the wake word; leans on the
   openWakeWord upgrade the vtuber plan lists as ◻.

**Recommendation:** ship push-to-talk + music first (no conflict at all — PTT already calls
`stopSpeaking()`), document headphones for hands-free, and treat (2) as the follow-up. Do not block
the DJ skill on solving AEC.

### Voice command mapping

No new NLU layer: STT transcript → normal chat turn → the model emits a `media_*` tool call. "Play
something upbeat", "skip this", "what is this song?" all route through the same tool-calling path
already verified above. The wake word (`applyWakeWord`) is unchanged.

## Phases

- **Phase 0 — TZ + date/time. ✅ DONE.** `TZ` derived from the host in `run-podman.sh`, defaulted in
  `compose.yaml`; assistant-mode date/time injected as a trailing system message in
  `prompt_builder.build_messages`. No skill machinery involved.
- **Phase 0b — skill scaffolding.** `CharacterCard.skills`, a skill registry, the two-pass tool loop
  in `chat()` with the trailing note re-appended, an SSE `skill` event, and tool schemas counted
  against the prompt budget. **Nothing has been built here yet** — this is the real first
  implementation step, and Phase 2 depends on it.
- **Phase 1 — System Info.** Now just `get_system_status` over read-only bind mounts. Still worth
  doing first: it's the smallest possible skill, so it shakes out the tool loop end-to-end before
  the DJ skill's network and audio complexity land on top.
- **Phase 2 — Media, read-only.** Needs Orpheus server Phase 1 (library, resolver, pairing).
  `media_connect` / `media_find` / `media_status`. She can talk about the library before she can
  touch it — the low-risk half.
- **Phase 3 — Media playback.** Needs Orpheus **Phase 5** (control protocol; Orpheus Phase 4 became
  profiles and state) and at least one Orpheus
  player. `media_play` / `media_control` / `media_volume`, PTT-compatible. Hands-free documented as
  headphones-only.
- **Phase 4 — Audio coexistence.** `duck` / `unduck` around TTS, plus a VAD policy for when a nearby
  player is playing (see the note in the hard-problem section).
- ~~**Phase 5 — Remote sink.**~~ Folded into Phase 3: every paired Orpheus player is a target.

## Security

The app **publishes on all interfaces** (`0.0.0.0:7867`) and is multi-user with `admin`/`user` roles —
the same reasoning that killed the hardcoded admin password applies here, harder.

- **No arbitrary command execution, no arbitrary file reads.** Fixed tools with typed, validated,
  enumerated parameters. A skill is an allowlist, not an interpreter.
- **The LLM is untrusted input.** Tool arguments come from a model steered by a user-authored card,
  and cards are importable from Chub. Validate every argument server-side; never interpolate one into
  a shell, a path, or a URL without validation.
- **Per-user, not global.** Skill availability and NAS credentials resolve through the request `Ctx`
  like every other data route. One user's card must not reach another user's config.
- **Prompt injection is in scope.** A malicious card's `description` or `post_history_instructions`
  can try to drive skills. Keep skills to read-mostly, low-consequence actions; anything destructive
  needs explicit UI confirmation, not a tool call.
- **Pairing with Orpheus needs a human.** The server issues a token only after someone approves the
  code on the server, so a prompt-injected card can start a pairing request but can't complete one.
  Pantheon pairs as a `controller`: it can find, play, and teach aliases, but can't scan, prune,
  override matches, or approve other devices.
- **Host status endpoint (if built)** is read-only, unparameterized, and bound to `ai-net` — not
  published to the LAN.

## Open decisions

- Where the skill registry lives — `services/skills/` with a decorator-based registry, vs. explicit
  wiring in `main.py`. (Registry is cleaner; explicit is more in keeping with the current codebase.)
- Whether tool schemas are trimmed per turn (only inject `media_*` once media is plausibly relevant)
  to claw back the +218 tok — cheap heuristically, but a mid-conversation schema change may confuse
  the model.
- Whether a skill result should be able to write to memory (e.g. "she remembers you like this album")
  or stay strictly ephemeral. Leaning ephemeral for now; auto-facts already have a review surface.
- Whether `media_connect` should be a tool at all, or only a settings-UI button (as a tool it costs
  schema tokens, but only while unpaired).
- Whether `get_system_status` should be admin-gated.

## Integration points (reuse, don't rebuild)

- `kobold.chat_generate_stream` — the tool loop wraps it; its auto-continue + trailing-system-turn
  re-append logic is the pattern to extend, not bypass.
- `prompt_builder.build_messages` — tools ride alongside, not inside, the system block.
- `PROMPT_BUDGET.md` — tool schemas are a new fixed cost on every turn; add them to the table.
- `AppSettings` + `GET/PUT /api/settings` — Orpheus server ID, URL, device token, default player, and
  per-skill toggles (shown to users as what is on each bartender's menu).
- `Ctx` — per-user isolation for skill config, same as every other data route.
- The `piper:` TTS backend is the precedent for a **pluggable provider behind one interface**; the
  library provider and playback sink should follow it.
- `run-podman.sh` / `compose.yaml` — the TZ fix, `ORPHEUS_SCAN_CIDR` (derived from the host like
  `TZ`), and any new mounts.
