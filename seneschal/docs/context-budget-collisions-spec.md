# Spec — the one file every PR must edit, and the history a hand-resolution erases

**Status:** `PARTIAL(phase 1 — the chain rule and the --enforce-chain flag, deliberately NOT wired into CI, §9 q1 open; the phase 2 rechain helper is not in this tree, and phase 3's convention lives in the parent spec)` — the design was built as all three phases in one PR upstream; this tree carries phase 1 in
`check_context_budget.py` (`chain_scan`, `chain_breaks`, `render_chain`, `--enforce-chain`, plus
`ChainContinuityTests` and `ChainIsReportOnlyTests`). **No format changed and nothing migrated**,
exactly as §8 requires: `seneschal/context-budget.json`'s shape is untouched, and the CI workflow gains
nothing for this spec.

**Why the three phases were built in one PR, against §6's own phasing.** §6 separates them to reduce
risk per merge, and at build time **the dominant risk was another PR touching this ledger**: every
merge turned every other open PR `CONFLICTING` on the one ledger file, with several still open. Two
PRs would have been two more collisions of the exact kind this spec is about. §6's stated reason for
holding phase 3 back — *"a sentence written before the tool exists describes something you have to do
by hand"* — dissolves when the tool ships in the same PR.

**One measurement in this document was superseded by its own build: §1.4's "0 violations" stopped
holding.** It was true when the spec was written. By build time **continuity was still 0** across 25
chains and 224 entries, but **one artifact failed termination** (the references router; see §1.4's
note) and **4 entries failed the date ordering §1.4 also audited.** §4.2 never proposed a date rule,
and that call is vindicated rather than lucky — see §1.4. **In this tree the ledger carries no
`raises[]` entries at all** (33 budgeted artifacts, 0 chains), so `--enforce-chain` exits 0 here
trivially — which says nothing about whether the rule would stay green once raises accumulate.

**Scope (as designed):** one new validation rule inside `check_context_budget.py` (~30 lines), its
tests, one new flag, and — phase 2 — one new stdlib script beside it. **No format change. No
migration. No new dependency. No CI blocking change without an owner decision.**

**Parent:** [`context-budget-spec.md`](context-budget-spec.md) — this is a follow-up to its §12
(the cross-PR hole) and §15 (the honest-attribution convention), and it **does not reopen either**.
Read that first; it is the authority on intent and this document defers to it everywhere they touch.
The mechanism itself is also documented in `check_context_budget.py`'s module docstring ("The chain
must be continuous").

**Machines this sits on and must not contradict:**
[`context-budget-spec.md`](context-budget-spec.md) **§3.4** (the ratchet, its escape hatch, and the
two alternatives it explicitly rejected — a separate raises log, and a commit trailer), **§3.7** (the
Goodhart trap: a gate satisfiable by relocating text), **§6** (phase 0 is report-only *by design*, and
why), **§12.1–§12.5** (two green PRs, one over-budget tree; the after-merge block and its open
question 5), and **§15.1** — the owner's decision that ***a raise describes its OWN bytes, and
inherited bytes get their own entry and their own name.*** That decision is load-bearing here: every
option below is judged partly on whether it keeps that sentence true, and the recommended one is
chosen because it makes it **checkable** rather than merely written down.

**The ask:** five merge conflicts in one day on the budget ledger, all of them additive and
semantically trivial, one of which silently dropped a `raises[]` entry while every automated check
stayed green. Evaluate at least six options, recommend one, say why each loser lost.

**The short answer, up front.**

- **The conflicts are not the problem worth fixing. The silent history loss is.** Five hand
  resolutions cost perhaps an hour; the near-miss cost a `raises[]` entry that no check could have
  reported. §4.1 gives "do nothing" a real hearing and it very nearly wins — it loses on exactly
  one fact, and that fact is the near-miss.
- **Recommended: (b) + (f), in that order, as two independently-mergeable phases.** Teach the checker
  that a `raises[]` chain must be continuous (**phase 1**, ~30 lines, green on the ledger as measured
  — §1.4), then ship a rechain helper so the fix is one command and one wording (**phase 2**).
- **(c) per-artifact files loses on measurement, not on taste.** **All five of the day's collisions
  were two PRs raising the SAME artifact** (§2.1), which a split does not help at all. (d) loses
  because it is §3.4's already-rejected alternative wearing a different file extension. (e) loses
  because no PR can install it and a `union` driver on JSON is actively unsafe.

---

## 1. What was true — read, not remembered

Everything in this section was read or run against the ledger and checker as they stood when the spec
was written, in the repository this framework was distilled from. The magnitudes are that tree's; they
illustrate the mechanism, and the seneschal ledger (which starts with empty chains) will grow its own.

### 1.1 The file, and why it is one file

The budget ledger (`seneschal/context-budget.json` here) was then **777 lines, 131,359 bytes on disk,
one JSON document**, holding **36 budgeted artifacts and 85 `raises[]` entries** (counted with a
standalone audit script). Its header carries `_basis`, `_seeded_from` and a multi-line `_note`; the
budgets live under a single `"budgets"` object, keyed by artifact path — the scripts router, the docs
router, the Dream mode file, and so on.

It is one file **on purpose**, and the reason is in `context-budget-spec.md` §3.3: tracked and
reviewable, *"at the root of the tree it governs"*, deliberately not under `seneschal/state/`, because
**a budget that cannot appear in a diff cannot be defended in review, which is the entire mechanism
of §3.4.** Any option below that moves or splits the ledger is arguing with that paragraph and has to
say so.

The consequence is structural and is the whole subject of this spec: **every PR that adds a router
row, a spec, a script entry or a rule file must edit this one document**, so two such PRs conflict by
construction. `check_context_budget.py`'s `BUDGETED_GLOBS` is the list of shapes that must be
declared — every `CLAUDE.md`, every `subagents/**/SKILL.md`, `seneschal/SKILL.md`, `persona/*.md`, and
`.claude/rules/**/*.md`. That list is deliberately wide (§3.1: a hole in it is the whole exploit), and
wide means frequently touched.

### 1.2 What the ratchet actually validates — and the three things it does not

The raise validation in `check()`, in full, as it stood:

```python
# The ratchet: a raise needs a matching, reasoned raises[] entry (§3.4).
if base is not None and artifact in base:
    was = base[artifact].get("max_bytes")
    if isinstance(was, int) and isinstance(limit, int) and limit > was:
        raises = entry.get("raises") or []
        last = raises[-1] if raises else None
        good = (isinstance(last, dict) and last.get("to") == limit
                and str(last.get("reason") or "").strip())
        if not good:
            unratcheted.append(...)
```

It reads **`raises[-1]` and nothing else**, and from that entry it reads **`to` and `reason` and
nothing else**. Three consequences, each verified against the code rather than inferred:

1. **`from` is never read.** No entry's `from` is compared to anything, on any code path. `measure()`,
   `check()` and `after_merge()` do not mention it.
2. **No entry but the last is ever read.** A chain of 21 entries is validated by its 21st. The first
   20 could be deleted, reordered, or invented and the check would not move.
3. **The rule only arms when `limit > was`.** A PR that does not raise the number is not checked at
   all — so a resolution that keeps `max_bytes` correct while losing an intermediate entry passes
   through the branch that never runs.

