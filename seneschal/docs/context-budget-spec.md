# The context-budget ratchet — a byte budget and a dangling-pointer check

**Status:** `PARTIAL(phases 0, 1 and §14)` — phase 0 (both checks, report-only) and phase 1 (the pointer check blocking in CI) BUILT, §14's CRLF-phantom fix BUILT; phases 2-3 designed, not built — the byte cap itself is still report-only.

**Phase 0 — built.** Both checks written, both wired into CI, both report-only at that point (exit 0
by design). `seneschal/context-budget.json` seeded from *measured* sizes; `seneschal/context-pointers.json`
near-empty on purpose, since phase 0's output *is* the survey that fills it. **No byte trimmed from
`CLAUDE.md`** — the trim is not in this spec (§10) and neither is §5's producer change.
**Phase 1 — built** (§6's table, and §6 is the authority): the **pointer** check turned blocking — CI
runs `check_context_pointers.py --enforce`, and every standing finding (76, when this was first done)
was resolved first, because a gate red on the day it lands is a gate someone disables. **The byte-budget
check is still report-only**; that is phase 2. What *does* block on the budget side is narrower:
`--enforce-headroom`, a shape check on a **new** `raises[]` entry (§3.4's successor rule), which CI runs.
**§14 — built.** **Phases 2-3 designed, not built.** Also built beside the phases, report-only: the
after-merge measurement (§12) and the `raises[]` chain-continuity check (§15.6).

**Reviewed against a later remediation design, and NOT retired.** That review put a newer design ahead
of this document wherever the two disagree, with retiring most of this one offered as an option if
necessary. On audit it was not: check #2 in full (§4), the CRLF trap (§14), the cross-PR-hole incident
(§12), the two-hard-routers decisions (§13) and the honest-attribution convention (§15) describe
mechanisms untouched by the one thing that actually needed fixing, and they are the cited authority for
sibling documents (`scripts-subdirectories-spec.md`, `concurrent-pr-collisions-spec.md`,
`grounding-restructure-spec.md`, `spend-levers-spec.md`), both check modules and both their test suites
— retiring the file would have dangled all of them for no gain. **What actually needed fixing was
narrower: §3.4's ratchet convention — a raise sets `max_bytes` to exactly the measured size — is a
"high-water mark, not a limit" defect, and its successor rule (headroom ≈ four weeks of the artifact's
own trailing growth, floor ~2k tokens, denominated in time) now lives in code, in
`check_context_budget.py`'s docstring (*Headroom*) and `headroom_bytes()`.** §3.4, §5 and §6 below
point at it rather than re-describe it. §11's five open questions were updated in the same review: Q1
is now decided, Q3 was measured rather than left as a guess, Q4's wording is corrected as stale (the
scope it asked about moved twice since it was written), and Q2 and Q5 carry forward exactly as open as
they were — none of the five is silently dropped.

**The first time the report-only gate was actually worked**, it had accumulated **six** over-budget
artifacts; all six went back under. Four by relocation (the scripts sub-router −13,217 B, the Watch
mode body −2,744, the Dream mode body −1,645, the Reminders mode body −327), two by a reasoned
`raises[]` entry — the root, over by the 24 bytes of one CI job name, and `subagents/reminders/SKILL.md`,
whose remaining 825 B was the seed pass's executable state machine after 799 B of narrative had been
relocated. **What the exercise showed about §3.7's Goodhart trap:** every destination was checked to
already carry the fact in fuller form before a byte was removed, and in the two cases where it did not
(`INTEGRATIONS.md`, the loop's as-deployed architecture) the text was **moved verbatim** rather than
summarised — which is the grounding restructure's discipline, borrowed because this spec does not state
it.
**And then, the same morning, the sweep's own PR exposed a hole in the design: §12.** Two PRs that were
each green put `develop` over on **five** artifacts, and neither could see it, because the check only
ever asked *"did this branch grow it?"* against a merge base that had stopped existing — **then it
happened a third time, to the branch fixing it.** The after-merge measurement (`git merge-tree`,
report-only) and the `fetch-depth: 0` CI change that makes it — **and the raise ratchet, which had
been silently bootstrapping in CI on every run** — actually work, both shipped as a result.

**Scope:** checks **#1** (byte budget per grounding file) and **#2** (dangling-pointer check) of the
four proposed — the owner scoped the work to these two explicitly.
**Siblings:** `mouth-spec.md` (the shape this follows — measure first, phase 0 changes no behaviour,
name what it deliberately does not do); `background-jobs-spec.md` (a contract worth having is written
in code).

**The ask**, paraphrased as a requirement: *rules and code that enforce, going forward, that
`CLAUDE.md` does not regrow to the size it reached; that information stops being duplicated; and that
the assistant is not lobotomized in the process.*

Three clauses. **This spec answers the first. It does not answer the second or the third** — §10 says
so plainly rather than leaving the owner to find out.

---

## 1. The thesis, in one paragraph

The trim is a weekend; the ratchet is the engineering. A cleanup that isn't defended regrows, because
the growth has a **producer**, and the producer is us — faithfully following a standing instruction to
keep docs in sync with code. So: name the producer and change it (§5), put a cheap deterministic gate
under it (§3), and ship the pointer check **with** it rather than later (§4), because a router
system's real failure is not bloat but a pointer into nothing.

---

## 2. What was actually true at design time

Measured against `origin/develop` in a development clone, and against the live daemon's
`seneschal/state/metrics.jsonl` (read-only). The figures below are **illustrative magnitudes from the
tree this design was worked out on**, not this repository's current sizes — `seneschal/context-budget.json`
carries those. Nothing here was restated on anyone's authority, including the owner's — a spec whose
thesis is *measure, don't assert* cannot open with numbers it took on trust.

### 2.1 The grounding files

`git ls-files` + file size (working tree), and `git cat-file -s HEAD:<path>` (git object):

| File | Worktree B | Git-object B | ~tokens (est. 4:1) |
|---|---:|---:|---:|
| `CLAUDE.md` | 119,801 | 118,537 | ~29,600 |
| `seneschal/SKILL.md` | 78,141 | 77,296 | ~19,300 |
| the warm-session grounding literal (`presence.py`) | 9,935 | — | ~2,500 |
| the persona file | 6,659 | 6,566 | ~1,600 |
| `phone/CLAUDE.md` | 4,942 | — | ~1,200 |
| the owner profile | 3,146 | — | ~800 |
| the resume preamble literal (`presence.py`) | 422 | — | ~105 |
| 14 × `subagents/*/SKILL.md` | 4,054–12,154 (98,360 total) | — | ~24,600 total |

**Exactly two** `CLAUDE.md` files existed (root, and `phone/`'s) and **zero sub-routers**. The top two
files were ~49k estimated tokens between them.

Two corrections found by measuring:

- **The grounding literal was 9,935 B, not the "~8 kB" the root router described.** An earlier
  research note recorded the trajectory in one line — 5.3 kB when first measured, ~8 kB / ~2k tokens
  later. So: 5.3 → 8 → 9.9 kB, each figure correct when written and stale within weeks. **A number
  maintained in prose is a number that is wrong**; that is the smallest possible argument for this
  spec.
- **Worktree bytes ≠ repository bytes on an `autocrlf` host.** With `core.autocrlf=true` and no
  `.gitattributes` pin, every markdown file's worktree size exceeds its git-object size by exactly its
  line count (`CLAUDE.md`: 119,801 − 118,537 = 1,264 = its LF count exactly; `seneschal/SKILL.md` 845).
  CI is `ubuntu-latest` and reads the LF form; a Windows development box reads CRLF. **A byte gate
  that doesn't pin which it counts is non-deterministic across platforms by ~1%.** §3.2 decides it.
  (This tree now carries a repo-wide `* text=auto eol=lf` in `.gitattributes`, so a fresh checkout is
  LF everywhere — but the check normalises regardless, because it cannot know how a given checkout was
  made.)

### 2.2 The economics — why startup context is the expensive part

`seneschal/state/metrics.jsonl` — the per-turn rows `presence.py` writes **in code**. A **live** log,
so this is a snapshot: read at **713 rows, 88 warm sessions**, about a week of `mode: Chat` turns.

| Measure | Value |
|---|---:|
| Sessions serving **exactly one turn** | **49 / 88 = 55.7%** |
| **Median** turns per session | **1** (mean 8.10 — a long tail of 20–47-turn sessions) |
| Mean `cache_read_input_tokens` | 551,514 |
| Mean `context_tokens` | 132,013 |
| **Read amplification** | **4.18×** |
| **First-turn `context_tokens`, median** | **75,462** (n=76 successful; p25 68,084, p75 87,632, min 41,531) |
| First-turn `cache_creation`, median | 88,184 |
| Spend on first turns | **31.8% of the window's spend, on 12.3% of turns** |

**Startup context was ~75k tokens and the median session never amortized it.** Under
`governor.TOKEN_WEIGHTS` (the billable basis — cache read 0.1×, 5-min write 1.25×, 1-hour write 2×,
input/output 1×), writing N tokens once and reading them over *k* turns costs `N·(2.0 + 0.1k)`; not
caching costs `N·k`. **Caching only wins at k ≥ 3.** The median session is k = 1, so the cache write is
a **2× penalty** on all ~75k tokens.

Across the window: cache reads **56.3%** of billable, 1-hour writes **36.4%**, output 3.1%, 5-min
writes 4.1%, input 0.1%. **Better than a third of the bill was writing startup context into a cache the
median session throws away.** That is the number the trim moves, and §7 commits to reporting it before
and after.

### 2.3 The producer — named first, because a budget over an unchanged producer is theatre

`CLAUDE.md` did not drift. It did what it was told. `git log --first-parent` (what actually landed on
`develop`, not intermediate commits inside branches), sizing each blob with `git cat-file -s`, over
about five weeks:

| File | Size over the window | Merges changing it | ↑ | ↓ | Mean Δ | Net rate |
|---|---|---:|---:|---:|---:|---|
| `CLAUDE.md` | 5,232 → 118,537 B (**22.7×**) | 81 | **81** | **0** | +1,399 B | 3,237 B/day (~809 est. tok/day) |
| `seneschal/SKILL.md` | 6,886 → 77,296 B (**11.2×**) | 56 | **56** | **0** | +1,257 B | 2,012 B/day (~503 est. tok/day) |
| `references/proposed-learnings.md` | 2,156 → 65,100 B | 30 | 26 | 4 | +2,527 B | 1,851 B/day |
| the persona file | 3,495 → 6,566 B | 9 | **9** | **0** | +341 B | 88 B/day |

**137 merges touched the two big grounding files. 137 made them bigger. Zero made them smaller.** The
ratchet already existed and already turned one way; this spec reverses its direction. The only
grounding-adjacent file that had ever shrunk on `develop` was `proposed-learnings.md`, four times — and
that is Dream's gated file, the one place a reduction is already somebody's explicit job. At that rate
the two added **~1.3k estimated tokens of startup context per day, forever.**

**Who writes it: the feature commits themselves**, not a separate sync pass — the biggest jumps were
`docs(jobs):` (+6,904), `fix(governor):` (+4,544), `feat(mouth):` (+3,888). Every shipped feature
carried its own paragraph into the root.

**And the instruction that causes it is not in this repo.** Grepping the tracked tree for anything
telling an agent to update `CLAUDE.md` returned **nothing** (the README merely links to it). All three
producing instructions were host-side and unversioned:

| # | Where | What it says |
|---|---|---|
| 1 | `~/.claude/CLAUDE.md` | a line telling the agent to run `/sync-claude-md` to update any `CLAUDE.md` that has gone stale |
| 2 | `~/.claude/CLAUDE.md` | **whenever code changes, correct the docs that describe it, in the same change, unprompted** — `CLAUDE.md` files included; **stale documentation is a bug** |
| 3 | `~/.claude/skills/sync-claude-md/SKILL.md` (2,460 B) | its only structural guidance for the repo-structure row: *add the new entries* |

Instruction #2 is the real producer — unconditional, standing, global, *in the same change,
unprompted*, exactly the commit pattern above. **None of the three says a word about *where* the
detail should land.** No rule is being violated; a rule is being followed, and it is under-specified.

**Consequence: a PR in this repo cannot fix the producer.** The same constraint a host-side slash
command hit, and that `job-origin-routing-spec.md` §3.3 and `mouth-spec.md` §7.3 both hit. §5 writes the
host-side edit out in full so it is copy-paste rather than rediscovery; §6 gates phase 2 on it.

### 2.4 Corroboration, and the order it arrived in

§2.2's measurement was taken **before** an Anthropic engineering article on context engineering for
the Claude 5 generation of models was read — by a few hours. The order is recorded on purpose: the
article is **corroboration, not the source.** Had it come first, the honest thing would be to say so.

It reports removing the large majority of Claude Code's own system prompt for the newest models with no
measurable loss on coding evaluations, and names the mechanism §5 depends on: a **tree of files loaded
at the right time**, and a `CLAUDE.md` kept lightweight — a brief description of what the repo is for,
with most of the tokens spent on the codebase's gotchas. It gives no size threshold, so it does not set
our numbers — §3.3 seeds those from measurement.

---

## 3. Check #1 — the byte budget

### 3.1 Which files are budgeted

**A file is budgeted if it is loaded without being asked for.** A budget on files nobody loads is
theatre; a file read on every cold spawn is where the money is.

| Tier | Files | Budgeted? |
|---|---|---|
| **A — every cold spawn** | `CLAUDE.md`, `seneschal/SKILL.md`, `persona/persona.default.md`, the warm-session `GROUNDING_TEMPLATE` literal | **Yes — these are the ones that matter** |
| **B — loaded just-in-time by a mode** | `subagents/**/SKILL.md`, `seneschal/modes/*.md`, the sub-routers (`phone/CLAUDE.md`, `cockpit/CLAUDE.md`, `archons/CLAUDE.md`, `seneschal/docs/CLAUDE.md`, `seneschal/references/CLAUDE.md`), `.claude/rules/**/*.md` | **Yes**, looser — a mode still pays for its whole skill |
| **C — read on demand, by pointer** | `seneschal/references/*.md`, `seneschal/docs/*.md` | **No** — these are the *destination* of §5's producer change |

**`.claude/rules/**/*.md` is tier B, and the tier is the whole decision.** A rule file with `paths:`
frontmatter *looks* like tier C — it sits outside the `CLAUDE.md` namespace and does not load at
startup — but `scripts-subdirectories-spec.md` §4 measured it and it behaves like grounding on every
axis that matters here: it is injected on a `Read` of any file its globs match, it is **obeyed as
instruction rather than read as reference**, and it is **re-attached after compaction** (§4.1 there).
Left unbudgeted it would be the cheapest possible way to make this check report a smaller number while
the tree got no smaller — the `CLAUDE-2.md` trick of the paragraph below, one directory over. Enforced
**before the directory has any file in it**, deliberately: the hole's shape was already known, and the
first rule file anyone writes should not be the one that discovers it. The subdirectories work measured
a candidate and declined to create it (`scripts-subdirectories-spec.md` §5), so the directory is empty
and the glob matches nothing — which is what enforcement over an allowlist is *for*.

Tier C is the load-bearing exclusion: budget the leaves too and "move the detail to the leaf" stops
being available, leaving deletion as the only way to pass — the failure mode §3.7 and §10 are about.

**The string literals are budgeted like files.** The grounding literal is startup context living in a
`.py`, and §2.1 shows it outgrew its documentation. Measured by parsing `presence.py` with `ast` and
reading the named module-level `ast.Constant` — no import, so the daemon's dependencies aren't needed
and CI's dependency-free job can run it. The entry is named `path::NAME`
(`seneschal/scripts/presence.py::GROUNDING_TEMPLATE` in this tree; the budgeted object is the
*template*, since the rendered grounding substitutes the owner's identity at runtime).

**A new grounding file with no budget entry FAILS** — the difference between a gate and a decoration.
If unbudgeted files passed silently, the cheapest permanent way to satisfy check #1 would be to create
`CLAUDE-2.md`. The check globs tier A + tier B's patterns (`BUDGETED_GLOBS`: every `CLAUDE.md` at any
depth, `subagents/**/SKILL.md`, `seneschal/SKILL.md`, `persona/*.md`, `.claude/rules/**/*.md`); any
match without an entry errors, printing a ready-to-paste stanza with its measured size — one reviewed
line, not a silent 20k tokens. **Only *tracked* matches are policed** — a per-install persona or owner
profile (both gitignored), or a host-local rule file in one owner's own checkout, is not this repo's
grounding, and a check that failed on one would fail for something no PR could fix.

### 3.2 Bytes, and which bytes

**Bytes, not tokens.** Deterministic, free, dependency-free, stable across model changes. A tokenizer
drifts with the model — the number would move without the file moving, the one property a ratchet
cannot have — and `pyproject.toml` allows **exactly two** runtime dependencies (`websockets`, `tzdata`)
by explicit design. Adding tokenizer weight to power a lint is the worst trade in the repo. **Tokens
are reported as a labelled estimate at a fixed ratio** (`BYTES_PER_TOKEN`): `118,537 B (~N est.
tokens)`. The *est.* is not decoration — same reason `governor.py` makes every rollup name its basis.
Nothing decides on the estimate; the gate compares bytes. (The ratio was designed as 4:1; §11 Q3
measured it at ~2.59 bytes/token for this tree's prose, and the module now uses the measured figure.)

**Count git-object bytes, not the working tree** (§2.1). The check normalises `\r\n` → `\n` before
counting (equivalently `git cat-file -s`), so a Windows host and CI agree exactly, and a PR that only
changes line endings cannot consume budget.

### 3.3 Where the budgets live

**`seneschal/context-budget.json`** — tracked, reviewable, beside the subtree it governs. Deliberately
**not** under `seneschal/state/`: that is gitignored, and a budget that cannot appear in a diff cannot
be defended in review, which is the entire mechanism of §3.4.

Format follows `state/governor-config.json` + `governor.py`'s `SCHEMA`: one entry per artifact, `note`
carrying the *why*, and the schema in the Python module rather than duplicated in the JSON. Seeded
entries start with `raises: []`; the second entry below shows the shape *after* a later raise.

```json
{
  "_basis": "git-object bytes (LF-normalised). Token figures anywhere are labelled estimates.",
  "_seeded_from": "measured sizes on the tree that introduced this file",
  "budgets": {
    "CLAUDE.md": {
      "tier": "A", "max_bytes": 118537, "raises": [],
      "note": "Router to the tree. Detail belongs in the leaf it governs (§5)."
    },
    "seneschal/scripts/presence.py::GROUNDING_TEMPLATE": {
      "tier": "A", "max_bytes": 10400,
      "note": "Module-level literal, read by ast. Every cold spawn pays this.",
      "raises": [{"date": "<YYYY-MM-DD>", "from": 9935, "to": 10400,
                  "reason": "Mouth phase 3: the assertions tail joins the grounding block."}]
    }
  }
}
```

### 3.4 The ratchet, and its escape hatch

**Mechanism.** Read `seneschal/context-budget.json` at HEAD and at the **merge-base with
`origin/develop`**; fail when any `max_bytes` is **greater** than its merge-base value. Merge-base, not
tip: comparing against a moving tip makes an unrelated merge fail your branch, and a check that fails
for reasons the author didn't cause gets disabled. Absent at the merge-base ⇒ every entry is new and
permitted; that is how it bootstraps.

**The escape hatch, because there must be one.** A raise is legitimate; an *unexamined* raise is not.
**Any increase must append an entry to that file's `raises[]`, whose `to` equals the new `max_bytes`
and whose `reason` is non-empty.** Missing, unchanged, or mismatched ⇒ fail, naming the exact stanza
required.

Alternatives rejected: a separate **budget-raises log file** — a second file that must agree with the
first is precisely the duplication this repo is trying to stop, and it goes stale silently. A
**commit-message trailer** — invisible after the fact; you cannot read a number's history by looking
at the number. `raises[]` puts the history where the raise is, so the fifth raise is legible in the
same hunk as the fourth.

**What stops it becoming a rubber stamp — honestly, code cannot.** Any hatch a human can fill in, a
human can fill in carelessly. What code can do is make the *pattern* impossible to miss: `raises[]` is
append-only and never pruned, and the output prints the count and last three reasons, so a file raised
five times in a month reads as *"raised 5× since <date>"* in the CI log and the PR. **The defence is
review, and the reviewer is the owner**; the check's job is to put the evidence in front of them at the
moment of the raise.

**SUPERSEDED — and this is now the load-bearing correction.** "A raise sets `max_bytes` to exactly the
new measured size" — the convention this subsection originally described — is a defect. The intent of
a budget was always *"if it gets bigger than this, look at what's really important in it"*; what the
convention produced instead was a threshold set to the sizes on the day the check shipped, then only
ever raised to each new size and never above it. A cap that always equals the current size carries
zero headroom, trips on any growth, and turns every raise into paperwork that records what already
happened rather than constraining anything — exactly how a budget becomes a changelog. **The escape
hatch mechanism above (append a reasoned `raises[]` entry, `to`/`reason` validated, history kept beside
the number) is unaffected and stays.** What changes is what a raise sets `max_bytes` *to*: not the exact
measured size, but measured size plus headroom — **approximately four weeks of that artifact's own
trailing growth rate, floor ~2k tokens, denominated in time, not bytes.** That rule is **built**:
`headroom_bytes()` computes it (with a step-change reset so a router diet does not read as four weeks
of growth, and the floor when history is too short to trust a rate), `check()` reports it beside every
artifact, and `--enforce-headroom` — which CI runs — fails a **new** `raises[]` entry whose `to` does
not equal the computed value, so a raise cannot be by the wrong amount because the number is computed,
never hand-typed. §6 below records what this means for phase 2's own gating.

### 3.5 What CI reports on failure

A gate that says *"too big"* without saying *"these 40 lines are new"* gets worked around.

```
FAIL  CLAUDE.md
      budget 118,537 B (~N est. tok) | actual 122,904 B | +4,367 B (+3.7%)  [merge-base <sha>]
      what grew:  §"Repo state" +2,981 B · §"Architecture" +1,102 B · §"Dependencies" +284 B
      Detail belongs in the leaf (§5). To raise instead, append to raises[].
```

Section attribution (`section_growth`) is a `git diff` against the merge-base blob, bucketing each hunk
under the nearest preceding heading — approximate by construction (a hunk spanning a heading is charged
to the first) and worth it: it turns *"the file is too big"* into *"the Repo-state section grew 3 kB"*,
which names the paragraph to move. For the `presence.py` literals it degrades to a plain line diff.

### 3.6 Auto-tightening — the check does not; Dream proposes

**The check never rewrites its own config.** Auto-tightening inside the PR that shrank the file is the
real ratchet — and also how an unrelated PR fails a week later for something it didn't cause, never
having been told the number moved. Never tightening leaves no downward pawl at all.

The split resolves both. CI stays deterministic and read-only over its own config, which is what makes
it trustworthy; the pawl runs nightly on the cadence this repo already uses for gated proposals.
**When a budgeted file has sat ≥10% under budget for ≥7 consecutive days, Dream opens a PR tightening
it to `actual + 5%`**, measurement in the body — hysteresis in both constants so a file oscillating at
the boundary doesn't generate nightly PRs, and a normal reviewable PR, so a tightening the owner
disagrees with is closed, not fought. (Not built — this is phase 3.)

### 3.7 The Goodhart trap — a knowingly gameable gate

**Read this before trusting a green check #1.**

The two cheapest ways to pass a byte budget are **(a) move text somewhere unmeasured** — a tier-C leaf
nothing routes to, a docstring, an already-bloated reference, a new grounding file (mitigated in §3.1)
— and **(b) delete something load-bearing**, which the budget cannot distinguish from deleting an
obsolete paragraph.

**The two checks that make a byte budget honest are check #3 (orphan/reachability) and check #4
(duplication), and both are out of scope here.** The owner scoped this spec to #1 and #2; that is their
call and it is recorded as theirs. The consequence is recorded too, unsoftened: **as specified, check #1
ships as a gate satisfiable by relocating text into an unreachable file, and check #2 will not catch
it** — #2 verifies that pointers resolve, not that files are pointed *at*. Not hypothetical: it is the
path of least resistance under deadline, and it converts a bloat problem into a **knowledge-loss**
problem, which is worse and much harder to notice.

**Interim mitigation, designed.** Check #1 should additionally report on every run, including green
ones, the **total bytes of the whole routed tree** — every tier A/B file plus every tier C file
reachable from one via the §4 pointer graph — as an unbudgeted, non-failing line:

```
routed tree: 47 files, 1,204,881 B (~N est. tok)   [merge-base 1,198,433 B, +6,448]
```

A pure relocation leaves the per-file number down and this one flat or up. It **does not block** —
that is check #4's job — but it makes the move **visible in the same output**, so "the root shrank"
and "nothing was removed" appear together. An accepted risk, the mitigation named as partial. **Not yet
in the module:** today's report prints the total over the *budgeted* artifacts only, which a relocation
into tier C lowers rather than holds flat.

---

## 4. Check #2 — the dangling-pointer check

### 4.1 Why it ships with #1, not after

The failure mode of a router system is a pointer to a renamed file — after which the knowledge is
**unreachable**, worse than duplicated, because duplicated knowledge is at least present. §5's
producer change deliberately moves knowledge from inline to one hop away, raising this bug's blast
radius. **Check #2 is the harness that makes §5 responsible to do**; trim first, harness later is the
wrong order. That it finds real bugs is not a prediction — a throwaway prototype found one (§4.5).

### 4.2 What counts as a pointer

Recognised: **markdown links** `[text](path)` and **backticked path-shaped tokens**
`` `seneschal/scripts/jobs.py` ``. A token is path-shaped if it contains `/`, no whitespace, and matches
`^[A-Za-z0-9_.~*<>/@-]+$`.

Deliberately **not** recognised: bare unbackticked path-shaped tokens in prose. Opening the recogniser
to bare prose multiplies the candidate set by roughly an order of magnitude for a pointer style this
repo doesn't write — all 951 pointers the prototype found were already backticked or link targets.
**False positives kill adoption faster than false negatives**, and a check nobody trusts is worth less
than no check.

### 4.3 The resolution ladder

A pointer resolves if **any** rung matches; it is reported only if none does.

**Rung 0 — bases.** Try repo-root, then **every ancestor directory** of the containing document (its
own directory first), then any **declared base** for that document (§4.6). Measured at design time, of
610 resolving pointers: **56.8% repo-root, 25.1% doc-relative, 18.1% relative to the `seneschal/`
subtree.** No single base works — `CLAUDE.md` writes `` `scripts/router.py` `` and
`` `references/archons.md` ``, subtree-relative because the surrounding block describes the
`seneschal/` subtree. A single-base recogniser reported 619 findings on a clean tree and would have
been deleted the same day.

**Rung 0′ — the exception: an explicitly-relative pointer gets ONE base, its own document's directory,
and the ladder above must not touch it.** A leading run of `../` is not a guess about where the reader
is standing; it is a traversal the author wrote, and "up from *here*" is only meaningful if *here* is
fixed. Let the ancestor walk near it and the check dissolves: a pointer written as
../subagents/store-qa/SKILL.md inside seneschal/modes/ask.md would resolve through the `seneschal/`
ancestor as seneschal/../subagents/store-qa/SKILL.md = `subagents/store-qa/SKILL.md` and report clean —
while the pointer as written means seneschal/subagents/, which has never existed. **A pointer wrong by
exactly one level is invisible to a resolver that tries every level**, and that is how eleven of them
survived in the mode bodies (§4.9). If the normalised path lands above the repo root the pointer is
external after all — a sibling repository — and is skipped as the decision it is, not reported.

**Rung 1 — tracked.** In `git ls-files`, or a directory prefix of a tracked path. **610 of 951
(64.1%)** in the prototype.

**Rung 2 — declared runtime.** `git check-ignore` says a tracked `.gitignore` rule covers it. **264 of
951 (27.8%)** — `seneschal/state/run-log.md`, `state/metrics.jsonl`, an archon's `state/` files, and the
rest of the gitignored-but-real world.

This rung replaces the hand-maintained runtime allowlist, and is the better answer: **the declaration
already exists, in the repo, beside the thing it describes, written by whoever created the runtime
path.** Each archon carries its own `.gitignore` whose `state/*` line sits under a heading declaring
that directory runtime churn, never in git. Files under it exist on the daemon's box and in no clone;
a naive `ls-files` check calls them dangling and is wrong. A hand-maintained allowlist would need
~264 entries and would rot; `check-ignore` needs zero, because it reads the rules git reads.

**The honest limit, because it changes what a green means:** `check-ignore` proves *intent*, not
*existence* — it cannot tell `state/run-log.md` (real) from `state/typo-log.md` (never existed), both
matching `seneschal/state/*`. So check #2's guarantee is **"no dangling pointer to a path that would be
tracked"**, not "every pointer resolves". The right trade: the alternative fails on a fresh clone and
in CI, i.e. is unusable.

**Rung 3 — allowlist** (§4.6).

### 4.4 The hard cases, each with a decision

| Case | Example | Decision |
|---|---|---|
| **Placeholders** | `state/jobs/<id>.json` | Treat `<…>` as `*`, glob-expand, **require ≥1 match**. A placeholder expanding to nothing is exactly the bug we want. A placeholder in the **first** segment stands in for a root (`<cwd>/…`, `<out_dir>/…`) and is skipped — nothing repo-relative can resolve it. |
| **Globs** | `seneschal/scripts/test_*.py`, `phone/android/**` | Same rule — expanded against tracked files **and** gitignore rules, so a glob over runtime output passes on rung 2. |
| **Deliberately external** | `~/.claude/commands/assistant.md`, an absolute drive-letter path, `SCRIPT_DIR/../..` | **Skipped, never reported.** Prefix-recognised: `~/`, `./`, leading `/`, drive letter, any backslash; plus any `..` **after a real segment**, which escapes from the middle. **Narrowed later:** a *leading* run of `../` is no longer external — it is a pointer, resolved document-relative by rung 0′. The old rule skipped **413 tokens, the single largest skip class**, and they were mostly the repo's own cross-directory pointers (`../../persona/persona.default.md`-shaped) rather than another repo's; it is what hid §4.9. A leading-`../` token that normalises above the repo root is still external, but only rung 0′ can say so, because the answer depends on the document. Also an *implementation* requirement — §4.7 trap 2, which has its **own** predicate (`_git_batch_safe`) rather than sharing this decision's. |
| **URLs** | `https://…`, `github.com/<owner>/<repo>` | Skipped by scheme; a first segment with a dot **and** a known TLD is a host, not a path. |
| **Not-paths that look like paths** | `and/or`, `they/them`, `try/except`, `A/B`, `0/7`, `Multiple/day`, `save/load_daemon_state` | Stoplist plus shape rules: all-short-lowercase-words with no extension is prose; a two-part numeric or per-unit token is a ratio; an extensionless run of four-plus segments, or one with an underscored identifier, is a field/function enumeration; `mcp__` is a tool name. |
| **Schema identifiers** | `seneschal.job-leads/1`, `seneschal.outbound/1` | **Skipped.** `^[a-z][a-z0-9.-]*/\d+$` is this repo's schema-version idiom. 14 in the prototype — the largest false-positive class without this rule. |
| **HTTP routes** | `/api/status`, `/auth/login` | Skipped by the leading-`/` rule, which earns its keep twice. |
| **Git refs / branches** | `origin/develop`, `feat/archon-forge` | Skipped: `origin/*`, and no-extension tokens whose first segment is a branch prefix (`BRANCHY_FIRST`). 10 in the prototype. Narrow on purpose — `seneschal/SKILL.md` has an extension and is not skipped. |
| **Git internals** | `.git/info/exclude` | Skipped: real files git owns, never tracked, present or not depending on the checkout's own state. |
| **Gitignored-but-real** | `seneschal/state/run-log.md` | Rung 2. |
| **Forward references** | `seneschal/scripts/advisors.py` — named as a **deliberately deferred** Option-B module (`seneschal/references/advisor-chain.md`) | **Allowlist, with `reason`.** A design doc naming a file it proposes is legitimate and common here; silently passing it is not. One reviewed line, and the line is a to-do with a home. |
| **Build artifacts** | the cockpit's `npm run build` output | Rung 2 where the output directory is gitignored (it is, here); **allowlist** for the small class that is neither tracked nor ignored. |
| **Paths quoted as examples** | This spec quotes a path that never existed *as the bug it found* | **Allowlist, per document** — or, as this document now does, quote it unbackticked. Found by running the prototype over this spec (§4.5). A doc *about* paths is full of paths that aren't pointers, and no shape rule tells a citation from a reference. Rare enough that one reviewed entry per doc is correct. |

### 4.5 The measured false-positive rate — and the bug the prototype already found

A throwaway prototype (scratchpad only; no code shipped with the design) implementing §4.2 and §4.3
over §4.7's scope:

| | |
|---|---:|
| Files scanned | 52 (1,034,765 B) |
| Backticked/linked tokens examined | 7,112 |
| Path-shaped candidates after skip rules | 951 |
| Rung 1 — tracked | 610 (64.1%) |
| Rung 2 — declared runtime | 264 (27.8%) |
| **Rung 3 — unresolved, i.e. reported** | **77 (8.1%)** |

Of those 77, classified by hand against §4.4: 28 doc-relative to an undeclared base, 14 schema
identifiers, 10 branch names, 8 prose slashes, 7 HTTP routes, 3 the `advisors.py` forward reference, 2
the cockpit build output, and 3 singletons (a bare URL, a literal `...` ellipsis, an external host-side
command file) — **and 2 were a real bug**:

> Both files under `seneschal/docs/spec-prompts/`, in their *Read first* list, instructed a reader to
> read a `CLAUDE.md` *inside the assistant subtree* — "repo overview, act-low/ask-high, conventions,
> CI". **That file did not exist**; `CLAUDE.md` is at the repo root. Two documents whose entire job is
> routing a fresh agent to its grounding, pointing at a path that had never existed.

Exactly the failure mode this check exists for, found before the check was built, in the two files
least likely to be noticed because nobody reads a spec-prompt twice. (Both now point at the root
`CLAUDE.md`.)

**The lesson the numbers carry:** without §4.4's decisions the check reports 77 findings of which 2 are
real — **2.6% precision**. That is not a check, it is a wall, and it is why §6 makes phase 0
report-only. With the decisions applied, expected steady state is ~2 findings plus a handful of
allowlist entries. **The gap between those two numbers is the entire engineering content of check #2.**

**Dogfooded:** the prototype was also run over *this* spec — 52 candidates, 26 tracked, 12 declared
runtime, 14 unresolved. All 14 were either files this spec **proposes to create** or paths it **quotes
as examples** of the classes §4.4 decides on. Both are real cases, both now have decisions, and the
second was found only by running it. A spec is the document most likely to trip this check, which is
worth knowing before phase 1 blocks anything.

### 4.6 The allowlist

`seneschal/context-pointers.json`, mirroring §3.3's style, **`reason` required and non-empty per
entry** — an exemption should be a decision someone wrote down, not a silent pass. Same principle as
§3.4's `raises[]`.

```json
{
  "bases": { "CLAUDE.md": ["seneschal/"], "README.md": ["seneschal/"] },
  "allow": [
    {"pointer": "seneschal/scripts/advisors.py",
     "reason": "Deferred Option-B advisor rails (advisor-chain.md Phase 3). A proposal, not built."}
  ]
}
```

Entries are exact strings, not patterns: a pattern-based allowlist grows to cover things nobody
examined, and the point is that each exemption was looked at once. A row whose pointer has started
resolving on its own is named in the report so it gets deleted.

### 4.7 Scope, and the implementation traps

**Start narrow, widen on evidence.** Phase 0 scanned tier A + tier B plus `seneschal/docs/**.md` and
`seneschal/references/**.md` — **52 files, ~1.03 MB** at design time — where the pointers that matter
live. Not `bench/`, `cockpit/`, `phone/`, `archons/`. **The widening path**, so it isn't a someday:
after phase 1 has been green one week, add all tracked `**/*.md` in one PR, report-only for that
increment, promoted once its false-positive classes are decided on the way §4.4 decided on these.
Widening is cheap; widening blind produces a 600-finding wall and a disabled check.

**Widened once on evidence, and the evidence was a live bug** (§4.9). The set above guarded the *docs*
and skipped the *instructions*: `seneschal/modes/**` — the mode bodies a run dispatches into by an
**imperative read** (`grounding-restructure-spec.md` §4) — was never scanned at all, and neither were
several of the sub-routers (`archons/`, `cockpit/`, the scripts directory). Those files are
*executed*, not merely read, so a wrong path there is a mode that cannot load its subagent — the worst
place in the tree for this bug to hide, and the one place the check wasn't looking. **The scope is now
pinned by a test** (`ScanScopeTests`), asserting against the glob list (`SCAN_GLOBS`) rather than
against file contents, so narrowing it again is a red test rather than a silent hole. The decisions had
tests from day one; the scope had none, which is why the gap was invisible.

**Widened once more, and this time BEFORE the bug rather than after it.** `.claude/rules/**/*.md` joins
the scan. It is the only prospective entry in the list: every other pattern was added after something
shipped out of the gap, and this one is added while the directory is still empty, because
`scripts-subdirectories-spec.md` §4 established that a path-scoped rule file is an **executed**
instruction file in exactly the sense `seneschal/modes/**` is — obeyed, not read. A prospective glob
forfeits `ScanScopeTests`' existence check, so it owes a **fixture** test proving the entry works, plus
a self-deleting guard that goes red the day a real rule file lands — otherwise the exemption rots into
exactly the silent hole this class exists to prevent.

**Do not confuse *scanning* a rule file with *resolving a pointer into* `.claude/`.** The first is
ordinary and machine-independent; the second is the third trap below, and it is why every `.claude/`
path stays on rung 3. Two facts, both checked rather than reasoned about: `paths:` globs resolve
against the **project root**, not the rule file's own directory — so a rule file gets no useful
ancestor base and must write repo-root-relative pointers — and a rules directory needs no `git add -f`
when the only local exclude covers `.claude/worktrees/`.

**A third trap, found by this gate going red in CI while green locally, on the very PR that turned it
blocking.** Rung 2 resolves a pointer if `git check-ignore` claims it — but `.claude/worktrees/` can be
ignored **in one checkout only**, via an *untracked* `.git/info/exclude`. A fresh clone has no such
entry, so the same pointer resolves at home and dangles in CI. **The gitignore rung is therefore
machine-dependent there**, which is exactly what a blocking gate must not be. Anything under `.claude/`
is allowlisted rather than left to rung 2 for this reason. The general lesson matches the other two:
the oracle's answer depends on the checkout, and only running it in both places reveals where.

**Two traps, both found by running `git check-ignore` rather than reasoning about it. Both make the
oracle silently under-report — i.e. emit false *dangling* findings, the failure that kills adoption.**

1. **`--stdin` must be `-z` (NUL-separated) and written as bytes.** In text mode on Windows the pipe
   translates `\n` → `\r\n`; git takes the `\r` as part of the pathname, quotes its output
   (`"seneschal/state/run-log.md\r"`), and **drops other entries entirely** — one of three test paths
   vanished. The `-z` form returned all three cleanly.
2. **One malformed path aborts the whole batch.** Feeding `/api/status` or a path escaping the repo
   exits **128** (`fatal: Invalid path '/api'`) and **everything after the bad entry is silently
   lost**, while the output produced before it looks like a normal success. So: filter
   absolute/external/`..` paths *before* batching (§4.4 already requires it), **and check the exit
   code** — anything but 0 or 1 means *the oracle failed*, which must abort loudly (exit 2,
   `OracleFailure` — a tool failure, never a finding), never degrade to *nothing is ignored*.

In the prototype these two cost 264 correct resolutions and would have reported `state/run-log.md` —
referenced 17× — as dangling. Invisible in review, obvious in a test.

(A fourth trap points the *other* way — the oracle over-reporting *ignored* — and has its own section,
§14.)

### 4.8 Output

`file:line`, the pointer **as written**, the candidates tried, and why it failed. Non-zero exit under
`--enforce`. **No auto-fixing** — a check that rewrites prose will one day rewrite the wrong prose, and
the fix is usually "this moved, update the sentence," needing a human who knows where.

```
seneschal/docs/spec-prompts/slack-draft-and-hold.prompt.md:13
    pointer: seneschal/CLAUDE.md
    tried:   seneschal/CLAUDE.md | seneschal/docs/spec-prompts/seneschal/CLAUDE.md | seneschal/seneschal/CLAUDE.md
    reason:  not tracked; not covered by any .gitignore rule; not allowlisted
    hint:    did you mean CLAUDE.md?  (tracked, 118,537 B)
```

`hint` is a basename match against tracked files, offered only when exactly one candidate exists —
suggestion only, never an edit.

### 4.9 The mode-file pointers — what a green check was not looking at

**Eleven dangling pointers lived in the mode bodies while this check reported a clean tree**, and both
reasons it could not see them were decisions recorded in this spec.

The bug itself is one move's fallout. The grounding restructure relocated the mode bodies out of
`seneschal/SKILL.md` into `seneschal/modes/`, one directory deeper, *verbatim* — which is the right way
to move prose and the wrong way to move a relative path. A ../subagents/NAME/SKILL.md pointer, correct
from `seneschal/`, now meant seneschal/subagents/, which has never existed. Six mode files, eight such
pointers, plus a one-level-short persona pointer in the Chat mode and an archon-stable pointer in the
Forge mode. `seneschal/SKILL.md` itself carried thirteen ../state/… pointers to a repo-root state
directory that does not exist, and two more turned up outside the modes: `seneschal/references/advisor-chain.md`
reaching the persona one level short, and `archons/CLAUDE.md` naming a `docs/` spec without its subtree
prefix. **These files are executed, not read** — a mode dispatches by an imperative read of the path it
names — so each was a mode that could not load its subagent.

**Why the check was silent, twice over:**

1. **Scope (§4.7).** `seneschal/modes/**` was not scanned. "Start narrow, widen on evidence" was the
   right instinct and it drew the line in the wrong place — around the *documents* rather than around
   the *instructions*. Several sub-routers were outside it too.
2. **The `external` decision (§4.4).** Even scanned, every one of these would have been skipped: any
   `..` segment meant "another repo". That rule was written for a sibling repository and it was quietly
   swallowing **413 tokens**, most of them this tree's own cross-directory pointers.

Either fix alone changes nothing. Scope without the narrowed `external` rule scans the files and skips
every pointer in them; the narrowed rule without scope examines pointers in files that were never the
problem.

**The lesson worth keeping:** the skip decisions each shipped with a test, because each was a judgment
someone expected to be argued with. The *scope* shipped as a tuple with a comment. **A check is the
intersection of what it knows and what it looks at, and only one of those halves was defended** — so
`ScanScopeTests` now pins the second half the way `RulingTests` pins the first.

One finding was **deliberately left open**: a Dream-mode pointer, written with a leading ../ into an
archon's state file, sitting beside a shell command whose working directory is `seneschal/`.
Document-relative it is wrong; cwd-relative it is right; nothing in the tree settles which reading
governs, and the Dream prompt in `SCHEDULING.md` mixed both conventions in one sentence. Allowlisted
with that reasoning rather than guessed at — fixing the prose would break the command, and the reverse.

---

## 5. The producer change — a prerequisite of check #1, not a follow-up

A byte budget over unchanged producers turns CI red every week, and the budget then gets raised every
week; §3.4's hatch is the lever that would get pulled. So the producer changes **first**.

**The change, in one line:** *detail lands in the leaf file beside the code it governs; the root gains
at most a pointer, and only if no pointer already reaches it.*

Per §2.3 all three producing instructions are host-side and unversioned, so **no PR in this repo can
make this change.** The edits, written out so they are copy-paste:

**(1) `~/.claude/CLAUDE.md`, "Documentation stays in sync with code"** — append:

> **Put the detail in the leaf, not the root.** When code changes, the doc that changes is the one
> **beside the code** — the module docstring, the `docs/` spec, the sub-directory's own `CLAUDE.md`.
> A repo-root `CLAUDE.md` is a **router**: it says what exists and where to look, not how it works.
> Add to the root only when **no existing pointer reaches the new thing**, and then add the pointer,
> not the explanation. A root section growing past a paragraph is the signal to split it into a leaf.

**(2) `~/.claude/skills/sync-claude-md/SKILL.md`** — the Step 2 row *"Repo structure / file tree → Add
the new entries"* is the specific instruction that produced §2.3's history. Replace with:

> | Repo structure / file tree | New files, directories, or routes were added | Add a **one-line pointer** to the file or directory. Put the explanation in the leaf doc beside it. If the root entry would exceed one line, write the leaf doc and link it. |

And add:

> ## Step 5: Report the size delta
> After editing, report each touched `CLAUDE.md`'s size before and after, and the net byte change. If
> the root grew, say what was added and why a leaf would not do.

That step makes *"did the root grow?"* a **reviewable outcome the producer reports** rather than the
silent side effect it had been for all 81 merges.

**(3) The assistant's Dream mode** (`seneschal/modes/dream.md`) needs **no change**: it writes to
`references/proposed-learnings.md` and never touches `CLAUDE.md`. Verified, not assumed. **This
corrects the premise this work started from**, which named Dream as a co-producer; the git history
says otherwise. Dream *is* the right home for §3.6's tightening proposals — a new job, not a changed
one.

**Because a PR cannot land (1) and (2), §6 gates phase 2 on them.** Phase 2's PR body must record that
the host-side edits are in place on the machine the work runs from; until then the report-only output
is all that runs. Not enforceable in CI — which is exactly why it is written here rather than assumed.

**On the host this design was developed on, both edits are in place**: the global instructions gained
edit (1) verbatim as *"Put the detail in the leaf, not the root"*, and the sync skill gained edit (2)'s
one-line-pointer row plus its own *"Step 5: Report the size delta."* That satisfies this section's half
of phase 2's gate for that host. **Every other install must apply them itself** — they are host-side by
nature, and nothing in this repo can check whether they are there. §6 records the other half.

---

## 6. Phases — the first changes no behaviour

**Phase 0 — both checks, report-only.** ✅ **BUILT.** Both modules written, both wired into CI, **both
`exit 0` unconditionally** at that stage. They print; they cannot fail a build. `context-budget.json` is
seeded from *measured* sizes (§2.1), not aspirational ones; `context-pointers.json` starts nearly empty
on purpose, so phase 0's output *is* the survey that fills it.

*Why report-only first — the load-bearing scheduling decision:* **a gate that is red on the day it
merges gets disabled, and then nothing is enforced at all.** §4.5 measured 2.6% precision for the
naive recogniser; §4.4's decisions are *predictions* about the real false-positive classes, and phase 0
is how those get checked against a tree nobody tuned them on. Seeding from measurement is the same
discipline: a budget of "80k because that feels right" is red on merge, and the first thing anyone
learns is that the escape hatch works. Same posture as the router's shadow phase and the Mouth's
phase 0. Exit criterion: one week of output, every false-positive class either decided on in §4.4 or
allowlisted with a reason.

**First-run results.**

*Check #1* — 21 artifacts, 339,684 B, all within budget, because the seeds are the measurement. **The
seed itself is the finding:** `CLAUDE.md` measured 118,537 B when the design was measured and **140,539 B
two days later — +22,002 B (+18.6%)**, from five merged PRs each of which correctly documented itself
in the root. Nobody was careless. That is §2.3's thesis arriving as data rather than as an argument,
and it is why §5's producer change is a prerequisite rather than a follow-up.

*Check #2* — 59 files, 9,233 tokens examined, 1,056 path-shaped candidates: **624 tracked (59.1%), 335
declared-runtime (31.7%), 4 allowlisted, 93 unresolved (8.8%)**. Against §4.5's prototype (64.1% /
27.8% / 8.1% over 951 candidates) the ladder behaves as measured, over a slightly wider scope. It finds
the known real bug — the never-existing subtree `CLAUDE.md`, in both spec-prompt files.

**Two things the first run taught, both now decided and tested:**

1. **`..` must be recognised as a segment ANYWHERE, not only as a prefix.** §4.4 decides on leading
   `../`; the first run hit `SCRIPT_DIR/../..`, a code-shaped token quoted in prose, which escapes the
   repo from the middle. `git check-ignore` exited 128 and aborted the batch — and the §4.7 oracle
   guard caught it *loudly*, which is exactly what it exists for. Had that guard degraded to "nothing
   is ignored", the run would have reported several hundred real runtime paths as dangling.
2. **Rung 0 must walk the document's ANCESTORS, not just its own directory.** §4.3 measured 18.1% of
   pointers resolving relative to the `seneschal/` subtree — a doc in `seneschal/docs/` writing
   `state/run-log.md` means `seneschal/state/run-log.md`. Without the ancestor walk the first run
   reported **399 findings, 233 of them that one class**. With it, 93. The spec called for declared
   per-document bases; the ancestor walk is the general rule that class was actually asking for, and
   `bases` stays for the genuine exceptions.

**What the remaining 93 mostly were, and it was encouraging:** nearly all were **forward references**
— sub-routers and scripts that sibling specs (`grounding-restructure-spec.md`,
`delegated-work-isolation-spec.md`, a journalling spec) **proposed to create**. §4.4 already decides that
class into the allowlist with a reason, and they were deliberately *not* pre-allowlisted: making each
one a reviewed line is the point, and doing it in bulk before anyone read them would be the rubber stamp
§3.4 warns about.

**Phase 1 — check #2 turns blocking. ✅ BUILT** (the standing findings resolved first — a gate red on
the day it lands is a gate someone disables). The safe one first: a dangling pointer is unambiguously a
bug, the fix is local, and blocking it makes phase 2's trimming safe (§4.1).

**Phase 2 — check #1 turns blocking. Designed, not built.** §5's host-side gate is cleared on the
development host. The design decision for this phase is: **enforce, and raise the target the same day
it goes red** — the report-only fallback this paragraph originally offered is closed. And *what a raise
sets `max_bytes` to* is decided and **built**: not the exact measured size (§3.4's SUPERSEDED note) but
measured size plus headroom — ~4 weeks of trailing growth, floor ~2k tokens — enforced on new raises by
`--enforce-headroom`. Budgets therefore do **not** stay at their measured seeds once a raise happens;
that convention is exactly what was found defective. What remains unbuilt is the cap itself blocking:
CI still does not pass `--enforce`. **The trim itself is still not in this spec** (§10), and turning
the cap on is ordered after a router diet, because the per-artifact headroom numbers should be measured
against post-trim trailing growth, not about-to-change rates.

**Phase 3 — the tightening proposal.** §3.6's Dream job, gated on phase 2 being green one week — a
downward pawl on a gate nobody trusts yet is just noise. **A second design for this same job is on the
table and unbuilt**: a script could author the *raise* itself when the headroom rule fires, closing
§11 Q5's "no author" objection for the raise half specifically. That is safe only because the raise
target is computable and principled (measured + trailing-growth headroom) — under the OLD
raise-to-exact-size convention an auto-raiser would have been the ratchet this whole correction exists
to end, automated. See §11 Q5 for the full proposal and its one hard caveat (an escalation rail, not
unconditional auto-raising).

---

## 7. The measured before/after contract

**The spec commits to reporting the trim's effect as a measurement, not an assertion.** Same
instrumentation as §2.2 — `state/metrics.jsonl`, already written in code — over a ≥7-day window before
and after.

**Headline: `first_turn_context_tokens`, median** — the median `context_tokens` of the first
successful turn of each warm session. At design time **75,462** (n=76). It is what the trim is supposed
to move, measured per-session, and not confounded by how long a conversation runs.

`turns served per cache write` was the candidate headline and is **kept as a secondary, not
promoted**: it measures *the owner's usage*, not the file, so a chattier fortnight improves it with
nothing trimmed. It stays reported (at design time mean 8.10, **median 1**) because it decides whether
caching startup context is worth it at all (§2.2: break-even at k ≥ 3); if it rises durably above 3 the
economics change and this spec's premise should be revisited. Reported alongside from the same rows:
mean `cache_read_input_tokens` (551,514), read amplification (4.18×), the billable split (56.3% read /
36.4% 1-hour write), and share of spend on first turns (31.8%).

**The honest caveat:** these are observational windows, not an A/B. Usage varies — one day in the
window saw 38 sessions at 3.47 turns each, the next saw 10 at 22.4 — and a fortnight either side does
not control for that. **If the before/after difference is smaller than that between-day variance, the
correct conclusion is "not measurable at this sample size"** — more useful than a number that flatters
the work.

---

## 8. Where the code lives

- `seneschal/scripts/check_context_budget.py` and `seneschal/scripts/check_context_pointers.py` — **one
  module per check**, so each fails, reads, and is tested independently. Their module docstrings are
  the operative design record; this spec is the argument.
- **Stdlib only.** `pyproject.toml` permits exactly two runtime dependencies by explicit design, and its
  header says to read `asyncio-daemon-design.md` before adding another; a lint does not clear that bar.
  All that's needed is stdlib: `re`, `json`, `ast`, `pathlib`, `subprocess` (git), `difflib` (§4.8's
  hint).
- Tests as `seneschal/scripts/test_check_context_*.py`, picked up by CI's existing
  `python -m unittest discover -s seneschal/scripts -p "test_*.py"` — no workflow change for the tests;
  wiring the checks in is one step in the existing `python` job of `.github/workflows/ci.yml`.
- Tests run against **fixture trees, not the live repo**: a check whose tests assert against today's
  `CLAUDE.md` goes red when someone edits `CLAUDE.md` — the fastest route to a disabled check. The
  `check-ignore` oracle is exercised against a real temporary git repo; §4.7's traps are invisible to
  a mock and were found only by running the real command.
- Load-bearing test classes: **`RulingTests`** — one case per §4.4 row, asserting the *decision*, so
  widening a skip rule must argue with a red test first (same posture as `jobs.py`'s classifier
  tests); **`ScanScopeTests`** — the scan scope (§4.7); and **`RatchetTests`** — a raise without a
  `raises[]` entry fails, one with passes, and a *lowering* always passes without ceremony. Later
  additions: `CrossPRBlindness` (§12), `CrlfPhantomPatternTests` and `CommittedIgnoreFilesAreLfTests`
  (§14), `ChainContinuityTests` / `ChainIsReportOnlyTests` (§15.6), and the headroom classes
  (`HeadroomConstantsTests`, `TrailingGrowthTests`, `HeadroomViolationTests`,
  `EnforceHeadroomWiringTests`).

---

## 9. Deliberately not in scope

Any trim of `CLAUDE.md` or `seneschal/SKILL.md` (§10) · tokenizer-based measurement (§3.2) ·
auto-fixing pointers (§4.8) · auto-tightening inside CI (§3.6) · a pattern-based allowlist (§4.6).
Each rejected with a reason above, so none is re-proposed later as an oversight.

---

## 10. What this spec does NOT answer — named, so it is not lost

1. **Check #3 — orphan / reachability.** *Which files are pointed at by nothing?* **This is the clause
   of the owner's ask this spec does not answer: "so the assistant is not lobotomized."** §3.7 shows
   check #1 is satisfiable by relocating text into a file nothing routes to, and check #2 verifies that
   pointers resolve — **not** that files are reached. **#3 is what stands between "thin" and
   "lobotomized,"** and without it a green board is compatible with knowledge having quietly become
   unreachable. Its own spec, and it should be next.
2. **Check #4 — duplication with declared exceptions.** The second clause, *"so that information is no
   longer duplicated."* The interesting part is not detection but the **declared exception**: some
   duplication is deliberate (`governor.py`'s tables mirrored in `cockpit/server/governor.py`, with
   `cockpit/server/test_parity.py` as the tripwire; the persona mirrored into `phone/src/persona.ts`). A
   duplication check without a first-class exception mechanism gets disabled in week one.
3. **The golden-question reachability suite.** The behavioural counterpart to #3: questions the
   assistant must still answer after a trim (*"where does the ledger live?"*). The only proposed check
   that measures whether the assistant still **knows** something rather than whether a file exists.
   Expensive, and the honest one. Not shipped in this tree (§13.3 records what one caught where it
   existed).
4. **The trim itself.** Trimming before #2 and #3 exist is doing the dangerous half first.

---

## 11. Open, and the owner's to decide

**Revisited in the same review as the header's** — two of the five got a decision, one was measured
rather than guessed, one is corrected as stale wording, and one carries forward with more context
attached but still unresolved. None was answered by assumption or quietly dropped.

1. **Should tier C (`seneschal/docs/`, `seneschal/references/`) really be unbudgeted? — DECIDED.** Tier C
   stays unbudgeted **as a default**, with an explicit **opt-in list** of tier-C files that ARE checked
   — not the all-or-nothing choice §3.1 originally posed. The motivating case: `proposed-learnings.md`
   should not be growing; it should shrink as proposed learnings graduate into actual ones, and the
   fact that it only grows suggests a missing piece of that process. **Not built here**: the opt-in
   mechanism itself (`seneschal/context-budget.json` carries no tier-C entry today), and the
   proposed-learnings lifecycle gap — that is its own design, `proposed-learnings-lifecycle-spec.md`,
   not solved by this decision.
2. **Is `raises[]` + review enough** (§3.4), or does a raise deserve a harder rail — e.g. capping a
   single raise at +10%, so growth must be argued repeatedly rather than once and largely? **DEFERRED,
   deliberately** — *undecided for the moment; measure more first.* Recorded as a deliberate deferral
   conditional on more measurement, not an oversight. (The headroom rule now computes the raise target,
   which narrows this question to whether the *computed* raise needs a cap.)
3. **The 4:1 token estimate — ANSWERED by measurement, not by decision.** The owner asked the assistant
   to spot-check it against a real tokenizer rather than guess. Measured differentially against the
   real model via the `claude` CLI's own reported usage (no public Claude tokenizer exists) — baseline
   prompt alone, then the same prompt with a file appended, delta = that file's tokens:

   | file | bytes | delta tokens | bytes/token |
   |---|---:|---:|---:|
   | `seneschal/references/CLAUDE.md` | 6,130 | 2,397 | **2.56** |
   | the persona file | 7,221 | 2,699 | **2.68** |
   | `seneschal/modes/chat.md` | 52,068 | 20,642 | **2.52** |

   **Mean ≈ 2.59 bytes/token, not 4:1 or 3.5:1.** Every `~N est. tok` figure printed at 4:1
   understated real tokens by a factor of ~1.55, and a headroom "floor of about 2k tokens" is **~5.2 KB,
   not ~8 KB** at the correct rate — any headroom arithmetic converting at 4:1 sets every floor ~55% too
   generous. **Caveats, kept rather than stripped:** three files is a spot-check, not a study; all three
   are this repo's own dense markdown (which is the population the budget covers, so the sample is the
   right one, but it does not generalise to prose in general); and the differential method assumes the
   CLI's own system-prompt overhead is stable between paired calls — the three results agreeing within
   6% is the evidence that it is. **Applied in code:** `check_context_budget.py` now carries
   `BYTES_PER_TOKEN = 2.59`, and `HEADROOM_FLOOR_BYTES` is derived from it.
4. **Phase 1's blocking scope — the question as originally worded is STALE.** It asked whether "the
   52-file gate" is worth having on its own. That scope no longer exists: §4.7 records it widening once
   (adding `seneschal/modes/**` and the sub-routers — executed instruction files, the worst place for
   this bug to hide) and again prospectively (`.claude/rules/**/*.md`), and the scope is now pinned by
   `ScanScopeTests` rather than left to drift. Nothing further was identified as open — the widenings
   each shipped with their own justification and test, and no further widening or narrowing was
   proposed. Left here rather than deleted, so a future reader does not re-litigate wording that already
   aged out.
5. **Should phase 2 block on an AFTER-MERGE finding, and/or should `develop` require up-to-date
   branches? — carried forward, not answered, with more context attached.** Stated in full at the end
   of **§12.5**. On the repository this was designed on, `develop`'s branch protection had
   **required-up-to-date branches OFF** (`required_status_checks.strict = false`), with no required
   reviews and admins not enforced — check your own install's settings rather than assuming. **The
   distinction the question turns on: CONFLICT is not the same as BEHIND.** A branch deconflicts only on
   a textual collision; a branch merely *behind*, with no collision, merges silently, and its green CI
   describes a tree that never existed. §12.6 records a fresh, reproducible instance of exactly this.
   **A proposal for the "no author" objection to blocking:** make a script the author — the thing that
   fixes the issue when it happens. Carried forward as a design input with one hard caveat: an
   after-merge violation has two possible fixes, raise (mechanical) or trim (a judgment) — a script can
   only author the raise, and under the OLD raise-to-exact-size convention an auto-raiser would BE the
   ratchet this document exists to end, industrialised. Under the headroom rule it becomes safe,
   because the raise is computable and principled. It also needs an escalation rail: if one artifact
   is auto-raised more than N times in a window, stop raising and surface it to the owner — the "or much
   sooner if growth goes wild" half of that design. **Not built here.** Neither half of the original
   question is decided; this is context for whoever decides it.

---

## 12. The cross-PR hole — two green PRs, one over-budget tree

**The check could not see a violation that both of the PRs producing it were individually clean of —
and it shipped one within the hour, on the very sweep this document's header records as the first
time the gate was worked.**

### 12.1 What happened, in order

| When | What |
|---|---|
| T+0:00 | **PR A** (the budget sweep) cut from `develop`. It measured the scripts sub-router at **37,384 B** against its 37,642 B budget — under — and it set four other budgets from the same tree. |
| T+0:33 | **PR B** (a journalling decision-log feature) merged, adding to **five** shared artifacts: the Chat mode body +3,630, the scripts sub-router +3,047, `seneschal/SKILL.md` +774, the Dream mode body +694, `seneschal/docs/CLAUDE.md` +478. Green. |
| T+0:52 | **PR A** merged. Still green — still measuring its own merge base, a tree that had stopped existing nineteen minutes earlier. |
| T+1:48 | `develop` is over on **all five**: Chat +3,633, scripts sub-router +2,789, `seneschal/SKILL.md` +735, Dream +689, `seneschal/docs/CLAUDE.md` +476. |
| ~T+4:20 | **It happened again, to the branch fixing it.** Five more PRs merged while this work was in progress, and `develop`'s overage grew rather than shrank: scripts sub-router +4,275 and Dream +1,254 against the same budgets. Each of those PRs was green. |

**Neither PR was ever red. CI passed on both. There is no author.** Each grew a shared artifact inside
its own headroom; the sum is the violation, and a sum has no commit. The last row is the same mechanism
a third time in one day, on a branch whose entire subject was the mechanism — which is the strongest
available argument that it is structural and not a lapse.

### 12.2 Why the design could not see it — and it is not the merge-base rule's fault

§3.4 compares a branch's config against the **merge base**, deliberately, so an unrelated merge cannot
fail your branch. That rule is right and is not what broke. What broke is narrower and worse: **the
check only ever asked one question — "did *this branch* grow it?" — and answered it against the tree
the branch was cut from.** For attributing growth that is correct. For "is `develop` about to be over"
it is a measurement of a tree that no longer exists by the time both PRs have landed.

The general form: **N PRs each consuming a fraction of one artifact's headroom are each individually
green and collectively over.** Nothing in §3 or §6 noticed, and the report-only posture meant the first
run that *did* notice — the `push: develop` run after the second merge — printed it into a green job's
log that nobody reads. Two failures stacked: **invisible in advance, and unread in arrears.**

### 12.3 The three candidate fixes, and why the choice went the way it did

**(a) Evaluate the prospective merged tree — CHOSEN.** Measure the artifact as it would be after
merging HEAD into the **current** `origin/develop`, beside the branch measurement, and report the two
separately. `git merge-tree --write-tree` (git ≥ 2.38) computes that tree without touching the working
tree, the index or any ref.

*Why it wins:* it is the only one of the three that answers the question **while the author can still
act**, and it works in both places the number is ever taken — CI, and the local sweep. That second half
matters more than it looks: **PR A's wrong number was measured locally**, and the budgets it wrote came
from that measurement. A fix that only lives in CI would have left the sweep itself blind. Budgets for
the pass come from the **merged** config, not HEAD's, because the question is what `develop` will say
once this lands and the other side may have tightened one — which is precisely what PR A did.

*What it costs, stated:* it needs `origin/develop` in the checkout (a workflow change, §12.4), it
writes inert tree/blob objects into the object store, and it degrades to **no answer** — never a guess
— on an absent ref, a conflicted merge, or git < 2.38.

**(b) A post-merge check on `develop`, separate from the per-PR delta.** *"Did this PR grow it"* and
*"is the tree over"* are different questions and now print as two blocks. But this half **already
existed** and still missed it: the workflow runs on `push: [develop]`, the working tree there *is*
`develop`, and the absolute figures were already correct in the post-merge run's log. It is kept, and
made legible — when HEAD already contains the base, the after-merge block is suppressed and the report
says the figures above ARE the tree state — but on its own it is a post-mortem, not a fix. **Its real
defect is visibility, not measurement**, and that is a property of the cap being report-only rather
than something this change can repair.

**(c) Re-measure at merge time / on a merge queue.** The only option that actually *prevents* the
state rather than reporting it, and therefore **the only one that is a merge policy**: GitHub's
"require branches to be up to date before merging", or a merge queue, would have forced PR A to re-run
against post-B `develop` and gone red. That is a blocking gate. §6 says phase 0 does not block, and
§3.4's whole thesis is that a gate red on the day it merges gets disabled. **It is not shipped, and it
is not this module's call** — see §12.5, question 5.

### 12.4 What CI needed, and it is honestly a workflow change

On a `pull_request` event `actions/checkout` already checks out `refs/pull/N/merge` — GitHub's own
merge of the PR into its base — so CI had *always* been measuring a merged tree. What it lacked was a
**fresh** base: the default shallow fetch creates no `origin/develop` remote-tracking ref, so neither
the merge-base ratchet nor the new pass had anything to compare against. **`fetch-depth: 0` on the
`python` job** fixes both, and the second thing it fixes was itself invisible: the raise ratchet of §3.4
had been silently taking its bootstrap path in CI on every run, i.e. an unratcheted raise **could not
be caught by CI at all** and only ever failed locally.

**The gap this does not close, said plainly.** The verdict is only as fresh as the run. GitHub rebuilds
`refs/pull/N/merge` when the base moves but does **not** re-run the workflow, so a PR whose base moved
after its last green run still reports the older tree — which is exactly PR A's situation. Closing that
requires (c), a merge policy. **A report-only check cannot make a stale-but-green run re-run itself,
and pretending otherwise would be the weaker thing built and called done.**

### 12.5 What shipped

`check_context_budget.py` gains `merged_tree()`, `measure_in_tree()` and `after_merge()`, and `check()`
returns `merged_tree` / `merged_is_head` / `merged_over`. Findings carry `only_after_merge` — **the field
that separates "this PR is too big" (has an author) from "these two PRs are too big together" (does
not)**. They are **not** added to `violations`, so `--enforce` cannot fail on one.

Guarded by **`CrossPRBlindness`** in `test_check_context_budget.py` (9 cases), which reproduces the
A/B sequence with fixtures: a shared file with two sections, one PR growing each, both within headroom,
merging clean. **Seven of the nine failed against the pre-fix module.** One of the two that passed is
`test_the_branch_alone_is_within_budget_which_is_the_whole_problem`, which pins the blindness rather
than the fix — it must keep passing, because attributing growth to a branch is still correct. The other
pins report-only-ness, which is the invariant most likely to be lost later.

Also swept in the same PR: the five artifacts §12.1 lists, measured against `develop` after the last
row of that table, not the one before it.

| Artifact | Budget | `develop` | After |
|---|---:|---:|---:|
| `seneschal/modes/chat.md` | 23,837 → **27,470** (raise) | 27,470 | 27,470 |
| the scripts sub-router | 37,642 | 41,917 | **37,605** |
| `seneschal/SKILL.md` | 23,588 | 24,323 | **23,582** |
| `seneschal/modes/dream.md` | 22,238 | 23,492 | **22,236** |
| `seneschal/docs/CLAUDE.md` | 6,624 | 6,642 | **6,162** |

Four came back under by relocation, every removed byte landing in a leaf the file still points at and
every destination checked to already carry the fact in fuller form first. The Chat mode took a
**reasoned raise to 27,470 B**, because its whole overage was a single new rule — a no-silent-rewrite
promise about the owner's journal — and trimming a three-day-old commitment of that kind to satisfy a
byte counter is the wrong trade. The way that one comes back down is retiring the rule, not editing it:
once the call site moves into code, the rule's CLI table has a home that is not a prompt.

**Open, and the owner's to decide (5, continuing §11's list):** should phase 2 block on an after-merge
finding, or only on a branch one? Blocking it is the only thing that would have stopped §12.1, and it
is also a gate whose red has no author — the failure mode §3.4 warns about, in its purest form. The
adjacent lever is (c): turning on **required-up-to-date branches** or a merge queue on `develop`, which
moves the enforcement out of this module and into the merge policy, where a stale green cannot be
merged in the first place. **Neither is shipped.**

