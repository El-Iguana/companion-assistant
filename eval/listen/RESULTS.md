# Hands-free listening: results (#6)

Synthetic scenes, `eval/listen/make_scenes.py` (seed 2026): six user voices (Kokoro), her
voice (af_sarah) back through a simulated room, room tone and knocks at predator's measured
levels, real tracks from the library for music. 8 scenes, 640 s, 28 wake commands. Scored by
`eval/listen/run.py`: the same Whisper (base.en, CPU int8) and wake gate (phrase mode,
"Mira") as the app. amd-halo, 2026-11-24.

## Baseline: today's energy segmenter (#89)

`voicecore.Segmenter`, the pages' rules: RMS over 0.015 opens, 800 ms under closes, ~85 ms
blocks, 0.7 s pre-roll, half duplex (deaf while she is audible).

| scene | wake | sent right | missed | false sends | Whisper calls (on no speech) | delay (median) |
|---|---|---|---|---|---|---|
| quiet_room | 5 | 3 | 2 | 0 | 11 (1) | 0.97 s |
| noisy_room | 5 | 4 | 1 | 0 | 15 (5) | 1.02 s |
| music_0 | 4 | 0 | 4 | 0 | 0 | – |
| music_1 | 4 | 1 | 3 | 0 | 8 (5) | 2.16 s |
| music_2 | 4 | 0 | 4 | 0 | 0 | – |
| her_replies | 6 | 2 | 4 | 0 | 7 (1) | 1.21 s |
| no_wake_noise | 0 | – | – | 0 | 20 (13) | – |
| no_wake_music | 0 | – | – | 0 | 0 | – |
| **total** | **28** | **10 (36 %)** | **18** | **0** | **61 (25)** | **1.02 s (p90 1.44 s)** |

What goes wrong:
- **Music holds the gate open.** Over the level threshold all the time, the segmenter never
  sees 800 ms of quiet, so an utterance never ends: music_0 and music_2 sent nothing to
  Whisper at all. 11 of 12 wake commands during music were lost.
- **Whisper hears "Mira" as "Mirror", "Miro", "MIRO"**: 9 of the 18 misses were transcribed
  and then failed the exact wake-word match. No segmenter fixes that: the wake word has to
  reach Whisper as a hint (faster-whisper `hotwords`).
- **Noise reaches Whisper:** 25 of 61 calls were on no speech (knocks, room tone), 13 in
  the scene where nobody spoke to her. None became a message (the gate held), but each costs
  ~0.3–0.5 s of CPU and a chance to invent words.
- No false sends: the text wake word is a good last gate.
