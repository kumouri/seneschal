# READ FIRST retirement — Dream needs a rule to retire, not just to add

**Status:** `PARTIAL(the store + CLI BUILT — standing_safety.py / state/standing-safety.json; the
digest itself RETIRED; the §4 classification/compression process and §6's completeness check remain
unbuilt design)`

**Where it stands.** The READ FIRST standing-safety items no longer live in the context digest. They
have their own gitignored store, `state/standing-safety.json`, owned by one script
(`scripts/standing_safety.py` — `add` / `retire` / `list` / `render` / `import-digest`). Every item
carries `source`, `added` and a `retire_when` — a date, `"when <decision> lands"`, or `"standing"`; a
fourth shape is refused, not stored. That `retire_when` field IS the retire path this spec asks for,
and Dream reaches the store only through the CLI (`modes/dream.md` step 1). The daemon's READ FIRST
block is `standing_safety.render()`: a `## READ FIRST — standing safety items (do NOT re-raise cold)`
heading, then one bullet per active item, verbatim, with no metadata appended (the wording of a
standing-safety line is load-bearing).

The digest itself is retired: Dream no longer condenses the day into `state/context-digest.md` (open
loops route to `state/open-loops.json` via `scripts/loops.py`; the day's summary is the Run Log
itself). An install that still has a digest migrates once: Dream runs `standing_safety.py
import-digest` against it exactly once, while the store is empty — the CLI's own refusal on a second
run (no `--force`) is what makes "exactly once" true without depending on a prompt remembering not to
repeat it. On the daemon side the digest read is a **migration shim, not standing behavior**
(`check_read_first_migration`, once per boot): while the store is still empty it logs one loud line,
queues one nudge, and lets the digest cover that boot; once the store has held active items for **two
consecutive boots**, the shim latches permanently off and the digest is never read again on that host.

§4's classification/compression *process* and §6's completeness-check *countermeasure* below remain
design, not code — they were designed to keep the digest growing safely, and retiring the digest is a
different resolution to the same original problem.

---

## 0. Why this gap exists

The READ FIRST injection — a short block of standing-safety instructions injected verbatim into every
cold spawn (`grounding-restructure-spec.md` §4.7) — was an early build, shipped before Dream's step
ledger (`dream_steps.py`) or any byte ratchet existed. An add path was built and a retire path was
not. That is an early-build omission, recorded as context, not a fault.

---

## 1. The problem

