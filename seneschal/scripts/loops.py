#!/usr/bin/env python3
"""The work-item register — `state/open-loops.json`, its ONE writer, and its carry-over projection.

A work item is a record with a nameable closing event: something the owner or the assistant has to
finish, wait on, or decide. This module is the only code that writes the register. It owns the
store, the lifecycle verbs, the read-side queries other instruments build on, and the generated
region it appends to `state/carry-over.md`.

## Why a script owns this file, and not a rule

Every `state/` store with one owning script holds its shape — `notion-outbox.sqlite` (`outbox.py`),
`acks.json` (`reminders_dequeue.py`), `jobs/*.json` (`jobs.py`). A store written by an LLM turn
writing prose drifts: a carry-over the rules say to rebuild each run grows instead, a callout
accumulates stale lines. **The difference is not diligence. It is whether a schema can refuse.**

`memory_write.py` is the one writer of **bytes** (atomic replace, preserved line endings) with no
opinion about content. This module is the one writer of **meaning**, and uses `memory_write`
underneath for both byte-level guarantees.

## The store

`state/open-loops.json` — `{"schema": "seneschal.open-loops/1", "last_rendered": …, "items": {id:
record}}`. The records sit one level down under `items` so the schema version travels with the
store (the same envelope pattern as other versioned `state/` files).

## What refuses, and why each refusal is the mechanism rather than validation

* **`add` refuses without `--terminal-state`.** *Can you name the event that would close this?* is
  the membership test for the register. A writer who cannot answer has been told by the schema that
  this is a note, not a work item. A test that cannot be skipped is the only kind that holds; a
  written guideline gets skipped.
* **`add` refuses without `whose_move`, `next_action`, `kind_assistant` and `priority_assistant`** —
  all four mandatory, with `unknown` a legal value on each. Mandatory-with-`unknown` is not a
  formality: a row is then honestly `unknown` rather than falsely attributed, and the count of
  `unknown` is the adoption meter.
* **No assistant-side verb can write `kind_owner`, `priority_owner`, or `status: "abandoned"`.**
  Those three record facts only the owner can assert, so they are written by three DISTINCT
  mutations (`owner_kind`, `owner_priority`, `owner_abandon`) — not a shared mutation that checks its
  caller. "The assistant sets the owner's priority" must have **no code path to travel**. The
  assistant-side verbs take no parameter that reaches them, and `test_loops.py::OwnerOnlyWrites`
  reads this module's AST to prove it. **A `set` verb with an `--as-owner` flag would NOT satisfy
  this and must never be added.**
* **`drop` requires `--because`; `owner-abandon` refuses one.** `dropped` means someone decided and
  carries the reason; `abandoned` means *it is over and nobody ever decided* — the absence of a
  reason is that value's whole content.
* **An unset `kind_owner`/`priority_owner` is DATA and is never pre-filled.** The assistant's value
  may not seed, default to, or overwrite the owner's — not on write, not on import, not "as a
  starting point to edit". `None` and an absent key read identically as *unset*.

## The render, and its containment

The projection is **appended below the hand-written content** of `state/carry-over.md`, inside a
marked generated region (see `../docs/carry-over-region-spec.md` for the file's other regions).
Nothing above the marker is ever touched, and a rollback is deleting one generated section.

**`render` writes to STDOUT by default.** `--write` is the only path that touches the file, and it
holds four refusals, each with a test:

1. the target must exist, and must not hold a malformed region (two openers, a closer before its
   opener, a closer with no opener, an opener with no closer);
2. an empty item payload is refused — `--allow-empty` to mean it, `memory_write.write_text`'s own
   precedent, because **atomicity stops a partial write and not a complete write of nothing**;
3. the composed bytes are checked to start with the hand-written region **byte for byte, before
   anything is written**, so a failure costs the write and never the file;
4. the file is re-read after the replace and the same prefix is asserted again; a mismatch restores
   the original bytes and raises.

Every byte this module writes goes through `memory_write.write_text`; there is no `open(p, "w")`.

## Statuses

Nine values are STORED (`STORED_STATUSES`); `dormant` is a tenth, DERIVED from `last_touched` at read
time and never written, so a threshold change reclassifies the corpus with no migration and a touched
row leaves `dormant` by arithmetic. There is no `retired` (it folds into `done`) and `blocked` is not
a status (`blocked_by` says what blocks it; `whose_move` says who it waits on).

* `in_progress` — the row actually being worked. `start()` writes it and refuses on a terminal
  record; every projection sorts it FIRST.
* `observation` — deliberately waiting for evidence to accumulate. **Not `held`** (the assistant
  cannot verify the item is still live; bounded) **and not `paused`** (the owner's deliberate, open-
  ended stop). It is the one status that REQUIRES a machine-checkable gate, refused without one
  (`observe()`, `_validate_gate_requires`). A `min_elapsed` requirement may carry its own `since` to
  backdate its clock when the evidence period began before the gate was attached.
* `observation-complete` — what a met gate looks like. `mark_observation_complete()` (called only by
  `observation_gate.py`'s scanner, never on this CLI) stamps `gate['met_at']` and sets `whose_move`
  to the item's override or `"both"`. Sorted with `in_progress`. Full design:
  `../docs/observation-gate-spec.md`.

## Thresholds

`DORMANT_AFTER_DAYS` (14) and `REASK_AFTER_DAYS` (10) are the two distinct cadence knobs. This
module is their one source of truth; `cadence_chain.py` imports them. They back `is_dormant`'s and
`needs_reask`'s own default parameters only — **`render`/`project`/the CLI's `--dormant-after-days`
have no default**, and when it is not given the scope line **says the overlay did not run**, so an
under-report is arithmetic on the page rather than dead rows quietly rendered as `open`.

There is no `prune` verb: the store is meant to be bounded by age, never by count, and no age has
been chosen. **The ~15 is a TRIPWIRE, NOT A QUOTA** — reported in the region and on stderr, never
truncating: a cap that drops a record destroys the only evidence it was ever open.

## Read-side queries

* `cadence_verdict()` — a lazily-imported wrapper over `cadence_chain.evaluate` (a row may carry an
  optional `checks: [handler-name, …]`; without one it gets `cadence_chain.DEFAULT_CHAIN`).
  Report-only: nothing in `render` reads a verdict.
* `items(whose_move=…)` — a plain equality filter. `for_person(store, person)` is the UNION query: a
  row is on a person's list if EITHER `audience` OR `whose_move` names them; `unknown` in either
  field, or `both` in `whose_move`, counts as both people — never dropped, never guessed.
* `default_query()` — the assistant's own working-memory read (`mine`): **unfiltered by person**
  (a row waiting on the owner alone still belongs in view), non-terminal, dormant excluded,
  `in_progress`/`observation-complete` first, then cadence verdict, then age.
* `last_raised_at` / `raise_item()` — a separate field recording that the assistant actually raised
  the item to the owner on some surface. Its own verb, not a side effect of `carry()` (a projection
  question) or `update()` (whose `last_touched` moves on any edit). `owi_resurface.py` reads it.
* `dormant_at_epoch()` — was a row already dormant at a recorded epoch snapshot
  (`state/owi-dormant-at-epoch.json`, when one has been written), so a row that went quiet before
  measurement began is never scored as a resurfacing miss.

## The register -> Notion Tasks projection (Notion backend only)

`resolve`/`drop`/`hold`/`start`/`observe`/`mark_observation_complete`/`owner_abandon` call
`forward_task_status()` after their own `save()` (a `forward: bool = True` keyword; an importer that
relays a status Notion already carries passes `False` so it is never echoed back). It enqueues a
`task_status` op through `outbox_common.py`, the durable write-behind path every other act-low Notion
write uses. `TASK_STATUS_FORWARD_MAP` is the mapping; `open` is deliberately absent.
`project_task_status()` / CLI `project-status` is the backlog report. Design:
`../docs/register-notion-projection-spec.md`.

**The projection is gated twice and is a no-op when either gate is closed:** (a) the active store
backend must be `notion` (`store_backend()` — `seneschal/store/config.json`'s `active`, or a legacy
`scripts/notion-mcp.json` meaning notion; never raises), since filesystem backends have no Tasks
database and no outbox; and (b) `outbox_common` must provide the `task_status` op
(`task_status_key`) — until it does, forwarding enqueues nothing and `project-status` reports empty
buckets. A row resolves to a Tasks page only through `state/owi-migration-map.json`
(`tasks:<page-id>` -> `{"loop_id": …}`), which an importer writes; the `renders_elsewhere` URL itself
is never parsed. On an install with no such map the projection is inert.

CLI:
    loops.py add --text … --audience … --terminal-state … --whose-move … \\
                 --next-action … --kind … --priority … [--checks NAME [NAME ...]]
    loops.py carry <id> --because …      # writes carried_reason; without it a record drops at rebuild
    loops.py start <id> [--because …]    # status=in_progress; refuses on a terminal record
    loops.py observe <id> --requires '[{"type": "min_elapsed", "days": 14, "since": "2026-01-15"}]' \\
                          [--whose-move-on-complete …]   # status=observation; REFUSES without a
                                          # gate. `since` backdates a `min_elapsed` requirement's own
                                          # clock; checking whether a gate is MET is
                                          # observation_gate.py's job, not this CLI's.
    loops.py hold <id> --because …       # status=held, held_since stamped
    loops.py resolve <id> --because …    # status=done
    loops.py drop <id> --because …       # status=dropped, --because REQUIRED
    loops.py update <id> [--next-action …] [--blocked-by …] [--kind …] [--priority …] \\
                         [--checks NAME [NAME ...]] [--whose-move …]
    loops.py raise <id>                  # stamp last_raised_at
    loops.py project-status [--apply] [--json]  # register/Notion Tasks Status backlog — dry-run
                                          # default; --apply enqueues every `mismatch` row
    loops.py list [--audience …] [--status …] [--whose-move …] [--json]
    loops.py mine [--json]                # the assistant's own default query, READ-ONLY
    loops.py show <id>
    loops.py render [--write] [--dormant-after-days N] [--allow-empty]
    loops.py report [--audience …] [--status …]  # cadence_chain verdicts, READ-ONLY
    loops.py owner-kind <id> --kind …          # OWNER ONLY
    loops.py owner-priority <id> --priority …  # OWNER ONLY
    loops.py owner-abandon <id>                # OWNER ONLY, takes no --because

Per-field contract: `../state/README.md` -> `open-loops.json`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import failures  # noqa: E402
import memory_write  # noqa: E402
import outbox_common  # noqa: E402 — the register -> Notion Tasks projection's write-behind path
import tz_common  # noqa: E402 — every stamp is the owner's wall clock, with its offset

SCHEMA = "seneschal.open-loops/1"
STORE_FILE = "open-loops.json"
CARRY_OVER_FILE = "carry-over.md"

DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

#: The pluggable store's config (`seneschal/store/README.md`) and the pre-/setup-store legacy Notion
#: MCP config — the same two sources `presence.store_backend_active` reads. Module constants so a
#: test can point them at a temp dir.
STORE_CONFIG = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "store", "config.json"))
LEGACY_NOTION_MCP = os.path.join(SCRIPT_DIR, "notion-mcp.json")

#: NINE values are STORED. `dormant` is the tenth and is DERIVED at read time, never written to a
#: record. A reader validating the store against ten rejects nothing; a reader rendering nine
#: under-reports.
STORED_STATUSES = ("open", "in_progress", "observation", "observation-complete", "held", "paused",
                   "done", "dropped", "abandoned")
DERIVED_STATUS = "dormant"
RENDERED_STATUSES = STORED_STATUSES + (DERIVED_STATUS,)
TERMINAL_STATUSES = ("done", "dropped", "abandoned")

#: The requirement vocabulary an `observation` gate's `requires` list draws from
#: (`../docs/observation-gate-spec.md`). Kept HERE, not in `observation_gate.py`, so `observe()` can
#: validate a requirement's `type` at write time without importing the sibling that CHECKS one —
#: read-side instruments (`owi_resurface.py`, `cadence_chain.py`, `observation_gate.py`) import
#: `loops`, never the reverse.
REQUIREMENT_TYPES = ("min_elapsed", "min_rows", "file_exists", "job_finished", "manual")

#: Per-type fields `observe()` refuses a requirement without. Not exhaustive validation — a
#: `min_rows` filter's shape or a `job_finished` id's meaning are `observation_gate.py`'s to
#: interpret — just enough that a requirement missing the field its own type needs is refused at
#: write time rather than silently unevaluable the first time anything reads it.
_REQUIREMENT_REQUIRED_FIELDS = {
    "min_elapsed": ("days",),
    "min_rows": ("source", "n"),
    "file_exists": ("path",),
    "job_finished": ("job_id",),
    "manual": ("note",),
}

#: The two people a list is FOR.
AUDIENCES = ("owner", "assistant")
#: FIVE values. `both` is a row genuinely gated on the owner AND the assistant acting together; it
#: renders on both lists BY CHOICE, distinct from `unknown`, which renders on both BY IGNORANCE. The
#: two stay distinguishable because a later instrument reading *why* a row is on both lists needs the
#: two reasons kept apart. Widening this vocabulary is the owner's call, on evidence.
WHOSE_MOVE = ("owner", "assistant", "external", "unknown", "both")
#: Seven kinds, `unknown` included. Ideas stay `unknown` rather than getting an eighth kind.
KINDS = ("bug", "enhancement", "debt", "decision", "spec", "chore", "unknown")
#: Four NAMED levels plus `unknown` — names over numbers.
PRIORITIES = ("critical", "high", "normal", "low", "unknown")

#: "Tripwire, not a quota." Reported, never enforced.
PROJECTION_TRIPWIRE = 15

#: THE ONE SOURCE OF TRUTH for both cadence numbers — `cadence_chain.py`'s handlers import these.
#: Re-asking comes before dormancy, leaving a few days to decide or discover there is time to do it.
#: Neither is a default for `render`/`project`/the CLI's `--dormant-after-days`, which stays unset by
#: design — these back `is_dormant`'s and `needs_reask`'s OWN default parameter only, so a bare call
#: uses the number while an explicit `None` (what `render`'s CLI path passes unless told otherwise)
#: still means "the overlay did not run."
DORMANT_AFTER_DAYS = 14
REASK_AFTER_DAYS = 10

#: An epoch snapshot of which rows were already dormant when resurfacing measurement began — read-only
#: from here, never written. `dormant_at_epoch()` below is a plain JSON read.
DORMANT_SNAPSHOT_FILE = "owi-dormant-at-epoch.json"

#: The containment. The hand-written region is everything ABOVE the opener and is never touched; the
#: generated region is everything from the opener to the closer inclusive.
BEGIN_MARK = "<!-- BEGIN GENERATED — seneschal/scripts/loops.py render · state/open-loops.json -->"
END_MARK = "<!-- END GENERATED — seneschal/scripts/loops.py render -->"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class LoopsError(Exception):
    """A refusal. Carries a real sentence, the way `jobs.py`'s refusals do."""


