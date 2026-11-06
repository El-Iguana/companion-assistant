# Shared memory core — review and plan (Pantheon + Rhapsode)

Status (2026-11-04): **Phase 1 done for Pantheon** (the move, no behaviour change; see Progress at
the end); Phase 0 fixtures in progress. Spans two repos: Pantheon (`companion/`) and
Rhapsode (`ai_story_maker/`). Pantheon's shipped mechanics stay documented in `MEMORY.md`,
`RELATIONSHIP.md` and `PROMPT_BUDGET.md`; this file covers the review and the plan to pull the
reusable parts into one package both apps use.

## Verdict

- **Pantheon's memory is solid and well measured.** Every rule in it traces back to an observed
  failure. Its weak points are structural: no medium-term layer, a long-term store that is
  rewritten from scratch on every fold, memory and relationship state that never refer to each
  other, and memory budgets fixed in characters whatever the context window is.
- **Rhapsode has about 5% of that system, copied once and since drifted.** One rolling
  summary per session, no token fitting, no per-NPC memory, one emotion read shared by every NPC
  in the scene, and a relationship descriptor that still uses the wording Pantheon replaced in
  `88701cf` because it did nothing.
- **The drift itself is the argument for modularity.** Every fix Pantheon has made since the
  port (pronouns, boundary phrasing, the trailing boundary note, chunked folds, token fitting)
  is missing from Rhapsode. A shared package ends that.

## What exists today

| Tier | Pantheon (companion) | Pantheon (assistant) | Rhapsode |
|---|---|---|---|
| Short: verbatim turns | token-fitted (`context.fit_history`) | same | **last 30 messages, no token limit** |
| Short: scene state | `Where things stand:` line inside the summary | none (by design) | same line inside the summary |
| Session pins | `MemoryState.pinned` | same | none |
| Medium: session summary | rolling, chunked, tunable | none (by design) | rolling, **not chunked**, fixed constants |
| Medium: episodes | **none** | none | **none** (the arc `ledger` exists but the story prompt never sees it) |
| Long: pairing summary | `CharacterMemory.summary`, 10 bullets, rewritten each fold | `notes` | **none** |
| Long: facts | user + auto, deduped by embeddings, 40 cap | directives + facts | **none** |
| Recall | BGE int8, margin-gated, user turns only | none | **none** |
| Relationship | per (char × persona), persistent, milestones, escalation, pronouns, boundary note | off | per (NPC × persona), session only, **old wording, no boundary note, no milestones** |
| Eval | `eval/assistant_memory` | same | none |

## Findings

Ranked by how much each costs in play.

### Both apps

1. **Memory and relationship don't know about each other.** The descriptor can say "trust: high"
   but nothing records *why*. Milestones store only an id (`first_kiss`), not what happened. The
   character knows they trust you without remembering anything that earned it, which is what
   makes long relationships feel generic. The emotion classifier already detects the turns that
   matter (large axis swings, milestone crossings), but nothing keeps the text of those turns.
2. **Memory budgets don't scale with the window.** `LONGTERM_BUDGET_CHARS = 2400` is labelled
   "at 8192 context" and stays 2400 at 32K or 128K. At 4K it is about 15% of the window. The
   system block has no budget at all: pins, lorebook, `mes_example` and the character definition
   are unbounded, and only history is trimmed (`PROMPT_BUDGET.md` steps 2, 4 and 5 are still
   open). With priority-based filling, a bigger window would also mean more memory, not just more
   history.

### Pantheon

3. **There is no medium-term layer.** A finished chat ends up in 10 bullets plus at most 3 auto
   facts. Everything between "verbatim" and "a bullet" is lost, except user turns in the recall
   index. The per-session summary, the best record of the chat, is never kept as an episode. It
   only gets folded into the 10 bullets.
4. **The long-term summary erodes over time.** Each fold rewrites the whole summary from the
   previous one plus new events. After many sessions, early material survives only if the model
   keeps choosing it, and the REPLACE rule pushes it toward dropping things. Facts stop the worst
   of it, but only for the user's facts.
