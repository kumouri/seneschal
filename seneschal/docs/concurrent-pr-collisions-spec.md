# Spec — concurrent pull requests: name the overlap before the tap, repair the wreckage after the merge

**Status:** `PARTIAL(phase 1, 2R, 2b-as-a-job, 2a's detection rows and §5B BUILT as modules; phase 1.5 and 2a's report unbuilt; §7 held pending 2a)`
— the design below describes the whole system. What exists: `pr_overlap.py` + `merge_guard.overlap_band`
(phase 1), `check_context_budget.py`'s `reconcile()` (§1.4's measure-don't-sum rule, report-only),
`pr_rebase.py` (2R), `pr_repair.py` (2b as a delegated job, plus 2a's detection rows in
`state/pr-repair-log.jsonl`) and `job_pr_draft.py` (§5B). **The daemon's call into `pr_repair.sweep`
arrives with the presence port** (the PR-watch task in `presence.py`, §5A.9); until then the pass runs
on demand (`python seneschal/scripts/pr_repair.py --dry-run`). §7 is a decision about the approval gate
that belongs to the owner, and it is **held**, not answered (§7.6).

**Scope:** one stdlib module per phase, their tests, one new band in an existing picker, and — for
phases 2 and 2R — one call each inside the daemon's existing PR-watch task. **No format change. No
migration. No new dependency. No change to the approval gate** — and §7 is the one place that would
be, which is why it is held rather than decided.

**Where this sits.** The context-budget ledger (`seneschal/context-budget.json`, checked by
`check_context_budget.py`) is the one file two prose PRs conflict on **by construction**: every PR
that adds a router row, a spec, a script entry or a rule file raises some artifact's budget entry, and
a hand resolution of that conflict can silently drop history. This spec is the same collision seen
from the other end: not *what the ledger should validate*, but **what the owner is told, and what is
repaired for them, when two pull requests are in flight at once.**

**Configuration.** Which repository the repair acts on and which branch is "the base" are never code.
`repo_config.py` resolves them from `../references/pr-guard.json` (per-install, gitignored), else the
shipped `../references/pr-guard.example.json`, else git itself: the repository is this checkout's
`origin`; the base is `develop` when `origin/develop` exists (Git Flow), else the branch
`origin/HEAD` names, else `main`. Everything below that says "the base branch" means that value.

---

## The short answer, up front

- **Ordering cannot prevent this conflict. It can only decide who pays for it.** Whoever merges second
  rebases, in every ordering, because both PRs raise the **same artifact's** budget entry. Any design
  that reads as *"merge them in the right order and there is no conflict"* is wrong before it starts.
- **Phase 1 is pure information and ships alone.** Before a merge-approval picker goes out, name the
  other open PRs whose changed files intersect this one's — **inside the existing question's body**,
  never as a second question and never as a third option. It states the overlap, a fact; it does not
  predict a conflict, which would be a claim about a merge nobody has run.
- **Phase 2 acts, so it is deliberately narrow.** It repairs only file shapes on a named allow-list,
  refuses everything else with one notice, **recomputes** the byte number from the merged tree rather
  than merging two hunks, **never merges**, and lets the existing picker path **re-ask at the new
  head**.
- **There are TWO repair classes.** A pull request can be perfectly mergeable and still need a rebase,
  because its CI verdict was computed against a base that no longer exists. That class is cheaper —
  **one server-side rebase, no worktree, no clone** — and a watcher keyed on `DIRTY` skips it entirely
  (§1.6).
- **The auto-rebaser's danger is the feature working.** It moves heads; a moved head kills a live
  approval; and it fires every few minutes. **It must not move a head the owner has already tapped,
  and if it cannot tell, it does not move** (§5A.3).
- **The residual is a second tap, and it is not the assistant's to remove.** §7 lays out both sides
  and stops; the owner holds it pending measurement (§7.6).

---

## 1. The failure class

### 1.1 What happens, in order

Two delegated jobs finish minutes apart, each from the same base. Each opens a PR; each goes green;
each gets a merge-approval picker, and the owner taps Approve on both. The first merges. Within
seconds the second is `mergeable: CONFLICTING`, `mergeStateStatus: DIRTY`. Its conflict is resolved
(by hand or by a job), which moves its head — and **the approval, bound to the old head SHA, is now
void.** The owner taps a second time for a pull request they already approved, and the picker for the
second PR gave them no way to know the first existed.

Pickers reach the owner through two doors — `merge_guard.py request` (agent-callable) and
`merge_guard.ask_on_green` (the resident sweep). Whatever phase 1 does has to be true of **both**, or
it will be absent from whichever one the next pair of jobs happens to use.

### 1.2 The two PRs are not dependent, and that is the finding

Nothing in either PR needs the other. They collide for one structural reason:

> **Both added a row to a router document, and therefore both raised THE SAME artifact's entry in
> `seneschal/context-budget.json`.**

Every PR that adds a router row, a spec, a script entry or a rule file must edit that one ledger, so
two such PRs conflict by construction. On a busy day this is not rare: several PRs going
`CONFLICTING` on the same two or three files (the guard, its router, the budget ledger) is the normal
shape, three-way rather than pairwise.

### 1.3 Ordering cannot prevent it. It only decides who pays.

This is the sentence that disciplines every design choice below, so it is stated as a rule:

> **There is no merge order in which neither PR rebases.** The second one to merge always must, because
> the conflicting hunk is the *same key in the same object*. What an ordering decision buys is not
> avoidance — it is the choice of **which branch does the rebase work**.

Corollary, and it is the one a well-meaning implementation gets wrong: a design that promises to
sequence merges "to avoid conflicts" has promised something unachievable and will be judged, correctly,
as broken the first time the second PR still needs a rebase.

### 1.4 The correct post-merge number is neither side's, and it is not the sum — it is measured

Take an artifact at base size *B*. PR 1 adds *d1* bytes to it, PR 2 adds *d2*. After both land, the
right `max_bytes` is **the merged file's measured size** (LF-normalised — a CRLF checkout overstates
every file by its line count). Neither *B + d1* nor *B + d2* is the answer, which is the obvious half.

The non-obvious half: for purely additive edits in disjoint regions, *B + d1 + d2* **equals** the
measured size — and that agreement is arithmetic, not luck. It is the **expected** outcome of a clean
resolution, so nothing is learned from the two numbers matching. **The informative event is the
MISMATCH, and its DIRECTION is a diagnosis:**

| measured vs. (base + declared deltas) | what it means |
|---|---|
| **equal** | clean resolution — nothing lost, nothing duplicated. The expected case. |
| **BELOW the sum** | the resolution **DROPPED** bytes: a hunk kept from one side only, or an entry claiming bytes that never landed. **This is the silent-drop failure the ledger exists for.** |
| **ABOVE the sum** | the resolution **ADDED its own**: a reword, a re-indent, a row kept from both sides. |

The additive shortcut is right **only when the resolution keeps every byte of both sides and
introduces none of its own — a property the resolver cannot know without measuring**. So the measured
number stays the answer and the sum stays a claim about it. Both failure modes are unlikely in a
rebase, and **rare + silent + green is the worst combination available**: nobody catches a
one-in-fifty event by remembering to look, while a subtraction catches it every time and costs
nothing.

**BUILT — a property the tooling checks, not a discipline.** `check_context_budget.py`'s `reconcile()`
computes *(merge-base bytes) + (sum of the `raises[]` entries this branch ADDED)* and subtracts the
measured working-tree size, naming the direction and what it implies. It is scoped to artifacts whose
`raises[]` gained entries, **report-only** (a gate red on the day it lands is a gate someone
disables), and it **abstains, loudly and by name**, on an unreachable merge-base, a newly created
artifact, a deleted one, and a `raises[]` chain that is itself discontinuous.