`test_check_context_budget.py`'s `RatchetTests` (8 cases) pinned exactly this behaviour and no more:
at-budget, over-budget, a raise without an entry, a raise with a matching entry, a mismatched `to`, an
empty `reason`, a lowering, and the bootstrap. **There was no case about continuity**, because there
was no continuity rule to pin.

### 1.3 What the checker *prints* is not what the file *contains* — measured

The checker prints the ready-to-paste stanza a failing raise must append (it still does, in
`render()`):

```
Append: {"on": "YYYY-MM-DD", "from": …, "to": …, "reason": "why this is worth the startup context"}
```

`context-budget-spec.md` §3.3's worked example uses `"on"` as well. **All 85 entries in the live file
used `"date"`. Zero used `"on"`** (counted across the whole document; the key histogram was exactly
`{date: 85, from: 85, to: 85, reason: 85}`).

Nobody did anything wrong and nothing is broken — `date` is a perfectly good key. The finding is what
it *demonstrates*, and it is this spec's thesis arriving as an accident rather than an argument:
**the two fields the checker reads (`to`, `reason`) have never drifted; both fields it does not read
(`on`/`date`, `from`) have.** One drifted in its name, the other is the one a hand-resolution can
silently truncate. An unvalidated field in a hand-maintained ledger does not stay correct because it
is documented; it stays correct until the first time someone is in a hurry.

### 1.4 The chain was continuous — and that is the fact that makes phase 1 cheap

Auditing all 36 artifacts and all 85 entries for the three properties a chain rule would assert, at
spec time and again at build time:

| Property | Violations at spec time | Violations at build time |
|---|---:|---:|
| every entry's `from` equals the previous entry's `to` | **0** | **0** |
| the last entry's `to` equals `max_bytes` | **0** | **1** |
| `date` present and non-decreasing | **0** | **4** |

23 artifacts carried a chain; 13 carried `"raises": []`. The two hot ones carried most of the history:
**the docs router 21 entries, the scripts router 20**, then the Chat mode file 8, and a tail of 4s and
2s. (At build time: 36 artifacts, **25 chains, 11 empty, 224 entries**, and the two hot routers were
at **79 and 74** — the concentration §2.4 measured had intensified, not spread.)

**The second column is the build re-running the audit rather than trusting it, and it changes two
things.** It does not change the recommendation; it changes what phase 1 is allowed to claim.

