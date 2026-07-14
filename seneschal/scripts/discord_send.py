#!/usr/bin/env python3
"""Send a Discord message as the assistant's bot. Standard library only.

Discord bots talk to a simple HTTPS REST API. This is the outbound half of the assistant's Discord channel —
reminders, nudges, approval prompts, and the assistant's side of the two-way chat. The inbound side is
`discord_poll.py` (REST message polling, not the gateway websocket — see that file). One dedicated
private channel is the assistant's surface; its id is DISCORD_CHANNEL_ID.

CREDENTIALS — never hard-coded. Provide via environment or an --env-file (KEY=VALUE lines):
  DISCORD_BOT_TOKEN   the bot token from the Discord Developer Portal (required)
  DISCORD_CHANNEL_ID  default channel id to post in (the assistant's private channel); overridable with --channel-id
  DISCORD_API_BASE    default https://discord.com/api/v10  (override only for a proxy/test)

Setup is documented in DISCORD_SETUP.md.

USAGE:
  python discord_send.py --text "Reminder: tetanus shot at 3pm"
  python discord_send.py --text-file body.txt --channel-id 123456789012345678
  python discord_send.py --dry-run --text "hi"     # build the request only, no network
  python discord_send.py --check-auth              # GET /users/@me (verifies the token), no send

Prints a one-line JSON result. Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ENV_KEYS = ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_API_BASE")

# Discord asks bots to send a descriptive User-Agent; urllib's default can draw a 403.
USER_AGENT = "DiscordBot (https://github.com/kumouri/seneschal, 1.0)"


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
        "token": creds.get("DISCORD_BOT_TOKEN"),
        "channel_id": creds.get("DISCORD_CHANNEL_ID"),
        "api_base": creds.get("DISCORD_API_BASE", "https://discord.com/api/v10").rstrip("/"),
    }


def api_request(c: dict, method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    """Call the Discord REST API and return the parsed JSON. Raises on transport / API error."""
    if not c["token"]:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN (set in env or --env-file)")
    url = f"{c['api_base']}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bot {c['token']}",
        "User-Agent": USER_AGENT,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:  # Discord returns JSON error bodies on 4xx
        detail = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(detail)
            msg = payload.get("message", detail)
        except Exception:  # noqa: BLE001
            msg = detail
        raise RuntimeError(f"Discord {method} {path} failed: HTTP {e.code} {msg[:200]}") from e


def main() -> int:
    p = argparse.ArgumentParser(description="Send a Discord message as the assistant's bot.")
    p.add_argument("--text", help="message text")
    p.add_argument("--text-file", help="read message text from a file")
    p.add_argument("--channel-id", help="channel id to post in (default DISCORD_CHANNEL_ID)")
    p.add_argument("--env-file", help="KEY=VALUE file with DISCORD_* settings (kept untracked)")
    p.add_argument("--dry-run", action="store_true", help="build the request and print a summary; no network")
    p.add_argument("--check-auth", action="store_true", help="call GET /users/@me (verify token); do not send")
    args = p.parse_args()

    creds = load_env(args.env_file)
    c = cfg(creds)
    if args.channel_id:
        c["channel_id"] = args.channel_id

    if args.check_auth:
        try:
            me = api_request(c, "GET", "/users/@me")
            print(json.dumps({"ok": True, "check_auth": "token OK", "bot": me.get("username"), "id": me.get("id")}))
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
    if not c["channel_id"]:
        print(json.dumps({"ok": False, "error": "no channel id (set DISCORD_CHANNEL_ID or pass --channel-id)"}))
        return 2

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "channel_id": c["channel_id"], "chars": len(text)}))
        return 0

    try:
        res = api_request(c, "POST", f"/channels/{c['channel_id']}/messages", {"content": text})
        print(json.dumps({"ok": True, "sent": True, "channel_id": c["channel_id"], "message_id": res.get("id")}))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e), "channel_id": c["channel_id"]}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
