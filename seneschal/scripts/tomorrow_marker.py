#!/usr/bin/env python3
"""Tomorrow's Lead — the first-class "prioritize tomorrow" marker (`../docs/tomorrow-marker-spec.md`;
its §6 open questions are all decided, each as the spec's option A).

**What this is not.** Not carry-over (which rebuilds every run and prunes on resolution — §1 of the
spec), not a priority-field rewrite, not a reminder-ladder change, not a second reminders database. A
bare mark with nothing linked never nudges; see the spec §5.

## The store

One durable file, `state/tomorrow.json` (gitignored runtime state), one schema (`SCHEMA`), written
ONLY through `stateio.write_json_atomic` — never a bare `open(p, "w")` (`check_state_writes.py
--enforce` fails the build on that: a half-written state file exists nowhere else and is
unrecoverable). `items` is a flat list; nothing here ever deletes a row — `close`/`drop` set
`status`, and only `prune()` (Dream's to call, not wired here — see the spec's Retention section)
removes a `done`/`dropped` row once `resolved_at` is more than `PRUNE_AFTER_DAYS` old. An `open`/
`rolled` item is NEVER pruned by age (the spec's own argument: a thing dropped without anyone asking
is worse than a thing that stays visible).

## `for_date`

Computed the same way every other "today" in this repo is — the owner's configured timezone and
day boundary (`activity_day.today`, via `tz_common`/`clock`) — **plus one day** (spec §2.1): a mark
typed at 01:00 local on Tuesday, while the after-midnight rule still calls "today" Monday, means
Monday's tomorrow. Every fresh mark (any of the
three doors) defaults to this; only `roll()` advances an existing item's own `for_date` by one day
from whatever it already was, not from "today" — a rolled item that has already earned its spot
keeps `order` unchanged (spec §4.2).

## Ordering

`order` is simply "whatever is already queued for that `for_date`, plus one" (spec §2.2/§2.3) —
append-only. A chat turn typing several `<tomorrow>` tags in one message calls `mark()` once per tag
in the order the owner typed them, and since `order` is derived fresh from the current max each call, that
sequence of calls IS the sentence order with no extra parameter needed.

## The Wrap resolution and the ask-high proposal

`close()`/`roll()`/`drop()` only ever touch THIS store — none of them writes to the data store. A `close` on
an item whose `linked_kind` is `task`/`reminder` returns a `proposal` string (a suggested Task/
Reminder `Done` flip) rather than writing one: flipping the linked row is ask-high, exactly as it
already is everywhere else in this tree (`../references/reminders-policy.md` -> "Act-low vs
ask-high") — never an automatic write from a picker tap.

## The Wrap's two pickers

`ask_wrap_grid`/`apply_wrap_grid` are the per-item Close/Roll/Drop grid (spec §4.2);
`ask_lead_grid`/`apply_lead_answer` are the separate "what's tomorrow's lead?" multi-select (spec
§2.3), which only ever PROMOTES candidates the Wrap already gathered — it never accepts freeform text
(spec §6.6: decided no). A tap on either comes back through the daemon's picker-callback path, keyed
on the `meta.kind` constants below; this module never sends on its own initiative.

## Phase 2 — Notion reconciliation (Notion backend only)

`reconcile()` folds `Tomorrow = true` Tasks/⏰ Reminders rows into this store — see its own docstring
for the idempotency key and the fail-open contract. It only has rows to fold on the Notion backend,
where the `Tomorrow` checkbox lives; on a filesystem backend nothing calls it. No stdlib Notion bridge
exists in this tree, so (matching `brief_prestage.py`'s own split) a model turn runs the live query
and hands this function the rows; it never queries Notion itself.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import activity_day  # noqa: E402
import paths  # noqa: E402
import stateio  # noqa: E402

STORE_FILE = "tomorrow.json"
SCHEMA = "seneschal.tomorrow-marker/1"

#: Spec §2 — where a mark came from. Audit only; nothing branches behavior on it except the
#: reconciliation dedupe key (Phase 2).
SOURCES = ("chat_tag", "notion_task", "notion_reminder", "wrap_picker")

#: Spec §4.2's three Wrap resolutions.
STATUS_OPEN = "open"
STATUS_ROLLED = "rolled"
STATUS_DONE = "done"
STATUS_DROPPED = "dropped"
ACTIVE_STATUSES = (STATUS_OPEN, STATUS_ROLLED)
TERMINAL_STATUSES = (STATUS_DONE, STATUS_DROPPED)

#: Spec §3 Retention — only a terminal item ages out, and only this long past its own `resolved_at`.
PRUNE_AFTER_DAYS = 3

#: `--meta`'s `kind` for the two Wrap-time Telegram pickers this module drives — read by the
#: daemon's callback dispatch to route a tap here rather than to a sibling picker's clause.
ASK_META_KIND_WRAP = "tomorrow-marker-wrap"
ASK_META_KIND_LEAD = "tomorrow-marker-lead"

#: Spec §4.2 — the per-item grid choices, in order. Index 0/1/2 map to close/roll/drop; changing the
#: ORDER would silently remap every grid already sent to the owner's phone, so treat this list as append-only.
WRAP_CHOICES = ["Close (done)", "Roll to tomorrow", "Drop"]
_CHOICE_TO_ACTION = {"Close (done)": "close", "Roll to tomorrow": "roll", "Drop": "drop"}


class TomorrowMarkerError(ValueError):
    """A caller asked for something the store cannot honor — an unknown id, a bad action, a bad
    `for_date`. Never raised for "nothing to do" (an empty batch, a zero count) — those are ordinary
    empty answers, not errors."""


# --------------------------------------------------------------------------- the store

def _path(state_dir: str | None) -> str:
    return os.path.join(paths.state_dir(state_dir), STORE_FILE)


def load(state_dir: str | None = None) -> dict:
    """The store, or a fresh empty one if it doesn't exist yet or fails to parse — a corrupt/missing
    file is never a crash, it is an empty day's plan (fail-open, matching every `state/` reader in
    this tree)."""
    try:
        with open(_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"schema": SCHEMA, "items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"schema": SCHEMA, "items": []}
    return data


def save(state_dir: str | None, store: dict) -> None:
    store["schema"] = SCHEMA
    stateio.write_json_atomic(_path(state_dir), store)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _tomorrow(now: datetime | None = None) -> str:
    """Spec §2.1's `for_date` rule: the owner's activity day (their zone, after-midnight cut) plus one."""
    return (activity_day.today(now) + timedelta(days=1)).isoformat()


def _advance_one_day(for_date: str) -> str:
    return (date.fromisoformat(for_date) + timedelta(days=1)).isoformat()


def _next_order(items: list, for_date: str) -> int:
    existing = [i.get("order") or 0 for i in items if i.get("for_date") == for_date]
    return (max(existing) + 1) if existing else 1


def _next_id(items: list, now: datetime | None = None) -> str:
    day = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    prefix = f"tmw-{day}-"
    n = sum(1 for i in items if str(i.get("id") or "").startswith(prefix)) + 1
    return f"{prefix}{n}"


def get(store: dict, item_id: str) -> dict | None:
    return next((i for i in store["items"] if i.get("id") == item_id), None)


# --------------------------------------------------------------------------- mutation

def mark(state_dir: str | None, text: str, *, for_date: str | None = None,
        source: str = "chat_tag", linked_kind: str | None = None,
        linked_page_id: str | None = None, now: datetime | None = None) -> dict:
    """Add one item to the plan. `for_date` defaults to `_tomorrow()` (every door does this — spec
    §2) — pass it explicitly only for a reconciled Notion row or a rolled-forward re-mark. `source`
    is audit-only (`SOURCES`); an unrecognized value is refused rather than silently accepted, since
    it feeds nothing but is still worth catching at the door."""
    text = (text or "").strip()
    if not text:
        raise TomorrowMarkerError("mark() needs non-empty text")
    if source not in SOURCES:
        raise TomorrowMarkerError(f"unrecognized source {source!r}; must be one of {SOURCES}")
    store = load(state_dir)
    fd = for_date or _tomorrow(now)
    item = {
        "id": _next_id(store["items"], now),
        "text": text,
        "for_date": fd,
        "order": _next_order(store["items"], fd),
        "source": source,
        "linked_kind": linked_kind,
        "linked_page_id": linked_page_id,
        "status": STATUS_OPEN,
        "created_at": _stamp(now),
        "resolved_at": None,
    }
    store["items"].append(item)
    save(state_dir, store)
    return item


def _resolve(state_dir: str | None, item_id: str, *, action: str,
            now: datetime | None = None) -> dict:
    store = load(state_dir)
    item = get(store, item_id)
    if item is None:
        raise TomorrowMarkerError(f"no such tomorrow-marker item: {item_id!r}")
    if item.get("status") in TERMINAL_STATUSES:
        raise TomorrowMarkerError(
            f"{item_id!r} is already {item['status']!r} — resolve is for an open/rolled item")
    proposal = None
    if action == "close":
        item["status"] = STATUS_DONE
        item["resolved_at"] = _stamp(now)
        if item.get("linked_kind") in ("task", "reminder"):
            # ASK-HIGH — never an automatic Notion write from a picker tap (spec §4.2/§5).
            proposal = (f"propose flipping the linked {item['linked_kind']} "
                        f"({item.get('linked_page_id')}) to Done — the owner's call, not a direct flip")
    elif action == "roll":
        item["status"] = STATUS_ROLLED
        item["for_date"] = _advance_one_day(item["for_date"])
        # order is deliberately UNCHANGED (spec §4.2: "it still leads, having already earned the
        # spot once").
    elif action == "drop":
        item["status"] = STATUS_DROPPED
        item["resolved_at"] = _stamp(now)
    else:
        raise TomorrowMarkerError(f"unrecognized action {action!r}")
    save(state_dir, store)
    result = {"ok": True, "id": item_id, "status": item["status"], "item": item}
    if proposal:
        result["proposal"] = proposal
    return result


def close(state_dir: str | None, item_id: str, *, now: datetime | None = None) -> dict:
    return _resolve(state_dir, item_id, action="close", now=now)


def roll(state_dir: str | None, item_id: str, *, now: datetime | None = None) -> dict:
    return _resolve(state_dir, item_id, action="roll", now=now)


def drop(state_dir: str | None, item_id: str, *, now: datetime | None = None) -> dict:
    return _resolve(state_dir, item_id, action="drop", now=now)


def prune(state_dir: str | None = None, now: datetime | None = None,
         keep_days: int = PRUNE_AFTER_DAYS) -> int:
    """Drop `done`/`dropped` items whose `resolved_at` is more than `keep_days` old. An `open`/
    `rolled` item is NEVER touched, by construction — this function never even looks at `for_date`
    for one. Dream's to call (spec §3 Retention); nothing in this tree calls it yet."""
    store = load(state_dir)
    cutoff = (now or datetime.now(timezone.utc))
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    cutoff = cutoff - timedelta(days=keep_days)
    survivors, dropped = [], 0
    for item in store["items"]:
        if item.get("status") in TERMINAL_STATUSES and item.get("resolved_at"):
            try:
                resolved = datetime.fromisoformat(item["resolved_at"].replace("Z", "+00:00"))
            except ValueError:
                survivors.append(item)
                continue
            if resolved < cutoff:
                dropped += 1
                continue
        survivors.append(item)
    if dropped:
        store["items"] = survivors
        save(state_dir, store)
    return dropped


