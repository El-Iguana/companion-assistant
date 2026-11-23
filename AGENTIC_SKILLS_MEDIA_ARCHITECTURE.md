# Agentic Skills Architecture — Media Playback Reference Implementation

## Purpose

Implement a general-purpose **skill framework** for an existing locally hosted AI assistant that already supports text chat, speech-to-text (STT), text-to-speech (TTS), and local LLM inference. Use music search and playback as the first reference skill, with playback in both the assistant's browser and selectable remote devices.

This document is implementation context for Claude Code. **Inspect the existing repository before making changes.** Reuse established frameworks, services, authentication, network conventions, and coding patterns. Treat all names and endpoints below as proposed contracts, not claims about existing code.

## Existing environment and assumptions

- Local AI stack uses **KoboldCpp** for LLM inference, **Podman** containers and an existing AI companion frontend.
- The assistant already has or will expose STT and TTS. Both voice and text must converge on the same command/skill pipeline.
- A separately designed media server is under the owner's control and can expose new APIs.
- Media server implementation, player backend, exact endpoints, existing schemas, and authentication mechanisms are **not yet confirmed**. Discover them before integrating.
- Favor local-first execution. Do not assume a cloud music service or public internet exposure is necessary.

## Desired user experiences

Examples:

- “Play Pink Floyd in my browser.”
- “Play Tom Sawyer by Rush.”
- “Play my favorites on the office computer.”
- “Move this music to the living room.”
- “Pause,” “skip,” “volume 40 percent.”
- “Add this song next.”
- “What's playing in the bedroom?”
- “Stop the music everywhere.”
- “Play more Yes and less Genesis.”
- “What album is this from? Play the rest of it.”

Support follow-up references such as “this,” “there,” “their earlier albums,” and “turn it down,” resolving against verified conversation context and authoritative media-server state. Ask for clarification if multiple sessions/devices or media results are plausible.

## Architectural principles

1. **Separation of concerns:** assistant interprets intent; skill runtime validates/authorizes/dispatches; media server owns catalog, queues, sessions, and playback coordination; playback clients render audio.
2. **Shared interface:** text and STT output feed the same orchestrator. TTS is an output channel, not a separate command implementation.
3. **Control plane vs. media plane:** API/WebSocket messages carry commands and status; authenticated HTTP streams carry audio directly to playback clients. Never pipe audio through the LLM or skill executor.
4. **Authoritative state:** media server owns playback sessions, queue and device assignment. LLM conversation memory is not playback truth.
5. **Hybrid execution:** deterministic fast path for high-confidence pause/skip/volume; LLM tool calling for semantic search and multi-step requests.
6. **Portability:** native skill interface with JSON Schema/OpenAI-style tool definitions; optional MCP adapters later.
7. **Explicit permissions:** least-privilege per skill, user, session and device; no arbitrary URLs or code execution supplied by the model.
8. **Async lifecycle:** distinguish a tool operation finishing from ongoing playback continuing.

## Logical topology

```text
Text chat ───────────┐
                     ├──> Assistant Orchestrator ──> Intent Router
Microphone -> STT ───┘                                 │
                                                      ├──> Fast-path commands
                                                      └──> LLM tool calling (KoboldCpp)
                                                               │
                                                               v
                                                Skill Registry / Runtime
                                                - schema validation
                                                - authorization
                                                - context resolution
                                                - tool dispatch
                                                - structured results
                                                               │
                                                               v
                                                   Music Skill Adapter
                                                               │
                                                               v
                                                     Media Server API
                                            - catalog / media resolution
                                            - playback sessions / queues
                                            - device registry / presence
                                            - stream authorization
                                            - playback events
                                                  │            │
                                        HTTP audio │            │ WebSocket control
                                                  v            v
                                            Browser Player   Remote Player Agents

Tool result -> Orchestrator -> Chat response / TTS
Playback events -> UI player and session state (without LLM inference)
```

## Skill runtime design

Build an independently deployable, internal service/module, adapting to repository conventions. Suggested responsibilities:

- **Registry:** discover versioned skill manifests and expose relevant tools.
- **Executor:** validate structured arguments, enforce timeouts, dispatch tools, normalize results and errors.
- **Permission engine:** authenticate caller and enforce allowed actions, resource scope and device/session ownership server-side.
- **Context resolver:** bind authenticated user, conversation, current playback session, preferred/default device, and room context if available.
- **Adapter layer:** native skills first; optional MCP tool-server adapters later.
- **Operation tracking:** `requested -> authorized -> running -> completed | failed | cancelled`; operation IDs, idempotency and audit records.