**Termination, 1 violation — and it was a real bug, not drift.** The references router
(`seneschal/references/CLAUDE.md`'s budget) carried an entry reading `4797 -> 5216` while `max_bytes`
was left at **4,797**. The entry landed; the number never moved. That is the *inverse* of §2.2's
dropped-entry case and the ratchet cannot see either of them (§1.2(3): it only arms when the number
went **up** against the merge base). It was also, exactly, the +419 B that artifact had been reported
over budget ever since — so the finding was visible the whole time in a form nobody connected to the
chain.

**So §4.2's rule was NOT green on the tree it merged into, and that is the argument for §9 q1's
answer being "not yet" rather than against the rule.** Report-only, it prints a true finding about a
real defect. Wired to `--enforce-chain`, **the PR that built it would have merged a red build** —
this section's own reason for why that matters, arriving as data.

**Date ordering, 4 violations — and this is why §4.2 does not make it an assertion.** All four are a
rebase writing its entry on the day it *lands* rather than the day it was drafted, which is the
correct behaviour of the very workflow §4.6 automates. A date rule would have gone red on merge day
for something that is not a bug. §4.2's three assertions were the right three.

**This is the single most important cost fact in the document.** A new rule that is red on the day it
merges is a rule someone disables — `context-budget-spec.md` §6 says so as its load-bearing scheduling
decision, and §6's own phase 1 needed 76 standing findings resolved before the pointer check could
turn blocking. The chain rule needed **zero backfill** at spec time: it went green on the tree as
written.

### 1.5 The byte gate is report-only in CI, and only the *other* checks enforce

`.github/workflows/ci.yml` runs `python seneschal/scripts/check_context_budget.py --enforce-headroom`
— **no `--enforce`, and no `--enforce-chain`**. The same step runs `check_context_pointers.py
--enforce`. The workflow's own comment block records why: the *pointer* check is blocking because a
dangling pointer is unambiguously a bug with a local fix, while the byte gate stays report-only pending
the parent spec's host-side producer change; `--enforce-headroom` blocks only the narrow shape of a
NEW raise.

`context-budget-spec.md` §15.5 already names the consequence: bytes accumulated across several merges
without one red build, because nothing reads these entries at merge time except a reviewer. Anything
this spec proposes to *detect* therefore has to answer a second question — **detected into what?** —
and §4.2 does.

### 1.6 One more constraint, only visible by reading the merge guard

`merge_guard.py`:

```python
DOCS_ONLY_ALLOWLIST = frozenset({"seneschal/context-budget.json"})
```

pinned by `test_merge_guard.py`, which asserts the set is *exactly* that one path. The module
docstring calls it *"exactly one entry and… not a place to add things"*; the scripts index calls it
**"THE SOFT UNDERBELLY"**, and its membership test is *"changing it cannot change what runs"*.

This exists because this ledger is the one non-`.md` file nearly every prose PR must touch, and it is
what lets a docs-only PR stay merge-on-green under the owner's docs-only merge policy. **It is also a
cost that lands on option (c) and on nothing else** — see §4.3.

---

## 2. The evidence

### 2.1 The five collisions, and the measurement that decides option (c)

Reconstructed by diffing the ledger across each first-parent merge on `develop` on the day of the ask
(a standalone collision-scope script). Every budget move that landed that day, in merge order (PRs
lettered; SHAs and times omitted):

| Merge | PR | Artifact | max_bytes |
|---|---|---|---|
| 1 | A | docs router | 15,739 → 16,580 (+841) |
| 2 | B | scripts router | 74,241 → 77,683 (+3,442) |
| 3 | C | docs router | 16,580 → 17,218 (+638) |
| 4 | D | docs router | 17,218 → 17,777 (+559) |
| 5 | E | scripts router | 77,683 → 79,781 (+2,098) |
| 6 | F | docs router | 17,777 → 18,257 (+480) |
| | | scripts router | 79,781 → 82,874 (+3,093) |
| 7 | G | Dream mode file | 22,807 → 23,047 (+240) |
| | | scripts router | 82,874 → 84,544 (+1,670) |

**Nine raises, across exactly three artifacts.** Four PRs raised the docs router (A, C, D, F); four
raised the scripts router (B, E, F, G); one PR (G) raised the Dream mode file, alone.

> **Every one of the five collisions was two PRs raising the same artifact.** A split into
> per-artifact files removes a conflict only when the two PRs touch *different* artifacts. On this
> day's evidence that is **one raise out of nine**, and it collided with nobody.

That single table is why §4.3 loses, and it is worth stating plainly because the intuition runs the
other way: the ledger *looks* like a wide file that many PRs touch in scattered places, and it is
really a narrow file that many PRs touch in **the same two places**.

The collisions are attested in the ledger itself, in the reason strings written at the time of each
resolution — three on the scripts-router chain and one on the docs-router chain. Paraphrasing the
fullest of them: *"RECHAINED AT MERGE (same delta, new base): sibling PRs landed on develop first and
moved this artifact, so this entry's `from` no longer chained. Same +1670 B of THIS PR's own bytes;
nothing re-measured downward, no PR's bytes dropped. FIFTH cross-PR budget collision on this repo
today — the §12.1 mechanism, named rather than absorbed."* Another names the sequence: the FOURTH
collision that day, after A/C, then D, then E/F.

### 2.2 The near-miss — and it is the reason this is a spec rather than a shrug

Resolving PR F by taking one side of the conflict wholesale **dropped a `raises[]` entry**: the
+480 B step on the docs router, 17,777 → 18,257. It came back only because someone diffed the resolved
file against the PR's own version by hand after the fact.

**Every automated check stayed green throughout**, and §1.2 is why, mechanically:

- The final `max_bytes` was correct, so the file measured under budget → not in `over`.
- The last entry's `to` matched the new `max_bytes` → not in `unratcheted`.
- `from` is never read, so the discontinuity the drop created was not a thing the checker had a name
  for.
- The after-merge block (§12.5) measures **bytes in a prospective tree**, not ledger structure, so it
  had nothing to say either.

The loss is not a byte-count error. It is an **attribution** error, and attribution is the entire
product: §3.4's escape hatch has exactly one defence — *the defence is review, and the reviewer is the
owner* — and §15.1 exists so that nobody's entry ever lies. A chain with a hole in it does not lie in
any single sentence; it lies by omission, and it looks perfect. **A green board was compatible with a
budget's history having been quietly truncated**, which is the same shape as §3.7's Goodhart trap and
§10.1's "thin versus lobotomized" — the check passes because it is not looking at the thing that was
lost.

**One thing this near-miss is not:** evidence that anyone was careless. It was the fifth hand
resolution of a mechanical, additive conflict in one day. That is the condition under which this class
of mistake is *expected*, which is precisely §12's argument about the cross-PR hole — the strongest
available argument that it is structural and not a lapse.

### 2.3 The wording had already started to drift — two resolutions, two rationales

The four rechain reasons written that day were not one convention:

| Entry | Opening clause | Collision named as |
|---|---|---|
| scripts router, 1st | *"RECHAINED AT MERGE (same delta, new base): PR E landed on develop first…"* | "FOURTH… today" |
| scripts router, 2nd | *"RECHAINED AT MERGE (same delta, new base): PR E landed on develop first…"* | "FOURTH… today" |
| scripts router, 3rd | *"RECHAINED AT MERGE (same delta, new base): **sibling PRs** landed on develop first…"* | "FIFTH… on" the date |
| docs router | *"RECHAINED AT MERGE, **not re-measured**: this PR (C…) was written against a 15,739 B base…"* | "the section 12.1 cross-PR mechanism" |

Two of the four are byte-identical templates; the third generalises the producer to "sibling PRs" and
re-dates the count; the fourth is a different sentence entirely. All four say the same true thing.
**Nothing is wrong yet** — this is drift at the point where it is still cheap, and it is included as
evidence for §4.6 rather than as a complaint. A convention that is retyped from memory each time is
the classic rule-drift shape — *one rule, two representations, nothing comparing them* — four
representations in.

### 2.4 The base rate — was the day atypical? No.

Over the last **120 first-parent merges** on `develop` (about fifteen days — the whole life of the
ledger at the time), a base-rate script:

| | |
|---|---:|
| Merges examined | 120 |
| Merges touching the budget ledger | **41 (34.2%)** |
| Days on which ≥2 merges raised the **same** artifact | **6 of 15** |
| Total raises recorded in the window | 93 across 26 distinct artifacts |
| Raises landing on the docs router | **20** |
| Raises landing on the scripts router | **19** |

**A third of all merges touch this file**, and the two hot routers absorb 39 of 93 raises. The
same-artifact multi-raise pattern recurred on 6 of 15 days. **One earlier day was worse than the day
of the ask**: 12 budget-touching merges, 26 raises across 16 artifacts, with the docs router ×4 and the
scripts router ×4 — the same two files, the same shape. That day is already written up as §12's
incident.

So the honest reading of "concurrency this high may be atypical" is: **the concurrency is typical, and
the day of the ask was the fourth-heaviest day of the ledger's existence.** Whatever else §4.1 has
going for it, it cannot rest on this being a one-off.

### 2.5 The negative result — git history cannot count these conflicts, and that matters

I attempted to convert §2.4's day-level proxy into a true conflict count by testing branch overlap:
two PRs conflict by construction iff both edit the ledger *and* the later one's merge-base predates
the earlier one's merge commit. Over the same 120-merge window that measurement returns
**0 same-artifact overlapping pairs** — including for A/C, which we *know* collided.

The reason is not a bug in the query, and it is worth recording because it will mislead the next
person who tries:

```
PR A  merge 1   merge-base with develop = the merge before it
PR C  merge 3   merge-base with develop = merge 2 (PR B)   <- already contains PR A
PR D  merge 4   merge-base with develop = merge 3 (PR C)
PR F  merge 6   merge-base with develop = merge 5 (PR E)
PR G  merge 7   merge-base with develop = a later tip
```

Every colliding branch was brought forward onto the tip immediately before it — *that update is where
the conflict was resolved* — so the merged DAG records a clean, sequential, non-overlapping history.
**The resolution erases its own evidence.** The conflicts survive in exactly one place: the reason
strings quoted in §2.1, because a human chose to name them there.

Two things follow. First, the five collisions in this spec rest on those attestations plus the ask,
**not** on a reconstruction I can re-run — §7 records that. Second, it is a point in favour of §4.6:
a rechain note is not merely tidy, it is **the only durable record that the collision happened at
all**, which raises the cost of it being retyped from memory each time (§2.3).

---

## 3. The problem, sharpened

Three failures are tangled together in the ask and they have different sizes, different fixes, and
different urgencies. Separating them is most of the work:

1. **The conflicts.** Two PRs edit one JSON document; git reports a textual conflict. Frequency:
   ~5 on a heavy day, on 6 of 15 days measured. Cost: a few minutes each, entirely mechanical.
   **This is the loudest failure and the least important one.** It is self-announcing — git refuses
   to merge — and it has never once produced a wrong *number*.
2. **The silent history loss.** A hand resolution can drop an intermediate `raises[]` entry while
   leaving `max_bytes` correct, and **nothing detects it**. Frequency: observed once, in five
   attempts. Cost: the ledger's attribution guarantee, silently. **This is the quiet failure and the
   important one.** It is the only one where a green board is wrong.
3. **The convention drift.** The rechain rationale is retyped each time and had four wordings after
   one day (§2.3). Cost: nothing yet; the archival value of the reason string degrades as the
   wordings diverge, and §2.5 shows that string is the only surviving record of the collision.

**The design consequence.** A fix aimed at (1) — a split, a format change, a merge driver — does not
touch (2) at all: a hand-resolved per-artifact file can drop an entry exactly as easily, and a
union-merged JSONL can *reorder* one. A fix aimed at (2) is small, format-free, and works no matter
how (1) is or is not addressed. **So the ordering is forced: fix (2) first, and treat (1) as
optional.** Every recommendation below follows from that sentence.

**And a constraint on all of them, from §15.1.** A rechain must preserve the entry's **delta** while
re-basing its `from` — the +480 stays +480, starting from wherever `develop` actually left the file.
Anything that instead re-measures the entry against the new base absorbs the other PR's bytes into
this PR's reason clause, which is precisely the misattribution the owner's decision forbids. All four
hand resolutions that day did this correctly and said so (§2.1); a mechanism must not make it
possible to do otherwise by accident.

---

## 4. The six options

### 4.1 (a) Do nothing — the null hypothesis, given a real hearing

**The case for it is stronger than it looks, and it is made here at full strength.**

- **The conflicts are self-announcing and cheap.** Git refuses to merge; nobody has to notice
  anything. Five in a day at a few minutes each is under an hour, against a repo that merged 11 PRs
  that day. Measured against the alternative — build, test, document and maintain new tooling — an
  hour is not obviously the losing side.
- **Every one was additive and semantically trivial** (the ask says so and §2.1 confirms it: nine
  raises, zero disagreements about a number). There is no case in the record of two PRs wanting
  *different* budgets for one artifact.
- **The repo already refuses tooling on this exact reasoning, repeatedly and correctly.**
  [`scripts-subdirectories-spec.md`](scripts-subdirectories-spec.md) measured its candidates, shipped
  **nothing**, and **closed the axis** (§5). [`session-job-watcher-spec.md`](session-job-watcher-spec.md)
  argues its own value down. "It would be nice to automate this" is not a standard this repo accepts,
  and it should not start here.
- **§3.6 already refuses the adjacent move**: the check *never* rewrites its own config, because
  automation that edits the ledger is how an unrelated PR fails a week later for something it did not
  cause.
- And the honest framing of the friction: five conflicts is a symptom of **11 PRs in one day**, which
  is a throughput most repos would take. Optimising the merge friction of a healthy day is a strange
  place to spend engineering.

**Why it loses, and it loses on exactly one fact.** Not the hour. The near-miss (§2.2). Under (a),
the mechanism that caught the dropped entry — a human diffing the resolved file against the PR's own
version, after the fact, unprompted — remains the *only* mechanism. That is not a control; it is
someone having a good day. And the loss it catches is the one the ledger exists to prevent: §3.4's
whole design is that history lives *at* the number so the fifth raise is legible beside the fourth,
and §15.1's whole point is that no entry lies. **A truncated chain defeats both while every check
reports green**, and §14.1(d) names why that class survives: *an over-reporting check produces no
symptom to investigate.*

**So (a) is rejected narrowly and specifically.** If the near-miss had not happened, this document's
recommendation would be (a) with §4.2 as a note for later. It did happen, and the fix for it is ~30
lines that went green on the tree as measured with no backfill (§1.4). At that price, accepting a
known silent-corruption path is not frugality.

**What survives from (a), and it survives into the recommendation:** the friction itself — problem
(1) in §3 — genuinely does not justify a format change, a migration, or host-side config. §4.3, §4.4
and §4.5 are all rejected partly on (a)'s reasoning. **(a) wins the argument about conflicts and
loses the argument about history**, which is exactly the split §3 predicts.

### 4.2 (b) Make the checker catch a broken chain — **RECOMMENDED, phase 1**

**The rule, in three assertions**, evaluated for every artifact whose `raises[]` is non-empty:

1. **Continuity** — for every `i > 0`, `raises[i].from == raises[i-1].to`.
2. **Termination** — `raises[-1].to == max_bytes`.
3. **Origin** — `raises[0].from` is *not* constrained. The first entry's base predates the ledger
   for most artifacts and there is nothing to check it against; inventing a rule there would go red
   on history rather than on mistakes.

Assertion 1 catches a dropped, reordered, or duplicated entry. Assertion 2 catches a `max_bytes`
edited without an entry **in the case the existing ratchet cannot see** — §1.2(3): the ratchet only
arms when the number went *up* relative to the merge base, so a resolution that keeps the final
number right while losing the last entry slips past it. Together they are the smallest rule that
fails on §2.2's actual event.

**What it costs.** ~30 lines in `check()`, one new list in the returned dict (`broken_chains`
alongside `unratcheted`), a render block, and 4-6 test cases in a new `ChainContinuityTests` class
built the way §8 requires — **fixture configs, not the live tree**, so it cannot go red when someone
edits `CLAUDE.md`. It reads only data already loaded; no new git call, no new dependency, no measurable
runtime.

**What makes it unusually cheap here, and it is the deciding factor: it was green on the tree as
measured.** §1.4 — 0 gaps, 0 tail mismatches, across all 36 artifacts and all 85 entries. There is no
backfill, no standing-findings sweep like phase 1's 76, and no window in which the rule is red while
someone decides whether to disable it. It can merge on the day it is written.

**Where it must be delivered, and this is the one real design question.** §1.5: the budget check runs
**without** `--enforce` in CI, so a `broken_chains` finding folded into `violations` would print into
a green job's log — §12.2's *invisible in advance, and unread in arrears*, exactly. Three ways out:

- **Fold into `violations` and leave it report-only.** Zero risk, near-zero value: it detects into a
  log nobody reads.
- **Fold into `violations` and add `--enforce` to CI.** Rejected outright — that is **phase 2 of the
  parent spec**, gated on its host-side producer change, and turning the byte ratchet blocking as a
  side effect of a structural rule would be deciding the owner's open question for them.
- **A separate narrow flag — `--enforce-chain` — that exits non-zero on a broken chain and on
  nothing else.** **This is the proposal.** It leaves the byte gate report-only exactly as today, and
  it is justified by the same argument that made the *pointer* check the safe one to turn blocking
  first (§4.1/§6 of the parent): **a broken chain is unambiguously a bug, with a local fix, and no
  producer problem behind it.** Nobody has to trim anything to make it green; they have to restore an
  entry they dropped. That is a different kind of red from "this file is too big", and it is the kind
  that does not get disabled.

Whether that flag goes into `ci.yml` in the same PR is a genuine fork, and it is §9's question 1
rather than a decision taken here.

**What (b) does not do, stated plainly:** it prevents **no conflicts at all**. Frequency addressed:
zero. It converts a silent corruption into a loud one, and that is its entire claim.

### 4.3 (c) Split into per-artifact files — **REJECTED on measurement**

A context-budget directory, one file per budgeted artifact, plus a loader that folds
them into today's `budgets` dict.

**The case for it** is real and is the reason it was worth evaluating: two PRs touching different
artifacts stop conflicting entirely, and the two 20-entry chains stop being 40% of a 777-line file
that everyone edits.

**Why it loses, in the order the arguments killed it:**

1. **It does not address the conflicts we actually had.** §2.1: **5 of 5 collisions were two PRs
   raising the same artifact**, and a split leaves same-artifact collisions exactly as they are. On
   the day of the ask it would have prevented **zero**. The reason is §2.4's concentration — the docs
   router (20 raises) and the scripts router (19) absorb 42% of every raise ever recorded, so the
   conflict distribution is not spread across 36 artifacts, it is stacked on two. **A split is a fix
   for a uniform distribution, and this one is a power law.**
2. **It does nothing for the near-miss.** A hand-resolved per-artifact file for the docs router drops
   an intermediate entry exactly as easily as the big file does. Problem (2) is untouched.
3. **It breaks the merge guard's soft underbelly.** §1.6: `DOCS_ONLY_ALLOWLIST` is a `frozenset` of
   **exactly one path**, pinned by a test asserting that exact set, and its membership property is
   *"changing it cannot change what runs."* 36 files means 36 entries — or a pattern, which destroys
   the property that makes the allowlist safe. Either way a docs-only PR's merge-on-green path now
   depends on a list that grows with the repo. That cost is invisible from the ledger and is only
   found by reading `merge_guard.py`; it is not decisive on its own, but it is unpriced in the
   obvious framing of this option.
4. **It argues with §3.3 without a better answer.** The ledger is one reviewable document *on
   purpose*. 36 files means a raise is reviewed in a hunk with no siblings — the fifth raise stops
   being legible beside the fourth in the same diff, which is §3.4's stated reason for `raises[]`
   living where it does.
5. **Migration cost is real and one-way-ish**: 36 files, a loader, the `_basis`/`_seeded_from`/`_note`
   header needing a home, `_budgets_at()` and `merged_tree()`'s blob reads rewritten (they read one
   path out of a git tree), and `check_context_pointers.py`'s scope list to re-check.