# --------------------------------------------------------------------------- paths and time

def default_state_dir() -> str:
    return DEFAULT_STATE_DIR


def _dir(state_dir: str | None) -> str:
    return state_dir or DEFAULT_STATE_DIR


def store_path(state_dir: str | None = None) -> str:
    return os.path.join(_dir(state_dir), STORE_FILE)


def carry_over_path(state_dir: str | None = None) -> str:
    return os.path.join(_dir(state_dir), CARRY_OVER_FILE)


def _now(now: datetime | None = None) -> datetime:
    """The owner's current wall clock, AWARE (`tz_common.local_now` — the configured owner zone,
    else machine-local). Aware on purpose: every stamp carries its offset."""
    return now if now is not None else tz_common.local_now()


def _stamp(now: datetime | None = None) -> str:
    """ISO-8601 with an offset. `last_touched` is the sole input to derived `dormant`, so a stamp
    without an offset would make the derivation depend on the reader's timezone."""
    return _now(now).isoformat(timespec="seconds")


def _parse_stamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed


def _slug(text: str, limit: int = 40) -> str:
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP.sub("-", folded.lower()).strip("-")
    return slug[:limit].strip("-") or "item"


def new_id(text: str, now: datetime | None = None, taken=()) -> str:
    """`loop-YYYYMMDD-slug`. The `loop-` prefix is kept even though the prose vocabulary is *work
    item*: ids are forever, and renaming them would orphan every reference already written."""
    base = f"loop-{_now(now).strftime('%Y%m%d')}-{_slug(text)}"
    if base not in taken:
        return base
    for n in range(2, 100):
        candidate = f"{base}-{n}"
        if candidate not in taken:
            return candidate
    raise LoopsError(f"could not mint an id for {base!r} — 99 collisions on one day")


# --------------------------------------------------------------------------- the store

def empty_store() -> dict:
    return {"schema": SCHEMA, "last_rendered": None, "items": {}}


def load(state_dir: str | None = None) -> dict:
    """Read the store. A MISSING file is an empty store; a MALFORMED one is a refusal.

    The asymmetry is deliberate: *fail-open on unknown, fail-closed on known-bad.* A fresh checkout
    has no store and must be able to `add`; a store that exists and does not parse is a fact about a
    file somebody must look at, and silently replacing it with `{}` is how the only copy of every
    work item gets overwritten."""
    path = store_path(state_dir)
    if not os.path.exists(path):
        return empty_store()
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise LoopsError(
            f"{path} exists and does not parse ({exc}). REFUSING to treat it as empty — "
            f"state/ is gitignored, so this file is the only copy of its records. Fix or move it.")
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        raise LoopsError(
            f"{path} is not a {SCHEMA} store (expected an object with an `items` map). "
            f"REFUSING to overwrite it.")
    data.setdefault("schema", SCHEMA)
    data.setdefault("last_rendered", None)
    return data


def save(store: dict, state_dir: str | None = None) -> None:
    """Atomically, through `memory_write`. NEVER `open(path, "w")` — a truncate-then-write is how a
    `state/` file is lost to a crash mid-write."""
    memory_write.write_text(store_path(state_dir),
                            json.dumps(store, indent=2, ensure_ascii=False) + "\n")


def get(store: dict, item_id: str) -> dict:
    item = store["items"].get(item_id)
    if item is None:
        raise LoopsError(f"no work item `{item_id}` — `loops.py list` shows what is in the register.")
    return item


def _one_of(name: str, value, allowed) -> str:
    if value not in allowed:
        raise LoopsError(
            f"{name} must be one of {' | '.join(allowed)} — got {value!r}. "
            f"The vocabulary is RULED; widening it is the owner's call, on evidence, and narrowing "
            f"it is theirs absolutely.")
    return value


def _required_text(name: str, value, why: str) -> str:
    if value is None or not str(value).strip():
        raise LoopsError(f"{name} is required — {why}")
    return str(value).strip()


def unset(item: dict, field: str) -> bool:
    """Is one of the owner's two fields unset? **An unset value is DATA**: the owner has not weighed
    in, which is one of the two most informative states the pair has. An absent key and an explicit
    `null` read identically, so a record written either way means the same."""
    return item.get(field) is None


# --------------------------------------------------------------------------- the assistant's verbs

