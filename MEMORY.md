# Memory system — how it works today

What the app actually remembers, where it lives, and why each piece is shaped the way it is.
This describes the **shipped** system; `MEMORY_PLAN.md` is the design/decision log from before the
split (its companion phases describe El-Iguana/companion, not this repo), and `PROMPT_BUDGET.md`
covers the one gap this system does not close.

Code: `services/assistant_memory.py`, the shared fold helpers in `services/memory.py`
(`needs_summary`, the speaker-labelled transcript, `_fold_chunks`, `fold_budget` from
`muninn.fold`), assembled in `services/prompt_builder.build_messages()`, wired in `main.py`
(`chat()` + `_maintain_memory` + `/api/assistant-memory` + `/api/sessions/{id}/sweep`).

## One pipeline (since the split, 2026-11-07)

This repo was split from El-Iguana/companion, which keeps the **companion** pipeline (a rolling
"story so far", a cross-session narrative `CharacterMemory`, semantic recall over an embedding
index, story memory). None of that is here. A bartender must remember *how to behave* and *what is
true*, and above all must not drift, so there is exactly one store:

| | |
|---|---|
| Module | `services/assistant_memory.py` |
| Store | `AssistantMemory` — directives + facts + task notes |
| Files | `data/u/<uid>/assistant/_shared.json` (the user's facts, all-bartender directives) + `{cid}.json` (her directives, notes, fold marks), since Memory Phase 7 #16 |
| This chat | `last_summarized_index` = how far the fold has read. Pinned facts moved into the shared facts (#16) |
| Per-session summary | none — `session.memory.summary` is never written or injected; a companion-era one is cleared on the next fold |
| Semantic recall | none |
| Route | `GET/PUT /api/assistant-memory` |

Why a separate store with no narrative is in `MEMORY_PLAN.md` (Phase 6): every leak found was
roleplay framing reaching an assistant through a shared path. Here there is no such path left.

## The store — `AssistantMemory`

**Memory belongs to the bartender (Memory Phase 7, #16, 2026-11-23).** Each library has two kinds of
file:
- `_shared.json`: facts about the user, which every bartender knows, plus directives marked for all
  bartenders (`scope: "all"`);
- `{cid}.json`: her own directives, task notes and fold marks.

`main._load_assistant_memory(cur, cid, pid)` returns one merged view, and
`_save_assistant_memory` splits it back, so the fold, the merge (`_store_fold`) and the modal work on
a single `AssistantMemory`.
- **Persona is a tag:** a fact may carry `persona_id` and is then shown only under that persona.
  Untagged facts are the user's. Switching persona no longer hides a bartender's instructions.
- **Migration:** the old `{cid}__{pid}.json` files are merged on her first load (deduped; fold marks
  take the max). The chats' pinned facts move into the shared facts. The old files are kept.
- **Deleting** a bartender removes `{cid}.json`; deleting a persona removes facts tagged with it.
- `main.memory_for_turn` is the one place that decides what memory puts in a turn's prompt, and
  `eval/memory_phase7` measures it.

Three buckets and nothing else:

| Bucket | What it is | Cap |
|---|---|---|
| `directives[]` | standing instructions to **obey** — "address me as Sir", "times in Eastern" | 20 |
| `facts[]` | things that are **true** about the user, known but not acted on | 40 |
| `notes[]` | terse task state — open threads, decisions taken | 12 |

`directives`/`facts` are `MemoryFact{text, origin: user|auto}`, so everything auto-written is
badged and revertible. `notes` are plain strings and are **rewritten whole** on each fold rather
than appended: a list of task state should *shrink* when the task is done, and an empty list is the correct answer
when nothing is outstanding.

**The instruction/fact split is the reason the pipeline exists.** Asked for "durable facts" as one
bucket, the model filed *"El Iguana prefers to be addressed as Sir"* as a fact — true, and stored
where nothing tells the assistant to do it. An instruction that is merely known is an instruction
that gets ignored. `extract()` requires each line to be prefixed exactly `INSTRUCTION:` or `FACT:`
and **drops an unprefixed line** rather than guessing, because guessing reintroduces the failure the
split exists to stop. It holds: `misfiled 0/15` across the eval.

**Injection (`block()`) puts directives FIRST and never truncates them.** Facts and notes yield to
the budget (`BUDGET_CHARS` 2400); directives
do not, because dropping an instruction to fit a budget changes behaviour with no error and nothing
in the transcript to explain it.

### Folding

`fold()` keeps a per-session high-water mark (`swept_sessions`) and reads the range in chunked
passes (`memory._fold_chunks`, `muninn.fold`), writing directives, facts and notes, never a narrative.
Two kinds of fold fire it. The chat flow folds at the `summarize_after` threshold, all but
`keep_recent` turns (advancing `session.memory.last_summarized_index` so the prompt cannot grow
without bound, while `session.memory.summary` stays **empty**: there must be no prose to inject).
A **sweep** folds **everything** not yet folded, the recent turns included, so nothing said in a
chat is left out of long-term memory once it ends. `max_new` (`auto_facts_max_new`) is spent
across **both** categories together.

**When a sweep runs** (since 2026-11-23; before, only a 5-minute timer in the browser did, and a
closed tab, a reload or a restart before it fired left the recent turns out):

| Trigger | Where |
|---|---|
| Leaving a chat: another chat, a new chat, any other screen | page, `ChatView.unmount` → `app.end_chat` |
| The hands-free conversation ends: sleep phrase, timeout, mic off | `voice.js` sends `pvoice:asleep`; page and desktop |
| The page or the desktop window closes (`pagehide`) | keepalive `POST /pdesk/sweep`, which outlives the page |
| Another bartender opened in the desktop window | `desktop.js` `openCharacter` |
| 5 minutes after the last reply, whatever the client did | server, `_schedule_sweep`, re-armed by every turn |
| Startup: recent chats (2 days) with user turns not yet swept | server, `start_sweep_catchup`, staggered |

Every sweep runs as a background job under the memory job's rules (one per chat at a time, a
reply cancels it) and one that gave way runs again a minute later. A chat with nothing new, or
with only the greeting, returns at once without asking the backend anything.

**A session copied from the roleplay app (or from a card that was once in roleplay mode) arrives
carrying a summary, and it is dealt with in two places.** Nothing in this app writes that field, so
the text is *frozen* at whatever the roleplay summarizer last produced — third-person narration that nothing will
ever rewrite, injected into every turn. Found on a live 51-turn session: 1851 characters of it,
opening with a hardcoded date and time, which is exactly what `NOTES_PROMPT` forbids `notes` from
containing. So `build_messages` **never injects `mem.summary`**, and `fold()` **clears the stored
copy**, but only once the fold has passed the point the summary covered, so nothing is discarded that
was not just read into directives, facts and notes. `session.messages` is untouched either way: this
deletes a recap, not history.

An **empty result is valid** and does not stop the fold; only a hard failure does.

### Three enforcements in code, because the prompt rule did not hold

Each of these is a rule that is present in the prompt, was ignored, and had to be made
deterministic. Measured with `eval/assistant_memory` — see below.

- **Extraction reads USER turns only.** On a transcript of pure small talk the extractor stored
  *"Address El Iguana as Sir"* as a standing instruction in **all three runs**. Nobody said it: the
  assistant says "Sir" out of politeness in its own replies, and its own manners were mined into an
  instruction to itself, which then injects into every later prompt. The prompt already forbade
  recording what the assistant said. Removing the material is not a rule it can ignore — it cannot
  mine what it cannot see.
- **Status notes are dropped (`_drop_status_notes`).** Notes are open threads; *"Jarvis is fully
  awake and operational"* is a status line that would still be sitting in the prompt next week, and
  *"El Iguana has issued the command to proceed"* is a transcript entry wearing a note's clothes.
  Matching is anchored to the **subject** — the assistant's name plus a state verb, or the user's
  name plus a speech verb — so real threads survive (`Jarvis needs the API key`, `Jarvis has not
  finished the migration`). A third pattern catches the subjectless case (*"System rebooted
  successfully."*), with the verb list kept tight so *"System migration still pending"* lives.
- **Notes that restate a directive or fact are dropped (`_drop_restated`).** Told not to repeat
  them, the model re-emitted *"Address Jared Revan as Sir"* and *"Use Eastern time instead of UTC"*
  as notes while both were already stored as directives. Overlap is scored against the **shorter**
  of the two strings, so a terse directive still matches a wordier restatement.

Dedupe is **string-level, not embeddings** (`_dedupe`): directives are short, literal and few, and
an assistant behaving correctly must not depend on an optional model being loaded. (Since the split
the image no longer bakes the embedding model at all.)

### The eval

`eval/assistant_memory/` — five labelled fixture transcripts, a runner, and `RESULTS.md` recording
every run. This exists because eyeballing single runs gave the **opposite** verdict to three runs, in
both directions: a fix declared working scored 0/3, and a leak recorded as unfixed scored 3/3.

```
podman cp eval companion-assistant:/app/eval          # eval/ is deliberately not baked into the image
podman exec -i companion-assistant python3 /app/eval/assistant_memory/run.py --runs 3
```

Four counters: `clean` / `misfiled` (instruction stored as a fact) / `forbidden` (stored something
that must never persist) / `missed`. Record the totals with any prompt change — a rate that moves is
evidence, a single better-looking run is not.

Current: `clean 13/15 · misfiled 0/15 · forbidden 1/15 · missed 1/15` (run 3, from `7/15 · 0/15 ·
7/15 · 4/15` at baseline).

## Failure posture

Every step degrades to nothing. A fold exception or an unreadable store yields no block and leaves
the chat turn untouched. Nothing in the memory path is allowed to raise into the hot path or fail a
reply. An `AssistantMemory` file that fails to parse starts fresh rather than 500-ing a chat turn.

## API and UI

```
GET/PUT /api/sessions/{id}/memory                 this chat's pinned facts
GET/PUT /api/assistant-memory?character_id=&persona_id=
                                                  directives[] + facts[] + notes[];
                                                  directives/facts round-trip by id, notes as lines
POST /api/sessions/{id}/sweep                     a chat ended: fold everything not yet folded; idempotent
POST /pdesk/sweep {session_id}                    the same, as a plain route for keepalive on page close
GET/PUT /api/settings                             summarize_after · keep_recent · summary_max_tokens ·
                                                  auto_facts_enabled · auto_facts_max_new
```

`summarize_after` / `keep_recent` / `summary_max_tokens` set the fold window and budget (the names
predate the split; no summary comes out of it). Settings → Memory labels them "Fold memory after",
"Keep recent verbatim" and "Fold length".

The Memory modal (chat ⋮ → Memory) has two tabs: *This chat* (pinned facts) and *Long-term*:
Standing instructions, Things it knows about you, and Where things stand (one note per line; empty
is correct). Auto-written entries carry an `AUTO` badge and are individually editable and deletable
— the correction surface for everything the extractor gets wrong, which is what `origin` is for.

## Known gaps

Most of these are addressed by **Phase 7** (`MEMORY_PLAN.md`, issues #15–#23), which is planned,
not built: memory that belongs to the bartender, folded per episode, recalled per turn.

- Long-term `facts` never expire; they stay until the user removes them. Whether they should decay is
  still open.
- **The prompt is budgeted by dropping old turns only.** Everything here has to fit the context
  window (Settings → Context window, capped at the backend's real limit) alongside the card and
  persona blocks. Since 2026-10-30 `build_messages` drops the oldest verbatim turns to fit,
  so history overflow no longer deletes the memory blocks. A *system block* too big on its own still
  overflows and still loses its front — the eviction order for that is in `PROMPT_BUDGET.md`, not built.
- **Work in progress still leaks into `facts`.** Measured — "We still need to finish the
  middleware" was filed as a durable fact despite a rule against it, where it will outlive its truth.
  Visible and deletable in the Memory modal; that is the current mitigation.
- **The same-sentence split is unsolved, and its fixture is noisy.** The durable half of
  "my friends Tom and Bill are visiting next month" is lost more often than not, and the fixture has
  scored 0/3, 0/3, 1/3, 0/6 and 2/3 on essentially the same code. It needs more runs per verdict than
  the rest of the suite, and any conclusion from three runs of it is worth very little. A described
  rule beat a worked example here (1/9 vs 0/6) — the opposite of the expression beats, and recorded
  in the module so it is not retried on the same reasoning.
- **There is no semantic recall.** Its store is small, literal and capped, so
  retrieval has nothing to add yet; if `facts` grows to its cap in practice this is where to look.
