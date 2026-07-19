#!/usr/bin/env python3
"""CLI for the durable **Notion write-behind outbox** — enqueue work, drain it, inspect it.

The outbox makes the assistant's act-low Notion writes durable: journal the write **here first**
(survives a reboot), then flush it to Notion. The flush is done by the warm/scheduled LLM turn via the
MCP tools (option (a) — this CLI never talks to Notion; ``outbox_common`` never does either). A
**Notion-backend** component: filesystem store backends write locally and atomically and never route
through it. See ``../docs/notion-write-behind-outbox-spec.md``.

Verbs:
  ack           enqueue a reminder ack (⏰ row → Status/Last Acknowledged/Consecutive Misses/untick Ack)
  medlog        enqueue a med-intake row (a create in the 💊 Med Intake Log)
  enqueue       enqueue any write intent (generic: --op/--target-kind/--target-id/--payload/--key)
  pull          claim ready entries and print them as JSON — the drain's "give me work" step
  mark          resolve a claimed entry: --done | --retry | --dead-letter
  status        counts, oldest-pending age, and the dead-letter list (add --json for machine form)
  prune         drop DONE entries older than N days (Dream housekeeping; dead-letters never pruned)

Every verb prints a one-line JSON summary. Stdlib only.

The drain loop a turn runs (option a):
  python outbox.py pull --json                         # claim ready entries
  # …for each: replay op+payload via the matching MCP tool (notion-update-page / notion-create-pages)…
  python outbox.py mark --id <id> --done [--notion-page-id <pid>]     # on success
  python outbox.py mark --id <id> --retry --error "429"              # transient: back off + retry
  python outbox.py mark --id <id> --dead-letter --error "404 gone"   # permanent: surface it
"""
import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import outbox_common as ob  # noqa: E402

try:  # owner-local ack dates (guarded — presence.py house style)
    import tz_common as _tz_common  # noqa: E402
except ImportError:  # pragma: no cover — behave like an unconfigured install (machine-local dates)
    _tz_common = None


def _default_ack_date() -> str:
    """Today as ``YYYY-MM-DD`` on the **owner's** calendar (house rule: date/day-boundary logic uses the
    owner's timezone, via ``tz_common``, so the outbox key and the ``acks.json`` ledger stamp the same
    day). Machine-local when ``tz_common`` is unavailable — the unconfigured-install behavior."""
    if _tz_common is not None:
        return _tz_common.local_today()
    from datetime import datetime  # noqa: PLC0415 — only needed on the fallback path
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _out(obj) -> int:
    print(json.dumps(obj, ensure_ascii=False))
    return 0 if obj.get("ok", True) else 1


def _load_payload(args):
    if getattr(args, "payload_file", None):
        with open(args.payload_file, "r", encoding="utf-8") as fh:
            return json.load(fh)
    if getattr(args, "payload", None):
        return json.loads(args.payload)
    return {}


def cmd_ack(conn, args) -> int:
    """Convenience enqueue for the marquee case — a reminder ack. Fixed payload (Done / today / 0 /
    untick), key ``ack:<row>:<date>`` so a repeat ack the same day is a no-op."""
    date = args.ack_date or _default_ack_date()
    payload = {"reminder_id": args.reminder_id, "status": args.status,
               "last_acknowledged": date, "consecutive_misses": 0, "untick_ack": True}
    r = ob.enqueue(conn, "ack_reminder", "page", args.reminder_id, payload,
                   ob.ack_key(args.reminder_id, date))
    return _out(r)


def cmd_medlog(conn, args) -> int:
    """Convenience enqueue for a med-intake row (a create). Mints a per-dose intent if none is given, so
    the key dedups replays of *this* enqueue, not distinct doses."""
    intent = args.intent or ob.new_intent()
    payload = {"name": args.name, "dose_mg": args.dose_mg, "taken_at": args.taken_at}
    if args.set:
        payload["set"] = args.set
    r = ob.enqueue(conn, "med_log", "db", args.collection, payload, ob.medlog_key(intent))
    r["intent"] = intent
    return _out(r)


def cmd_enqueue(conn, args) -> int:
    """Generic enqueue — the escape hatch for run_log_finalize / reminder_status / anything not covered
    by a convenience verb. Caller supplies the idempotency key."""
    try:
        payload = _load_payload(args)
    except (OSError, ValueError) as e:
        return _out({"ok": False, "error": f"bad payload: {e}"})
    try:
        r = ob.enqueue(conn, args.op, args.target_kind, args.target_id, payload, args.idempotency_key)
    except ValueError as e:
        return _out({"ok": False, "error": str(e)})
    return _out(r)


def cmd_pull(conn, args) -> int:
    """Claim up to --limit ready entries for the drain (FIFO, oldest first), printing what to flush."""
    claimed = ob.claim_ready(conn, limit=args.limit)
    return _out({"ok": True, "claimed": [
        {"id": c["id"], "op": c["op"], "target_kind": c["target_kind"], "target_id": c["target_id"],
         "payload": c["payload"], "attempts": c["attempts"], "idempotency_key": c["idempotency_key"]}
        for c in claimed]})