**What would change this answer:** if the raise distribution flattened — many artifacts each raised
occasionally, rather than two raised constantly — (c) would start to pay. §2.4 is the measurement to
re-run before anyone proposes it again.

### 4.4 (d) An append-only JSONL ledger with `max_bytes` derived — **REJECTED**

A `context-budget.jsonl`, one line per raise, `max_bytes` computed by folding the chain rather than
stored.

**The case for it** is the strongest of the three format changes: git merges append-only files far
better than structured ones; a derived `max_bytes` makes assertion 2 of §4.2 true *by construction*
rather than by checking; and the repo already runs several append-only JSONL ledgers with a shared
contract (`mouth.py`, `turns.py`, `pending_checks.py`, `transcript_archive.py` among them).

**Why it loses:**

1. **It is §3.4's already-rejected alternative wearing a different extension.** §3.4 considered and
   refused a separate raises log — *a second file that must agree with the first is precisely the
   duplication this repo is trying to stop* — and refused a commit trailer because *you cannot read a
   number's history by looking at the number.* A JSONL moves the history away from the number;
   deriving `max_bytes` removes the second-file-disagreement half of the objection but not the first.
   Reopening a decided alternative needs new evidence, and "it merges better" is not evidence about
   the property §3.4 was protecting.
2. **The `state/` precedent does not transfer, and the difference is the point.** Those ledgers are
   **gitignored, machine-written, fail-open, and never merged** — `record_*` never raises because a
   failed append must cost the row and never the message. This file is **tracked, human-written,
   reviewed in a diff, and merged constantly.** The contract that makes JSONL right there is about
   surviving a crash mid-append; the problem here is surviving a *review*.
