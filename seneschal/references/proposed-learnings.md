# Proposed learnings (the held queue)

How the assistant *learns from the day* without changing its own behavior behind the owner's back.
**Dream mode** (nightly) watches for repeated patterns and writes **proposals** here; each one is
**ask-high** — it becomes policy only when the owner approves it. This is the gated middle path: the
assistant gets sharper over time, but every behavior change is the owner's call and is auditable.

## The flow

1. **Dream proposes.** When the assistant notices a repeated pattern worth encoding — *"you decline
   every recruiter invite," "you archive the X newsletter every time," "you always ask me to remind
   you about the same thing at 9"* — it appends a proposal under **Pending** and surfaces it to the
   owner (Telegram / the store / the next Brief). It does **not** act on it.
2. **The owner rules.** Approve → the assistant applies it and closes the row `applied`. Decline → the
   assistant closes the row `declined` (so the same thing doesn't keep getting re-proposed) — no
   separate move to a `## Declined` heading happens at decline time; see "Closing one" below for why.
3. **The assistant applies (on approval only).** It makes the concrete change — edit
   `autonomy-config.json` (e.g. graduate an action ask-high → act-low) and/or the relevant reference
   (a noise rule in `comms-mapping.md`, a default in `briefing.md`) and/or the persona — **and logs
   it** in the `autonomy-policy.md` graduation log with the date and trust basis. An outward-facing
   action (sending, accepting/declining invites, posting) only ever graduates by the owner's explicit
   decision (see the graduation criteria in `autonomy-policy.md`).
4. **A closed row eventually retires.** Once a row is closed **and** stamped **and** at least 30 days
   old, Dream's nightly `retire` step moves it out of this file into
   `proposed-learnings-archive.md` — a sibling file (created on first use), never loaded at grounding,
   so this file stops only ever growing. Nothing is deleted; see "Retiring one," below.

## Closing one

**Applying a fix and closing its row are one action, not two.** Use the CLI — it refuses on an unknown
or already-closed date rather than doing nothing quietly, so a script applying a fix learns that its
bookkeeping failed. Closing also stamps a structured **disposition** (`applied`/`declined`) and a
**closed-at** date on the row (`— CLOSED <YYYY-MM-DD> (<disposition>)` appended to the title line) —
the two things a later retirement pass needs and cannot recover reliably from free-text prose:

```bash
python ../scripts/learnings.py close 2026-07-17 --note "applied in <commit>"
python ../scripts/learnings.py close 2026-07-18 --declined --note "no clean fix path; see row"
python ../scripts/learnings.py audit    # open items whose Applies-to file has since changed
python ../scripts/learnings.py list
```

**Why a CLI rather than "remember to tick the box".** A row whose fix has landed but which nobody
closed gets re-raised by Dream every night — recommending work that is already done, and reaching the
owner as an open item when it isn't one. Editing this file only ever happens when whoever applied the
fix is already in here; from a Telegram turn, "apply the fix" and "edit a tracked Markdown file" are
unrelated actions. Dream runs `audit` at step 4d as the backstop for when the CLI isn't used. `audit`
is a **heuristic and reports, never edits**: a file named in `Applies to:` having changed since the
proposal was raised is a reason to *look*, not a verdict.

## Retiring one

**Closing a row never shrinks this file on its own — it only grows it** (a close appends a note; see
`../docs/proposed-learnings-lifecycle-spec.md` for the measured history). `learnings.py retire` is the
missing step: it moves a row that is closed, stamped, and at least 30 days old (default;
`--min-age-days` to change) into `proposed-learnings-archive.md`, verbatim, never deleting it. Dry-run
by default; Dream runs it with `--apply` at step 4d, right after `audit`:

```bash
python ../scripts/learnings.py retire            # dry run — what WOULD move
python ../scripts/learnings.py retire --apply     # actually move it
```

**Only a row carrying the structured `CLOSED <date> (<disposition>)` stamp is ever touched** — an
older row closed before this stamp existed is left exactly alone rather than guessed at from its
free-text note. `learnings.py restamp` backfills that stamp onto a historical row, once, and itself
refuses on anything ambiguous (an open row, an already-stamped row, a same-date collision with no
`--match` to disambiguate):

```bash
python ../scripts/learnings.py restamp 2026-07-14 --closed-at 2026-07-15 --disposition applied
```

**A declined row is never auto-retired** (`--include-declined` to override) — the whole point of
keeping one around is so Dream doesn't re-propose the same thing, and the archive is deliberately
outside what Dream re-reads before writing a new proposal. A row that mixes an applied half with a
declined half has no single disposition: leave it open and unstamped for the owner to split or rule
on, rather than guessing.

## Proposal format

```
- [ ] <YYYY-MM-DD> — <short title>
  Pattern: <what the assistant observed, with how many times / over what span>
  Proposal: <the specific behavior change being requested>
  Applies to: <autonomy-config.json | comms-mapping.md | briefing.md | persona | …>
  Gate check: <which graduation criteria it clears / why it's safe — or why it needs a human call>
```

`Applies to:` is load-bearing, not decoration: it names the file(s) the change would touch, and it is
what `learnings.py audit` checks for changes since the row was raised. Name real repo paths.

A closed row keeps its place and its body; only the title line changes:

```
- [x] <YYYY-MM-DD> — <short title> — CLOSED <YYYY-MM-DD> (applied)
  <the original Pattern / Proposal / Applies to / Gate check lines, unchanged>
  <the --note text, appended>
```

**Worked example — a salience prune recommendation** (shape only; a real one is drafted by
`scripts/salience_rollup.py --propose` and lands here via Dream's PR **only after** the evidence window
is met — ≥30 days / ≥50 tagged docs — and never for a θ_protect-shielded category; see `salience.md`):

```
- [ ] 2026-08-15 — Salience: `ephemeral.ack` predictions look safe to soft-prune (gated)
  Pattern: 61 disposable-tagged docs in `ephemeral.ack` accrued 2 recall hits over 34 days
  (mean 0.03/doc; no protecting forgetting-event). The disposability prediction held.
  Proposal: mark this category's tagged docs older than N days disposable=2 (approved-forgotten —
  SOFT prune: excluded from answers, fully reversible, still counted for un-forget evidence).
  No hard deletion; that would be a separate, later gate.
  Applies to: state/rag-index.sqlite chunk rows (via a small marking script) + references/salience.md.
  Gate check: internal-only, reversible by flipping the column back, no outbound; θ_protect and
  identity.core ineligibility both held over the window. Needs the owner's explicit approval + their
  choice of the age threshold N.
```

## Pending

*(nothing yet)*

## Applied
_(Historical heading. Closed rows now stay in place under **Pending** with their `CLOSED … (applied)`
stamp until `retire` moves them to the archive. Mirror every applied change into
`autonomy-policy.md`'s graduation log.)_

*(nothing yet)*

## Declined
_(Historical heading. Declined rows stay in place under **Pending** with their `CLOSED … (declined)`
stamp, and are never auto-retired — kept so the assistant doesn't re-propose them.)_

*(nothing yet)*