def add(state_dir: str | None = None, *, text=None, audience=None, terminal_state=None,
        whose_move=None, next_action=None, kind=None, priority=None, origin=None,
        blocked_by=None, renders_elsewhere=None, notes=None, carried_reason=None,
        checks=None, item_id=None, now: datetime | None = None) -> dict:
    """Mint a work item. Every refusal below is the mechanism, not input validation.

    **`terminal_state` is the membership test** — a writer who cannot name the event that would
    close this has been told by the schema that it is not a work item. **`whose_move`,
    `next_action`, `kind` and `priority` are MANDATORY with `unknown` legal**: the schema refuses to
    let them be forgotten while backfill costs nothing, because an unfilled row is honestly
    `unknown` rather than falsely attributed.

    It takes NO parameter that reaches `kind_owner`, `priority_owner` or `status: "abandoned"`, and
    adding one would delete the requirement rather than extend the verb.

    `checks` is OPTIONAL and unvalidated here — a list of `cadence_chain` handler names, or `None`
    for `cadence_chain.DEFAULT_CHAIN`. An unknown name is `cadence_chain.evaluate`'s `ABORT` case,
    surfaced at read time by `report`/`dry-run`, never a write-time crash here."""
    store = load(state_dir)
    text = _required_text("--text", text, "a work item with no text is not a record of anything.")
    _one_of("--audience", audience, AUDIENCES)
    terminal_state = _required_text(
        "--terminal-state", terminal_state,
        "the register's membership test is *can you name the event that would close this?* If you "
        "cannot, this is not a work item: it belongs in notes, which have no terminal state by "
        "construction. This refusal IS the membership test, which is why it cannot be skipped.")
    _one_of("--whose-move", whose_move, WHOSE_MOVE)
    next_action = _required_text(
        "--next-action", next_action,
        "MANDATORY — one imperative clause, or the literal \"unknown\". `unknown` is a legal "
        "answer; forgetting is not, and the count of `unknown` is the adoption meter.")
    _one_of("--kind", kind, KINDS)
    _one_of("--priority", priority, PRIORITIES)

    stamp = _stamp(now)
    item_id = item_id or new_id(text, now=now, taken=store["items"])
    if item_id in store["items"]:
        raise LoopsError(f"`{item_id}` already exists — ids are not reused.")

    item = {
        "id": item_id,
        "text": text,
        "audience": audience,
        "status": "open",
        "opened": stamp,
        "last_touched": stamp,
        "origin": origin or "unknown",
        "carried_reason": carried_reason,
        "held_since": None,
        "terminal_state": terminal_state,
        "renders_elsewhere": renders_elsewhere,
        "whose_move": whose_move,
        "next_action": next_action,
        "blocked_by": blocked_by,
        "kind_assistant": kind,
        # The owner's two. Written null and NEVER pre-filled from the assistant's value beside them:
        # pre-filling converts "has not weighed in" into a value the owner is presumed to have
        # asserted, and any calibration measurement then reads the assistant's opinion back as
        # agreement.
        "kind_owner": None,
        "priority_assistant": priority,
        "priority_owner": None,
        "notes": notes,
        # Absent/None means "use cadence_chain.DEFAULT_CHAIN"; no migration needed for old rows.
        "checks": checks,
        # None until `observe()` writes one; no migration needed for old rows.
        "gate": None,
        # A separate field — see "Read-side queries" in the module docstring for why it is distinct
        # from both `carried_reason` and `last_touched`.
        "last_raised_at": None,
    }
    store["items"][item_id] = item
    save(store, state_dir)
    return item


def _touch(item: dict, now: datetime | None = None) -> None:
    item["last_touched"] = _stamp(now)


def carry(state_dir: str | None = None, *, item_id=None, because=None,
          now: datetime | None = None) -> dict:
    """Write `carried_reason` — the field that makes the carry-over's inverted default mechanical.

    *The default for every line is DROP; carrying requires a reason the run can name.* A record whose
    reason was never written is dropped by **arithmetic, not by diligence**."""
    store = load(state_dir)
    item = get(store, item_id)
    item["carried_reason"] = _required_text(
        "--because", because,
        "carrying REQUIRES a nameable reason. Without one the record drops at the next rebuild, "
        "which is the intended behaviour and not a bug.")
    _touch(item, now)
    save(store, state_dir)
    return item


def hold(state_dir: str | None = None, *, item_id=None, because=None,
         now: datetime | None = None, forward: bool = True) -> dict:
    """`status: "held"` — *the assistant cannot verify whether this is still live.*

    `held_since` is stamped because the holding bound runs off it (a few journal days, with a hard
    cap on how many rows may sit in holding — growing past it is evidence the rebuild is failing, not
    a reason to grow the group). **That bound is not enforced here**; this verb stamps the clock the
    bound is read off, and `held_count()` is what a later reader asks for.

    `held` is NOT `paused`: `paused` is the owner's own deliberate, open-ended stop and must never be
    nagged. Collapsing them puts deliberate pauses into a nag queue.

    `forward` (default `True`) — the register -> Notion Tasks projection (module docstring). `False`
    only from an importer relaying a status Notion already carries, which must never be echoed back."""
    store = load(state_dir)
    item = get(store, item_id)
    item["status"] = "held"
    item["held_since"] = _stamp(now)
    if because:
        item["notes"] = str(because).strip()
    _touch(item, now)
    save(store, state_dir)
    if forward:
        forward_task_status(item_id, "held", state_dir=state_dir, now=now)
    return item


def start(state_dir: str | None = None, *, item_id=None, because=None,
          now: datetime | None = None, forward: bool = True) -> dict:
    """`status: "in_progress"` — the row is actually being worked. Without it, work that had begun
    kept rendering in a mirrored Tasks view as not started.

    **Refuses on a terminal record** — a `done`/`dropped`/`abandoned` item is over; starting it again
    is re-adding it, not resuming it. `open`/`held`/`paused`/`observation-complete` -> `in_progress`
    and `in_progress` -> `done` via `resolve` are ordinary transitions.

    `forward` (default `True`) — see `hold()`."""
    store = load(state_dir)
    item = get(store, item_id)
    if item["status"] in TERMINAL_STATUSES:
        raise LoopsError(
            f"`{item_id}` is already {item['status']!r}, a terminal status. `start` moves a LIVE "
            f"record to in_progress; a terminal one is over — re-add it if the work is starting "
            f"again, rather than trying to resume the same record.")
    item["status"] = "in_progress"
    if because:
        item["notes"] = str(because).strip()
    _touch(item, now)
    save(store, state_dir)
    if forward:
        forward_task_status(item_id, "in_progress", state_dir=state_dir, now=now)
    return item


def _validate_gate_requires(requires, *, now: datetime | None = None) -> list:
    """`observation` REFUSES without a gate, and this is the refusal. A non-empty list of requirement
    dicts, each carrying a `type` from `REQUIREMENT_TYPES` and that type's own required fields
    (`_REQUIREMENT_REQUIRED_FIELDS`) — checked for SHAPE only. Whether a requirement is met is never
    asked here; that is `observation_gate.py`'s job, on its own schedule, read-only.

    **`min_elapsed`'s optional `since`.** `observe()` always stamps the gate's `started_at` as NOW,
    so without `since` a gate attached for an evidence period that began earlier could not read met
    on attach. When present it must be a parseable ISO date/datetime (naive means the owner's
    timezone) that is not in the future — checked at write time so a typo is a refusal, not a gate
    that silently never reads met."""
    if not isinstance(requires, list) or not requires:
        raise LoopsError(
            "`observation` REQUIRES a gate — a non-empty `requires` list "
            "(observation-gate-spec.md). `observation` means deliberately waiting for evidence to "
            "accumulate, and a wait with no evidence bar named is `held` with an extra syllable, "
            "not a distinct status.")
    reference = _now(now)
    validated = []
    for i, req in enumerate(requires):
        if not isinstance(req, dict) or "type" not in req:
            raise LoopsError(f"--requires[{i}] must be an object carrying a `type` key.")
        rtype = req["type"]
        _one_of(f"--requires[{i}].type", rtype, REQUIREMENT_TYPES)
        missing = [f for f in _REQUIREMENT_REQUIRED_FIELDS[rtype] if not req.get(f) and req.get(f) != 0]
        if missing:
            raise LoopsError(
                f"--requires[{i}] is a {rtype!r} requirement missing {', '.join(missing)} — "
                f"observation-gate-spec.md names what each requirement type needs.")
        if rtype == "min_elapsed" and req.get("since") is not None:
            since = req["since"]
            parsed = _parse_stamp(since if isinstance(since, str) else None)
            if parsed is None:
                raise LoopsError(
                    f"--requires[{i}].since is not a parseable ISO date/datetime: {since!r}.")
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=reference.tzinfo)
            if parsed > reference:
                raise LoopsError(
                    f"--requires[{i}].since ({since!r}) is in the future — a `min_elapsed` clock "
                    f"cannot start later than now.")
        validated.append(dict(req))
    return validated


def observe(state_dir: str | None = None, *, item_id=None, requires=None,
            whose_move_on_complete=None, because=None, now: datetime | None = None,
            forward: bool = True) -> dict:
    """`status: "observation"` — a work item deliberately waiting for evidence to accumulate before
    it can proceed (`../docs/observation-gate-spec.md`).

    **Not `held`** (the assistant cannot verify the item is still live; bounded) **and not `paused`**
    (the owner's deliberate stop, with NO EXIT CONDITION to check). `observation` is the one status
    that REQUIRES a machine-checkable exit condition and refuses without one — `requires`
    (`_validate_gate_requires`) is that refusal, validated for SHAPE only.

    `whose_move_on_complete` (optional) is written into `whose_move` when the gate is met; `None`
    means the default, `"both"` — a resurfaced item is everyone's to pick back up unless the item
    itself says otherwise.

    Refuses on a terminal record, same reason as `start()`. `forward` (default `True`) — see
    `hold()`."""
    store = load(state_dir)
    item = get(store, item_id)
    if item["status"] in TERMINAL_STATUSES:
        raise LoopsError(
            f"`{item_id}` is already {item['status']!r}, a terminal status. `observe` puts a LIVE "
            f"record into observation; a terminal one is over.")
    validated = _validate_gate_requires(requires, now=now)
    if whose_move_on_complete is not None:
        _one_of("--whose-move-on-complete", whose_move_on_complete, WHOSE_MOVE)
    stamp = _stamp(now)
    item["status"] = "observation"
    item["gate"] = {
        "kind": "observation",
        "started_at": stamp,
        "requires": validated,
        "met_at": None,
        "whose_move_on_complete": whose_move_on_complete,
    }
    if because:
        item["notes"] = str(because).strip()
    _touch(item, now)
    save(store, state_dir)
    if forward:
        forward_task_status(item_id, "observation", state_dir=state_dir, now=now)
    return item


