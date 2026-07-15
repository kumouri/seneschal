#!/usr/bin/env python3
"""Read recent mail from assistant@example.com via Proton Mail Bridge (local IMAP). Standard library only.

Proton Mail Bridge exposes a local IMAP server (default 127.0.0.1:1143, STARTTLS) authenticated with a
Bridge-generated username/password. This script lists/reads messages so the assistant's email-triage skill can
summarize and categorize the inbox. Read-only — it never deletes or sends.

CREDENTIALS — never hard-coded. Provide via environment or an --env-file (KEY=VALUE lines):
  PROTON_IMAP_HOST       default 127.0.0.1
  PROTON_IMAP_PORT       default 1143
  PROTON_BRIDGE_USER     the Bridge username for the assistant@example.com account
  PROTON_BRIDGE_PASS     the Bridge-generated password (NOT your Proton login password)
  PROTON_IMAP_STARTTLS   "1" (default) to STARTTLS; "0" for plain
  PROTON_IMAP_INSECURE   "1" to accept Bridge's self-signed cert (default "1"; Bridge is local)

USAGE:
  python proton_read.py --check-auth                       # connect + login only
  python proton_read.py --mailbox INBOX --unseen --limit 20
  python proton_read.py --mailbox INBOX --since 2026-06-25 --limit 50 --bodies

Prints one-line JSON: {"ok":true,"count":N,"messages":[{uid,from,to,subject,date,seen,snippet}, ...]}.
With --bodies, each message includes a truncated plain-text "body". Exit 0 on success.
"""
from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import ssl
import sys
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

ENV_KEYS = (
    "PROTON_IMAP_HOST", "PROTON_IMAP_PORT", "PROTON_BRIDGE_USER", "PROTON_BRIDGE_PASS",
    "PROTON_IMAP_STARTTLS", "PROTON_IMAP_INSECURE",
)


def load_env(env_file: str | None) -> dict:
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


def cfg(creds: dict) -> dict:
    return {
        "host": creds.get("PROTON_IMAP_HOST", "127.0.0.1"),
        "port": int(creds.get("PROTON_IMAP_PORT", "1143")),
        "user": creds.get("PROTON_BRIDGE_USER"),
        "password": creds.get("PROTON_BRIDGE_PASS"),
        "starttls": creds.get("PROTON_IMAP_STARTTLS", "1") != "0",
        "insecure": creds.get("PROTON_IMAP_INSECURE", "1") != "0",
    }


def _decode(s) -> str:
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:  # noqa: BLE001
        return str(s)


def _connect(c: dict) -> imaplib.IMAP4:
    if not c["user"] or not c["password"]:
        raise RuntimeError("Missing PROTON_BRIDGE_USER / PROTON_BRIDGE_PASS (set in env or --env-file)")
    M = imaplib.IMAP4(c["host"], c["port"])
    if c["starttls"]:
        ctx = ssl.create_default_context()
        if c["insecure"]:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        M.starttls(ssl_context=ctx)
    M.login(c["user"], c["password"])
    return M


def _plain_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""


def main() -> int:
    p = argparse.ArgumentParser(description="Read recent mail via Proton Bridge (IMAP), read-only.")
    p.add_argument("--mailbox", default="INBOX")
    p.add_argument("--unseen", action="store_true", help="only unread messages")
    p.add_argument("--since", help="only messages on/after this date (YYYY-MM-DD)")
    p.add_argument("--limit", type=int, default=25, help="max messages (newest first)")
    p.add_argument("--bodies", action="store_true", help="include truncated plain-text bodies")
    p.add_argument("--snippet-len", type=int, default=200)
    p.add_argument("--body-len", type=int, default=2000)
    p.add_argument("--env-file", help="KEY=VALUE file with PROTON_* settings (kept untracked)")
    p.add_argument("--check-auth", action="store_true", help="connect + login only; no fetch")
    args = p.parse_args()

    c = cfg(load_env(args.env_file))

    try:
        M = _connect(c)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    try:
        if args.check_auth:
            print(json.dumps({"ok": True, "check_auth": "Bridge IMAP login OK", "host": c["host"], "port": c["port"]}))
            return 0

        M.select(args.mailbox, readonly=True)
        crit = []
        if args.unseen:
            crit.append("UNSEEN")
        if args.since:
            # IMAP wants DD-Mon-YYYY
            import datetime
            d = datetime.datetime.strptime(args.since, "%Y-%m-%d")
            crit.extend(["SINCE", d.strftime("%d-%b-%Y")])
        if not crit:
            crit = ["ALL"]
        typ, data = M.search(None, *crit)
        uids = data[0].split()
        uids = uids[-args.limit:][::-1]  # newest first

        messages = []
        for uid in uids:
            typ, msg_data = M.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            body = _plain_body(msg) if args.bodies else ""
            snippet = " ".join(_plain_body(msg).split())[: args.snippet_len]
            try:
                date_iso = parsedate_to_datetime(msg.get("Date")).isoformat()
            except Exception:  # noqa: BLE001
                date_iso = msg.get("Date", "")
            item = {
                "uid": uid.decode(),
                "from": _decode(msg.get("From")),
                "to": _decode(msg.get("To")),
                "subject": _decode(msg.get("Subject")),
                "date": date_iso,
                "snippet": snippet,
            }
            if args.bodies:
                item["body"] = body[: args.body_len]
            messages.append(item)

        print(json.dumps({"ok": True, "mailbox": args.mailbox, "count": len(messages), "messages": messages}))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
