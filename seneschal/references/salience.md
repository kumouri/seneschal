# Salience learning — "what's safe to forget" (observe-only)

The assistant decides, every turn, what to persist vs. let go — with **no feedback loop** on that
judgment. This experiment builds the loop: **instrument the judgment, log the evidence, and only much
later let evidence inform pruning — never auto-delete.** Mechanics + how to read the data:
`../scripts/SALIENCE_SETUP.md`.

The two core commitments:

- **Salience = frequency × emotional-weight, not frequency alone.** A rare-but-heavy fact (a birthday, a
  bereavement in the family) must survive pruning that a pure access-count would greenlight. Phase 2's
  sentiment signal is load-bearing, not garnish.
- **Observe-first, exactly like the shadow Router.** Run the mechanism, log to gitignored local caches,
  change **zero** behavior. Anything that would actually discard/forget a memory is **act-high** —
  draft-and-hold through `proposed-learnings.md`, resolved by the owner, reversible-first.

## The taxonomy (closed vocabulary)

Tagged at write time (Dream's ingest pass) so the Phase-3 rollup can analyze *kinds*. Closed like the
Router's category set: an unknown tag is coerced to `unknown`, never passed through raw. Machine copy:
`SALIENCE_CATEGORIES` in `../scripts/rag_common.py`.

| Category | What it is | Disposable-eligible? |
|----------|------------|----------------------|
| `ephemeral.ack` | "watered the plants", "did that" — a consumed acknowledgment | ✅ (near-certain) |
| `logistics.transient` | one-off scheduling detail now past ("moved 3pm to 4") | ✅ |
| `status.snapshot` | point-in-time state a later query supersedes | ✅ |
| `identity.core` | birthdays, names, relationships, a family bereavement | ❌ **never** |
| `commitment.durable` | a promise/deadline that matters until resolved | ❌ |
| `preference.standing` | "I do my wind-down in the evening" — proposed-learnings material | ❌ |
| `unknown` | the safe default when Dream can't classify | ❌ |

`identity.core` is present-but-never-disposable **by design**: it encodes the frequency×weight thesis.
Those items will show near-zero access frequency (nobody re-asks a core family fact weekly) yet must
never be pruned — the Phase-3 report must visibly demonstrate that protection or the model is wrong.

## The `disposable` ladder (chunk rows in the RAG index)

| Value | Meaning | Retrieval behavior |
|-------|---------|--------------------|
| `0` | normal | returned as always |
| `1` | **predicted-disposable** — Dream's logged prediction "I won't need this again" | **returned as always** (a real memory is never hidden by a prediction) — and its recalls are counted, which is the experiment |
| `2` | **approved-forgotten** — a soft prune the owner explicitly approved (Phase-4+, gated) | excluded from answers (as if deleted, next-best backfills), **still counted** — post-prune demand is the un-forget signal |

Nothing in the codebase writes `2` automatically; it exists so the first approved prune is reversible
(flip the column back) and observable before anything is ever hard-deleted.

## Dream's tagging rubric (ingest time, step 2b)

When Dream writes the nightly ingest JSONL, each record may carry `salience_cat` and `disposable`:

1. **Classify** the entry into exactly one category above; when torn, `unknown`.
2. **Predict** `disposable: 1` only when (a) the category is disposable-eligible **and** (b) Dream
   genuinely expects never to need the entry again (a consumed ack, a superseded logistic). The
   prediction is *about the future, logged now, scored later* — the shadow-Router shape.
3. **Never force it.** Untagged records are fine (`unknown` / not-disposable is the default). A wrong
   `keep` costs nothing; the experiment only needs *enough* predictions, not total coverage.
4. Ineligible-category predictions are cleared at index time with a warning (`rag_index.py`) — belt and
   braces under rule 2a.

## Forgetting events — the emotional-weight axis (Phase 2)

The access counter alone would greenlight pruning anything rarely queried — including the unforgettable
(nobody re-asks a core family fact weekly). The second factor comes from **forgetting events**: one
appended line in `../state/forgetting-events.jsonl` each time the assistant *failed to recall or surface
something and there was a reaction* (schema: `../state/README.md`).

**Write triggers — all in the reasoning loop, all organic:**

1. **Reactive (primary).** The owner's message reads as a reaction to a miss ("you forgot X", "you never
   remember Y", visible frustration about a lapse). This is the high-value, negative-weight case.
2. **Self-noticed (secondary).** The assistant catches its own failure to surface something that mattered
   (a deadline unflagged) — logged at lower confidence, `sentiment_source: assistant_inferred`.
3. **Never proactively fished for.** The assistant does **not** ask "did that upset you?" — it logs
   reactions that occur naturally, never interrogates for sentiment, and never surfaces a running "how
   upset were you" tally back at the owner.

**Weighing:** the warm Opus session assigns the signed weight (−1..+1) **in context** at log time — only
it knows "you forgot a memorial anniversary" is a 10 and "you forgot to move my 3pm" is a 2. The owner's
own stated weight always overrides. Offline, Dream runs `scripts/sentiment.py` (local `qwen3.5:4b`,
mirrors `router.py`, abstains to 0.0 when Ollama's down) over each new event's `reaction_text` as an
independent **cross-check** — divergence is a data-quality flag in the rollup, never an override.

**How it protects:** at analysis time (Phase 3), any memory/category with an associated event of
`|sentiment| ≥ θ_protect` (default **0.7**) is **ineligible for a disposable recommendation, full
stop**, independent of access counts. That single clause is `salience = frequency × weight` made
operational — `identity.core` sits at frequency ≈ 0, weight ≈ max, and stays.

## The access signal (what the counters mean)

`rag_query.py::search()` bumps `salience_access` for every hit scoring ≥ the access floor (default
0.55). The counter means **"returned in top-k above the floor"** — a *relative* comparator between
categories, deliberately not ground truth of use (returned ≠ used; the bias is uniform, so comparisons
hold). The gold standard it is calibrated against is the **memory-ablation A/B** (Phase 4c): answer a
sampled turn with and without the memory, the owner judges which is better and why.

**Analysis reads must pass `--no-record`** (or `record_access=False`) so the rollup's own queries and
ad-hoc debugging never pollute the counters.

## The ablation A/B — the human-judged oracle (Phase 4c)

Access counts measure *demand*; sentiment measures *cost of forgetting*; neither proves a memory
**improved an answer**. The ablation A/B does, and it's the ground truth the cheap proxies are
calibrated against — pilotable by hand from day one:

1. On a suitable recall turn, the assistant generates the response **twice** — once **with** the
   retrieved memory in context, once with it **withheld** — presented as A/B with the labels randomized
   (blind).
2. The owner picks the better answer **and says why.** The verdict scores the memory (`with` = it earned
   its keep; `without`/`tie` = disposability evidence); the *why* is labeled data for **what about**
   the memory mattered (Phase 4a's question).
3. The judgment is logged durably via `scripts/ablation_log.py` → `state/ablation-judgments.jsonl`
   (schema: `../state/README.md`), and the weekly `salience_rollup.py` folds the verdicts into its
   report — including under abstention, since human ground truth is meaningful from judgment #1.

**Friction rules:** always on the owner's request; offered unprompted at most rarely, never as a quiz
they didn't invite, never mid-flow on something urgent. Sampled and on-request only — the A/B never
replaces a normal answer without the owner opting in, and nothing about it changes live retrieval.

## Cold start / kill criteria

Empty tables and zero tags are the expected phase-1 state — accumulation *is* the phase. The rollup
abstains ("insufficient data") until **≥30 days and ≥50 disposable-tagged entries**. If after the window
all disposable categories cluster at the same near-zero hit count, the retrieval-touch proxy is too
coarse — that's a **kill signal** for the access axis (lean on sentiment), not a reason to instrument
the reasoning loop.
