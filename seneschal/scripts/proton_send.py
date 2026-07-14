#!/usr/bin/env python3
"""Send an email as assistant@example.com via Proton Mail Bridge (local SMTP). Standard library only.

Proton has no public mail API. Proton Mail Bridge runs locally and exposes a standard SMTP server
(default 127.0.0.1:1025, STARTTLS) authenticated with a Bridge-generated username/password. This script
talks to that local SMTP endpoint — so the assistant can send as its own address.

CREDENTIALS — never hard-coded. Provide via environment or an --env-file (KEY=VALUE lines):
  PROTON_SMTP_HOST       default 127.0.0.1
  PROTON_SMTP_PORT       default 1025
  PROTON_BRIDGE_USER     the Bridge username for the assistant@example.com account (often the address)
  PROTON_BRIDGE_PASS     the Bridge-generated password (NOT your Proton login password)
  PROTON_SENDER          From address, default assistant@example.com
  PROTON_SMTP_STARTTLS   "1" (default) to STARTTLS; "0" for plain (not recommended)
  PROTON_SMTP_INSECURE   "1" to accept Bridge's self-signed cert (default "1"; Bridge is local)

Bridge setup is documented in EMAIL_SETUP.md.

USAGE:
  python proton_send.py --to t@example.com --subject "Hi" --body "text"
  python proton_send.py --to a@x.com --cc assistant@example.com --subject S --body-file b.txt --html-file b.html
  python proton_send.py --dry-run --to t@x.com --subject "Hi" --body "text"   # build only, no network
  python proton_send.py --check-auth                                          # connect + login only, no send

Prints a one-line JSON result. Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

ENV_KEYS = (
    "PROTON_SMTP_HOST", "PROTON_SMTP_PORT", "PROTON_BRIDGE_USER", "PROTON_BRIDGE_PASS",
    "PROTON_SENDER", "PROTON_SMTP_STARTTLS", "PROTON_SMTP_INSECURE",
)


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


def cfg(creds: dict) -> dict:
    return {
        "host": creds.get("PROTON_SMTP_HOST", "127.0.0.1"),
        "port": int(creds.get("PROTON_SMTP_PORT", "1025")),
        "user": creds.get("PROTON_BRIDGE_USER"),
        "password": creds.get("PROTON_BRIDGE_PASS"),
        "sender": creds.get("PROTON_SENDER", "assistant@example.com"),
        "starttls": creds.get("PROTON_SMTP_STARTTLS", "1") != "0",
        "insecure": creds.get("PROTON_SMTP_INSECURE", "1") != "0",
    }


def build_message(to, cc, subject, body_text, body_html, sender):
    if body_html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body_text or "", "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))
    else:
        msg = MIMEText(body_text or "", "plain", "utf-8")
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject or ""
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="example.com")
    return msg


def _connect(c: dict) -> smtplib.SMTP:
    server = smtplib.SMTP(c["host"], c["port"], timeout=30)
    if c["starttls"]:
        ctx = ssl.create_default_context()
        if c["insecure"]:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        server.starttls(context=ctx)
    if not c["user"] or not c["password"]:
        raise RuntimeError("Missing PROTON_BRIDGE_USER / PROTON_BRIDGE_PASS (set in env or --env-file)")
    server.login(c["user"], c["password"])
    return server


def main() -> int:
    p = argparse.ArgumentParser(description="Send an email as assistant@example.com via Proton Bridge (SMTP).")
    p.add_argument("--to", action="append", default=[], help="recipient (repeatable, or comma-separated)")
    p.add_argument("--cc", action="append", default=[], help="cc (repeatable, or comma-separated)")
    p.add_argument("--subject", default="")
    p.add_argument("--body", help="plain-text body")
    p.add_argument("--body-file", help="read plain-text body from a file")
    p.add_argument("--html-file", help="optional HTML body from a file")
    p.add_argument("--from", dest="sender", help="From address (default assistant@example.com / PROTON_SENDER)")
    p.add_argument("--env-file", help="KEY=VALUE file with PROTON_* settings (kept untracked)")
    p.add_argument("--dry-run", action="store_true", help="build the message and print a summary; no network")
    p.add_argument("--check-auth", action="store_true", help="connect + login only; do not send")
    args = p.parse_args()

    def split(items):
        out = []
        for it in items:
            out.extend([x.strip() for x in it.split(",") if x.strip()])
        return out

    to, cc = split(args.to), split(args.cc)

    body_text = args.body
    if args.body_file:
        with open(args.body_file, "r", encoding="utf-8") as fh:
            body_text = fh.read()
    body_html = None
    if args.html_file:
        with open(args.html_file, "r", encoding="utf-8") as fh:
            body_html = fh.read()

    creds = load_env(args.env_file)
    c = cfg(creds)
    if args.sender:
        c["sender"] = args.sender

    if args.check_auth:
        try:
            server = _connect(c)
            server.quit()
            print(json.dumps({"ok": True, "check_auth": "Bridge SMTP login OK", "host": c["host"], "port": c["port"]}))
            return 0
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1

    if not to:
        print(json.dumps({"ok": False, "error": "no --to recipient provided"}))
        return 2

    msg = build_message(to, cc, args.subject, body_text, body_html, c["sender"])

    if args.dry_run:
        print(json.dumps({
            "ok": True, "dry_run": True, "from": c["sender"], "to": to, "cc": cc,
            "subject": args.subject, "html": bool(body_html), "bytes": len(msg.as_bytes()),
        }))
        return 0

    try:
        server = _connect(c)
        server.send_message(msg, from_addr=c["sender"], to_addrs=to + cc)
        server.quit()
        print(json.dumps({"ok": True, "sent": True, "from": c["sender"], "to": to, "cc": cc}))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e), "to": to, "cc": cc}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
