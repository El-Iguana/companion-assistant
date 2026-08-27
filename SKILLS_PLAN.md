# Skills — plan

Give **assistant-mode characters** real capabilities: read the host system, and run the music.
A skill is a set of tool definitions plus the code behind them, attached to a character card.

First two skills: **System Info** (host date/time + machine status) and **DJ** (NAS music library —
catalog, metadata, playback by voice).

Status: **mostly not built.** The date/time half of System Info shipped early (2026-08-17) without
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
  | DJ (5 tools) | **+218** |
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

- **`boundary_note` stays last.** It is deliberately appended after `post_history_instructions`, and
  `kobold.chat_generate_stream` re-appends it on each auto-continue round so `"Continue."` can't
  demote it. The second pass is another round — it must re-append too, or the physical boundary
  silently drops out of exactly the turns that ran a skill. See `RELATIONSHIP_PLAN.md`.
- **Only the final visible reply** gets classified for emotion / mood / relationship / memory. Tool
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

## Skill 2 — DJ

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

### Phase 0 — stand up the API (prerequisite, on the NAS)

Install **Navidrome** via UGOS Pro's Docker/app support, pointed at the existing music share.
Rationale:

- Speaks the **Subsonic API**, the widest-supported open music API — which means the DJ skill is not
  locked to one server, and any Subsonic client (phone, car) works against the same library for free.
- Gives **cataloging, metadata, search, artwork, and stream URLs** out of the box. Building our own
  scanner over an SMB mount would mean re-implementing all of it (mutagen/ffprobe, an index, an
  invalidation strategy) — and `MEMORY_PLAN.md` already records the lesson about building elaborate
  storage before retrieval is defined.
- Lightweight, runs happily on a 2-bay NAS, and reads the library **read-only**.
- Matches the "over the network, no host mount" decision exactly: container → `http://192.168.1.219:4533`.

*(Jellyfin is the alternative if video matters later — it's heavier and its music API is less clean.
Plex is rejected: cloud account dependency conflicts with the local-first rule.)*

### Design

`services/skills/dj.py` + `services/music/` split into two seams so neither choice is baked in:

**Library provider** (`SubsonicProvider`, adapter interface) — `search`, `get_album`, `get_artist`,
`stream_url`, `cover_art`. Swapping to Jellyfin later means one new class.

**Playback sink** — per the decision to do browser now and remote later:
- `BrowserSink` (now): the SPA plays the stream URL. Reuses nothing from the TTS path except the
  audio context — deliberately a **separate gain node** (below).
- `RemoteSink` (later): DLNA/UPnP, MPD, or Snapcast to play on other speakers.

Tools exposed (the 5 measured above at +218 tok): `music_search`, `music_play`, `music_control`
(pause/resume/next/previous/stop), `music_volume`, `music_now_playing`.

Credentials (`NAVIDROME_URL`, user, token) go in the per-user settings store, not the image.

### The hard problem: music audio vs. the voice loop

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

No new NLU layer: STT transcript → normal chat turn → the model emits a `music_*` tool call. "Play
something upbeat", "skip this", "what is this song?" all route through the same tool-calling path
already verified above. The wake word (`applyWakeWord`) is unchanged.

## Phases

- **Phase 0 — TZ + date/time. ✅ DONE.** `TZ` derived from the host in `run-podman.sh`, defaulted in
  `compose.yaml`; assistant-mode date/time injected as a trailing system message in
  `prompt_builder.build_messages`. No skill machinery involved.
- **Phase 0b — skill scaffolding.** `CharacterCard.skills`, a skill registry, the two-pass tool loop
  in `chat()` with `boundary_note` re-appended, an SSE `skill` event, and tool schemas counted
  against the prompt budget. **Nothing has been built here yet** — this is the real first
  implementation step, and Phase 2 depends on it.
- **Phase 1 — System Info.** Now just `get_system_status` over read-only bind mounts. Still worth
  doing first: it's the smallest possible skill, so it shakes out the tool loop end-to-end before
  the DJ skill's network and audio complexity land on top.
- **Phase 2 — DJ, read-only.** Navidrome on the NAS; `music_search` / `music_now_playing` only. She
  can talk about the library before she can touch it — cataloging and metadata first, which is also
  the low-risk half.
- **Phase 3 — DJ playback (browser sink).** `music_play` / `music_control` / `music_volume`, separate
  gain node, PTT-compatible. Hands-free documented as headphones-only.
- **Phase 4 — Audio coexistence.** Ducking music under TTS + music-aware VAD gating (option 2).
- **Phase 5 — Remote sink.** DLNA/MPD/Snapcast, per the "browser now, remote later" decision.

## Security

The app **publishes on all interfaces** (`0.0.0.0:7861`) and is multi-user with `admin`/`user` roles —
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
- **Host status endpoint (if built)** is read-only, unparameterized, and bound to `ai-net` — not
  published to the LAN.

## Open decisions

- Where the skill registry lives — `services/skills/` with a decorator-based registry, vs. explicit
  wiring in `main.py`. (Registry is cleaner; explicit is more in keeping with the current codebase.)
- Whether tool schemas are trimmed per turn (only inject `music_*` once music is plausibly relevant)
  to claw back the +218 tok — cheap heuristically, but a mid-conversation schema change may confuse
  the model.
- Whether a skill result should be able to write to memory (e.g. "she remembers you like this album")
  or stay strictly ephemeral. Leaning ephemeral for now; auto-facts already have a review surface.
- UGOS Pro's Docker support specifics — needs confirming on the actual unit before Phase 2.
- Whether `get_system_status` should be admin-gated.

## Integration points (reuse, don't rebuild)

- `kobold.chat_generate_stream` — the tool loop wraps it; its auto-continue + trailing-system-turn
  re-append logic is the pattern to extend, not bypass.
- `prompt_builder.build_messages` — tools ride alongside, not inside, the system block.
- `PROMPT_BUDGET.md` — tool schemas are a new fixed cost on every turn; add them to the table.
- `AppSettings` + `GET/PUT /api/settings` — NAS URL/credentials and per-skill toggles.
- `Ctx` — per-user isolation for skill config, same as every other data route.
- The `piper:` TTS backend is the precedent for a **pluggable provider behind one interface**; the
  library provider and playback sink should follow it.
- `run-podman.sh` / `compose.yaml` — the TZ fix, and any new mounts.
