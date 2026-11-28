# Tempo for new music

How a track gets the tempo and beats she dances to (#114, #124), and how music added to Orpheus
gets them without anyone asking.

## Who does what

| | Owns |
|---|---|
| **Orpheus** | The library. It says *when* a library last changed (`changed_at`), nothing about tempo. |
| **Cantinero server** | Measuring. `services/skills/music_tempo.py` measures each music track once and keeps the result in `<library>/music_tempo.json`. |
| **Resident (player)** | Asks for the beats of the track it starts (`/pmedia/music/tempo?ids=&beats=1`) and sends `{bpm, phase, local}` to the avatar. Unchanged. |

The analyser stays in Cantinero, where it was tuned against the real library (prior 100 ± 1
octave, DP beat tracking, FFT autocorrelation). Orpheus stays a media server and gains only a
change signal any client can use.

## What was wrong (2026-11-28)

1. **Measuring only started on a play.** `refresh_soon` ran when something was queued, and only
   when the cache was more than 6 h old. New music sat unmeasured until then.
2. **New albums came in only after a vague play.** The tempo build read `music_catalog.json` as it
   was. That file was refreshed only by a vague play ("play something upbeat"), so an album added
   to Orpheus could stay invisible to the tempo build indefinitely.
3. **The first play of a new song never danced.** Its tempo was measured after the queue was sent.
4. **Tempos were keyed by Orpheus's `item.id`.** It is stable only for the life of a `library.db`
   (Orpheus spec AD-1). After a rebuild the same number can be a different track, and she would
   dance to another song's beats.

## The design

### Orpheus: `changed_at` (El-Iguana/orpheus#38)

`GET /api/v1/libraries` gains, per library, `changed_at`: the latest `item.updated_at` in it, as
RFC 3339 (UTC), or `null` for an empty library. Assembly writes `updated_at` only for groups a scan
marked dirty, so a no-change rescan leaves it alone. Removals don't bump it, but they lower
`items.track`, which the listing already has. Clients compare the pair (`changed_at`,
`items.track`).

### Cantinero: keyed by `identity_key` (C1)

Cache entries are keyed by the track's `identity_key`, which survives a `library.db` rebuild. Each
entry also stores the `id` it was last seen under. Lookups go id → identity_key through the queue
entry, which now carries `identity_key`. The file says `"format": 3`, and the entries' measurement
version stays 2. Moving from the old file means re-keying the existing entries through one walk of the library, without measuring anything again. A retagged
file gets a new identity key, so it is measured again, which is correct.

### Cantinero: the library watcher (C2)

A background task in the server, started when Orpheus is paired:

- At start-up, and every `WATCH_S` (300 s), it reads `GET /api/v1/libraries`.
- When a music library's (`changed_at`, `items.track`) differs from the last value seen (stored
  in the cache file), it rebuilds `music_catalog.json` (the Fortuna spoken-word check runs only on
  new albums) and then runs the tempo build. That build measures only tracks it has no entry for
  and drops tracks that are gone.
- The tempo build always refreshes a stale catalog first, instead of reading it as it was.
- If Orpheus has no `changed_at` (an older server), it falls back to the 6 h timer.
- One build per library at a time, as now. The build stays at ~1 track/s and never blocks a play.

Expected delay: add an album, let Orpheus scan, and within ~5 min plus ~0.7 s per track it
has tempos.

### Cantinero: play-before-measured (C3)

When a queue is sent and its first track has no entry, that track is measured at once (~0.7 s,
in the background, ahead of the build). The player already asks for the beats of the track it
starts, and it asks again on `tempo:updated` if the first ask found nothing. So even a song played
a minute after it was added dances within about a second.

### Never

- Spoken word never gets a tempo (catalog `spoken`, user rule 2026-11-25).
- A play never waits for measuring.

## Steps

1. **Orpheus** (El-Iguana/orpheus#38): `changed_at` on `/api/v1/libraries`.
2. **C1** (#148): tempo cache keyed by `identity_key` (format 3, re-key without re-measuring).
   On a copy of amd-halo's cache (2026-11-28) all 917 entries were re-keyed and 0 were measured again.
   The same build found 25 tracks already in Orpheus that had never been measured, which is
   problem 1 above.
3. **C2** (#149): library watcher; the tempo build refreshes a stale catalog.
4. **C3** (#150): the track being played is measured first.
5. **Live check** (#151): copy an album to the NAS, rescan in Orpheus, and within 5 min "Mira, play
   <it>" dances on predator with the Dance switch on.