def mark_observation_complete(state_dir: str | None = None, *, item_id=None,
                              now: datetime | None = None, forward: bool = True) -> dict:
    """`status: "observation"` -> `"observation-complete"`. `observation_gate.py`'s scanner calls
    this, only once every one of an item's gate requirements has just been found met — and it is
    deliberately **not** on `loops.py`'s own CLI: the transition is a derived fact about a gate, not
    a thing decided directly the way `hold`/`drop` are.

    **Refuses on anything but a record actually in `observation`** — the one transition with exactly
    one legal predecessor, which also makes this the first and only time `gate['met_at']` is stamped.
    Sets `whose_move` to the item's `whose_move_on_complete` or `"both"`.

    **Does not call `raise_item()`.** That is the caller's job, immediately after: this function
    answers *is the gate met*, not *has the owner been told*."""
    store = load(state_dir)
    item = get(store, item_id)
    if item["status"] != "observation":
        raise LoopsError(
            f"`{item_id}` is {item['status']!r}, not `observation` — `mark_observation_complete` "
            f"only ever follows a gate that was actually running.")
    gate = dict(item.get("gate") or {})
    gate["met_at"] = _stamp(now)
    item["gate"] = gate
    item["whose_move"] = gate.get("whose_move_on_complete") or "both"
    item["status"] = "observation-complete"
    _touch(item, now)
    save(store, state_dir)
    if forward:
        forward_task_status(item_id, "observation-complete", state_dir=state_dir, now=now)
    return item


def resolve(state_dir: str | None = None, *, item_id=None, because=None,
            now: datetime | None = None, forward: bool = True) -> dict:
    """`status: "done"` — the `terminal_state` event happened.

    There is no `retired`; it folds into `done`, and putting it back is a vocabulary change, not a
    restoration. `forward` (default `True`) — see `hold()`."""
    store = load(state_dir)
    item = get(store, item_id)
    item["status"] = "done"
    if because:
        item["notes"] = str(because).strip()
    _touch(item, now)
    save(store, state_dir)
    if forward:
        forward_task_status(item_id, "done", state_dir=state_dir, now=now)
    return item


def drop(state_dir: str | None = None, *, item_id=None, because=None,
         now: datetime | None = None, forward: bool = True) -> dict:
    """`status: "dropped"`, `--because` REQUIRED.

    **`drop` sets a status; it does not remove a record** — "not in the carry-over" never means
    "destroy it", and terminal records are kept so the pile rate stays re-measurable. **`dropped` is
    not `abandoned`**: this one means *someone decided*, and recording a decision nobody made would
    permanently pollute the count of real drops.

    `forward` (default `True`) — see `hold()`."""
    store = load(state_dir)
    item = get(store, item_id)
    item["status"] = "dropped"
    item["notes"] = _required_text(
        "--because", because,
        "`dropped` REQUIRES a reason — it means SOMEONE DECIDED, and the reason is what separates "
        "it from `abandoned`, which means nobody ever did.")
    _touch(item, now)
    save(store, state_dir)
    if forward:
        forward_task_status(item_id, "dropped", state_dir=state_dir, now=now)
    return item


def update(state_dir: str | None = None, *, item_id=None, next_action=None, blocked_by=None,
           kind=None, priority=None, text=None, renders_elsewhere=None, origin=None,
           checks=None, whose_move=None, now: datetime | None = None) -> dict:
    """Edit THE ASSISTANT'S OWN mutable fields, and move `last_touched`.

    `next_action` is mandatory and mutable, and `last_touched` — *when anything last happened to
    this* — needs a verb for the things that are not a lifecycle change. **It takes no `status`
    parameter**, so it cannot reach a terminal value, and **no parameter of it reaches `kind_owner`
    or `priority_owner`**: the three owner-only writes have their own mutations.

    `checks` re-composes the row's own cadence chain; an empty list is stored as `None` (falling back
    to `cadence_chain.DEFAULT_CHAIN`, not an empty, always-`SKIP` chain).

    `whose_move` is correctable here (`owi_unknowns.py` records the owner's picker answer through it).
    It is not an owner-only field: the assistant already writes it at birth, so correcting it later
    through the same verb as every other mutable field is not an exception to the owner-only rule."""
    store = load(state_dir)
    item = get(store, item_id)
    if next_action is not None:
        item["next_action"] = _required_text(
            "--next-action", next_action,
            "it is MANDATORY on the record; pass the literal \"unknown\" to say you cannot name one.")
    if blocked_by is not None:
        item["blocked_by"] = blocked_by or None
    if kind is not None:
        item["kind_assistant"] = _one_of("--kind", kind, KINDS)
    if priority is not None:
        item["priority_assistant"] = _one_of("--priority", priority, PRIORITIES)
    if text is not None:
        item["text"] = _required_text("--text", text, "a work item with no text records nothing.")
    if renders_elsewhere is not None:
        item["renders_elsewhere"] = renders_elsewhere or None
    if checks is not None:
        item["checks"] = list(checks) or None
    if origin is not None:
        item["origin"] = origin or "unknown"
    if whose_move is not None:
        item["whose_move"] = _one_of("--whose-move", whose_move, WHOSE_MOVE)
    _touch(item, now)
    save(store, state_dir)
    return item


def raise_item(state_dir: str | None = None, *, item_id=None, now: datetime | None = None) -> dict:
    """Stamp `last_raised_at` — record that the assistant actually surfaced `item_id` to the owner
    (in the Brief, a chat turn, or `owi_resurface.py`'s own report), the one fact the
    positive-resurfacing instrument scores against.

    Its own verb, deliberately, rather than a side effect of `carry()` or `update()`:
    `carried_reason` answers whether a record still belongs in the NEXT carry-over render (and once
    set keeps it there with no further action), and `last_touched` moves on every edit, including a
    routine `update`/`hold`/`drop` that never reached the owner. Scoring off either would let one
    early `carry` or an unrelated `update` read as "raised" forever after."""
    store = load(state_dir)
    item = get(store, item_id)
    item["last_raised_at"] = _stamp(now)
    _touch(item, now)
    save(store, state_dir)
    return item


# --------------------------------------------------------------------------- THE OWNER'S THREE
#
# `kind_owner`, `priority_owner` and the `abandoned` status are writable ONLY by mutations the
# assistant's verbs do not share. Not a shared mutation that checks its caller: a DISTINCT mutation,
# so "the assistant sets the owner's priority" and "the assistant declares a thing abandoned" have
# NO CODE PATH TO TRAVEL. A caller check is a discouragement, and a discouragement does not hold.
#
# THE THREE FIELD NAMES BELOW APPEAR IN NO ASSISTANT-SIDE VERB, AND `test_loops.py` READS THIS FILE'S
# SOURCE TO KEEP IT THAT WAY. Do not add an `--as-owner` flag to a shared verb.

def owner_kind(state_dir: str | None = None, *, item_id=None, kind=None,
               now: datetime | None = None) -> dict:
    """OWNER ONLY. What kind of work the OWNER says it is. Never inferred from their behaviour — not
    from what they opened, not from what they replied to fastest, not from what a model reads out of
    their prose. **If it did not come from the owner, it is unset.**"""
    store = load(state_dir)
    item = get(store, item_id)
    item["kind_owner"] = _one_of("--kind", kind, KINDS)
    _touch(item, now)
    save(store, state_dir)
    return item


def owner_priority(state_dir: str | None = None, *, item_id=None, priority=None,
                   now: datetime | None = None) -> dict:
    """OWNER ONLY. How urgent the OWNER says it is.

    The pair is two columns and not one because **the disagreement is the product**: an item the
    assistant has flagged `priority_assistant: high` for three weeks with no `priority_owner` at all is
    a "high priority" flag the owner has been walking past. **A merged, defaulted or auto-synced pair
    does not implement this; it deletes it** — a merged field cannot diverge from itself — and that
    covers a nightly job copying one into the other, a fallback when unset, and a rendering that shows
    one priority column."""
    store = load(state_dir)
    item = get(store, item_id)
    item["priority_owner"] = _one_of("--priority", priority, PRIORITIES)
    _touch(item, now)
    save(store, state_dir)
    return item


def owner_abandon(state_dir: str | None = None, *, item_id=None,
                  now: datetime | None = None, forward: bool = True) -> dict:
    """OWNER ONLY. *It is over, and nobody ever decided that.*

    **It takes no reason and must never be given one** — the absence of a reason is the value's
    whole content, which is what separates it from `dropped`. And it may NEVER be derived from a
    timestamp: *dormant* is a fact about the record, *abandoned* is a claim about the owner, and the
    assistant asserting the second across many rows off a modification date is a confident guess with
    a schema field to live in, made worse by being terminal.

    `forward` (default `True`) — see `hold()`; an importer relaying an archive the owner made by hand
    in Notion passes `False` so it is not echoed back to itself."""
    store = load(state_dir)
    item = get(store, item_id)
    item["status"] = "abandoned"
    _touch(item, now)
    save(store, state_dir)
    if forward:
        forward_task_status(item_id, "abandoned", state_dir=state_dir, now=now)
    return item


# --------------------------------------------------------------------------- reading

def items(store: dict, *, audience=None, status=None, whose_move=None) -> list:
    """`whose_move` is a PLAIN equality filter, parallel to `audience`/`status` — it backs
    `list --whose-move`, a raw lookup. It is NOT the union query: asking for `whose_move="owner"`
    here does not also return a row whose `audience` is `"owner"` but whose `whose_move` is
    something else. **`for_person()` below is the union**, and is a distinct function on purpose —
    collapsing the two would make a raw `--whose-move owner` silently grow to include rows it was
    never asked for."""
    rows = list(store["items"].values())
    if audience:
        rows = [r for r in rows if r.get("audience") == audience]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if whose_move:
        rows = [r for r in rows if r.get("whose_move") == whose_move]
    return sorted(rows, key=lambda r: (r.get("opened") or "", r.get("id") or ""))


def _matches_person(item: dict, person: str) -> bool:
    """The UNION test for one item and one person: does `audience` OR `whose_move` name them?
    `unknown` in EITHER field counts as both people — never dropped, never guessed. `both` in
    `whose_move` counts as both BY CHOICE rather than by ignorance, which is why it is listed beside
    `unknown` rather than folded into it: the two reach the same visibility outcome for different
    reasons, and a later instrument reading *why* needs that kept apart at the source field.
    `audience` has no `unknown` value today (`AUDIENCES` is two-valued); the check on it is
    defensive, for if that ever changes."""
    audience = item.get("audience")
    move = item.get("whose_move")
    if audience == person or audience == "unknown":
        return True
    return move == person or move in ("unknown", "both")


