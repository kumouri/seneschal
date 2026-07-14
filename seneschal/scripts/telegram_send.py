#!/usr/bin/env python3
"""Send a Telegram message as the assistant's bot. Standard library only.

Telegram bots talk to a simple HTTPS API (https://api.telegram.org/bot<TOKEN>/sendMessage). This is
The assistant's free, always-with-the-owner push + reply channel: reminders, nudges, approval prompts, and the assistant's side
of the two-way chat all go out through here. The inbound side is `telegram_poll.py`.

CREDENTIALS — never hard-coded. Provide via environment or an --env-file (KEY=VALUE lines):
  TELEGRAM_BOT_TOKEN     the bot token from @BotFather (required)
  TELEGRAM_CHAT_ID       default recipient chat id (the owner's DM with the bot); overridable with --chat-id
  TELEGRAM_API_BASE      default https://api.telegram.org  (override only for a proxy/test)
  TELEGRAM_PARSE_MODE    default ""  ("MarkdownV2" | "HTML" | "" for plain) — plain avoids escaping traps

Setup is documented in TELEGRAM_SETUP.md.

USAGE:
  python telegram_send.py --text "Reminder: tetanus shot at 3pm"
  python telegram_send.py --text-file body.txt --chat-id 12345678
  python telegram_send.py --dry-run --text "hi"     # build the request only, no network
  python telegram_send.py --check-auth              # getMe only (verifies the token), no send

Prints a one-line JSON result. Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_API_BASE", "TELEGRAM_PARSE_MODE")


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
        "token": creds.get("TELEGRAM_BOT_TOKEN"),
        "chat_id": creds.get("TELEGRAM_CHAT_ID"),
        "api_base": creds.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
        "parse_mode": creds.get("TELEGRAM_PARSE_MODE", ""),
    }


def api_call(c: dict, method: str, params: dict, timeout: int = 30) -> dict:
    """POST to the Bot API and return the parsed JSON. Raises on transport / API error."""
    if not c["token"]:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN (set in env or --env-file)")
    url = f"{c['api_base']}/bot{c['token']}/{method}"
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # Telegram returns JSON error bodies even on 4xx
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"HTTP {e.code} from Telegram {method}") from e
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload.get('description', payload)}")
    return payload


def main() -> int:
    p = argparse.ArgumentParser(description="Send a Telegram message as the assistant's bot.")
    p.add_argument("--text", help="message text")
    p.add_argument("--text-file", help="read message text from a file")
    p.add_argument("--chat-id", help="recipient chat id (default TELEGRAM_CHAT_ID)")
    p.add_argument("--parse-mode", help='"MarkdownV2" | "HTML" | "" (default TELEGRAM_PARSE_MODE)')
    p.add_argument("--disable-preview", action="store_true", help="disable link previews")
    p.add_argument("--env-file", help="KEY=VALUE file with TELEGRAM_* settings (kept untracked)")
    p.add_argument("--dry-run", action="store_true", help="build the request and print a summary; no network")
    p.add_argument("--check-auth", action="store_true", help="call getMe only (verify token); do not send")
    args = p.parse_args()

    creds = load_env(args.env_file)
    c = cfg(creds)
    if args.chat_id:
        c["chat_id"] = args.chat_id
    if args.parse_mode is not None:
        c["parse_mode"] = args.parse_mode

    if args.check_auth:
        try:
            me = api_call(c, "getMe", {})
            u = me.get("result", {})
            print(json.dumps({"ok": True, "check_auth": "token OK", "bot": u.get("username"), "id": u.get("id")}))
            return 0
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1

    text = args.text
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as fh:
            text = fh.read()
    if not text:
        print(json.dumps({"ok": False, "error": "no --text or --text-file provided"}))
        return 2
    if not c["chat_id"]:
        print(json.dumps({"ok": False, "error": "no chat id (set TELEGRAM_CHAT_ID or pass --chat-id)"}))
        return 2

    params = {
        "chat_id": c["chat_id"],
        "text": text,
        "parse_mode": c["parse_mode"] or None,
        "disable_web_page_preview": "true" if args.disable_preview else None,
    }

    if args.dry_run:
        print(json.dumps({
            "ok": True, "dry_run": True, "chat_id": c["chat_id"],
            "parse_mode": c["parse_mode"], "chars": len(text),
        }))
        return 0

    try:
        res = api_call(c, "sendMessage", params)
        mid = res.get("result", {}).get("message_id")
        print(json.dumps({"ok": True, "sent": True, "chat_id": c["chat_id"], "message_id": mid}))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e), "chat_id": c["chat_id"]}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