### 12.6 A fresh, reproducible instance

**Demonstrated from the merge graph, not reconstructed from attestations.** A later PR merged eight
minutes after another, with a head that did **not** contain the earlier merge — one merge behind,
textually clean, both green. The two PRs overlapped on **five files**: `seneschal/context-budget.json`,
`seneschal/docs/CLAUDE.md`, `seneschal/docs/rulings.md`, the scripts sub-router, and
`seneschal/state/README.md`. **The combination that landed on `develop` was never run by CI** — the
exact mechanism this section names, caught this time by the merge graph itself rather than by a human
noticing a number.

This is better evidence than §12.1 for one reason: **it is reproducible from the merge graph**, where
earlier collisions could only be attested via reason strings because a hand-resolved rebase erases its
own evidence (§15.6 clause 3). Two green PRs one merge apart, with no conflict and no author for the
combination, is the plainest possible statement of §11 Q5's "CONFLICT is not the same as BEHIND"
distinction — feeding directly into that question rather than opening a new one.
`concurrent-pr-collisions-spec.md` works the general version of this failure class.

---

## 13. The two hard routers — the split that did not happen, and the line that replaced it

**A budget sweep brought seven artifacts under budget and raised these two rather than solving them**,
saying so honestly at the time. This section records what was decided when they were worked properly,
because a decision argued only in a PR body is a decision that gets re-argued.