def for_person(store: dict, person: str, *, status=None) -> list:
    """The UNION query: an item appears on `person`'s list if EITHER `audience` or `whose_move`
    names them. `person` must be `"owner"` or `"assistant"` (`AUDIENCES`) — the two people a list is
    FOR; `external` and `unknown` are values a field can hold, never a person who reads a list.

    This is the primitive a per-person filter view would read. It is NOT what backs
    `carry-over.md`'s render, which stays on its `audience`-only filter: adding the union to what
    already surfaces would be a policy change, not a primitive."""
    if person not in AUDIENCES:
        raise LoopsError(f"--person must be one of {' | '.join(AUDIENCES)} — got {person!r}. "
                         f"`external` and `unknown` are values a field holds, never a person a list "
                         f"is FOR.")
    rows = items(store, status=status)
    return [r for r in rows if _matches_person(r, person)]


_CADENCE_VERDICT_ORDER = {"INCLUDE": 0, "SKIP": 1, "EXCLUDE": 2, "ABORT": 3}


def _age_days(item: dict, now: datetime) -> float | None:
    touched = _parse_stamp(item.get("last_touched"))
    if touched is None:
        return None
    if touched.tzinfo is None:
        touched = touched.replace(tzinfo=now.tzinfo)
    return (now - touched).total_seconds() / 86400.0


def default_query(store: dict, *, now: datetime | None = None) -> list:
    """The assistant's own working-memory read (`loops.py mine`). What matters most is what is seen
    by default, because a turn is grounded on the default list, not on the full one it may never
    open. So this is **UNFILTERED BY PERSON**, deliberately: a row waiting on the owner alone is still
    something the assistant needs in view (to know whether to re-ask it). `for_person(store,
    "assistant")` is a NARROWER, different question.

    Filter: `status` non-terminal, `dormant` at `DORMANT_AFTER_DAYS` EXCLUDED, `unknown`/`both`
    included same as every other value. **Order: `in_progress`/`observation-complete` rows FIRST**
    (the rows actually being worked, or just resurfaced), **then the cadence chain's verdict**
    (`INCLUDE` before `SKIP` before `EXCLUDE`/`ABORT` — the last two should not appear here since
    terminal and dormant rows are excluded above, but the order is defined for a custom `checks`
    chain that reaches one anyway), **then by age**, oldest first within a group. Uses
    `cadence_verdict()`, so this module still does not import `cadence_chain` at import time."""
    reference = _now(now)
    rows = [r for r in store["items"].values() if r.get("status") not in TERMINAL_STATUSES]
    rows = [r for r in rows if not is_dormant(r, DORMANT_AFTER_DAYS, now=reference)]

    def _key(item):
        in_progress_rank = 0 if item.get("status") in ("in_progress", "observation-complete") else 1
        verdict = cadence_verdict(item, reference)["verdict"]
        rank = _CADENCE_VERDICT_ORDER.get(verdict, len(_CADENCE_VERDICT_ORDER))
        age = _age_days(item, reference)
        return (in_progress_rank, rank, -(age if age is not None else 0.0), item.get("id") or "")

    return sorted(rows, key=_key)


def held_count(store: dict) -> int:
    """What the holding cap is read off. The cap is not enforced here; this is the number a later
    reader asks for."""
    return sum(1 for r in store["items"].values() if r.get("status") == "held")


def is_dormant(item: dict, dormant_after_days: int | None = DORMANT_AFTER_DAYS,
               now: datetime | None = None) -> bool:
    """`dormant` is DERIVED from `last_touched` and NEVER STORED, so changing the threshold
    reclassifies the whole corpus with **no migration**, and a touched row leaves `dormant` **by
    arithmetic rather than by anyone remembering to re-run anything.**

    **Returns False when the threshold is None, and that is not the same as "not dormant."** An
    EXPLICIT `None` — what `render`/`project`/the CLI's `--dormant-after-days` pass unless told
    otherwise — means the overlay did not run. The default is `DORMANT_AFTER_DAYS`: a bare
    `is_dormant(item)` answers against it, while every caller that passes its own value is
    unaffected, because Python only applies a default when the argument is OMITTED."""
    if dormant_after_days is None or item.get("status") != "open":
        return False
    touched = _parse_stamp(item.get("last_touched"))
    if touched is None:
        return False
    reference = _now(now)
    if touched.tzinfo is None:
        touched = touched.replace(tzinfo=reference.tzinfo)
    return (reference - touched) > timedelta(days=dormant_after_days)


def needs_reask(item: dict, reask_after_days: int | None = REASK_AFTER_DAYS,
                now: datetime | None = None) -> bool:
    """The second, DISTINCT cadence knob — `is_dormant`'s sibling, not its opposite: a row can be
    neither (untouched 5 days), only this one (untouched 11), or both (untouched 20 —
    `cadence_chain.DEFAULT_CHAIN`'s ordering is what keeps a chain from re-asking about something it
    just excluded as dormant, not this function).

    `whose_move` must be `owner` **or** `both`: a `both` row is gated on the two of them together,
    and it is still the assistant's move to raise it, just never to nag it solo. This answers ONLY
    the arithmetic question (*is it time to raise this again*); phrasing a `both` row as a joint ask
    is `ask_line()`'s job. Nothing here sends anything."""
    if item.get("status") != "open" or item.get("whose_move") not in ("owner", "both"):
        return False
    if reask_after_days is None:
        return False
    touched = _parse_stamp(item.get("last_touched"))
    if touched is None:
        return False
    reference = _now(now)
    if touched.tzinfo is None:
        touched = touched.replace(tzinfo=reference.tzinfo)
    return (reference - touched) > timedelta(days=reask_after_days)


#: The drafted re-ask per `whose_move`: a `both` row is a JOINT ask, never a solo nag, because the row
#: is gated on the two of them together and nagging the owner for it misstates who is on the hook. An
#: `owner` row is theirs to move, so the ask is where it stands. Everything else is not a re-ask
#: candidate (`needs_reask`) and gets no line.
ASK_TEMPLATES = {
    "both": "Do you have time for “{text}”, or should I move on it myself?",
    "owner": "“{text}” is on you — where does it stand?",
}


def ask_line(item: dict) -> str | None:
    """The drafted re-ask for `item`, by `whose_move` — or `None` for a row that is not the
    assistant's to raise (`assistant`/`external`/`unknown`, or any terminal row). Pure text, no side
    effects: the callers that actually raise something (`mine`'s output, `owi_resurface.report`)
    hand this to the turn so a `both` row reaches the owner as a joint ask and never as a solo
    reminder."""
    if item.get("status") in TERMINAL_STATUSES:
        return None
    template = ASK_TEMPLATES.get(item.get("whose_move") or "")
    if template is None:
        return None
    return template.format(text=item.get("text") or item.get("id") or "this")


def dormant_at_epoch(item: dict, state_dir: str | None = None) -> bool:
    """Was `item` already dormant at the recorded epoch snapshot (`state/owi-dormant-at-epoch.json`,
    `{"dormant_ids": [...]}`)? Such a row is a PERMANENT exemption from the positive-resurfacing
    obligation — it went quiet before measurement began, so silence since then cannot be scored as a
    miss — and must stay distinguishable from a row that only crossed `DORMANT_AFTER_DAYS` afterward.

    **Fails to `False`, never to `True`, on a missing or corrupt snapshot** — no epoch recorded means
    nothing is exempt, never the reverse (fail-open on absent, never fail-open on a claim it cannot
    read)."""
    path = os.path.join(_dir(state_dir), DORMANT_SNAPSHOT_FILE)
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return item.get("id") in (data.get("dormant_ids") or [])


def cadence_verdict(item: dict, now: datetime | None = None) -> dict:
    """Report-only. What `cadence_chain`'s check-chain says about `item` today:
    `{"verdict", "decided_by", "reason"}`. Imports `cadence_chain` LAZILY, inside this function, so
    importing `loops` never pulls in `cadence_chain` (which itself imports `loops` at module level) —
    the two stay independently testable with no import-order fragility.

    Nothing in `render` reads this; `report`, `mine` and `default_query()` are its callers."""
    import cadence_chain
    return cadence_chain.evaluate(item, _now(now))


# --------------------------------------------------------------------------- register -> Notion Tasks
#
# `../docs/register-notion-projection-spec.md`. A status change on a record that resolves to a Notion
# Tasks row is forwarded to that row's `Status` through the durable outbox (`outbox_common.py`), with
# NO approval prompt — an act-low projection of a decision already made through the register, not a
# new outbound ask. NOTION BACKEND ONLY, and inert until `outbox_common` provides the `task_status` op
# (see the module docstring and `_projection_gate()`).

#: The correlation map an importer writes — the ONLY place a register item id is tied to the Notion
#: surface its `renders_elsewhere` URL belongs to; the URL string itself carries no collection id.
MIGRATION_MAP_FILE = "owi-migration-map.json"
TASKS_SURFACE_PREFIX = "tasks:"

#: register `status` -> Notion Tasks `Status` value. `open` is DELIBERATELY ABSENT — the register has
#: no state that precedes `open`, so there is nothing honest to send. `paused` has no assistant-side
#: writer today (no verb reaches it); it is mapped so it is correct the day one exists.
#: `observation` -> `Paused` and `observation-complete` -> `Not Started`: Notion has no value for
#: "deliberately waiting on evidence" or "just resurfaced, not yet picked up", so each borrows the
#: closest existing value — never `In Progress`, which would claim work has begun.
TASK_STATUS_FORWARD_MAP = {
    "in_progress": "In Progress",
    "observation": "Paused",
    "observation-complete": "Not Started",
    "done": "Done",
    "dropped": "Archived",
    "abandoned": "Archived",
    "held": "Paused",
    "paused": "Paused",
}


