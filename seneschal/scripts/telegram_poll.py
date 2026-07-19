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

ATTACHMENTS: with --download-dir, an inbound document/photo/voice/audio/video is fetched via getFile and
written there, and the message carries an `attachment` record with its `local_path`. Without the flag the
attachment is described but not downloaded (so a bare peek stays read-only). See
../docs/telegram-inbound-spec.md §2.

CUSTOM-EMOJI REACTIONS: Telegram Premium lets the owner react with ~any emoji, and those arrive as
`ReactionTypeCustomEmoji` — an opaque `custom_emoji_id`, no plain `emoji` field. Extraction resolves it to
its base emoji via getCustomEmojiStickers (batched, up to 200 ids/call) and a small local cache
(../state/custom-emoji-cache.json, gitignored like the rest of state/) so a repeat reaction costs no
network. Fail-open: no token, a network error, an unknown id, or a sticker with no `emoji` field all leave
the reaction's emoji blank — it still reaches the warm session, just mapped to the same "note" intent an
unrecognized plain emoji gets. See ../docs/telegram-inbound-spec.md §3.

USAGE:
  python telegram_poll.py                         # peek: return new messages, do NOT advance offset
  python telegram_poll.py --commit                # return new messages AND acknowledge them
  python telegram_poll.py --commit --timeout 25   # long-poll up to 25s (interactive use, not the sentinel)
  python telegram_poll.py --commit --download-dir ../state/inbox   # also fetch attachments (the daemon)
  python telegram_poll.py --prune-days 30         # GC mode: sweep old attachments, no network (Dream)

Prints a one-line JSON result:
  {ok, count, messages:[{update_id,chat_id,chat_type,from,text,date,caption?,attachment?}], next_offset}
where `attachment` is {kind,file_name,mime_type,file_size,local_path?,too_large?,error?}.
Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_API_BASE")

DEFAULT_OFFSET_FILE = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state", "telegram-offset")
)
DEFAULT_INBOX_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state", "inbox")
)
DEFAULT_CUSTOM_EMOJI_CACHE = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "state", "custom-emoji-cache.json")
)
# getCustomEmojiStickers' own cap on `custom_emoji_ids` per call.
CUSTOM_EMOJI_BATCH = 200

# Media fields we understand, in the order Telegram would populate them on a single message.
MEDIA_KINDS = ("document", "photo", "voice", "audio", "video")
# How much of a swipe-replied-to message we quote back. Enough to identify it, not enough to crowd out
# the actual reply.
REPLY_QUOTE_CHARS = 300
# The Bot API's getFile download ceiling. Bigger files simply cannot come down this path — see
# the "drop it on the machine" branch in fetch_attachment().
FILE_LIMIT_BYTES = 20 * 1024 * 1024
# Everything outside this set is scrubbed out of a client-supplied filename.
_UNSAFE_NAME_RE = re.compile(r"[^-\w. ]")


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


def extract_media(msg: dict) -> dict | None:
    """The message's attachment as {kind, file_id, file_unique_id, file_name, mime_type, file_size},
    or None for a plain text message."""
    for kind in MEDIA_KINDS:
        blob = msg.get(kind)
        if not blob:
            continue
        if kind == "photo":
            # `photo` is an array of PhotoSizes — the same image at several resolutions. Take the
            # largest; the small ones are thumbnails.
            sizes = [p for p in blob if isinstance(p, dict) and p.get("file_id")]
            if not sizes:
                continue
            blob = max(sizes, key=lambda p: (p.get("file_size") or 0,
                                             (p.get("width") or 0) * (p.get("height") or 0)))
            # PhotoSize carries no name/mime; getFile's remote path supplies the extension.
            return {"kind": kind, "file_id": blob["file_id"], "file_unique_id": blob.get("file_unique_id"),
                    "file_name": None, "mime_type": "image/jpeg", "file_size": blob.get("file_size")}
        if not isinstance(blob, dict) or not blob.get("file_id"):
            continue
        return {"kind": kind, "file_id": blob["file_id"], "file_unique_id": blob.get("file_unique_id"),
                "file_name": blob.get("file_name"), "mime_type": blob.get("mime_type"),
                "file_size": blob.get("file_size")}
    return None


