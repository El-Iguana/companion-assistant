# The resident: Cantinero where the speakers are

**Status:** plan, 2026-11-23. Scope from the user, the same day.

## Scope

- **The resident app is the primary voice interface.** It is a local Cantinero on any machine that plays music
  or talks: wake word, STT, TTS, the avatar, and the Orpheus player.
- **The web chat is for configuration and testing.**
- **The first resident is predator** (the speakers). **amd-halo stays the brain:** KoboldCPP, the cards, the
  bartenders' memory, accounts, the DJ's Orpheus pairing.

Three decisions (user, 2026-11-23):

1. **STT and TTS run on the resident**, so audio never leaves the machine. Only text goes to amd-halo.
2. **The Orpheus player is built into the resident and always on.** A play from anywhere starts at once,
   with nothing to launch. This replaces #36's "desktop shell launches the player" (`can_launch`,
   `gtk-launch`), which is removed.
3. **A browser chat plays on a default speaker machine** (a per-user setting: predator). A named player
   overrides it ("in the lounge"). A voice request to a resident plays on that resident's own player.

## Shape

```
predator (resident)                                   amd-halo (server)                 homelab
┌───────────────────────────────────────────┐        ┌──────────────────────────┐      ┌─────────┐
│ shell (GTK4 + WebKit, tray)               │        │ Cantinero :7867/:7868    │      │ Orpheus │
│  ├─ /desktop page: wake word, avatar      │  text  │  /pdesk… (bearer)        │ cmds │  :8096  │
│  │    ↕ http://127.0.0.1:7867 (loopback:  │ ─────► │  chat, memory, DJ        │ ───► │         │
│  │      the mic works, no HTTPS needed)   │ HTTPS, │  KoboldCPP               │      │         │
│  └─ music window (GStreamer) ◄── commands ┼─pinned─┼── DJ, music hub          │ ◄────┤ streams │
│ resident service (container)              │  CA    └──────────────────────────┘      │   WS    │
│  ├─ local: /pmedia/tts, /pmedia/stt       │                                          └─────────┘
│  └─ everything else → amd-halo, with the  │
│     resident's device token               │
└───────────────────────────────────────────┘
```

**Why a local service rather than pointing the shell at amd-halo:**
- a page loaded from `http://127.0.0.1` is a secure context, so `getUserMedia` works with no certificate
  in the WebView;
- STT and TTS run next to the mic and speakers (decision 1);
- the device token never reaches a page.

The desktop page's whole surface is small: `/pdesk/{state,open,chat,sweep,settings,card}`,
`/pmedia/{tts,stt,music/duck}`, the card PNG and `avatar.vrm`. So the resident serves the same
`/desktop` page and static files unchanged.

## Decisions inside the plan

- **R-1 Residents are paired devices of an account.**
  - The resident asks amd-halo for a pairing code. The user approves it in the web chat's
    Settings → Residents, and the resident gets a token.
  - amd-halo stores only a hash of the token (`data/auth/residents/<id>.json`: id, name, user id, hash,
    `orpheus_player_id`, last seen).
  - Revoking a resident cuts it off at once.
  - Same shape as Orpheus pairing, which the user already knows.
- **R-2 A resident calls the desktop page's own routes with a bearer token, on an allowlist.**
  - Allowed: `/pdesk/*`, a card's PNG, `avatar.vrm` and `/pmedia/music/duck`.
  - In code, each allowed route says `_ctx(request, resident=True)`. A token on any other route is a 401
    (no admin, no studio, no accounts). A bearer call needs no CSRF token.
  - So the resident forwards paths unchanged. `/resident/*` holds only what is a resident's own:
    `POST /resident/pair`, `GET /resident/pair/{id}`, `GET`/`PUT /resident/self` (its Orpheus player).
  - Built in R1 (2026-11-23); the first draft had a `/resident/*` copy of every route.
- **R-3 Resident → amd-halo goes over HTTPS on :7868**, with Cantinero's CA pinned by fingerprint at pairing
  (`pantheon_web/tls.py` already makes it). A plain-HTTP fallback is allowed only if the user turns it on.