5. **The character's own canon isn't remembered.** `LONGTERM_PROMPT` and `AUTO_FACT_PROMPT` both
   exclude what the character says, which was the right call against noise ("noted, stored,
   remembered"). The cost: whatever she makes up about herself (a sister, a hometown, a scar)
   isn't kept, so she can contradict it next session. Recall can't help either, because only
   user turns are indexed.
6. **Facts carry no time or salience.** `MemoryFact` has `text` and `origin` only. There's no
   created date, last-confirmed date or source session, so eviction can only go by "oldest auto",
   and decay (an open question in `MEMORY_PLAN.md`) has nothing to work from.
7. **The companion prompts have no eval.** `MEMORY_PLAN.md` already says this. Any restructuring
   below needs fixtures first, or there's no way to tell an improvement from a regression.

### Rhapsode

8. **The prompt can probably overflow, and nobody would know.** History is capped at 30
   *messages*, never tokens, and the reply reserve is `paragraphs × 400 + 200` (1,400 tokens at
   3 paragraphs). Twelve to 24 unsummarized messages of multi-paragraph prose, plus the
   world/characters/summary/relationship system block, is close to 8K, and over it whenever the
   summary job falls behind. KoboldCPP cuts from the **front** without an error, so the world,
   the characters, STORY SO FAR and the relationship block go first. *Not measured yet*: step 1
   of Phase 2 is to measure it.
9. **The summary pass isn't chunked.** A backlog (failed jobs, a long imported session) renders
   one transcript, and if it overflows, the fold instructions are what get cut.
10. **Every NPC in the scene gets the same emotion read.** One `emotions.classify` over the whole
    narrator reply is applied to every NPC in `active_npc_ids`. In a scene where the ally comforts
    the player while the villain gloats, both NPCs move the same way. Per-NPC reads are listed as
    deferred in `STORY_RELATIONSHIP_PLAN.md`.
11. **NPCs have no memory of their own.** The only memory is one narrator summary. An NPC has
    relationship numbers but no record of what the player did to them, so they can't bring it up.
    This is the biggest gap for the "Perchance RPG" feel the relationship plan is aiming for.
12. **The relationship port is stale.** It still says `willing up to "{tier}" and no further`
    (the null instruction Pantheon replaced with concrete acts plus the first refused rung), has
    no trailing boundary note, no `Pronouns`, and no milestones or escalation.
13. **The narrative engine and the story memory are separate worlds.** Quest outcomes go into
    `Arc.ledger` (SQLite) with a `summary`, but `prompt_builder.build_messages` never reads them.
    The narrator can forget a completed quest once it falls out of the summary.
14. **The "who's in the scene" check is loose.** It's a substring match on names in the last
    3 messages ("Al" matches "also") and falls back to the first NPC. There's also no NPC↔NPC
    state, so the "relationship matrix" is player × NPC only.

## Target architecture: one package, three profiles

A pure-Python package, **Muninn** (after Odin's raven of memory; `../muninn`), with **no imports from either app**. It
follows the same rule `rhapsode/` already follows ("imports nothing from the app") and the
injected-dependencies pattern in `world-builder/docs/cross-repo-patterns.md` §10.

```
muninn/
  deps.py          # MemoryDeps: generate(prompt, params) -> str, embedder (optional), clock, log
  scope.py         # Scope(owner_id, subject_id, kind)  — (char, persona) | (npc, persona) | (world, session)
  budget.py        # context window, token estimate, PRIORITY assembler (sections, min/max, drop policy)
  fold.py          # chunked fold engine + high-water marks (today's _fold_chunks / swept_sessions)
  store.py         # MemoryStore protocol + JsonStore (Pantheon) + SessionStore (Rhapsode)
  model.py         # Fact, Episode, Summary, SceneState, RelationshipEvent — pydantic
  recall.py        # embedding index + margin gate (today's index_add / index_search)
  extract.py       # reluctant extractor + dedupe + code-enforced filters
  relationship/    # ONE engine: axes, stages, ladder, Pronouns, descriptor, boundary_note, milestones
  profiles/
    companion.py   # Pantheon roleplay prompts and policy
    assistant.py   # Pantheon assistant prompts and policy — still its own module, see below
    narrative.py   # Rhapsode: narrator summary, per-NPC memory, scene state, ledger bridge
  eval/            # fixture runner shared by all profiles
```

**The "module, not branch" decision stays.** Profiles share mechanics (fold, budget, store,
recall, extract) and never prompts or policy. That is the import rule `assistant_memory.py` follows
today (it reuses `_fold_chunks`/`_transcript` and nothing else), applied to the whole package:
`profiles/*` may import the core, the core never imports a profile, and profiles never import
each other.

### Core interfaces (sketch)

```python
class MemoryDeps(Protocol):
    async def generate(self, prompt: str, params: dict) -> str: ...
    embedder: Embedder | None          # None -> recall and semantic dedupe degrade to string match
    def context_window(self) -> int: ...

class Profile(Protocol):
    name: str
    def fold_prompt(self, ctx: FoldContext) -> str: ...
    def extract_prompt(self, ctx: FoldContext) -> str | None: ...
    def filters(self) -> list[Callable[[str], bool]]: ...   # code-enforced rules
    def render(self, mem: MemoryView, budget: Budget) -> list[Section]: ...

async def maintain(deps, profile, store, scope, transcript_source, *, upto) -> bool: ...
def assemble(sections: list[Section], window: int, reserve: int) -> AssembledPrompt: ...
```

`Section` = `(name, text, priority, min_chars, max_share, drop: "never" | "truncate_tail" |
"drop_oldest_items")`. `assemble()` replaces `fit_history` plus the separate per-block caps, and
reports what it dropped, which covers `PROMPT_BUDGET.md` step 6 (visibility).

## The tiers, redesigned

### Short term (this scene)
- **Verbatim window**, token-fitted by `assemble()` in both apps.
- **`SceneState`**, structured, replacing the free-text `Where things stand:` line: location,
  who is present, time of day, current goal and open beat. Written by the summary pass (a short
  JSON tail, validated in code, falling back to the old line if it doesn't parse). In Rhapsode it
  also replaces `active_npc_ids`'s guesswork: present = `SceneState.present`, plus names
  word-boundary-matched in the latest turn.
- **Pins** (Pantheon has them; Rhapsode gets them).

### Medium term (recent sessions): new
- **`Episode`**: `{scope, session_id, span, summary, participants, salience, created_at}`. Every
  fold chunk produces one at no extra cost, because the summary pass already reads that
  transcript. A short "what happened in this stretch" line is added to its output.
- **Retrieved, not always injected.** The last N episodes for the scope by recency, plus the
  top-k by recall similarity, inside the memory section's budget. This fills the gap between the
  verbatim window and a 10-bullet summary.
- Capped per scope (e.g. 200), evicted lowest-salience-oldest first, never ones that are
  linked to a relationship event (below).

### Long term (the pairing)
- **Facts gain metadata**: `subject` (`user` | `self` | `world`), `origin`, `created_at`,
  `last_seen_at`, `source_session`. Re-detecting a fact (dedupe hit) bumps `last_seen_at`, which is
  enough to support a decay or "stale?" UI later without deciding decay now.
- **`self` facts**: the character's own canon, extracted from **her** turns by a separate,
  strictly filtered prompt ("what did {char} state about their own life, history or body?
  NONE is a good answer"). This keeps today's ban on recording her acknowledgements and offers;
  self-description is a different category. Behind a setting and evaluated before it ships
  (finding 5).
- **Consolidation instead of rewrite-every-fold**: the long-term summary is rebuilt from
  `facts + top episodes` on the idle sweep only, not on every threshold fold. Fewer rewrites means
  less erosion (finding 4), and the input is structured instead of last summary + raw transcript.
- **`RelationshipEvent`**, the bridge between memory and relationship (finding 1): when an axis
  moves past a threshold in one turn (e.g. |Δ| ≥ 3) or a milestone fires, store
  `{axis, delta, milestone?, snippet, episode_id}`. The snippet is the user's turn plus one line
  of the reply, trimmed. The descriptor then gets **one** grounding line: "{char} trusts {user}
  because: {most recent salient event}". Cheap (no extra model call), and it gives the
  numbers a story.

### Rhapsode specifics (`profiles/narrative.py`)
- **Per-NPC memory** = the same `Fact`/`Episode`/`RelationshipEvent` stores, scoped
  `(npc_id, persona_id)`. Only on-scene NPCs are rendered, each with a small budget (about
  3 facts + 1 event), the same way relationship blocks are scoped today.
- **Per-NPC emotion read** (finding 10): one classify call per *present* NPC, asking how that
  NPC feels about the player in this reply. Costs N calls, so capped (≤ 3 NPCs, highest salience
  first), and NPCs not mentioned in the reply don't move.
- **Ledger bridge** (finding 13): completed or failed quest `LedgerEntry.summary` becomes a
  `world` fact plus an episode for the questgiver's scope.
- **Scope of persistence** stays session-scoped by default, as decided in
  `STORY_RELATIONSHIP_PLAN.md`. The store abstraction makes an opt-in "this NPC remembers you
  across adventures" a config flag rather than a rewrite, but it isn't proposed now.
- **NPC↔NPC matrix**: out of scope for v1. The `Scope` shape allows `(npc, npc)` later.

## Packaging

- Its own git repo at `ai_ecosystem/muninn/` (GitHub: `JotunheimPress/muninn`), installed into both images as a **named build
  context**, the way Pantheon already pulls wapyt (`COPY --from=wapyt-git`). Pin a tag in each
  app so a change in one app's needs doesn't silently ship into the other.
- Host dev: `uv pip install -e ../muninn`.
- The embedder stays optional. Rhapsode works without BGE (string dedupe, no recall); baking the
  33 MB model into Rhapsode's image is a separate decision.
- Storage stays app-native: JSON files per scope for Pantheon (same paths, so no migration), and
  JSON inside `StorySession` for Rhapsode. SQLite next to `rhapsode.db` only if episode volume
  needs it.

## Phases

Each phase ends with tests passing and, where prompts change, an eval run recorded the way
`eval/assistant_memory/RESULTS.md` does it.

0. **Fixtures first.** Companion-profile fixtures (fold, auto-facts, recall), and a Rhapsode
   fixture set (long adventure, multi-NPC scene with opposing reactions). Without them, nothing
   below can be called an improvement (finding 7).
1. **Extract with no behavior change.** Move `context.py`, `_fold_chunks` + high-water marks, the
   recall index, `_dedupe_facts`, `embeddings.py` and Pantheon's `relationship.py` into
   Muninn. Pantheon imports them, and its tests and eval stay identical. This is the riskiest
   step and must be a pure move.
2. **Rhapsode on the core (the overflow and drift fixes).** Measure the real prompt size first
   (finding 8). Then token-fit history, chunk the summary, switch to the shared relationship
   engine (new boundary wording, trailing boundary note, pronouns, milestones), and drop the
   copied `services/relationship.py` and `services/memory.py`.
3. **Priority assembler** in both apps (`PROMPT_BUDGET.md` steps 2, 4, 5, 6): memory budgets as a
   share of the window, never-evict list kept (system prompt, persona, relationship descriptor,
   boundary note).
4. **Episodes + RelationshipEvents** (medium tier, the memory↔relationship bridge). Pantheon
   first, since the eval exists there.
5. **Rhapsode narrative profile**: per-NPC memory, per-NPC emotion reads, SceneState, ledger
   bridge.
6. **Long-term consolidation + fact metadata + `self` facts**, each behind a setting and each
   measured.
7. to 11. **Reinforcement, a rare-word dedupe veto, hybrid recall, a recall reranker, and a
   sectioned long-term page.** From the 2026-11-06 external review; see "Next phases" below.

## Decisions carried over (don't re-open)

From `MEMORY_PLAN.md` and `RELATIONSHIP_PLAN.md`; the package keeps all of them:
assistant memory as a separate profile with directives never dropped; user turns only in the
recall index; margin gate, not floor; dedupe in code; auto entries evicted before curated ones;
drop oldest, never truncate mid-block; best-effort everywhere (memory never fails a turn); scenario
sessions excluded from pairing memory; no idle/time decay for relationships; context slows pace and
never adds compliance; boundary phrasing and trailing placement are load-bearing.

## Open questions

- Whether Rhapsode gets the embedder (+33 MB image, CPU only).
- `self` facts on by default, or opt-in per card?
- Per-NPC emotion reads cost one model call per present NPC. Is the cap 3, or is it a setting?
- Should a Pantheon character imported into Rhapsode bring its pairing memory along (read-only
  copy, like cards today)? It's possible once both use the same store shape, but it's a product
  call.

## Next phases: from the external review (2026-11-06)

Muninn (at `f2560be`) was compared against outside memory systems: **claude-mem**
(`thedotmack/claude-mem` v15.10.1, a Claude Code plugin, cloned to `~/Development/claude-mem`),
**GBrain**, Karpathy's **LLM Wiki** pattern and Google's **Code Wiki**, plus the options for the
recall layer. None of them replaces Muninn. Each solves a different problem (agent memory for
coding tools, or a knowledge base an agent browses with tool calls), while Muninn puts memory
straight into an 8–16K prompt for a local model, every turn, without ever failing the turn. A few
of their parts do fit, and they are planned below. claude-mem is Apache-2.0: ported code keeps a
credit line.

Every phase follows the README's rules: a change to `tests/data/golden.json` is decided on purpose
and explained in the commit, and anything that changes what is stored or recalled gets an eval
run on the same model as the baselines (Cydonia-24B), in `eval/companion_memory` and
`eval/story_memory`.

7. **Reinforcement: repeats keep a memory alive.** *(Completes Phase 6's "fact metadata".)*
   - **The gap.** The consolidation fold no longer sees stored memory, so when a chat restates
     something already remembered, `consolidate.append` drops the repeat and records nothing.
     Past `LONGTERM_KEEP_BULLETS` the oldest bullets go, so a detail raised in every chat is
     evicted on age alone, exactly like one mentioned once. Erosion is still 2/3. The same
     applies to `episodic.add_notes` (oldest auto note first) and Pantheon's auto facts.
   - **The mechanism** (claude-mem `src/services/reinforcement/`, about 220 lines): each item
     keeps the days it was re-confirmed, at most 10, oldest dropped first. A second confirmation
     on the same day does nothing. Strength is
     `ln(1 + age_created^-0.5 + α·Σ age_reinforced^-0.5)`, the ACT-R power-law decay. Nothing is
     deleted by decay; strength only decides eviction order.
   - **The change.** `facts.dedupe_facts` already computes which stored item each repeat
     matches. It returns that match (index and score) as well as the new items, and callers
     reinforce the matched item instead of discarding the signal. Eviction becomes "weakest auto
     item first", with the newest quarter protected (claude-mem's `recencyHeadSize`). Curated
     entries (`user`, `quest`) still outrank every auto entry, as decided.
   - **Storage.** Bullets need a little structure: `{text, created, seen: [dates]}` beside the
     rendered `summary`, read as plain bullets when the field is missing, so no migration. `Note`
     and `MemoryFact` gain `seen`. This is the `last_seen_at` the long-term tier section asks for.
   - **Measure.** Erosion and missed details on `eval/companion_memory` with `--consolidate`.
     Expect no change in a single chat: the effect only shows across several.

8. **A rare-word veto on dedupe.** *(Ships with 7; both change `facts.dedupe_facts`.)*
   - **The gap.** A fixed cosine cutoff (0.86) can merge items that differ in one important
     word: "plans a trip to Lisbon" and "…to Porto", or two notes that differ only by a name.
     That is silent data loss, now in three callers. Rhapsode has no embedder, so it only
     catches exact repeats and misses rewordings.
   - **The mechanism** (claude-mem `src/services/dedup/`, about 60 lines): IDF-weighted cosine
     over whitespace tokens (compound names stay one token), and a veto when any word present on
     only one side is rare across the store. It requires at least 2 shared tokens before trusting
     the score. claude-mem validated it on 7,651 work-log titles, not chat facts, so the
     thresholds are ours to set.
   - **The change.** Run the veto after the embedding check, using the scope's own items as the
     document set. Without the embedder, use the TF-IDF cosine as the reword check, so Rhapsode
     gains it with no model.
   - **Measure.** Add dedupe fixtures (true rewordings, and near-misses that differ by a name or
     place) to the golden set, then check that auto-fact and note counts on both evals don't drop.

9. **Hybrid recall: keyword matching beside embeddings.**
   - **The gap.** Embeddings are weakest on what roleplay depends on: names, places and invented
     words ("Biscuit", "Captain Imra", "the Vael Ridge").
   - **The change.** A keyword index (BM25) next to the `.vec.npz` index for the same entries,
     the two ranked lists merged by reciprocal rank fusion, then the existing gate. Pantheon can
     keep both in its per-scope files. Rhapsode can use SQLite FTS5 in `rhapsode.db`, which makes
     recall possible there without the embedder and partly answers the open question about the
     33 MB model.
   - **Not a vector database.** At about 2,000 vectors per pairing, a direct numpy search takes
     well under a millisecond. Chroma, pgvector or Qdrant would add a service and gain nothing.
   - **Measure.** The recall check in `eval/companion_memory` (relevant found, irrelevant leaked),
     with fixtures built around names and places.

10. **A reranker for the recall gate.**
    - **The gap.** Eval bug 5: with small talk in the index, 7/11 irrelevant queries leak. The
      margin gate was calibrated on a set with no small talk.
    - **The change.** Score the top ~20 candidates from 9 against the query with a small
      cross-encoder (`bge-reranker-base` class, int8 ONNX, CPU, baked like the embedder), and
      gate on its score instead of the margin. Optional like the embedder: missing model, old
      gate.
    - **Cost.** One CPU pass over 20 short pairs per turn, before the reply starts. Measure
      latency next to the chat model, which already uses CPU threads (see the SNAC thread cap
      in `CLAUDE.md`).
    - **Measure.** Same recall check, with the small-talk fixtures that exposed bug 5.

11. **Long-term memory as a page with sections.** *(From the LLM Wiki pattern.)*
    - **The idea.** Store the long-term memory as a short page with fixed sections instead of one
      flat list: *About {user}*, *About {char}* (her own canon, finding 5), *Shared history*,
      *Open threads*. The fold appends bullets under a section, the review already acts as the
      wiki's "health check", and caps are per section, so a burst of history can't push out what
      she knows about you.
    - **For Rhapsode** the same shape fits world lore and NPC memory that carry across adventures
      (still opt-in, as decided), next to `world-builder`.
    - **Depends on** 7 (per-item metadata) and the `self` facts decision. Prompt changes, so it
      needs its own eval run, and it ships behind a setting first.

Folded into existing phases:

- **Phase 3 (priority assembler):** claude-mem's `fitContextToBudget`
  (`src/services/context/ContextBudget.ts`, about 100 lines) is a proven first version of
  `assemble()`. Render with a pure function, measure, drop the next item on a fixed ordered list,
  render again, and return what was dropped (`PROMPT_BUDGET.md` step 6). Items are always removed
  whole. Build that loop first and add per-section shares on top.
- **Phase 4 (episodes):** give each episode a one-line context header before embedding it ("In
  the chat about the move to Porto…"), which improves recall cheaply. When a recall hit is an
  episode, also offer its neighbours (claude-mem's search → timeline → details), within the same
  budget.
- **Embedder swap:** only if an eval says so. EmbeddingGemma-300M and Qwen3-Embedding-0.6B are
  the candidates. Both are larger than BGE-small, so the gain has to show on Cydonia.

Order: 7 and 8 together, then 9, then 10. 11 waits for 7 and the `self` facts decision.

### Evaluated and not adopted

So these don't get re-opened without a new reason:

- **claude-mem's architecture**: hooks on every event, a background worker, an LLM pass per tool
  call, Chroma. Wrong shape for a library that never touches app storage. Its "modes" (memory
  types and prompts in JSON) are the shared-path-with-config design that Phase 6 of
  `MEMORY_PLAN.md` moved away from.
- **GBrain** (MIT, April 2026): markdown in git, indexed in Postgres + pgvector, used by agents
  over MCP. It assumes an agent that searches its own memory with tool calls, and it adds a
  Postgres service. Our models get memory placed in the prompt.
- **LLM Wiki as a system**: navigating pages by index files needs a strong agent model and
  costs extra calls per turn. Only its page shape is taken (11).
- **Code Wiki** (Google, preview): documents code, not characters, and only public repos are
  supported.
- **A vector database** for recall: see 9.

## Progress

### Phase 1: the move (Pantheon), done 2026-11-04

- `../muninn` is its own repo (private on GitHub as `JotunheimPress/muninn`). It contains `context`,
  `fold`, `recall`, `facts`, `embeddings`, `emotion`, `models` (`Relationship`,
  `RelationshipPreset`) and `relationship`.
- Pantheon imports them. `services/context.py`, `memory.py`, `expressions.py` and
  `models/schema.py` re-export under the old names. `services/relationship.py` and
  `services/embeddings.py` are deleted, and `main.py` and `bff/chat.py` use
  `from muninn import relationship`.
- **No behaviour change, proven two ways.** First, `muninn/tests/data/golden.json` is the
  pre-move output of Pantheon's code over a seeded grid: 80 relationship traces with every
  descriptor and boundary text, every tier × tension band, context fitting, 40 fold splits,
  dedupe, recall add/search/evict, and the recall constants. Muninn reproduces it exactly, and
  so does Pantheon's own call surface. The test was mutation-checked: changes to `RATE`,
  `_TIER_ACTS` and the recall gate are all caught. Second, all 55 Pantheon tests pass unchanged.
- Rhapsode depends on Muninn too, as of the reasoning fix: the same Containerfile stage and
  `run-podman.sh` build context, pinned at `1d8986b` like Pantheon. Its 133 tests pass, and
  `localhost/ai-story-maker:muninn-test` builds.
- Image: a `muninn-git`/`muninn-src` stage pinned by `MUNINN_REF`, the same pattern as wapyt.
  `run-podman.sh` builds from `../muninn` when that directory exists. The install sits after the
  baked models so that a Muninn change doesn't re-download them. The test image
  `localhost/companion:muninn-test` builds and runs, and the embedder works inside it. **Until
  `JotunheimPress/muninn` is pushed, compose builds (which clone it) fail.**
- The companion eval run inside that image (same model) matches the baseline: the recall gate is
  identical, and the LLM rates are within noise (clean 6/33 in both runs). Details in
  `eval/companion_memory/RESULTS.md`.

### Phase 2: Rhapsode on Muninn, first part done

- History fitted to the window (`muninn.context`, with 0 = Auto for the context setting and a
  reserve that counts the client's three continuation rounds), and the summary folded in
  context-sized passes (`muninn.fold`). In the eval's prompt-size check, every configuration
  now fits the window, runaway replies included, and fits at 8K too.
- The relationship engine, models and emotion vocabulary are Muninn's, and the copied
  `services/relationship.py` is gone. The math was compared with the old copy over random
  sequences and is identical; only the descriptor's wording changes (concrete acts, pronouns).
  NPCs got a `pronouns` field (blank = they/them, filled by the Pantheon import).
- The trailing boundary note (gated to NPCs where intimacy has begun, attraction is notable,
  or the player reaches for it) and milestones are in. In the eval the note primed no romance
  in ordinary scenes. NPCs at rung 0 refused with or without it in short scenes, so its benefit
  in long stories is still to be shown.
- Per-NPC emotion reads (finding 10): each NPC the reply is about is read for how they feel
  toward the player, and NPCs absent from the reply no longer move. Off-list answers map to the
  nearest label (`muninn.emotion.normalize`). In the eval, ally and villain now move in
  opposite directions in most scenes, where they never could before; a one-line reaction
  behind someone else's long speech can still read neutral.
- NPC memory (`muninn.episodic`, `muninn.names`): moments recorded when a turn moves an NPC's
  relationship sharply or a milestone fires (no model call), and notes extracted per NPC at
  fold time, each from only the turns that NPC witnessed. That restriction is enforced in code:
  the prompt rule alone let NPCs remember scenes they weren't in. In the eval, NPCs bring up a
  remembered event when asked almost every time with their memory in the prompt, and never
  without it.
- Ledger bridge (finding 13): the engine calls an optional `on_outcome` hook once per recorded
  quest outcome. Rhapsode copies it into a session quest log, shown to the narrator in every
  mode, and into the questgiver's memory as a note that extracted notes can't push out. The
  engine still imports nothing from the app.
- Scene presence (finding 14): a stateless tracker over the last few messages. Whole-word
  names, role titles ("the captain"), exits respected until the NPC is named again, and nobody
  present when the player is alone among several NPCs. This replaces substring matching and the
  first-NPC fallback. It was checked against held-out cases written before it ran, as well as
  the original ones.

### Pantheon: relationship grounding (finding 1)

- Moments (`muninn.episodic`) are recorded in `CharacterMemory.moments` when a reply moves the
  relationship sharply or a milestone fires, the latest is appended to the relationship note,
  and the Memory modal lists them for removal. Not for scenario or assistant chats.
- Found along the way and fixed: the Memory modal's Save was rejected (400) for chats
  without long-term memory, a memory edit saved while a reply streamed was overwritten by the
  turn, and a long-term edit made during a fold was overwritten by the fold.

### Phase 6: long-term consolidation, done 2026-11-05, on by default

- `muninn.consolidate`: append-only folds (new bullets only, deduped in code) and a review that
  only deletes, by number, at most half the list per pass. Pantheon wires it behind
  `AppSettings.longterm_consolidate`; the review runs on the idle sweep once per memory state.
- First version (eval `--consolidate`, 33 runs): fewer forbidden and format failures than the
  rewrite, but more missed details (10 vs 6). Fixed by not showing the fold the stored memory,
  filtering non-note lines, and reading only the review's final `DELETE:` line. Then: clean
  16 vs 9, forbidden 11 vs 20, format 6 vs 13, missed 5 vs 6, erosion 2/3, so it is on by
  default. Details in `MEMORY.md`, Consolidation.
- Also: a reply now cancels a running memory job (the summary pass was delaying the next
  reply's first token by up to a minute).

### Phase 0: fixtures and baselines, done 2026-11-04

Both baselines are on `Cydonia-24B-v4.3-absolute-heresy.Q8_0` (16K context). The backend was
switched from Rocinante-X-12B partway through, and the prompts were tuned on a Qwen 9B. A
baseline only holds for one model, so later comparisons must use the same one.

- **Pantheon** (`eval/companion_memory/`): clean 6/33, forbidden 24/33, format 17/33, missed
  7/33. Recall gate with durable turns only: 12/14 relevant found, 2/11 irrelevant leaked. With
  three small-talk turns in the index, **7/11 irrelevant leaked**. Four fixtures need 6 or more
  runs per verdict.
- **Rhapsode** (`ai_story_maker/eval/story_memory/`): summary facts kept 159/225, and
  first-fold facts erode to 50/75 by fold 2. One stand line at the end in 13/25. Divergent
  NPC reactions 0/15. NPC presence 2/16. Prompt overflow at 16K: 0/24 configurations with
  normal replies, 16/24 with runaway replies (10/24 and 24/24 at 8K). The ledger never reaches
  the prompt.

New bugs the evals found (reported, not fixed; candidates for the next phases):

1. ✅ **Fixed 2026-11-04 (Muninn `1d8986b`, `muninn.text.strip_reasoning` in both apps'
   `generate_answer`).** Replayed over every stored baseline output, 33/33 tagged outputs come out
   clean and 0/90 clean outputs change. Reasoning written with no tag at all, such as italic
   `*I'll update the memory…*` asides, echoed prompt rules and pasted transcript (9 of the 33),
   needs per-profile output checks; that's the next item. **Measured on the live image:** stored tags
   9 → 0, reasoning flags 22 → 9, forbidden 22/33 → 18/33.
   ✅ **Untagged reasoning: output checks added 2026-11-04 (Muninn `e1f75a8`, `muninn.checks`)** in
   Pantheon's rolling summary, long-term fold and auto-facts, and in Rhapsode's summary. They drop
   task talk, whole-line italic asides, echoed prompt rules and pasted transcript. Replayed over
   every stored baseline summary (159 summaries, 1595 lines): 116 lines dropped, all reviewed as
   non-memory, with no reasoning pattern left. Not yet measured live. Assistant memory keeps its
   own filters (prefixed lines only). **Reasoning stored as memory (both apps).** `generate_answer` pre-fills a Qwen-style empty
   `<think></think>`. Neither model loaded today follows that, so reasoning text plus a stray
   closing tag get saved into summaries and facts. Cydonia did this in 15 of 39 runs.
2. ✅ **Fixed 2026-11-04 (Muninn `e95d7b1`, `muninn.summary`, in both apps' `summarize`).** The
   clients now report a reply that used its whole `max_length` (from `completion_tokens`, since
   KoboldCPP's `finish_reason` is unreliable). A cut-off last line is dropped, exactly one stand
   line is kept and put last, and a missing line is asked for alone in one short call (final pass
   only). The prompt is unchanged. Not yet re-measured. **The 400-token summary cap cuts off the
   `Where things stand:` line** in both apps
   (Rhapsode 13/25 capped; Pantheon's second fold lost it 5/9), and the cut summary is saved.
3. **Unchecked summary output (Rhapsode):** planning preambles and RPG tags get stored as canon,
   and 3/25 folds replaced the whole memory with a single stand line.
4. **Auto-facts:** a "NONE" that isn't the first word gets stored as a fact (Rocinante: 21 of 41
   facts were junk). On Cydonia extraction answers NONE almost always (3 facts in 27 folds).
5. **Recall gate vs. small talk:** greetings longer than 20 characters get indexed, and then
   greeting queries match them. The gate was calibrated on a set with no small talk.
6. **Off-list emotion labels become neutral** (Rhapsode: "contempt" → no movement).
7. **Rhapsode replies degenerate** into word salad over about 10 turns on Rocinante, which is
   what drives the overflow. This is one session, so not yet a rate.
