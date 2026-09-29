# Observation gates — a scanner for work waiting on evidence

**Status:** `PARTIAL(phase 1 BUILT — the observation/observation-complete statuses, the gate schema,
loops.py's observe/mark_observation_complete, observation_gate.py's scanner, Dream step 2i, the
Brief's lead-in, min_elapsed's since (§2.2); §8's seed table is proposed — attaching a gate to a live
register row is an install-side action, not shipped code)`

## 0. The problem

A work item whose next phase is gated on evidence — *"two weeks AND ≥ 30 classified rows in this
log, whichever lands second"* — goes quiet the moment it is parked. Nothing is broken, nothing
crashes; the gate simply has no watcher. The bar gets met, and nobody notices for weeks — by which
time the collected data may no longer reflect how the system operates, and a fresh observation period
is needed anyway.

Two things follow:

1. **"Waiting on evidence" is a status**, not a mood of some other status — the register must be able
   to say it directly (§1).
2. **Something must look.** A scanner (part of Dream) walks every row in that status, checks whether
   its evidence requirements are met, and flips the ones that are to a "ready to continue" state the
   Brief leads with (§3, §5, §7).

## 1. `observation` — a stored status, not a derived label

The register's stored status vocabulary gains `observation`, deliberately distinct from the two states
it most resembles:

| | `held` | `paused` | `observation` |
|---|---|---|---|
| Who writes it | the assistant | the owner | the assistant or the owner |
| What it means | *"I cannot verify this is still live"* | *"The owner deliberately stopped it"* | *"Waiting for evidence to accumulate"* |
| Bounded? | Yes — a few journal days, a cap, then a picker | No — and must never nag | No fixed bound, but **has an exit condition** |
| Exit condition | A picker resolves it | The owner says resume | **A machine-checkable gate becomes met** |

**`held` is bounded but has no evidence bar** — it exists because the assistant cannot tell if a row
is still live, not because a number needs to reach a threshold. **`paused` has no evidence built in**
— it is deliberately open-ended, the owner's to end. Neither can carry *"resume automatically once N
days have passed and M rows exist in this log"* without overloading a value that means something else,
which is exactly how a partial read happens: a reader sees `held` and reasonably assumes "the
assistant doesn't know," when the truth is "everyone knows exactly what it's waiting for and precisely
how to check."

**`observation-complete` is a second new stored value** — what the gate being met looks like. It is a
**stored value**, not a derived label, for one structural reason: `dormant` can be derived because it
is a pure function of `last_touched` and a threshold — recompute it from data that already exists.
Whether a gate is *met* depends on external evidence (a log file, a job's exit code, elapsed time
against a stamp) that the register itself does not carry continuously; something has to have
**looked**, once, and recorded that it looked. A derived label would mean re-running every gate's
requirements on every render just to answer "is this still true," which is exactly the
live-evaluation posture §2 refuses. Stamping `gate.met_at` once, at the moment the scanner found it
true, is cheaper and gives the Brief something to point at.

> **The alternative, named so it can be revisited:** a derived `ready` flag over an item still
> carrying `status: "observation"`, recomputed at render/query time by re-checking the gate. This
> avoids a second stored value at the cost of re-running every requirement (including a `min_rows`
> jsonl scan) on every read — `render`, `mine`, and the Brief's launch, all several times a day.
> `mark_observation_complete()` and the two statuses are the only places that decision lives; nothing
> else in the design depends on which one wins.

**Neither value narrows anything** — both are additive widenings of the status vocabulary, the same
posture as `in_progress`. Widening is the assistant's (or a builder's) to propose and the owner's to
decide; narrowing is the owner's alone.

## 2. The gate — schema and requirement vocabulary

A gate lives on the item record, under an optional field, `gate`:

