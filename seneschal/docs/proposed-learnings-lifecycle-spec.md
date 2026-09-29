# Spec — why `proposed-learnings.md` only ever grows

**Status:** `BUILT` — `learnings.py retire` moves a closed, stamped, 30-day-old row into a sibling
archive file; `close` stamps a disposition and a closed-at date; `restamp` backfills rows closed
before the stamp existed.

## 0. The question

`../references/proposed-learnings.md` is the queue Dream proposes improvements into. It should shrink
once a proposal is decided — yet in practice it only grows. Is a step of the process missing?

## 1. What already exists

The file's own **"## The flow"** section states three steps: Dream proposes (`../modes/dream.md` step
3) → the owner decides → *approve* → the assistant applies it and moves it to **Applied**; *decline* →
it moves to **Declined** (so it isn't re-proposed). A **"## Closing one"** section names the
mechanism: `../scripts/learnings.py close <date> --note "..."`, plus `learnings.py audit` as the
backstop for when the CLI isn't used.

**What `close()` originally did:** find the row by its leading date inside `## Pending`, flip `[ ]` →
`[x]`, and append the `--note` text to the same one-line header — nothing else. It never touched the
multi-line body below the header (Pattern / Proposal / Applies to / Gate check, plus however many
"Recurrence" paragraphs Dream appended while the item was open). It never moved a row anywhere. A
`## Declined` heading sat at the bottom of the file, and **nothing ever wrote to it**: `close()` took
no applied/declined distinction at all, so a decline and an approval were recorded identically, as
free text in `--note`.

**What `audit()` does:** it flags an *open* (`[ ]`) proposal whose `Applies to:` file has changed since
the proposal's date — evidence that a fix may have landed without anyone remembering to close the
row. It is a heuristic aimed at getting items **into** the closed state sooner. It says nothing about
what happens to a row **after** it is closed, and neither did anything else in the tree.

## 2. The shape of the growth

Reading the file's own git history, commit by commit:

- **Almost every commit that touches it grows it.** The handful of historical shrinks are incidental
  (a revert of a same-day proposal, unrelated doc re-points, one fix commit that happened to trim its
  own just-added text) — none is an archival step.
- **At any given moment, most or all rows are already closed.** The "propose → decide → close" half of
  the loop works; the queue is not stuck with un-actioned items.
- **Closing a row is one of the most reliable ways to make the file bigger.** The resolution note (the
  full diagnosis, what shipped, sometimes many "Recurrence" paragraphs accumulated while the item sat
  open) is appended in place and never leaves.

The file grows because **closing a row has never meant removing it.**

## 3. The finding

A step is missing — and it is not a step that exists and silently isn't run; **it has never existed,
in code or in prose.** `close()` was built, correctly, to solve a different real problem (a closed fix
being presented as still open). The file's three-step flow describes *propose → decide →
apply-or-decline*; nothing describes a fourth step — *retire the record once its resolution has had
time to be useful and is no longer news.*

**A second, smaller gap bears on "what happens to a rejection":** the flow promises a decline "moves to
**Declined**," but no code path ever performed that move. The mechanism that keeps a rejection from
being silently dropped **while also not being confused for an applied fix** did not exist. A decline
needs its own disposition recorded at close time, or an archival step has no way to tell "this was
fixed" from "this was turned down" when deciding what a closed row's age should mean.

## 4. What the missing step needs to do

1. **A disposition, recorded at close time, not inferred later.** A free-text `--note` cannot tell an
   archival pass "applied" from "declined" from "already done, no action needed" without parsing
   English. The flow already names the two dispositions that matter (Applied / Declined); `close()` is
   the one place that should stamp one of them, structurally.
2. **A closed-at timestamp, recorded at close time.** Only the proposal's original date survived, and
   a row can sit open for weeks (gaining dated "Recurrence" paragraphs) before it closes. An archival
   rule keyed on "N days since it stopped being news" needs the close date, not the propose date.
3. **A retirement action, run on a cadence, that moves — never deletes — a row whose disposition and
   age both clear a bar.** This repo's convention, everywhere a record's usefulness fades, is *flag and
   keep, never delete*: the RAG index's bitemporal `supersedes`, salience's soft `disposable=2` prune
   (`../references/salience.md`). A retired proposal's full text, including every recurrence
   paragraph, moves somewhere durable, so a later "wait, did we already try this" question is still
   answerable.
4. **Somewhere for a retired row to go that is not this file.** The point of retiring a row is to stop
   `../modes/dream.md` step 3 and step 4d — the two places Dream reads and writes this file every night
   — from paying to load an ever-growing document to append one new proposal and audit a handful of
   open ones. Moving the row to a section further down the *same* file solves nothing.

## 5. The design decisions

1. **Where does a retired row go? A new sibling file**, `references/proposed-learnings-archive.md`,
   append-only, never loaded at grounding (created with a header on first retirement). Rejected: a
   `## Retired` section in the same file (does nothing for the size Dream pays for nightly); deleting
   outright and trusting `git log` (breaks the flag-never-delete convention). The archive is a
   convenience for a human skimming it — the full history is durable in git regardless — and keeps the
   record in the same reviewable-Markdown shape.
2. **How long does a row stay before it is eligible? 30 days after its closed-at stamp**, overridable
   per call (`--min-age-days`). A closed row is still useful for a while — Dream re-reads recent closes
   to avoid re-proposing the same thing, and the owner may want to check a fix landed the way it was
   approved. Too short a floor retires something still being watched; too long defeats the point. 30
   days is the order of magnitude this repo uses for a settled decision kept only as insurance.
3. **Who runs it, and how? `learnings.py retire`, dry-run by default** like `worktree_gc.py --apply`
   (a removal-adjacent operation over a tracked file); wired into `../modes/dream.md` step 4d right
   after `audit`, invoked there with `--apply` because it only ever touches what it can prove is safe —
   a row that is closed, structurally stamped, and old enough — never a guess. A manual-only tool would
   degrade to the same failure this spec is about: a step that exists but nobody remembers to run.
4. **Does a decline need its own verb? No** — `close()` takes a `--declined` flag and stamps the same
   structure an applied close does: `CLOSED <date> (applied|declined)`. A decline and an approval share
   every other property of "this proposal stopped being open," and a second CLI command is a second
   thing to remember.

   **One refinement:** a declined row is **not** retired by default (`--include-declined` to
   override). The flow keeps a decline around specifically so Dream doesn't re-propose the same thing,
   and the archive is deliberately outside what Dream re-reads before writing a new proposal — retiring
   a declined row on the standard clock would quietly reopen the door `## Declined` exists to keep
   shut. It also means a decline closes in place with its stamp rather than relocating to a
   `## Declined` heading; if one is ever retired (explicit override), the archive entry's disposition
   tag is what preserves "this was declined."

## 6. What shipped

- **`learnings.py close <date> [--declined] --note "..."`** — ticks the row and stamps
  `CLOSED <today> (<disposition>)`.
- **`learnings.py retire [--min-age-days N] [--include-declined] [--apply]`** — moves every eligible
  closed, stamped row out of `proposed-learnings.md` and into the archive. Dry-run unless `--apply`.
  On apply it verifies byte-for-byte, via its own recorded offsets, that every retired block's exact
  original text is absent from the shrunk file and present verbatim in the archive — a block that
  would otherwise land nowhere is refused, never lost.
- **`learnings.py restamp`** — a one-time backfill primitive giving a row closed *before* the
  structured stamp existed the same `CLOSED <date> (<disposition>)` marker, with the same
  refuse-rather-than-guess posture as `close`/`retire` (refuses on an open row, an already-stamped row,
  or an ambiguous same-date collision without `--match`).

**Migrating an existing file:** run `restamp` on each closed row whose disposition and closed-at date
are unambiguous from its own prose, then `retire` (dry-run first, against a scratch copy if in doubt).
A row whose resolution mixes an applied part and a declined part cannot honestly carry a single
disposition — leave it unstamped for the owner to split into two rows or decide one disposition for,
rather than guessing. An unstamped row is never retired.