# --------------------------------------------------------------------------- reads

def for_day(state_dir: str | None, for_date: str, *, statuses=ACTIVE_STATUSES) -> list:
    """Every item for `for_date` in `statuses`, in stored `order` — never re-sorted by anything the
    caller thinks it knows (spec §4.1: "rendered by code and printed verbatim")."""
    store = load(state_dir)
    rows = [i for i in store["items"]
            if i.get("for_date") == for_date and i.get("status") in statuses]
    rows.sort(key=lambda i: (i.get("order") or 0, i.get("id") or ""))
    return rows


BRIEF_HEADING = "🎯 Tomorrow's Lead"


def brief_line(state_dir: str | None = None, *, for_date: str | None = None,
              now: datetime | None = None) -> str | None:
    """The Brief's rendered section (spec §4.1) — `None` when there is nothing marked for today, so
    the caller omits the section entirely rather than printing "nothing marked" (`modes/brief.md`'s
    existing empty-section rule). Sits ABOVE "Needs You": it is the day's own stated plan, not a
    subset of what needs a decision."""
    fd = for_date or activity_day.today(now).isoformat()
    rows = for_day(state_dir, fd)
    if not rows:
        return None
    lines = [BRIEF_HEADING]
    for row in rows:
        lines.append(f"- {row['text']}")
    return "\n".join(lines)