```jsonc
{
  "gate": {
    "kind": "observation",
    "started_at": "2026-01-10T09:00:00+00:00",   // stamped by loops.py observe, never by hand
    "requires": [
      {"type": "min_elapsed", "days": 14},
      {"type": "min_rows",
       "source": "seneschal/state/example-log.jsonl",
       "filter": {"kind": "example.event", "typed": true},
       "n": 30}
    ],
    "met_at": null,                              // stamped once, by mark_observation_complete
    "whose_move_on_complete": null               // null -> "both" (the default); an item may override
  }
}
```

**Refusing `observation` without a `requires` list is the whole mechanism** (`loops.observe`,
`_validate_gate_requires`) — the same "the refusal IS the mechanism" idiom the register already uses
for `terminal_state`. A wait with no evidence bar named is `held` with an extra syllable, not a
distinct status, so the schema will not create one.

### 2.1 The five requirement types

Every requirement is checkable from **local state only** — never a live network call, never an LLM
judgment call pretending to be a check:

| `type` | Fields | Met when | Fails open to `None` (unknown) when |
|---|---|---|---|
| `min_elapsed` | `days`, `since` (optional) | `N` days have passed since `since` if given, else the gate's own `started_at` | neither `since` nor `started_at` is readable |
| `min_rows` | `source` (jsonl or sqlite path), `filter` (dict), `n` | ≥ `n` rows in `source` match every key/value in `filter` | `source` doesn't exist or can't be read |
| `file_exists` | `path` | the (repo-root-relative or absolute) path exists | an OS error checking it |
| `job_finished` | `job_id` | `jobs.py`'s own record for that id has left `running`/`retry-pending` (success or not — "finished" means *reached a terminal state*, not *succeeded*) | no record exists for that id |
| `manual` | `note` | **never** — always a human question | always (by construction) |

**A gate is met only when every requirement in it reports `met: True`.** One `None`/`False` holds
the whole gate — the same semantics as `all()` over the list, and the same posture the register's own
`is_dormant`/`needs_reask` already take: an unreadable source is a reason to say "I don't know,"
never a reason to guess "yes."

**`min_rows`'s sqlite door is guarded against SQL injection at the identifier level** — a `table`
name or a `filter` key that isn't a plain alphanumeric identifier is refused to `unknown` rather than
interpolated into SQL text (`observation_gate._safe_identifier`), because sqlite3's parameter binding
cannot parameterize a table or column name and a gate's `requires` list is, structurally,
untrusted-enough input to warrant the check even though nothing hostile is expected to write one.

### 2.2 `min_elapsed`'s `since` — a per-requirement clock start

**The problem it fixes:** `started_at` is stamped by `observe()` as NOW, always. If `min_elapsed`
counted only from `started_at`, a gate attached for an evidence period that had *already* been
running for weeks (the common backfill case — §8) would read "0d elapsed" the instant it was attached
and never flip on the next scan, even though the real bar was long met.