3. **Append-only does not mean conflict-free.** Two PRs appending at the end of the same file is the
   canonical git conflict. It is *easier* to resolve correctly — and (e)'s union driver would
   automate it — but §4.5 kills that half, and without it (d) buys ordering-safety, not
   conflict-avoidance.
4. **It weakens review.** Today a reviewer sees the artifact, its budget, its tier, its note and its
   whole chain in one hunk. Folded from a flat log, a raise is a line whose meaning depends on every
   preceding line for that key.
5. **Migration is the largest of the six**: a new parser, 85 entries converted, `_budgets_at()`,
   `merged_tree()`, `after_merge()` and both test modules rewritten, and every existing consumer of
   the JSON shape re-verified.

**The one piece of (d) worth keeping** is its best idea, and §4.2 already has it: *derive or check the
relationship between the chain and the number instead of trusting them to agree.* Assertion 2 buys
that for ~10 lines and no migration.

### 4.5 (e) A git merge driver — **REJECTED**

`.gitattributes` plus a `union` or custom driver on the budget ledger.

1. **A `union` driver on JSON is actively unsafe, not merely inelegant.** Union merge concatenates
   both sides' hunks verbatim. On a structured document that produces duplicate keys, doubled
   `raises` arrays, and missing or doubled commas — i.e. either invalid JSON (loud, fine) or
   **valid JSON with a duplicated key, where Python's `json.load` silently keeps the last one**
   (quiet, and a strictly worse version of the bug being fixed). This is not a hypothetical about
   this repo; it is what union merge does to JSON.
2. **A *custom* driver would need a script plus host configuration, and no PR can install it.**
   `.gitattributes` can name a driver, but `merge.<driver>.driver` lives in git config. That is the
   same class as `merge_guard.py`, `bash_path_guard.py` and `/assistant` itself: **inert until the
   owner installs it**, on every checkout separately — the daemon's, a dev checkout, and *every
   delegated worktree*, which are cut fresh and would each inherit an unconfigured driver. §14.2 of
   the parent already worked this exact population question and concluded the consumer-side fix is
   the load-bearing one *because* config reaches no existing checkout.
3. **It addresses only problem (1), and (a) already wins that argument.** Paying host-side
   configuration and a bespoke merge script to save an hour a day is the trade §4.1 says not to make.

**The one thing (e) has going for it** — zero format change — is a real merit and is why it was worth
listing. It is not enough against an unsafe default and an uninstallable custom.

### 4.6 (f) A resolution helper — **RECOMMENDED, phase 2**

A `rechain_budget.py` beside the checker: given the ledger with a resolved-but-unchained state,
recompute each chain so that every `from` follows its predecessor's `to`, **preserving each entry's
delta**, set `max_bytes` to the final `to`, and emit the rechain note in one fixed wording. (Built
upstream; **not present in this tree** — see §6.1. `check_context_budget.py`'s
`_git_allowing_conflict` exists to serve it.)

**What it must and must not do** — these are the invariants, and they come from §15.1:

- **It preserves `to - from` per entry and re-bases `from`. It never re-measures.** That is the
  difference between "+480 B, all of it this PR's, from wherever develop left the file" and "+X B,
  silently including someone else's bytes" — the misattribution the owner's decision forbids. All four
  hand resolutions did this correctly (§2.1); the helper makes doing otherwise require editing the
  helper.
- **It never invents, drops, merges or reorders an entry.** It only changes `from` values and
  `max_bytes`. If it cannot reconcile a chain — a delta that does not add up, an entry whose `to`
  precedes its `from` — it **refuses and names the entry**, in `jobs.py`'s `preflight_refusal` shape:
  fail-open on unknown, fail-closed on known-bad.
- **It stamps a note in one wording**, appended to the rechained entry's `reason`, naming the PR that
  landed first and the base it re-chained onto. §2.3's four wordings become one, and §2.5 is why that
  string matters: it is the only durable record that the collision happened.
- **It writes nothing but the ledger, and it is never run by CI or by the daemon.** §3.6 is explicit
  that the check does not rewrite its own config; this is not the check, and it runs only when a human
  or an agent resolving a conflict invokes it. That boundary is the whole reason it is safe.

**What it costs:** one stdlib script, its tests, a paragraph in the scripts index, and a sentence in
the parent spec's §15. No format change. No migration. It touches the ledger only when invoked.

**What it does not do:** prevent a conflict. Frequency addressed: zero, same as (b). It makes the
resolution correct-by-construction and identical every time, and it removes the drop-an-entry failure
mode **at the point of the fix** while (b) removes it **at the point of review**.

**Why it is phase 2 and not phase 1.** It is strictly more code than (b), it has no forcing function
on its own — a helper nobody runs is worth nothing — and (b) is exactly the forcing function: once a
broken chain is a named finding, the helper is the obvious way to clear it. Built the other way round,
the helper is a convenience nobody reaches for on a day they are in a hurry, which is the only day it
matters. **This is baseline-before-optimizing applied honestly: the detector before the convenience
layer.**

The sibling [`concurrent-pr-collisions-spec.md`](concurrent-pr-collisions-spec.md) later took the
same ledger rule — union both sides, rechain, recompute `max_bytes` — into an automated conflict
resolver for the whole PR; it is the place to look for how the rechain step is driven end to end.

