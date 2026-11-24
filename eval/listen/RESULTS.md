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

## V2: Silero VAD, the wake word named to Whisper (#90)

`services/listen.py`: Silero (faster-whisper's own `silero_vad_v6.onnx`) frame by frame, 32 ms;
speech starts after 2 frames ≥ 0.5, ends after 0.5 s < 0.35; 0.5 s pre-roll; 12 s cap; the
same half duplex and her-turn rules. Whisper is told the wake word in a sentence of context
(`initial_prompt="The assistant's name is Mira."`).

Each change alone, then both (same scenes, same Whisper):

| pipeline | sent right | missed | false sends | Whisper calls (on no speech) | delay median (p90) |
|---|---|---|---|---|---|
| energy (today) | 10 / 28 | 18 | 0 | 61 (25) | 1.01 s (1.37 s) |
| energy + the name | 19 / 28 | 9 | 0 | 61 (25) | 1.03 s (1.25 s) |
| Silero | 17 / 28 | 11 | 0 | 44 (0) | 0.65 s (0.72 s) |
| **Silero + the name** | **27 / 28** | **0** | **0** | **44 (0)** | **0.67 s (0.72 s)** |

(The 28th was sent with a word misheard: "Mira, next time" for "next song".)

- **Music no longer holds the gate:** every wake command in the three music scenes was heard
  (energy: 1 of 12).
- **No Whisper on noise:** 0 of 44 calls on knocks or room tone (energy: 25 of 61).
- **The name, said right:** 28 of 28 wake utterances transcribed starting with "Mira", 17 of 28
  without. faster-whisper's `hotwords` made it worse (15 of 28): it reads as text already
  heard, and Whisper left the name out ("What time is it?"). Naming her in a sentence added
  "Mira" to none of the chatter or her echo (0 false sends).
- **Faster:** the end of a command to the message, 0.67 s against 1.01 s (Silero needs 0.5 s
  of quiet, the level gate 0.8 s, and Whisper has less to read).

**Live on predator** (its CPU; the scenes streamed in real time into the resident's
`/pmedia/listen`): 15 of 15 wake commands sent across quiet_room, music_1 and her_replies,
0 false sends in no_wake_noise, every Whisper call on speech, delay 0.70–0.99 s from the end
of speech to the message.