- **R-4 The player is Cantinero's own, built for an assistant to drive** (user, 2026-11-23, replacing
  the Orpheus web player in a WebKit view).
  - **Cantinero commands it directly.** Orpheus is only the library and the stream source; Orpheus's
    Phase 5 command relay is not used by it.
  - **A small window of its own** on the speaker machine: cover, title, artist and album, a seek bar,
    previous / play-pause / next, volume, and the queue. It is built for glancing at, not browsing.
  - **Native:** GStreamer `playbin3` in the shell's process (GTK4). Every format the machine decodes
    plays direct, so there are no transcode sessions for music.
  - **The superseded first build** (#45, the Orpheus web player in a second WebKit view) proved WebKit
    plays Orpheus streams from a remote command (album 351 ms, duck 16 ms, minimized). Its lesson, kept
    for any WebKit media: both autoplay gates (the gesture setting and
    `WebsitePolicies(autoplay=ALLOW)`) must be open.
- **R-5 Streams come from Orpheus to the resident as its own `player` device.**
  `python -m resident pair-orpheus` pairs once (approved on the Orpheus server). The resident reports a
  GStreamer capability set (`engine: gstreamer`) and asks `POST /items/{id}/playback` for each track's
  URL, so play counts and progress land on the right device and profile.
- **R-6 DJ targeting:** the players are the user's **residents**.
  - a voice request from a resident plays on that resident;
  - otherwise a resident named in the request;
  - otherwise the **default resident** (Settings → Music);
  - otherwise the only one connected;
  - otherwise she asks.
- **R-7 The command path is amd-halo → resident → player.**
  - Each resident holds one WebSocket to amd-halo (`/resident/connect`, bearer token, pinned TLS,
    reconnect 1 s → 30 s). Commands go down it with ids and come back as acks; state comes up it.
  - amd-halo's DJ expands what to play (album → tracks, artist → every album, playlist → its entries)
    through Orpheus's ordinary API and sends the resident a queue of items with titles, artists, albums
    and artwork ids.
  - The resident service passes commands to the shell's player over a loopback WebSocket.
  - **Ducking never leaves the machine:** the resident's own page tells the resident service, which
    ducks its player directly.
- **R-8 One image, two roles.** The resident runs the same `companion-assistant` image with
  `PANTHEON_ROLE=resident` (entrypoint `resident/service.py`), which already has Kokoro, Piper and Whisper
  baked in. No KoboldCPP and no library on the resident; its own state is
  `~/.local/share/cantinero-resident/` (tokens 0600, pinned fingerprint, settings).
- **R-9 amd-halo unreachable:** she says so in her own voice through local TTS ("I can't reach the bar"). No
  silent failure.

## Steps (each usable on its own)

1. ✅ **R1 — amd-halo: residents** (#37, merged 2026-11-23). Pairing and approval, token auth on the allowlist (R-2), Settings →
   Residents (approve, rename, revoke), the default player in Settings → Music, DJ targeting (R-6), and
   removing the launch path. Tests.
2. ✅ **R2 — the resident service** (#38, built 2026-11-23). `resident/`, where `PANTHEON_ROLE=resident` runs:
   - local TTS and STT;
   - `/desktop` and static files;
   - a proxy for the allowlisted routes, path unchanged, that streams the chat SSE;
   - `python -m resident pair <server>`, with CA pinning.

   Tests against a fake amd-halo. Runs on amd-halo itself for development (loopback to its own server).
3. **R3 — Cantinero's own player** (#39). Revised 2026-11-23 (R-4 to R-7):
   - amd-halo's resident hub and DJ retargeting;
   - the resident's relay and local ducking;
   - the native player window in the shell;
   - `pair-orpheus` and the capability report.

   ✅ Built 2026-11-23. The player was verified on predator against a stand-in resident: an album
   streamed from Orpheus; play, volume, duck/unduck, next, seek, previous, pause, resume and stop were
   all acked; it reconnected by itself; an unknown command was refused. The full chain (amd-halo → the
   resident service → the player) is R4's install and R5's acceptance.
4. **R4 — install** (#40). `resident/install.sh`: build or pull the image, a systemd user unit for the
   service container and for the shell, both pairings walked through, and an uninstall.
5. **R5 — acceptance on predator** (#41):
   1. Wake word, reply spoken on predator, STT and TTS local (no audio on the LAN, checked in amd-halo's
      logs).
   2. "Mira, play Ágætis byrjun" plays on predator's built-in player.
   3. "Play Rush" typed in the web chat on amd-halo plays on predator (the default player).
   4. "Pause", "next", "turn it down" by voice in under a second.
   5. Music ducks under her voice and while she listens, and comes back.
   6. With amd-halo stopped she says she can't reach the bar. Starting it again recovers with no restart.
   7. A reboot of predator brings the resident and its player back, signed in.

## Not in this plan

- Several bartenders speaking at once.
- Multi-room sync.
- A phone as a resident (a browser on a phone stays a web chat).
- Moving KoboldCPP.
- The Android Orpheus player.
