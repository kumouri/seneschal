# The grounding restructure — a router, sub-routers, and the duplication kill

**Status:** `PARTIAL(the mode router, the mode files and the four sub-routers BUILT; the READ FIRST
injection BUILT; the reachability test, the InstructionsLoaded logger and phase 4 NOT built)`

- **Built in this release:** `seneschal/SKILL.md` is a thin mode router that dispatches, by an
  imperative read, to `seneschal/modes/{chat,brief,wrap,triage,ask,reminders,watch,dream,forge}.md`
  (Journal and Archive dispatch to their subagents, which were always separate skills). The
  sub-routers `seneschal/docs/CLAUDE.md`, `seneschal/references/CLAUDE.md`, `archons/CLAUDE.md` and
  `cockpit/CLAUDE.md` exist and the root `CLAUDE.md` names each of them. The standing-safety block is
  injected verbatim into the warm session's cold grounding (§4.4).
- **Not built:** the golden-question reachability test (§6.3); the host-side `InstructionsLoaded`
  load logger (§6.2 — a PR cannot ship it); phase 4, deletion (§7). The byte ratchet that stops the
  routers regrowing is the enforcement half and lives in `context-budget-spec.md` (§8).

**The ask this answers:** split the grounding into a router plus sub-routers, kill genuine
prompt-side duplication, and **do not lobotomise the assistant** — the third clause is the point of
the document.

---

## 1. The thesis

An assistant's grounding tends to become an encyclopedia re-read in full before every question,
including the ones it has nothing to do with. Every feature adds a paragraph to the file that is
always loaded, because that is the one place a new rule is guaranteed to be seen. The growth is
monotone and fast, so the fix is not a trim — a one-time cut of a file that grows every week buys a
few weeks. The fix is **progressive disclosure**: a root small enough to read every time, saying
*where things are*, plus a tree that loads when relevant.

**The restructure is worth doing because it changes where new prose lands**, not because it is a
diet. And the risk is that progressive disclosure fails **silently** — nothing turns red, the
assistant just stops knowing a thing — so the load-bearing half of this spec is §6, the detector.

## 2. The grounding surfaces

Four surfaces can land in the same context window on a single warm-session turn:

| Surface | Loaded how |
|---|---|
| root `CLAUDE.md` | **auto**, at launch, in full — `presence.py` spawns `claude` with `cwd` at the repo root |
| `seneschal/SKILL.md` | skill invocation, or an explicit read the grounding asks for |
| `presence.GROUNDING` (rendered from `GROUNDING_TEMPLATE`) | the cold-spawn message, by construction |
| `persona/persona.md` (falling back to `persona/persona.default.md`) | explicit read, same line of `GROUNDING` |
| nested `CLAUDE.md` files | only when a file in that directory is read (§3) |

**All four primary surfaces share one window.** That is what makes §5 a duplication problem rather
than a filing problem: the same contract restated in three of them is paid for three times, and —
worse — can disagree with itself inside a single prompt.

Two cost facts shape the design. Prompt bytes on a prompt-cached CLI are dominated by the **cache
write** at spawn, not by the re-reads, so a cold spawn pays full price for every grounding byte no
matter how short the session. And a warm session's median life is short, so the write is rarely
amortised. Shrinking what loads *unconditionally* is the only lever that moves the cold-spawn figure.

---

## 3. The load mechanics everything rests on

Verified against the Claude Code memory and skills documentation, and by probe (see
`scripts-subdirectories-spec.md` for the sentinel-string experiments):

1. **`CLAUDE.md` files at or above the cwd load in full at launch; files in subdirectories load on
   demand when a file in that directory is *read*.** The daemon's cwd is the repo root, so every
   sub-router qualifies.
2. **The trigger is the `Read` tool and nothing else.** Not a `Grep` hit, not a `Glob` match, not a
   Bash `cat`, not a `Write` of a new file there, not a mention in prose. Design as if only a `Read`
   counts.
3. **A turn driven by a skill gets no help from nested routers** — skills load by the skill
   mechanism (§4.2).