def render_for_wrap(state_dir: str | None = None, *, for_date: str | None = None,
                    now: datetime | None = None) -> list:
    """The Wrap's per-item grid rows (spec §4.2) — `[{"id":..., "label":...}, ...]`, empty when
    nothing is due today. `for_date` defaults to TODAY (the day the Wrap is closing out), never
    tomorrow — the items being resolved are the ones that were supposed to lead *today*."""
    fd = for_date or activity_day.today(now).isoformat()
    return [{"id": r["id"], "label": r["text"]} for r in for_day(state_dir, fd)]


# --------------------------------------------------------------------------- the two Wrap pickers

def ask_wrap_grid(state_dir: str, env_file: str | None, *, for_date: str | None = None,
                  chat_id: str | None = None, dry_run: bool = False,
                  now: datetime | None = None, api=None) -> dict:
    """Compose + send the Close/Roll/Drop grid over today's due items (spec §4.2) — never a prose
    list. `{"ok": True, "sent": False, "reason": ...}` when there is nothing to ask about, same shape
    every sibling picker in this tree uses for "nothing to do"."""
    import telegram_ask as ta  # local import: only the sending path needs Telegram config
    items = render_for_wrap(state_dir, for_date=for_date, now=now)
    if not items:
        return {"ok": True, "sent": False, "reason": "nothing marked for today"}
    c = ta.cfg(ta.load_env(env_file))
    if chat_id:
        c["chat_id"] = chat_id
    plural = "s" if len(items) != 1 else ""
    question = f"Tomorrow's Lead — {len(items)} item{plural} to close out. Close, roll, or drop each?"
    return ta.ask_grid(c, paths.state_dir(state_dir), question, items, WRAP_CHOICES, dry_run=dry_run,
                       api=api, now=now, meta={"kind": ASK_META_KIND_WRAP, "count": len(items)})