### 13.1 The measurement that framed both

| Artifact | At the sweep | Measured here | Budget | Composition |
|---|---:|---:|---:|---|
| `seneschal/modes/chat.md` | +4,540 | **+4,964** | 35,508 | 13 numbered rules = **95%** of the file; the journalling rule alone = 8,150 B (20%) |
| the scripts sub-router | +10,724 | **+21,341** | 55,041 | 58 parenthesised entries = **89%** of the file |

**The scripts sub-router had doubled its overage in a week — +10,617 B across 17 merges, none of which
took a `raises[]` entry, none of which was individually wrong.** That is §12's cross-PR mechanism
arriving on the largest artifact in the tree, and it is new evidence for §12.5's open question 5 rather
than a new question.

### 13.2 Decision 1 — does the journalling rule leave the Chat mode? **No. And nothing else leaves as a rule either.**

**The case for moving it** (the one the sweep named): the journalling rule is the single largest block
in the file, the journalling work's next phase is a `subagents/journal-steward/` change anyway, and
someone who knows the feature could land it there now.

**The counter-case, which won: the rule is a RECOGNISER, and a recogniser cannot live in a file you
only open once you have recognised the thing.** Every rule that has ever successfully left this file
left because something *dispatched* to it — Chat delegates to a subagent and the subagent's SKILL.md
loads. **There is no dispatch for "the owner just said something that will be hard to reconstruct
later."** The always-on tier fires on turns that give no signal journalling is in play; that is the
whole point of the bar. Move the bar into the steward's own skill and the only turns that consult it
are the turns that already decided — *invented once, applied half the time*, with an extra file in it.
The same argument the §12.5 raise made, and it generalises.

