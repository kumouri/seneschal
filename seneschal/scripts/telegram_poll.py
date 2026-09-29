#!/usr/bin/env python3
"""Poll Telegram for new inbound messages to the assistant's bot. Standard library only.

The inbound half of the two-way chat. Calls the Bot API's getUpdates with a stored offset so each message
is returned once, and (with --commit) advances that offset to acknowledge them. The sentinel runs this
cheaply every cycle (timeout 0 = a single fast call); when it returns messages, the assistant's brain
wakes to reply via `telegram_send.py`.

Telegram retains undelivered updates for ~24h, so messages that arrive while the machine is asleep are
picked up on the next poll — the same catch-up behavior as the rest of the local stack.

CREDENTIALS — never hard-coded. Provide via environment or an --env-file (KEY=VALUE lines):
  TELEGRAM_BOT_TOKEN          the bot token from @BotFather (required)
  TELEGRAM_ALLOWED_CHAT_IDS   optional comma-separated allowlist; if set, only these chats are returned
  TELEGRAM_API_BASE           default https://api.telegram.org

Offset is persisted in a small file (default: ../state/telegram-offset, gitignored). Delete it to replay
the last ~24h of updates.

ATTACHMENTS: with --download-dir, an inbound document/photo/voice/audio/video/sticker/video_note is
fetched via getFile and written there, and the message carries an `attachment` record with its
`local_path`. Without the flag the attachment is described but not downloaded (so a bare peek stays
read-only). See ../docs/telegram-inbound-spec.md §2.

NON-FILE KINDS: a location, venue, contact, poll or dice carries no `file_id` at all — there is
nothing to fetch. Without special handling one of these arrives with no text and no attachment,
renders as an empty line, and is silently dropped by the daemon's `if not line: continue` — with the
offset already committed, so Telegram never sends it again (`../docs/telegram-capability-map.md` §2.2
D1; a sticker had the same fate before it joined the fetchable set above). `describe_unsupported()`
names the kind and its content in a short synthesized line that rides out on the ordinary `text`
field instead.

**EVERY HTTP CALL HERE IS IDEMPOTENT AND RETRIES** (`telegram_http.py`). A getUpdates, a
getFile or a file download can be re-run at no cost, so all three take the `IDEMPOTENT` policy: bounded
exponential backoff over connection resets, DNS failures, TLS errors, timeouts, 429s and 5xx. That is
the whole reason this module reads the way it does — the *send* side is the conservative one, and the
reasoning for the asymmetry lives in `telegram_http.py`'s docstring.

**AND A DOWNLOAD THAT ULTIMATELY FAILS KEEPS ITS `file_id`.** A Telegram `file_id` stays valid long
after the fetch that failed, so a lost image is genuinely recoverable — but only if the id survives the
failure. The record carries `file_id` and `refetchable: true`, the daemon's placeholder can say so
in words, and `--refetch <file_id>` is the door
that makes the claim true rather than decorative. A failed ATTACHMENT download deliberately does NOT
hold the rest of the batch back — the recorded `file_id` is what makes that trade safe: the message
still arrives, "not yet fetched" rather than "lost."

**THE OFFSET ADVANCES LAST, ONLY AFTER A READ THAT SUCCEEDED AND WAS PROCESSED** — Telegram never
re-sends an update once it has been acknowledged, so committing the offset before a batch is actually
handled would be the one mistake this module cannot recover from.

TOPICS: a message the owner types inside a private-chat topic (Bot API 9.3) carries `message_thread_id` and
`is_topic_message`, both **absent — not null — when there is no topic**, and both are reproduced on the
payload with that shape intact. The chat id is unchanged, so nothing about the allowlist or the offset
moves; the daemon uses the id to pick a per-topic continuity cache and to send the assistant's reply
back into the same thread. See `extract_thread_id` for why the id routes and the boolean does not.

**`extract_thread_id` ROUTES — IT DOES NOT MERELY SURFACE**: `message_thread_id` (present only when
Telegram sent one; `0` is not a thread) is what picks the per-topic continuity cache and rides back
out on the assistant's reply. `is_topic_message` is surfaced BESIDE it and routes
NOTHING — a boolean cannot name a thread, and two fields that must agree are two fields that can
disagree, so exactly one of them decides; the check is `is True`, never truthiness.

**AN UNNAMED `allowed_updates` TYPE IS NEVER DELIVERED AT ALL** — a subset means "and nothing else",
which is exactly how edits stay invisible: nothing in the payload is wrong, the update simply never
arrives.

EDITS: Telegram delivers an edited message as its own `edited_message` update carrying the WHOLE message
again (new text, same `message_id`) — never a diff, and never the text it replaced. It is off by default
in `allowed_updates`, exactly like `message_reaction`, which is why an edit can be invisible: the owner
fixes a typo, their screen reads as corrected, and the assistant answers the uncorrected text with
neither side able to see the divergence. An edit is extracted through the SAME path as a new message
(text, caption, reply-to, media) and marked `kind: "edit"`; the daemon decides what to do with it from
the `message_id` (replace it in the queue if it hasn't been answered yet, otherwise hand it over as a
marked-up new inbound). An edit that replaces its message while un-answered
is simple; one that arrives after the answer is already mid-flight is not — since the drainer pops by
position, rewriting the head of the queue answers the OLD text and discards the fix, so a mid-turn edit
arrives annotated instead of silently replacing anything. See ../docs/telegram-inbound-spec.md §6a.

BUTTON TAPS: a tap on one of the assistant's question pickers (`telegram_ask.py`) arrives as a
`callback_query` update — named explicitly in `allowed_updates`, the same switch one type further over
from edits. It is extracted as
`kind: "callback"` carrying the query's `id` (`callback_id`), the button's `data` and the question
message's id. **The `id` is an obligation, not a detail:** the Bot API leaves the client's button
spinning until `answerCallbackQuery` is called with it, which `telegram_ask.py resolve` does on every
path, including the ones that cannot record an answer. See ../docs/telegram-inbound-spec.md §6b.

ALBUMS: an album — several photos sent from one tap of Send — is not one update. Telegram delivers a
nine-photo album as NINE `message` updates sharing one `media_group_id` (Bot API 3.5), with the caption
on exactly one of them. This module extracts that id onto the record and **does nothing else with it**:
no coalescing, no holding, no reordering, so nothing is acked to Telegram before it has been read and
the offset rules below are untouched. The turn boundary is the daemon's business — it holds a
growing group and hands it over as ONE turn. See
`extract_media_group_id` and ../docs/telegram-inbound-spec.md §6c.

CUSTOM-EMOJI REACTIONS: Telegram Premium lets the owner react with ~any emoji, and those arrive as
`ReactionTypeCustomEmoji` — an opaque `custom_emoji_id`, no plain `emoji` field. Extraction resolves it to
its base emoji via getCustomEmojiStickers (batched, up to 200 ids/call) and a small local cache
(../state/custom-emoji-cache.json, gitignored like the rest of state/) so a repeat reaction costs no
network. Fail-open: no token, a network error, an unknown id, or a sticker with no `emoji` field all leave
the reaction's emoji blank — it still reaches the warm session, just mapped to the same "note" intent an
unrecognized plain emoji gets. See ../docs/telegram-inbound-spec.md §3.

POLL TRACE: every real cycle (a `getUpdates` actually attempted — GC mode and `--refetch` are not
polls and write nothing here) appends one line to `<offset-file's dir>/telegram-poll-trace.jsonl`
(override with `--poll-trace-file`), including the boring "ok, nothing new" case and the case where
`getUpdates` itself raised. This is the durable answer to "did the daemon call getUpdates during a
given gap, and what did Telegram return" — without it, every trace this module writes is conditioned
on a message actually arriving, and a dropped message leaves nothing to investigate. Each line: `{ts, ok, offset_in, offset_out, update_count, update_ids,
committed, error?}` — update_ids and counts only, **never message text**. Bounded growth via
`log_rotation.roll_closed` (same size trigger + generation cap as every other rotating `state/` log);
see `record_poll_trace` and `../state/README.md`.

USAGE:
  python telegram_poll.py                         # peek: return new messages, do NOT advance offset
  python telegram_poll.py --commit                # return new messages AND acknowledge them
  python telegram_poll.py --commit --timeout 25   # long-poll up to 25s (interactive use, not the sentinel)
  python telegram_poll.py --commit --download-dir ../state/inbox   # also fetch attachments (the daemon)
  python telegram_poll.py --refetch <file_id> --download-dir ../state/inbox   # re-fetch a failed download
  python telegram_poll.py --prune-days 30         # GC mode: sweep old attachments, no network (Dream)

Prints a one-line JSON result:
  {ok, count, messages:[{update_id,kind,message_id,chat_id,chat_type,from,text,date,caption?,
                         attachment?,reply_to?,edit_date?,callback_id?,data?,
                         message_thread_id?,is_topic_message?,media_group_id?}], next_offset}
where `kind` is "message" | "edit" | "reaction" | "callback" and `attachment` is
{kind,file_name,mime_type,file_size,local_path?,too_large?,error?,file_id?,refetchable?}.
Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling import also works when we're imported, not run

import log_rotation as lr  # noqa: E402 — bounded growth for the poll trace, same door every rotating
                           # state/ log goes through
import memory_write as mw  # noqa: E402 — atomic write; a torn `state/telegram-offset` re-reads or
                           # skips a batch of the owner's messages (see check_state_writes.py)
import telegram_http as th  # noqa: E402 — one transport, one place

ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_API_BASE")

# The per-poll-cycle trace filename, written beside whatever --offset-file this run is using (so a
# test that points --offset-file at a tempdir gets an isolated trace file too, with no separate
# default rooted in real state — see the "tests default into live state" hazard this sidesteps).
POLL_TRACE_FILENAME = "telegram-poll-trace.jsonl"

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

# Media fields we understand, in the order Telegram would populate them on a single message. Each of
# these carries a `file_id` — the same generic branch in extract_media() below handles all of them.
# `sticker` and `video_note` are here (../docs/telegram-capability-map.md §2.2 D1) because both are
# real fetchable files; outside this list a sticker falls all the way through — no media match, no
# text, an empty line, and the daemon's `if not line: continue` drops it with the offset already
# committed, so Telegram never sends it again.
MEDIA_KINDS = ("document", "photo", "voice", "audio", "video", "sticker", "video_note")
# Non-file inbound kinds (the same D1 fix): Telegram never puts a `file_id` on any of these — there
# is nothing extract_media() could fetch — so they would vanish the identical way a sticker did: no media match, no text, dropped, unrecoverable. describe_unsupported() below names each one
# in a short synthesized line instead, so at minimum the kind and its content survive to the warm
# session even though nothing was downloaded.
UNSUPPORTED_KINDS = ("location", "venue", "contact", "poll", "dice")
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
    # Atomic: this is the only record of how far the poller got, and an empty file reads as "no
    # offset" -- which replays every update Telegram still holds.
    mw.write_text(path, str(offset))


def record_poll_trace(trace_path: str, *, offset_in, offset_out, updates: list, ok: bool,
                      error: str | None = None, committed: bool = False,
                      max_bytes: int = lr.DEFAULT_MAX_BYTES, keep: int = lr.DEFAULT_KEEP) -> None:
    """Append one line to the durable per-poll-cycle trace — even on the boring "ok, nothing new"
    cycle, and even when `get_updates` itself raised.

    **Why it exists.** A plain-text message Telegram delivered can fail to reach this module's
    offset/router/turns traces, and every one of those is written only for a message that arrived —
    a poll cycle that called getUpdates and got nothing back would write nothing at all, so there
    would be no way to say whether the daemon was even polling during the gap. This function exists so a recurrence is
    diagnosable instead of invisible: one line per cycle, always, naming the offset going in, the
    offset coming out, how many updates Telegram returned, and their ids.

    **Never message TEXT.** update_ids and counts only — this is a durable surface and the owner's
    words do not belong on it.

    Fail-open, like every writer in this module: a poll cycle that could not be traced still happened
    and its updates were still processed above. Losing this row costs future diagnosis, never the
    message."""
    try:
        ids = [u.get("update_id") for u in (updates or [])
              if isinstance(u, dict) and u.get("update_id") is not None]
        record = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ok": bool(ok),
            "offset_in": offset_in,
            "offset_out": offset_out,
            "update_count": len(updates or []),
            "update_ids": ids,
            "committed": bool(committed),
        }
        if error:
            record["error"] = str(error)
        # A cheap size check every cycle (a no-op stat call unless the file is actually over the
        # trigger); the file is never held open between subprocess invocations, so roll_closed's
        # "assume nothing has it open" contract holds by construction here, not by luck.
        lr.roll_closed(trace_path, max_bytes=max_bytes, keep=keep)
        mw.append_text(trace_path, json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001 — a trace line must never cost the poll cycle it describes
        pass


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


def describe_unsupported(msg: dict) -> str | None:
    """A short, honest one-line description of a non-file inbound kind (UNSUPPORTED_KINDS) — a
    `location`, `venue`, `contact`, `poll` or `dice` — or None if the message carries none of them.

    None of these five ever have a `file_id`, so there is nothing to hand to `fetch_attachment`; the
    old behaviour was to fall all the way through `extract_media` and reach main() with an empty
    `text` and no attachment, which renders as an empty line and gets silently dropped (see
    UNSUPPORTED_KINDS above and ../docs/telegram-capability-map.md §2.2 D1). This names the kind and
    what it actually said instead of a generic placeholder, so a poll or a shared location is
    something the warm session can act on rather than a bare "something unsupported arrived"."""
    loc = msg.get("location")
    if isinstance(loc, dict) and loc.get("latitude") is not None and loc.get("longitude") is not None:
        return f"[shared a location: {loc['latitude']}, {loc['longitude']}]"
    venue = msg.get("venue")
    if isinstance(venue, dict) and venue.get("title"):
        address = venue.get("address")
        return f"[shared a venue: {venue['title']}" + (f", {address}" if address else "") + "]"
    contact = msg.get("contact")
    if isinstance(contact, dict) and contact.get("phone_number"):
        name = " ".join(p for p in (contact.get("first_name"), contact.get("last_name")) if p)
        return f"[shared a contact: {name or 'unnamed'}, {contact['phone_number']}]"
    poll = msg.get("poll")
    if isinstance(poll, dict) and poll.get("question"):
        return f"[shared a poll: {poll['question']}]"
    dice = msg.get("dice")
    if isinstance(dice, dict) and dice.get("value") is not None:
        return f"[rolled {dice.get('emoji') or '🎲'} — {dice['value']}]"
    return None


def resolve_text(msg: dict) -> str:
    """The item's `text` field: the sender's actual text if there is any, else a synthesized
    description for one of UNSUPPORTED_KINDS (describe_unsupported), else empty — never both, and
    never overriding real typed text. A media kind (MEDIA_KINDS) is untouched here; it gets its non-empty line
    from the `attachment` field main() adds separately."""
    text = msg.get("text") or ""
    return text if text else (describe_unsupported(msg) or "")


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


def extract_thread_id(msg: dict):
    """Which private-chat TOPIC this message arrived in, or `None` for the main chat.

    `Message.message_thread_id` is documented for supergroups **and private chats** (Bot API 9.3 —
    `../docs/telegram-capability-map.md` §2.1). The assistant sends pickers into topics
    (`telegram_topics.py`), so a reply the owner types in that thread arrives carrying this field, and
    a poller that never reads it cannot tell it apart from a message in the main chat.

    **AND THE DAEMON ROUTES ON IT.** This value picks which per-topic continuity cache the message is
    appended to, and rides back out on the assistant's reply, so a conversation stays in the thread
    it started in. What has
    not changed is anything about *delivery*: the chat id is the same, so the allowlist and the
    offset behave exactly as before and a message from a topic was never at risk of being dropped.

    **This is the field that decides, and `is_topic_message` is not.** Both are documented for
    private chats and both are absent-not-null when there is no topic, but only this one carries the
    id — a boolean cannot name a thread. `is_topic_message` is surfaced beside it (see `main`) as
    corroboration for a human reading the payload, and nothing routes on it: two fields that must
    agree are two fields that can disagree.

    `0` is not a thread. Telegram uses the general/main thread's absence, not a zero, and treating a
    falsy id as real would put a `message_thread_id: 0` on ordinary messages."""
    tid = msg.get("message_thread_id")
    if not isinstance(tid, int) or isinstance(tid, bool) or tid <= 0:
        return None
    return tid


def extract_media_group_id(msg: dict):
    """Which ALBUM this message is one member of, or `None` for a message that stands alone.

    Telegram has no "album" update. A nine-photo album sent from one tap of Send arrives as **nine
    separate `message` updates**, each carrying one photo, each carrying the same
    `Message.media_group_id` (Bot API 3.5), and with the caption on exactly ONE of them. A poller that
    never reads that field (`../docs/telegram-capability-map.md` §2.3) hands the daemon nine unrelated
    attachment lines, and it answers a slice of the conversation the owner was showing it — each time
    confidently, and each time wrong about something a later image corrects.

    **This function decides nothing and the poller coalesces nothing.** The id is carried out on the
    normalized record and the daemon's inbound path holds and merges on it. That split is deliberate: coalescing means *waiting*, waiting means a message
    exists only in memory for a moment, and the one thing this module must never do is hold a message
    it has already acknowledged to Telegram. The offset semantics here are untouched.

    Telegram documents the id as a String and its value is opaque — it is an equality key, never
    parsed, never ordered, never joined onto a path. A non-string (a future payload quirk, a number)
    is read as "no album" rather than coerced: the fail-open answer is the current behaviour, one
    message per photo, and that is a bug we have already survived. An empty string is likewise not a
    group — it would collapse every unrelated media message into one."""
    gid = msg.get("media_group_id")
    if not isinstance(gid, str):
        return None
    gid = gid.strip()
    return gid or None


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
    file called `NUL`/`CON` from resolving to a device rather than a file.

    **Both round trips retry.** They are pure reads, so `telegram_http.IDEMPOTENT` covers the whole
    recognised transient set in either phase — including a reset partway through the body, which is what
    is how an inbound photo gets lost and which a connect-only retry would sail straight past."""
    url = f"{api_base.rstrip('/')}/bot{token}/getFile?" + urllib.parse.urlencode({"file_id": file_id})
    payload = th.get_json(url, timeout=timeout)
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
    return th.download(dl, dest, timeout=timeout)