**The fix:** `min_elapsed` accepts an optional `since` — an absolute ISO date or datetime (a naive
value is read on the owner's wall clock, matching every other wall-clock read in this tree). When
present, it is the requirement's OWN clock start; `started_at` is untouched and keeps meaning exactly
one thing — "when this gate was attached" — an audit fact, never backdated itself.
`_validate_gate_requires` refuses a `since` that doesn't parse or that names a future instant, at
`observe()` time, the same way every other requirement field is checked for shape before it can ever
silently fail to evaluate later. `observation_gate.check_requirement` reports which clock it used
(`"from since"` / `"from started_at"`) in its `detail` string, so a `check`/`scan` read never has to
guess which one applied.

**Why a per-requirement field and not an `observe()` flag that backdates the whole gate:** a gate's
`started_at` already has one clear meaning load-bearing elsewhere (`_is_stale`'s staleness read, the
schema's own comment), and a gate can carry more than one time-shaped requirement with different real
start dates. Scoping the backdate to the one requirement that needs it keeps `started_at`
single-meaning and costs nothing for the gates that don't need it (`since` omitted behaves exactly as
before).

## 3. The two mutations, and who owns them

`loops.py` remains the register's **one writer** — the two mutations live there and nowhere else:

- **`observe(item_id, requires, whose_move_on_complete=None, because=None)`** — `status: "observation"`.
  Refuses on a terminal record (same reason `start()` does: an item that is over is not something to
  put on hold for more data) and refuses without a valid `requires` list. Forwards to the Notion
  backend when active (§4).
- **`mark_observation_complete(item_id)`** — `status: "observation" -> "observation-complete"`.
  Refuses on anything but a record actually in `observation` (the one status transition with exactly
  one legal predecessor). Stamps `gate.met_at`, sets `whose_move` to the item's own
  `whose_move_on_complete` or `"both"` (reusing the existing `whose_move` vocabulary —
  `owner`/`assistant`/`external`/`unknown`/`both` — rather than minting a new value), and forwards to
  the Notion backend when active. **Deliberately not on `loops.py`'s own CLI** — the transition it
  performs is a derived fact about a gate, not a thing a human decides directly the way `hold`/`drop`
  are, and it does not call `raise_item()` itself (that is its caller's job, so the status-flip
  question and the "has the owner been told" question stay two separate concerns).

`seneschal/scripts/observation_gate.py` is the **read/decide** half, and it is a separate module
rather than more of `loops.py` for the same reason `cadence_chain.py`/`owi_resurface.py` are already
separate: `loops.py`'s job is "the store, the writer, and the render" — deciding whether a gate's
requirements are *actually met* is a different kind of work, with its own file-format parsers (jsonl,
sqlite) and its own `jobs.py`/`paths.py` dependencies, that does not need to load every time someone
runs `loops.py list`. It **never writes to `open-loops.json` directly** — every mutation it causes
goes through `loops.mark_observation_complete` + `loops.raise_item`, enforced structurally by
`test_loops.py`'s writer pin test (a source scan asserting the two allowed calls and refusing every
other verb name, the same idiom that test file already uses for the register's other callers).

**The scanner (`observation_gate.scan()`) is idempotent, never touches anything but a
`status == "observation"` row, and never closes an item** — a row that reaches `observation-complete`
is no longer `observation`, so re-running the scan against it is structurally a no-op, not a guard
the code has to remember to check.

## 4. Notion projection (Notion backend only)

`register-notion-projection-spec.md`'s mapping table carries two rows for these statuses:

| register `status` | Notion Tasks `Status` | why |
|---|---|---|
| `observation` | `Paused` | Notion has no value meaning "deliberately waiting on evidence" — `Paused` is the closest, and it is equally true that nothing should happen to the row right now |
| `observation-complete` | `Not Started` | Notion has no value meaning "a gate just closed, nobody has picked it up yet" — `Not Started` is the closest (never `In Progress`, which would claim the opposite of "ready to continue") |

Both ride the outbox's task-status op (`forward_task_status`) — no new plumbing, and a no-op on a
filesystem backend or while that op is absent (see that spec).

## 5. Rendering — `carry-over.md`, `mine`, cadence, and the Brief

- **`project()` (`carry-over.md`'s projection):** `observation-complete` joins `open`/`in_progress`
  in the shown set, sorted to the SAME leading tier as `in_progress` — a gate that just closed is
  exactly as worth leading with as work already underway. `observation` itself stays **withheld**,
  same treatment as `held`/`paused`: there is nothing to act on while a row is still waiting.
- **`default_query()` (`mine`):** same tier change — `observation-complete` ranks with `in_progress`.
  `observation` is non-terminal so it still appears, unranked specially, same as any other waiting
  row; it is never marked `dormant` (that overlay only ever applies to `status == "open"`).
- **`cadence_chain.py`:** unchanged. Its resolved-exclusion only excludes terminal statuses (neither
  new value is terminal); the dormancy and re-ask handlers both key on `status == "open"`, so an
  `observation` row simply gets `SKIP` from both — neutral, not excluded, not force-included. No new
  handler is needed; if an item ever needs an odd cadence, the chain's own escape hatch is a new
  check.
- **The Brief:** `observation_gate.brief_line()` composes *"observation complete, ready to continue:
  <item> (<id>)"* for every `observation-complete` row, capped at 5 with the overflow reported as a
  count. It is wired into the daemon's Brief context builder (the same code-hook pattern
  `owi_unknowns.brief_line` uses) and printed **above Tomorrow's Lead**, which sits above "Needs
  You" — a gate that just closed is the day's own leading news, the same way Tomorrow's Lead is the
  day's own stated plan, and neither is a subset of what needs a decision.

## 6. Staleness — a report, never a second mutation

The underlying concern: collected data may stop being relevant to how the system operates. A gate
met more than `STALE_AFTER_DAYS` ago with **no follow-up** — nothing has touched the record since the
flip, read structurally as `last_touched == gate.met_at` (the two are stamped to the same instant by
`mark_observation_complete`; anything that acts on the item afterward moves `last_touched` strictly
past it) — is flagged in `scan()`'s own output and inline in `brief_line()`.

**`STALE_AFTER_DAYS = 14`.** Unlike the register's own dormancy threshold (deliberately left for the
owner to name), this one ships with a number: the failure it exists to catch is measured in "a week or
two, or months," and shipping with no default would let that failure repeat indefinitely while a
threshold sits unset. It is a plain module constant (`observation_gate.STALE_AFTER_DAYS`), tunable at
any time.