def extract_reply_to(msg: dict) -> str | None:
    """A short descriptor of the message the owner swipe-replied to, or None.

    Telegram hands us the whole quoted message; we keep just enough to identify which one it was. A
    quoted *attachment* has no text of its own, so it gets a synthesized descriptor rather than
    disappearing into an empty quote."""
    parent = msg.get("reply_to_message")
    if not isinstance(parent, dict):
        return None
    quoted = (parent.get("text") or parent.get("caption") or "").strip()
    if not quoted:
        media = extract_media(parent)
        if media:
            # No inner quotes: the caller wraps this whole string in quotes, and nesting them reads badly.
            name = safe_filename(media.get("file_name"), "")
            quoted = f'a {media["kind"]}' + (f" named {name}" if name else "")
        else:
            quoted = "an earlier message"
    if len(quoted) > REPLY_QUOTE_CHARS:
        quoted = quoted[:REPLY_QUOTE_CHARS].rstrip() + "…"
    return quoted


def safe_filename(name: str | None, fallback: str) -> str:
    """A filename safe to join onto the inbox dir. The sender controls `name`, so it is never trusted for
    the write path: directories are stripped, anything outside [-\\w. ] (including control characters)
    collapses to _, and leading/trailing dots go — so `..`, `../../x`, and `C:\\evil` cannot escape."""
    base = os.path.basename((name or "").replace("\\", "/").strip())
    base = _UNSAFE_NAME_RE.sub("_", base).strip(" .")
    return base[:120] or fallback


def telegram_get_file(token: str, api_base: str, file_id: str, dest_dir: str,
                      file_name: str | None = None, fallback: str = "file", timeout: int = 60) -> str:
    """getFile(file_id) → download to dest_dir/<utc-stamp>-<safe-name>. Returns the local path.

    The timestamp prefix keeps two same-named sends from clobbering each other — and, on Windows, keeps a
    file called `NUL`/`CON` from resolving to a device rather than a file."""
    url = f"{api_base.rstrip('/')}/bot{token}/getFile?" + urllib.parse.urlencode({"file_id": file_id})
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram getFile failed: {payload.get('description', payload)}")
    remote = (payload.get("result") or {}).get("file_path")
    if not remote:
        raise RuntimeError("Telegram getFile returned no file_path")
    name = safe_filename(file_name or os.path.basename(remote), fallback)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{stamp}-{name}")
    dl = f"{api_base.rstrip('/')}/file/bot{token}/{remote.lstrip('/')}"
    with urllib.request.urlopen(dl, timeout=timeout) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    return dest


def fetch_attachment(media: dict, token: str, api_base: str, dest_dir: str | None) -> dict:
    """Turn one extracted media blob into the record the daemon enqueues, downloading it if we can.

    Never raises: an attachment is a convenience on top of the text pipeline, so a failed fetch costs us
    the file, never the message. The failure is reported in the record and the assistant says so in its
    own words — the daemon composes no prose."""
    att = {k: media.get(k) for k in ("kind", "file_name", "mime_type", "file_size")}
    if (media.get("file_size") or 0) > FILE_LIMIT_BYTES:
        att["too_large"] = True  # getFile would refuse it anyway — don't spend the round-trip
        return att
    if not dest_dir:
        return att  # peek mode: describe it, don't fetch it
    try:
        att["local_path"] = telegram_get_file(
            token, api_base, media["file_id"], dest_dir, file_name=media.get("file_name"),
            fallback=f"{media.get('kind') or 'file'}-{media.get('file_unique_id') or 'unknown'}")
    except Exception as e:  # noqa: BLE001 — fail open; the text half of the message still gets through
        att["error"] = str(e)
    return att


