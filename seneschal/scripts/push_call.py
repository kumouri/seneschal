#!/usr/bin/env python3
"""Place a phone call to the owner via the call-screener Worker's POST /push-call endpoint.

For the rare, can't-miss reminder (meds, a flight, a court date), a silent Telegram line isn't enough —
this actually rings the owner's phone and speaks one short line. Twilio credentials live in the Cloudflare Worker,
not here; this script only needs the Worker's public URL and the bearer secret. Standard library only
(urllib). Sibling of push_sms.py.

**`--talk`** (`../docs/voice-call-spec.md`) starts a live voice conversation instead of speaking one
scripted line: the Worker dials the owner, then hands the call to a `RelaySession` running in OWNER
mode (the assistant's to-the-owner register, not the screener's). This script's only job for that
mode is building a compact, best-effort context snapshot (today's calendar, today's pending
reminders, the head of `state/carry-over.md`) and posting `{"mode": "talk", "context": "..."}` — the
Worker enforces that talk mode always rings the owner's own phone regardless of anything this script
sends, and answers 402 when the day's talk budget is spent (`../../phone/README.md`). `../modes/chat.md`
documents the trigger ("call me" / "let's talk on the phone"). Every snapshot source degrades to
nothing on its own: no Google bridge, no queue file, no carry-over — the call still goes through.

**The send gate.** No `--to` override (and every `--talk` call) is the owner's own phone — owner
class, passes untouched, so a Call Me reminder is never slowed. An explicit `--to` needs an approved
row for that number in `state/pending-approvals.json` (`send_gate.py`), or the script prints the
refusal and exits 3 with nothing placed.

CREDENTIALS — never hard-coded. Provide via environment or an --env-file (KEY=VALUE lines):
  PUSH_CALL_URL      full endpoint URL, e.g. https://seneschal-screener.<subdomain>.workers.dev/push-call
  PUSH_CALL_SECRET   the bearer secret (matches `wrangler secret put PUSH_CALL_SECRET` on the Worker)
  PUSH_CALL_TO       optional recipient E.164; omit to let the Worker default to USER_CELL_E164 (not used by --talk)
  PUSH_CALL_CALENDAR_ACCOUNT  optional; the `gcal_api.py --account` label whose calendar the --talk
                     snapshot reads. Omit to use the only account in the Google token store (none, or
                     several → the snapshot skips the calendar)

USAGE:
  python push_call.py --env-file push-call.env --text "It's your assistant. Take your morning meds."
  python push_call.py --env-file push-call.env --body-file line.txt
  python push_call.py --env-file push-call.env --text "hi" --dry-run     # build only, no network
  python push_call.py --env-file push-call.env --talk                   # live conversation, not a script

The spoken line should be short and self-contained (it is read once, then the call hangs up). Prints a
one-line JSON result. Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import clock  # noqa: E402
import paths  # noqa: E402
import send_gate  # noqa: E402
import send_recipients  # noqa: E402
import tz_common  # noqa: E402

ENV_KEYS = ("PUSH_CALL_URL", "PUSH_CALL_SECRET", "PUSH_CALL_TO", "PUSH_CALL_CALENDAR_ACCOUNT")

#: Cap on the talk-mode context snapshot, so the seed POST to the RelaySession Durable Object stays
#: small — this data only ever rides the wire to the owner's own call, never anywhere else.
CONTEXT_MAX_BYTES = 6000

#: How many of today's pending reminders / calendar events to list — plenty for a phone call's worth
#: of context, not a full brief.
CONTEXT_ITEM_LIMIT = 8

#: The Google bridge's env file (`GOOGLE_SETUP.md`). Absent → the snapshot has no calendar section.
GOOGLE_ENV = os.path.join(SCRIPT_DIR, "google.env")


def _calendar_account(creds: dict | None) -> str | None:
    """The account label the snapshot reads: `PUSH_CALL_CALENDAR_ACCOUNT` when set, else the ONLY
    account in the Google token store. None (skip the calendar) when there is none or several —
    guessing between two calendars would read the wrong one as "today"."""
    explicit = (creds or {}).get("PUSH_CALL_CALENDAR_ACCOUNT")
    if explicit:
        return explicit
    try:
        import google_common as gc  # noqa: PLC0415 — only a --talk call pays for the import
        accounts = gc.load_tokens(gc.cfg(gc.load_env(GOOGLE_ENV))["token_store"]).get("accounts") or {}
    except Exception:  # noqa: BLE001 — a snapshot source never blocks the call
        return None
    labels = sorted(accounts)
    return labels[0] if len(labels) == 1 else None


def _todays_calendar(creds: dict | None = None) -> str:
    """Today's calendar (the owner-local calendar day, `gcal_api.py events --start/--end <today>`),
    best-effort — returns "" on any failure (no google.env configured, no account to read, no
    network, a non-zero exit, unparseable output). A talk-mode call never blocks on a calendar it
    can't reach."""
    if not os.path.exists(GOOGLE_ENV):
        return ""
    account = _calendar_account(creds)
    if not account:
        return ""
    today = tz_common.local_today()
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "gcal_api.py"), "events",
             "--account", account, "--env-file", GOOGLE_ENV, "--start", today, "--end", today,
             "--max", str(CONTEXT_ITEM_LIMIT)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return ""
        data = json.loads(proc.stdout.strip())
    except Exception:  # noqa: BLE001 — a snapshot source never blocks the call
        return ""
    if not isinstance(data, dict) or not data.get("ok"):
        return ""
    events = data.get("events") or []
    if not events:
        return "Today's calendar: nothing on it."
    lines = [f"- {e.get('summary') or '(untitled)'} at {e.get('start') or '?'}" for e in events[:CONTEXT_ITEM_LIMIT]]
    return "Today's calendar:\n" + "\n".join(lines)