4. **Compaction.** The documentation says nested files are not re-injected after compaction; probed
   on current CLI versions, the harness *does* re-attach the matched set. Do not rely on either
   reading: a rule that must survive compaction belongs in the root.

### 3.1 Two traps this rules out

**`@path` imports are not progressive disclosure.** Imports expand at launch, so they help
organisation but do not reduce context. **Routing tables use backticked paths, never `@paths`** —
one stray `@` re-inlines a whole spec and the restructure evaporates with nothing going red. The
ratchet lints for it (§8).

**The skill re-attach cap.** After compaction, Claude Code re-attaches the most recent invocation of
each skill but keeps only its first ~5,000 tokens. A monolithic `SKILL.md` several times that size
loses most of itself on the first compaction — and what survives is whatever sits at the top, not
Dream, not the reference index, not the rules at the bottom of Chat. **That is a lobotomy a
monolithic orchestrator already ships**, and it is an argument for §4.2 that does not depend on cost
at all.

### 3.2 The hole — named, not papered over

**Progressive disclosure is a file-touching optimisation, and a warm chat turn often touches no
file.** "How's the cockpit doing?" reads nothing under `cockpit/`, so `cockpit/CLAUDE.md` never
loads. What covers such a turn:

- **The root routing table — yes.** It is in the auto-loaded root. Each row must carry *what lives
  there* **and** *read this before answering about it*.
- **Explicit "read X before answering about Y" — yes**, because they are rows in that table. One
  tool call at the point of use; the right trade.
- **Nested `CLAUDE.md` — no.** That is the hole.
- **The local RAG index — no, unless it indexes the docs.** Retrieval covers what was ingested;
  specs and references are not ingested by default. Until they are, it must not be counted as
  covering the hole.

So the root's floor is: **it must answer "where does this live" for every topic**, because on the
surface the assistant is used most, nothing else will.

---

## 4. What goes where

### 4.1 The root and its sub-routers

**The rule for the root:** everything a turn needs **before it knows what it is about to touch**,
plus the map.

| Stays in the root | Why it cannot move |
|---|---|
| What this repo is (a paragraph) | Frames every other read |
| Non-negotiable conventions (Git Flow, merge commits, never merge red, secrets, Markdown-is-canonical) | They bind turns that touch no directory |
| The deploy contract (the daemon runs `main`; merging deploys) | A `git checkout` is not a file read, so no sub-router can warn in time |
| A one-line-per-directory layout skeleton | The map |
| **The routing table** | §3.2 — on a chat turn it is the only thing that fires |

Keep the *skeleton*, not an annotated tree: a layout table that annotates every file is not a map,
it is the territory redrawn 1:1.

**A sub-router earns its own file** only if it governs a directory a turn can touch **and** holds
rules that would be wrong elsewhere:

| Sub-router | Why not folded into a sibling |
|---|---|
| `seneschal/docs/CLAUDE.md` | A spec index — one line per spec: its status and what it *decides*. Not a summary |
| `seneschal/references/CLAUDE.md` | A reader here is about to load a large reference and needs to know which; it also carries the subagents' `references/` convention (one convention is not a file of its own) |
| `cockpit/CLAUDE.md` | Several dependency worlds (the `cockpit` uv extras, npm, a deliberately stdlib-only breakglass) and the no-third-copy parity rule — wrong applied to `seneschal/scripts/` |
| `archons/CLAUDE.md` | A rule that exists nowhere else: an archon never writes a tracked file directly |
| `phone/CLAUDE.md` | Already existed; the Worker and the Android app have their own toolchains |

**Deliberately none:** `persona/` (small files, always read explicitly); `seneschal/state/` —
`state/README.md` already is its router; `seneschal/scripts/` — the module docstrings are the
authority, and `scripts-subdirectories-spec.md` records why a scripts router does not pay for itself
by moving things around.

**The mechanism fork.** `.claude/rules/*.md` with `paths:` frontmatter would also work and is more
precise (a glob, not a directory). **Nested `CLAUDE.md` wins on co-location:** a rule for a directory
is found by anyone who opens that directory, without having to know a config file exists elsewhere.
Precision is not what is scarce here.

