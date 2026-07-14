#!/usr/bin/env python3
"""Gmail API v1 bridge for the assistant — read threads, draft, label, send-on-approval. Stdlib only.

Sits on the shared OAuth plumbing in `google_common.py`; every call is keyed by an account label
(--account). Reading + drafting + labeling are act-low (the assistant does them directly to triage the inbox).
Actually SENDING is act-high and outbound — the assistant draft-and-holds and only sends on the owner's approval, so
the send paths (`send`, `send-draft`) are separate subcommands the owner can gate.

  python gmail_api.py profile   --account personal --env-file google.env
  python gmail_api.py labels    --account personal --env-file google.env
  python gmail_api.py list      --account personal --query "is:unread newer_than:2d" --max 10 --env-file google.env
  python gmail_api.py get       --account personal --id <messageId> --env-file google.env
  python gmail_api.py draft     --account personal --to a@b.com --subject "Hi" --body "..." --env-file google.env
  python gmail_api.py modify    --account personal --id <messageId> --mark-read --env-file google.env
  python gmail_api.py send-draft --account personal --id <draftId> --env-file google.env   # OUTBOUND (approval)
  python gmail_api.py send      --account personal --to a@b.com --subject "Hi" --body "..." --env-file google.env  # OUTBOUND

All output is one JSON object. Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from email.message import EmailMessage

import google_common as gc

API = "https://www.googleapis.com/gmail/v1/users/me"


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _header(payload: dict, name: str) -> str | None:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def _extract_text(payload: dict) -> str:
    """Depth-first walk for the first text/plain part; fall back to a stripped text/html part."""
    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")
    if mime == "text/plain" and body_data:
        return _b64url_decode(body_data).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        found = _extract_text(part)
        if found:
            return found
    if mime == "text/html" and body_data:  # last resort — hand back raw html
        return _b64url_decode(body_data).decode("utf-8", "replace")
    return ""


def _slim_message(m: dict, *, with_body: bool = False) -> dict:
    payload = m.get("payload", {})
    out = {
        "id": m.get("id"),
        "threadId": m.get("threadId"),
        "from": _header(payload, "From"),
        "to": _header(payload, "To"),
        "subject": _header(payload, "Subject"),
        "date": _header(payload, "Date"),
        "snippet": m.get("snippet"),
        "labels": m.get("labelIds"),
        "unread": "UNREAD" in (m.get("labelIds") or []),
    }
    if with_body:
        out["body"] = _extract_text(payload)
    return out


def cmd_profile(c, args) -> dict:
    res = gc.authorized_request(c, args.account, "GET", f"{API}/profile", where="profile")
    return {"ok": True, "account": args.account, "email": res.get("emailAddress"),
            "messagesTotal": res.get("messagesTotal"), "threadsTotal": res.get("threadsTotal")}


def cmd_labels(c, args) -> dict:
    res = gc.authorized_request(c, args.account, "GET", f"{API}/labels", where="labels.list")
    labels = [{"id": l.get("id"), "name": l.get("name"), "type": l.get("type")}
              for l in res.get("labels", [])]
    return {"ok": True, "account": args.account, "count": len(labels), "labels": labels}


def cmd_list(c, args) -> dict:
    params = {"maxResults": args.max}
    if args.query:
        params["q"] = args.query
    if args.label:
        params["labelIds"] = [x.strip() for x in args.label.split(",") if x.strip()]
    res = gc.authorized_request(c, args.account, "GET", f"{API}/messages", params=params, where="messages.list")
    ids = res.get("messages", [])
    if args.ids_only:
        return {"ok": True, "account": args.account, "count": len(ids), "messages": ids}
    # Enrich each id with metadata headers (bounded by --max) so the caller gets a readable digest.
    enriched = []
    for ref in ids:
        m = gc.authorized_request(
            c, args.account, "GET", f"{API}/messages/{ref['id']}",
            params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
            where="messages.get(meta)")
        enriched.append(_slim_message(m))
    return {"ok": True, "account": args.account, "count": len(enriched),
            "estimate": res.get("resultSizeEstimate"), "messages": enriched}


def cmd_get(c, args) -> dict:
    fmt = args.format or "full"
    m = gc.authorized_request(c, args.account, "GET", f"{API}/messages/{args.id}",
                              params={"format": fmt}, where="messages.get")
    if args.raw or fmt in ("raw", "minimal"):
        return {"ok": True, "account": args.account, "message": m}
    return {"ok": True, "account": args.account, "message": _slim_message(m, with_body=True)}


def _build_raw(args) -> str:
    body = args.body
    if args.body_file:
        with open(args.body_file, "r", encoding="utf-8") as fh:
            body = fh.read()
    if not (args.to and args.subject and body is not None):
        raise RuntimeError("need --to, --subject, and --body/--body-file")
    msg = EmailMessage()
    msg["To"] = args.to
    if args.cc:
        msg["Cc"] = args.cc
    if args.bcc:
        msg["Bcc"] = args.bcc
    msg["Subject"] = args.subject
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def cmd_draft(c, args) -> dict:
    raw = _build_raw(args)
    body = {"message": {"raw": raw}}
    if args.thread_id:
        body["message"]["threadId"] = args.thread_id
    res = gc.authorized_request(c, args.account, "POST", f"{API}/drafts", body=body, where="drafts.create")
    return {"ok": True, "account": args.account, "drafted": True, "draftId": res.get("id"),
            "messageId": (res.get("message") or {}).get("id")}


def cmd_send_draft(c, args) -> dict:
    res = gc.authorized_request(c, args.account, "POST", f"{API}/drafts/send",
                                body={"id": args.id}, where="drafts.send")
    return {"ok": True, "account": args.account, "sent": True, "messageId": res.get("id"),
            "threadId": res.get("threadId")}


def cmd_send(c, args) -> dict:
    raw = _build_raw(args)
    body = {"raw": raw}
    if args.thread_id:
        body["threadId"] = args.thread_id
    res = gc.authorized_request(c, args.account, "POST", f"{API}/messages/send", body=body, where="messages.send")
    return {"ok": True, "account": args.account, "sent": True, "messageId": res.get("id"),
            "threadId": res.get("threadId")}


def cmd_modify(c, args) -> dict:
    add = [x.strip() for x in (args.add_labels or "").split(",") if x.strip()]
    remove = [x.strip() for x in (args.remove_labels or "").split(",") if x.strip()]
    if args.mark_read:
        remove.append("UNREAD")
    if args.archive:
        remove.append("INBOX")
    if not add and not remove:
        raise RuntimeError("modify needs --add-labels/--remove-labels or --mark-read/--archive")
    res = gc.authorized_request(c, args.account, "POST", f"{API}/messages/{args.id}/modify",
                                body={"addLabelIds": add, "removeLabelIds": remove}, where="messages.modify")
    return {"ok": True, "account": args.account, "id": args.id, "labels": res.get("labelIds")}


def main() -> int:
    p = argparse.ArgumentParser(description="Gmail API v1 bridge for the assistant.")
    p.add_argument("command", choices=[
        "profile", "labels", "list", "get", "draft", "modify", "send-draft", "send"])
    p.add_argument("--account", required=True, help="account label (e.g. personal, work)")
    p.add_argument("--env-file", help="KEY=VALUE file with GOOGLE_* settings (kept untracked)")
    p.add_argument("--query", help="Gmail search query (list), e.g. 'is:unread newer_than:2d'")
    p.add_argument("--label", help="restrict list to label id(s), comma-separated")
    p.add_argument("--max", type=int, default=10, help="max messages (list; default 10)")
    p.add_argument("--ids-only", action="store_true", help="list: return ids only, skip metadata fetch")
    p.add_argument("--id", help="message id (get/modify) or draft id (send-draft)")
    p.add_argument("--format", help="get: full | metadata | minimal | raw (default full)")
    p.add_argument("--to", help="recipient(s) (draft/send)")
    p.add_argument("--cc", help="cc (draft/send)")
    p.add_argument("--bcc", help="bcc (draft/send)")
    p.add_argument("--subject", help="subject (draft/send)")
    p.add_argument("--body", help="body text (draft/send)")
    p.add_argument("--body-file", help="read body from a file (draft/send)")
    p.add_argument("--thread-id", help="attach the draft/message to an existing thread")
    p.add_argument("--add-labels", help="modify: label ids to add, comma-separated")
    p.add_argument("--remove-labels", help="modify: label ids to remove, comma-separated")
    p.add_argument("--mark-read", action="store_true", help="modify: remove UNREAD")
    p.add_argument("--archive", action="store_true", help="modify: remove INBOX")
    p.add_argument("--raw", action="store_true", help="get: emit the full API object")
    args = p.parse_args()

    needs_id = {"get": "get", "modify": "modify", "send-draft": "send-draft"}
    if args.command in needs_id and not args.id:
        print(json.dumps({"ok": False, "error": f"{args.command} needs --id"}))
        return 2

    c = gc.cfg(gc.load_env(args.env_file))
    handlers = {
        "profile": cmd_profile, "labels": cmd_labels, "list": cmd_list, "get": cmd_get,
        "draft": cmd_draft, "modify": cmd_modify, "send-draft": cmd_send_draft, "send": cmd_send,
    }
    try:
        print(json.dumps(handlers[args.command](c, args), ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "account": args.account, "command": args.command, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