Never execute arbitrary model-generated code or use model-supplied unrestricted HTTP endpoints. Credentials must be injected securely, not exposed in tool definitions or model context.

### Proposed skill package

```text
skill-runtime/
  registry/
  executor/
  permissions/
  context/
  adapters/native/
  adapters/mcp/
skills/music/
  skill.yaml
  tools.py
  client.py
  schemas/
media-server/             # only if this code is in the repository
  catalog/
  playback/
  devices/
  streaming/
  events/
player-agent/
  controller/
  backend/
  telemetry/
```

Use existing repository layout rather than imposing this tree blindly.

### Example manifest (proposed)

```yaml
id: media.music
name: Music Playback
version: 1.0.0
runtime:
  type: native
  entrypoint: skills.music.tools
permissions:
  - media.catalog.read
  - media.playback.control
  - media.queue.modify
  - media.devices.read
configuration:
  media_server_url:
    env: MEDIA_SERVER_URL
  api_token:
    env: MEDIA_SERVER_TOKEN
tools:
  - music.search
  - music.play
  - music.pause
  - music.resume
  - music.skip
  - music.seek
  - music.queue
  - music.volume
  - music.now_playing
  - music.list_devices
  - music.transfer
```

Declared permissions are not sufficient: enforce authorization during execution.

## Music skill interface

Expose a small semantic surface to the LLM rather than every low-level media endpoint.

| Tool | Function |
|---|---|
| `music.search` | Find tracks, artists, albums, playlists with ranked results and stable IDs |
| `music.play` | Resolve a natural-language request or known media ID, then start playback |
| `music.pause` / `music.resume` | Control active or explicit session |
| `music.skip` | Next or previous track |
| `music.seek` | Change track position |
| `music.queue` | Append, insert next, remove, reorder or replace items |
| `music.volume` | Absolute or relative adjustment, bounded to supported range |
| `music.now_playing` | Read authoritative session and track metadata |
| `music.list_devices` | List available, authorized destinations |
| `music.transfer` | Transfer session to another destination |

Suggested high-level `music.play` arguments:

```json
{
  "query": "Tom Sawyer by Rush",
  "media_type": "track",
  "destination": "office-desktop",
  "mode": "replace"
}
```

`media_type` may be `track | album | artist | playlist | auto`; `mode` may be `replace | append | next`. Also accept an exact stable media ID through a separate validated field or equivalent typed command. Do not guess IDs. The adapter can internally search and select when the match is unambiguous; return `AMBIGUOUS_MEDIA` with candidate IDs and labels otherwise. Subjective playlist requests require explicit, testable catalog/ranking logic, not just LLM assertions.

### Tool response contract

Use structured results, for example:

```json
{
  "ok": true,
  "operation_id": "op-123",
  "session_id": "session-123",
  "device_id": "office-desktop",
  "status": "playing",
  "media": {
    "id": "track-456",
    "title": "Tom Sawyer",
    "artist": "Rush"
  }
}
```

Only claim playback started when the destination confirms actual playback, not merely receipt of a command.

Suggested error codes: `AMBIGUOUS_MEDIA`, `MEDIA_NOT_FOUND`, `DEVICE_OFFLINE`, `PLAYBACK_BLOCKED`, `PERMISSION_DENIED`, `SESSION_CONFLICT`, `DEVICE_UNSUPPORTED`, `TIMEOUT`.

## Playback session model

**Session is the primary abstraction**, separate from any physical device. It retains queue, playback position and logical identity across device transfers.

```json
{
  "session_id": "session-123",
  "owner_id": "user-1",
  "device_id": "living-room",
  "status": "playing",
  "current_track_id": "track-456",
  "position_ms": 84000,
  "volume": 65,
  "shuffle": false,
  "repeat": "off",
  "queue_version": 12
}
```

Keep queue items/order in a separate persistent structure. Maintain monotonic session revisions and serialized mutations per session. Support multiple simultaneous sessions per user only with explicit selection and disambiguation rules.

Device context precedence:

1. Explicit device/session in current command.
2. Active conversation/session selection, if unambiguous and authorized.
3. Voice endpoint's configured room/device, if present.
4. User's preferred device (often browser for text chat).
5. Ask for clarification if no safe, unambiguous choice exists.

## Device registry and players

Treat browser tabs and remote agents as registered playback endpoints. Server assigns/authenticates device identities. Track presence through heartbeat/expiry.

Example capability advertisement:

```json
{
  "device_id": "office-desktop",
  "name": "Office Desktop",
  "type": "desktop",
  "capabilities": {
    "play": true,
    "pause": true,
    "seek": true,
    "volume": true,
    "queue": true,
    "formats": ["mp3", "flac", "opus"],
    "max_channels": 2
  },
  "status": "online"
}
```

### Browser player

- HTML5 audio with authenticated stream access, supported byte-range requests, and compatible codecs.
- Connect to session/device event channel; render album art, title, queue, progress, output device and controls.
- Browser UI controls call the media API directly, not through an LLM inference.
- Handle browser autoplay policies: remote commands may be blocked until a user gesture; report `PLAYBACK_BLOCKED` and show a usable play control.
- Handle tab reloads, reconnects, stale presence and state reconciliation.
- If TTS and music share an output device, implement ducking and avoid STT echo/self-triggering.

### Remote Player Agent

- Lightweight authenticated outbound connection to media server, preferably WebSocket for commands/events.
- Play audio with MPV or GStreamer (Linux reference implementation).
- Fetch audio directly over authorized HTTP(S), report actual playback state and position.
- Support reconnection, command IDs, heartbeat, capability negotiation, and local audio-device selection.
- Avoid inbound ports on individual player devices where possible.
- For off-LAN devices, prefer private connectivity such as Tailscale and TLS/WSS.

## Media server API (proposed, adapt to actual design)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/music/search` | Catalog search |
| GET | `/api/music/artists/{id}` | Artist metadata |
| GET | `/api/music/albums/{id}` | Album/tracks |
| GET | `/api/music/playlists` | Playlists |
| POST | `/api/playback/play` | Start/replace playback |
| POST | `/api/playback/pause` | Pause |
| POST | `/api/playback/queue` | Mutate queue |
| POST | `/api/playback/volume` | Volume |
| GET | `/api/playback/state` | Authoritative state |
| GET | `/api/playback/devices` | Device list |
| POST | `/api/playback/transfer` | Transfer session |
| GET | `/api/stream/{track_id}` | Authenticated media stream |
| WS | `/api/playback/events` | Commands/status/events |

Separate media stream authorization from general API credentials. Prefer short-lived, session/device-scoped stream tokens and HTTP range support. Format negotiation/transcoding should consider actual client support; initial browser fallback can be MP3/AAC, with native FLAC/Opus where supported.

## Event protocol

Commands should carry a unique command ID, session ID, device ID, expected session revision, and timestamps. Acknowledge receipt separately from actual playback start.

Server-to-player example:

```json
{
  "type": "playback.command",
  "command_id": "cmd-1024",
  "session_id": "session-123",
  "device_id": "living-room",
  "action": "play",
  "payload": {
    "track_id": "track-456",
    "stream_url": "/api/stream/track-456",
    "position_ms": 84000
  }
}
```

Player-to-server example:

```json
{
  "type": "playback.status",
  "command_id": "cmd-1024",
  "session_id": "session-123",
  "device_id": "living-room",
  "status": "playing",
  "position_ms": 84120
}
```

Implement idempotency, stale-command rejection, reconnection snapshots, session revisions and per-session serialization.

## Transfer / handoff protocol

For “move music to the living room”:

1. Authorize source session and destination device.
2. Acquire session-level transfer lock; snapshot current queue, track and position.
3. Prepare destination stream and playback capability without stopping source.
4. Confirm destination readiness; start at an adjusted recent position.
5. Confirm destination is actually playing.
6. Commit authoritative device assignment; stop source and broadcast state.
7. On failure, preserve/restore original playback and return a structured error.

Guard against concurrent transfers, duplicated playback and stale revisions. An initial handoff can have a short interruption; sample-accurate synchronization and synchronized multiroom playback are explicitly later-stage work.

## Persistence and deployment

- Prefer existing project storage; PostgreSQL is suitable for sessions, queue state, device records and user preferences.
- Redis is optional for multi-instance coordination, not required for the first single-instance deployment.
- Podman deployment should attach to the existing internal network, with only the necessary HTTPS/WSS gateway exposed to authenticated clients.
- Never expose the skill runtime or raw internal service endpoints publicly.
- Keep tokens in secrets/environment injection rather than source code, manifests or LLM prompts.

Illustrative Compose skeleton only (not production-ready):

