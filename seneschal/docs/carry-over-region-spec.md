# `carry-over.md` — bounded regions instead of prepend-and-keep

**Status:** `PARTIAL(carryover_region.py + check_carryover_prose.py BUILT; §4 decided — head ceiling
stays report-only, a witness-only ad-hoc note goes to scripts/notes.py's dated file, never
carry-over; the Brief stays read-only on carry-over.md)`

## 1. The problem

Left alone, `state/carry-over.md` only ever grows. Measured over consecutive nightly snapshots, it
gained a steady block of lines every night with zero prunes, caused by exactly two accumulation
points:

- the Wrap **prepending** a full `WRAP SEED` block onto the **entire previous file** every night, and
- ad-hoc dated notes **appended** at the bottom by other turns and never revisited.

Meanwhile `loops.py`'s own generated region — built for exactly this problem — was never actually
wired to run against the live file, so the file held no `BEGIN GENERATED` marker at all.

The fix: **give the Wrap a bounded, always-rebuilt region for its nightly snapshot, the same shape
`loops.py`'s generated region already uses for the open-work-item projection**, so a night's WRAP
SEED is a *replacement*, not a page glued onto yesterday's.

## 2. The design — three kinds of content, three different rules

`carry-over.md` is three things stacked in one file, top to bottom:

```
<!-- WRAP SEED: BEGIN -->
… tonight's snapshot, REBUILT WHOLE every night …
<!-- WRAP SEED: END -->

… hand-written head — free text, bounded by a line-count tripwire, never touched by tooling …

<!-- BEGIN GENERATED — seneschal/scripts/loops.py render · state/open-loops.json -->
… the open-work-item projection …
<!-- END GENERATED — seneschal/scripts/loops.py render -->
```

### 2.1 `WRAP SEED` — replaced whole, every night

The Wrap's own current-state block (Tomorrow / Done / Slipped / Waiting on the owner / Standing
holds / Do NOT re-raise) is *already* a full snapshot on every run — it doesn't restate anything from
the night before, it recomputes all of it from the store, the calendar and the reminders tracker.
Every earlier WRAP SEED in a multi-night sample is 100% redundant with the one above it. So the fix
is not a smarter diff, it is a smaller claim: **the region holds only tonight's block, and writing it
always throws away last night's**, the same way `loops.py render --write` throws away the prior
GENERATED region rather than diffing against it.

Nothing durable lives only in the WRAP SEED prose: open loops belong in `open-loops.json`, held
approvals in `pending-approvals.json`, standing-safety items in `standing-safety.json`. A WRAP SEED
that vanishes at the next Wrap loses nothing that isn't *also* recoverable from its own generating
read (tomorrow's calendar, tonight's store state) or already durable elsewhere.

### 2.2 The hand-written head — kept, bounded, never touched by tooling

Between the regions sits free text: the dated ad-hoc project notes a session appends when it has
something to say that isn't an open loop and isn't tonight's snapshot (a side project's status
block, a list of sub-tasks for some tool, a witness-only entry). **No tool in this design writes,
rebuilds, or prunes this region.** It is exactly as hand-written as it always was.

What changes is that it is now **measured**. `carryover_region.py check` counts its lines and prints
a tripwire warning — never a failure, never a truncation, the same "tripwire not a quota" posture
`loops.py`'s own `PROJECTION_TRIPWIRE` uses for its projection — past a ceiling (default 60 lines,
`--head-line-ceiling` to override). Sixty is not a measured optimum; it sits comfortably above a
typical amount of legitimately-still-open hand-written content, so the tripwire fires on *growth*,
not on the file's current honest size. A tripwire that fires immediately gets ignored the same way a
gate that is red on the day it lands gets disabled.

### 2.3 The `GENERATED` region — already built, owned elsewhere

`loops.py render --write` already does everything this spec would otherwise have to build for a
second bounded region: it appends a marked, always-rebuilt projection of `open-loops.json` below the
hand-written content, asserts the hand-written prefix is untouched before and after the write, and
refuses a malformed region rather than guessing at one. **This spec does not change that code.** The
measured gap was never in `loops.py` — it was that nothing called `loops.py render --write` against
the live file. That is a scheduling gap (a Dream step or daemon task), not a design gap, and this
spec flags it rather than resolving it (§5).

