# State durability — 0-byte losses of `state/` files, and the four layers that answer them

**Status:** `PARTIAL(the atomic writer, its empty-write refusal, check #4, the rotation and the incident record)` —
the prevention hook, `health.db`'s hand-entered-row backup and an off-box copy are named in §8 and
unbuilt (so is a shrink guard, deliberately).

**Scope:** losing a `state/` file to a bad write, and having nothing to restore it from. Not the
confidently-wrong-recall failure and not the countermeasure-binding failure — but §7 below is a direct
application of the second (*a countermeasure that cannot refuse does not bind*), because the first
countermeasure for this failure was written, was correct, and did not bind.

**Vocabulary note, for the grep that brings someone here.** This document is about **data loss**, a
**truncated file**, a **0-byte file**, `carry-over.md` **empty**, **losing state**, **no backup**,
`UnicodeEncodeError`, **surrogates not allowed**, **atomic write**, **os.replace**, **CRLF**, and
**how do I get the file back**.

---

## 1. The incident classes

The record behind this document is four losses of a `state/` file to exactly 0 bytes, across two
files, three of them `carry-over.md`. They fall into two mechanisms, and the difference between the
two mechanisms is the design.

### 1.1 Class A — truncate-then-raise

The assistant prepended an entry to `seneschal/state/carry-over.md` with a Python script, composed in a
turn, that did `io.open(PATH, "w")` and then wrote.

**`w` truncates on open.** The file was emptied before the first byte of new content was produced.
The write then raised:

```
UnicodeEncodeError: 'utf-8' codec can't encode characters in position …: surrogates not allowed
```

— an emoji that had reached a source literal as its **UTF-16 surrogate pair** (two `\uD8xx\uDCxx`
escapes) where a single `\U0001XXXX` was meant. The truncate had already happened. The content never
arrived.

**`carry-over.md` was left at 0 bytes — several hundred lines destroyed.**

**It had happened before, to the same file, about a month earlier**, mid-Dream: a Python one-liner
wrote it with `open(path, "w")`, and the write raised `UnicodeEncodeError` on a non-ASCII character
under Windows's non-UTF-8 default encoding. Same shape: truncate first, fail second. That one was
reconstructed from a session transcript captured more than an hour earlier, with a
residual-uncertainty window that was never closed.

That first incident is why `seneschal/scripts/memory_write.py` exists. Dream proposed it; the owner
approved it. **It was built, it was correct, and on the second occurrence it was not used** — the write
came from a script composed in a turn, and nothing made the helper binding. §7.

### 1.2 What made recovery possible, and it was luck

A **Claude Code session transcript** (the per-project `.jsonl` files under the user's
`~/.claude/projects/` directory) held a full read of the file taken about ten minutes before the loss.
The file was restored from it.

**Session transcripts are an accidental backup of anything a session has read.** Nobody had written
that down; it worked twice by chance. It is written down here because it is genuinely useful, and
because its limit has to be stated in the same breath:

> **It recovers the file as of the last time some session read it — not as of the moment before the
> loss.** Any edit between that read and the loss is permanently gone, and nothing can say what was
> in it.

Three further limits, so nobody plans around this:

* **It only holds what was READ.** A file no session has opened this month is recoverable only as of
  whenever one last did, if ever.
* **A `Read` with `offset`/`limit` captures a slice.** A partial read restores a partial file, and
  the transcript does not announce which.
* **It is outside this repository and outside anything a PR can protect** — a different retention
  policy, a `/clear`, or a machine rebuild takes it with no notice.

### 1.3 Class B — a complete write of nothing, THROUGH the helper

Two more losses followed within hours of the fix being opened as a PR.

The first was `carry-over.md` at 0 bytes again, and it is recorded with what is *not* established:
**whether it truncated restored content or wrote empty onto an already-empty file.** Nothing logs the
write itself. It is left open rather than assumed; the byte count is 0 either way.

The second was **`state/context-digest.md`, ~19 kB → 0** (a generated digest file, since retired —
see §6.1). And this one is a different failure from the three above it, which is why it gets its own
layer in §3.3:

> **It did not use `open(p, "w")`. It used `memory_write.py`'s CLI, correctly, and piped it an EMPTY
> STDIN** — because the staging step that was supposed to produce the content had already failed on
> an assertion and the shell ran the next statement anyway.

The write was atomic. The temp file was written, flushed and fsynced. `os.replace` landed. **Zero
bytes went faithfully over ~19 kB and the caller saw exit 0.**

**`memory_write.py` prevents a PARTIAL write. It did not prevent a COMPLETE WRITE OF NOTHING** — and
from the outside those two are indistinguishable: no exception, no leftover `.tmp`, no signal at all.

Two fixes were offered to the owner for it: this guard, and *"use a script file with `set -e` next
time."* The owner's answer, in substance: **only one of those can actually be fixed** — a habit cannot
be enforced, a helper that refuses can — so queue the job to fix that one.

That is right, and §7 is the measurement behind why: **54 written countermeasures, 17 recurrences, 0
clean.** A resolution to be more careful is one more entry on that list. §3.3 is the one that can
refuse.

### 1.4 The asymmetry that is the actual bug

At the moment of the second loss, `seneschal/state/` held:

| File | Backups on disk |
|---|---|
| `reminders.json` | two `reminders.backup-YYYYMMDD-HHMM.json` copies, one and two days old |
| `carry-over.md` | one `carry-over.backup-YYYYMMDD-HHMM.md` — **a month stale** |

Two irreplaceable local-first files, one protected and one not, **and nothing had decided that.**

The reminders copies were not produced by code. `git grep` found no writer for them anywhere in the
tree; they were made **by hand, by Dream, on the two nights Dream happened to make them**, and the
run log records it in prose — a step that pruned `reminders.json` **150 → 70** rows (dropping spent
rows), with *"backup saved"* appended.

So the "precedent to follow" turned out to be a habit, not a mechanism — which is the same finding as
§7, arriving from the other direction.

### 1.5 The cost this document never counted: decisions, re-made by hand

Everything above costs the losses in **bytes** and in **recoveries**. They were later costed in the one
currency no rotation can give back.

The assistant surfaced five pending decisions to the owner. The owner answered all five, and noted
that three of them had already been decided — and that they would decide them again, on the
assumption that the lost `carry-over.md` was why.

**Three of five items presented as open decisions had already been decided.** That is the first
evidence on this record that the truncations destroyed **decisions** rather than notes, and a decision
has no backup: §6's rotation can hand back hundreds of lines of `carry-over.md`, and nothing anywhere
can hand back the fact that the owner already decided something. The only copies were the file and the
owner's memory of having said it.

**The measured number is three re-made decisions in one message.** It is small, and it is the right
unit — every other cost on this page is denominated in bytes, and bytes are precisely the part that is
recoverable.

**One of the three had its answer carried out by the loss itself.** A picker asked whether to clear a
block of stale entries from `carry-over.md`; the owner said yes. The block was **already gone** — a
grep of the remaining file for every distinctive term in it returned zero hits. The question outlived
the content it was asking about, and the truncation had satisfied the decision before it was given.

**One of the three is NOT a casualty of this failure, and saying so is the honest version of the
count.** That picker had been answered in conversation, and the answer shipped to `develop` minutes
later. It was still presented as open two days after it had been implemented, for a reason with nothing
to do with `state/`: a picker record is settled only by a tap, and the owner had answered in words.
That class is `picker-state-marking-spec.md` §10.4. **So the three re-made decisions are real and the
tap cost is real, and they are not all one mechanism** — at least one settled decision came back to the
queue by a route this document does not own and cannot close.

---

## 2. The audit (check #1 of the brief)

`seneschal/scripts/check_state_writes.py` classifies every write to a `state/`-derived path in tracked
Python. Measured at this change's merge-base:

| | Sites |
|---|---|
| **write-temp-then-`os.replace`** (correct) | **29** |
| **in-place truncating writes** | **15** |

The 15, by tree: **8** in the shipped `archons/proteus/` example's `tools/`, **7** in
`seneschal/scripts/`. Every one is fixed in this change except the single allowlisted survivor in §5.