**Why that argument does not prove too much** — the objection that could not be dismissed, and the line
that answers it. "A rule must be present to be obeyed" would justify never trimming anything, and it is
exactly how the file reached 40 kB. So the trim ran on a narrower line, applied to **every** rule
including the journalling one:

> **The rule stays; the evidence goes one hop out.** The invariant, the refusal, the imperative, the
> thing an edit could silently undo — those stay. The incident, the date, the measured number, the
> alternative that was rejected — those go to the module docstring or the spec named beside them.
> **You look up evidence when you want to argue with a rule, and at that moment you will go and look;
> you obey a rule you were never told to go and find.**

Applied to the Chat mode that was **−3,555 B (40,472 → 36,917) with no rule, refusal or trigger
removed** — more than the journalling rule's whole share of the overage, found without touching it. The
journalling rule lost ~150 B of narrative on the same terms as everything else.

**What stayed inline against its own trim rule, deliberately:** the journalling rule's decision-log CLI
table. It is mechanics, and mechanics went out everywhere else. It stays because **its caller is the
same turn that must not stop to look something up** — the report-after and the record are conditions of
the owner's grant, not reference material. That is the exception, it is argued rather than assumed, and
§13.4 says what would settle it.

**And a structural note that kills the obvious "just point at the scripts sub-router" move:** a Chat
turn never touches `seneschal/scripts/`, so it never loads that file. The two routers are not duplicates
of each other; they have different audiences and neither can be a pointer to the other.