The reference behaviour any automation must reproduce is the careful hand resolution: keep **both**
`raises[]` entries, re-chain the second onto the first (each `from` equals the previous `to`), set the
last `to` and `max_bytes` to the measured size, and write the tail entry's `reason` naming which PR
landed first and what this branch's own delta is. **The work is entirely mechanical, which is the whole
argument for §5.**

### 1.5 A shared path is not a conflict

`git merge-tree --write-tree --name-only <merged> <other-head>` on a typical pair names **two** shared
paths — the router document and the budget ledger — and reports a conflict in **one**: the ledger.
The two router rows land in different regions of the table and git merges them unaided; the ledger
conflicts because both PRs edit the *same key* in the same object with no blank line for git to anchor
on.

So a file-list intersection names more shared paths than there are conflicts. That is not a defect to
engineer away; it is the honest accuracy of the cheap signal, and §4.2 decides what to do about it.

### 1.6 Two repair classes, not one

The same pull request can need two repairs that are **not the same kind of event.**

**Class 2 — genuinely conflicted.** A sibling merges and the PR goes `CONFLICTING` on shared paths.
The repair is a private worktree, a real merge, conflict resolution, and a push. §5 addresses this.

**Class 1 — the head is fine; the VERDICT is stale.** The PR is mergeable, but its CI ran against a
base that no longer exists. Its verdict may say red where today's base would say green (a failure the
base has since fixed) or green where today's base would say red (a break a sibling introduced). With
branch protection's "require branches to be up to date" on, GitHub computes exactly this as
`mergeStateStatus: BEHIND`.

> **The trigger is not *"is it conflicted"*. It is *"is this head's verdict still a statement about
> today's base"*.** A watcher keyed on `mergeable: CONFLICTING` / `mergeStateStatus: DIRTY` alone
> skips class 1 entirely.

### 1.7 The class-1 repair costs one server-side call

`gh pr update-branch --rebase <n>` — or the GraphQL `updatePullRequestBranch` mutation with
`updateMethod: REBASE` it wraps:

- **No worktree, no clone, no local checkout.** GitHub performs the rebase server-side. One API round
  trip.
- **`--rebase` is load-bearing**: the bare command updates with a *merge commit* instead, putting the
  base branch's history into the PR's own diff.
- The rebased head keeps the author date and gets a new committer date — the signature of a rebase.
- **It force-updates the branch.** That is not a footnote; it is the whole hazard of §5A.3.
- CI re-runs **fresh** against the new base, and the resident sweep asks once it is green.

**It cannot do the class-2 job**: a server-side rebase has no conflict resolution — it either produces
a clean rebase or it fails. So the two classes want different machinery, and only one of them is
expensive. §5 is the class-2 machine; **§5A is the class-1 machine**, specified separately because it
shares nothing with §5's resolver except the word "repair".

---

## 2. The machines this sits between

