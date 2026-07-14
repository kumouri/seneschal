#!/usr/bin/env python3
"""Poll Telegram for new inbound messages to the assistant's bot. Standard library only.

The inbound half of the two-way chat. Calls the Bot API's getUpdates with a stored offset so each message
is returned once, and (with --commit) advances that offset to acknowledge them. The sentinel runs this
cheaply every cycle (timeout 0 = a single fast call); when it returns messages, the assistant's brain wakes to
reply via `telegram_send.py`.

Telegram retains undelivered updates for ~24h, so messages that arrive while the machine is asleep are
picked up on the next poll — the same catch-up behavior as the rest of the local stack.

CREDENTIALS — never hard-coded. Provide via environment or an --env-file (KEY=VALUE lines):
  TELEGRAM_BOT_TOKEN          the bot token from @BotFather (required)
  TELEGRAM_ALLOWED_CHAT_IDS   optional comma-separated allowlist; if set, only these chats are returned
  TELEGRAM_API_BASE           default https://api.telegram.org

Offset is persisted in a small file (default: ../state/telegram-offset, gitignored). Delete it to replay
the last ~24h of updates.

USAGE:
  python telegram_poll.py                         # peek: return new messages, do NOT advance offset
  python telegram_poll.py --commit                # return new messages AND acknowledge them
  python telegram_poll.py --commit --timeout 25   # long-poll up to 25s (interactive use, not the sentinel)

Prints a one-line JSON result: {ok, count, messages:[{update_id,chat_id,chat_type,from,text,date}], next_offset}.
Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_API_BASE")

DEFAULT_OFFSET_FILE = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state", "telegram-offset")
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


def read_offset(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def write_offset(path: str, offset: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(offset))


def get_updates(token: str, api_base: str, offset, timeout: int, limit: int) -> list:
    params = {"timeout": timeout, "limit": limit, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    url = f"{api_base.rstrip('/')}/bot{token}/getUpdates?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    # Network timeout slightly above the long-poll window so the server hangup wins.
    with urllib.request.urlopen(req, timeout=timeout + 15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram getUpdates failed: {payload.get('description', payload)}")
    return payload.get("result", [])


def main() -> int:
    p = argparse.ArgumentParser(description="Poll Telegram for new inbound messages.")
    p.add_argument("--offset-file", default=DEFAULT_OFFSET_FILE, help="where the update offset is stored")
    p.add_argument("--timeout", type=int, default=0, help="long-poll seconds (0 = single fast call; sentinel default)")
    p.add_argument("--limit", type=int, default=100, help="max updates per call")
    p.add_argument("--commit", action="store_true", help="advance the offset (acknowledge the returned updates)")
    p.add_argument("--env-file", help="KEY=VALUE file with TELEGRAM_* settings (kept untracked)")
    args = p.parse_args()

    creds = load_env(args.env_file)
    token = creds.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(json.dumps({"ok": False, "error": "Missing TELEGRAM_BOT_TOKEN (set in env or --env-file)"}))
        return 1
    api_base = creds.get("TELEGRAM_API_BASE", "https://api.telegram.org")
    allowed = {x.strip() for x in (creds.get("TELEGRAM_ALLOWED_CHAT_IDS", "") or "").split(",") if x.strip()}

    offset = read_offset(args.offset_file)
    try:
        updates = get_updates(token, api_base, offset, args.timeout, args.limit)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    messages = []
    max_update_id = None
    for u in updates:
        uid = u.get("update_id")
        if uid is not None:
            max_update_id = uid if max_update_id is None else max(max_update_id, uid)
        msg = u.get("message")
        if not msg:
            continue  # non-message update; still acknowledged via offset advance
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        if allowed and chat_id not in allowed:
            continue  # outside the allowlist — ignore but still acknowledge
        frm = msg.get("from", {})
        messages.append({
            "update_id": uid,
            "chat_id": chat_id,
            "chat_type": chat.get("type"),
            "from": frm.get("username") or frm.get("first_name"),
            "text": msg.get("text", ""),
            "date": msg.get("date"),
        })

    next_offset = (max_update_id + 1) if max_update_id is not None else offset
    if args.commit and max_update_id is not None:
        write_offset(args.offset_file, next_offset)

    print(json.dumps({
        "ok": True, "count": len(messages), "messages": messages,
        "next_offset": next_offset, "committed": bool(args.commit and max_update_id is not None),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