def prune_inbox(inbox_dir: str, days: int) -> int:
    """Delete inbox files older than `days`; returns how many went. Dream's nightly GC — the inbox is a
    landing pad, not an archive (anything worth keeping has been moved somewhere real by then), and a
    20 MB-a-file drop zone shouldn't grow forever. Best-effort: a locked or vanished file waits a night."""
    if days <= 0 or not os.path.isdir(inbox_dir):
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for name in os.listdir(inbox_dir):
        path = os.path.join(inbox_dir, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


def _reaction_keys(reactions) -> list:
    """Every reaction in the array as a stable string key, used only to diff old vs new: `e:<emoji>` for
    a plain ReactionTypeEmoji, `c:<custom_emoji_id>` for a Telegram Premium ReactionTypeCustomEmoji (an
    opaque id, no plain emoji field — the two namespaces are prefixed so they can never collide). Anything
    else (a future reaction type) is skipped, same as before."""
    keys = []
    for r in (reactions or []):
        if not isinstance(r, dict):
            continue
        if r.get("type") == "emoji" and r.get("emoji"):
            keys.append(f"e:{r['emoji']}")
        elif r.get("type") == "custom_emoji" and r.get("custom_emoji_id"):
            keys.append(f"c:{r['custom_emoji_id']}")
    return keys


def extract_reaction(update: dict, allowed: set) -> dict | None:
    """A `message_reaction` update as an inbound item, or None.

    Telegram sends the whole before/after arrays rather than a delta, so the *added* reaction — what the
    owner just did — is new minus old. A reaction they **removed** yields nothing to act on.

    A Telegram Premium custom-emoji reaction is extracted here too, but comes out with `emoji: ""` and a
    `custom_emoji_id` — resolving that opaque id needs a Bot API round-trip (batched across the whole poll
    batch, see `resolve_reactions`), which this function deliberately doesn't do so it stays a pure,
    easily-tested diff. Unresolved is not dropped: it still reaches the warm session, same as any other
    unmapped emoji (`note`)."""
    upd = update.get("message_reaction")
    if not isinstance(upd, dict):
        return None
    chat = upd.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    if allowed and chat_id not in allowed:
        return None  # outside the allowlist — ignored, still acknowledged via the offset advance
    added = [k for k in _reaction_keys(upd.get("new_reaction")) if k not in _reaction_keys(upd.get("old_reaction"))]
    if not added:
        return None
    key = added[0]
    frm = upd.get("user") or {}
    item = {
        "update_id": update.get("update_id"),
        "kind": "reaction",
        "chat_id": chat_id,
        "chat_type": chat.get("type"),
        "from": frm.get("username") or frm.get("first_name"),
        "message_id": upd.get("message_id"),  # which of the assistant's messages they reacted to
        "date": upd.get("date"),
        "text": "",
    }
    if key.startswith("c:"):
        item["emoji"] = ""  # unresolved until resolve_reactions() runs over the whole batch
        item["custom_emoji_id"] = key[2:]
    else:
        item["emoji"] = key[2:]
    return item


def load_custom_emoji_cache(path: str) -> dict:
    """custom_emoji_id -> base emoji, cheap to lose. Corrupt, missing, or wrong-shaped ⇒ start empty, never
    crash — the cache is a speed-up, not a source of truth (an empty cache just means the next lookup for
    that id costs a network round-trip instead of being free)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v}


def save_custom_emoji_cache(path: str, cache: dict) -> None:
    """Best-effort: a failed write costs the next lookup a network round-trip, never an exception."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except (OSError, ValueError):  # ValueError: e.g. an embedded NUL in a bad path on Windows
        pass


def get_custom_emoji_stickers(token: str, api_base: str, ids: list, timeout: int = 15) -> dict:
    """getCustomEmojiStickers(custom_emoji_ids) -> {custom_emoji_id: base emoji}, for every returned
    Sticker that actually carries an `emoji` field (Telegram Premium's own custom set may not always set
    one). Raises on transport/API error, same as get_updates — the caller (resolve_custom_emojis) owns
    the fail-open contract."""
    if not ids:
        return {}
    params = {"custom_emoji_ids": json.dumps(list(ids))}
    url = f"{api_base.rstrip('/')}/bot{token}/getCustomEmojiStickers?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram getCustomEmojiStickers failed: {payload.get('description', payload)}")
    out = {}
    for sticker in payload.get("result", []) or []:
        if isinstance(sticker, dict) and sticker.get("custom_emoji_id") and sticker.get("emoji"):
            out[sticker["custom_emoji_id"]] = sticker["emoji"]
    return out