def cmd_mark(conn, args) -> int:
    if args.done:
        r = ob.mark_done(conn, args.id, notion_page_id=args.notion_page_id)
        verb = "done"
    elif args.retry:
        r = ob.mark_retry(conn, args.id, args.error or "transient", retry_after=args.retry_after)
        verb = "retry"
    elif args.dead_letter:
        if not args.error:
            return _out({"ok": False, "error": "--dead-letter requires --error"})
        r = ob.mark_failed(conn, args.id, args.error)
        verb = "dead-letter"
    else:
        return _out({"ok": False, "error": "give one of --done / --retry / --dead-letter"})
    if r is None:
        return _out({"ok": False, "error": f"no entry with id {args.id!r}"})
    return _out({"ok": True, "id": args.id, "marked": verb, "status": r["status"],
                 "attempts": r["attempts"], "not_before": r.get("not_before")})


def cmd_status(conn, args) -> int:
    s = ob.stats(conn)
    if args.json:
        return _out({"ok": True, **s})
    c = s["counts"]
    age = s["oldest_pending_age_sec"]
    age_str = "—" if age is None else f"{age / 60:.1f} min"
    lines = [
        f"outbox: {c[ob.PENDING]} pending · {c[ob.INFLIGHT]} inflight · {c[ob.DONE]} done · "
        f"{c[ob.FAILED]} dead-letter    (oldest un-landed: {age_str})",
    ]
    for d in s["dead_letters"]:
        lines.append(f"  ✗ {d['id'][:8]} {d['op']} → {d['target_id']}  "
                     f"[{d['attempts']} attempts] {d.get('last_error') or ''}")
    print("\n".join(lines))
    return 0


def cmd_prune(conn, args) -> int:
    removed = ob.prune_done(conn, older_than_days=args.days)
    return _out({"ok": True, "pruned": removed, "older_than_days": args.days})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Durable Notion write-behind outbox.")
    p.add_argument("--state-dir", default=ob.DEFAULT_STATE_DIR, help="dir holding notion-outbox.sqlite")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ack", help="enqueue a reminder ack")
    a.add_argument("--reminder-id", required=True, help="the ⏰ row page id")
    a.add_argument("--ack-date", help="local ack date YYYY-MM-DD (default: owner-local today)")
    a.add_argument("--status", default="Done", choices=["Done", "Finished"],
                   help="Done = done-for-today (default); Finished = explicit retire")
    a.set_defaults(func=cmd_ack)

    m = sub.add_parser("medlog", help="enqueue a med-intake row (create)")
    m.add_argument("--collection", required=True, help="the 💊 Med Intake Log collection id")
    m.add_argument("--name", required=True)
    m.add_argument("--dose-mg", type=float, required=True)
    m.add_argument("--taken-at", required=True, help="ISO instant the dose was taken")
    m.add_argument("--set", help="optional label, e.g. 'usual-am'")
    m.add_argument("--intent", help="dedup token (default: freshly minted per dose)")
    m.set_defaults(func=cmd_medlog)

    e = sub.add_parser("enqueue", help="enqueue any write intent (generic)")
    e.add_argument("--op", required=True, choices=ob.OPS)
    e.add_argument("--target-kind", required=True, choices=ob.TARGET_KINDS)
    e.add_argument("--target-id", required=True)
    e.add_argument("--idempotency-key", required=True)
    e.add_argument("--payload", help="inline JSON payload")
    e.add_argument("--payload-file", help="read the JSON payload from a file")
    e.set_defaults(func=cmd_enqueue)

    pl = sub.add_parser("pull", help="claim ready entries for the drain (JSON)")
    pl.add_argument("--limit", type=int, default=25)
    pl.add_argument("--json", action="store_true", help="(default output is already JSON)")
    pl.set_defaults(func=cmd_pull)

    mk = sub.add_parser("mark", help="resolve a claimed entry")
    mk.add_argument("--id", required=True)
    g = mk.add_mutually_exclusive_group(required=True)
    g.add_argument("--done", action="store_true")
    g.add_argument("--retry", action="store_true", help="transient failure: back off + retry")
    g.add_argument("--dead-letter", action="store_true", help="permanent failure: surface, stop retrying")
    mk.add_argument("--notion-page-id", help="(with --done) the created/updated row id")
    mk.add_argument("--error", help="(with --retry/--dead-letter) the failure message")
    mk.add_argument("--retry-after", type=float, help="(with --retry) server Retry-After seconds")
    mk.set_defaults(func=cmd_mark)

    st = sub.add_parser("status", help="counts, oldest-pending age, dead-letters")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    pr = sub.add_parser("prune", help="drop DONE entries older than N days")
    pr.add_argument("--days", type=int, default=14)
    pr.set_defaults(func=cmd_prune)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    conn = ob.connect(args.state_dir)
    try:
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