Measured against a live digest, the READ FIRST section grew about 2.5× in four weeks and arrived
within a couple of hundred bytes of its cap (`READ_FIRST_MAX_BYTES`, 4,000 B — chosen for ~2.6×
headroom over the section's size when it was set, "so Dream never has to write around it"). Measured
bullet by bullet, the few largest bullets were well over half the section, and in each of them the
safety **instruction** was one clause while the rest of the bullet was the **evidence** for it.

**What truncation does when the cap is crossed, and why that is the wrong failure mode for this
content:** the block builder drops **whole lines from the end** until the kept text plus a truncation
marker fits. The section is a flat bullet list with no ordering by importance, so the bullets at the
bottom lose first — by construction, not by any judgment about which ones matter least. A
standing-safety block truncated by position is worse than one visibly short, because nothing about
*which* items survive is chosen.

---

## 2. The two causes

**Cause 1 — narrative, not instruction.** Each bullet was written to be self-justifying: the
instruction (*"don't raise this"*) is embedded in a paragraph carrying the incident that makes the
instruction non-arbitrary. That is the right shape for a `docs/` spec or a decision record, where the
reader benefits from the *why*. It is the wrong shape for a block that is injected **unconditionally,
verbatim, on every cold spawn** — the *why* is paid for on every single turn, whether or not the turn
ever touches the topic.

**Cause 2 — no retirement path.** Dream was told to **overwrite** the digest with a fresh view of the
day, but nothing said to **remove** a READ FIRST bullet once its reason for existing had passed. In
practice resolution becomes a **one-way conversion**: an open thread becomes a closed one, and the
bullet that used to say "this is in progress" becomes a permanent "do not raise this cold" — it does
not disappear, because disappearing is exactly the thing the bullet exists to prevent for the *live*
case, and nothing distinguishes the live case from the one that has since actually closed. Two kinds
accumulate:

- **Closed threads** — each with its own memory-file record saying so, and each still occupying bytes
  in the injected block specifically so nobody reopens it cold.
- **Self-expiring facts** — "X is away until Friday," "this routine is paused this week" — which are
  simply **wrong** the day after their date, and nothing collects them.

A section that only ever gains entries, on a fixed byte cap, with truncation that drops from the
bottom by position, is a monotonic growth function running against a hard wall.

---

## 3. The three-way split

"Retire" means three different things here, and a single rule that treats every bullet alike will
delete something load-bearing. The split, and what happens to each class:

### (A) SELF-EXPIRING

Carries a date after which the instruction is simply no longer true (someone away until a date, a
routine paused until a date). Safe to **drop outright** the first Dream run after that date — there is
no version of "keep this around, compressed" that makes sense once the fact it asserts has changed.

### (B) RESOLVED-BUT-MUST-NOT-BE-RAISED

The matter is over and the entry exists **precisely so nobody reopens it cold** — a closed thread, a
rejected opportunity, a finished chase. **These cannot simply be dropped.** Dropping one is exactly how
it gets raised cold again — a fresh Dream run, or a fresh session with no access to the memory-file
record behind it, has no way to know the topic is closed rather than simply unaddressed. **Where these
live instead, and in what form, is left open in §7.1.**

### (C) STANDING

Permanently true, not tied to any event or date: a communication rule, a hold on a sensitive topic, an
"always do / never do" about how the owner is addressed. These **stay**, but should carry the
**instruction** and a **pointer** to the fuller case (a memory file, a decision record) rather than
the case file inline. The instruction is what the injection needs on every cold spawn; the evidence
for it is what a memory-file read is for.

---

## 4. What Dream must do each night (the process, unbuilt)

1. **Classify every bullet** — not re-derive its classification from prose each time (§5's tag exists
   so this step is a read, not a judgment call), but confirm every bullet actually carries one of the
   three classes before proceeding.
2. **Drop every class-A bullet whose expiry date has passed.** Outright removal, no compression, no
   residue.
3. **Re-home or compress every class-B bullet.** Never delete outright. Which of "re-home" or
   "compress" it gets, and where a re-homed entry lives, is **open — §7.1.**
4. **Compress every class-C bullet to instruction + pointer.** The narrative content moves to (or
   already lives in) the memory file / decision record the pointer names; the injected bullet keeps
   only the imperative clause.
5. **Never let the write silently exceed the cap.** If the classified-and-compressed result still
   does not fit, that is itself a signal worth surfacing to the owner rather than something Dream
   resolves on its own by picking what to drop.

Written in the same numbered-step shape `modes/dream.md` already uses, but not added to it.

---

## 5. How an entry declares its own class and expiry

Classification must not be re-guessed nightly from prose — a session rediscovering something already
decided is a real, recurring cost. The entry needs to **carry its own class**, decided once, at write
time.

The original strawman was an inline tag at the end of each bullet:

```
- **The houseguest leaves Friday.** Don't raise the visit as ongoing after that. _(class: A, expires: 2026-01-16)_
- **The vendor contract — rejected, closed.** Do not raise cold. _(class: B, see: vendor-contract-closed.md)_
- **Never prescribe generic productivity advice.** _(class: C, see: no-generic-advice.md)_
```

**What shipped is the simpler `retire_when` field on the store** (a date / `"when <decision> lands"` /
`"standing"`), which covers A (date) and C (standing) directly and gives B a gated shape. What is
**not** open: the class must be machine-parseable by whatever nightly step reads it, so classification
is a lookup rather than a judgment call performed fresh every night.

---

## 6. Countermeasure binding — is this code or prose, and would it hold?

**Prose alone would not bind, and the evidence is in the very file under discussion.** Dream's mode
file already carried a prose instruction on this exact section — *keep it short, keep the block under
~4 kB, past which it is truncated* — and the section grew 2.5× under it anyway. A second sentence
asking Dream to also retire entries is the same intervention class: a disposition that requires the
model to self-observe ("has this resolved? has this expired?") on a schedule nothing checks. **The
trigger is an internal state, not an observable act**, and that class of countermeasure does not hold.

**So the countermeasure that would actually bind has to be code-shaped.** Two candidates fit the
repo's proven patterns (annotate a defect mechanically; "a skip must cost something, a stamp"):

- **A classification-completeness check**, run by Dream itself before it finishes writing: every
  outgoing READ FIRST item must carry a parseable class, and an item without one is a defect Dream
  must fix before the write counts as done — not merely note. (The store's `add` refusal of a
  malformed `retire_when` is the write-time half of this, and it is built.)
- **A `dream_steps.py` entry for this step**, the same ledger already used for Dream's other numbered
  steps — "a skip must cost something (a stamp), and the record is written by the script that did the
  work, never by the Dream prompt." A retirement pass Dream silently skipped would show up as a missing
  stamp.

**The structural limit on either candidate:** `state/` is gitignored, so **CI can never see the live
store or digest** — no `check_*.py` can enforce this the way a CI guard enforces a tracked file. The
only enforcement surface is inside Dream's own run, at write time, in code Dream itself calls. That is
a real constraint on the design, not a reason to fall back to prose: the check has to be something
Dream's own step **is**, not something a separate CI job verifies afterward.

**Confidence, if built as designed:** moderate-to-high for the mechanical completeness-and-stamp pair,
because it reuses mechanisms already proven to hold (annotate-not-block; skip-costs-a-stamp). **Low**
for any version that reduces to a sentence in `modes/dream.md` asking Dream to remember to retire
things — that sentence's sibling already existed, and the growth is what happened under it.

---

## 7. Open questions

### 7.1 Where do class-B entries live, if not in the grounding push?

A resolved-but-must-not-be-raised entry cannot be dropped (§3) but also does not need to cost bytes on
**every** cold spawn the way a standing rule does — its whole value is defensive, tripped only if
someone is about to raise the topic. Candidates, none chosen:

- Stay in the injected block, compressed to the shortest safe form (costs bytes forever — the same
  failure mode, an order of magnitude slower).
- Move to a place a turn can look **if it is about to raise the topic** — but that requires the turn
  to think to look, which is the retrieval-is-a-menu problem: a fresh look costs a tool call, and
  free-and-stale wins.
- A dedicated closed-topics register, structurally different from both the store and the memory
  index, with its own retrieval story yet to be designed.

Today every item lives in the one store regardless of class.

### 7.2 Does the byte rail move off the section onto the whole injected block?

`READ_FIRST_MAX_BYTES` caps only the section text. A fixed preface is prepended to it with no cap of
its own, so the string actually substituted into the grounding prompt can exceed a 4,000 B reading of
"the whole injected block" before the section's own cap ever triggers — and that is before any other
grounding slice claims a share. Whether the rail should cover preface + block (+ any future slice) as
one number, or stay as it is with the preface understood as always-on overhead, is open.

### 7.3 What should the ceiling be, once content is compressed?

If §4's compression (instruction + pointer, no inline case file) lands, 4,000 B may be far more
headroom than the compressed form ever needs — or the wrong number entirely if class-B entries end up
staying in the block (§7.1). Setting a new number before either question is answered repeats a known
mistake: *a cap that always equals the current size is a high-water mark, not a limit.* Nothing here
proposes a number.

---

## 8. Deliberately not built

- **A "remember to retire" sentence in `modes/dream.md`.** Dream's step 1 names CLI commands instead
  (`add`, `list --due`, `retire`) — the binding half is the CLI's own refusal and the `list --due`
  read, not prose.
- **§5's class/expiry tag syntax.** Superseded in shape by the simpler `retire_when` field.
- **A change to `READ_FIRST_MAX_BYTES`.** Unchanged — the block builder sources its section from
  `standing_safety.render()`, but the byte cap and truncation marker are shared code, applied
  identically regardless of source.
- **A number for the compressed-content ceiling** (§7.3) — no compression process exists yet, so there
  is nothing to size a ceiling against.
- **A separate home for class-B entries** (§7.1).
- **§6's completeness check inside Dream's write.** Distinct from the migration shim above, which only
  governs when the old digest stops being read.