### 4.2 `SKILL.md` needs a different answer — the read is a step, not a suggestion

A skill's rendered `SKILL.md` enters the conversation whole, on invocation, and stays for the
session. Supporting files are loaded only when the skill *reads* them. The skills documentation's
own guidance is to keep `SKILL.md` well under 500 lines. So **a mode's detail can only be loaded by
the orchestrator explicitly reading the file**, and the dispatch table must say so imperatively:

    | Dream | scheduled nightly consolidation | **Read `modes/dream.md` now**, then work its steps in order. |

— not "see also". A "see also" is a suggestion a competent-looking run can skip; a numbered step is
something a run visibly did or did not do.

`seneschal/SKILL.md` therefore carries only what every mode needs: the anchors, the execution
philosophy, the Advisor Chain, when to ask, the trigger→mode table with one imperative read per mode,
and the reference index. Each mode's body lives in `seneschal/modes/<mode>.md`. Journal and Archive
dispatch straight to `subagents/journal-steward/` and `subagents/message-archivist/`.

Two arguments, and **the second is stronger**: a Dream run stops paying for Chat's rules and vice
versa; and the re-attach cap (§3.1) stops silently discarding whatever sat below the fold, because a
router plus one mode body both fit inside it. **This is a bug fix that happens to save tokens.**

**What it does not fix:** a mode file, once read, is a tool result rather than a skill message, so
compaction may summarise it away with no re-attach. The mitigation is that a re-read is one cheap
call and the dispatch table itself stays inside the surviving re-attach window. Don't pretend the
split is free.

### 4.3 `GROUNDING` — the one surface where a pointer costs a tool call

On the root, `SKILL.md` and the persona a pointer is free: the model already holds a file-reading
tool and the alternative is paying for the prose every turn. **On `GROUNDING` it is not.** A rule
that became a pointer must be *noticed*, *chosen* and *fetched* before it can be obeyed — and the
rules living in `GROUNDING` are the ones whose violation is silent. So the test is not "is this worth
its bytes" but:

> **Does this rule have to fire without a lookup, on a turn that gives no cue to look?**

| Block | Verdict |
|---|---|
| Identity, channel, voice, clock | **Keep** — wrong from the first token if absent |
| The act-low / ask-high gate + "never claim a write that didn't land" | **Keep** — binds every turn |
| Reminder-ack | **Keep the trigger, point for the fields.** Keep *an ack the owner speaks must reach the store and `reminders_dequeue.py` this turn or it is lost*; field names are re-read at write time anyway (`references/reminders-policy.md`) |
| Store rate limits | **Point** — the cue is present: you are about to fan out reads |
| Daemon self-restart hazards | **Keep.** No cue exists; the wrong action is instant, self-erasing, and replays on every boot |
| Background jobs | **Keep one sentence, point for the flags.** Keep *never promise a future report without a job id* — the failure is a promise made in the same breath, so nothing prompts a lookup |
| Fable delegation / the Oikonomos governor's refusal | **Point** — the router hint, the `!fable` line and the refusal text are themselves the cue |

The general form of the asymmetry: **buy certainty where the failure is invisible; take the risk
where the failure reports itself.** A missed ack re-nudges the owner, which is visible. A rule whose
miss corrupts an unrecoverable record stays whole, verbatim, whatever it costs.

### 4.4 The one thing that moved the OTHER way — READ FIRST

Applied to what `GROUNDING` contained, §4.3's question answered "trim or point" every time. Applied
to something it did **not** contain, the same question returns the opposite verdict — the one place
this spec spends bytes rather than reclaiming them.

**The failure class.** A warm session volunteers something it has been explicitly told not to raise
cold — a closed topic, an announcement the owner has already heard and asked not to repeat. The
correction exists in the durable record, but only as a file the session would have to choose to
read. Meanwhile an auto-recalled memory rides into context for free and asserts the stale version.

> **Free-and-stale beats costly-and-correct every time, and both sound equally confident.**

