# Why `jobs.py` mislabeled finished work `ended-unknown`

**Status:** `MEMO` — measurement memo, not a plan of record · **Scope:** the `ended-unknown` records in
`../state/jobs/*.json`, `check_terminal()` and `reconcile()` in `../scripts/jobs.py`. §§1-8 record the
original investigation and made no behaviour change; §9 records the narrow fix that shipped (option A,
tightened, at the one call site §4 identified); §10 is the recurrence check §7 asked for — zero since
the fix. The B-vs-C decision for every other writer, and §5's forensic experiment for the historical
records, both stay open and stay the owner's.

**Why this exists.** Jobs were coming back with a status `jobs.py` could not read. This memo records
what the ledger showed, the mechanism the code makes possible, and the options, costed.

---

## 0. TL;DR

1. **The jobs were not failing. The bookkeeping about them was.** The logs of `ended-unknown` records
   end in a complete, clean final agent report — the same shape a `done` job's log has — and at least
   one opened a PR that was merged minutes after its record was stamped `ended-unknown`. The work
   finished; the record says it didn't.
2. **The rate was rising** — a handful of isolated cases, then a burst of them inside two days.
3. **The mechanism: `check_terminal()` treats "the shim's PID is gone" as proof the shim died before
   recording an outcome, with no re-read and no grace window.** That inference is the daemon's own
   choice, not a hardware fact (§3).
4. **The on-disk "corroborating" detail — `ended_at` matching `torn_down_at` to the microsecond — does
   not corroborate anything.** `reconcile()` stamps both fields in the same pass regardless of which of
   the two competing causes below actually happened (§3.2).
5. **A code-grounded mechanism for "the reconciler clobbered a shim that had already finished
   cleanly"**: `reconcile()`'s per-tick record snapshot is taken once, at the top of the pass, and never
   re-read before the write that follows — the exact shape `honour_cancel` was built to close, but only
   for cancels (§4).
6. **What cannot be told apart from the ledger alone: did the shim's process die before it ever got to
   write, or did it write and get overwritten a beat later.** Both leave the identical on-disk shape.
   Naming the experiment that would decide it, not guessing, is the point of §5.
7. **Three options, honestly costed, the owner's call:** a grace window (smallest, narrows but doesn't
   close the race), a revision counter at the `save_job` chokepoint (closes the whole class, medium
   cost), an explicit exit-stamp file (removes the PID inference entirely, but trades it for a second
   source of truth that can itself disagree with the record) (§6).

---

## 1. What's actually in the ledger

### 1.1 The signature

**Every `ended-unknown` record has `exit_code: null` and `attempts: []`.** Every `done` record has
`attempts` of length exactly 1. Every `cancelled` record has `attempts` of length 0, which is expected —
a cancel pre-empts before an attempt completes.

A separate, older corruption also exists in the wild — a record file that is entirely null bytes, a
torn or zero-filled write. It is not an `ended-unknown` case and is out of scope here; noted so it isn't
mistaken for the same bug later.

### 1.2 The rate

The count of `ended-unknown` records was low and scattered for weeks, then several landed inside two
days. One filter worth warning against: counting only jobs that ran with `--worktree` undercounts,
because a job that ran in the shared repo root carries the identical `exit_code: null` / `attempts: []`
signature without a `worktree` field (§1.3).

### 1.3 What the recent records share

A job that ran with `--worktree` (a `worktree` object on the record, `claude` in `argv`) shows
`ended_at == worktree.torn_down_at` to the microsecond. A job that ran in the shared daemon checkout
(no `worktree` field, `completion.skipped == "shared-repo-root"`) instead shows `ended_at ==
notified_at` to the microsecond. Same underlying pattern, different paired field, because there's no
worktree teardown to pair it with. §3.2 explains why this pairing is expected either way and doesn't
by itself prove anything about cause.

---

## 2. The jobs were not failures

The tails of the affected jobs' `.log` files end in a complete, clean final agent report — section
headers, a summary of what shipped, a test count, a PR link — the same shape a `done` job's log has,
not a crash trace or a truncated stream. One such PR was confirmed merged about three minutes after
its record was stamped `ended-unknown`.

This is the load-bearing fact for the whole memo: **the underlying `claude` work finished successfully.**
What failed is `jobs.py`'s ability to say so.

---

## 3. The mechanism, and a correction to how it was corroborated

### 3.1 `check_terminal()`

Called from `reconcile()`, which runs on the daemon's ~5 s tick. For a record still marked `running`:

```
if not pid_alive(pid):
    # The shim exits only after stamping a terminal status OR `retry-pending`, so a gone PID with
    # the record still on `running` means the shim itself died ... the outcome is genuinely
    # unreadable. Say so; never assume success.
    return {"status": ENDED_UNKNOWN, "ended_at": _stamp(now)}
```