def _pending_reminders_today() -> str:
    """Today's still-pending (unfired/unsuppressed/unacked) `state/reminders.json` entries — the
    daemon's local fire queue, the same on every store backend — best-effort."""
    path = os.path.join(paths.state_dir(), "reminders.json")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            rows = json.load(fh)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(rows, list):
        return ""
    today = clock.now_local().date()  # the owner's plain calendar date — not the activity-day cut
    pending: list[tuple] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("fired_at") or row.get("suppressed_at") or row.get("acked_at"):
            continue
        text = row.get("text")
        due_at = row.get("due_at")
        if not text or not due_at:
            continue
        instant = clock.parse_iso(due_at)
        if instant is None:
            continue
        local_dt = clock.to_local(instant)
        if local_dt.date() != today:
            continue
        pending.append((local_dt, text))
    if not pending:
        return ""
    pending.sort(key=lambda p: p[0])
    lines = [f"- {text} ({dt.strftime('%I:%M %p').lstrip('0')})" for dt, text in pending[:CONTEXT_ITEM_LIMIT]]
    return "Today's pending reminders:\n" + "\n".join(lines)


def _carry_over_head(max_lines: int = 40) -> str:
    """The first `max_lines` of `state/carry-over.md` — the assistant's open loops — best-effort."""
    path = os.path.join(paths.state_dir(), "carry-over.md")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception:  # noqa: BLE001
        return ""
    head = "".join(lines[:max_lines]).strip()
    return f"From carry-over (open loops):\n{head}" if head else ""


def build_context_snapshot(creds: dict | None = None) -> str:
    """Best-effort talk-mode context: today's calendar, today's pending reminders, and the head of
    carry-over.md — each section skipped independently on any failure, capped to CONTEXT_MAX_BYTES."""
    sections = [s for s in (_todays_calendar(creds), _pending_reminders_today(), _carry_over_head()) if s]
    text = "\n\n".join(sections)
    encoded = text.encode("utf-8")
    if len(encoded) > CONTEXT_MAX_BYTES:
        text = encoded[:CONTEXT_MAX_BYTES].decode("utf-8", "ignore") + "\n…(truncated)"
    return text