```yaml
services:
  skill-runtime:
    build: ./skill-runtime
    environment:
      MEDIA_SERVER_URL: http://media-server:8080
      LLM_URL: http://koboldcpp:5001
    networks: [ai-network]
    restart: unless-stopped

  media-server:
    build: ./media-server
    volumes:
      - media-data:/app/data
      - /srv/music:/media:ro
    networks: [ai-network]
    restart: unless-stopped

  player-agent:
    build: ./player-agent
    environment:
      MEDIA_SERVER_URL: http://media-server:8080
      PLAYER_NAME: local-player
    networks: [ai-network]
    profiles: [local-player]
    restart: unless-stopped

networks:
  ai-network:
    external: true

volumes:
  media-data:
```

Validate actual container names, networks, volumes, API ports and permissions against the repository.

## Voice and conversational behavior

- Route both typed messages and STT transcripts through the same intent router.
- Deterministic fast path only for high-confidence, well-scoped playback controls; semantic or ambiguous commands go through LLM tool calling.
- Prefer a single high-level `music.play` call for simple search-and-play requests to minimize inference/tool round trips.
- For follow-ups, combine conversational references with fresh media-server state. Never fabricate current playback or devices.
- TTS confirmations follow verified results; report partial failures accurately.
- Support interruptions, music ducking, microphone echo cancellation and barge-in as platform capabilities allow.

## Security, reliability and observability

- Authenticate users and devices, enforce per-user/device/session permissions on every request.
- Rate-limit and audit tool calls, recording caller, action, target, outcome and operation ID (without leaking secrets).
- Validate all tool arguments with strict schemas; never let the LLM select arbitrary backend URLs.
- Implement timeouts, bounded retries, idempotency and typed error results.
- Use TLS/WSS for client access, short-lived stream authorization and appropriate CSRF/origin protections for browser endpoints.
- Handle disconnected players, autoplay denial, unsupported formats, media ambiguity, duplicate commands and stale session state.
- Track metrics for STT-to-command latency, tool execution, playback start latency, device health and transfer success.

## Delivery phases

### Phase 1 — Minimum useful vertical slice

- Inspect repo and existing media-server contract.
- Skill registry, schemas, executor, permissions and result handling.
- Media catalog search, stable ID resolution, browser player, playback sessions and queue.
- LLM tool-call loop and high-confidence fast path for simple playback commands.
- Shared voice/text invocation and browser state updates.

### Phase 2 — Remote playback

- Device registry and authenticated remote Player Agent.
- WebSocket control/status and authenticated streaming.
- Device selection, session transfer and reconnection recovery.
- Tests for transfer failure, concurrent commands and offline devices.

### Phase 3 — General skill ecosystem

- MCP adapter support and versioned skill packaging.
- More sophisticated permissions and skill management UI.
- Cross-skill workflows and long-running operation monitoring.
- Optional synchronized multiroom audio as a distinct feature.

## Acceptance tests

1. Text command searches a song and plays it in the browser.
2. Voice command invokes the same music skill with equivalent behavior.
3. Pause/skip/volume work without an LLM call when intent is unambiguous.
4. Search ambiguity yields selectable candidates rather than guessing.
5. Browser playback state stays synchronized with authoritative server events.
6. A remote player registers, advertises capabilities and plays a supported stream.
7. “Move this to living room” transfers queue/position; source stops after destination starts.
8. Failed transfer leaves original playback functional.
9. Unauthorized user cannot control another user's session/device.
10. Disconnected device, autoplay block, stale command and reconnect are handled gracefully.
11. LLM cannot trigger unregistered tools or arbitrary network requests.
12. Tool operation lifecycle completes when playback start is confirmed, while playback continues independently.

## Instructions to Claude Code

1. **First inspect the repository** and summarize relevant existing assistant, STT/TTS, LLM/tool-calling, media-server, frontend, auth and Podman components.
2. Identify mismatches between this proposed architecture and the actual implementation. **Do not replace working components unnecessarily.**
3. Produce a concrete integration plan: module boundaries, interfaces, schemas, state model, migration needs, security controls, tests and phased milestones.
4. Flag unanswered design questions, especially: actual media server APIs, music library/search semantics, browser streaming auth, supported remote platforms, preferred persistence, multiuser requirements, and device ownership.
5. Prefer implementing **Phase 1 as a minimal end-to-end vertical slice**, with tests and documented configuration, once the plan is approved.
6. Avoid broad refactoring, speculative dependencies or exposing services publicly without explicit approval.

**Primary success criterion:** a reusable, secure skill runtime that allows the local AI assistant to search and control music by voice or text, with browser playback initially and selectable remote playback devices through the same media-server session model.