**The repo already had an atomic-write helper, and this is why the callers did not use it.** It is
`seneschal/scripts/memory_write.py` — `write_text` / `append_text` / `prepend_text` plus a CLI — with
`seneschal/scripts/stateio.py` as its JSON-shaped sibling (`write_text_atomic` / `write_json_atomic`
plus the append/tail/prune jsonl family, carrying the same empty-payload refusal). No third one was
added; the fix routes callers to them. The reasons they were not using it, in order of how many sites
each accounts for:

1. **Most of them predate it.** `memory_write.py` landed after the `state/`-writing modules, and each
   of those had independently grown its own build-then-`os.replace` writer — which is why the
   29-vs-15 split is as healthy as it is. Those 29 are not violations and were left alone; a
   migration of correct code would be churn with a real regression risk and no durability gain.
2. **It is text-only, and most of the 15 write JSON.** There was no `write_json`, so a caller with a
   dict had to reach for `json.dump(obj, open(p, "w"))` or hand-roll the temp file.
   `archons/proteus/tools/proteus_paths.py` now carries a `write_json` that delegates to
   `memory_write`; `seneschal/scripts` callers pass `json.dumps(...)` or use `stateio.write_json_atomic`.
3. **`archons/proteus/tools/` is a different tree** with its own discovery root and its own paths
   module, and nothing there imported from `seneschal/scripts` at module scope. Its 8 sites are the
   largest single cause, and they are now routed through one re-export in `proteus_paths.py` rather
   than eight `sys.path` bootstraps.
4. **Nothing checked.** Which is §7.

---

## 3. The helper (check #2)

`memory_write.write_text` already wrote to a sibling temp file, flushed, `os.fsync`'d and
`os.replace`'d. Two things were added, and a third followed the class-B loss.

### 3.1 Line endings are preserved, and it was actively wrong before

**Measured on a live daemon's `state/`:**

| File | CRLF endings | LF-only endings |
|---|---|---|
| `carry-over.md` | **825** | 0 |
| `run-log.md` | 0 | **6,299** |
| `context-digest.md` | 0 | 209 |
| `reminders-id-cache.md` | 0 | 69 |

`state/` is **mixed**. Two memory files in one directory, written through the same helper, with
opposite line endings.

`prepend_text` read the old content in universal-newline mode (`\r\n` → `\n`) and wrote it back with
`newline=""` (no translation). Run against a copy of a live `carry-over.md`, that **converted all 825
endings to LF** — a 72 kB whole-file rewrite in which the one line actually added is invisible.

So the target's existing endings are now detected from its **bytes** and matched. A file with no line
ending yet — new, or empty — is written **verbatim**: the honest answer to *"what did this file use?"*
is *"nothing yet"*, and guessing is how a directory becomes mixed in the first place.

A UTF-8 BOM already on the target is preserved; `append_text` never uses `utf-8-sig`, which would
give a brand-new file a BOM nobody chose. Nothing re-encodes existing content — `prepend_text` raises
on a `UnicodeDecodeError` rather than writing back a file it misread.

### 3.2 The surrogate error names the cause and says the file survived

The class-A message was `surrogates not allowed`, which does not say where the surrogate came from or
whether the target lived. It now reads:

> `… -- a lone surrogate in the text (an emoji pasted as a UTF-16 pair rather than written as
> \U0001XXXX?). <path> was NOT modified.`

The temp file is unlinked on that path. **The atomicity is the fix; the message is so the first
minute after it fires is spent on the right question.**

### 3.3 `write_text` refuses zero bytes over a file that holds bytes

Added for §1.3's class-B loss — **the only one of the four that went through this helper rather than
around it.** `write_text` now raises `EmptyWriteRefused` and writes nothing when the payload is empty
*and* the target exists *and* the target is non-empty:

```
memory_write: REFUSED to write 0 bytes over …/context-digest.md, which currently holds 19,336 bytes.
The file was NOT modified. An empty payload is almost always a producer that failed upstream -- …
If you really do mean to empty it, pass --allow-empty (CLI) or allow_empty=True (write_text).
```

