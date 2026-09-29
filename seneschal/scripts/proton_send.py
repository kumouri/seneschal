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
  python proton_send.py --to t@x.com --subject S --body b --attach spec.md --attach report.pdf
  python proton_send.py --dry-run --to t@x.com --subject "Hi" --body "text"   # build only, no network
  python proton_send.py --check-auth                                          # connect + login only, no send

**`--attach PATH`, repeatable.** A file too big or too awkward to paste goes as a real attachment
instead of being inlined into the body. A path is resolved and read **before the send gate and
before any connection is opened**: a missing/unreadable file, or a total attachment size over
:data:`MAX_ATTACHMENT_BYTES` (20 MB — Proton's own cap is 25 MB; this leaves headroom), is refused
locally (exit 2, JSON error) rather than failing partway through an SMTP conversation. **With no
`--attach`, the message is byte-identical to a build without the feature** — `build_message` only
wraps the alternative(text/html) part in an outer `multipart/mixed` when there is something to
attach. The send gate is untouched either way: an attachment is not a recipient.

**The send gate.** Before any connection, every `--to`/`--cc` address is classified against the
owner's own addresses (`send_recipients.classify_emails`) and passed through
`send_gate.require_approval`: an owner-only send passes untouched, anything else needs an approved
row in `state/pending-approvals.json`, or the script prints the refusal and exits 3 with nothing
sent.

Prints a one-line JSON result. Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import smtplib
import ssl
import sys
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

import send_gate
import send_recipients

#: Proton's own attachment cap is 25 MB; this leaves headroom for MIME/base64 overhead and any
#: body text, so a send that would land right at Proton's limit is refused here instead of by SMTP.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


class AttachmentError(ValueError):
    """A `--attach` path that can't be read, or a total over :data:`MAX_ATTACHMENT_BYTES` — always
    raised and refused BEFORE the send gate and before any connection is opened."""


def load_attachments(paths: list[str]) -> list[dict]:
    """Read every `--attach` path into memory and refuse before any network touches this send.

    Each entry carries `path`/`name` (the basename — the one name the recipient sees)/`data`/`mime`
    (guessed via `mimetypes`, falling back to `application/octet-stream`)/`size`. Raises
    :class:`AttachmentError` on a missing/unreadable path or a total over the cap — never partially
    builds a message."""
    attachments = []
    total = 0
    for p in paths:
        if not os.path.isfile(p):
            raise AttachmentError(f"attachment not found: {p}")
        try:
            with open(p, "rb") as fh:
                data = fh.read()
        except OSError as e:
            raise AttachmentError(f"attachment unreadable: {p} ({e})") from e
        mime, _ = mimetypes.guess_type(p)
        mime = mime or "application/octet-stream"
        total += len(data)
        attachments.append({"path": p, "name": os.path.basename(p), "data": data,
                            "mime": mime, "size": len(data)})
    if total > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(
            f"attachments total {total} bytes, over the {MAX_ATTACHMENT_BYTES}-byte (20 MB) cap "
            f"(Proton's own limit is 25 MB; this leaves headroom)")
    return attachments


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


def build_message(to, cc, subject, body_text, body_html, sender, attachments=None):
    if body_html:
        body = MIMEMultipart("alternative")
        body.attach(MIMEText(body_text or "", "plain", "utf-8"))
        body.attach(MIMEText(body_html, "html", "utf-8"))
    else:
        body = MIMEText(body_text or "", "plain", "utf-8")

    if attachments:
        # Only wrapped when there's something to attach — with none, `msg` IS `body`, so the
        # message shape stays byte-identical to before attachments existed.
        msg = MIMEMultipart("mixed")
        msg.attach(body)
        for att in attachments:
            maintype, _, subtype = att["mime"].partition("/")
            part = MIMEBase(maintype or "application", subtype or "octet-stream")
            part.set_payload(att["data"])
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=att["name"])
            msg.attach(part)
    else:
        msg = body

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
    p.add_argument("--attach", action="append", default=[], dest="attach", metavar="PATH",
                   help="attach a file (repeatable). Refused BEFORE the send gate and before any "
                        "connection: a missing/unreadable path, or a total over "
                        f"{MAX_ATTACHMENT_BYTES} bytes (20 MB — Proton's own cap is 25 MB).")
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

    try:
        attachments = load_attachments(args.attach) if args.attach else []
    except AttachmentError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2

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

    msg = build_message(to, cc, args.subject, body_text, body_html, c["sender"], attachments=attachments)

    if args.dry_run:
        print(json.dumps({
            "ok": True, "dry_run": True, "from": c["sender"], "to": to, "cc": cc,
            "subject": args.subject, "html": bool(body_html), "bytes": len(msg.as_bytes()),
            "attachments": [{"name": a["name"], "size": a["size"], "mime": a["mime"]}
                            for a in attachments],
        }))
        return 0

    try:
        cls = send_recipients.classify_emails(*to, *cc)
    except Exception:  # noqa: BLE001 — a classification failure logs unknown, never raises into the send
        cls = "unknown"
    # The send gate: owner-class passes untouched; anything else needs an approved row in
    # state/pending-approvals.json (send_gate.py). Fail-closed — refused means nothing is sent.
    verdict = send_gate.require_approval("email", to + cc, recipient_class=cls, channel="proton_email")
    send_recipients.record("proton_email", cls, gate=verdict)
    if not verdict["allowed"]:
        print(json.dumps(send_gate.refusal_payload(verdict, to=to, cc=cc)))
        return send_gate.EXIT_REFUSED

    try:
        server = _connect(c)
        server.send_message(msg, from_addr=c["sender"], to_addrs=to + cc)
        server.quit()
        print(json.dumps({"ok": True, "sent": True, "from": c["sender"], "to": to, "cc": cc,
                          **({"attachments": [a["name"] for a in attachments]} if attachments else {})}))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e), "to": to, "cc": cc}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
