# The memory fold's decision: KoboldCPP against Fortuna

**2026-11-23 · 48 user turns × 3 runs · KoboldCPP (Cydonia-24B) and Fortuna (JevK5 4B q8)**
`podman cp eval companion-assistant:/app/eval && podman exec companion-assistant python3 /app/eval/memory_decisions/run.py --runs 3`

One question per user turn: what should the assistant remember long-term from it? The answer is
an instruction, a fact, a time-bound event, or nothing. `turns.jsonl` labels each turn with every
answer that is fair. "Tom and Bill are coming next month" is an event and a fact, for instance.

```
kobold   right 120/144  invented 21  missed 0  median 827 ms  max 5237 ms
fortuna  right 138/144  invented 0   missed 6  median 162 ms  max 186 ms
```

- **invented**: something stored where only "nothing" is right. This is the costly error,
  because a stored memory goes into every later prompt.
- **missed**: nothing stored where something was there.

## What each gets wrong

**KoboldCPP invents standing instructions from one-off requests:**
- "Play some jazz", "Turn it down a bit" and "Good night Mira";
- "Don't tell me the weather *today*";
- "Sorry, I meant Lisbon, not Porto";
- "I've got the dentist on Thursday", called an instruction.

This is the failure `eval/assistant_memory` found in September ("Address him as Sir", mined from
small talk), and the reason extraction now reads user turns only. It never missed anything.

**Fortuna invented nothing.** It missed two real standing instructions, three runs each:
- "Keep your answers short in the mornings, I'm never awake": nothing, at 0.83;
- "Never play country music, I can't stand it": nothing, at 0.63.

## A fair reading of KoboldCPP, and why it matters

The first run gave KoboldCPP 6 tokens. It began sentences ("This message…") and was scored on
"the" and "this": 75/144. That was a broken harness, not a weak model. With 40 tokens and the first
label it names taken as its answer, it scores 120/144. A comparison only counts when each side
gets a fair reading.

## Decision: Fortuna as a gate in front of the extraction, not a replacement

Fortuna's "nothing", by confidence:

```
conf >= 0.7: skips 63/144 turns, of which 3 held something
conf >= 0.8: skips 60/144 turns, of which 3 held something
conf >= 0.9: skips 45/144 turns, of which 0 held something
```

At 0.9, a turn Fortuna calls nothing is left out of the fold's input: 31 % of turns here, none
wrongly. Small talk can then never be mined, and a fold of only small talk needs no KoboldCPP
call at all. Fortuna's two misses scored under 0.9, so they still reach the extractor, which
caught them every time.

Next: build the gate into `assistant_memory.fold`, then rerun `eval/assistant_memory` before
and after.