## 7. Dream integration

`dream_steps.py` gains step `2i`, `max_age_days: 2` — deliberately short (not the register's own
dormancy window): the whole point is catching a gate the day it closes, not weeks later.
`seneschal/modes/dream.md` step 2i runs `observation_gate.py scan --apply` and instructs the run to
fold every `flipped` id and every `stale` row into the Run Log entry. Idempotent, report-only when
nothing has changed — a night with nothing to flip is a night with nothing to report.

## 8. Seeding — finding work already shaped like an observation period

On a fresh install nothing is gated. The candidates are specs (or project notes) whose next phase
names an explicit evidence bar — not merely "unbuilt," but a named time/count threshold. A
pure-manual gate ("waiting on the owner's decision") needs no scanner to see and is `manual`'s whole
job; only attach one when it sits beside a real time or row-count requirement.

An **illustrative** table (invented examples, not shipped rows):

| Work item / phase | Evidence bar (its own words) | `requires` | Notes |
|---|---|---|---|
| A logging feature's phase 2 | "two weeks AND ≥ 30 classified events" | `min_elapsed days=14 since=<phase-1 ship date>` + `min_rows source=state/<feature>-log.jsonl filter={kind: <event>, typed: true} n=30` | fully machine-checkable — the ideal gate |
| A classifier's phase 2 | "phase 1's data, and a decision on the thresholds" | `manual note="threshold decision made?"` (data half already satisfied) | manual-only; the scanner will never flip it |
| A background job's follow-up | "after the backfill job finishes" | `job_finished job_id=<id>` | met on the job's terminal state, success or not |

Attaching a gate is one command against the live register:

```
python seneschal/scripts/loops.py observe <work-item id> \
    --requires '[{"type": "min_elapsed", "days": 14, "since": "2026-01-10"},
                 {"type": "min_rows", "source": "seneschal/state/example-log.jsonl",
                  "filter": {"kind": "example.event", "typed": true},
                  "n": 30}]'
```

Use `since` whenever the evidence period began before the gate is attached (§2.2) — without it,
`min_elapsed` counts from the instant this command runs. If the bar is already met,
`python seneschal/scripts/observation_gate.py scan --apply` flips the row to `observation-complete`
on the very next run.

## 9. Out of scope

No cockpit register view is added. No new `cadence_chain.py` handler (§5). No gate is attached to any
register row by shipped code (§8) — that is an install-side action.