### 13.3 Decision 2 — does the scripts sub-router split into sub-routers? **No.**

**The case for splitting** (the one the sweep named): 66 kB describing ~60 scripts is read by the gate
as one artifact, and a turn editing an unrelated one-off tool pays for the reminders gate order, the
outbox supersession rule and every other script's refusals. That is the tier-A/B thesis one level down,
and it is a real cost.

**Three counter-arguments, in the order they killed it:**

1. **A sub-router here would not auto-load, because there is nothing to touch.** A nested `CLAUDE.md`
   loads when a turn touches *its directory*. All ~60 scripts are flat in `seneschal/scripts/`, so a
   would-be per-subject sub-router (a reminders one, say) would contain no code and be reached only by
   an explicit pointer — a reference doc wearing a router's name. Moving the *code* into subdirectories
   would fix that and is a different, much larger change (imports, absolute paths in scheduled tasks,
   the daemon's own spawn arguments). **That, not the byte count, is what would change this answer** —
   and `scripts-subdirectories-spec.md` has since worked that question to a close (§5 there): no.
2. **Named anything else, it is §3.7(a) exactly.** A set of per-subject invariants files beside the
   scripts is unbudgeted tier C: the per-file number falls, the routed-tree total does not move, and
   the check reports the move it cannot block. §3.7 names this as the path of least resistance under
   deadline. Declining to take it is the whole point of having written §3.7 down.
3. **No axis survives contact with the coupling.** *reminders / jobs / health / comms* was the proposed
   cut. `ack.py` is reminders + outbox + the store; `activity_day.py` is reminders + health; and
   **`telegram_send.py` carries the ack gate**, whose own recorded bug was *a second sender the reminder
   queue's fire-time gate never covered.* Filing that entry under "comms" hides it from the reader
   working on reminders — **the split would reproduce the exact blind spot the entry exists to record.**

**So the same line as §13.2 was applied instead**, with two hard rules: **every script keeps its line in
the parent** (nothing is ever further away than the pointer beside its name — the reversibility and
no-orphan requirement is satisfied by not moving the index at all), and **a destination is verified to
carry the fact in FULLER form before a byte leaves.** That verification is not ceremonial: measuring
entry-vs-docstring found `sentinel.py` (0.5×), `rag_common.py` (0.4×) and `archive_aggregate.py` (0.7×)
had docstrings *shorter* than their entries, so **those three were left long** — there was nowhere for
the evidence to go. Result: **−9,665 B (76,382 → 66,717)**.

**And a golden-question test caught one over-cut, which is the system working exactly as §10.3
hoped.** It pinned the answer to *"where is the billable-token weight basis defined, and what are the
cache ratios?"* **to the scripts sub-router**; the `governor.py` entry's trim dropped
`read 0.1x, 5m write 1.25x, 1h write 2x` on the grounds that `governor.py` carries it — and the run went
red. Restored verbatim, the test untouched. **The lesson is a correction to the trim rule itself: a
destination carrying a fact in fuller form is necessary, not sufficient.** Some facts are pinned to a
*router* because the router is where someone will ask, and only the golden questions know which. §10.3
calls that suite "the only proposed check that measures whether the assistant still **knows**
something"; it is the check that made this trim safe, and the byte gate could not have. (This tree
does not ship that suite; §10.3 stays open here.)

