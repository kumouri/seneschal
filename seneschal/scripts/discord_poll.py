#!/usr/bin/env python3
"""Poll Discord for new inbound messages in the assistant's channel. Standard library only.

The inbound half of the two-way Discord chat. Discord bots normally receive messages over a **gateway
websocket**, which needs a non-stdlib client and a resident connection. To stay stdlib-only and match the
`telegram_poll.py` offset pattern, this polls the REST API instead:
`GET /channels/{id}/messages?after=<last_seen_id>`, advancing a stored snowflake id so each message is
returned once. The presence daemon runs this each cycle; latency is poll-cadence, not instant-push.

Snowflake message ids increase over time, so the "offset" is just the largest id seen so far. On the very
first run (no offset yet) we **seed** to the newest message id and return nothing — the assistant starts listening
"from now" rather than replaying the whole channel.

CREDENTIALS — never hard-coded. Provide via environment or an --env-file (KEY=VALUE lines):
  DISCORD_BOT_TOKEN        the bot token from the Discord Developer Portal (required)
  DISCORD_CHANNEL_ID       the channel to watch (required)
  DISCORD_ALLOWED_USER_IDS optional comma-separated allowlist of author ids; if set, only these drive the assistant
  DISCORD_API_BASE         default https://discord.com/api/v10

Offset is persisted in a small file (default: ../state/discord-offset, gitignored). Delete it to re-seed.

USAGE:
  python discord_poll.py                 # peek: return new messages, do NOT advance offset
  python discord_poll.py --commit        # return new messages AND acknowledge them

Prints a one-line JSON result: {ok, count, messages:[{id,channel_id,author,author_id,text,ts}], next_offset}.
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

ENV_KEYS = ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_ALLOWED_USER_IDS", "DISCORD_API_BASE")

USER_AGENT = "DiscordBot (https://github.com/kumouri/seneschal, 1.0)"

DEFAULT_OFFSET_FILE = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state", "discord-offset")
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
            return fh.read().strip() or None
    except FileNotFoundError:
        return None


def write_offset(path: str, offset: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(offset))


def get_messages(token: str, api_base: str, channel_id: str, after: str | None, limit: int) -> list:
    params = {"limit": limit}
    if after is not None:
        params["after"] = after
    url = f"{api_base.rstrip('/')}/channels/{channel_id}/messages?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, method="GET", headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))  # newest-first array of message objects


def _max_id(ids) -> str | None:
    """Largest snowflake id (numeric compare) as a string, or None."""
    best = None
    for i in ids:
        try:
            if best is None or int(i) > int(best):
                best = i
        except (TypeError, ValueError):
            continue
    return best


def main() -> int:
    p = argparse.ArgumentParser(description="Poll Discord for new inbound messages.")
    p.add_argument("--offset-file", default=DEFAULT_OFFSET_FILE, help="where the message-id offset is stored")
    p.add_argument("--limit", type=int, default=50, help="max messages per call")
    p.add_argument("--commit", action="store_true", help="advance the offset (acknowledge the returned messages)")
    p.add_argument("--env-file", help="KEY=VALUE file with DISCORD_* settings (kept untracked)")
    args = p.parse_args()

    creds = load_env(args.env_file)
    token = creds.get("DISCORD_BOT_TOKEN")
    channel_id = creds.get("DISCORD_CHANNEL_ID")
    if not token or not channel_id:
        print(json.dumps({"ok": False, "error": "Missing DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID (env or --env-file)"}))
        return 1
    api_base = creds.get("DISCORD_API_BASE", "https://discord.com/api/v10")
    allowed = {x.strip() for x in (creds.get("DISCORD_ALLOWED_USER_IDS", "") or "").split(",") if x.strip()}

    offset = read_offset(args.offset_file)

    # First run: seed to the newest message id and listen from now on (no history replay).
    if offset is None:
        try:
            latest = get_messages(token, api_base, channel_id, None, 1)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
        seed = latest[0].get("id") if latest else None
        if args.commit and seed is not None:
            write_offset(args.offset_file, seed)
        print(json.dumps({
            "ok": True, "count": 0, "messages": [], "next_offset": seed,
            "committed": bool(args.commit and seed is not None), "seeded": True,
        }))
        return 0

    try:
        raw = get_messages(token, api_base, channel_id, offset, args.limit)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    messages = []
    seen_ids = []
    for m in reversed(raw):  # API returns newest-first; walk oldest-first
        mid = m.get("id")
        if mid is not None:
            seen_ids.append(mid)
        author = m.get("author", {}) or {}
        if author.get("bot"):
            continue  # ignore bot messages (including the assistant's own) — no self-echo loops
        author_id = str(author.get("id", ""))
        if allowed and author_id not in allowed:
            continue  # outside the allowlist — ignore but still acknowledge via offset advance
        messages.append({
            "id": mid,
            "channel_id": str(channel_id),
            "author": author.get("username") or author.get("global_name"),
            "author_id": author_id,
            "text": m.get("content", ""),
            "ts": m.get("timestamp"),
        })

    next_offset = _max_id(seen_ids) or offset
    if args.commit and _max_id(seen_ids) is not None:
        write_offset(args.offset_file, next_offset)

    print(json.dumps({
        "ok": True, "count": len(messages), "messages": messages,
        "next_offset": next_offset, "committed": bool(args.commit and _max_id(seen_ids) is not None),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
