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

## After #17, notes changed by number (2026-11-23)

```
TOTAL 9/15   cross-session-thread 3/3 · shared-basics 3/3 · expiry 3/3 · recall-precision 0/3 · lately 0/3
```

- **cross-session-thread 3/3** (was 2/3). Chat A's thread now survives chat B's fold in every run.
- **The first wording of the change prompt scored 0/3.** It kept "not what was merely discussed"
  from the rewrite prompt, and the model answered NONE to "I'll need to buy pasta sheets on Friday".
  Saying plainly that something the user still has to do *is* a thread, and that DONE needs the
  activity to say it's finished ("a different topic is not a reason"), fixed both halves.
- **expiry 3/3, but not for #20's reason.** The visit was stored as a task note, and notes leave
  the prompt after 14 untouched days. A visit stored as a *fact* would still fail here. #20 (fact
  lifetimes) is still to do, so this pass is partly incidental.

The fold eval on the same code (`eval/assistant_memory`, 5 × 3): clean 9/15, misfiled 0/15,
**forbidden 0/15**, missed 6/15. Forbidden is the best yet; missed is the same extraction misses as
before.

## After #18, "lately with this bartender" (2026-11-23)

```
TOTAL 12/15   cross-session-thread 3/3 · shared-basics 3/3 · expiry 3/3 · recall-precision 0/3 · lately 3/3
```

A new chat knows the end of her last one ("the Memmo Alfama"). It sits after the history, never in
the system block. Only recall precision is left, which is #21.

## After #20 (fact lifetimes) and #21 (tiered recall) (2026-11-23)

```
#20:  TOTAL 12/15   (unchanged: its scenario already passed through note staleness; unit tests cover it)
#21:  TOTAL 15/15   cross-session-thread 3/3 · shared-basics 3/3 · expiry 3/3 · recall-precision 3/3 · lately 3/3
```

`recall-precision` gained a second check when #21 was built, because the first could pass on
ordering luck: the newest fact lands in the always-on core. "When does my car insurance renew?" must
bring up an *old* fact ("renews in June") through recall, without unrelated old ones (the film, the
dentist). Both checks pass in all three runs.

From 2/15 to 15/15 over #16, #17, #18, #20 and #21.
