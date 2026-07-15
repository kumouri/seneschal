#!/usr/bin/env python3
"""Push a short SMS to the owner via the call-screener Worker's POST /push-sms endpoint.

Twilio credentials live in the Cloudflare Worker, not here — this script only needs the Worker's
public URL and the bearer secret. Standard library only (urllib).

CREDENTIALS — never hard-coded. Provide via environment or an --env-file (KEY=VALUE lines):
  PUSH_SMS_URL      full endpoint URL, e.g. https://seneschal-screener.<subdomain>.workers.dev/push-sms
  PUSH_SMS_SECRET   the bearer secret (matches `wrangler secret put PUSH_SMS_SECRET` on the Worker)
  PUSH_SMS_TO       optional recipient E.164; omit to let the Worker default to USER_CELL_E164

USAGE:
  python push_sms.py --env-file push-sms.env --text "Morning brief: 2 things need you."
  python push_sms.py --env-file push-sms.env --body-file highlights.txt
  python push_sms.py --env-file push-sms.env --text "hi" --dry-run     # build only, no network

Prints a one-line JSON result. Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ENV_KEYS = ("PUSH_SMS_URL", "PUSH_SMS_SECRET", "PUSH_SMS_TO")


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
    p = argparse.ArgumentParser(description="Push a short SMS via the call-screener /push-sms endpoint.")
    p.add_argument("--text", help="message text")
    p.add_argument("--body-file", help="read message text from a file")
    p.add_argument("--to", help="recipient E.164 (default: PUSH_SMS_TO, else the Worker's USER_CELL_E164)")
    p.add_argument("--env-file", help="KEY=VALUE file with PUSH_SMS_* settings (kept untracked)")
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
    url = creds.get("PUSH_SMS_URL")
    secret = creds.get("PUSH_SMS_SECRET")
    to = args.to or creds.get("PUSH_SMS_TO")

    payload: dict = {"text": text}
    if to:
        payload["to"] = to

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "url": url, "to": to, "chars": len(text)}))
        return 0

    if not url or not secret:
        print(json.dumps({"ok": False, "error": "missing PUSH_SMS_URL / PUSH_SMS_SECRET (set in env or --env-file)"}))
        return 1

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        # Explicit User-Agent: Cloudflare 1010-bans the default "Python-urllib/x.y"
        # signature (403) before the Worker sees it. Any non-default UA passes.
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
            "User-Agent": "seneschal-push-sms/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            print(json.dumps({"ok": True, "sent": True, "status": resp.status, "response": body[:300], "to": to}))
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
