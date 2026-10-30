#!/usr/bin/env python3
"""⏰ **Cadence lint** — Dream step 2k. Stdlib only.

A free-text ⏰ ``Cadence`` field (always so on the filesystem backends; on Notion once the select is
retyped) checks nothing, and the first value nobody enumerated — ``3 per week``, stepped up to 4, 5
and 6 over time — is exactly where a select needed one option per integer, the friction
``../docs/reminder-cadence-mechanism-spec.md`` §1 is about.

A select checked spelling for free. Text checks nothing, so the enforcement moved into code in two
places, and this module is the second:

1. **On write** — every ``Cadence`` value the assistant writes goes through
   ``reminders_cadence.py --canonical`` first, and a refusal is never written
   (``../references/reminders-policy.md`` → "Writing a Cadence").
2. **Nightly** — this. A value that got in anyway (typed in the store's own UI, pasted, or written
   before the rule) is found here, and the owner gets **one Telegram picker per bad row**: the
   parser's suggested fix first and recommended, "leave it", and — when no fix is computable, or it
   is the wrong one — "I'll type it". It never writes the store: a tap reaches the warm session as
   ordinary inbound, and the write happens there, through (1).

**Report-only otherwise, and it never blocks Dream.** An unparseable cadence already FAILS OPEN (due
every day, spec §5.1), so nothing here is urgent: the lint exists so the nag that failing open
produces has a one-tap way out.

## The once-per-value rule (the dedupe)

A bad row is asked about when **any** of these holds, and not otherwise:

* it has never been asked about;
* its value has **changed** since it was asked (a different typo is a different question);
* **``REASK_AFTER_DAYS`` (7) have passed** since the ask.

Seven is not a tuning knob: it is ``telegram_ask.QUESTION_TTL_DAYS``, the age at which the previous
picker **expires and can no longer be tapped**. So an ignored picker is re-sent exactly when the
old one stops working — never two live pickers for one row, and never a row that goes quiet while
still broken (a test pins the two numbers equal). "Leave it" is an answer for a week, not forever:
a value that fails open is firing every day, which is the thing worth re-raising.

The record lives in ``state/cadence-lint.json`` (``{"rows": {key: {value, title, asked_at,
question_id}}}``), keyed on the row id (the title when a row has none). It is written **only after
a landed send** — a failed send is retried the next night, never marked — and a row that now parses,
or is gone from the input, drops out, so a later regression asks afresh.

## Where the rows come from — no new store read

``--rows-json FILE`` (``-`` for stdin): ``[{"id", "title", "cadence"}, ...]``, which Dream writes
from **the Reminders SEED's own active-rows query** — the same data read, never a schema fetch.
Without it, the lint reads ``state/reminders-id-cache.md`` through
``reminders_cadence.id_cache_rows``, the same parse ``--audit`` uses; that is the fallback for a
night the store is unreachable, and the output says which source it read.

Usage::

  python reminders_cadence_lint.py --rows-json rows.json     # Dream step 2k
  python reminders_cadence_lint.py                            # id-cache fallback
  python reminders_cadence_lint.py --dry-run                  # render the pickers, send/write nothing
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import reminders_cadence as rc

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
DEFAULT_TELEGRAM_ENV = os.path.join(SCRIPT_DIR, "telegram.env")
TELEGRAM_ASK = os.path.join(SCRIPT_DIR, "telegram_ask.py")
LEDGER_FILE = "cadence-lint.json"
DREAM_STEP = "2k"

#: = ``telegram_ask.QUESTION_TTL_DAYS`` — re-ask when the last picker can no longer be tapped. Spelled
#: here rather than imported so this module stays cheap to import; ``test_reminders_cadence_lint``
#: pins the two equal.
REASK_AFTER_DAYS = 7

#: ``meta.kind`` on every picker this sends — how a tap is told apart from any other picker's.
ASK_META_KIND = "cadence-lint"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_stamp(text):
    try:
        stamp = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


# --- input --------------------------------------------------------------------------------------

def rows_from_json(text: str) -> list:
    """``[{"id", "title", "cadence"}]`` from Dream's own query. Raises ``ValueError`` on anything
    that is not a list of objects — a malformed hand-off is reported, never read as "no rows"."""
    data = json.loads(text)
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ValueError("--rows-json must be a JSON list of {id, title, cadence} objects")
    rows = []
    for r in data:
        page_id = str(r.get("id") or "").replace("-", "").lower()
        title = str(r.get("title") or "").strip()
        cadence = r.get("cadence")
        rows.append({"id": page_id, "title": title or page_id or "(untitled)",
                     "cadence": cadence if isinstance(cadence, str) else ""})
    return rows


def row_key(row: dict) -> str:
    return row.get("id") or "title:" + row.get("title", "")


# --- the lint -----------------------------------------------------------------------------------

def findings(rows) -> list:
    """Every row whose ``Cadence`` does not parse, with the parser's own message and the fix
    :func:`reminders_cadence.suggest` would recommend (``None`` when there is none)."""
    out = []
    for row in rows:
        try:
            rc.parse(row["cadence"])
            continue
        except rc.CadenceError as exc:
            error = str(exc)
        out.append({"key": row_key(row), "id": row.get("id") or None, "title": row["title"],
                    "cadence": row["cadence"], "error": error,
                    "suggestion": rc.suggest(row["cadence"])})
    return out


def should_ask(entry, finding: dict, now: datetime) -> bool:
    """The once-per-value rule — see the module docstring. An unreadable ``asked_at`` asks: the
    polarity is toward the owner hearing about a broken row, at most once a night."""
    if not isinstance(entry, dict):
        return True
    if entry.get("value") != finding["cadence"]:
        return True
    asked = _parse_stamp(entry.get("asked_at"))
    return asked is None or now - asked >= timedelta(days=REASK_AFTER_DAYS)


def build_ask(finding: dict):
    """``(question, ["Label|Description", ...], meta)`` for one bad row. The first option is the
    recommendation (``telegram_ask`` marks it); every option says what it does and what it costs."""
    title, bad, fix = finding["title"], finding["cadence"], finding["suggestion"]
    shown = bad if bad.strip() else "(empty)"
    question = (f'⏰ "{title}" has a Cadence I can\'t read: "{shown}". Until it\'s fixed it fires '
                f"every day. What should it be?")
    type_it = ("I'll type it|Tell me in chat what it should be. I run it through the cadence parser "
               "and write only a spelling that parses.")
    leave = (f"Leave it|Nothing changes: it keeps firing every day, and I won't ask about "
             f'"{shown}" again for {REASK_AFTER_DAYS} days.')
    if fix:
        extra = (" It also needs its Times set, or it fires only at its Time Window default."
                 if fix == rc.canonical_text("multiple/day") else "")
        options = [f'Change to "{fix}"|I rewrite this row\'s Cadence to "{fix}" in the store, and it '
                   f"fires on that schedule instead of every day.{extra}", type_it, leave]
    else:
        options = [type_it, leave]
    meta = {"kind": ASK_META_KIND, "reminder_id": finding["id"], "title": title, "value": bad,
            "suggestion": fix}
    return question, options, meta


def send_via_cli(question: str, options: list, meta: dict, *, state_dir: str,
                 env_file: str | None, dry_run: bool) -> dict:
    """One picker through ``telegram_ask.py ask`` — its option-description refusal, citation gate,
    default topic and durable record all apply unchanged. Returns the CLI's JSON (``ok`` false on
    any failure, with the reason); never raises."""
    argv = [sys.executable, TELEGRAM_ASK, "--state-dir", state_dir]
    if env_file:
        argv += ["--env-file", env_file]
    argv += ["ask", "--question", question, "--meta", json.dumps(meta, ensure_ascii=False)]
    for opt in options:
        argv += ["--option", opt]
    if dry_run:
        argv.append("--dry-run")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=120)
    except Exception as exc:  # noqa: BLE001 — reported per row, never fatal to the pass
        return {"ok": False, "error": f"telegram_ask did not run: {exc}"}
    try:
        res = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError):
        res = {"ok": False}
    if proc.returncode != 0 or not res.get("ok"):
        res["ok"] = False
        res.setdefault("error", (proc.stderr or proc.stdout or "").strip()[-400:]
                       or f"exit {proc.returncode}")
    return res


def _load_ledger(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"rows": {}}
    if not isinstance(data, dict) or not isinstance(data.get("rows"), dict):
        return {"rows": {}}
    return data


def run(rows, *, state_dir: str, send, now: datetime | None = None,
        dry_run: bool = False) -> dict:
    """One lint pass. ``send(question, options, meta) -> {"ok", "question_id", ...}`` is the seam
    (the CLI passes :func:`send_via_cli`). Writes ``state/cadence-lint.json`` only when not
    ``dry_run``, and only for a landed send."""
    import stateio  # a sibling primitive; imported late so the pure functions above stay light

    now = now or _now()
    path = os.path.join(state_dir, LEDGER_FILE)
    ledger = _load_ledger(path)
    bad = findings(rows)
    report = {"checked": len(rows), "bad": len(bad), "asked": [], "held": [], "errors": [],
              "cleared": []}
    keep = {}
    for f in bad:
        entry = ledger["rows"].get(f["key"])
        if not should_ask(entry, f, now):
            keep[f["key"]] = entry
            report["held"].append({"title": f["title"], "cadence": f["cadence"],
                                   "asked_at": entry.get("asked_at")})
            continue
        question, options, meta = build_ask(f)
        res = send(question, options, meta)
        if not res.get("ok"):
            if isinstance(entry, dict):
                keep[f["key"]] = entry
            report["errors"].append({"title": f["title"], "cadence": f["cadence"],
                                     "error": res.get("error")})
            continue
        report["asked"].append({"title": f["title"], "cadence": f["cadence"],
                                "suggestion": f["suggestion"],
                                "question_id": res.get("question_id")})
        keep[f["key"]] = {"value": f["cadence"], "title": f["title"],
                          "asked_at": now.isoformat(timespec="seconds"),
                          "question_id": res.get("question_id")}
    report["cleared"] = sorted(k for k in ledger["rows"] if k not in keep)
    if not dry_run and (keep != ledger["rows"]):
        stateio.write_json_atomic(path, {"rows": keep})
    return report


# --- CLI ----------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="⏰ Cadence lint — Dream step 2k.")
    ap.add_argument("--rows-json", metavar="FILE",
                    help="the SEED query's rows as [{id, title, cadence}] ('-' = stdin); omit to "
                         "read state/reminders-id-cache.md instead")
    ap.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    ap.add_argument("--env-file", default=None,
                    help="Telegram KEY=VALUE file (default scripts/telegram.env when present)")
    ap.add_argument("--dry-run", action="store_true",
                    help="render every picker; send nothing, write nothing, stamp nothing")
    args = ap.parse_args(argv)

    source = "rows-json" if args.rows_json else "id-cache"
    try:
        if args.rows_json == "-":
            rows = rows_from_json(sys.stdin.read())
        elif args.rows_json:
            with open(args.rows_json, encoding="utf-8") as fh:
                rows = rows_from_json(fh.read())
        else:
            rows = rc.id_cache_rows(args.state_dir)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "source": source, "error": str(exc)}, ensure_ascii=False))
        return 2

    env_file = args.env_file or (DEFAULT_TELEGRAM_ENV if os.path.exists(DEFAULT_TELEGRAM_ENV)
                                 else None)

    def send(question, options, meta):
        return send_via_cli(question, options, meta, state_dir=args.state_dir,
                            env_file=env_file, dry_run=args.dry_run)

    report = run(rows, state_dir=args.state_dir, send=send, dry_run=args.dry_run)
    report.update({"ok": True, "source": source, "dry_run": args.dry_run})
    for f in report["asked"]:
        print("! asked about %r: %r (fix: %r)" % (f["title"], f["cadence"], f["suggestion"]),
              file=sys.stderr)
    for e in report["errors"]:
        print("! could not ask about %r: %s" % (e["title"], e["error"]), file=sys.stderr)
    if not args.dry_run:
        import dream_steps
        dream_steps.record(args.state_dir, DREAM_STEP,
                           note="%d rows (%s), %d bad, %d asked, %d held, %d send errors"
                                % (report["checked"], source, report["bad"], len(report["asked"]),
                                   len(report["held"]), len(report["errors"])))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