def resolve_custom_emojis(ids, token: str, api_base: str, cache_path: str) -> dict:
    """Every requested custom_emoji_id resolved to its base emoji: a cache hit costs nothing, and every
    never-before-seen id is looked up in as few getCustomEmojiStickers calls as its count needs (batched
    to CUSTOM_EMOJI_BATCH per call). Never raises: no token, or an API call that fails, just leaves those
    ids unresolved — the caller degrades them to the ordinary unknown-emoji `note` path."""
    ids = sorted(set(ids))
    cache = load_custom_emoji_cache(cache_path)
    missing = [i for i in ids if i not in cache]
    if missing and token:
        found_any = False
        for i in range(0, len(missing), CUSTOM_EMOJI_BATCH):
            batch = missing[i:i + CUSTOM_EMOJI_BATCH]
            try:
                found = get_custom_emoji_stickers(token, api_base, batch)
            except Exception:  # noqa: BLE001 — fail open; an unresolved id is not a dropped message
                continue
            if found:
                cache.update(found)
                found_any = True
        if found_any:
            save_custom_emoji_cache(cache_path, cache)
    return {i: cache[i] for i in ids if i in cache}


def resolve_reactions(messages: list, token: str, api_base: str, cache_path: str) -> None:
    """Fill in `emoji` for every reaction item still carrying an unresolved `custom_emoji_id`, mutating
    `messages` in place. One batched lookup for the whole poll batch, regardless of how many distinct
    custom-emoji reactions are in it. Feeds the exact same `emoji` field plain reactions use, so everything
    downstream (`presence._norm_emoji` / `reaction_intent`) works identically — an id that can't be
    resolved just leaves `emoji` empty, which already maps to the unmapped 'note' intent."""
    ids = {m["custom_emoji_id"] for m in messages
           if m.get("kind") == "reaction" and m.get("custom_emoji_id") and not m.get("emoji")}
    if not ids:
        return
    resolved = resolve_custom_emojis(ids, token, api_base, cache_path)
    for m in messages:
        cid = m.get("custom_emoji_id")
        if m.get("kind") == "reaction" and cid and not m.get("emoji"):
            m["emoji"] = resolved.get(cid, "")


def get_updates(token: str, api_base: str, offset, timeout: int, limit: int) -> list:
    # `message_reaction` is OFF by default — Telegram won't send reactions unless we ask for them by
    # name, which is the single switch that makes §3 possible at all. In a private chat with the bot,
    # the user's own reactions arrive without any admin rights.
    params = {"timeout": timeout, "limit": limit,
              "allowed_updates": json.dumps(["message", "message_reaction"])}
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
    p.add_argument("--download-dir", help="fetch inbound attachments into this dir (omit = describe, don't download)")
    p.add_argument("--prune-days", type=int,
                   help="GC mode (Dream): delete attachments older than N days from the inbox and exit")
    p.add_argument("--env-file", help="KEY=VALUE file with TELEGRAM_* settings (kept untracked)")
    p.add_argument("--custom-emoji-cache", default=DEFAULT_CUSTOM_EMOJI_CACHE,
                   help="local cache mapping a Premium custom_emoji_id -> its base emoji")
    args = p.parse_args()

    # GC mode is offline and standalone: no token, no network, no offset — just sweep and report.
    if args.prune_days is not None:
        inbox = args.download_dir or DEFAULT_INBOX_DIR
        print(json.dumps({"ok": True, "pruned": prune_inbox(inbox, args.prune_days), "inbox": inbox}))
        return 0

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
            reaction = extract_reaction(u, allowed)
            if reaction:
                messages.append(reaction)
            continue  # other non-message update; still acknowledged via offset advance
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        if allowed and chat_id not in allowed:
            continue  # outside the allowlist — ignore but still acknowledge
        frm = msg.get("from", {})
        item = {
            "update_id": uid,
            "kind": "message",
            "chat_id": chat_id,
            "chat_type": chat.get("type"),
            "from": frm.get("username") or frm.get("first_name"),
            "text": msg.get("text", ""),
            "date": msg.get("date"),
        }
        # Additive, and only when present: a plain text message serializes exactly as it always has.
        # Media is fetched only for allowlisted chats — we're already past that gate here.
        if msg.get("caption"):
            item["caption"] = msg["caption"]
        reply_to = extract_reply_to(msg)
        if reply_to:
            item["reply_to"] = reply_to
        media = extract_media(msg)
        if media:
            item["attachment"] = fetch_attachment(media, token, api_base, args.download_dir)
        messages.append(item)

    # Batched across the whole poll, not per-reaction: however many custom-emoji reactions came in this
    # round, it costs at most ceil(distinct_ids / 200) API calls (usually zero once the cache is warm).
    resolve_reactions(messages, token, api_base, args.custom_emoji_cache)

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