That is not a discipline problem and cannot be fixed by telling the session to look harder. The two
sources are competing on price, not accuracy, and the stale one is free. The only fix that changes
the outcome is to **make the fresh source cost zero too**: the bytes arrive in the prompt whether or
not anyone chose to look. A prompt-side "read the digest first" is exactly the contract that fails.

**What ships.** `presence.py` renders `state/standing-safety.json` (maintained by
`scripts/standing_safety.py`, written by Dream) as a `## READ FIRST — standing safety items (do NOT
re-raise cold)` block and substitutes it into `GROUNDING` at **cold-spawn time**, ahead of the thread
tail and the owner's message. The properties, each a test:

- **Verbatim, not summarised.** A paraphrase of a safety correction is not the correction.
- **Capped**, with generous headroom over the typical block, and truncating on **whole lines** with a
  visible marker. The store is generated and nothing reviews it; uncapped, one runaway Dream run
  silently inflates every cold spawn.
- **Silent on every unhappy path.** No store, an empty store, an unreadable file — all render `""`,
  and the prompt is byte-identical to one without the feature. A fresh install has no store at all,
  so this is the common path on a new machine, not an edge case.
- **The literal grows by one placeholder.** A budget that measures the template literal cannot see
  the runtime bytes, so the real ceiling is *literal + cap*; record that beside the budget rather than
  raising the literal's budget to cover it.

**Two limits, stated rather than implied.** (1) **Only a cold spawn gets it.** A session that is
resumed rather than cold-spawned is sent a short preamble instead of `GROUNDING`, by design —
re-sending the block would spend exactly what resuming saves. The exposure is bounded by how old a
session may be and still be resumed: it was cold-grounded then and carried the block. (2) **The
interactive `/assistant` chat does not go through `presence.py`**, so this injection does not reach
it; that surface must read the store itself (`.claude/commands/assistant.md` and `modes/chat.md`).

**The dependency this creates.** The block's heading and the store's shape are a contract between
Dream (the writer) and `presence.py` (the reader). Before the store existed, the block lived only as a
convention inside `state/context-digest.md`, with nothing tracked asserting it — a Dream run that
renamed the heading would have turned the injection into a silent no-op. **The digest is retired**:
its jobs split into single-writer state files (`state/brief-prestage.json`,
`state/standing-safety.json`, `state/open-loops.json`) plus the Run Log, and the contract now lives in
`standing_safety.py`'s docstring and its tests.

---

## 5. Kill genuine prompt-side duplication

### 5.1 Drift is the argument

Search, don't remember: probe every prompt-side file for each contract's distinctive tokens, then
measure every hit. In a grounding that grew by accretion, the typical contract has several copies —
and the dangerous ones are not stylistic redundancy. **The copies disagree, and the wrong copy is
the one loaded closest to where the rule must be obeyed.** The recurring shapes:

- **A state set restated from memory.** A background-jobs contract restated in the orchestrator and
  in `GROUNDING` listing four terminal states when the code has five — two of three copies in the warm
  window wrong about the contract's central promise.
- **An asymmetric backstop.** The reminder-ack contract in the orchestrator required a
  journal-first durability step; the `GROUNDING` copy — the one the daemon's session, where most acks
  land, actually reads — never mentioned it.

That is the mechanism by which a third copy makes behaviour *worse*, not merely more expensive.

### 5.2 The ruling

> **The home is the surface that is loaded when the rule must be obeyed** — not the most convenient
> place to have written it.