`carryover_region.py check` treats `GENERATED`'s markers (imported from `loops.BEGIN_MARK` /
`loops.END_MARK`, never re-typed) as a second region for the *same* structural invariants — present
at most once, well-formed, counted toward the file total — without ever writing it. `write-region`
refuses `--name GENERATED` outright and names `loops.py render --write` as the actual owner, so this
tool cannot become a second writer of a region that already has one: one writer per fact, enforced by
refusal, not by convention.

### 2.4 Ad-hoc dated notes — NOT a fourth region; two existing homes instead

The second accumulation point — notes appended at the bottom through the day, never revisited — is
not solved by giving it a bounded region, because a region that is *appended to* forever is exactly
the bug this spec exists to remove. Two homes fit, and which one depends on what the note is:

- **It names something with a closing event** (a PR to merge, a decision to get, a task to finish) →
  `loops.py add`, the work-item register that already renders into the `GENERATED` region above. Its
  own `terminal_state` refusal is the test: *"can you name the event that would close this?"* If yes,
  it is a work item and belongs there, not in prose that has to be hand-pruned.
- **It is witness-only** — something to hold rather than solve, a "do NOT raise cold" entry,
  something with no closing event by design. `loops.py add` (correctly) refuses it, because it fails
  the same membership test on purpose. It does not belong in `carry-over.md` either, because nothing
  ever revisits or expires it there. It goes to `scripts/notes.py`'s dated file (§4 question 2).

## 3. `seneschal/scripts/carryover_region.py` — the mechanism

Stdlib only, alongside `memory_write.py` and `loops.py`. It is the one writer of the `WRAP_SEED`
region and the one machine-readable inventory of every region `carry-over.md` holds; it is
**deliberately not** a writer of the `GENERATED` region, which stays `loops.py`'s alone.

### 3.1 `write-region --name WRAP_SEED [--allow-empty]`

Body on stdin, exactly the shape `memory_write.py write` and `loops.py render --write` already use.

1. **`--name` other than `WRAP_SEED` is refused** — today that means `GENERATED`, named explicitly in
   the refusal message pointing at its real owner (`loops.py render --write`), and any future region
   this file doesn't yet know about. A generic `--name` flag that silently accepted an unregistered
   region would re-create the exact bug this spec fixes: a caller composing an ad-hoc slab of text and
   asking a tool to drop it in wherever, unbounded, forever.
2. **An empty body is refused without `--allow-empty`** — `memory_write.write_text`'s own precedent
   and reason: atomicity stops a partial write, not a complete write of nothing, and an
   empty-but-atomic write looks exactly like data loss from outside.
3. **The target must exist.** Like `loops.py render --write`, this tool refuses to invent
   `carry-over.md` from nothing — bootstrap-on-absence stays a `references/memory.md` rule (copy
   `carry-over.example.md`), not something this tool infers.
4. **All known regions are scanned first**, and a malformed one (duplicate opener, orphan closer, a
   closer before its opener) refuses the write, exactly as `loops.split_hand_written` does for
   `GENERATED` — the boundary is what the containment is made of, so a disagreement about where it
   falls is not something to write through.
5. **If `WRAP_SEED` markers exist, the span between them (inclusive) is replaced in place.** If they
   don't, the new region is inserted at the very top of the file — the position the old prepend-based
   mechanism already put it in, so a first run adds markers without moving any existing content.
6. **Everything outside the `WRAP_SEED` span, byte for byte, is asserted unchanged** — computed before
   the write and re-read from disk after it, mirroring `loops.write_render`'s own before-and-after
   assertion pair (original restored if the second check fails). `carry-over.md` is the most-written
   memory file; a region tool for its most-written slice inherits that paranoia rather than
   re-deriving it.
7. Written atomically through `memory_write.write_text`, line endings and any BOM preserved.

### 3.2 `check [--head-line-ceiling N] [--json] [--enforce]`

Read-only, exit 0 always except on a **structural** finding with `--enforce`:

- Each known region (`WRAP_SEED`, `GENERATED`) is **present 0 or 1 times** and, if present,
  well-formed (opener before closer, no orphan half). A region is legitimately allowed to be absent —
  a fresh install, or a file `loops.py render --write` has never touched — so "must exist" is never
  asserted; "must not be malformed" is.
- The **hand-written head** — every line outside every known region's span — is counted against
  `--head-line-ceiling` (default 60, §2.2) and reported as a tripwire, never a failure, regardless of
  `--enforce`.
- `--enforce` exits 1 only on a structural finding (malformed/duplicated region). This mirrors
  `loops.py`'s own split between a hard refusal (a malformed region, which is a bug) and a tripwire
  (too many items, which is real content that must never be dropped to make the check pass).

`check` cannot run in CI: `state/carry-over.md` is gitignored, so no PR diff ever contains a real
one. It is a host-side / Dream-side tool. Its CI presence is `check_carryover_prose.py`, a narrow
prose guard: the instruction files that tell a live Wrap how to write `carry-over.md` must name
`carryover_region.py`, and must never regress to the old "compose the new block, then
`memory_write.py write` the whole file" shape.

## 4. Decided questions

Kept for the reasoning and the rejected alternatives.

**Question 1 — the hand-written head's line ceiling. Decided: 60 lines, report-only** (§2.2) —
tunable via `--head-line-ceiling` without a code change, matching `loops.py`'s tripwire-not-a-quota
posture (nothing is ever dropped to satisfy it). Rejected:

- *A tighter ceiling (e.g. 20-25 lines)*, forcing a faster decision on ad-hoc notes — more
  false-positive tripwire noise on a legitimately busy week for an earlier warning.
- *No ceiling at all, just a reported count* — loses the one signal that would flag this exact
  failure mode before the file reaches thousands of lines.

**Question 2 — where a witness-only ad-hoc note goes** (it fails `loops.py add`'s membership test on
purpose, §2.4). **Decided: a dated notes file with its own retention**, built as
`seneschal/scripts/notes.py`: `add "<text>" [--source chat|watch|job] [--date YYYY-MM-DD]` appends to
`state/notes/YYYY-MM-DD.md` (the owner's activity day, `activity_day.py`'s cut), `list --days N`
reads the last N days (the Wrap's own read window is 2 — today's + yesterday's), `prune --days N`
(default 90) deletes files by filename date. Wired into `modes/chat.md`'s "note this" / witness-only
path. A home that matches what these notes are — a record with no closing event, meant to be found
later by grep or recall, not re-surfaced every night. Rejected:

- *Fold them into the RAG index as a new `source` type* — costs a schema decision; buys semantic
  recall instead of a flat grep.
- *Leave them in `carry-over.md`'s hand-written head*, relying solely on Question 1's tripwire — the
  status quo, and the shape that grew the file in the first place.

**Question 3 — does the morning Brief write anything to `carry-over.md`? Decided: no — the Brief
stays read-only** with respect to `carry-over.md`, and `modes/brief.md` says so explicitly. The
Brief reads a *different* carry-over object (the journal's own carry-over callout) and writes nothing
to `state/carry-over.md`. Giving it a second, competing writer into a file this spec narrows to
exactly two writers (`carryover_region.py` for `WRAP_SEED`, `loops.py` for `GENERATED`) would reopen
the two-writers-racing failure. Rejected: *the Brief also calls `loops.py render --write` at the top
of its run*, so an item added or resolved overnight shows before the next Wrap — buys freshness,
costs a second call site for a projection that already re-renders on its own cadence once §2.3's
scheduling gap is closed.

## 5. Out of scope

- **An install's existing `state/carry-over.md` is not migrated.** The first bounded `WRAP_SEED`
  region lands at the next Wrap after this mechanism is in place (§3.1 step 5 inserts it at the top).
- **`loops.py render --write` is not newly scheduled anywhere** (§2.3) — that is a Dream-step or
  daemon-task decision, named here as the remaining gap behind "the file holds no GENERATED marker,"
  and left for a narrower follow-up.