### 4.7 The combination, and why it is this one

**(b) + (f).** (b) is the detector and is worth building alone; (f) is the fix and is worth building
only after (b) exists. Together they close problem (2) at review time and at resolution time, close
problem (3), and **deliberately leave problem (1) open**, because §4.1 wins that argument.

The alternative combination the ask names — **(b) + (c), "the thorough version"** — is rejected on
§4.3(1): (c)'s contribution is preventing cross-artifact conflicts, and the day's evidence contains
none. It would be thorough about a problem this repo does not have.

---

## 5. The decision table

| | (a) do nothing | **(b) chain check** | (c) per-artifact files | (d) JSONL, derived | (e) merge driver | **(f) rechain helper** |
|---|---|---|---|---|---|---|
| **Conflict frequency addressed** | none | **none** | **0 of 5 measured** (all same-artifact, §2.1) | partial — appends still collide without (e) | most textual conflicts, if installed | none — makes them cheap, not rarer |
| **Catches a silent drop** | **no** — one human diff, after the fact | **YES — the exact near-miss event** | no | prevents the `max_bytes` half only | no — union merge can *cause* one | **prevents it at the fix** |
| **Migration cost** | none | **none** | 36 files + loader + 4 call sites | largest of the six: parser, 85 entries, 2 test modules | none | none |
| **Format change** | no | **no** | yes | yes | no | no |
| **Host-side config required** | no | **no** | no | no | **YES — no PR can install it**, on every checkout separately | no |
| **New code** | none | **~30 lines + 4-6 tests** | loader + migration | parser + rewrite | driver script + config | one script + tests |
| **Green on the measured tree** | n/a | **yes — 0 violations, §1.4** | n/a | n/a | n/a | n/a |
| **Reversibility** | n/a | **total — delete the rule** | poor: 36 files back into 1, plus consumers | poor | total — delete two lines | total — delete the script |
| **Argues with a parent decision?** | no | **no** | §3.3 (one reviewable document) | **§3.4** (the rejected separate log) | no | no |
| **Verdict** | rejected, narrowly (§4.1) | **RECOMMENDED — phase 1** | rejected on measurement | rejected | rejected | **RECOMMENDED — phase 2** |

---

## 6. Phasing — smallest useful first, each merging green on its own

**Phase 1 — the chain rule. ~30 lines. Merges green on the measured tree.**
`check_context_budget.py` gains a `chain_breaks(budgets)` helper and a `broken_chains` list in
`check()`'s return; `render()` gains a block naming the artifact, the entry index, the expected
`from`, the found `from`, and the byte gap. Tests: a new `ChainContinuityTests` over **fixture
configs** (§8) — a clean chain passes; a dropped middle entry fails; a `max_bytes` edited without a
final entry fails; an empty `raises[]` is clean; a single-entry chain is clean; `raises[0].from` is
unconstrained. Exit criterion: green on `develop` on merge day, which §1.4 established at spec time.
**Whether `--enforce-chain` also goes into `ci.yml` in this PR is §9 question 1** — the rule is worth
having either way, and the flag can be added without it being wired.

**Phase 2 — the rechain helper.** `rechain_budget.py` with §4.6's invariants, its tests, and its entry
in the scripts index. Gated on phase 1 having merged: the helper's job is to clear a finding that does
not exist yet.

**Phase 3 — the convention, written down where a resolver will meet it.** One short subsection in
`context-budget-spec.md` §15 recording the rechain rule as a rule (preserve the delta, re-base the
`from`, name the PR that landed first) and pointing at the helper. Deliberately last: a sentence
written before the tool exists is a sentence that describes something you have to do by hand, which
is what §2.3 already measured going wrong.

**Not a phase, and named so nobody schedules it:** turning the *byte* gate blocking. That is the
parent spec's phase 2, gated on its host-side producer change, and it is untouched here.

### 6.1 What was actually built — all three, in one PR — and what this tree carries

The phasing above is preserved as written; this records the deviation and its reason rather than
editing it away.

**All three merged together upstream.** The phasing exists to reduce risk per merge, and at build time
the dominant risk was not the code — it was **another PR touching the budget ledger.** Every merge
that day turned every other open PR `CONFLICTING` on that one file, several were still open, and two
PRs would have been two more instances of the collision this document is about. §6's stated reason for
holding phase 3 back is that *"a sentence written before the tool exists describes something you have
to do by hand"* — an objection that disappears when the tool ships in the same PR.

| Phase | Where it landed | In this tree? |
|---|---|---|
| 1 — the chain rule | `check_context_budget.py`: `chain_scan`, `chain_breaks`, `broken_chains` in `check()`, `render_chain`, `--enforce-chain`. `ChainContinuityTests` + `ChainIsReportOnlyTests` over fixture configs (§8) | **yes** |
| 2 — the rechain helper | a `rechain_budget.py` script + its test module + its scripts-index entry | **no** — the checker's `_git_allowing_conflict` anticipates it |
| 3 — the convention | `context-budget-spec.md` **§15.6** | with the parent spec (which names the helper) |

**Three things phase 1 does that this document did not specify, each because building it surfaced
them.** (a) `chain_break` — `reconcile()`'s existing abstention gate — was **refactored onto the same
scanner** rather than left as a second expression of continuity, since *one rule, two representations,
nothing comparing them* is the failure mode this repo names by name. It deliberately passes
`limit=None`: a termination break is a real finding but does not stop deltas from summing, and
abstaining on it would silence the reconciliation on exactly the artifacts already known to be wrong.
(b) A **malformed entry stops the walk**, or the missing `to` manufactures a second finding out of the
first. (c) An **absent `max_bytes` yields no termination finding** — that is a different check's
defect, and reporting it here names one bug twice under two names.

**And one thing phase 2 does that §4.6 did not anticipate.** §4.6 says the helper *"never merges"*
entries; the commissioning brief asked for **one consolidated entry per raised artifact.** Where they
disagree the spec wins, so the helper **preserves the branch's own entries as separate entries**,
each re-chained — and "consolidated" is honoured in the sense that actually matters: it mints **no
entry per intervening merge**, which is where someone else's bytes would otherwise acquire your name.
In the overwhelmingly common single-entry case the two readings produce identical output.

**The brief also asked for `to` to be RE-MEASURED off the working tree, and §4.6 says the helper
*never re-measures*.** Resolved in the spec's favour, and the distinction is worth stating because it
is subtle: `to` is **derived** as `from + delta` (rule 3), and the tree is then read **to check that
result**, never to source it. Re-measuring is only dangerous when the `from` is left stale — that is
what folds the other PRs' bytes into your span — but a helper that took its `to` from a measurement
would silently re-anchor the entry onto the measured size and change the delta, which §15.1 forbids.
So both halves of the brief survive: the delta is preserved *and* asserted against the tree, and the
assertion refuses rather than adjusting.

---

## 7. What I could not verify

Non-negotiable section. Everything below is named because it was **not** run or **not** read, and
anything resting on it should be read as weaker than the measurements above.

1. **The five collisions are attested, not reconstructed.** §2.5: the merged DAG shows zero
   overlapping same-artifact pairs, because each conflict was resolved by bringing the branch forward
   before merging. My evidence is (i) the ask, and (ii) the four reason strings in the ledger, which
   name "FOURTH" and "FIFTH" and list the sequence. **I did not observe a single conflict myself**,
   and I cannot re-derive the count from git. The fifth collision (the Dream mode file + the scripts
   router, PR G vs E/F) is the one I have the least independent evidence for — its reason says
   "sibling PRs" without naming them, and G's Dream-mode raise collided with nobody per §2.1's table.
