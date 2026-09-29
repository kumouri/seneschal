# The register → Notion Tasks status projection

**Status:** `PARTIAL(the forward + project-status BUILT; the in-progress question decided and BUILT —
loops.py start / in_progress; observation / observation-complete mapped — see
observation-gate-spec.md)`

**Notion backend only.** This projection exists only when the active store backend is Notion
(`seneschal/store/config.json`'s `active`, or a legacy `scripts/notion-mcp.json`). On the Obsidian or
Markdown backends there is no separate Tasks database to project into — the register and the store are
both local files — and every call below is a no-op. It also **requires the outbox's task-status op**
(`outbox_common.task_status_key` + the `task_status` op); while `outbox_common` lacks it, the forward
is a no-op and the register write proceeds untouched.

A register status change made through `loops.py` itself — `resolve`, `drop`, `hold`, `start`,
`observe`, an owner-abandon tap — must reach the Notion Tasks row it points at. A read-back in the
other direction (a Notion row the owner flipped to Done/Archived catching the register up) is a
separate import, not this spec. The code lives in `../scripts/loops.py`
(`forward_task_status` / `project_task_status`) and `../scripts/outbox_common.py` (the `task_status`
op).

## The authorization

**A register status change on a record that resolves to a Notion Tasks row is forwarded to that row's
`Status`, with no approval prompt.** This is an act-low projection of a decision already made through
the register — not a new outbound write requiring a fresh yes, the same posture every other
outbox-backed write already carries (reminder acks, run-log finalization, reminder status — none of
those pause for approval either, because the register/chat decision already happened; the outbox's job
is durability, not consent).

The failure it fixes: the owner moves work forward through the assistant (starts it, finishes it,
drops it), and the Notion Tasks view still shows every one of those rows as `Not Started`, because
nothing ever carried the register's change across.

## The mapping table

| register `status` | Notion Tasks `Status` | notes |
|---|---|---|
| `done` | `Done` | `Completed` (date) is also set — the field `../references/databases.md` §Tasks defines as "set when a task is marked `Done`" |
| `dropped` | `Archived` | no reason is carried — Notion's `Archived` has no field for one |
| `abandoned` | `Archived` | same target as `dropped` — Notion draws no distinction between "someone decided" and "nobody ever decided," only the register does |
| `held` | `Paused` | |
| `paused` | `Paused` | **No `loops.py` verb writes `status: "paused"` today** — `STORED_STATUSES` names it, but only `hold` exists, and it writes `held`, a distinct value (`held` is bounded and the assistant's; `paused` is deliberate, the owner's, and unbounded). The row is in the map so the mapping is correct the day a `paused`-writing verb exists. |
| `in_progress` | `In Progress` | `loops.py start` is the writer — see "The in-progress question," below. |
| `observation` | `Paused` | `observation-gate-spec.md` §4. Notion has no value meaning "deliberately waiting on evidence" — `Paused` is the closest, and it is equally true that nothing should happen to the row right now. `loops.py observe` is the writer. |
| `observation-complete` | `Not Started` | Same spec. Notion has no value meaning "a gate just closed, nobody has picked it up yet" — `Not Started` is the closest (never `In Progress`, which would claim the opposite of "ready to continue"). `observation_gate.py`'s scanner (via `loops.mark_observation_complete`) is the writer. |
| `open` | *(nothing forwarded)* | the register has no state that precedes `open` — there is nothing to forward |

`forward_task_status()`'s table is `loops.TASK_STATUS_FORWARD_MAP`; `open` is deliberately **absent**
from it, not mapped to a no-op string — forwarding requires a value to forward, and inventing one
(`Not Started`? `In Progress`?) is exactly the kind of vocabulary decision the register reserves for
the owner (its `KINDS` / `PRIORITIES` / `WHOSE_MOVE` vocabularies follow the same rule: widening is on
evidence and the owner's to decide; narrowing is the owner's alone).

## How a register row is known to point at a Tasks row

`renders_elsewhere` is a **bare Notion page URL**. The URL alone carries no collection id, so nothing
in it can answer "is this a Tasks row" without a live Notion read — and scripts in this tree never
make one (no `seneschal/scripts/*.py` module calls an MCP tool; only the warm/headless LLM turn does).

The correlation is recorded instead in `state/owi-migration-map.json` — the bookkeeping written by an
import that created register rows from store surfaces, keyed `tasks:<notion-page-id>` against the
register row it produced. `loops.py`'s `_tasks_notion_page_id(item_id)` reads that file **directly**
(never importing the importer, which imports `loops` — the reverse would be circular; the same
precedent `loops.dormant_at_epoch()` sets for its own state file) and returns the Notion page id for
`item_id`, or `None`. A missing or corrupt map means `None` — "cannot forward yet," never "the register
write failed."

**Consequence, stated plainly:** a register row added by hand with `loops.py add --renders-elsewhere
<url>` is invisible to this projection, by construction, not by bug. It has no map entry naming which
surface its URL belongs to, and this code does not parse the URL to guess.

## Direction rules vs. a read-back

Two writers can touch this relationship, in opposite directions, and the tie rule between them is
**which side originated the change**, not which ran more recently:

- **Notion wins on an owner-side change** — a status the owner set **by editing the Notion row
  directly**. A read-back that catches the register up (calling `loops.resolve` / `loops.owner_abandon`
  for rows flipped to Done/Archived) must **never echo back to Notion** — the fact originated there,
  and forwarding it would be a no-op write at best and, with unlucky timing, a redundant flush of a
  stale status if the owner has since moved the row again. Such a caller passes **`forward=False`**.
- **The register wins on an assistant-side change** — a status set **through the register itself**:
  `loops.py resolve`/`drop`/`hold`/`start` called from chat, a CLI session, or a picker tap (a tap on an
  unknowns picker IS the owner acting, but entering a decision through the register rather than through
  Notion directly is still the register side of this line). Every such call site keeps the
  `forward=True` default, so the change reaches the Tasks row without anyone remembering to ask twice.

**The genuine race this does not fully resolve:** if the owner edits Notion directly at the same moment
a chat turn calls `loops.py drop` on the same row, the two writers can disagree. There is no
ordering/timestamp reconciliation for that case — the outbox's own FIFO ordering means whichever write
is enqueued second is what Notion ends up showing, same as any other outbox op. Named as a known gap:
the population of rows where both sides change inside the same few minutes is expected to be
near-zero, and a full last-writer-wins clock for one op is more mechanism than the collision rate
justifies. (`notion-write-behind-outbox-spec.md`'s `superseding_date` guard exists for the dated-ack
ops specifically, `DATED_ACK_OPS`, because that collision is real; `task_status` is not in that set,
for the same reason — no measured collision to guard against.)

## The in-progress question — decided: a stored `in_progress` status

The register originally had no state between `open` and its terminal/held/paused values, so work the
owner considered started showed in Notion as `Not Started` — nothing on the register side had ever
asserted otherwise. The decision: **widen the register's stored `status` vocabulary with
`in_progress`** (`dormant` stays derived-only).

**What shipped:** `loops.py start <id> [--because …]` writes `status: "in_progress"`, refusing on a
terminal record (`done`/`dropped`/`abandoned`); `resolve`/`drop`/`hold`/`owner-abandon` from
`in_progress` are ordinary transitions, same as from `open`. `render`'s `project()` and `mine`'s
`default_query()` both treat `in_progress` as `open`'s equal for their filters, then sort it FIRST — it
is the row actually being worked. `TASK_STATUS_FORWARD_MAP` maps `in_progress` → `"In Progress"`. A
`jobs.py` job started with `--loop <id>` calls `loops.py start` for that id — one explicit signal,
never inferred from the job's own existence or from `whose_move`.

**The alternatives considered, kept for the reasoning:**

- **(a) Infer it from a `jobs.py` job whose goal names the loop id.** Real, already-durable evidence
  that *something* is happening, with no new write surface. Weakest as a *sole* signal: a job can
  reference an id in passing, and a job's existence says nothing about intent to keep working past that
  one run. It survives as `jobs.py --loop`'s *explicit* trigger — the flag makes the same signal a
  deliberate call to `start()` instead of an inference.
- **(b) Infer it from `whose_move` flipping to `assistant` with a non-`unknown` `next_action`.** Needs
  no new field, but conflates two questions: `whose_move` answers *whose turn it is to act*
  (`owner`/`assistant`/`external`/`unknown`/`both`), not *whether work has begun*; a row can sit at
  `whose_move: assistant` for reasons unrelated to active work. The register refuses the same collapse
  elsewhere — `kind_assistant`/`priority_assistant` and `kind_owner`/`priority_owner` are separate
  fields precisely so one column never answers two questions. Not built; `whose_move` is untouched.
- **(c) An explicit `start` verb writing a side-channel marker** (e.g. `started_at`) while `status`
  stays `open`, the way `raise_item()` stamps `last_raised_at`. Rejected in favor of the wider
  vocabulary — but its core reasoning carried over: *a decision, not an inference.* `start` is exactly
  as deliberate a call as `hold`/`resolve`/`drop`/`carry`/`raise_item`.

## What the mechanism consists of

- `outbox_common.py`: the `task_status` op (`OPS`), target_kind `page`; `task_status_key(page_id,
  status)` → `task_status:<page>:<status>` — a converging key per `(page, target status)`, so the same
  forward twice is a no-op and a later different status (e.g. `held` → `done`) enqueues fresh.
- `loops.py`: `TASK_STATUS_FORWARD_MAP`, `_tasks_notion_page_id`, `forward_task_status`,
  `project_task_status`; `resolve`/`drop`/`hold`/`owner_abandon`/`start`/`observe`/
  `mark_observation_complete` forward through `forward_task_status` after their own `save()` — never
  before it, so a broken outbox can never block the register write it is reacting to. The forward
  fails open (a failure is recorded via `failures.record`), mirroring every other `state/` writer's
  posture: a failed forward costs the forward, never the register mutation that already succeeded.
- The daemon's outbox drain turn maps `task_status` → `notion-update-page`, setting `Status` (and
  `Completed`, when the payload carries `completed_date`).
- `loops.py project-status [--apply] [--json]`: the one-shot backlog report — below.

## `loops.py project-status`

**Dry-run by default** (`--apply` to mutate — mirrors `render`'s `--write` asymmetry). Scripts here
never call Notion directly, so this cannot diff against Notion's *live* field value; what it CAN answer
without a network call is **"has this register row's current status ever been told to Notion through
this mechanism at all"** — which is exactly what "backlog" means for a projection turned on over an
existing register.

For every register row `_tasks_notion_page_id` resolves to a Tasks page:

- `no_mapping` — `status` has no `TASK_STATUS_FORWARD_MAP` entry (today, always `open`).
- `mismatch` — mapped, and the outbox holds **no entry at all** for
  `task_status:<page>:<desired status>` — never forwarded, ever. This is the bucket `--apply` acts on.
- `queued` — an entry exists and is `pending`/`inflight` (already enqueued, not yet landed).
- `dead_letter` — an entry exists and is `failed`.
- `already_forwarded` — an entry exists, `done`, unresolved (the write is presumed landed).

`--apply` calls `forward_task_status` for every `mismatch` row only — idempotent, so a second run finds
those rows now `queued`/`already_forwarded` and reports zero new `mismatch` entries.
