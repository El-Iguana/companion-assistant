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
│  └─ Orpheus player view (always on)  ◄────┼─pinned─┼──────────────────────────┼──────┤ control │
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
- **R-4 The built-in player is the Orpheus web player in a second WebKit view** of the same shell process:
  - its own window, hidden by default, shown from the tray;
  - autoplay allowed by the WebView setting, not a Chromium flag.
  - **Spike first** (R3): Pyodide, MSE/HLS, `canPlayType` for mp3/flac, and a remote `play` making sound in
    WebKitGTK. If WebKit can't run it, the fallback is a supervised Chromium `--app` window with
    `install.sh --remote`.
- **R-5 The resident pairs with Orpheus as a `player`** (the Orpheus admin approves the code once). It then
  signs its player view in by setting the token as the `orpheus_session` cookie, as the Phase 5
  end-to-end test did. So the resident knows its player's exact device id, sends it to amd-halo
  (`PUT /resident/self`), and the DJ targets it with no name matching.
- **R-6 DJ targeting:**
  - a voice request from a resident plays on its own player;
  - otherwise a player named in the request;
  - otherwise the user's **default player** (web chat Settings → Music, from Orpheus's device list,
    stored in `media/orpheus.json`);
  - otherwise the only one online;
  - otherwise she asks.
- **R-7 Ducking stays on amd-halo.** The resident's page posts `music/duck`, which the resident forwards, and
  amd-halo sends `duck`/`unduck` to that resident's player. The Orpheus controller token never leaves
  amd-halo.
- **R-8 One image, two roles.** The resident runs the same `companion-assistant` image with
  `PANTHEON_ROLE=resident` (entrypoint `resident/service.py`), which already has Kokoro, Piper and Whisper
  baked in. No KoboldCPP and no library on the resident; its own state is
  `~/.local/share/cantinero-resident/` (tokens 0600, pinned fingerprint, settings).
- **R-9 amd-halo unreachable:** she says so in her own voice through local TTS ("I can't reach the bar"). No
  silent failure.

## Steps (each usable on its own)

1. **R1 — amd-halo: residents** (#37). Pairing and approval, token auth on the allowlist (R-2), Settings →
   Residents (approve, rename, revoke), the default player in Settings → Music, DJ targeting (R-6), and
   removing the launch path. Tests.
2. **R2 — the resident service** (#38). `resident/`, where `PANTHEON_ROLE=resident` runs:
   - local TTS and STT;
   - `/desktop` and static files;
   - a proxy for the allowlisted routes, path unchanged, that streams the chat SSE;
   - `python -m resident pair <server>`, with CA pinning.

   Tests against a fake amd-halo. Runs on amd-halo itself for development (loopback to its own server).
3. **R3 — the built-in player** (#39). The WebKit spike (R-4), then a second view in the shell: pairing with
   Orpheus as a player, the cookie, `PUT /resident/self`, and the tray's Show/Hide player.
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
