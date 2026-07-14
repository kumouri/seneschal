#!/usr/bin/env python3
"""Place a phone call to the owner via the call-screener Worker's POST /push-call endpoint.

For the rare, can't-miss reminder (meds, a flight, a court date), a silent Telegram line isn't enough —
this actually rings the owner's phone and speaks one short line. Twilio credentials live in the Cloudflare Worker,
not here; this script only needs the Worker's public URL and the bearer secret. Standard library only
(urllib). Sibling of push_sms.py.

CREDENTIALS — never hard-coded. Provide via environment or an --env-file (KEY=VALUE lines):
  PUSH_CALL_URL      full endpoint URL, e.g. https://seneschal-screener.<subdomain>.workers.dev/push-call
  PUSH_CALL_SECRET   the bearer secret (matches `wrangler secret put PUSH_CALL_SECRET` on the Worker)
  PUSH_CALL_TO       optional recipient E.164; omit to let the Worker default to USER_CELL_E164

USAGE:
  python push_call.py --env-file push-call.env --text "It's your assistant. Take your morning meds."
  python push_call.py --env-file push-call.env --body-file line.txt
  python push_call.py --env-file push-call.env --text "hi" --dry-run     # build only, no network

The spoken line should be short and self-contained (it is read once, then the call hangs up). Prints a
one-line JSON result. Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ENV_KEYS = ("PUSH_CALL_URL", "PUSH_CALL_SECRET", "PUSH_CALL_TO")


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
    p.add_argument("--dry-run", action="store_true", help="build the request and print a summary; no network")
    args = p.parse_args()

    text = args.text
    if args.body_file:
        with open(args.body_file, "r", encoding="utf-8") as fh:
            text = fh.read()
    if not text or not text.strip():
        print(json.dumps({"ok": False, "error": "no --text / --body-file provided"}))
        return 2
    text = text.strip()

    creds = load_env(args.env_file)
    url = creds.get("PUSH_CALL_URL")
    secret = creds.get("PUSH_CALL_SECRET")
    to = args.to or creds.get("PUSH_CALL_TO")

    payload: dict = {"text": text}
    if to:
        payload["to"] = to
    if args.escalate:
        payload["escalate"] = True
        if args.interval_sec is not None:
            payload["intervalSec"] = args.interval_sec
        if args.max_attempts is not None:
            payload["maxAttempts"] = args.max_attempts

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "url": url, "to": to,
                          "escalate": bool(args.escalate), "chars": len(text)}))
        return 0

    if not url or not secret:
        print(json.dumps({"ok": False, "error": "missing PUSH_CALL_URL / PUSH_CALL_SECRET (set in env or --env-file)"}))
        return 1

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
            print(json.dumps({"ok": True, "placed": True, "status": resp.status, "response": body[:300], "to": to}))
            return 0
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        print(json.dumps({"ok": False, "error": f"HTTP {e.code}", "detail": detail}))
        return 1
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