### 13.4 What was NOT solved, and what would settle it

- **Both files still took a raise** — Chat +1,409, the scripts sub-router +11,619 over the old numbers.
  **The trim was driven by the line, not by the target**, and it stopped where the line stopped. Cutting
  the remainder means cutting rules to hit a figure, which is §3.7(b) — the failure the byte gate
  cannot distinguish from a legitimate deletion. A raise that says "this is what the file is for" is a
  better artifact than a green check bought with a deleted refusal.
- **The journalling decision-log table is the one argued exception**, and it has a falsifiable handle:
  the decision log's own stats after a week. If `written` rows are landing at the measured ~twice-a-day
  bar, the inline table is doing its job. **If the journal received entries while that log stayed
  empty, the table is not the thing keeping it inline** and it should move to the leaf with the rest of
  the mechanics.
- **The owner's call, not shipped and not assumed:** whether `seneschal/scripts/` should grow real
  subdirectories. It is the only thing that turns a sub-router from a reference doc into something that
  loads when it is relevant — i.e. the only version of decision 2 that is worth doing. Since closed by
  `scripts-subdirectories-spec.md`: the directory stays flat.

**A later router diet was checked against this section and had not absorbed it** — unbuilt, and not
citing it. Its own description (house rules ≤3 KB de-narrated, then one row per module, with 4-13 KB
entries moving verbatim into the module's own docstring) independently arrives at the same "rule stays,
evidence goes one hop out" shape decisions 1 and 2 above already settled for these same two files.
Whoever builds that diet should read §13.2's decided-not-to-move-the-journalling-rule and §13.3's
decided-not-to-split decisions first, rather than re-litigating an axis already measured twice and
closed (`scripts-subdirectories-spec.md` §5–§6).

---

## 14. Trap 3 — the CRLF `.gitignore`, and a green check that was looking at nothing

**BUILT** — `ignored_from_verbose()` and `crlf_ignore_files()` in `check_context_pointers.py`, guarded
by `CrlfPhantomPatternTests` and `CommittedIgnoreFilesAreLfTests`. The subdirectories work first
recorded a finding rather than a fix: on one checkout `git check-ignore -v` reported **any**
trailing-slash path as ignored, matched against a blank line, so every bare-directory pointer passed
rung 2 locally. That was correct as an observation and wrong about the cause. This section is the
diagnosis, the fix, and the guard.

### 14.1 The diagnosis, in the order the commands were run

**(a) The CR is NOT in the committed blob. It is introduced at checkout.**

```
$ git cat-file blob HEAD:.gitignore | …    bytes=3346  CR=0   LF=81
$ …the working-tree file…                  bytes=3427  CR=81  LF=81
DIFFERENT -> checkout transformed it; first differing byte: 9
$ git config --list --show-origin | grep autocrlf
file:<Git for Windows install>/etc/gitconfig    core.autocrlf=true
```

The blob is **pure LF**. So *"normalise the file"* — the obvious first fix — is a **no-op here**: there
is nothing in the object to normalise. The CR is added by `core.autocrlf=true`, set **machine-wide** in
the Git-for-Windows system config, i.e. by the installer's default rather than by anything the repo
did. All 12 tracked ignore/attributes files were clean in the object store.

**(b) The mechanism is the BLANK LINE, not CRLF in general — and the phantom pattern is EMPTY.** Three
throwaway repos, one variable each:

```
(a) LF file, has a blank line       invented-directory/   rc=1  <no match>
(b) CRLF file, has a blank line     invented-directory/   rc=0  .gitignore:2:	invented-directory/
                                    invented-directory    rc=1  <no match>
                                    totally/made/up/      rc=0  .gitignore:2:	totally/made/up/
(c) CRLF file, NO blank line        invented-directory/   rc=1  <no match>
```

(b) versus (c) is the isolation: a CRLF `.gitignore` with no blank line behaves perfectly. Reading the
`-v -z` output as bytes shows what the pattern field actually holds:

```
$ printf 'invented-directory/\0' | git check-ignore -v --stdin -z | od -c
   .   g   i   t   i   g   n   o   r   e  \0   2  \0  \0   i   n   v   e   n   t   e   d …
                                               ^^^^^^^ the pattern field is EMPTY
$ …the control, a real rule…
   .   g   i   t   i   g   n   o   r   e  \0   1  \0   s   t   a   t   e   /  \0   s   t …
```

So git **strips the trailing `\r` back off each line when it parses the file**, and a blank line
therefore becomes an *empty pattern* rather than a *skipped line*. The empty pattern matches any path
spelled with a trailing slash — and only those; without the slash there is no match. Observed on git
2.45 for Windows.

**(c) It is TRACKED-NESS, not existence — which is the worst possible shape.**

```
tracked_dir/            rc=1  <NO MATCH>     (contains a tracked file)
untracked_dir/          rc=0  matched blank line
ghost_dir/              rc=0  matched blank line
tracked_dir/ghost_sub/  rc=0  matched blank line
```

A directory holding tracked content is immune; an untracked or nonexistent one is not. **The false pass
lands exactly on the class this check exists to catch.** In the live checkout, on a blank line of the
root `.gitignore`:

```
totally-invented-dir/          rc=0  .gitignore:70:	totally-invented-dir/
seneschal/nonexistent-thing/   rc=0  .gitignore:70:	seneschal/nonexistent-thing/
cockpit/breakglass/            rc=1  <no match>
```

**(d) The consumers.** A repo-wide grep finds exactly one: `check_context_pointers.py`'s rung-2 oracle
(`check_ignored`). Nothing else shells out to `check-ignore`. A false *ignored* verdict there means a
pointer resolves as declared-runtime and is never reported — a silent over-report of health, which is
why it survived: **an over-reporting check produces no symptom to investigate.** It is the same
resolved-at-home / dangled-in-CI class as §4.7's third trap, now with a mechanism attached.

### 14.2 The fix, and the counter-case that lost

**Both halves were designed to ship, because they fix different populations and neither covers the
other.**

| Option | Fixes | Leaves broken |
|---|---|---|
| Normalise the blob | **nothing — the blob is already LF** (14.1a) | everything |
| `.gitattributes` pinning `.gitignore` to LF | the cause, for **checkouts made after it lands**, for **every** reader of the file | every working tree already on disk, until re-checkout |
| Harden the consumer | **every checkout including existing ones**, immediately | the trap stays live for anything else that ever reads `.gitignore` |

The consumer fix is the load-bearing one and would have been the choice if only one were allowed: the
population that matters is *checkouts that exist right now* — the daemon's, a development clone, and
every delegated worktree — and `.gitattributes` reaches none of them until something re-checks-out the
file. But `.gitattributes` is not decoration: it is the only half that protects a reader that **isn't**
our checker, and the cost is two lines. **The counter-case against shipping only `.gitattributes` is
decisive and is why it lost as a sole fix** — a delegated job cut from `origin/develop` before the pin
would inherit the trap for its whole life, and delegated jobs are precisely where this bug did its
damage.

**The diagnosis alone justified exactly two attribute entries, not a repo-wide `* text=auto`**, which
would rewrite files this fix has no business touching. **This tree carries a repo-wide
`* text=auto eol=lf` anyway**, for an unrelated reason recorded in `.gitattributes` itself (CRLF working
copies break `gradlew` on CI). It subsumes the pin: a fresh seneschal checkout gets an LF `.gitignore`
even on an `autocrlf` host. The consumer fix still ships, because a checkout made before the pin, or a
host overriding attributes, is exactly the population the table above says `.gitattributes` does not
reach.

**The consumer fix is `-v` plus two filters, and the second filter is a trap inside the fix.** Reading
the matching *pattern* rather than the *verdict* is the whole idea — an empty pattern is never a real
declaration, on any host. But `-v` **widens the result set**:

```
.gitignore = '*.log' then '!keep.log'
  plain : drop.log
  -v    : .gitignore|1|*.log|drop.log|  .gitignore|2|!keep.log|keep.log|
$ git check-ignore    keep.log ; echo $?   ->  1   (not ignored)
$ git check-ignore -v keep.log ; echo $?   ->  0   (a record, and a zero!)
```

A parser reading "`-v` emitted a record" as "ignored" would have swapped the phantom for a fresh false
positive — and this repo's `.gitignore` carries several negations (the `!`-prefixed re-includes of
`seneschal/state/README.md` and the `*.example.*` templates), so it would have fired at once.
`ignored_from_verbose` therefore drops **empty** and **`!`-prefixed** patterns, which reconstructs
plain-mode semantics exactly, minus the phantom. The exit code is now used **only** to detect an
aborted batch, never as the verdict.

