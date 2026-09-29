#!/usr/bin/env python3
"""**The turn that has nothing new to say** — the drainer's third outcome, and its audit trail.
Standard library only. `../docs/substance-or-silence-spec.md` §6 phase 2.

## What this is for

The assistant should not reply to EVERY message it receives. When the owner 👍-acks several reminders
in quick succession, one turn does all the work; without this module each later ack spends a
warm-session turn just to say it has already been done — several replies in under a minute, none
of which carries anything new.

The relay cannot know at relay time. An ack's own store write can land well after the ack was
relayed, inside a turn that had not yet started. The fact only exists at **drain** time.

## What a suppression IS, exactly

A queue entry whose text is a `_reaction_line` the daemon **synthesized itself**, for a ⏰ row whose
**every** owed store write has already landed. Nothing else is eligible. The unit is the queue
entry, never a sentence inside a reply — chat turns are strictly serialized, one entry per turn, so
*"the entire turn is a restatement"* and *"this one synthesized line"* are the same object (§7.4).

**It goes 3 → 1, not 3 → 0, and that is the design.** The first turn's reply is the report that the
⏰ row landed. If a successful write were silent, a *failed* one would be the only thing that ever
spoke, and the whole reason a 👍 is worth anything is that failure is audible against a background
of confirmations. §7.1 is that constraint, and in the mechanism it is not a rule anybody follows — it
is `reaction_ack_fully_landed`'s conjunction: an entry that is not `done` **is** the not-yet-landed
case, and the turn relays.

## The polarity, which is the whole safety argument

**The harm is a suppressed message the owner needed, not an extra one they didn't.** So every path
here fails toward SENDING: no side-map entry, an unreadable outbox, an id that will not normalize, a
raise anywhere, a daemon restart that loses the map — all relay, which is the default behaviour.
There is exactly one way to reach silence, and it requires an affirmative reading from a store that
only records a row after it has actually written through.

**Optional predicate.** The landed-check lives in `reminders_acks.reaction_ack_fully_landed`, imported
lazily inside :func:`verdict`'s try. Where that predicate is absent (or the store backend keeps no
write-behind journal to answer it), the import or call fails and every verdict is `suppress: False`
— the module degrades to "always relay", never to "sometimes drop".

**Structurally out of reach:** `🚨 Critical`, `🛑 Super-Critical` and `Call Me` do not come out of
this door at all — reminders fire from `sentinel.check_reminders`, a different Mouth door from the
chat drainer (`mouth-spec.md` §3.1). That is not a promise this module makes; it is which code path
the change lives in. Job-completion reports, questions and approval asks are likewise not
`_reaction_line`s, so none is eligible. There is deliberately ONE caller: a second one is how a
mechanism scoped to one queue entry becomes a general mute.

## Why it is audited, and why the log is append-only

**A suppression nobody can see is how a silent-drop defect ships.** Every one appends a row to
`state/suppressed-turns.jsonl` naming the ⏰ row, the verdict that licensed it, how old the landing
was, and **the withheld line itself** — so a human can count them and read exactly what was not said.
There is no prune: these are rare (one per redundant reaction relay), and the one thing you would
want on the day this mechanism is doubted is the whole history.

`!private` is honoured: the row carries a tombstone rather than the text. A reaction line is
daemon-synthesized and cannot carry the prefix today, but the redaction is applied rather than
assumed.

USAGE (the CLI is how a human counts these):
  python turn_suppression.py tail --limit 20
  python turn_suppression.py count [--days 14]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
SUPPRESSION_LOG = "suppressed-turns.jsonl"
SCHEMA = "seneschal.suppressed-turn/1"

#: How many (queued text -> ⏰ row) pairs the side map holds. Sized like `presence.INBOUND_ID_CAP`
#: and for the same reason: it is a WINDOW over what is currently queued, not a store. The durable
#: queue rarely holds more than a handful, and evicting the oldest costs a relay — today's behaviour
#: — which is the direction everything here fails in.
REACTION_ACK_CAP = 64


# --------------------------------------------------------------------------- the side map

def remember(acks: dict, text: str, reminder_id) -> None:
    """Record that this queued **text** is the relay of an ack on this ⏰ row, evicting oldest-first.

    Keyed by the text and not by an id, because the text is the only handle the drainer has: it pops
    `(channel, text, attempts)` and `ackable_nudge` computed the reminder id three layers away, in
    the inbound-line builder. The same handle the drainer's other queue-matching helpers key on.

    **Called AFTER the force-route transform**, exactly like `presence.remember_inbound_id`: the
    drainer matches this string against the durable queue, so a key recorded before the transform
    would never match the entry it belongs to.

    §6 option 2, chosen over widening the queue tuple. It does **not** survive a restart, and that is
    acceptable in exactly one direction: a lost entry means the turn is relayed."""
    if not text or reminder_id in (None, ""):
        return
    acks[text] = str(reminder_id)
    while len(acks) > REACTION_ACK_CAP:
        acks.pop(next(iter(acks)))


def take(acks: dict, text: str):
    """The ⏰ row this queued text relays, removing it. ``None`` when there is none.

    **Consumed rather than peeked, on every drain of that entry — suppressed or not.** A turn that
    ran and then failed to deliver is requeued, and on the retry the mapping is gone, so it relays.
    That is the fail-open direction and it is deliberate: a delivery failure is not evidence that the
    write landed, and re-deciding suppression against a store that has moved on since is how a retry
    would quietly become a drop."""
    if not text:
        return None
    return acks.pop(text, None)


# --------------------------------------------------------------------------- the verdict

def verdict(state_dir: str, reminder_id, now: datetime | None = None) -> dict:
    """Should this queued reaction relay be suppressed? ``{"suppress": bool, ...}``, never raises.

    A thin seam over `reminders_acks.reaction_ack_fully_landed` — thin on purpose, so the predicate's
    correctness is arguable in one place (its own tests) and this module owns only the *decision* to
    act on it. **The `sources` distinction is the whole of it**: only the `notion-outbox` arm counts,
    because the `ledger` arm is stamped by the daemon's own dequeue and is positive the instant the
    👍 lands — a gate on that would suppress the one turn that has to run.

    An import failure, a missing id, a raise: all `suppress: False`, which relays."""
    out = {"suppress": False, "why": "unknown", "key": None, "detail": None}
    if reminder_id in (None, ""):
        out["why"] = "no-reminder-id"
        return out
    try:
        import reminders_acks as ra
        landed = ra.reaction_ack_fully_landed(state_dir, reminder_id, now)
        out["key"] = landed.get("key")
        out["why"] = landed.get("why")
        out["detail"] = landed
        out["suppress"] = bool(landed.get("landed"))
        return out
    except Exception as e:  # noqa: BLE001 — a broken gate relays; it may never be why the owner wasn't told
        out["why"] = f"raised: {e}"
        return out


# --------------------------------------------------------------------------- the audit trail

def log_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or DEFAULT_STATE_DIR, SUPPRESSION_LOG)


def record(state_dir: str | None, *, channel: str, text: str, reminder_id,
           detail: dict | None = None, turn_id: str | None = None,
           redacted: bool = False, now: datetime | None = None) -> bool:
    """Append one `seneschal.suppressed-turn/1` row. Returns True iff it hit disk; **never raises.**

    Same contract as `mouth.record_assertion`, and here it points
    the other way round: for those, a failed append costs the row and never the message. Here the
    message is what is *not* being sent, so a failed append costs the only evidence the suppression
    happened — which is worse, and is why the caller logs a line beside this rather than relying on
    it alone.

    `text` is **the withheld line, verbatim**, because *"with enough to tell what was withheld"* is
    the point of the file. A `!private` turn writes a tombstone instead, the same rule `turns.py`
    applies to a redacted turn."""
    try:
        state_dir = state_dir or DEFAULT_STATE_DIR
        row = {
            "schema": SCHEMA,
            "at": (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
            "channel": str(channel or "unknown"),
            "reminder_id": str(reminder_id or ""),
            "withheld": "[redacted — !private]" if redacted else (
                text if isinstance(text, str) else str(text)),
            "redacted": bool(redacted),
        }
        if turn_id:
            row["turn_id"] = str(turn_id)
        if isinstance(detail, dict):
            # The verdict verbatim: which arms answered and the `why` string. A reader disputing a suppression needs the reasons, not the conclusion.
            row["verdict"] = detail
        os.makedirs(state_dir, exist_ok=True)
        with open(log_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception:  # noqa: BLE001 — see the docstring
        return False


def read(state_dir: str | None = None, limit: int | None = None,
         since: datetime | None = None) -> list:
    """Rows oldest-first, malformed lines skipped, `limit` keeping the **newest** N. Fail-open: an
    absent or unreadable log reads empty."""
    rows: list = []
    try:
        with open(log_path(state_dir), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if since is not None:
                    try:
                        at = datetime.fromisoformat(str(row.get("at")).replace("Z", "+00:00"))
                    except (TypeError, ValueError):
                        continue
                    if at < since:
                        continue
                rows.append(row)
    except OSError:
        return []
    if limit is not None and limit >= 0:
        rows = rows[-limit:] if limit else []
    return rows


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="What the daemon decided not to say, and why.")
    ap.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tail", help="the newest suppressions, verbatim")
    t.add_argument("--limit", type=int, default=20)
    c = sub.add_parser("count", help="how many, and for which rows")
    c.add_argument("--days", type=int, default=14)
    args = ap.parse_args(argv)

    if args.cmd == "tail":
        rows = read(args.state_dir, limit=args.limit)
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        print(json.dumps({"ok": True, "shown": len(rows)}, ensure_ascii=False))
        return 0

    since = datetime.now(timezone.utc) - timedelta(days=max(0, args.days))
    rows = read(args.state_dir, since=since)
    by_row: dict = {}
    for row in rows:
        by_row[row.get("reminder_id") or "?"] = by_row.get(row.get("reminder_id") or "?", 0) + 1
    print(json.dumps({"ok": True, "days": args.days, "suppressed": len(rows), "by_reminder": by_row},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
