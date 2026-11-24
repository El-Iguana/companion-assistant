# Memory Phase 7 scenarios

`podman exec companion-assistant python3 /app/eval/memory_phase7/run.py --runs 3` (5 scenarios × 3).
To measure a branch before it's merged, stage its code in a copy beside `/app`, without `data/`, and run
the copy's `eval/memory_phase7/run.py`: the runner imports the tree it sits in. Folds use the live
model (Cydonia-24B), on a scratch library.

| scenario | issue | what it checks |
|---|---|---|
| cross-session-thread | #17 | an open thread from chat A survives the fold of chat B |
| shared-basics | #16 | a fact told to Mira is known to Rex; Mira's own instruction is not |
| expiry | #20 | "coming to stay next month" leaves the prompt after its date |
| recall-precision | #21 | with 70 facts (over the 2,400-character budget), the newest relevant one is in and unrelated ones are out |
| lately-with-this-bartender | #18 | a new chat knows the answer from her last conversation |

## Baseline, before Phase 7 (2026-11-23)

```
TOTAL 2/15   cross-session-thread 2/3 · shared-basics 0/3 · expiry 0/3 · recall-precision 0/3 · lately 0/3
```

- **cross-session-thread 2/3:** one run in three, folding chat B dropped chat A's DNS thread or never
  wrote B's own.
- **shared-basics 0/3:** Rex has never heard of Porto.
- **expiry 0/3:** after 75 days the prompt still says "Tom and Bill are visiting next month".
- **recall-precision 0/3:** the newest fact ("Ana is vegetarian") is the one dropped to fit, every
  run, while the car, the passport and the bank are in. With 30 facts it fit; the silent drop
  needs a store over the budget, so the scenario has 70.
- **lately 0/3:** a new chat starts blank.

## After #16, memory belongs to the bartender (2026-11-23)

```
TOTAL 5/15   cross-session-thread 2/3 · shared-basics 3/3 · expiry 0/3 · recall-precision 0/3 · lately 0/3
```

Shared basics pass. The rest is unchanged, as it should be: those are #17, #18, #20 and #21.