Every copy resolves to one of four outcomes: **home** (one full statement, argued); **a one-line
invariant at the point of use** (§5.4); **a routing-table row** ("durable promises →
`seneschal/docs/background-jobs-spec.md`; read before starting long work"); or **nothing** — history and
rationale become nothing in the root beyond a routing row.

Where copies have drifted, the ruling **fixes** the drift rather than only deleting a copy: the home's
statement becomes the correct one and the retained one-liners are re-derived from it. Deleting the
wrong copy alone leaves the drift unresolved and merely quieter.

### 5.3 Deliberate mirrors that must NOT be collapsed

> A duplication is prompt-side repetition of the same text **into the same context window**. A mirror
> is the same fact held by **two processes that cannot read each other's files**. The first is waste;
> the second is an interface. A cleanup that cannot tell them apart will delete a working boundary.

| Mirror | What breaks if collapsed |
|---|---|
| `persona/persona.md` → `phone/src/persona.ts` | The voice on the phone — the Worker has no filesystem and no repo |
| `seneschal/scripts/model_config.py` / `governor.py` → `cockpit/server/` copies | The cockpit must not depend on the daemon's commit; it would fail to boot whenever the daemon's tree is mid-merge. `cockpit/server/test_parity.py` is the tripwire. A **third** copy (e.g. in the browser) is the one to delete — serve it from the Python table instead |
| `cockpit/server/jobs.py` written against `state/jobs/`'s on-disk shape | The cockpit acquires a dependency on the daemon's venv |
| `store/<backend>/schema.md` ← the live store's schema | Being a stale-tolerant local copy is the entire purpose — never fetch schemas per turn |
| `subagents/**/references/` | Skill-local; loads only when that subagent runs |

**The counter-example, so the test reads both ways:** `cockpit/breakglass/assertion.py` is **shared,
not duplicated**, between backend and supervisor — minter and verifier must agree byte-for-byte, so a
second copy would be a bug. A process boundary alone does not license a copy — **divergence-tolerance
does.** Where two copies drifting apart is survivable (a UI lagging a schema), mirror. Where it is not
(a signature), share.

### 5.4 Where a short restatement earns its bytes

All three must hold: **≤ ~150 bytes** (an invariant, not a contract); **at the point of use**, where
the surrounding text would otherwise read as licensing the opposite; and it **names its home**, so the
full rule is one hop away and drift is visible. Passing: *"act-low; see
`references/autonomy-policy.md`"* on a Chat rule; *"owner-local date, not UTC"* beside date
arithmetic; *"never as Claude"* in `GROUNDING`. Failing: any multi-kilobyte block re-typing a spec —
which is exactly how a copy comes to be missing a terminal state.

---

## 6. The risk — the lobotomy, and how to see it

### 6.1 What the failure looks like

Nothing goes red. CI is green — no test asserts a fact is *reachable*. The assistant answers, and is
simply, quietly, confidently wrong: re-nudges a reminder the owner acked, or promises a report on a
job it never started as one. **The symptom is indistinguishable from an ordinary model miss**, which
is why it can persist for weeks. Three mechanisms a restructure introduces:

1. **A pointer with no target.** A row names a spec that was renamed. Silent.
2. **A sub-router that never loads.** The turn never reads a file in that directory (§3.2). Silent.
3. **A fact that lands in no file at all** — a paragraph dropped in a reflow. **Undetectable
   afterwards.** This is the one §7's losslessness rule exists to prevent.

### 6.2 The cheapest honest detector — NOT BUILT (host-side)

Claude Code's **`InstructionsLoaded`** hook fires when a `CLAUDE.md` or rules file loads, with the
file path and a `load_reason` (`session_start`, `nested_traversal`, `path_glob_match`, `include`,
`compact`). One line per load to a local JSONL turns mechanism 2 from invisible into logged, at zero
model cost: which sub-routers **never** load; what fraction of warm turns load any; whether an
`include` ever appears (an `@path` import crept in). Two caveats: it is **host-side** (hooks live in
`~/.claude/settings.json`), so a PR cannot ship it; and it detects **loading**, not **using**. Probing
also showed it is sound for `path_glob_match` but not a complete census — it does not observe every
compaction re-attach.

### 6.3 Reachability, not comprehension — NOT BUILT

The design: a small set of golden questions whose answers live in different corners of the tree, each
row `{question, keyword, expected_path, answer_token}`, and a unittest that does a **keyword walk** —
grep the root routing table for `keyword`, assert it names `expected_path`, assert that file exists,
grep it for `answer_token`. Two hops, no model, runs in CI, costs nothing. Each check maps to §6.1:
the routing hop, mechanism 1 (the path exists and the root names it), mechanism 3 (the answer is still
in the file — the load-bearing one).

**Say plainly what it is.** It asserts a path from the root reaches the answer in ≤2 hops. It does
**not** assert that the assistant, mid-conversation, chooses to walk it. That is a real eval, with
model spend; reachability is the cheap floor that catches the otherwise-permanent failures.

**Guard the guard.** The set must be written **before anything moves, against the unsplit tree, and
pass there.** A set authored after the split describes the split, not the knowledge — it would go
green on a tree that had already lost something. When a later change relocates a fact, **the same
change updates the row**, and the test proves the answer survived the move. Verify each check by
re-introducing the failure it guards, not by reading the code. And when a test's content check fails,
don't print the haystack — the finding would arrive buried in the very document the test polices.

---

## 7. Phases — the first changes no behaviour

| Phase | What lands | Behaviour change | State in seneschal |
|---|---|---|---|
| **0** | The reachability set, passing against the unsplit tree; the load logger (host-side) | None | **Not built** |
| **1** | **Lossless relocation** of history and rationale out of the root into the spec that owns it | Root shrinks; nothing else | **Built** — the root is a router |
| **2** | Domain detail → sub-routers; the routing table names every sub-router; no `@path` imports | Sub-routers load on directory read | **Built** — `seneschal/docs/`, `seneschal/references/`, `archons/`, `cockpit/` (+ `phone/`) |
| **2.5** | `SKILL.md` → mode router + `seneschal/modes/*.md`, dispatched by imperative read | Mode bodies load by explicit read | **Built** |
| **3** | Duplication collapsed per §5.2, **correcting** drifted contracts at their home; `GROUNDING` per §4.3; the READ FIRST injection (§4.4) | Drifted copies stop disagreeing | **Built** for READ FIRST; the §4.3 test is a standing rule for every `GROUNDING` edit |
| **4** | **Deletion**, argued per item, from what phase 1 relocated | Facts leave the repo | **Not built — and may never be.** Relocated history in a `docs/` page costs nothing per turn; the phase exists so deletion is argued rather than drifted into |

**Phase 1 relocates; it does not delete.** A trim and a reorganisation reviewed together are
un-reviewable — the reviewer cannot tell a deliberate cut from a copy-paste casualty — and **the one
failure nobody can detect afterwards is a lost fact.** The one stated exception: a spec index cannot
move verbatim (a multi-kilobyte `docs/CLAUDE.md` is not a router, it is the same file with a worse
load rule), so it is **rewritten** to one line per spec naming the file, its status and what it
*decides* — a lossy step inside a lossless phase, safe only because everything beyond that line is by
construction already in the spec.

Phase 2.5 is separate from phase 2 deliberately: `SKILL.md` loads by a different mechanism (§4.2), so
bundling them would make one change whose halves fail differently and can only be rolled back
together.

### 7.1 Phase 2.5's live check

Half of phase 2.5's verification cannot happen in a build session: a Brief writes to the store and
sends mail, a Dream opens a PR. The structural half is checkable (every mode file exists, is named by
the router, is dispatched imperatively); the live half is the next scheduled run of each mode. The
failure is specific and silent: **a run dispatches, does not perform the read, and proceeds from
memory** — competent-looking output produced without the mode's rules. What to look for:

- **Brief / Wrap** — a Run Log entry that skips a step the mode file specifies.
- **Dream** — its steps are numbered; a run summary missing one is the tell.
- **Chat** — say *"I'm going to have lunch"*: an intention is not a report, so nothing is logged as
  done. The reminder-ack rule and "never promise a notification you haven't queued" are the other two
  probes.

**Mitigation that must ship with the split:** `GROUNDING` names `seneschal/modes/chat.md` directly.
Without it the warm session reads a router holding no Chat rules at all — the one version of this
failure that starts immediately rather than at the next scheduled run.

---

## 8. The enforcement half — deliberately not duplicated here

`context-budget-spec.md` is the CI ratchet (per-file byte budgets plus a dangling-pointer check) that
stops the routers regrowing. **This spec decides *what goes where*; that one decides *what stops it
coming back*.** Two things here are requests on it, not duplications of it: the `@path`-import lint
(§3.1) and the dangling-pointer check that §6.1's mechanism 1 leans on.