def fetch_attachment(media: dict, token: str, api_base: str, dest_dir: str | None) -> dict:
    """Turn one extracted media blob into the record the daemon enqueues, downloading it if we can.

    Never raises: an attachment is a convenience on top of the text pipeline, so a failed fetch costs us
    the file, never the message. The failure is reported in the record and the assistant says so in
    its own words — the daemon composes no prose.

    **A FAILURE KEEPS THE `file_id`, AND THAT IS THE DIFFERENCE BETWEEN A LOST FILE AND A DEFERRED ONE.**
    `telegram_get_file` has already retried by the time we get here, so reaching this branch means the
    transient window outlasted the policy — which is a reason to try again *later*, not a reason to
    forget the file. Telegram keeps a `file_id` valid long past that, so the id plus `refetchable` is a
    complete recovery handle: `telegram_poll.py --refetch <file_id>`. Without it the id dies with the
    exception and the only recovery is the owner noticing the reply ignored their photo and
    re-sending it by hand."""
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
        if media.get("file_id"):  # additive and only when present, like every other optional field
            att["file_id"] = media["file_id"]
            att["refetchable"] = True
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


def extract_callback(update: dict, allowed: set) -> dict | None:
    """A `callback_query` update — the owner tapping a button on one of the assistant's question
    pickers — as an inbound item, or None (see ../docs/telegram-inbound-spec.md §6b).

    Unlike every other kind here, this one carries an **obligation**: the Bot API spins the client's
    button until `answerCallbackQuery` is called with the query's `id`, so `callback_id` is the field
    that makes the item actionable at all. The rest is the tap itself (`data`, capped at 64 bytes by
    Telegram and carrying only a question id + an option index — never the option text) plus the
    message the keyboard is attached to, which is one of the ASSISTANT's messages, not the owner's.

    A query with no `id` is dropped: there is nothing we could answer, so there is nothing to do. A
    query from outside the allowlist is dropped for the same reason every other update kind is — and
    that deliberately leaves a stranger's button spinning rather than talking back to them. Our own
    keyboards are only ever sent into an allowlisted chat, so this cannot fire on one of the owner's."""
    cb = update.get("callback_query")
    if not isinstance(cb, dict) or not cb.get("id"):
        return None
    msg = cb.get("message")
    msg = msg if isinstance(msg, dict) else {}
    chat = msg.get("chat")
    chat = chat if isinstance(chat, dict) else {}
    chat_id = str(chat.get("id", ""))
    if allowed and chat_id not in allowed:
        return None  # outside the allowlist — ignored, still acknowledged via the offset advance
    frm = cb.get("from")
    frm = frm if isinstance(frm, dict) else {}
    return {
        "update_id": update.get("update_id"),
        "kind": "callback",
        "chat_id": chat_id,
        "chat_type": chat.get("type"),
        "from": frm.get("username") or frm.get("first_name"),
        "message_id": msg.get("message_id"),  # the question message the keyboard hangs off
        "callback_id": cb["id"],
        "data": cb.get("data") or "",
        "date": msg.get("date"),
        "text": "",
    }


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
    payload = th.get_json(url, timeout=timeout)
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