def load_env(env_file: str | None) -> dict:
    """Start from a KEY=VALUE file (if given); environment variables take precedence."""
    values: dict = {}
    if env_file:
        with open(env_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    for k in ENV_KEYS:
        if os.environ.get(k):
            values[k] = os.environ[k]
    return values


def main() -> int:
    p = argparse.ArgumentParser(description="Place a phone call via the call-screener /push-call endpoint.")
    p.add_argument("--text", help="line to speak on the call")
    p.add_argument("--body-file", help="read the spoken line from a file")
    p.add_argument("--to", help="recipient E.164 (default: PUSH_CALL_TO, else the Worker's USER_CELL_E164)")
    p.add_argument("--env-file", help="KEY=VALUE file with PUSH_CALL_* settings (kept untracked)")
    p.add_argument("--escalate", action="store_true",
                   help="keep calling back until the owner presses a digit (Worker-side loop). Default single call.")
    p.add_argument("--interval-sec", type=int, default=None,
                   help="seconds between escalation callbacks (Worker default 120); only with --escalate")
    p.add_argument("--max-attempts", type=int, default=None,
                   help="max escalation calls before giving up (Worker default 15); only with --escalate")
    p.add_argument("--talk", action="store_true",
                   help="start a live voice conversation with the assistant instead of speaking one "
                        "scripted line (mode=talk); always rings the owner's own phone and carries a compact context "
                        "snapshot — incompatible with --text/--body-file/--to/--escalate")
    p.add_argument("--dry-run", action="store_true", help="build the request and print a summary; no network")
    args = p.parse_args()

    creds = load_env(args.env_file)
    url = creds.get("PUSH_CALL_URL")
    secret = creds.get("PUSH_CALL_SECRET")

    if args.talk:
        if args.text or args.body_file or args.to or args.escalate:
            print(json.dumps({"ok": False,
                              "error": "--talk cannot be combined with --text/--body-file/--to/--escalate"}))
            return 2
        to = None  # talk mode always rings the owner; the Worker enforces this too
        payload: dict = {"mode": "talk", "context": build_context_snapshot(creds)}
    else:
        text = args.text
        if args.body_file:
            with open(args.body_file, "r", encoding="utf-8") as fh:
                text = fh.read()
        if not text or not text.strip():
            print(json.dumps({"ok": False, "error": "no --text / --body-file provided"}))
            return 2
        text = text.strip()

        to = args.to or creds.get("PUSH_CALL_TO")
        payload = {"text": text}
        if to:
            payload["to"] = to
        if args.escalate:
            payload["escalate"] = True
            if args.interval_sec is not None:
                payload["intervalSec"] = args.interval_sec
            if args.max_attempts is not None:
                payload["maxAttempts"] = args.max_attempts

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "url": url, "to": to, "talk": bool(args.talk),
                          "escalate": bool(args.escalate),
                          "chars": len(payload["text"]) if "text" in payload else None,
                          "context_bytes": len(payload["context"].encode("utf-8")) if "context" in payload else None}))
        return 0

    if not url or not secret:
        print(json.dumps({"ok": False, "error": "missing PUSH_CALL_URL / PUSH_CALL_SECRET (set in env or --env-file)"}))
        return 1

    try:
        cls = send_recipients.classify_single_recipient(to)
    except Exception:  # noqa: BLE001 — a classification failure logs unknown, never raises into the send
        cls = "unknown"
    # The send gate: no --to override is the owner's own phone (owner, passes — a Call Me reminder
    # is never slowed); an explicit override needs an approved row for that number (send_gate.py).
    verdict = send_gate.require_approval("call", to, recipient_class=cls, channel="push_call")
    send_recipients.record("push_call", cls, gate=verdict)
    if not verdict["allowed"]:
        print(json.dumps(send_gate.refusal_payload(verdict, to=to)))
        return send_gate.EXIT_REFUSED

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        # Set an explicit User-Agent: Cloudflare bans the default "Python-urllib/x.y"
        # signature with error 1010 (403) before the request reaches the Worker. Any
        # non-default UA passes; use an honest one that names this client.
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
            "User-Agent": "seneschal-push-call/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            print(json.dumps({"ok": True, "placed": True, "status": resp.status, "response": body[:300],
                              "to": to, "talk": bool(args.talk)}))
            return 0
    except urllib.error.HTTPError as e:
        with e:  # an HTTPError IS the response; close it rather than leave the connection open
            detail = e.read().decode("utf-8", "replace")[:300]
        out = {"ok": False, "error": f"HTTP {e.code}", "detail": detail}
        if e.code == 402:
            # The Worker's shared DAILY_BUDGET_USD cap is spent — nothing was dialed. Say so plainly
            # rather than leave "HTTP 402" for the caller to decode.
            out["budget_exhausted"] = True
        print(json.dumps(out))
        return 1
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
