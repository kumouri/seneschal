# Budget headroom — a time-denominated ratchet, computed in code

**Status:** `PARTIAL(phase 0's computation, report and --enforce-headroom validator BUILT in check_context_budget.py; the raise generator is not shipped in this repo; the byte cap itself stays report-only; further phases archived)` —
`seneschal/scripts/check_context_budget.py` computes every budgeted artifact's headroom
(`headroom_bytes`), reports it on every run, and CI blocks on a NEW `raises[]` entry whose `to` does
not equal the computed value (`--enforce-headroom`, `headroom_violations`). The generator half (§7) —
a `--raise` command that writes the correct entry for you — lives in the private tree this framework
was distilled from and is **not** shipped here; until it is, compute the value by running the checker
(its report prints the exact `to` a raise would need) and copy it verbatim.

**Two design decisions govern this document**, and both are the owner's rather than the
implementer's:

1. **The headroom rule is its own spec**, not an appendix to the byte-cap enforcement work it was
   first bolted onto — it answers a different question and ships on a different gate.
2. **Raising must be code, not a prose rule.** A raise must not be able to go up by the wrong amount,
   so the number is computed and validated, never typed by judgement.

**Siblings.** The context-budget checker (`seneschal/scripts/check_context_budget.py`, budgets in
`seneschal/context-budget.json`) owns the byte cap and its `--enforce` flag; that flip is gated
separately (§6) and is not touched here.

---

## 1. The rule

An artifact's budget headroom is **approximately four weeks of that artifact's own trailing growth
rate, with a floor of about 2k tokens.** Denominated in TIME, not bytes — which is what makes one rule
hold across a 40x size range, and what makes a trip mean what it was meant to mean: *"if it gets bigger
than this, we should look at what is really important in it"* — a real reading task worth doing about
monthly, not twice a week. The floor exists so a small stable file is not hair-triggered by one
paragraph.

What it replaces: a cap set to whatever size the file was when the check was introduced, and then only
ever raised to the file's new size, never above it. A cap that always equals the current size carries
zero headroom, trips on any growth, and turns every raise into paperwork that records what already
happened rather than constraining anything.

---

## 2. The bytes/token correction

Measured differentially against a real model (the CLI's own reported usage: a baseline prompt, then the
same prompt with one file appended; the delta is that file's tokens), this tree's Markdown runs at
**≈ 2.59 bytes/token, not the 4:1 the checker used to print.** At 4:1 a "2k-token floor" is ~8 KB; at
the measured rate it is **~5,180 B**.

The fix is display-only for every other figure: nothing in this repo decides on the token estimate, and
no violation is measured in tokens — the byte cap compares bytes, and the headroom arithmetic is
computed in bytes and only converted to a token label for the floor's human-readable form.
`BYTES_PER_TOKEN = 2.59` is the one place the conversion is defined, and `HEADROOM_FLOOR_BYTES` derives
from it rather than being a second, driftable literal.

---

## 3. Computing trailing growth

### 3.1 The two-endpoint measurement

Walk the artifact's commit history (first-parent, so a merge's *resulting* size is what is measured)
within a lookback window, and take the **first and last usable points** — never a regression over every
point, which would give intermediate refactors equal weight with the endpoints that define the rate.
`rate_per_day = (end_size - start_size) / span_days`.

### 3.2 Why time, not bytes

Budgeted artifacts span a ~40x size range and a far wider growth-rate range: a router file that grows
kilobytes a day sits beside a persona file that barely moves. **Any design that quietly reintroduces a
flat byte constant has failed** — a constant loud enough to matter on the fastest grower is deafening
on the slow ones, and one quiet enough to leave the small files alone is inaudible on the big ones.

### 3.3 The step-change rule

A deliberate trim (a "router diet") or a big rewrite resizes an artifact discontinuously. A trailing
window spanning it measures a **refactor**, not growth.

**The rule: the LATEST single-commit jump exceeding `HEADROOM_STEP_RATIO` (0.4 — a 40%+ change in one
commit) anywhere in the walked history is a reset.** Only history from that commit forward is used. It
is symmetric — a file that quadruples in one commit is as much a refactor as one that halves — because
a trailing rate describes organic *growth*, never *edits*.

**Minimum history, and the visible fallback.** Post-reset history shorter than
`HEADROOM_MIN_HISTORY_DAYS` (3 days) is not trusted — two points a day apart describe noise with a
slope. Below that bar `headroom_bytes` returns the **FLOOR**, and the `meta` dict names why:
`source: "insufficient-history"` and `reset_at: <date>`, so the report can say *"floored — a reset was
detected at <date>, too little history since"* rather than silently producing a number.

**Never extrapolate a fabricated rate** from whatever two post-reset points happen to say. A floor that
says why it floored is a better artifact than a number that looks precise and is manufactured from two
days of data.

### 3.4 Acceleration is a known, accepted underestimate

A trailing rate under-provisions headroom for a series that is still speeding up; that is a property of
trailing measurement, not a bug. The mitigation is that headroom is **never cached** — every raise
recomputes the rate fresh, from history as of the raise's own date — so with raises roughly monthly
the underestimate is bounded by one month of acceleration rather than compounded. A second-order fit
was rejected: it adds a free parameter to a rule whose virtue is being simple enough for CI to recompute
byte-for-byte.