def apply_wrap_grid(state_dir: str | None, resolution: dict, *, now: datetime | None = None) -> dict:
    """Write each `{item_id: choice_label}` pair through `close`/`roll`/`drop`. Per-item errors,
    never an aborted batch (`owi_unknowns.apply`'s own posture) — a row the owner never tapped is
    left untouched, never guessed. `proposals` collects every ask-high linked-Done suggestion a Close
    produced, for the caller to relay to the owner — never written automatically."""
    resolution = resolution or {}
    results, proposals = {}, []
    for item_id, choice in resolution.items():
        action = _CHOICE_TO_ACTION.get(choice)
        if action is None:
            results[item_id] = {"ok": False, "error": f"unrecognized choice {choice!r}"}
            continue
        try:
            res = _resolve(state_dir, item_id, action=action, now=now)
        except TomorrowMarkerError as e:
            results[item_id] = {"ok": False, "error": str(e)}
            continue
        results[item_id] = res
        if res.get("proposal"):
            proposals.append(res["proposal"])
    return {"ok": True, "results": results, "proposals": proposals}


def ask_lead_grid(state_dir: str, env_file: str | None, candidates: list, *,
                  chat_id: str | None = None, dry_run: bool = False,
                  now: datetime | None = None, api=None) -> dict:
    """Compose + send the "what's tomorrow's lead?" multi-select (spec §2.3) over candidates the
    Wrap already gathered (due Tasks / near-due Deadline Watches) — this door PROMOTES what the
    assistant already knows about, it never accepts freeform text (spec §6.6: decided no). `candidates` is
    `[{"id": <notion page id or None>, "text": ..., "why": ..., "linked_kind": "task"|"reminder"|None}]`
    — `why` is the required per-option description (`telegram_ask.parse_option`'s own rule: every
    option needs one)."""
    import telegram_ask as ta
    if not candidates:
        return {"ok": True, "sent": False, "reason": "nothing to promote"}
    c = ta.cfg(ta.load_env(env_file))
    if chat_id:
        c["chat_id"] = chat_id
    options = [{"label": cand["text"], "description": cand.get("why") or "from today's gather"}
              for cand in candidates]
    return ta.ask(c, paths.state_dir(state_dir), "Anything here should lead tomorrow?", options,
                 multi=True, dry_run=dry_run, api=api, now=now,
                 meta={"kind": ASK_META_KIND_LEAD, "candidates": candidates})