2. **The near-miss is entirely second-hand.** I verified that the +480 B entry **was present** after
   the fix (`from: 17777` → `to: 18257`) and I verified from the code why its loss would have gone
   undetected (§1.2). I did **not** observe the dropped state, and no artifact of it survives in git.
   The claim "`check_context_budget.py` stayed green" is my reading of the code, not a run against the
   broken file.
3. **I did not build or run the proposed rule at spec time.** §1.4's "0 violations" is from a
   standalone audit script implementing §4.2's three assertions over the live file — **not** from a
   modified `check_context_budget.py`. The claim is "this data satisfies the rule as specified", not
   "the implementation passes". (The build later re-ran it; §1.4's second column.)
4. **I did not run the test suite at spec time.** Neither `test_check_context_budget.py` nor the full
   `unittest discover` was executed. My statements about `RatchetTests` come from reading the file and
   its 8 case names.
5. **Union-merge behaviour on JSON is asserted from knowledge of `git merge=union`, not demonstrated
   here.** I did not construct a fixture repo and force a union merge on a JSON file. §4.5(1) would
   be stronger with that experiment, and it is the one claim in §4 I would test first if the decision
   went toward (e).
6. **The merge-driver installation claim is reasoned, not probed.** I read `merge_guard.py` and the
   repo's precedent for host-side hooks; I did **not** check whether any `merge.*.driver` config
   already exists on the host, or test `.gitattributes` behaviour in a delegated worktree.
7. **§2.4's base rate is a proxy in one direction.** "Days on which ≥2 merges raised the same
   artifact" is necessary but not sufficient for a conflict — two such PRs could have been strictly
   sequential. Per §2.5 I cannot tighten it, so **6 of 15 days is an upper bound on collision days**,
   not a count. The 34.2% touch rate and the per-artifact raise counts are exact.
8. **I did not measure resolution time.** "Perhaps an hour total" in the ask is taken at face value;
   nothing in the repo records how long a conflict resolution took, so §4.1's cost side is the one
   number in this document with no measurement behind it.
9. **The lowering interaction is measured but unresolved.** §9 question 2 — I found exactly one
   lowering in 120 merges and confirmed its effect on the chain, but I did not design the rule for it.
10. **I did not check whether any *other* consumer reads this file.** `check_context_budget.py` and
    the cockpit were not grepped for additional readers of the `raises` shape, so (c)/(d)'s migration
    cost may be understated.

---

## 8. What is NOT in scope

Recorded so none of it is re-proposed later as an oversight:

- **Any code, at spec time.** The spec PR was the spec alone; the build (§6.1) came after.
- **Any format change to the budget ledger** — not a split (§4.3), not JSONL (§4.4), not a key rename.
  **Including `on` → `date`:** §1.3 is evidence about unvalidated fields, **not** a proposal to rename
  anything. The 85 entries were consistent with each other; the checker's printed stanza and §3.3's
  example are the things that are out of step, and correcting either is a one-line follow-up that
  this spec does not make.
- **Any migration of the budgeted artifacts.**
- **Any `.gitattributes` or git-config change** (§4.5).
- **Turning the byte gate blocking / adding `--enforce` to the budget check in CI.** Parent spec
  phase 2, gated on its host-side producer change, and the owner's call (§15.5).
- **Re-opening §12.5's question 5** (should an after-merge finding block, should `develop` require
  up-to-date branches). It is adjacent — a merge queue would have prevented several of the day's
  collisions outright — and it is **the owner's, already asked, and not re-asked here**. §9 question 3
  only notes the overlap. **Half-answered later, out of scope of this document even so**:
  `required_status_checks.strict` was enabled on `develop` — the up-to-date-branches half of the
  lever, not a merge queue. See §9 question 3's addendum for what that does and does not change here.
- **Trimming either hot router.** The docs and scripts routers absorbing 42% of all raises (§2.4) is a
  real finding and it belongs to [`scripts-subdirectories-spec.md`](scripts-subdirectories-spec.md),
  which has already closed the rules axis.