### 3.5 State machine

| Condition | `source` | Headroom |
|---|---|---|
| Fewer than 2 measurable commits in the lookback window | `no-history` | FLOOR |
| A reset leaves fewer than 2 usable points, or less than `HEADROOM_MIN_HISTORY_DAYS` of post-reset span | `insufficient-history` | FLOOR |
| Positive post-reset rate, `rate × 28 < FLOOR` | `trailing-rate` (`floor_applied: true`) | FLOOR |
| Positive post-reset rate, `rate × 28 ≥ FLOOR` | `trailing-rate` (`floor_applied: false`) | `round(rate × 28)` |
| Flat or shrinking post-reset rate | `non-positive-rate` | FLOOR |

Every row is visible in the returned `meta` — a caller never has to re-derive *why* a number is what it
is.

---

## 4. Artifacts outside the default budget set

Reference docs (`seneschal/docs/*.md`, `seneschal/references/*.md`) are unbudgeted by default. If an
opt-in list for them is ever built, nothing here changes: `headroom_bytes(root, artifact)` takes only an
artifact key and a history to walk, and does not care which tier declared the artifact. The opt-in only
has to add the artifact's entry to `context-budget.json`.

---

## 5. What phase 0 reports, unconditionally

`check()` computes, for **every** budgeted artifact on every run, the headroom byte count, the
artifact's own trailing-growth `meta`, and a `headroom_budget` (`actual + headroom` — what the next
raise would set `max_bytes` to). Purely additive: `violations` is unchanged, and existing callers see
the same shape plus new keys. Printed on every run, including green ones:

```
headroom (next raise, if any, would set max_bytes to):
    CLAUDE.md: actual 33,980 B -> headroom budget 39,343 B (+5,363 B, 191.5 B/day over 29.1d)
    seneschal/docs/CLAUDE.md: actual 11,886 B -> headroom budget 17,066 B (+5,180 B, floor — reset detected at <date>, too little history since)
```

---

## 6. Why the byte cap itself is NOT flipped here

Flipping `check_context_budget.py --enforce` for the byte cap is a separate question — *"should today's
numbers block a build"* — and it should wait until any in-flight trims have settled, so the ceiling is
measured against post-trim growth. This spec answers a different question — *"how much headroom should
a raise contain"* — which is about the SHAPE of a `raises[]` entry and is enforceable the moment the
rule exists. Conflating them would either delay this rule or rush the cap flip.

---

## 7. Generate, then validate — both halves are required

A checker that only rejects a wrong number still requires someone to type a number, and typing it by
hand — to whatever the newly-measured size is, because that is the obvious thing to type — is exactly
how the old high-water-mark defect formed.

- **The generator** (a `--raise ARTIFACT --reason "..."` command) reads the artifact's current
  `max_bytes` (`from`), computes `headroom_bytes()` as of today, and writes the `raises[]` entry with
  `to = from + headroom`; the author supplies only `reason`. It refuses on an empty reason, an unknown
  artifact, or a tree already over the computed `to`. **Not shipped in this repo** (see Status).
- **The validator** — `check_context_budget.py --enforce-headroom` — re-derives the identical number for
  any `raises[]` entry a branch itself ADDS (compared against the merge-base config, so pre-existing
  history is exempt) and refuses on a mismatch — exact equality, not a range. CI runs it.

A generator with no check drifts the moment someone edits `context-budget.json` by hand; a check with
no generator is the prose rule wearing a CI badge. It ships enforcing immediately because it only fires
on a raise a branch adds — there is no pre-existing history for it to be red about on day one.

---

## 8. Tests

In `seneschal/scripts/test_check_context_budget.py`:

- `HeadroomViolationTests` — a raise to exactly the measured size (the old ratchet) is refused, an
  arbitrary wrong number is refused, a raise matching the computed value passes, and history already at
  the merge base is exempt.
- `TrailingGrowthTests` — a single commit is no history and floors; a step change resets the window and
  ignores pre-reset history; a step change with too little history after it floors *visibly*; a
  slow-but-positive rate still floors.
- `HeadroomConstantsTests` — the conversion is 2.59, not 4:1, and the floor is 5,180 B.
- `EnforceHeadroomWiringTests` — `--enforce-headroom` is actually wired into CI.

---

## 9. What this spec does not answer

- **An escalation rail** for an artifact that is raised unusually often — nothing raises automatically,
  so there is no auto-raise loop to escalate out of.
- **The opt-in list for reference docs** (§4) — only the promise that the arithmetic works unchanged.
- **The byte-cap `--enforce` flip** (§6).
- **A single-raise cap** (e.g. +10% per raise so growth must be argued repeatedly) — deferred until
  there is more measurement.

## 10. What could not be verified

- **Whether 28 days suits every artifact** or is only a reasonable default — "approximately four weeks"
  is taken literally as a fixed constant; no per-artifact window was measured.
- **The 40% step threshold and 3-day minimum are judgment calls**, chosen to classify known trims
  correctly and to require enough points for a rate; no sweep over alternatives was run.
- **Performance and truncation at a much larger commit count** — `HEADROOM_LOOKBACK_DAYS` and
  `HEADROOM_MAX_COMMITS` (60) keep the report a handful of git calls per artifact; whether the cap ever
  truncates a real artifact's window in a way that changes its classification was not exhaustively
  checked.