def message_payload(update: dict) -> tuple:
    """The Message this update carries and what kind it is: ``(msg, "message")`` for a new one,
    ``(msg, "edit")`` for an `edited_message`, ``(None, "")`` for anything else.

    An `edited_message` is byte-for-byte the same **Message** shape as a fresh one — same
    `message_id`, the full new text (never a diff), plus an `edit_date` — so everything downstream
    (media, caption, reply-to, the allowlist) reads it through the identical code rather than a
    parallel branch that could drift from it.

    Fail-open, like the rest of this module: a payload that isn't a non-empty dict (a partial update,
    a shape a future Bot API version invents) yields ``(None, "")`` and contributes nothing, rather
    than raising through `main`'s un-caught loop and costing the whole batch."""
    for key, kind in (("message", "message"), ("edited_message", "edit")):
        payload = update.get(key)
        if isinstance(payload, dict) and payload:
            return payload, kind
    return None, ""


def get_updates(token: str, api_base: str, offset, timeout: int, limit: int) -> list:
    # (../docs/telegram-capability-map.md §2.2 D4): only `chat_member`,
    # `message_reaction` and `message_reaction_count` are actually default-off. `edited_message` and
    # `callback_query` are NOT — the real mechanism is STICKINESS: Telegram's docs say "if not
    # specified, the previous setting will be used", so an earlier call that named `["message"]` alone
    # persists that narrowing across every later call, `allowed_updates` argument or not. Naming
    # `message` alone here would still be an implicit "and nothing else" — that was and remains the
    # whole edits defect (§6a): an edit could not arrive even in principle, so the owner's corrected
    # message and the assistant's answer diverged with nothing on either screen to show it. `callback_query` is the
    # same hazard one update type further over (§6b) — without it an inline keyboard renders and every
    # tap on it is a no-op that spins forever. In a private chat with the bot, the owner's reactions,
    # edits and taps all arrive without any admin rights. The fix either way is the same: name every
    # type this module wants, explicitly, every call.
    params = {"timeout": timeout, "limit": limit,
              "allowed_updates": json.dumps(["message", "edited_message", "message_reaction",
                                             "callback_query"])}
    if offset is not None:
        params["offset"] = offset
    url = f"{api_base.rstrip('/')}/bot{token}/getUpdates?" + urllib.parse.urlencode(params)
    # Network timeout slightly above the long-poll window so the server hangup wins.
    #
    # Retried: getUpdates is a pure read, and re-issuing it with the SAME offset returns the same
    # updates — which is precisely why the offset is not advanced anywhere near here. A reset during a
    # long poll used to surface as a whole polling cycle lost; now it costs a backoff. The wall-clock
    # ceiling is what keeps a 25 s long poll from turning into a 100 s one: it is checked before each
    # new attempt, so a couple of long attempts spend the budget and the caller gets its turn back.
    payload = th.get_json(url, timeout=timeout + 15)
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
    p.add_argument("--refetch", metavar="FILE_ID",
                   help="re-fetch ONE attachment by the `file_id` a failed download recorded, then exit. "
                        "Reads nothing and advances no offset — it is the recovery door for an "
                        "`attachment` record carrying refetchable: true.")
    p.add_argument("--env-file", help="KEY=VALUE file with TELEGRAM_* settings (kept untracked)")
    p.add_argument("--custom-emoji-cache", default=DEFAULT_CUSTOM_EMOJI_CACHE,
                   help="local cache mapping a Premium custom_emoji_id -> its base emoji")
    p.add_argument("--poll-trace-file",
                   help="durable one-line-per-cycle poll trace (default: alongside --offset-file, "
                        "so a test/tool pointing --offset-file at a tempdir gets an isolated trace "
                        "file too, never the real one)")
    args = p.parse_args()
    trace_path = args.poll_trace_file or os.path.join(
        os.path.dirname(os.path.abspath(args.offset_file)), POLL_TRACE_FILENAME)

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

    # Recovery mode: one file, by id, no getUpdates and no offset touched. A `file_id` outlives the
    # download that failed on it, so this is the door that makes `refetchable: true` a fact rather
    # than a claim — the assistant can run it from the placeholder line without asking the owner.
    if args.refetch:
        dest_dir = args.download_dir or DEFAULT_INBOX_DIR
        try:
            path = telegram_get_file(token, api_base, args.refetch, dest_dir)
            print(json.dumps({"ok": True, "refetched": args.refetch, "local_path": path}))
            return 0
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "refetched": args.refetch, "error": str(e)}))
            return 1

    offset = read_offset(args.offset_file)
    try:
        updates = get_updates(token, api_base, offset, args.timeout, args.limit)
    except Exception as e:  # noqa: BLE001
        # A failed getUpdates is exactly the boring-but-critical case the trace exists for: the
        # offset didn't move, but a durable line still says a call was attempted and what it did.
        record_poll_trace(trace_path, offset_in=offset, offset_out=offset, updates=[],
                          ok=False, error=str(e))
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    messages = []
    max_update_id = None
    for u in updates:
        uid = u.get("update_id")
        if uid is not None:
            max_update_id = uid if max_update_id is None else max(max_update_id, uid)
        msg, kind = message_payload(u)
        if msg is None:
            other = extract_reaction(u, allowed) or extract_callback(u, allowed)
            if other:
                messages.append(other)
            continue  # other non-message update; still acknowledged via offset advance
        chat = msg.get("chat")
        chat = chat if isinstance(chat, dict) else {}
        chat_id = str(chat.get("id", ""))
        if allowed and chat_id not in allowed:
            continue  # outside the allowlist — ignore but still acknowledge
        frm = msg.get("from")
        frm = frm if isinstance(frm, dict) else {}
        item = {
            "update_id": uid,
            "kind": kind,
            # An edit arrives under the id its ORIGINAL arrived under — the only handle the daemon has
            # for finding what this edit corrects (the daemon's edit handling). Recorded on every
            # message, not just on edits: the original has to be findable before the edit shows up.
            "message_id": msg.get("message_id"),
            "chat_id": chat_id,
            "chat_type": chat.get("type"),
            "from": frm.get("username") or frm.get("first_name"),
            "text": resolve_text(msg),
            "date": msg.get("date"),
        }
        if kind == "edit" and msg.get("edit_date"):
            item["edit_date"] = msg["edit_date"]
        # Additive, and only when present: a plain text message serializes exactly as it always has.
        # Media is fetched only for allowlisted chats — we're already past that gate here.
        if msg.get("caption"):
            item["caption"] = msg["caption"]
        reply_to = extract_reply_to(msg)
        if reply_to:
            item["reply_to"] = reply_to
        thread_id = extract_thread_id(msg)
        if thread_id is not None:
            item["message_thread_id"] = thread_id
        # Additive and only when Telegram said so. `is_topic_message` is documented as absent (not
        # false) on a message that is not in a topic, and it is reproduced here with that shape
        # intact: `True` or nothing. Compared with `is True` rather than truthiness so a future
        # payload quirk — the string "true", a 1 — is not silently promoted into a claim we would
        # then have to defend. It is corroboration, not the routing field; see `extract_thread_id`.
        if msg.get("is_topic_message") is True:
            item["is_topic_message"] = True
        # Additive, and carried WITHOUT being acted on — the whole point of `extract_media_group_id`.
        # It rides out on the record so the daemon can hold and merge an album into one turn; nothing
        # here waits, reorders or drops on account of it, and the offset advances exactly as it did.
        group_id = extract_media_group_id(msg)
        if group_id is not None:
            item["media_group_id"] = group_id
        media = extract_media(msg)
        if media:
            # An edit runs this too, so editing the CAPTION on a photo re-downloads the photo. Named
            # rather than special-cased: the duplicate costs one local copy of a file already sent
            # (the inbox is swept nightly), and a media-only branch here would be a second extraction
            # path that could drift from this one — which is exactly what §1 of the spec warned about.
            item["attachment"] = fetch_attachment(media, token, api_base, args.download_dir)
        messages.append(item)

    # Batched across the whole poll, not per-reaction: however many custom-emoji reactions came in this
    # round, it costs at most ceil(distinct_ids / 200) API calls (usually zero once the cache is warm).
    resolve_reactions(messages, token, api_base, args.custom_emoji_cache)

    # THE OFFSET ADVANCES LAST, AND ONLY AFTER A READ THAT ACTUALLY SUCCEEDED AND WAS PROCESSED.
    # This was already true and is written down here so a later edit has to argue with it rather than
    # merely not notice it: a failed `get_updates` returns above with the offset untouched, so the same
    # updates come back on the next poll, and every extraction step runs before this line. Advancing
    # earlier — or on a partial batch — would acknowledge messages to Telegram that nothing has read,
    # and Telegram never sends an acknowledged update again.
    #
    # A failed *attachment download* deliberately does NOT hold the offset back: the message itself was
    # read fine, and re-delivering it would re-run the whole batch to chase one file. The `file_id`
    # recorded by `fetch_attachment` is what makes that safe — the file stays reachable without the
    # message having to be.
    next_offset = (max_update_id + 1) if max_update_id is not None else offset
    committed = bool(args.commit and max_update_id is not None)
    if committed:
        write_offset(args.offset_file, next_offset)

    # The trace fires on EVERY successful cycle, including the empty batch — `updates` (Telegram's raw
    # return, before the allowlist/kind filtering that produces `messages`) is what answers "did the
    # daemon call getUpdates during the gap and what came back" — the question a dropped message
    # leaves.
    record_poll_trace(trace_path, offset_in=offset, offset_out=next_offset, updates=updates,
                      ok=True, committed=committed)

    print(json.dumps({
        "ok": True, "count": len(messages), "messages": messages,
        "next_offset": next_offset, "committed": committed,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