`stateio.write_text_atomic` carries the same refusal under the same flag. `write_json_atomic` never
needs it: a JSON-encoded `{}` / `[]` / `null` is never an empty *string*, so the refusal never fires on
legitimate empty JSON.

Three shapes are deliberately **not** refused, because none of them destroys anything: a zero-byte
write to a **non-existent** path (creating an empty file), a zero-byte write to an **already-empty**
file (nothing to lose — and refusing it would fire on the second run of a legitimate drain), and an
explicit `--allow-empty` / `allow_empty=True`.

**The escape hatch is not theoretical.** `archons/proteus/tools/promote_intel.py`'s `prune_landed`
empties `state/company-intel-pending.jsonl` whenever every queued proposal has landed in the tracked
ledger — the **success** case, not a failed producer — and it passes the flag. It is the same
distinction §5's allowlist draws for `cockpit_pipe.drain_inbox`, arriving from the other direction:
deliberately emptying a file is a real operation, and a guard that breaks it is a guard somebody
removes.

**Zero bytes only. There is no "suspicious shrink" heuristic and this is a decision, not an
oversight.** Zero-over-content is unambiguous; *"it got 90% smaller"* is a judgment call. All four
losses on the record ended at exactly 0 bytes, so the narrow guard covers every case there is — while a
size-ratio rule would fire on a legitimate prune (§1.4's own evidence: `reminders.json` **150 → 70
rows** in one Dream step) and a check that calls the correct operation a bug is a check that gets
switched off. **A shrink guard is recommended-and-unbuilt in §8, and the owner's to decide.**

**`append` and `prepend` are not guarded, and the reason is arithmetic rather than appetite.** They
are additive: an empty append writes no bytes to a file it never truncates, and an empty prepend
writes `"" + old`, which *is* `old`. Neither can lose content, so a refusal there would block an
operation with no failure mode. What an empty payload *does* still mean is the same failed producer —
so the **CLI** warns on stderr and exits 0, while the library functions stay silent, because their
callers are the fail-open `state/` writers and a warning printed from the reminder path costs more
than it buys.

---

## 4. The gate — CI check #4

`check_state_writes.py`, in the shape of `check_context_pointers.py --enforce`, which already blocks
the build. It runs in CI's `python` job (`.github/workflows/ci.yml`, the context-checks step) and
**fails on any un-allowlisted in-place write to a `state/` path in tracked Python.**

The recogniser, its four exclusions and the two recall rules are documented in the module's own
docstring rather than restated here. The one number worth carrying: **with the staging exclusion off
it reported 35 findings of which 7 were real — 20% precision — and 28 of the false ones were the exact
build-then-`os.replace` pattern the check exists to promote.** A gate that calls the fix a bug is a
gate somebody disables, so that exclusion is the engineering content of the check.

### 4.1 The limit, stated plainly, because it is large

**The class-A losses were caused by code this check cannot see.** One write came from an ad-hoc script
composed in a turn and never committed; the other from a one-liner. This gates *tracked Python*.

What it actually buys: the tree's own machinery cannot regress, and the rule now lives somewhere that
runs. **It is not a claim that `state/` is safe from a turn.** The layer that addresses a prompt-side
writer is `memory_write.py`'s CLI — so a skill has something to call instead of improvising — and the
layer that bounds the damage when neither works is §6's rotation.

**And the layer this check cannot be, by construction, is §3.3.** Everything here is about a write
that goes *around* the helper. The class-B loss went *through* it: correct call, correct atomicity,
empty payload. A gate that forces every caller into `memory_write.py` is worth nothing on that path
unless the helper itself refuses the destructive case — which is why the two ship together and why
§3.3 is not a nicety bolted onto this one. **Four layers, none sufficient alone**, and §7's table
says what each can and cannot refuse.