### 14.3 What it changed, measured

Scoring the real scan's own candidate batch both ways, on the tree the fix was built on:

```
candidates fed to the oracle : 1475
resolved rung-2, OLD (plain) : 203
resolved rung-2, NEW (-v)    : 184
supplied by the PHANTOM      :  19
```

**And the check was still green — `--enforce` reported no dangling pointers, before and after.** That
is the honest result and it should not be dressed up: all 19 were *wrong-base* variants — the base
ladder tries several prefixes per token, and the phantom was vouching for the prefixes that happen to
be wrong:

```
seneschal/docs/state/transcripts/     subagents/archons/scaffolds/     seneschal/out/<day>/
```

Each of those tokens *also* resolved correctly through another rung of §4.3's ladder, so nothing
depended on the phantom. **It was masking nothing on that tree.** What it was doing was standing ready
to resolve the next genuinely-dangling bare-directory pointer — which is exactly what it had done to
the subdirectories spec, twice in one session.

**A consequence worth naming: the local run is authoritative again.** The subdirectories work had told
a reader to distrust a green Windows `--enforce` and treat CI as the arbiter. On an LF checkout the
phantom never existed, so `-v` + the filters is a **no-op on CI** and a correction on Windows; the two
now agree by construction. That instruction is superseded.

### 14.4 The guard