| Machine | What it does | What phases 1–2 may not disturb |
|---|---|---|
| `merge_guard.py` `request_argv` | **The one place the approval picker is spelled.** Both `request` (agent-callable) and `ask_on_green` (automatic) build their argv through it | The question body is where descriptions live; `--no-recommendation` is passed, deliberately, so the assistant never recommends its own merge |
| `merge_guard.py` the `PreToolUse` hook | Stage 1 fails **open**, stage 2 fails **closed absolutely**. `record_approval` has exactly one caller, the daemon's Telegram callback | The hook's import graph (§4.1's lazy-import rule) |
| `pr_sweep.py` | The daemon's PR-watch pass, every ~3 min: `gh pr list` per watched repo → green non-draft candidates → `ask_on_green`; red ones → one notice via `pr_red_notify.py`. **It decides one thing** — is CI terminally green | Its single-decision character, its burst bounds, and its per-repo fail-open |
| `job_worktree.py` | One private worktree per delegated job, cut from `origin/<base>` spelled in full; removal is **never** `--force` | The rule that a refused removal is a **leak**: recorded and named, never forced |

Two constants bound what a picker may say:

- **`telegram_ask.py`'s question body is not chunked.** A body over 4096 characters would have to be
  split away from its own keyboard. Anything phase 1 adds is inside that ceiling and must bound
  itself (§4.4).
- **`merge_guard`'s docs-only allow-list is deliberately tiny** and pinned by a test; its membership
  test is *"changing it cannot change what runs."* **This spec proposes no change to it** (§9).

---

## 3. The problem, sharpened

1. **The owner is asked to approve PR 2 with no way to know PR 1 exists.** An *information* failure;
   phase 1 fixes it.
2. **The repair after PR 1 merges is hand-written, and entirely mechanical.** A *labour* failure;
   phase 2 fixes it.
3. **The repair moves the head, so the approval dies with it.** A *gate* question; §7 refuses to
   answer it on the owner's behalf.
4. **A pull request can be mergeable and still be stale** (§1.6). A *detection* failure; §5A fixes
   it — the cheap half of the labour failure, split out because it shares no machinery with it.

A fifth thing is **not** a failure and must not be treated as one: **PR 2 going `CONFLICTING` is
correct.** GitHub told the truth about a real divergence. Nothing here tries to prevent the conflict;
§1.3 already established that nothing can.

---

## 4. Phase 1 — overlap detection at picker time

**Ships alone and is useful alone.** It sends no new message, adds no option, changes no gate, and
writes nothing. If phase 2 is never built, phase 1 still means the owner taps Approve on PR 2 knowing
PR 1 is sitting beside it.

### 4.1 Where it lives — `merge_guard.py`, not `pr_sweep.py`

**The computation belongs to a stdlib sibling, `pr_overlap.py`; the *decision to show it* belongs to
`merge_guard.request_argv`.** Three reasons, in descending force:

1. **`request_argv` is the one place the picker is spelled, and both doors come through it.** A
   warning bolted onto the sweep would be absent from the `request` door — so the question the owner
   reads could depend on which door asked it.
2. **`pr_sweep.py` refuses to hold policy.** It decides one thing and hands over everything else; a
   second classifier or a second dedupe there is a bug waiting to drift. What another PR touches is
   policy about what a question says, so it goes where the question is built.
3. **The sweep's list is the wrong set anyway — the decisive reason.** `pr_sweep.candidates()`
   filters to green, non-draft PRs. The overlap set must include **drafts and red PRs**: a draft that
   touches the same file collides the moment it is marked ready, and CI colour has no bearing on
   whether two diffs touch the same path. Reusing the sweep's rows would **silently under-report**,
   the worst available failure for a warning.

**The hook path may not acquire a new import.** `merge_guard.py` is a `PreToolUse` hook, and an
`ImportError` at module scope would break it before `main()` can apply either failure polarity. So
`pr_overlap` is imported **lazily, inside the ask path only**, never at module scope.

### 4.2 The signal — file lists, and the false positive is accepted on the record

| | **File-list intersection** (`gh pr list --json files`) | **Merge-base test** (`git merge-tree`) |
|---|---|---|
| What it costs | **one `gh` call per picker sent** | a fetch of every other PR's head ref, plus a merge per pair |
| What it needs | `gh` and nothing else | a local clone — the daemon's deploy checkout, which must stay clean |
| Answer on a typical pair | 2 shared paths | 1 conflicted path — exact |
| False *"will conflict"* | **yes** (§1.5) | no |
| False *"won't conflict"* | none for content conflicts: a content conflict requires a shared path | none |
| Catches a **semantic** break (A renames, B calls) | no | no |

**Decision: the file-list intersection. The false positive is accepted, and it is neutralised by
wording rather than by engineering.** A claim is about *our evidence*, never the world:

> **The picker states the overlap. It does not predict the conflict.**
>
> ✅ *"PR 2 also changes `seneschal/context-budget.json` and `seneschal/docs/CLAUDE.md`."*
> ❌ *"merging this will conflict PR 2."*

With that wording there is no false positive left to accept, because nothing false was said. **A test
pins the absence of predictive wording** against the literal strings
(`OverlapStatesTheFactNeverThePredictionTest`).

The merge-base test is not rejected forever; it is rejected *here*. If it is ever wanted, it belongs in
phase 2's repair, which already has a private worktree and has already fetched what it needs (§5.6) —
not on the ask path, which must work from any checkout and from none.

### 4.3 What counts as an overlap

The **set intersection of the two PRs' changed-path lists**, forward-slashed and case-sensitive as `gh`
returns them:

- **Drafts are included.** A draft's collision is deferred, not absent.
- **Red and pending PRs are included.** CI colour says nothing about path overlap.
- **Closed and merged PRs are excluded** (`--state open` only).
- **The budget ledger is NOT suppressed**, even though nearly every prose PR touches it. Suppressing
  the single most collision-prone file in the tree to make the message tidier would hide the thing
  this spec is about. If the noise ever proves real, *rank* it last; never drop it.

### 4.4 The picker's shape — each rule has a named owner

`telegram_ask.py` enforces the picker rule as a *shape*: an option with no description is refused,
the first option is the marked recommendation, one question per message. Phase 1 sits **entirely inside
the body of a question that already exists**.

| Rule | Phase 1's obligation |
|---|---|
| One question per message | The overlap block is **not a question**. No new `ask` call, no new `question_id` |
| Options carry descriptions; two to ten | The option list is **unchanged** — `Approve` / `Not now`. A third option would make the overlap answerable, and it is context, not a decision |
| First option is the marked recommendation | Untouched: `request_argv` passes `--no-recommendation`, because a marked recommendation would be the assistant recommending its own merge |
| Body ≤ 4096 chars, not chunked | Bounded: **at most three other PRs, at most four shared paths each**, and **what was dropped is named** — *"(+2 more open PRs overlap)"* |

As built:

```
Also open, changing some of the same files:
• #12 feat(merge-guard): prose that runs is not docs-only … —
  seneschal/scripts/merge_guard.py, seneschal/scripts/CLAUDE.md, seneschal/context-budget.json
• #9 (draft) docs(archons): a fact about the employer is not an instruction … —
  seneschal/context-budget.json
```

- **The paths are named, not counted.** *"changes 2 of the same files"* is a shape, not content; it is
  the sentence that sends the owner to GitHub, which is the trip the picker exists to save.
- **No closing line about who rebases.** *"Whichever merges second will have to rebase"* is still a
  claim about a future action, and §1.5 shows a shared path that needed no help. The block says what is
  true of two file lists and stops.
- The whole block is bounded by `merge_guard.OVERLAP_CHARS` against a **measured** structural maximum
  that the bounded test re-derives every run — never a typed guess.

### 4.5 Ordering — considered, measured, and not emitted

The intuition is to choose **who pays the rebase**: a two-line doc fix should yield to a 600-line change
with tests. As a rule: *suggest merging the PR with the larger total diff first.* Worked against a
typical pair, the rule makes **no measurable difference**: the two PRs overlap on exactly one budgeted
artifact, so the second rebases exactly one `raises[]` chain in either direction. That is a negative
result about the rule's *value*, recorded rather than dressed up. **As built, phase 1 emits no ordering
clause at all** — a suggestion resting on a negative result does not get promoted to advice by being
implemented. If a better predictor is ever wanted, the column to try is *"overlapping budgeted
artifacts"*, not total diff size.

### 4.6 What phase 1 deliberately does not do

- **It does not gate.** No overlap of any size, count or shape blocks a picker or changes an option.
- **It does not fail the ask.** The whole computation is wrapped; if `gh` is missing, rate-limited,
  slow or unparseable, the picker goes out **unwarned**. *Asking is not merging and may not cost the
  verdict* — a warning that can suppress a question is strictly worse than no warning.
- **It does not record anything.** No new state file, no new ledger; `state/merge-ask-log.jsonl` keeps
  its schema.
- **It does not look at another repository.** `#45` in two repos is two pull requests, and a
  cross-repo path collision is not a merge conflict.

Two things it has that this list might seem to forbid, named rather than smuggled in:

- an **in-process**, per-repository, 120-second cache (`pr_overlap.CACHE_TTL_SEC`) — what makes one
  `gh` call per picker affordable inside the resident daemon. It is **never on disk**, because a cache
  file is a state file wearing a smaller name;
- a diagnostic `--overlap-from <file>` on `merge_guard.py request`/`render` that replays a captured
  `gh pr list` payload through the same seam the tests use, so a picker can be reproduced after the
  world it describes has moved on.

**And one hazard, fixed before it shipped.** An overlap entry relays **another PR's title and changed
paths**, and `ask_citations` — the gate that refuses a picker citing a document it cannot show —
resolves those paths against the local checkout. An overlapping PR that **adds** a file names a path
that is not there yet, and the picker would not send at all. The entry lines therefore go out as
`--quote` spans (relayed text, not the assistant's citations); the header and the overflow line are
**not** quoted, because those are the assistant's own sentences.

### 4.7 Phase 1.5 — the consequence line (UNBUILT)

The owner's reasonable want is *"I'll accept the second tap if I can see from the start in the picker
that approving this will block other things, then I can wait to tap those."* Phase 1 says *"also open,
changing some of the same files"* — an **overlap** claim. The want is a **consequence** claim — what
tapping *this* does to *those* — and the two are not the same sentence.

**What cannot be claimed, stated first:** that the other PR *will conflict* (§1.5); that it *will need
a rebase* (§4.4's narrowing); that approving this *invalidates* its approval (those two, chained). The
strong form needs a conflict prediction the ask path cannot make (§4.2).

**What CAN be said is better than a prediction: what is already at stake.** Two local, present-tense
facts the guard already owns:

1. **A live, unspent approval exists for that PR at its current head** — `merge_guard.verify_approval`
   returns `""`. The same predicate the merge door uses.
2. **A merge picker for that PR is pending and unanswered** — `picker_retire.pending_pr_pickers`.

Either means the owner's attention is *already committed* to that PR, and a rebase would spend it
again. The shape: the `• ` entry line **unchanged, byte for byte** (still inside its `--quote` span),
plus a separate, unquoted `↳` continuation line in the assistant's own voice:

| State | Line |
|---|---|
| A live unspent approval exists for that PR | `↳ you approved #<n> at <local time>; it has not merged yet.` |
| A merge picker for that PR is pending | `↳ a picker for #<n> is still waiting on your tap.` |
| Neither, **or either one unreadable** | **nothing at all** |

The time is rendered through the same owner-local clock `picker_retire.when_phrase` uses, never a
fourth spelling of the hour. An unreadable store costs the `↳` line, never the picker. The bounded test
re-derives the block's structural maximum **with** these lines. It refuses to claim a conflict, a
rebase, an invalidation, any ordering, or that the approval is dead — whether an approval survives a
rebase is §7, and **a picker may not pre-empt a decision by wording.**

**The gap this leaves, named:** the owner asked for *"it'll block other things"*; this delivers
*"here is what you have already spent on the things it might block."* That is the ingredients of the
requirement, not the requirement. If the strong form is ever needed, the merge-base test is where it
comes from — in a module that already has a worktree (§5), never on the ask path. 2a should measure
whether the weak form is enough (§5A.7).

---

## 5. Phase 2 — repair after a merge

Phase 2 is the first thing here that **acts**, on a branch, in a repository. Everything below is written
to keep its blast radius to the allow-listed files on one feature branch.

### 5.1 Trigger — the daemon's next sweep, not the merge

**The merge itself is not observable.** A merge happens through `gh pr merge` on the hook's path, and
`merge_guard`'s hook is a `PreToolUse` **refusal** that decides allow-or-deny and exits. Hanging a
repair off it would give a guard a side effect, and what makes the guard trustworthy is that
`record_approval` has exactly one caller and the guard writes nothing else.

So the trigger is **GitHub's own verdict, read on a cadence**: an open PR whose `mergeable` is
`CONFLICTING` against its base. That field is on `gh pr list` alongside `headRefOid`, `baseRefName`
and `isDraft`, so detection costs no extra call.

**Where it runs: inside the daemon's existing PR-watch task, as a second, independent call — not a new
task.** The cadence (~3 min) and the supervision already exist. The two calls share the tick and
nothing else: `pr_repair.py` has its own fail-open boundary, so a repair failure cannot cost the sweep
its pass and vice versa. Two bounds are inherited from `pr_sweep`'s burst analysis:

- **At most one PR repaired per pass.** Five PRs broken by one merge become five repairs across five
  passes, and anything held over is **named**, never dropped.
- **`mergeable: UNKNOWN` does nothing.** GitHub computes mergeability lazily; unknown is *"ask again
  next pass"*, never *"probably fine"*.

### 5.2 What it may resolve — a named allow-list

The original rule, and the membership test: **the file is a machine-written additive ledger, the union
of both sides is well-defined without reading prose, and any derived number in it can be recomputed
from the merged tree rather than chosen.**

| Shape | Verdict | Why |
|---|---|---|
| `seneschal/context-budget.json` — `budgets.<artifact>.raises[]` | **resolve** | Append-only chain. Union both sides, order by `date`, rechain, recompute `max_bytes` (§5.3) |
| `seneschal/context-pointers.json` — `allow[]` | **resolve** | A flat list of `{pointer, reason}` with no derived value. Union by `pointer`; **identical `pointer` with different `reason` is a REFUSAL** — two authors disagreeing in prose about why an exemption is legitimate is a judgment |
| Any `*.md` | **refuse** in the original rule — **superseded for auto-repair, §5A.9 item 14** | git already merges the easy Markdown case unaided (§1.5), so one reaching a repairer is by construction the harder one |
| Any `.py`, test, workflow, lockfile | **refuse** | Behaviour. Not negotiable, however simple the hunk looks |
| Instruction files that are prose (`CLAUDE.md`, `SKILL.md`, persona and mode files) | resolved only as Markdown under §5A.9, and **still ask-high at the merge guard** | Behaviour that happens to be prose — the owner's tap at the new head is still required |
| Any other `.json` | **refuse** | An unlisted ledger is an unexamined one. Additive-looking is not additive |

### 5.3 The number is recomputed, never merged

> **RULE. `max_bytes` is obtained by measuring the artifact in the merged working tree. It is never
> taken from either side, and it is never derived by arithmetic on the two diffs.**

1. Merge the new base into the PR branch in a private worktree. Every non-allow-listed path must merge
   cleanly, or stop (§5.5).
2. Resolve the ledger: union both sides' `raises[]`, dropping exact duplicates, ordering by `date`.
3. For each artifact whose chain changed, **measure the merged file**, LF-normalised.
4. Set `max_bytes` to the measured size and **rechain**: every entry's `from` becomes the previous
   entry's `to`; the last entry's `to` becomes `max_bytes`.
5. Write the tail entry's `reason` naming **which PR landed first and what this branch's own delta is**
   — the delta is this branch's, the new base is attributed by name, and nobody's entry lies.
6. **Verify before committing**: `check_context_budget.py` and `check_context_pointers.py` against the
   resolved tree, then the full local CI mirror (`ci_local.py`). A non-clean result is a failed repair
   (§5.5), not a warning to push past.

Step 6 is what makes this defensible rather than merely automated: the repair is **checked by the same
tooling CI runs** before anything leaves the worktree.

### 5.4 The invariants — stated as absolutes because that is what they are

1. **IT NEVER MERGES.** No `gh pr merge`, no push to the base branch, no auto-merge flag, no
   `--admin`, no merge queue. Asserted against the *file*: a test checks `pr_repair.py` and
   `pr_rebase.py` do not so much as name a merge subcommand or `record_approval`.
2. **IT NEVER MINTS, REUSES, EXTENDS OR RE-DATES AN APPROVAL.** `record_approval`'s single-caller
   property is untouched. A repair does not read the approval record to decide anything, does not copy
   one to the new head, and does not refresh a TTL.
3. **AFTER RESOLVING, THE EXISTING PATH RE-ASKS.** The new head is a new artifact; the resident sweep
   sends a fresh picker once CI on the new head goes terminally green, through `ask_on_green` with all
   of its refusals intact. The repair sends no picker of its own and does not bypass the ask log, so a
   repair that lands during quiet hours defers exactly as everything else does.
4. **A REPAIR IS ONLY EVER AN ADDITION.** It cannot make a PR *less* mergeable and cannot make a merge
   *more* likely than the gate already allows. Every path ends in either "the branch has a new commit
   and a fresh question will be asked" or "nothing changed and here is why."

Invariant 3 is where §7's residual is created, deliberately. Phase 2 is designed **as though the second
tap is required**, because it is.

### 5.5 Failure behaviour — fail-closed, and a leak is named

| Condition | Behaviour |
|---|---|
| A conflict in any non-allow-listed path | **Stop.** Nothing committed, nothing pushed. The owner is told the PR, the paths, and that a human is needed |
| An allow-listed file whose conflict is not the expected shape (a deleted key, malformed JSON, a `reason` disagreement) | **Stop.** "Additive-looking" is not additive |
| The tree's own checks non-clean after resolution | **Stop.** A repair that cannot pass the tree's checks is a wrong repair |
| A push that is rejected (the branch moved) | **Stop and re-read next pass.** Never force-push, never `--force` anything |
| `gh` or `git` unavailable, timeout, offline | **Costs this pass and nothing else.** No record, retried next pass |
| A worktree that cannot be removed | **Leaked, recorded, named** — never `--force`, per `job_worktree.py` |

Every stop that needs a human is **loud** — one Telegram line naming the PR and the reason — and
rate-limited to once per PR per head SHA, so a permanently unresolvable conflict cannot become a
repeating nudge. Silence on a failed repair is how a problem sits in a log nobody reads.

### 5.6 Where the work happens — a private worktree, never the daemon's checkout

A worktree per repair job, cut from `origin/<base>` spelled **in full** (a bare branch name is
ambiguous in a checkout with more than one remote), `-c core.fsmonitor=false` on every git call,
`git worktree remove` and **never** `--force`. This is `job_worktree.py`'s existing dance, reused via
`jobs.py start --worktree` rather than reimplemented. **The unit of collision is the checkout, not the
branch**: the daemon's checkout stays on its branch, clean, always — a repair that dirties it breaks
the daemon's auto-reload.

---

## 5A. Phase 2R — the auto-rebaser, and the invariant that keeps it from eating taps

### 5A.0 The acceptance criterion

The loop the owner wants, read as a machine:

> *"Scroll through the pull requests, find one that doesn't have any dependencies, see what it
> invalidates, approve it, and then just wait for the invalidated PRs to pop new pickers."*

| Step | What it needs | State |
|---|---|---|
| *"find one that doesn't have any dependencies"* | the overlap band — an empty band means no overlap | **BUILT** (phase 1) |
| *"see what it invalidates"* | the consequence line | **§4.7, unbuilt** — and only the weak form is deliverable |
| *"approve it"* | the picker | **BUILT** |
| *"wait for the invalidated PRs to pop new pickers"* | (a) broken PRs repair themselves; (b) the dead picker goes away; (c) a fresh picker arrives at the new head | (a) **this section**; (b) **BUILT** — §5A.4; (c) **BUILT** |

**What the loop buys is a change of kind, not of degree: taps stop being repair labour and become
sequencing decisions.** That is the thing to protect, and §5A.3 is how it gets destroyed.

### 5A.1 What it does — one call, and only the cheap class

Phase 2R repairs **class 1** (§1.6) with a single server-side rebase. It has **no local checkout, no
worktree, no clone, and no working tree of any kind.** That is a safety property:

> **The module cannot force-push anything, because it never holds the branch.** The force-update is
> GitHub's, performed inside the rebase. `pr_rebase.py` runs no `git` at all, so §5.6's worktree
> discipline and the *"never `--force`"* refusal are not weakened by it — they are not reachable from it.

`--rebase` (or `updateMethod: REBASE`) is not optional: a merge-commit update would put the base's
history into the PR's own diff, which breaks §7.4's candidate A outright — **the cheap repair may not
foreclose a decision that is still held.** A failed rebase is a **referral**, not an error.

### 5A.2 Where it lives — ONE trigger, and it is the sweep

Two shapes are possible: hang it off the process that performs the merge, or a standing watcher over
every open PR. **The answer is the standing watcher alone:**

1. **There is no process here that performs the merge.** The merge happens through `gh pr merge` in
   whatever session runs it, and the guard is a `PreToolUse` refusal that must not act.
2. **A merge nobody here performed is the general case anyway** — the web UI, another session, another
   machine. The standing watcher has to exist regardless, and once it exists it covers everything a
   fast path would.
3. **Two detectors of one event is a bug waiting to happen.** *Which PRs did that merge stale* is one
   question, and two answers to it can disagree.
4. **A fast path saves three minutes against a wait measured in hours.** Detection is never the
   binding constraint; the owner being awake is.

So `pr_rebase.py` is called from `pr_repair.py`, which the daemon's existing PR-watch task calls — not
a new task. One `gh pr list` of the target repository, several consumers.

**Candidacy versus refusal.** The sweep may *suppress* a merge picker for a `CONFLICTING` PR (a tap on a
conflicted head cannot be redeemed) — that decides whether a question is worth sending now. The guard
decides *refusal* from the pull request itself. A rebase is neither: it is an **act**, which is why it
lives in its own module.

**Order within the tick, and the order is load-bearing:** ask, then retire, then rebase/repair. Asking
must not be delayed by tidying; retiring before rebasing means a retired picker is one fewer live
question blocking §5A.3's predicate.

### 5A.3 THE HAZARD — a moved head kills a live approval

An approval record is **single-use and pinned to one exact commit**. A rebase moves the head. So a
rebaser that fires freely on a three-minute cadence can **destroy the owner's taps faster than they can
make them** — the one way this feature turns into a net loss.

> **RULE. THE REBASER MUST NOT MOVE A HEAD THAT CARRIES A LIVE APPROVAL, OR THAT A LIVE QUESTION IS
> PINNED TO. If it cannot tell whether one exists, IT DOES NOT REBASE.**

**The predicate.** The rebaser may move `(repo, pr)`'s head from `H` only if every one of these holds:

1. **`mergeable` is exactly `MERGEABLE` or exactly `CONFLICTING`.** `UNKNOWN` and any unrecognised
   value do nothing at all.
2. **If `MERGEABLE`:** `merge_guard.verify_approval(state_dir, pr, H, repo)` must **REFUSE** — the same
   function the merge door calls, never a second reading of the record, so a rebase and a merge can
   never disagree about whether an approval is live. Its refusals already cover the dead cases: an
   approval bound to an older SHA, a spent one, an expired one.
3. **If `MERGEABLE`:** no merge-approval question for `(repo, pr, H)` may be pending and unanswered
   (`picker_retire.pending_pr_pickers`). This clause is what actually closes the race below: **a tap can
   only land on a live picker**, and `merge_guard._question_confirms` is the floor under every
   approval, so no pending question means no approval can appear.
4. **If `CONFLICTING`:** clauses 2 and 3 **do not apply** — the single exception. **An approval pinned
   to a conflicted head cannot be spent**: GitHub refuses the merge before the guard ever sees it. Moving
   the head destroys nothing that had value. Written as `CONFLICTING` **explicitly**, never as *"not
   `MERGEABLE`"*, because the negative spelling silently swallows clause 1's `UNKNOWN`.
5. **The base has moved since the last recorded attempt** for this `(repo, pr)` (§5A.6).
6. **Every input was readable.** No state directory, no question store, `verify_approval` raising, a
   `gh` non-zero, a head SHA that does not match the row it came from — **do not move.** Can't tell ⇒
   don't touch.

**The corner clause 2 leaves open, named.** A class-1 PR carrying a **live** approval never gets
rebased. That is correct — a merge is about to happen, and moving the head under it is the exact
disaster. It resolves on its own within `merge_guard.APPROVAL_TTL_HOURS`, or sooner when the merge
lands. **A bounded wait is the right price.**

**The race, and it is real.** An approval can be recorded **between** the rebaser's read and GitHub's
push. The pass runs on a worker thread while the daemon's event loop keeps servicing Telegram, and
`record_approval`'s one caller is on that Telegram path — so a tap can land *genuinely concurrently*.
Clause 3 makes the window nearly unreachable; it is not closed, so it is **reported rather than
prevented**: after the rebase returns, the approval store is re-read at the *old* head `H`. If an
approval is now live there, the rebaser has destroyed a live tap, and that gets **one Telegram line
naming the PR, the SHA that was approved, and the new head**. The owner must never learn that a tap
vanished by tapping again.

**What the invariant does NOT do:** read an approval to decide *whether the merge is a good idea*, copy
one to the new head, extend a TTL, or touch `record_approval`'s single-caller property.

### 5A.4 The dependency the loop rests on — head-moved picker retirement (met)

The loop assumes a stale picker goes away. Retiring a picker whose PR **stopped being open** is not
enough: the loop needs a picker whose PR is **still open** but whose **head has moved** retired too,
because the tap is already dead and the question is stale. And it is a hard dependency, not a nice
companion: §5A.3 clause 3 refuses to move a head that a pending question is pinned to, so an uncovered
head-moved case would block the rebaser on exactly the PRs it exists to fix. **Met:**
`picker_retire.py` retires a moved-head picker using the `number -> headRefOid` index
`pr_sweep.open_heads` builds from the same `gh pr list` it already makes — zero added API calls.

### 5A.5 Failure — fail-closed, and almost nothing gets pushed

Every failure leaves the pull request **exactly as it was**; a server-side rebase is atomic.

| Condition | Behaviour |
|---|---|
| The rebase fails on a real conflict | **Referral, not an error** — recorded, **no push** |
| The head moved under us | **Stop and re-read next pass** |
| `gh` absent, unauthenticated, rate-limited, timed out, offline | **Costs this pass and nothing else** |
| Any clause of §5A.3's predicate unreadable | **Do not move.** Recorded as *could not tell* — a different fact from *nothing to do* |
| The rebase landed and CI on the new head then went red | Recorded, **no push** — the red-CI notice path already tells the owner |
| **The race fired: a tap was destroyed** | **PUSH.** One line, naming the PR, the approved SHA, and the new head |
| **The per-PR ceiling bound** (§5A.6) | **PUSH.** One line, once, naming the PR and that it has stopped being retried |

> **Nothing that is the expected outcome gets a push. Anything that leaves the repository in a state
> the rebaser CREATED and cannot undo gets one.**

A failed rebase on a conflicted PR is expected; pushing it every three minutes is how a channel gets
muted. Everything else is a report line in the daemon log — and **what was held over is named there**.

### 5A.6 Bounds — it must not become a force-push machine

1. **At most one rebase per `(repo, pr)` per BASE head SHA.** If the base has not moved since the last
   attempt, the answer has not changed. **This makes the rebaser idempotent against a stationary
   base**, which is the steady state.
2. **At most one rebase per pass.** Five PRs staled by one merge become five rebases across five
   passes, and what was held over is named.
3. **It cannot trigger on its own output.** After a rebase the head is new and CI is pending, so the
   sweep will not ask and the rebaser will not act (bound 1). Convergent by construction.
4. **A ceiling per `(repo, pr)` per rolling 24 h, and when it binds it says so.** A busy day can
   legitimately need several rebases of one PR as the base moves repeatedly, so the ceiling sits above
   that (`pr_rebase.MAX_REBASES_PER_PR_PER_DAY`); 2a's rows are what should revise it.

### 5A.7 Its measurement gate — columns in 2a, not a fourth phase

2a is the detector both 2R and 2b need: it reads the open-PR list and classifies why each broken PR is
broken. 2R's questions are columns on that same table:

| Column | Question | What it gates |
|---|---|---|
| `rebase_would_suffice` | Was the break repairable by rebase alone? | Whether 2R is worth running |
| `approval_live_at_detection` | Did the broken PR carry a live approval or a pending picker when detected? | **How often §5A.3 makes the rebaser WAIT.** If high, 2R is mostly a waiting machine |
| `rebases_per_pr_per_day` | How many base moves did one PR chase? | §5A.6's ceiling |

A live rebaser changes the population 2a measures — the PRs still broken after a cheap rebase are, by
construction, only the hard ones. So **2a records whether the rebaser was live for each row**
(`rebaser_live`), and the two populations are reported separately, never merged.

### 5A.8 Deliberately not in scope for 2R

- **Merging anything.** §5.4 invariant 1, in full force.
- **Resolving file contents.** That is §5; a server-side rebase either succeeds or refers.
- **A merge-commit update** instead of a rebase (§5A.1).
- **Rebasing a PR in a repository other than the target** — widening the watched set is `pr_sweep`'s
  decision, not a side door here.

### 5A.9 As built — 2R and the conflict repair, and the choices the text above left open

**What runs.** The daemon's PR-watch task calls `pr_repair.sweep` after `pr_sweep.sweep`, in its own
`try` (order: ask, retire, repair). One `gh pr list` of the target repository, one read of the base
branch's tip, then for each open, non-draft, same-repository PR based on the base branch:

- **`MERGEABLE` + `BEHIND` → `pr_rebase.consider`** — §5A.3's predicate, then GitHub's server-side
  rebase. At most one per pass.
- **`CONFLICTING`, confirmed by a fresh `gh pr view`** (the list's `mergeable` lags) → classify which
  paths actually conflict → all repair-eligible: **one `jobs.py start --worktree` repair job per
  `(PR, base sha)`**; anything else: **one Telegram line per head** to the pull-requests topic naming the
  PR, the files and why.

**The choices, each the conservative side of a silence:**

1. **The class-1 trigger is `mergeStateStatus: BEHIND`.** With strict required checks on, `BEHIND` is
   exactly *"this head's verdict is about a base that no longer exists"*, as a field GitHub computes.
2. **The rebase is GraphQL `updatePullRequestBranch(updateMethod: REBASE, expectedHeadOid: H)`** — the
   same server-side operation as the CLI, **pinned to the exact head §5A.3's predicate was evaluated
   at**, so the move cannot land on a head nobody checked. A narrowing.
3. **`CONFLICTING` never goes to the rebaser.** A server-side rebase has no conflict resolution, so
   trying it first would only spend a call to be referred; it goes straight to the repair path.
4. **The rebaser also waits for the head's CI to be terminal.** A pending rollup means someone just
   pushed (possibly a job still working its own branch) or the last rebase is still being judged.
5. **§5A.3 clause 4's exemption is NOT extended to `BEHIND`.** With strict checks on, an approval on a
   `BEHIND` head is also unspendable (short of an admin bypass), so the argument nearly applies — but it
   relaxes a rule about the owner's taps, which is the owner's to relax. Consequence: a `BEHIND` PR with
   a live approval or a pending picker waits (`merge_guard.APPROVAL_TTL_HOURS`;
   `telegram_ask.QUESTION_TTL_DAYS`). **The conflict repair DOES use clause 4.**
6. **§5A.4's dependency is met** — a rebase's stale picker comes down on the next pass.
7. **The ceilings needed numbers to build**: `pr_rebase.MAX_REBASES_PER_PR_PER_DAY` and
   `pr_repair.MAX_REPAIRS_PER_PR_PER_DAY`. When one binds, the PR stops being retried and the owner is
   told once.
8. **Phase 2b is a delegated agent job, not a deterministic resolver module.** The daemon decides
   *whether* to repair; the job does the repair, with a brief (`pr_repair.repair_brief`) that follows
   §5.2–§5.5: it cuts its own worktree, merges `origin/<base>` in (a merge, per §5.3 step 1, so nothing
   is ever force-pushed), aborts if any conflicted path is off the allow-list or is not a plain content
   conflict, resolves the budget ledger per §5.3, unions the pointer allow-list and refuses on a `reason`
   disagreement, requires `ci_local.py` green before a plain push, and never merges. Launched through
   `jobs.py`'s own CLI (`--requested-by assistant --reason-class merge-repair --agent`), so the
   concurrency caps apply verbatim; it never passes `--over-soft-cap`, and a cap refusal records nothing.
9. **Which paths conflict is computed without a checkout.** The compare API names the paths changed on
   both sides since the merge base; for each one, the three versions are fetched and merged with
   `git merge-file`, which needs no repository. A delete, a rename, an unreadable version or more than
   `pr_repair.MAX_PATHS_TO_MERGE` shared paths counts as conflicting (erring toward the notice, never
   toward a job), and a compare list at GitHub's 300-file cap is *could not tell*, which also gets the
   notice. This reproduces §1.5 exactly: a shared path that merges cleanly is not reported.
10. **Bounds beyond §5A.6**: at most one repair launch and two conflict classifications per pass; **one
    repair job in flight at a time**, across all PRs; no launch and no notice inside the quiet window
    (`merge_guard.in_quiet_hours` — a job's completion push would buzz the owner at 3 AM). Rebases do
    run overnight, because they send nothing.
11. **Scope is one repository — this checkout's `origin`** (`repo_config.origin_repo()`), because a
    repair job's worktree is cut from this repository's `origin/<base>` and cannot repair anything else;
    the base is `repo_config.base_branch()`. Both are resolved once per pass, never at import;
    `pr_repair.REPO` / `pr_repair.BASE_BRANCH` remain readable names for the same two values. A checkout
    with no `origin` reports an error and does nothing.
12. **What gets sent to Telegram**: §5A.5's two rows (a destroyed tap, a binding ceiling), plus §5.5's
    out-of-allow-list stop, once per head. Nothing else.
13. **A kill switch that needs no deploy**: an empty `state/pr-auto-repair-off` turns the pass off.
14. **Markdown is repair-eligible; this supersedes §5.2's "every `.md` is refused" for auto-repair.**
    A conflict whose every path is a `*.md` or one of the two ledgers gets the repair job
    (`pr_repair.repair_eligible`). Its brief resolves Markdown by `pr_repair.MARKDOWN_RULE` — *keep both
    sides; for tables/lists keep every row in chronological order; never delete either side's content;
    if both sides edited the SAME sentence differently, keep both versions adjacent and flag it in the PR
    comment* — and ends by commenting on the PR with the merge sha, every file resolved and how, and a
    *Needs your eyes* list of same-sentence clashes, so the owner can review the diff. Still refused, with
    the one notice: any code or config path, and a delete or rename of any file, Markdown included
    (there are no two sides to keep). Unchanged: the owner approves every merge, and `merge_guard.py`
    keeps instruction files ask-high — a repaired `CLAUDE.md` still needs a tap at the new head.

**Every detection is a 2a row.** Each `CONFLICTING` and `BEHIND` detection is appended to
`state/pr-repair-log.jsonl` (schema `seneschal.pr-repair/1`) with §5A.7's columns and
`rebaser_live: true`. The report that computes 2a's fraction from those rows is unbuilt.

---

## 5B. A PR a still-running job opened is a draft until that job ends

### 5B.1 The failure

A delegated job can run `gh pr create` while it is still working, then keep pushing. In between,
`pr_repair.py` may see the PR `CONFLICTING` at an intermediate head and launch a repair job against a
branch that is still moving. The repair correctly refuses (*"branch moved since detection"*), so
nothing is damaged — but a PR still being written was treated as a merge/repair candidate at all, and
it costs the owner a pointless job notice.

### 5B.2 Why code, not a brief convention

Telling a job's brief "open the PR as a draft and mark it ready when you're done" depends on every
future brief remembering to say it and every agent remembering to act on it, and says nothing about a
PR opened by code the brief never wrote. **The invariant is enforced from the READ side instead**: a
PR's branch is checked out in a running job's worktree, or it isn't — a fact observable independent of
how or when the PR was created.

### 5B.3 The design choice — sweep-side conversion, not a `gh pr create` rewrite

Two mechanisms were considered: **(1)** the sweep treats a PR whose `headRefName` is checked out in a
currently-running `--worktree` job's worktree as in-flight and converts it to draft; **(2)** a
`PreToolUse` guard on `gh pr create` inside a job's worktree. **Chosen: (1).** No hook here rewrites a
tool call's input — every `PreToolUse` hook only allows or denies — and a deny-and-retry hook still
needs a host-side registration step to take effect at all, the exact "depends on a step nobody is
forced to take" shape §5B.2 rules out. The sweep-side check needs no registration and cannot be skipped
by a brief that forgets, a hook that was never installed, or a PR opened outside `jobs.py` entirely.

### 5B.4 The mechanism — `job_pr_draft.py`

- **`list_running_worktree_branches(state_dir)`** — `{branch: job_id}` for every active `--worktree`
  job whose worktree still exists and is on a real (non-detached) branch. Lazily imports `jobs`, so a
  broken import costs this map, never the caller's pass. Fail-open to `{}`.
- **`inflight_job_for(row, branches)`** — pure: does `row["headRefName"]` match a running job's branch?
  Wired into `pr_sweep.candidates()` (silently dropped, like a GitHub draft) and its red-notify arm,
  into `pr_repair.py`'s per-row loop **ahead of** the BEHIND/CONFLICTING dispatch, and into
  `pr_rebase.blocked_reason` as defense in depth.
- **`draft_and_record(...)`** — the first time `pr_repair.py` sees a non-draft in-flight PR, it runs
  `gh pr ready <pr> --repo <repo> --undo` and, on success, records the conversion in
  `state/job-drafted-prs.json`. **The record, not a live re-check, answers "did THIS mechanism draft
  it"** — a PR drafted by a human for its own reasons never gets an entry and is never touched.
- **`resolve_for_job(state_dir, job_id, success=...)`**, called once from `jobs.py`'s `reconcile` at a
  job's terminal transition: `gh pr ready <pr>` if the job ended `done` with exit code 0, else leave it
  a draft. A `gh` failure leaves the store entry standing for a later attempt; the completion push names
  the outcome.

### 5B.5 Deliberately not built

- **No cross-repository scope** — a job's worktree is always cut from this repository.
- **No retry loop** inside `job_pr_draft.py` — a failed call is reported and left for the next pass.
- **No change to who may merge** — it only ever calls `gh pr ready` / `gh pr ready --undo`.

A job that opens more than one PR from the same worktree drafts and resolves each independently (the
store is keyed `<repo>#<pr>`).

---

## 6. Phasing — each merges green on its own

| Phase | What lands | Depends on | Useful alone? |
|---|---|---|---|
| **1 — BUILT** | `pr_overlap.py` + tests; `merge_guard.overlapping_prs` / `overlap_band` + the `overlap=` keyword in `request_argv`; the lazy-import rule | nothing | **Yes.** The owner sees the other PR before tapping. Nothing acts, nothing gates |
| **1.5 — UNBUILT** | §4.7's consequence line | phase 1 | **Yes.** Body text on an existing question |
| **2a — DETECTION ROWS BUILT; report UNBUILT** | the detector/reporter: find broken PRs, classify the conflict against the allow-list and the repair class, report | phase 1 | **Yes.** It measures how often the allow-list is sufficient — **and §7's hold is bound to its numbers** |
| **2R — BUILT** | `pr_rebase.py`, under §5A.3, bounded by §5A.6 | 2a's detector; §5A.4's retirement | Yes — the *"broken PRs repair themselves"* half of §5A.0 |
| **2b — BUILT AS A DELEGATED JOB** | the resolver: worktree, union, rechain, recompute, verify, push; re-ask through the existing path | 2a | Yes |
| **5B — BUILT** | `job_pr_draft.py` | nothing | Yes |
| **3** | only if §7 is decided in favour | **a held decision** (§7.6) | — |

**2a is load-bearing for a decision, not only for 2b's go/no-go.** It answers *of the PRs broken by a
merge, what fraction have a conflict confined to the allow-list?* — and the owner's hold on §7 waits on
the same numbers. Phase 1 can ship, be used for weeks, and be judged on its own: it touches no gate,
writes no state, and its worst failure is a picker that looks like it would without it.

---

## 7. HELD PENDING 2a — the second tap

**Not decided here. Not assumed anywhere above.** Phase 2 is designed as though the answer is *ask
again*, which is also the interim position.

### 7.1 The question

Resolving a conflict — or rebasing — moves the PR's head. The approval is bound to a head SHA, so it
dies, and the owner taps again for a pull request they have already approved. Removing that would mean:

> **An approval survives a rebase or a merge from the base branch that introduces no change to the PR's
> own diff.**

> **When a PR you have already approved is rebased onto a moved base, and the rebase changes nothing in
> the PR's own diff, should your approval survive the new head SHA — or should you be asked again?**

Both readings of the same fact are live: *"a sentence authorizes 'that PR' while a tap authorizes this
SHA"* is the argument **for** the binding (`../references/autonomy-policy.md`), and a conflict-resolution
commit that did not exist when the owner tapped is exactly the kind of commit it has in mind. A tap
spent a second before a sibling merges, followed by a rebase that changes zero lines of what was read,
is the argument **against**. Only rebases that change nothing in the PR's own diff are this question; a
PR that gained a real commit between two taps is the binding **working**.

### 7.2 What it would save

| | |
|---|---|
| Taps saved | one per PR whose head moved **without its own diff changing** |
| Latency saved | a repair that lands during quiet hours sits unmerged until the owner wakes, holding the base back from a change they approved hours earlier |
| What it does **not** save | anything on a first approval |

### 7.3 What it would risk

| | |
|---|---|
| It is a change to the approval gate | The gate exists because an unasked merge is possible without it; every relaxation is measured against that |
| The invariant it weakens | *"a tap authorizes **this SHA**"* — relaxing it means overturning an argument built on this exact event class |
| The escape hatch it opens | if an approval can survive *some* new commits, a survival predicate becomes code standing between a tap and a merge — a second door beside `record_approval`'s one |
| It is a judgment call again | same user, same disk: what the gate buys is that routing around it stops being a judgment call and becomes a forgery |
| The number could be wrong | §7.4's exclusion — the ledger legitimately changes on a repair |

### 7.4 How *"no change to the PR's own diff"* would be computed

**Candidate A — patch identity at zero context.** `git diff -U0 $(git merge-base <base> <head>) <head>`
before and after, normalised to added/removed lines per path; require byte-equality. `-U0` strips the
line-number shift a clean rebase causes. **What it must exclude:** the budget ledger legitimately
changes (a recomputed `max_bytes`, a rechained `from`), so the predicate would be *"identical except the
ledgers"* — **an approval surviving a change to the one number that must never lie.** Mitigation:
require the recomputed number to equal the measured merged size, which converts "trust the repair" into
"trust `len()`". It does not make the exclusion vanish.

**Candidate B — tree identity.** Require `tree(head_new)` to equal git's own `merge(base_new,
head_old)`. Stronger, needs nothing excluded — but only satisfiable when that merge is *clean*, which is
exactly the case with no conflict to repair. **B is vacuous for the conflict case** and recorded only so
nobody re-proposes it as the obvious safe option. (For a pure class-1 rebase it is not vacuous, which
is worth noting when 2a reports.)

**A third framing, needing no decision at all:** leave the SHA binding untouched and make the **second
tap cheaper** — the re-asked picker says this is the same PR approved at `<old sha>`, that the only
change since is a rebase onto the base, and that its own diff is unchanged. Phase 1 already built the
machinery this needs (a bounded band of the guard's own facts; an ask ledger that records every head a
picker went out at). **Not built and not proposed here** — recorded so that whichever way §7 goes, the
cheaper-tap option is known to be cheap.

### 7.5 Status

**HELD PENDING 2a.** Phases 2a, 2R and 2b are buildable without an answer. Phase 3 does not exist until
§7 is decided.

### 7.6 The hold, read exactly

The owner's position is *"leave it as is until the measurements from 2a come back; I'll probably be fine
if I can see from the start in the picker that it'll block other things."* That is three things:

1. **The SHA binding stays, for now.** No survival predicate, no phase 3.
2. **It is a HOLD, not an answer.** It has a named trigger — 2a's measurements — and does not become
   settled by the passage of time.
3. **Its reason is conditional on a feature whose strong form is not deliverable** (§4.7): the picker
   can say what is already at stake, not what this merge will do.

**One counter-consideration, recorded under the hold rather than as a revision of it:** the hold's cost
is not fixed — it scales with how well 2R works. An automatic rebase is an automatic head move, and a
head move kills an approval. §5A.3's clauses 2 and 3 exist only *because* the binding is SHA-tight, and
they are the reason 2R waits on exactly the PRs the owner is most likely waiting on. That belongs in
what 2a reports back.

---

## 8. What could not be verified

- **The base rate** of *"a merge broke another open PR and the conflict was confined to the allow-list"*.
  Resolution erases its own evidence, so it cannot be recovered from git history; 2a collects it going
  forward.
- **How often a picker carries an overlap block at all.** A block that fires on 3% of pickers is a
  different feature from one that fires on 60%, and phase 1 records nothing (§4.6), so this stays
  unmeasured by design.
- **Whether `gh pr list --json files` truncates for a large PR.** A truncated list would cause a
  **missed** overlap, the bad direction. Phase 1 carries the suspicion: a row whose file list reaches
  `pr_overlap.FILE_LIST_SUSPECT` is marked `files_truncated`, deliberately **not rendered** (a caveat
  about GitHub's pagination is not something the owner can act on).
- **The 4096-character ceiling in practice** — handled by a bounded test that re-derives the structural
  maximum rather than a number typed here.
- **Historical `mergeable` / `mergeStateStatus` values** — GitHub reports `UNKNOWN` for a merged PR, so
  2a snapshots both fields at detection time.
- **The base rate of class 1 versus class 2**, and **how often §5A.3 makes the rebaser wait** —
  `rebase_would_suffice` and `approval_live_at_detection` in §5A.7.

---

## 9. Deliberately not in scope

- **Widening `merge_guard`'s docs-only allow-list** (e.g. to the pointer ledger). It arguably meets the
  membership test, but widening it deserves its own change, argument and review.
- **Making `check_context_budget.py` blocking in CI.**
- **Splitting or reformatting the budget ledger.** It is one reviewable file on purpose.
- **Preventing the conflict.** §1.3. Nothing can.
- **Serialising delegated jobs so two PRs are never in flight.** The parallelism is the feature; the
  surprise is what wants fixing.
- **Semantic conflicts** (A renames, B calls) — invisible to both signals in §4.2 and to git. CI on the
  merged base is the only thing that catches them.

---

## 10. What the tests assert

No counts — CI measures the suite every run. The properties:

**Phase 1.** Intersection is exact on forward-slashed, case-sensitive paths; drafts and red PRs are
included and closed/merged are not; the rendered body **never** contains predictive wording (asserted
against literal strings); the option list is byte-identical with and without an overlap; the bound is
applied and what it dropped is named; **a `gh` failure of every kind still produces the picker,
unwarned**; the module writes nothing (asserted against its source); an in-flight job's worktree diff is
read against its recorded base, else `origin/<configured base>`.

**Phase 1.5 (§4.7).** The `↳` line appears only in its two positive states and is absent when either
store is unreadable; the `• ` line is byte-identical with and without it; the `↳` line is not quoted;
it never contains *dead*, *invalid*, *will*, *conflict* or *rebase*; the bounded test re-derives the
maximum with the new lines.

**Phase 2R (§5A).** `pr_rebase.py` does not name a merge subcommand, `record_approval`, `push`,
`--force` or `git` — asserted against the file; `UNKNOWN` produces no call; a live approval and a
pending picker at the current head each independently produce no call — two tests, because a single
test passing for the wrong reason is how a two-clause guard ends up with one clause; a `CONFLICTING` PR
is repaired through both of those, in a test that names §5A.3 clause 4 so it cannot be "fixed" later;
an unreadable store and a raising verifier each produce no call; the same base SHA twice produces one
call; the race path produces exactly one push naming the PR and both SHAs; the ceiling binds and says
so once.

**Phase 2 (§5).** `pr_repair.py` names neither a merge subcommand nor `record_approval`; every
non-allow-listed conflicting path, and every delete or rename, gets one notice and no job; a Markdown or
ledger conflict gets exactly one job per `(PR, base sha)`; a stale `CONFLICTING` is confirmed by a fresh
read; a cap refusal records nothing and retries; quiet hours defer; the kill switch disables the pass;
the brief forbids force and merging, spells `origin/<base>` in full and runs the tree's checks; the
target repository and base branch come from `repo_config` at call time.

**§5B.** An in-flight PR is drafted and skipped — never classified, rebased or repaired — and a failed
draft attempt is deferred rather than dropped.

---

## 11. Summary of the decisions this spec makes

| # | Decision | Section |
|---|---|---|
| 1 | Ordering cannot prevent the conflict; it only chooses who rebases | §1.3 |
| 2 | The post-merge budget number is **measured from the merged tree**, never summed; a mismatch's direction is the diagnosis | §1.4, §5.3 |
| 3 | Phase 1 lives in `merge_guard`, computed by a lazily-imported `pr_overlap.py` | §4.1 |
| 4 | The signal is the **file-list intersection**, neutralised by **stating the overlap, never predicting the conflict** | §4.2 |
| 5 | The overlap block is body text on the existing question — not a question, not an option | §4.4 |
| 6 | Ordering was measured, found weak, and is not emitted | §4.5 |
| 7 | Repair triggers off GitHub's own verdict, read by the daemon's existing PR-watch task | §5.1 |
| 8 | Repair acts only on an allow-list (two ledgers, and Markdown by keeping both sides); code and config get a notice | §5.2, §5A.9 |
| 9 | Repair never merges, never touches an approval, and lets the existing path re-ask | §5.4 |
| 10 | Every failure stops, changes nothing, and a human-needed stop says so once per head | §5.5 |
| 11 | There are **two repair classes**; the trigger is *"is this verdict still about today's base"* | §1.6 |
| 12 | The cheap class is **one server-side rebase**, with no local checkout | §1.7, §5A.1 |
| 13 | The rebaser has **one trigger, the sweep** | §5A.2 |
| 14 | **It may not move a head carrying a live approval or a live question**, and cannot-tell means don't; the one exception is `CONFLICTING` | §5A.3 |
| 15 | Only two rebaser outcomes push: a destroyed tap, and a ceiling that stopped retrying | §5A.5 |
| 16 | Every detection is a 2a row carrying `rebaser_live`, so populations stay separable | §5A.7, §5A.9 |
| 17 | The target repository and base branch are **configuration** (`repo_config`), resolved per pass | §5A.9 item 11 |
| 18 | A PR a running job opened is a draft until the job ends, enforced from the read side | §5B |
| 19 | The consequence line states **what is already at stake**, never what the merge will do | §4.7 |
| 20 | The second tap is **held pending 2a**, and phase 2 assumes it stays | §7 |