No re-read of the record and no grace window sat between "the PID is gone" and "write
`ended-unknown`." The comment's premise — *the shim exits only after stamping a terminal status* — is
an assertion about the shim's own code, not something `check_terminal` can verify at the moment it
fires. On these records that premise did not hold: the outcome existed (§2), the record just doesn't
have it.

Worth noting for §6: the sibling branch just above it (a `running` record with no PID at all) already
has a grace period — `STARTUP_GRACE_SEC = 120`. The PID-went-away branch, the one producing these
records, had none. A grace window here isn't a new idea in this codebase; it's an asymmetry with an
existing sibling.

`pid` here (`rec["pid"]`, stamped at `_run_shim`'s own start via `os.getpid()`) is **the shim's own
process**, not the `claude` child (`child_pid`, tracked separately). `pid_alive()` is deliberately
conservative — on Windows it never uses a signal probe (`os.kill(pid, 0)` maps to `TerminateProcess`
there and would kill the job), it uses `OpenProcess`/`GetExitCodeProcess`, and anything it can't answer
reads as **alive**. So a `False` reading here is a real, OS-confirmed "this process is gone" — not a
flaky probe. That matters for §4: the *process* really had exited by the time `check_terminal` ran; the
open question is **when**, relative to the shim's own final write.

### 3.2 The "corroborating detail" doesn't corroborate

It is tempting to read `ended_at == torn_down_at` to the microsecond as evidence that "the reconciler
observed the PID gone and won the race against the shim's final write." Reading `reconcile()` end to
end shows this doesn't hold up.

`check_terminal` sets `ended_at` on a record inside the per-record loop. Later in that **same
iteration**, because the record is now terminal and not yet notified, `release_worktree` runs and
stamps `torn_down_at` — using the **same `now`** the whole pass was called with. Then, still in the same
iteration, the completion push stamps `notified_at` — same `now` again. **This happens for every
`ended-unknown` record `reconcile` ever produces, regardless of whether the shim genuinely died before
writing (case a, below) or wrote and got overwritten (case b).** The equality confirms only that the
reconciler — not the shim — produced this record, which was already obvious from `exit_code: null` /
`attempts: []` alone. It says nothing about which of the two causes is the real one: it is a structural
artifact of `reconcile()`'s single-pass design, not independent evidence.

---

## 4. A mechanism for the clobber case that the ledger alone can't show, but the code can

`reconcile()` calls `list_jobs(state_dir)` **exactly once**, at the top of its pass (`for rec in
list_jobs(state_dir):`). Every `rec` used for the rest of that record's processing — including the call
to `check_terminal(rec, now)` — is that one-time snapshot. Before the fix it was never re-read
immediately before the decision or the write.

Follow the timeline this makes possible: the daemon's tick starts, `list_jobs()` snapshots a job while
its shim is still `running`. Before this pass reaches that record in its loop (or across one ~5 s tick,
if the shim's whole remaining run — post-processing, its own final `save_job` — happens to fall inside
that window), the shim genuinely finishes: it re-reads for the cancel guard, classifies the outcome,
appends to `attempts[]`, writes its own terminal status, and exits. `pid_alive()` on its now-gone
process correctly returns `False` — the process really is gone. But `check_terminal` is evaluating the
**stale snapshot**, which still shows `status: running`. It concludes `ended-unknown`, and the write
that follows —

```
rec.update(changes)
save_job(state_dir, rec)
```

— writes that **stale snapshot plus the ended-unknown patch**, wholesale, back to disk. `save_job` →
`honour_cancel` → `save_json` doesn't merge with whatever the shim subsequently wrote; it overwrites it.
`honour_cancel` only re-reads and restores the fields a **cancel** owns (`CANCEL_OWNED_FIELDS`) — there
is no equivalent guard for an ordinary terminal write racing the same way.

This is the identical shape `jobs.py`'s own docstring already names for the cancel race — `cancel_job`
and the shim are two independent writers of the same file, and every writer is a load-modify-save with
no lock — generalized. `honour_cancel` closed it for cancels specifically. The reconciler's once-per-pass
snapshot means every record it touches on a given tick carries the same staleness risk, and only the
cancel path got a guard against a fresher on-disk write losing.

**This explains how a legitimate, already-completed shim write could get clobbered. It does not prove
that's what happened to any given record** — it is equally consistent with the shim's process having
been killed or crashed before it ever reached its final write (daemon restart, an OS-level kill on
worktree teardown, an unhandled exception between `proc.wait()` returning and the final `save_job`).
Both produce the exact same `exit_code: null` / `attempts: []` shape on disk. §5 is about telling them
apart.

---

## 5. What is NOT established, and the experiment that would settle it

The ledger cannot currently distinguish:

- **(a) The shim's process died before it ever reached its final write** — killed, crashed, or reaped,
  with no completed outcome to lose.
- **(b) The shim wrote its terminal status successfully, then a stale-snapshot reconciler write (§4)
  overwrote it** — the outcome existed on disk, briefly, and was destroyed.

`attempts: []` is consistent with both: in (a) the attempt was never appended; in (b) the reconciler's
write blindly replaces the whole record, including whatever `attempts[]` the shim had just added.
Nothing in the current record format captures "this field used to say something else." Choosing
between B and C before this is known is optimizing before measuring.

**Two ways to settle it, either sufficient on its own:**

1. **Per-attempt shim logging independent of the job record.** Have the shim append one line to a
   separate, append-only file (not `state/jobs/<id>.json`) at the moment it is about to make its final
   write — before, not after, the write that can be clobbered. If a future `ended-unknown` record has a
   matching append-log line, that's (b); if it doesn't, that's (a).
2. **A monotonic revision counter on the record, observed across a captured repro.** Increment a
   counter on every `save_job` call for a given id; if the shim's counter value is ever visible on disk
   (even transiently, via a debug read loop watching the file) and then a later `ended-unknown` write
   carries a LOWER counter value than what the shim's own attempted write would have produced, that's
   direct proof of (b). This is effectively option B (§6) run in observe-only mode before deciding to
   ship its enforcement.

Either experiment needs a live repro, not a retroactive read of existing records — none of them carry
the instrumentation that would answer this after the fact.

---

## 6. Three options

### A. Grace window

After `pid_alive` goes `False`, wait N seconds and **re-read** the record from disk before writing
`ended-unknown`. Smallest change; no schema change; the sibling no-PID branch already has this shape
(`STARTUP_GRACE_SEC`, §3.1) so it's not a new pattern in this file.

**Cost:** narrows the race window, doesn't close it — a shim whose write lands after the grace period
still gets clobbered. Does nothing for case (a): if the shim's process is genuinely gone with no
outcome to recover, waiting longer just delays the same correct `ended-unknown`.

### B. Revision counter at the `save_job` chokepoint

Every record carries a monotonically increasing revision. A write built from a stale in-memory copy —
exactly the `list_jobs()`-snapshot-then-write shape in §4 — is refused, the same way `honour_cancel`
already refuses to let a stale write relabel a cancel. This isn't a new pattern grafted on; it's
`honour_cancel` generalized to every field, at the one chokepoint (`save_job`) every writer in this
module already goes through.

**Cost:** medium — touches the read-modify-write shape of every caller of `save_job`, not just this one
symptom. **Closes (b) for the whole ledger, every writer, not just this bug.** Does not fix (a): if the
shim's process is genuinely gone before writing anything, there is nothing for a revision check to
recover.

### C. Explicit exit-stamp file

The shim's last act writes `<id>.exit` atomically; the reconciler reads that file instead of inferring
an outcome from PID liveness.

**Be precise about what this does and doesn't do, because it's easy to overstate:**

- **It closes (b).** A separate file the reconciler only reads, never blindly overwrites, can't be
  clobbered by the reconciler's own record write the way `rec.update(changes)` clobbered the JSON
  record.
- **It does NOT fix (a).** If the shim dies before its last act, there is no `.exit` file — the
  reconciler is exactly back to inferring from `pid_alive`, and the answer is `ended-unknown` again.
  What changes is that the answer is now **honest** rather than a guess dressed as a stamped status:
  the absence of the exit file is itself the evidence, not an inference from a process handle. C makes
  case (a) legible; it does not make the job report an outcome it never reached.
- **The real cost is not "more code."** It is a **second source of truth**: the job record and the
  `.exit` file are two files with no transaction between them, so they can disagree — the record can
  still say `running` while the exit file says `0` (a window between the two writes), or the reverse
  after a crash lands between them. That is a genuinely new consistency problem, not extra lines. B
  adds no new file and reuses the `save_job` chokepoint that `honour_cancel` already proved works in
  this exact codebase.

**Largest change of the three; adds one file per job.**

---

## 7. Why not just ship C

The instinct toward C is right in one respect: A and B both leave the PID-liveness inference in place
for case (a), and a partial fix to an inference-based bug is unsatisfying precisely because the
inference is the thing that's untrustworthy. C is the only option of the three that removes it.

The honest counter-argument isn't "C is too much work" — it's that **C trades one untrustworthy signal
for two files that can disagree with each other**, which is a new failure mode, not the absence of one.
And more importantly: **neither B nor C is justified yet, because §5's experiment hasn't run.** If the
answer turns out to be case (a) — every one of these records was a shim that really died with nothing
to recover — then C's entire value proposition (closing the clobber) is solving a problem these records
didn't have, and B's revision counter is equally moot for the same reason. Choosing the architecture
before the measurement buys certainty before the real cause is known.

**Recommendation: run the experiment in §5 first. Ship A in the meantime** — it's cheap, it's already
the shape of the code's own sibling branch, and it can only help, never hurt, regardless of which of
(a)/(b) the experiment finds. **B vs. C is the owner's call once the data names which failure is
actually happening**, not before. And a practical test falls out of shipping A: **if the rate does not
drop close to zero, that is itself evidence the dominant historical cause was (a), not (b)** — an
observational answer to §5's question without running the instrumented experiment first.

---

## 8. Not done in §§1-7

No code in `../scripts/jobs.py` changed as part of the investigation. It documents the measurement and
the three options; the choice among them — and whether to run the §5 experiment before or instead of
shipping A — is the owner's.

---

## 9. What shipped — A, tightened

**Trigger.** More jobs hit the identical `exit_code: null` / `attempts: []` signature, each with a
clean, complete log showing its real work landed, and a fix was asked for directly.

**What shipped is A, not B or C — but tightened past the grace-window shape §6 costed it as.** Rather
than "wait N seconds, then re-read," `reconcile`'s per-record loop now re-reads the record from disk
**immediately**, unconditionally, before it ever acts on `check_terminal`'s verdict — no delay at all.
This closes the §4 race completely at this one call site rather than merely narrowing it: if the shim's
own final `save_job` landed on disk at any point before `reconcile` gets to this record in the tick —
whether that gap was 5 ms or 30 s — the fresh read sees it and the stale `ended-unknown` verdict is
discarded in favor of whatever the shim actually wrote (`done`, `failed`, or `retry-pending`, each with
its real `exit_code` and populated `attempts[]`). Verified with a deterministic test
(`test_jobs.ReconcileStaleSnapshotRaceTests`) that patches `list_jobs()` to return a stale snapshot
while the real on-disk file already carries the shim's genuine completion — it fails against the
pre-fix code and passes against the fix, so it is not a test that would have passed regardless.

**This is still narrower than B.** B, as costed, protects every writer through `save_job`'s chokepoint
with a general revision counter; what shipped protects only the one call site identified in §4
(`reconcile`'s pid-gone/timed-out verdict). That is a deliberate, proportionate response, not a
re-litigation of the B-vs-C question — a schema change (a revision counter on every record, or a second
exit-stamp file) still needs the decision §7 reserves for the owner, and neither was made here.

**§5's question stays open for the historical records.** Nothing here proves which of case (a) or case
(b) explains the records written before the fix — that would still need the per-attempt append-log or
the observed revision counter §5 describes, run against a live repro. What changed is the shape of the
remaining exposure: case (b) — the shim writes, then gets clobbered — can no longer happen at this call
site, at any staleness window, so every future `ended-unknown` is more likely to be a genuine case (a)
(or the log-contradicts-exit-code kind, unrelated to this race).

**A second, smaller fix rode along:** the completion push and the `--wake` line used to render the SAME
"no exit code" wording for `check_terminal`'s two different `ended-unknown` shapes — a PID that was
never recorded at all (nothing ran) and a PID that was recorded and then genuinely vanished (a process
WAS running). `check_terminal` now tags `unknown_reason.kind` (`"never-started"` /
`"pid-gone-no-stamp"`) and both renderers read it, so the push stops making a benign "this never even
started" read exactly like the alarming "something was running and is now unaccounted for."

---

## 10. The observational answer §7 predicted

After the §9 fix reached the running daemon (it picks up a merged integration branch on its own update
cycle, so records ending before that are not evidence against it), the ledger was re-read over a
following window of more than a week. **Every `ended-unknown` record on disk predated the fix; across
many times as many completed jobs since (`done`, `failed`, `cancelled`, a few still `running`), zero
were `ended-unknown` or `timed-out`.**

**Reading this against §7's own test: the rate dropped to zero, which is the (b)-dominant answer.**
Case (b) — the shim writes its terminal status, then a stale-snapshot `reconcile` pass clobbers it —
was the dominant, and quite possibly the *only*, live mechanism producing the burst; closing it at the
one call site §4 identified appears to have closed the whole recent class, not merely narrowed it. This
says nothing about older records that have since aged out of `state/jobs/` — there is no on-disk
evidence left to attribute them with, and the §5 forensic experiment, if it is ever worth running,
would need a fresh live repro.

**What this measurement does NOT do:** it does not retire the B-vs-C decision (§6/§7) — that question
was never "is there still a bug," it was "should the general defensive mechanism (a revision counter,
or a second exit-stamp file) be built anyway, against a class of bug that has now gone quiet." That
remains the owner's call, unforced by this data, because a rate of zero over a week is evidence the
narrow fix worked, not proof no future writer can ever race `reconcile`'s snapshot again. **No code
changed in this section** — §9's fix is the whole mechanism; this is the follow-up measurement §7 asked
for.