`test_check_context_pointers.py`, fixtures not the live tree, two classes:

- **`CrlfPhantomPatternTests`** — a fixture repo whose `.gitignore` is written as **raw bytes** (a text
  handle would translate the very terminators under test) with `core.autocrlf=false` pinned, so the trap
  reproduces on Linux CI where the live file is LF and the bug is otherwise invisible. It asserts the
  regression (a ghost directory must not resolve), the LF control, the **negation** case that the naive
  fix would have broken, the parser as a unit, and — the one that keeps the guard honest — that the raw
  oracle **is still fooled**, so the filter cannot quietly become dead code. That last one *skips*
  rather than fails if a future git stops registering the empty pattern, because the trap being fixed
  upstream is good news and must not read as a regression.
- **`CommittedIgnoreFilesAreLfTests`** — `crlf_ignore_files()` over the **committed blob**, not the
  working tree. The worktree is legitimately CRLF on any `autocrlf` host without an attributes pin, and
  that is fine; what must never happen is a CR reaching the shared object, because then every clone
  inherits the phantom and no local setting can undo it. Unit-tested against fixtures, plus one
  live-repo assertion — a narrow, deliberate exception to §8's fixtures-only rule, since §8 exists so a
  check does not go red when someone edits `CLAUDE.md`, and this asserts an invariant whose entire
  purpose is to go red.

---

## 15. `develop`'s own drift is recorded separately — the convention, and why

**This is the rule for what a `raises[]` entry may claim, and it governs every future raise.** §3.4 says
a raise needs a non-empty `reason`; this section says what makes one *true*.

### 15.1 The decision

Asked as a tappable question with three options, the owner chose **"fix develop's drift first"**: a
separate small PR records `develop`'s existing context-budget drift honestly, then the in-flight PR
rebases onto it cleanly and records only its own bytes. **Nobody's entry ever lies.**

**The convention that follows, and it is the operative half:** when a branch finds an artifact already
over budget on `develop`, that overage is **recorded in its own PR, attributed to whoever actually
produced it** — never folded into the `reason` of whatever change happens to be in flight. A raise
entry describes *its own* bytes; inherited bytes get their own entry and their own name.

### 15.2 What forced the question

A docs-only measurement PR had sat for two days. Four PRs merged to `develop` in the meantime. On
rebasing it, its `raises[]` entries were found to assert a *measured base* — *"develop measured X
exactly against a 14,385 B budget, so the file had zero headroom and there is no inherited drift to
separate out"* — which the new base made **false**.

Neither obvious resolution works, and that is the whole point:

- **Keep the PR's raise** ⇒ the files stay over budget; the entry is honest but useless.
- **Raise to cover current HEAD** ⇒ the entry silently absorbs *other PRs'* bytes, contradicting the
  very sentence quoted above.

The escape hatch of §3.4 has exactly one defence — *the defence is review, and the reviewer is the
owner* — and a reason clause that misattributes several kilobytes to the wrong change is that defence
being fed bad evidence. Hence a separate PR.

### 15.3 What was measured

Seven artifacts over budget, **10,793 B in total** — not the two that the aborted rebase reported from a
conflicted tree. LF-normalised per §3.2 and cross-checked against `git cat-file -s`, because that host
checked out CRLF and a naive `len(read())` overstates every file by its line count.

| Artifact | Budget | Actual | Over | Produced by |
|---|---|---|---|---|
| the scripts sub-router | 68,348 | 72,976 | +4,628 | **PR W +1,612, PR X +3,016** |
| `subagents/calendar-steward/SKILL.md` | 3,979 | 6,306 | +2,327 | PR X |
| `seneschal/SKILL.md` | 25,187 | 26,443 | +1,256 | PR X |
| `subagents/morning-briefing/SKILL.md` | 6,948 | 8,189 | +1,241 | PR X |
| `subagents/eod-wrap/SKILL.md` | 4,761 | 5,421 | +660 | PR X |
| `seneschal/docs/CLAUDE.md` | 14,805 | 15,185 | +380 | **PR U + PR V sum +75, then PR W +305** |
| `seneschal/modes/brief.md` | 3,367 | 3,668 | +301 | PR X |

**THE ATTRIBUTION IS PER-FILE AND FIVE OF THE SEVEN HAVE A SINGLE NAMED AUTHOR.** The framing the
drift PR was commissioned under — *"accumulated across multiple PRs, not attributable to any single
change"* — was true of exactly two artifacts and **false of the other five**, all of which were one
commit (PR X, a calendar-bridge fix) alone. Writing the blanket sentence on those five would have been
the same defect the decision exists to prevent, one file over. **A convention about honest attribution
that is applied dishonestly on first use is worth less than no convention**, so each entry names its
own producer and the two shapes are kept apart.

### 15.4 `seneschal/docs/CLAUDE.md` is §12 happening again, on the file that documents §12

Worth its own paragraph, because it is not ordinary drift and a future reader will otherwise file it as
such. PR U and PR V **both branched from a base with this file at 14,385 B**:

- PR U added **+75** → 14,460
- PR V added **+420** → 14,805, **and raised the budget to exactly 14,805** — correct, measured, and
  green against its own merge base
- Merged: 14,385 + 75 + 420 = **14,880 B against a 14,805 budget. Over by 75 B, with no author.**

Then PR W added +305, reaching 15,185. **The first 75 bytes are precisely §12.1's failure — two PRs
each within their own headroom summing to a violation neither side can observe** — and §12.5's
after-merge block *would* have caught it, except that it is report-only and **not counted in
`violations`** by explicit design, so nothing was ever going to stop it. That is not a bug in the
block; it is the open question §12.5 hands to the owner, arriving as data for the second time.

### 15.5 The observation this raises, and it is NOT decided here

`check_context_budget.py`'s cap runs **report-only** in `.github/workflows/ci.yml` — only
`check_context_pointers.py` gets `--enforce` (the budget module's `--enforce-headroom` checks the
*shape* of a new raise, not the cap). **That is why 10,793 B accumulated across six merges without one
red build**, and it is why a wrong number in the measurement PR would have merged green: nothing reads
these entries at merge time except a reviewer.

**Making check #1 blocking is phase 2, it is gated on §5's producer change, and it is the owner's call —
the drift PR did not touch the workflow.** Recorded here so the next person to ask *"how did seven
artifacts drift?"* finds the mechanism rather than re-deriving it. It also strengthens §15.1's
convention rather than substituting for it: while the gate reports, **the reason clause is the only
enforcement there is**, which is exactly why it may not lie.

### 15.6 The rechain rule — what to do when your `from` no longer chains

§15.1 says what a raise entry may *claim*. This says what to do when `develop` moves underneath one
before it lands, which on a busy repo is the ordinary case rather than the exception: **roughly a third
of all merges touched this ledger** on the repository this was designed on, so two PRs that both raise a
budget conflict **by construction**. Five hand resolutions in one day, five more on another, and **the
anchor was stale on every one of them** — `seneschal/docs/CLAUDE.md`'s budget walked 85,218 → 94,253 →
96,632 → 99,992 while one branch waited.

**The rule, in three clauses. It is the same sentence as §15.1, applied to a moving base.**

1. **Re-base the `from`.** It becomes `develop`'s *current* `max_bytes` for that artifact. The old one
   was measured against a tree that no longer exists; carrying it forward double-counts every
   intervening raise and **silently drops those PRs out of the ratchet** — their bytes end up inside
   your entry's span with your name on them.
2. **Preserve the delta.** `to - from` does not change. The entry still describes **its own** bytes:
   *"+480 B, all of it this PR's, from wherever develop left the file."* Re-measuring the entry against
   the new base instead is precisely the misattribution §15.1 forbids, and it is the easy mistake — it
   produces a number that looks right and a sentence that is false. (Under the headroom rule the delta
   is itself computed, so this clause now means: recompute against the new base, never hand-carry.)
3. **Name the PR that landed first.** **git cannot count these conflicts afterwards.** Every colliding
   branch is brought forward onto the tip immediately before merging, and that update is where the
   conflict is resolved — so the merged DAG records a clean, sequential, non-overlapping history and
   **the resolution erases its own evidence.** The reason string is the only durable record that the
   collision happened at all.

**Do not do this by hand, ideally.** A helper that performs all three clauses, refuses rather than
guessing where it cannot reconcile, and stamps one fixed wording is designed but **not shipped in this
tree**; until it is, apply the clauses by hand and keep the wording fixed. The fixed wording is the
point: the resolutions written by hand in one day were already **four different sentences** by its
end, and a convention retyped from memory each time is *one rule, several representations, nothing
comparing them*.

**And it is checkable now, which §15.1 was not.** `check_context_budget.py`'s `chain_breaks()` asserts
that every chain is continuous and terminates at its `max_bytes`. That turns a dropped entry from a
silent loss into a named finding — an earlier near-miss passed every gate green, because until then
**nothing read `from` on any code path.** It is report-only; `--enforce-chain` exists, and **whether CI
should use it is an open decision** — a test (`ChainIsReportOnlyTests`) fails if anything wires it
first. A sibling report-only pass, `reconcile()`, compares each touched artifact's measured post-merge
bytes with base + declared deltas, so a resolution that silently dropped or added bytes shows up as a
mismatch with a direction.

## Router entry

**Status:** phases 0-1 BUILT; §14 BUILT; the §15.6 chain check and the §12 after-merge block BUILT
(report-only); the §3.4 headroom successor BUILT and enforced on new raises; phases 2-3 unbuilt.

**What it decides:** Its enforcement half: the byte ratchet (the cap still report-only) and the pointer
check — **which BLOCKS since phase 1**, `--enforce` in CI. A raise on a router is the documented
affordance, not a defeat. **§14 is trap 3 and it points the OPPOSITE way from §4.7's two — the oracle
silently OVER-reported ignored, so a green check looked at nothing**: a CRLF `.gitignore` (added at
*checkout*; the blob is LF) leaves a blank line as the **empty pattern**, matching any trailing-slash
path. Read it before touching `check_ignored` — **`-v` WIDENS the output**, so the obvious fix trades
one false positive for another. Reviewed against a later remediation design and found **not
majority-retirable** — check #2, §14's trap, §12's incident, §13's decisions and §15's convention are
unrelated to the one thing that WAS broken: §3.4's raise-to-exact-size convention, a high-water mark
rather than a limit. Its successor (headroom ≈ 4 weeks trailing growth, floor ~2k tokens) is in
`check_context_budget.py`. **§11**: Q1 decided (tier C stays unbudgeted by default + an opt-in checked
list, unbuilt), Q3 measured (the 4:1 token estimate was wrong — **~2.59 B/token**, now the module's
`BYTES_PER_TOKEN`), Q4 corrected as stale (the scan scope is pinned by `ScanScopeTests`), Q2 and Q5
carried forward open — Q5 with a fresh, reproducible cross-PR instance (§12.6) and a proposal for a
script-authored raise under the headroom rule.