A fifth is available and **not built here**: a `PreToolUse` hook refusing a Bash command that
truncates a `state/` path, in the shape of `seneschal/scripts/bash_path_guard.py`. It is the only layer
that would have stopped the class-A incidents. It is not in this change because a hook fires in every
session on the machine, its false-positive rate is unmeasured, and `bash_path_guard.py`'s own rule is
that the measurement is the only thing licensing the rule. **Recommended, unbuilt, and the owner's to
decide** — registration is host-side (the user's `~/.claude/settings.json`) and no PR can install it.

---

## 5. The allowlist

`seneschal/state-write-allowlist.json`, `reason` required per entry and enforced at load time. One entry:

* **`cockpit_pipe.drain_inbox`** — a deliberate truncate-to-**empty** under a lock, after the content
  has been read into memory. It writes no bytes, so there is nothing that can fail to encode, which
  is the failure mode this check is for. The real hazard there is different and pre-existing (a crash
  between the truncate and the caller enqueueing drops the items) and atomicity cannot close it.

---

## 6. Backup coverage (check #4 of the brief) — the inventory

**The selection rule: a file is rotated iff (a) it is not cheaply derivable from the store, git, or a
re-run, AND (b) it is rewritten WHOLESALE rather than appended to.**

Half (b) is the part worth arguing. The failure this answers is a truncating rewrite, which *cannot
happen* to a file opened `"a"`. An append-only ledger's worst case is one torn line at the tail, which
`seneschal/scripts/transcript_archive.py` already heals. Copying several MB of `turns.jsonl` nightly to
guard against a failure mode its writer does not have would buy nothing and would make the rotation
expensive enough to turn off.

### 6.1 Rotated — `seneschal/scripts/state_backup.py`, 12 files, on the order of a couple of MB a night

A file missing from disk is skipped silently — not every install has every one of these.

| File | Irreplaceable? | Why it is in |
|---|---|---|
| `carry-over.md` | yes | destroyed repeatedly; nothing rebuilds it |
| `run-log.md` | the store is the record | re-deriving thousands of lines of the local mirror is hours |
| `context-digest.md` | regenerable, and **retired** | nothing writes it any more; kept while an upgraded install still has one for `standing_safety.py import-digest` to migrate from |
| `standing-safety.json` | yes | the owner's hand-curated READ FIRST items — one home, no other copy |
| `open-loops.json` | yes | the work-item register: gitignored, so it exists nowhere else, and `loops.py` rewrites it WHOLE on every verb — both halves of the selection rule. Its own projection is `carry-over.md`, the file at the top of this table |
| `reminders.json` | yes | wholesale-rewritten by every reconcile; the hand-made precedent |
| `reminders-id-cache.md` | yes in practice | rebuilding means re-querying the store row by row |
| `acks.json` | a day's worth | losing it re-fires nudges the owner already answered |
| `project-roots.json` | yes | hand-edited config, no seed beyond the example |
| `archive-people.json` | yes | hand-edited person registry for the message archiver |
| `model-config.json` | yes | the model dials; rewritten whole by the cockpit |
| `governor-config.json` | yes | hand-tuned budgets and quotas |

### 6.2 NOT rotated — every candidate the brief named, checked rather than assumed

| File | Irreplaceable? | Rotated? | Why |
|---|---|---|---|
| `presence.db` | **no** | no | `state/README.md`: a regenerable cache. Also pruned to 30 days nightly |
| `health.db` | **mostly no** | **no — KNOWN GAP for hand-entered rows** | Can grow to hundreds of MB. Rows from the phone export re-import (the export is cumulative); **any row no import reproduces — entered by hand — does not.** A nightly copy of the whole db would be GBs a week. The right fix is a sqlite `.backup` of the non-reimportable rows on a weekly cadence — a separate change with its own size argument, deliberately not a line added to `FILES` |
| `turns.jsonl` | **yes** | no | several MB, **append-only**. Rule (b): its writer cannot truncate it |
| `state/transcripts/` | **yes** | no | append-only, keep-everything by decision, torn tails already healed |
| owner-profile material (e.g. `persona/owner-profile.md`) | **yes** | **no — KNOWN GAP** | irreplaceable and swept by nothing, but **hand-edited, not machine-rewritten**. Exposed to a disk loss, not to this bug. The answer is an off-box copy (the chat-turn indexing design already argues that trade), not this rotation |
| session ledger (`state/sessions/`, `state/session-mail/`) | no | no | per-session lifecycle files; a lost one costs one session's registry row |
| job ledger (`state/jobs/`, `state/job-pushes/`) | no | no | swept nightly at 14 days by design; a terminal job's value is its push, which is already delivered |
| `rag-index.sqlite` | no | no | a large regenerable cache; Dream rebuilds it |
| `notion-outbox.sqlite` | no | no | a queue, drained continuously; fail-closed by design (Notion backend only) |
| `telegram-questions.json` | no | no | pending pickers, 7-day self-liquidating; a lost picker is re-askable |
| `telegram-message-map.json` | no | no | `state/README.md`: best-effort context, **not** truth |
| the append-only `*.jsonl` family | mixed | no | rule (b) — `metrics`, `assertions`, `governor-ledger`, `router-log`, `plan-usage`, `seed-log`, `journal-offers`, `interleave-log`, `instructions-loaded`, `session-starts`, `session-distillations`, `merge-ask-log`, `watch-gate`, `suppressed-turns`, `forgetting-events`, `cockpit-audit`, `outbound` |

### 6.3 Retention: 7 copies, and why

The whole set is small — a couple of MB a night — so depth is nearly free and the number is **not** a
storage argument. It is a latency one: every copy past the point where somebody would have *noticed*
buys correlation, not coverage. Every real loss on the record was caught inside one Dream cycle or
within minutes. A week is comfortably past that, and short enough that the directory stays readable by
eye. **The evidence that would change it is a loss found late** — `--keep` takes any number.

An unchanged file is not re-copied: a second identical copy would spend a rotation slot pushing a
*different* day's content off the end of the window.

### 6.4 Where it runs

**Dream step 2g** (`seneschal/scripts/dream_steps.py`), first in step 2 — deliberately **before** the
reconciles, because a backup taken after `reminders.json` has been reconciled backs up the result
rather than the input. It keeps the hand-made precedent's evening slot and its file naming
(`<stem>.backup-YYYYMMDD-HHMM<ext>`, byte-compatible, so any existing hand-made `reminders.backup-*`
files join the rotation instead of sitting beside it) and replaces its mechanism.

It stamps `dream_steps` step `2g` itself, `max_age_days: 2`. That is the difference that matters:
**the hand-made version could stop happening and nothing would say so.** Two days rather than seven
because the window this protects is *"how much can one bad write cost"*, and every night it does not
run is a night of `carry-over.md` with no copy.

---

## 7. Why a helper was not enough, and what this change does differently

An audit of the originating deployment's written countermeasures found **54 written countermeasures,
17 with recurrences, 0 clean, and 0 of 54 able to refuse a factual claim.** This incident is one more
entry, and it is an unusually clean instance of the pattern:

* A file is destroyed by a truncating write (class A).
* A correct atomic helper is built and approved, with a CLI *specifically so a skill can call it
  instead of improvising a one-liner*, and a docstring saying **"the rule, and it is not negotiable for
  anything under `state/`"**.
* The rule is also written as an imperative — `seneschal/scripts/stateio.py`'s docstring opens with
  *"Never `open(path, "w")` a `state/` file."*
* The same file is destroyed by a truncating write about a month later, by a script that did not call
  the helper.

**The helper closed the instance. Nothing closed the class**, because nothing could refuse. So this
change deliberately does not add one more sentence:

| Layer | What it can refuse | What it cannot |
|---|---|---|
| `memory_write.py`'s atomicity | a **partial** write | a complete write of **nothing** — §1.3, class B |
| **`memory_write.py`'s empty-write guard (§3.3)** | **0 bytes over a file that holds bytes**, through the helper | a non-empty payload that is nonetheless wrong |
| **`check_state_writes.py --enforce`** | **a commit** | anything untracked, i.e. three of the four incidents |
| **`state_backup.py`** | nothing — it bounds the loss | preventing it |
| a `PreToolUse` hook (§4.1) | **the write itself** | *unbuilt — the one layer that would have stopped both class-A losses* |

The honest summary is that this change makes a recurrence **survivable**, makes the tracked half
**impossible**, and — since §3.3 — makes the *specific* untracked shape that destroyed
`context-digest.md` impossible too. **A write that goes around the helper entirely is still open**,
and §4.1 is the recommendation for it.

**The class-B loss is also the sharpest available answer to the "be more careful" fix.** The two
candidates on the table were §3.3 and *"use a script file with `set -e`"*. The second is one more
written countermeasure; the table above is what the previous fifty-odd bought. The owner settled it in
one line: only one of the two can actually be fixed.

---

## 8. Open, and the owner's to decide

1. **The `PreToolUse` truncating-write hook** (§4.1) — the only layer that would have prevented the
   class-A incidents. Needs a false-positive measurement first, in `bash_path_guard.py`'s shape.
   **§1.5 is the strongest argument on this page for building it:** what these losses cost in bytes is
   recoverable, and what they cost in the owner's decisions is not.
2. **`health.db`'s hand-entered rows** (§6.2) — a weekly sqlite `.backup` of the rows no import
   reproduces, sized and scheduled separately.
3. **An off-box copy for owner-profile material and `turns.jsonl`** — the two irreplaceable things this
   rotation deliberately does not cover. The chat-turn indexing design already recommends **against**
   one, for privacy reasons that have not changed; this is a pointer to that argument, not a
   reopening of it.
4. **A "suspicious shrink" guard on top of §3.3** — refuse a write that drops a file below some
   fraction of its current size. **Deliberately not built**, and the argument against it is in §3.3:
   zero-over-content is unambiguous and covers all four incidents on the record, while a ratio is a
   judgment call that fires on a legitimate prune. If it is ever built it needs the same thing
   `bash_path_guard.py` needed — **a measured false-positive rate against real `state/` writes
   first**, because a guard that cries wolf on a correct operation is the one that gets removed and
   takes the narrow guard with it. **The owner's to decide.**

## Router entry

**Router status:** **PARTIAL — the writer, its empty-write refusal, CI check #4, the rotation and the
incident record BUILT; the prevention hook, `health.db`'s hand-entered rows, an off-box copy and a
shrink guard named in §8 and unbuilt.** **What it decided:** **Losing a `state/` file to a bad write,
and having nothing to restore it from.** Four 0-byte losses across two files, in two classes. **Class
A:** `open(p, "w")` truncates first, then `UnicodeEncodeError` (once from a **surrogate pair** in a
literal) — the first occurrence is *why `memory_write.py` exists*, and the second shows the
countermeasure-binding pattern cleanly: **the helper was built, was correct, was approved, was written
down as an imperative, and could not refuse.** **Class B went THROUGH the helper:** the CLI, called
correctly, on an **EMPTY STDIN** from a producer that had already failed — **atomicity stops a PARTIAL
write, not a complete write of NOTHING.** §3.3 makes `write_text` (and `stateio.write_text_atomic`)
**refuse** 0 bytes over a non-empty target (`--allow-empty` / `allow_empty=True` to mean it). **Zero
bytes ONLY — a shrink heuristic is §8's, unbuilt, because a ratio fires on a legitimate prune and gets
switched off.** The alternative, *"use `set -e` next time"*, was set aside because a habit cannot be
enforced and a refusing helper can. **The asymmetry is the real bug**: `reminders.json` had rotating
backups and `carry-over.md` a month-stale copy — and no writer existed for the reminders ones, because
Dream made them by hand. **Audit: 29 correct build-then-`os.replace` sites vs 15 in-place**, 14 fixed.
**`state/` is MIXED and a normalising helper would bury every diff** — `prepend_text` was silently
converting CRLF files to LF. **Recovery via session transcripts is written down with its limit: as of
the last read, not as of the loss.** **§4.1 is the honest limit** — the class-A losses came from
untracked ad-hoc code the check cannot see; the layer that would stop them is a `PreToolUse` hook,
recommended, deliberately unbuilt. §6 is the backup inventory: **rotate iff irreplaceable AND rewritten
wholesale** — so `turns.jsonl` and the transcript archive are OUT by rule, and `health.db` is out on
size, both named as gaps rather than assumed.