def store_backend() -> str | None:
    """The active store backend's name (`notion` / `obsidian` / `markdown` / …), or `None` when no
    store is configured. Same sources as `presence.store_backend_active`: a parsed
    `seneschal/store/config.json` answers with its `active` key; with no usable config, a legacy
    `scripts/notion-mcp.json` means a pre-/setup-store **notion** install. **Never raises** — an
    unreadable config reads as "not configured", which closes the Notion-only gate."""
    try:
        try:
            with open(STORE_CONFIG, encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            cfg = None
        if isinstance(cfg, dict):
            active = cfg.get("active")
            return active if isinstance(active, str) and active else None
        return "notion" if os.path.exists(LEGACY_NOTION_MCP) else None
    except Exception:  # noqa: BLE001 — a gate read must never break a register write
        return None


def _projection_gate() -> str | None:
    """`None` when the register -> Notion Tasks projection may run, else the reason it is a no-op:
    the active backend is not Notion (filesystem backends have no Tasks database and no outbox), or
    `outbox_common` does not yet provide the `task_status` op."""
    backend = store_backend()
    if backend != "notion":
        return f"store backend is {backend or 'unconfigured'}, not notion"
    if not hasattr(outbox_common, "task_status_key"):
        return "outbox_common has no task_status op yet"
    return None


def _tasks_notion_page_id(item_id: str, state_dir: str | None = None) -> str | None:
    """Does `item_id` resolve to a Notion Tasks row? Reads `owi-migration-map.json` and returns the
    page id from the first `tasks:<id>` key whose entry's `loop_id` matches, or `None`.

    A row added by hand (`loops.py add --renders-elsewhere <url>`) is invisible to this lookup BY
    CONSTRUCTION — nothing names which surface its URL belongs to, and this function never parses
    the URL to guess. Fails to `None` on a missing or corrupt map, never raises: an unreadable map
    means "cannot forward yet", not "the register write failed"."""
    path = os.path.join(_dir(state_dir), MIGRATION_MAP_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return None
    for ref, entry in entries.items():
        if not isinstance(ref, str) or not ref.startswith(TASKS_SURFACE_PREFIX):
            continue
        if isinstance(entry, dict) and entry.get("loop_id") == item_id:
            return ref[len(TASKS_SURFACE_PREFIX):]
    return None


def forward_task_status(item_id: str, status: str, *, state_dir: str | None = None,
                        now: datetime | None = None) -> dict | None:
    """Enqueue a `task_status` outbox op so `item_id`'s register status reaches its Notion Tasks
    row's `Status`, through the durable write-behind path (journal here, flush by the drain turn —
    `outbox.py`). **Nothing here talks to Notion.**

    Returns `None` — a no-op, not a refusal — when the projection is gated off (`_projection_gate`:
    a non-Notion backend, or no `task_status` op in `outbox_common` yet), when `status` has no
    `TASK_STATUS_FORWARD_MAP` entry (`open`), or when `item_id` does not resolve to a Tasks row.
    Otherwise returns the outbox `enqueue()` result.

    **Fails open, deliberately.** The register mutation this is called FROM has already saved — a
    broken outbox store must cost the forward, never unwind or block a register write that already
    succeeded. A failure is recorded via `failures.record` rather than silently swallowed."""
    if _projection_gate() is not None:
        return None
    notion_status = TASK_STATUS_FORWARD_MAP.get(status)
    if notion_status is None:
        return None
    page_id = _tasks_notion_page_id(item_id, state_dir)
    if page_id is None:
        return None
    payload = {"status": notion_status}
    if notion_status == "Done":
        # The owner's calendar date for the completion instant (never UTC's).
        payload["completed_date"] = tz_common.to_local(_now(now)).date().isoformat()
    try:
        key = outbox_common.task_status_key(page_id, notion_status)
        conn = outbox_common.connect(_dir(state_dir))
        try:
            return outbox_common.enqueue(conn, "task_status", "page", page_id, payload, key)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — see the docstring: this must cost the forward, not the write
        failures.record(_dir(state_dir), "loops.forward_task_status", "outbox_enqueue_failed",
                        detail=f"{item_id} -> {page_id} {notion_status}: {exc}")
        return None


def _empty_buckets() -> dict:
    return {"mismatch": [], "queued": [], "dead_letter": [], "already_forwarded": [],
            "no_mapping": []}


def project_task_status(state_dir: str | None = None) -> dict:
    """`loops.py project-status`'s whole computation — for every register row that resolves to a
    Notion Tasks row, has this mechanism ever told Notion its current status? Scripts never call the
    MCP tools directly, so this is NOT a live diff against Notion's field — it is the question code
    can answer without one: has a `task_status` entry for `(this page, the status this row's current
    `status` maps to)` ever been enqueued.

    Buckets, each a list of `{id, status, page_id, text, desired_status}`:
      * `no_mapping`  — `status` has no `TASK_STATUS_FORWARD_MAP` entry (today, always `open`).
      * `mismatch`    — mapped, and the outbox holds NO entry for `(page, desired status)` — never
                        forwarded. The bucket `--apply` acts on.
      * `queued`      — an entry exists and is `pending`/`inflight`.
      * `dead_letter` — an entry exists and is `failed`.
      * `already_forwarded` — an entry exists and is `done`.

    Every bucket is empty when the projection is gated off (`_projection_gate`)."""
    buckets = _empty_buckets()
    if _projection_gate() is not None:
        return buckets
    store = load(state_dir)
    conn = outbox_common.connect(_dir(state_dir))
    try:
        for item in store["items"].values():
            page_id = _tasks_notion_page_id(item["id"], state_dir)
            if page_id is None:
                continue  # not a Tasks pointer at all — out of scope for this projection
            row = {"id": item["id"], "status": item["status"], "page_id": page_id,
                   "text": item.get("text")}
            desired = TASK_STATUS_FORWARD_MAP.get(item["status"])
            row["desired_status"] = desired
            if desired is None:
                buckets["no_mapping"].append(row)
                continue
            key = outbox_common.task_status_key(page_id, desired)
            entry = outbox_common.get_by_key(conn, key)
            if entry is None:
                buckets["mismatch"].append(row)
            elif entry["status"] == outbox_common.FAILED:
                buckets["dead_letter"].append(row)
            elif entry["status"] == outbox_common.DONE:
                buckets["already_forwarded"].append(row)
            else:
                buckets["queued"].append(row)
        return buckets
    finally:
        conn.close()


# --------------------------------------------------------------------------- the projection

def _is_new_since(item: dict, since) -> bool:
    """Was this opened since the last rebuild? **Every ambiguity answers YES.**

    Stamps are second-resolution, so an item added in the same second as a render is genuinely
    undecidable — hence `>=` rather than `>`, and an unparseable stamp on either side reads as new.
    The direction is the point: a redundantly-shown work item costs one line skimmed, and a
    silently-dropped one is the failure this register exists to prevent. The cost is bounded at one
    extra render, because the next rebuild stamps a `last_rendered` strictly after it."""
    if since is None:
        return True
    opened = _parse_stamp(item.get("opened"))
    marker = _parse_stamp(since)
    if opened is None or marker is None:
        return True
    if opened.tzinfo is None or marker.tzinfo is None:
        return opened.replace(tzinfo=None) >= marker.replace(tzinfo=None)
    return opened >= marker


def project(store: dict, *, audience: str = "owner", dormant_after_days: int | None = None,
            now: datetime | None = None) -> dict:
    """Select what `state/carry-over.md`'s generated region shows, and count everything it does not.

    The filter: records that are `open` **and** carry a non-null `carried_reason`, or that were
    opened since the last rebuild. `held` is not shown and is pointed at; the other audience is not
    shown and is counted. **`in_progress` and `observation-complete` are treated as `open`** for this
    filter and sorted FIRST within `shown` — the row being worked, or a gate that just closed, is the
    one most worth leading with. **`observation` itself stays withheld**, like `held`/`paused`: there
    is nothing to do about a row still waiting on its gate.

    **What is missing has to be visible as a NUMBER even though its content is not.** A partial pull
    then stops being silent and becomes arithmetic that does not add up."""
    since = store.get("last_rendered")
    shown, withheld = [], {}

    def withhold(reason: str) -> None:
        withheld[reason] = withheld.get(reason, 0) + 1

    for item in items(store):
        if item.get("audience") != audience:
            withhold(f"audience={item.get('audience')}")
            continue
        status = item.get("status")
        if status in TERMINAL_STATUSES:
            continue  # terminal records are RETAINED in the store and never rendered
        if is_dormant(item, dormant_after_days, now=now):
            withhold("status=dormant")
            continue
        if status not in ("open", "in_progress", "observation-complete"):
            withhold(f"status={status}")
            continue
        if item.get("carried_reason") is None and not _is_new_since(item, since):
            withhold("no carried_reason")
            continue
        shown.append(item)

    # `in_progress`/`observation-complete` FIRST, stable otherwise — `items()` already sorted
    # `shown` by (opened, id).
    shown.sort(key=lambda r: 0 if r.get("status") in ("in_progress", "observation-complete") else 1)

    return {
        "audience": audience,
        "shown": shown,
        "withheld": withheld,
        "total": len(store["items"]),
        "considered": sum(1 for r in store["items"].values()
                          if r.get("status") not in TERMINAL_STATUSES),
        "since": since,
        "dormant_after_days": dormant_after_days,
        "tripwire": len(shown) > PROJECTION_TRIPWIRE,
    }


def _scope_lines(view: dict, now: datetime | None = None) -> list:
    """The scope line. **The artifact states the filter that produced it**, so a reader quoting its
    items as *everything outstanding* is contradicted by the bottom of the page they just read."""
    shown, considered = len(view["shown"]), view["considered"]
    lines = [
        f"— rendered {_stamp(now)} from state/{STORE_FILE} ({SCHEMA})",
        f"  showing {shown} of {considered} live records · "
        f"filter: audience={view['audience']}, status=open, in_progress, or observation-complete, "
        f"carried_reason≠null or new since {view['since'] or 'the first render'}",
    ]
    if view["withheld"]:
        parts = []
        for reason in sorted(view["withheld"]):
            note = " (awaiting a decision)" if reason == "status=held" else ""
            parts.append(f"{view['withheld'][reason]} {reason}{note}")
        lines.append("  not shown: " + " · ".join(parts))
    if view["dormant_after_days"] is None:
        lines.append("  dormant overlay: NOT APPLIED — the threshold is unset and is the owner's "
                     "to choose, so an `open` row here may be one nobody has touched in months.")
    else:
        lines.append(f"  dormant overlay: applied at {view['dormant_after_days']} days since "
                     f"last_touched (derived, never stored)")
    if view["tripwire"]:
        lines.append(f"  ⚠ TRIPWIRE: {shown} items is past ~{PROJECTION_TRIPWIRE}. "
                     f"Tripwire, not a quota — nothing was dropped. If this list is long, the "
                     f"rebuild probably did not happen.")
    return lines


def _item_line(item: dict) -> str:
    bits = [f"**{item['id']}** — {item['text']}"]
    tail = [f"next: {item.get('next_action') or 'unknown'}",
            f"whose move: {item.get('whose_move') or 'unknown'}",
            f"kind: {item.get('kind_assistant') or 'unknown'}",
            f"priority: {item.get('priority_assistant') or 'unknown'}"]
    # The owner's is shown ONLY when THEY set it. An absent value is DATA, and rendering a fallback
    # to the assistant's here would be the forbidden merge, performed at the last possible moment.
    if not unset(item, "kind_owner"):
        tail.append(f"OWNER kind: {item['kind_owner']}")
    if not unset(item, "priority_owner"):
        tail.append(f"OWNER priority: {item['priority_owner']}")
    if item.get("blocked_by"):
        tail.append(f"blocked by: {item['blocked_by']}")
    if item.get("renders_elsewhere"):
        tail.append(f"renders at: {item['renders_elsewhere']}")
    gate = item.get("gate")
    if gate:
        n = len(gate.get("requires") or [])
        if item.get("status") == "observation":
            tail.append(f"gate: watching {n} requirement(s) since {gate.get('started_at')}")
        elif gate.get("met_at"):
            tail.append(f"gate: met {gate['met_at']}")
    bits.append("  \n  " + " · ".join(tail))
    return "- " + "".join(bits)


def render_region(store: dict, *, audience: str = "owner", dormant_after_days: int | None = None,
                  now: datetime | None = None) -> str:
    """The generated region, markers included, newline-joined with `\\n`.

    The caller retargets line endings; nothing here assumes one."""
    view = project(store, audience=audience, dormant_after_days=dormant_after_days, now=now)
    lines = [BEGIN_MARK, "",
             "## Work items — GENERATED, do not hand-edit",
             "",
             "> Rebuilt by `seneschal/scripts/loops.py render --write` from `state/open-loops.json`, the "
             "one writable source. **Everything ABOVE the BEGIN marker is hand-written and is never "
             "touched by this tool.** A rollback is deleting this section.",
             ""]
    if view["shown"]:
        lines.extend(_item_line(item) for item in view["shown"])
    else:
        lines.append("_No work items match this projection's filter. The scope line below says what "
                     "was withheld and why._")
    lines.append("")
    lines.extend(_scope_lines(view, now=now))
    lines.extend(["", END_MARK])
    return "\n".join(lines)


# --------------------------------------------------------------------------- the guarded write

def split_hand_written(text: str) -> tuple:
    """`(hand_written, had_region)`. Refuses a malformed region rather than guessing at one.

    The hand-written region is everything BEFORE the first opener. That definition is what makes the
    containment checkable: nothing above the marker moves."""
    opens, closes = text.count(BEGIN_MARK), text.count(END_MARK)
    if opens > 1:
        raise LoopsError(
            f"{CARRY_OVER_FILE} holds {opens} generated-region openers. REFUSING to write — with "
            f"more than one, 'everything above the marker' names two different regions and this "
            f"tool cannot tell which is hand-written. Resolve it by hand.")
    if closes > 1:
        raise LoopsError(
            f"{CARRY_OVER_FILE} holds {closes} generated-region closers. REFUSING to write.")
    if opens == 0 and closes == 1:
        raise LoopsError(
            f"{CARRY_OVER_FILE} holds a generated-region CLOSER with no opener. REFUSING to write — "
            f"the file has been hand-edited inside the region and the boundary is unknowable.")
    if opens == 1 and closes == 1 and text.index(END_MARK) < text.index(BEGIN_MARK):
        raise LoopsError(
            f"{CARRY_OVER_FILE}'s generated-region closer comes before its opener. REFUSING to write.")
    if opens == 1 and closes == 0:
        raise LoopsError(
            f"{CARRY_OVER_FILE} holds a generated-region OPENER with no closer — the region is "
            f"truncated. REFUSING to write, because replacing it would silently discard whatever "
            f"followed it.")
    if opens == 0:
        return text, False
    return text[:text.index(BEGIN_MARK)], True


def _read_bytes(path: str) -> tuple:
    raw = open(path, "rb").read()
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    return raw, raw.decode(encoding), encoding


def write_render(state_dir: str | None = None, *, audience: str = "owner",
                 dormant_after_days: int | None = None, allow_empty: bool = False,
                 now: datetime | None = None) -> dict:
    """Replace the generated region of `state/carry-over.md` and touch NOTHING above it.

    Four refusals, in the order they can fire, and every one costs the write rather than the file:

    1. **the target must exist** — creating it would mean this tool inventing the hand-written half;
    2. **a malformed region is refused** (`split_hand_written`), because the boundary is what the
       containment is made of;
    3. **an empty item payload is refused** unless `allow_empty` — atomicity stops a PARTIAL write,
       not a complete write of NOTHING, and an upstream producer that already failed would otherwise
       replace the region with an empty one through the correct helper, exit 0;
    4. **the composed bytes must start with the hand-written bytes VERBATIM** — asserted before the
       write, so a mismatch writes nothing, and again after the write against what is on disk, where
       a mismatch restores the original bytes and raises."""
    path = carry_over_path(state_dir)
    if not os.path.exists(path):
        raise LoopsError(
            f"{path} does not exist. REFUSING to create it — this tool owns ONE generated region "
            f"appended below hand-written content it must not invent. "
            f"Seed it from state/{CARRY_OVER_FILE.replace('.md', '.example.md')} first.")

    original_bytes, original_text, encoding = _read_bytes(path)
    hand_written, had_region = split_hand_written(original_text)
    hand_bytes = hand_written.encode(encoding)
    # FIRST, and not the same check as the one below: does the region this tool believes is
    # hand-written actually exist, byte for byte, at the head of the file on disk? The later
    # assertion compares the composed text against `hand_bytes`, which is where the composition
    # started — so it can only catch a bug BELOW this line and is tautological without this one.
    # This is what catches a slice/decode round-trip that shifted the boundary.
    if not original_bytes.startswith(hand_bytes):
        raise LoopsError(
            f"REFUSED: the hand-written region this tool computed is not a byte-prefix of {path} "
            f"as it is on disk. Nothing was written. The boundary is what the containment is made "
            f"of, so a disagreement about where it falls is not something to write through.")

    store = load(state_dir)
    view = project(store, audience=audience, dormant_after_days=dormant_after_days, now=now)
    if not view["shown"] and not allow_empty:
        raise LoopsError(
            f"REFUSED: the projection selected 0 of {view['considered']} live records, so the "
            f"generated region would carry no items. That is almost always a producer that failed "
            f"upstream — an unreadable store, a wrong --audience, a filter that matched nothing. "
            f"The file was NOT modified. If the register really is empty, pass --allow-empty.")

    newline = memory_write.detect_newline(path) or "\n"
    region = render_region(store, audience=audience, dormant_after_days=dormant_after_days, now=now)
    region = region.replace("\n", newline)
    # The separator is exactly enough to leave ONE blank line, and it is IDEMPOTENT.
    #
    # Not cosmetic. The hand-written region is *everything before the opener*, so after the first
    # render it INCLUDES the blank line this added — and a separator computed as "always add one"
    # appends another on every rebuild. Run nightly, that is a file that grows a blank line a day
    # while every assertion here stays green. Asking what the region already ENDS with is what makes
    # a re-render a no-op.
    if not hand_written:
        separator = ""                      # a file that is only a region: no leading blank lines
    elif hand_written.endswith(newline * 2):
        separator = ""                      # already separated — a re-render must change nothing
    elif hand_written.endswith(newline):
        separator = newline
    else:
        separator = newline * 2
    new_text = hand_written + separator + region + newline

    composed = new_text.encode(encoding)
    if not composed.startswith(hand_bytes):
        raise LoopsError(
            "REFUSED: the composed file does not begin with the hand-written region byte for byte. "
            "Nothing was written. This is the assertion that makes the containment real rather "
            "than intended.")

    # newline=None: translate nothing. The region was already retargeted to the file's own ending
    # above, and the hand-written half is carried through exactly as decoded — so a MIXED file keeps
    # its mixture instead of being silently normalised.
    memory_write.write_text(path, new_text, newline=None)

    after = open(path, "rb").read()
    if not after.startswith(hand_bytes):
        memory_write.write_text(path, original_text, newline=None)
        raise LoopsError(
            f"POST-WRITE ASSERTION FAILED: {path} no longer begins with its hand-written region. "
            f"The original {len(original_bytes):,} bytes have been restored. This should be "
            f"unreachable; if it fires, do not re-run — read the file first.")

    store["last_rendered"] = _stamp(now)
    save(store, state_dir)
    return {"path": path, "shown": len(view["shown"]), "withheld": view["withheld"],
            "replaced_existing_region": had_region, "hand_written_bytes": len(hand_bytes),
            "tripwire": view["tripwire"]}


# --------------------------------------------------------------------------- CLI

def _add_state_dir(parser) -> None:
    # No default is baked into the flag: every test passes one explicitly, because a state-defaulting
    # flag turns an old test into a live writer against the daemon's own store.
    parser.add_argument("--state-dir", default=None,
                        help="override seneschal/state/ (tests MUST pass this)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="The work-item register — the ONE writer of state/open-loops.json.")
    _add_state_dir(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="mint a work item (every required flag below is the mechanism)")
    a.add_argument("--text", required=True)
    a.add_argument("--audience", required=True, choices=AUDIENCES)
    a.add_argument("--terminal-state", required=True,
                   help="the membership test: the event that would close this. No answer ⇒ it is "
                        "a note, not a work item")
    a.add_argument("--whose-move", required=True, choices=WHOSE_MOVE)
    a.add_argument("--next-action", required=True,
                   help="one imperative clause, or the literal \"unknown\"")
    a.add_argument("--kind", required=True, choices=KINDS,
                   help="kind_assistant — the ASSISTANT'S, never the owner's")
    a.add_argument("--priority", required=True, choices=PRIORITIES,
                   help="priority_assistant — the ASSISTANT'S, never the owner's")
    a.add_argument("--origin", default=None)
    a.add_argument("--blocked-by", default=None)
    a.add_argument("--renders-elsewhere", default=None,
                   help="POINT at a store row, never copy it")
    a.add_argument("--because", dest="carried_reason", default=None,
                   help="carried_reason at birth; without one the record drops at the next rebuild")
    a.add_argument("--notes", default=None)
    a.add_argument("--checks", nargs="*", default=None,
                   help="cadence_chain handler names for this row's own chain; omit to use "
                        "cadence_chain.DEFAULT_CHAIN")
    a.add_argument("--id", dest="item_id", default=None)

    for name, helptext in (("carry", "write carried_reason so the record survives a rebuild"),
                           ("start", "status=in_progress — refuses on a terminal record"),
                           ("hold", "status=held, held_since stamped"),
                           ("resolve", "status=done — the terminal_state event happened"),
                           ("drop", "status=dropped — --because REQUIRED")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("item_id")
        s.add_argument("--because", default=None)

    ob_ = sub.add_parser("observe", help="status=observation — REQUIRES a gate "
                                         "(observation-gate-spec.md)")
    ob_.add_argument("item_id")
    ob_.add_argument("--requires", required=True,
                     help="a JSON array of requirement objects, e.g. "
                          '\'[{"type": "min_elapsed", "days": 14, "since": "2026-01-15"}]\' — '
                          "`since` (optional, min_elapsed only) backdates that requirement's own "
                          "clock start instead of counting from the gate's started_at")
    ob_.add_argument("--whose-move-on-complete", default=None, choices=WHOSE_MOVE,
                     help="written to whose_move when the gate is met; omit for the default, both")
    ob_.add_argument("--because", default=None)

    u = sub.add_parser("update", help="edit the assistant's own mutable fields and move "
                                      "last_touched")
    u.add_argument("item_id")
    u.add_argument("--next-action", default=None)
    u.add_argument("--blocked-by", default=None)
    u.add_argument("--kind", default=None, choices=KINDS)
    u.add_argument("--priority", default=None, choices=PRIORITIES)
    u.add_argument("--text", default=None)
    u.add_argument("--renders-elsewhere", default=None)
    u.add_argument("--origin", default=None)
    u.add_argument("--checks", nargs="*", default=None,
                   help="replace this row's cadence_chain handler names; an empty list is stored "
                        "as None, falling back to cadence_chain.DEFAULT_CHAIN")
    u.add_argument("--whose-move", default=None, choices=WHOSE_MOVE,
                   help="correct a row's whose_move after birth (owi_unknowns.py's picker "
                        "resolution path)")

    ls = sub.add_parser("list", help="what is in the register")
    ls.add_argument("--audience", default=None, choices=AUDIENCES)
    ls.add_argument("--status", default=None, choices=RENDERED_STATUSES)
    ls.add_argument("--whose-move", default=None, choices=WHOSE_MOVE,
                    help="a PLAIN equality filter, not the union query (see items())")
    ls.add_argument("--json", action="store_true")

    mn = sub.add_parser("mine", help="the assistant's own default working-memory read — "
                                     "READ-ONLY, changes nothing")
    mn.add_argument("--json", action="store_true")

    sh = sub.add_parser("show", help="one record, verbatim")
    sh.add_argument("item_id")

    r = sub.add_parser("render", help="the carry-over projection — STDOUT unless --write")
    r.add_argument("--audience", default="owner", choices=AUDIENCES)
    r.add_argument("--write", action="store_true",
                   help="replace the generated region of state/carry-over.md IN PLACE")
    r.add_argument("--dormant-after-days", type=int, default=None,
                   help="derive `dormant` from last_touched. NO DEFAULT: the threshold is the "
                        "owner's to choose; without it the scope line says the overlay did not run")
    r.add_argument("--allow-empty", action="store_true",
                   help="…and I really do mean the projection is empty")

    rp = sub.add_parser("report", help="cadence_chain verdicts over the store — READ-ONLY, "
                                       "changes nothing")
    rp.add_argument("--audience", default=None, choices=AUDIENCES)
    rp.add_argument("--status", default=None, choices=RENDERED_STATUSES)
    rp.add_argument("--json", action="store_true")

    ck = sub.add_parser("owner-kind", help="OWNER ONLY — kind_owner")
    ck.add_argument("item_id")
    ck.add_argument("--kind", required=True, choices=KINDS)

    cp = sub.add_parser("owner-priority", help="OWNER ONLY — priority_owner")
    cp.add_argument("item_id")
    cp.add_argument("--priority", required=True, choices=PRIORITIES)

    ca = sub.add_parser("owner-abandon",
                        help="OWNER ONLY — status=abandoned. Takes NO reason, deliberately")
    ca.add_argument("item_id")

    rs = sub.add_parser("raise", help="stamp last_raised_at — the assistant raised this to the "
                                      "owner")
    rs.add_argument("item_id")

    pj = sub.add_parser("project-status", help="register/Notion Tasks Status backlog (Notion "
                                               "backend only) — dry-run by default")
    pj.add_argument("--dry-run", action="store_true",
                    help="the default; accepted explicitly for callers that want to say so")
    pj.add_argument("--apply", action="store_true",
                    help="enqueue the forward for every `mismatch` row (idempotent)")
    pj.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    sd = args.state_dir

    try:
        if args.cmd == "add":
            item = add(sd, text=args.text, audience=args.audience,
                       terminal_state=args.terminal_state, whose_move=args.whose_move,
                       next_action=args.next_action, kind=args.kind, priority=args.priority,
                       origin=args.origin, blocked_by=args.blocked_by,
                       renders_elsewhere=args.renders_elsewhere, notes=args.notes,
                       carried_reason=args.carried_reason, checks=args.checks,
                       item_id=args.item_id)
            print(json.dumps({"ok": True, "id": item["id"]}, ensure_ascii=False))
            return 0
        if args.cmd in ("carry", "start", "hold", "resolve", "drop"):
            verb = {"carry": carry, "start": start, "hold": hold, "resolve": resolve,
                    "drop": drop}[args.cmd]
            item = verb(sd, item_id=args.item_id, because=args.because)
            print(json.dumps({"ok": True, "id": item["id"], "status": item["status"]},
                             ensure_ascii=False))
            return 0
        if args.cmd == "observe":
            try:
                requires = json.loads(args.requires)
            except ValueError as exc:
                raise LoopsError(f"--requires is not valid JSON: {exc}")
            item = observe(sd, item_id=args.item_id, requires=requires,
                           whose_move_on_complete=args.whose_move_on_complete,
                           because=args.because)
            print(json.dumps({"ok": True, "id": item["id"], "status": item["status"]},
                             ensure_ascii=False))
            return 0
        if args.cmd == "update":
            item = update(sd, item_id=args.item_id, next_action=args.next_action,
                          blocked_by=args.blocked_by, kind=args.kind, priority=args.priority,
                          text=args.text, renders_elsewhere=args.renders_elsewhere,
                          origin=args.origin, checks=args.checks, whose_move=args.whose_move)
            print(json.dumps({"ok": True, "id": item["id"]}, ensure_ascii=False))
            return 0
        if args.cmd == "list":
            store = load(sd)
            rows = items(store, audience=args.audience, status=args.status,
                         whose_move=args.whose_move)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(f"{row['id']}\t{row['status']}\t{row['audience']}\t"
                          f"{row.get('whose_move')}\t{row['text']}")
            return 0
        if args.cmd == "mine":
            store = load(sd)
            rows = default_query(store)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                now = _now()
                for row in rows:
                    verdict = cadence_verdict(row, now)["verdict"]
                    ask = ask_line(row) or ""
                    print(f"{row['id']}\t{verdict}\t{row['status']}\t"
                          f"{row.get('whose_move')}\t{row['text']}\t{ask}")
            return 0
        if args.cmd == "show":
            print(json.dumps(get(load(sd), args.item_id), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "render":
            if not args.write:
                print(render_region(load(sd), audience=args.audience,
                                    dormant_after_days=args.dormant_after_days))
                return 0
            result = write_render(sd, audience=args.audience,
                                  dormant_after_days=args.dormant_after_days,
                                  allow_empty=args.allow_empty)
            if result["tripwire"]:
                print(f"loops: TRIPWIRE — {result['shown']} items is past ~{PROJECTION_TRIPWIRE}. "
                      f"Nothing was dropped; re-read the rebuild.", file=sys.stderr)
            print(json.dumps({"ok": True, **result}, ensure_ascii=False))
            return 0
        if args.cmd == "report":
            store = load(sd)
            rows = items(store, audience=args.audience, status=args.status)
            now = _now()
            results = [{"id": row["id"], **cadence_verdict(row, now)} for row in rows]
            if args.json:
                print(json.dumps(results, ensure_ascii=False, indent=2))
            else:
                for r in results:
                    print(f"{r['id']}\t{r['verdict']}\t{r['decided_by']}\t{r['reason']}")
            return 0
        if args.cmd == "raise":
            item = raise_item(sd, item_id=args.item_id)
            print(json.dumps({"ok": True, "id": item["id"],
                              "last_raised_at": item["last_raised_at"]}, ensure_ascii=False))
            return 0
        if args.cmd == "project-status":
            gated = _projection_gate()
            buckets = project_task_status(sd)
            applied = []
            if args.apply:
                for row in buckets["mismatch"]:
                    r = forward_task_status(row["id"], row["status"], state_dir=sd)
                    if r is not None:
                        applied.append({"id": row["id"], "page_id": row["page_id"],
                                        "desired_status": row["desired_status"], "enqueue": r})
            if args.json:
                print(json.dumps({"ok": True, "gated": gated, "buckets": buckets,
                                  "applied": applied}, ensure_ascii=False, indent=2))
            else:
                if gated:
                    print(f"(projection inactive — {gated})")
                for name in ("mismatch", "queued", "dead_letter", "already_forwarded", "no_mapping"):
                    print(f"{name}: {len(buckets[name])}")
                print(f"applied: {len(applied)}" if args.apply
                      else "(dry-run — pass --apply to enqueue every `mismatch` row)")
            return 0
        if args.cmd == "owner-kind":
            owner_kind(sd, item_id=args.item_id, kind=args.kind)
        elif args.cmd == "owner-priority":
            owner_priority(sd, item_id=args.item_id, priority=args.priority)
        else:
            owner_abandon(sd, item_id=args.item_id)
        print(json.dumps({"ok": True, "id": args.item_id}, ensure_ascii=False))
        return 0
    except LoopsError as exc:
        # A refusal is a real sentence and exit 2, kept distinguishable from a disk failure (1) —
        # this directory's convention for a refusal that is a decision rather than a failure.
        print(f"loops: refused — {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "refused": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