def apply_lead_answer(state_dir: str | None, meta: dict, selected: list, *,
                      now: datetime | None = None) -> dict:
    """A Done tap on the lead multi-select: `mark()` every selected candidate, `source="wrap_picker"`
    (spec §2.3). `selected` is the list of chosen indices into `meta["candidates"]`, exactly the shape
    `telegram_ask.resolve` hands back for a classic (non-grid) multi-select."""
    candidates = (meta or {}).get("candidates") or []
    marked = []
    for i in selected or []:
        if not isinstance(i, int) or i < 0 or i >= len(candidates):
            continue
        cand = candidates[i]
        item = mark(state_dir, cand.get("text") or "", source="wrap_picker",
                   linked_kind=cand.get("linked_kind"), linked_page_id=cand.get("id"), now=now)
        marked.append(item)
    return {"ok": True, "marked": marked}


# --------------------------------------------------------------------------- Phase 2 — Notion reconciliation

def reconcile(state_dir: str | None, source_kind: str, rows: list, *,
             now: datetime | None = None) -> dict:
    """Fold Notion rows where `Tomorrow = true` into the store (spec §2.2/§7 Phase 2). `source_kind`
    is `"task"` or `"reminder"`; `rows` is `[{"page_id": ..., "text": ..., "url": ...}, ...]`, already
    filtered live by the caller's own Notion query — this function never queries Notion itself (no
    stdlib bridge exists, matching `brief_prestage.py`'s own split: a model turn reads Notion, a
    script does the idempotent write). Notion backend only — see the module docstring.

    **Idempotent, keyed on `page_id`** — a page already carrying a NON-terminal marker item is left
    alone (its checkbox still needs unticking, since a still-`true` box after this pass means the
    prior untick never landed, but no second marker item is created); everything else, this pass
    always returns `page_id` for the CALLER to untick (Notion is a third-party write this module
    never makes). **Fails open on an empty/absent `rows`** — an unreadable or missing `Tomorrow`
    property upstream must never block the Brief or Wrap; the caller's own query simply returns
    nothing to fold in."""
    if source_kind not in ("task", "reminder"):
        raise TomorrowMarkerError(f"source_kind must be 'task' or 'reminder', got {source_kind!r}")
    source = "notion_task" if source_kind == "task" else "notion_reminder"
    store = load(state_dir)
    untick, upserted = [], []
    for row in rows or []:
        page_id = row.get("page_id")
        if not page_id:
            continue
        untick.append(page_id)
        already = any(i.get("linked_page_id") == page_id and i.get("status") in ACTIVE_STATUSES
                     for i in store["items"])
        if already:
            continue
        fd = _tomorrow(now)
        item = {
            "id": _next_id(store["items"], now),
            "text": (row.get("text") or "").strip() or page_id,
            "for_date": fd,
            "order": _next_order(store["items"], fd),
            "source": source,
            "linked_kind": source_kind,
            "linked_page_id": page_id,
            "status": STATUS_OPEN,
            "created_at": _stamp(now),
            "resolved_at": None,
        }
        store["items"].append(item)
        upserted.append(item["id"])
    if upserted:
        save(state_dir, store)
    return {"ok": True, "upserted": upserted, "untick": untick}


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Tomorrow's Lead marker store.")
    p.add_argument("--state-dir", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mark", help="add one item to the plan")
    m.add_argument("text")
    m.add_argument("--for-date", default=None)
    m.add_argument("--source", default="chat_tag", choices=SOURCES)
    m.add_argument("--linked-kind", default=None, choices=("task", "reminder"))
    m.add_argument("--linked-page-id", default=None)

    for name, fn in (("close", close), ("roll", roll), ("drop", drop)):
        sp = sub.add_parser(name, help=f"{name} one item by id")
        sp.add_argument("item_id")
        sp.set_defaults(_fn=fn)

    lst = sub.add_parser("list", help="items for a date (default: today), READ-ONLY")
    lst.add_argument("--for-date", default=None)
    lst.add_argument("--all-statuses", action="store_true")

    sub.add_parser("brief-line", help="the Brief's rendered section, or nothing")

    sub.add_parser("render-for-wrap", help="today's due items as grid rows, JSON")

    pr = sub.add_parser("prune", help="drop terminal items older than --keep-days")
    pr.add_argument("--keep-days", type=int, default=PRUNE_AFTER_DAYS)

    wa = sub.add_parser("wrap-ask", help="send the Close/Roll/Drop grid")
    wa.add_argument("--env-file", default=None)
    wa.add_argument("--chat-id", default=None)
    wa.add_argument("--dry-run", action="store_true")

    wap = sub.add_parser("wrap-apply", help="apply a {item_id: choice} resolution")
    wap.add_argument("resolution", help='JSON object, e.g. \'{"tmw-...": "Close (done)"}\'')

    la = sub.add_parser("lead-ask", help="send the 'what's tomorrow's lead?' multi-select")
    la.add_argument("candidates", help="JSON list of {id, text, why, linked_kind}")
    la.add_argument("--env-file", default=None)
    la.add_argument("--chat-id", default=None)
    la.add_argument("--dry-run", action="store_true")

    lap = sub.add_parser("lead-apply", help="mark every selected candidate")
    lap.add_argument("meta", help="JSON {candidates: [...]}")
    lap.add_argument("selected", help="JSON list of selected indices")

    rec = sub.add_parser("reconcile", help="fold Tomorrow=true Notion rows into the store")
    rec.add_argument("source_kind", choices=("task", "reminder"))
    rec.add_argument("rows", help="JSON list of {page_id, text, url}")

    a = p.parse_args(argv)
    sd = a.state_dir

    if a.cmd == "mark":
        print(json.dumps(mark(sd, a.text, for_date=a.for_date, source=a.source,
                              linked_kind=a.linked_kind, linked_page_id=a.linked_page_id),
                         ensure_ascii=False))
        return 0
    if a.cmd in ("close", "roll", "drop"):
        try:
            print(json.dumps(a._fn(sd, a.item_id), ensure_ascii=False))
        except TomorrowMarkerError as e:
            print(f"tomorrow_marker: {e}", file=sys.stderr)
            return 2
        return 0
    if a.cmd == "list":
        fd = a.for_date or activity_day.today().isoformat()
        statuses = None if a.all_statuses else ACTIVE_STATUSES
        rows = for_day(sd, fd) if statuses is None else for_day(sd, fd, statuses=statuses)
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if a.cmd == "brief-line":
        line = brief_line(sd)
        if line:
            print(line)
        return 0
    if a.cmd == "render-for-wrap":
        print(json.dumps(render_for_wrap(sd), ensure_ascii=False))
        return 0
    if a.cmd == "prune":
        print(json.dumps({"pruned": prune(sd, keep_days=a.keep_days)}, ensure_ascii=False))
        return 0
    if a.cmd == "wrap-ask":
        print(json.dumps(ask_wrap_grid(sd, a.env_file, chat_id=a.chat_id, dry_run=a.dry_run),
                         ensure_ascii=False))
        return 0
    if a.cmd == "wrap-apply":
        print(json.dumps(apply_wrap_grid(sd, json.loads(a.resolution)), ensure_ascii=False))
        return 0
    if a.cmd == "lead-ask":
        print(json.dumps(ask_lead_grid(sd, a.env_file, json.loads(a.candidates), chat_id=a.chat_id,
                                       dry_run=a.dry_run), ensure_ascii=False))
        return 0
    if a.cmd == "lead-apply":
        print(json.dumps(apply_lead_answer(sd, json.loads(a.meta), json.loads(a.selected)),
                         ensure_ascii=False))
        return 0
    if a.cmd == "reconcile":
        print(json.dumps(reconcile(sd, a.source_kind, json.loads(a.rows)), ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