- **Auto-tightening** (§3.6 — Dream's job, phase 3 of the parent).
- **Anything touching `seneschal/state/`.**

---

## 9. Open questions for the owner

Five, and each is a genuine fork where the decision changes the design rather than the wording. **All
five were still open after the build**; nothing below was answered by building it, and question 5 is
new and was found by building it.

**1. Should the chain rule BLOCK, and if so how?** §4.2 proposes `--enforce-chain` — a narrow flag
that fails on a broken chain and on nothing else, leaving the byte ratchet report-only exactly as
today. The argument for is §4.1's and the parent's §6: a broken chain is unambiguously a bug with a
local fix and no producer problem, which is the same reason the *pointer* check was the safe one to
turn blocking first. The argument against is that CI turning red on this repo's most-edited file is
precisely the pressure §3.4 warns about, and a rule that blocks on the day of a five-conflict afternoon
is a rule someone reaches for the bypass on. **If it does not block, phase 1 detects into a green
job's log** — §12.2's "unread in arrears" — and is worth noticeably less. My lean: block, via the
narrow flag, in a PR *after* phase 1 merges green, so the rule and the enforcement are separately
revertible.

> **Built, and the answer got harder rather than easier.** `--enforce-chain` exists, exits 1 on a
> broken chain and on nothing else, and **is not in `ci.yml`**; a test
> (`test_enforce_chain_is_NOT_wired_into_ci`) reads the real workflow and fails if anything wires it.
> My lean above was *"in a PR after phase 1 merges green"* — **and upstream, phase 1 did not merge
> green.** §1.4's second column: the references router failed termination on `develop`, so the
> condition the lean was attached to was not satisfied, and wiring the flag in that PR would have
> merged a red build.
>
> That was one standing finding, not seventy-six, and it was nearer than it looked. But **which edit
> clears it is itself a decision**: raise `max_bytes` to 5,216 — the entry was already written and
> already named its own producer, so this completes a half-landed raise, and is evidently what its
> author intended — or trim the file back under 4,797. Neither was the build PR's to make; its brief
> said explicitly not to absorb the standing violations into its own entry.
>
> **In this tree the question is open on different facts:** the ledger carries no `raises[]` entries
> yet, so `--enforce-chain` exits 0 today. Wiring it here would be green on the day it lands — the
> precondition the lean asked for — which makes the decision cheaper, not automatic.

**2. What should a chain rule do about a LOWERING?** Measured, and it is live rather than theoretical.
There was exactly **one** lowering in 120 merges: a grounding restructure that took `seneschal/SKILL.md`
from 77,296 → 23,232 B (**−54,064**) — see [`grounding-restructure-spec.md`](grounding-restructure-spec.md).
§3.4 says *a lowering always passes without ceremony* and a test pins that. The effect on the ledger
was visible: `seneschal/SKILL.md`'s chain **began at 23,232** — the post-lowering floor — so **the
ledger contained no record that a 54 kB reduction ever happened.** The chain's history is already
truncated at the one place it moved downward. Three options: (i) leave it — a lowering resets the
chain's origin, and assertion 2 stays `last.to == max_bytes` with a lowering silently starting a new
chain; (ii) require a lowering to append an entry too (`to < from`), making the chain a complete
history in both directions; (iii) allow `max_bytes < last.to` as an unrecorded lowering, weakening
assertion 2. **This becomes urgent if §3.6's Dream auto-tightening is ever built**, since that
generates lowerings nightly — which is why it is a fork and not an implementation detail. My lean:
(ii), because "the history lives at the number" is §3.4's whole thesis and a −54 kB move is the most
interesting thing that ever happened to that number.

**3. Is the merge-policy lever the one you actually want instead?** §12.5's open question 5 offers
**required-up-to-date branches or a merge queue** on `develop`. That is the only option anywhere in
this family that **prevents** the collisions rather than making them cheap — it would have forced each
of the day's five PRs to re-run against the moved base. It is not this module's call, it is already
in front of the owner, and I have deliberately not re-argued it. But if the answer there is *yes*,
then problem (1) closes at the source and this spec's phase 1 + 2 are still exactly as necessary,
because a merge queue does not make a hand resolution honest — it only reduces how many there are.
**Nothing below phase 1 changes either way**; I raise it because deciding it would change how much
(a)'s "do nothing about conflicts" costs.

> **Answered in part: `required_status_checks.strict` was enabled on `develop`.** That is the
> up-to-date-branches half of the lever; a merge queue was not adopted. It closes problem (1) at the
> source **going forward**, for any PR that is stale relative to `develop` at merge time — GitHub
> itself now refuses that merge. It does **not** retroactively touch a PR that is already
> `CONFLICTING`, and it adds a shape this document never modeled: a PR that is clean and green can
> still be refused for being merely BEHIND, which is not a conflict and needs no hand resolution — a
> plain merge-up clears it. **My lean's own claim holds exactly as stated**: nothing below phase 1
> changes, because a BEHIND PR was never a budget-ledger collision — the byte-ratchet and chain
> machinery here answer a different question and are untouched. What changed instead is upstream, in
> the picker/ask machinery (`pr_sweep.py`, `picker_mark.py`, `merge_guard.py`), which reads
> `mergeStateStatus` and refuses to spend or offer a tap on a BEHIND head.

**4. Should the rechain note be machine-readable?** §2.5's finding is that the reason string is the
**only** durable record a collision happened — git erases the rest. Phase 2's helper could stamp a
structured field (`"rechained_from": 16377, "rechained_onto": "<PR>"`) beside the prose instead of
only appending a sentence. **The argument against is real and is §1.3's**: a field the checker does
not read is a field that drifts, and adding one un-validated key to 85 entries' worth of convention is
how `on`/`date` happened. The argument for is that "how often does this actually happen" is currently
answerable only by grepping four differently-worded sentences. My lean: **prose only**, one fixed
wording, unless the owner wants the count — in which case the field should be validated by the same
rule that validates the chain, or not added.

---

**5. Should a raise be per-PR at all? — NEW, found by building phases 1-3.** Recorded because it is a
real question and it is the owner's; **nothing was changed for it.**

`.github/workflows/ci.yml` runs `check_context_budget.py` with **no `--enforce`** (only the narrow
`--enforce-headroom`), while the pointer check, the doc-status check and the state-write check all run
enforcing. §1.5 records that as the reason bytes accumulated across several merges without one red
build. The consequence goes one step further than §1.5 states, and it is worth saying plainly:

> **Nothing in CI requires a PR to touch this ledger at all.** A PR that grew a router and simply did
> not raise its budget would go green, with one more reported violation printed beside the ones
> already standing on `develop`. **The per-PR raise is a convention, not an enforcement** — and every
> collision in this document, every hand resolution, and this spec's entire phase 2 are costs that
> convention imposes.

Three shapes, and they are genuinely different:

- **(a) Keep it as it is.** The convention is doing real work: the ledger was continuous, 224 entries
  deep, and §3.4's *the defence is review, and the reviewer is the owner* has held. The cost is the
  collisions, and phases 1-2 make those cheap and safe rather than merely frequent.
- **(b) Enforce it** — the parent spec's phase 2. Makes the convention real, and is gated on the
  producer change for a reason: a budget over unchanged producers turns CI red every week and the
  escape hatch becomes the weekly ritual it exists to prevent.
- **(c) Stop requiring a per-PR raise** and let a periodic sweep re-anchor the numbers instead. That
  removes the collisions **at the source** — no PR edits the ledger, so no two PRs conflict on it —
  and it costs the thing the ledger is *for*: a raise stops being a decision reviewed at the moment
  it is made, and becomes a number a script moved. §3.4 argues against this without naming it.

**My lean is (a) for now**, because phases 1-2 just changed the cost side of the argument and the
sensible thing is to see whether they land before re-pricing it. But the question is not mine, the
observation is not a bug report, and I have deliberately **not** made the check blocking, **not**
touched the workflow, and **not** stopped raising.

---

*One sentence on the obvious irony, and it is deliberately one sentence: this spec's own router row put
the docs router **1,034 B over an 18,257 B budget it had zero headroom against**, so it needed the very
`raises[]` entry §4.2 is about — and if another PR raised that artifact first, it needed exactly the
rechain §4.6 proposes to automate. (**The spec PR deliberately did not take that raise**: its scope
was the spec, the budget check is report-only so nothing was blocked, and the raise was left as the
one edit whose `reason` should name whichever base it actually lands on — §15.1.)*

## Router entry

**Status:** **PHASE 1 IN THIS TREE (the chain rule + `--enforce-chain`); phases 1-3 were built together
upstream in ONE PR; no format changed, nothing migrated. `--enforce-chain` EXISTS AND IS NOT WIRED —
§9 q1 is open, and a test goes red if anything wires it.**

**What it decides:** Follow-up to the parent's §12/§15: one JSON ledger every router PR must edit, so
two conflict **by construction** — five times in one day, and again on the build day. **The conflicts
are not the finding.** A hand resolution dropped a `raises[]` entry and every check stayed green: the
ratchet reads `raises[-1]`'s `to` and `reason` and **nothing else — `from` is never read, on any
path**. **(c) per-artifact files loses on measurement: all 5 collisions were two PRs raising the SAME
artifact**, and 2 artifacts held 42% of every raise. (d) is §3.4's rejected separate log in a new
extension; (e) installs by no PR, and `union` on JSON can *cause* the bug. **(a) very nearly wins,
losing on the near-miss alone.** **Git cannot count these conflicts post-hoc — resolution erases its
own evidence**, so the reason strings are the only record. **Built as (b) the chain rule + (f) a
rechain helper + the convention at `context-budget-spec.md` §15.6, together, because the dominant risk
at build time was another PR touching the ledger** — several were open and every merge made them all
`CONFLICTING` — and §6's reason for holding the convention back dissolves once the tool ships beside
it. **§1.4's headline measurement was SUPERSEDED by its own build**: continuity was still **0** across
25 chains / 224 entries, but **termination failed on one artifact** (the references router, whose
entry said `4797 -> 5216` while `max_bytes` was left at 4,797 — the same +419 B the budget check had
reported all along), **so the rule was NOT green on the tree it merged into** and that is the concrete
argument against wiring the flag. **The date rule §4.2 declined to add would have been red too** — 4
entries, because a rebase writes its entry on the day it LANDS — so the three assertions were the
right three. **§9 gained a fifth question, found by building: CI runs this check with NO `--enforce`,
so nothing requires a PR to touch the ledger at all** — the per-PR raise is convention, not
enforcement, and whether it should be per-PR is the owner's decision.
