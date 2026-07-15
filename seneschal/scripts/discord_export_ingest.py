#!/usr/bin/env python3
"""Ingest the official Discord data package into the normalized archive schema. Standard library only.

Discord's "Request all of my data" (Settings → Privacy & Safety) mails a zip whose ``messages/`` tree
holds one ``c<channel_id>/`` dir per channel — ``channel.json`` (id, type, DM ``recipients``) plus
``messages.json`` (the message rows) — and an ``index.json`` mapping channel ids to display names.
This reads that extracted package and emits a normalized ``normalized/discord.json`` for the
aggregator — no network, no credentials (identities come from the person registry).

**IMPORTANT — the package contains ONLY the owner's own messages.** Discord exports just the
requesting account's half of every conversation (nobody else's messages are included), so every
record here is ``direction=from_me`` and the merged archive shows the owner's side of Discord
threads only. For both sides of a channel, the bot REST collector (``discord_poll.py``) remains the
supplementary live source wherever the bot is present.

Other quirks this collector absorbs (or documents):
  * **Timestamps are naive strings** (``"2024-01-15 14:23:45"``) whose zone has varied across package
    vintages and is undocumented — they are **treated as UTC**. Expect skew up to a few hours on
    older packages.
  * **Attachments are expiring CDN URLs** (space-separated in one field). They are recorded as
    ``remote_url`` with ``meta.missing=true`` and are **never downloaded** — the signed links are
    usually already dead by the time the package arrives.
  * ``ID`` may be an int or a string depending on vintage; both normalize to the same record.
  * **Older packages ship ``messages.csv`` instead of ``messages.json``** — those are not supported;
    the fix is to request a fresh package (current ones are JSON).

Channel selection: explicit ``--channel-id`` wins; otherwise channels are auto-selected from the
person registry's ``services.discord`` block — every id in ``channel_ids`` plus any DM/group-DM
whose ``recipients`` include ``user_id``.

USAGE:
  python discord_export_ingest.py --person alex --package "C:/.../discord-package"
  python discord_export_ingest.py --person alex --channel-id 111222333444555666   # explicit channel
  python discord_export_ingest.py --person alex                                    # package_dir from the registry

Prints a one-line JSON result {ok, person, count, channels, media_count, out}. Exit 0/non-zero.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import archive_common as ac

#: CDN-URL extension → normalized media kind (best effort; anything else is "file").
_EXT_KIND = {
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".webp": "photo", ".bmp": "photo",
    ".gif": "animation",
    ".mp4": "video", ".mov": "video", ".webm": "video", ".mkv": "video",
    ".mp3": "audio", ".ogg": "audio", ".wav": "audio", ".m4a": "audio", ".flac": "audio",
}


def parse_ts(stamp) -> int:
    """``"2024-01-15 14:23:45"`` (naive → **treated as UTC**) or ISO-with-offset → epoch seconds."""
    s = str(stamp or "").strip()
    if not s:
        return 0
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def attachment_media(att_field) -> list:
    """Space-separated CDN URLs → media entries (remote_url only; links expire, nothing is fetched)."""
    out = []
    for url in str(att_field or "").split():
        name = os.path.basename(urlsplit(url).path)
        ext = os.path.splitext(name)[1].lower()
        out.append(ac.make_media(kind=_EXT_KIND.get(ext, "file"), remote_url=url,
                                 meta={"missing": True, "original_name": name}))
    return out


def _get(msg: dict, *names, default=None):
    """First present key wins — package vintages differ on ``ID``/``id`` capitalization."""
    for n in names:
        if n in msg:
            return msg[n]
    return default


def _recipient_ids(channel: dict) -> set:
    """DM/group-DM recipient ids as strings (entries may be bare ids or ``{"id": ...}`` dicts)."""
    ids = set()
    for r in channel.get("recipients") or []:
        rid = r.get("id") if isinstance(r, dict) else r
        if rid is not None:
            ids.add(str(rid))
    return ids


def discover_channels(package_dir: str) -> list:
    """Scan ``messages/c<id>/`` dirs → ``[{"id", "channel", "dir"}, ...]`` (channel.json parsed)."""
    mdir = os.path.join(package_dir, "messages")
    if not os.path.isdir(mdir):
        raise ValueError(f"no messages/ dir under {package_dir!r} — is this an extracted Discord data package?")
    chans = []
    for entry in sorted(os.listdir(mdir)):
        cdir = os.path.join(mdir, entry)
        if not entry.startswith("c") or not os.path.isdir(cdir):
            continue
        channel = {}
        cjson = os.path.join(cdir, "channel.json")
        if os.path.exists(cjson):
            with open(cjson, "r", encoding="utf-8") as fh:
                channel = json.load(fh) or {}
        chans.append({"id": str(channel.get("id") or entry[1:]), "channel": channel, "dir": cdir})
    return chans


def select_channels(channels: list, channel_ids=None, user_id=None) -> list:
    """Union of explicit ``channel_ids`` and DM/group-DMs whose recipients include ``user_id``."""
    want = {str(c) for c in (channel_ids or [])}
    uid = str(user_id) if user_id else None
    out = []
    for c in channels:
        if c["id"] in want or (uid is not None and uid in _recipient_ids(c["channel"])):
            out.append(c)
    return out


def channel_messages_path(cdir: str) -> str:
    """The channel's messages.json — with a clear error for the unsupported CSV vintage."""
    j = os.path.join(cdir, "messages.json")
    if os.path.exists(j):
        return j
    if os.path.exists(os.path.join(cdir, "messages.csv")):
        raise ValueError(
            f"{cdir!r} is the older CSV package vintage (messages.csv) — not supported. "
            "Request a fresh data package from Discord (Settings → Privacy & Safety → Request all of "
            "my data); current packages export messages.json.")
    raise ValueError(f"no messages.json in {cdir!r}")


def ingest_channel(chan: dict, person: str, sender_name: str, channel_name=None) -> tuple:
    """Parse one channel's ``messages.json`` → (normalized records, source path).

    Every record is ``from_me`` — the package holds the owner's own messages only (see module
    docstring). Deterministic for a given package, and the writer atomically replaces the whole
    output file, so re-running is idempotent (same semantics as ``telegram_ingest``).
    """
    msgs_path = channel_messages_path(chan["dir"])
    with open(msgs_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("messages", [])
    meta = {"channel_name": channel_name, "channel_type": chan["channel"].get("type")}
    records = []
    for msg in data:
        try:
            records.append(ac.make_record(
                service="discord", service_msg_id=_get(msg, "ID", "id"), person=person,
                thread_id=chan["id"], direction="from_me",
                ts_utc=parse_ts(_get(msg, "Timestamp", "timestamp")), sender_name=sender_name,
                text=str(_get(msg, "Contents", "contents", default="") or ""),
                media=attachment_media(_get(msg, "Attachments", "attachments")),
                service_meta=dict(meta)))
        except Exception:  # noqa: BLE001 - one malformed message never kills the ingest
            continue
    return records, msgs_path


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest a Discord data package into the normalized schema.")
    p.add_argument("--person", required=True)
    p.add_argument("--package", help="the extracted data-package dir (else from the person registry)")
    p.add_argument("--channel-id", action="append", dest="channel_ids",
                   help="ingest exactly these channel ids (repeatable; overrides registry auto-select)")
    p.add_argument("--people-file", default=ac.DEFAULT_PEOPLE_FILE)
    p.add_argument("--out", help="output path (default <ARCHIVE_OUT_DIR>/<person>/normalized/discord.json)")
    p.add_argument("--env-file")
    args = p.parse_args()

    try:
        people = ac.load_people(args.people_file)
        svc = ac.person_service(people, args.person, "discord")
        package = args.package or svc.get("package_dir")
        if not package:
            return _fail(f"no Discord package_dir for {args.person!r} (pass --package)")
        channels = discover_channels(package)
        if args.channel_ids:
            selected = select_channels(channels, channel_ids=args.channel_ids)
        else:
            selected = select_channels(channels, channel_ids=svc.get("channel_ids"),
                                       user_id=svc.get("user_id"))
        if not selected:
            return _fail(f"no channels matched for {args.person!r} (registry services.discord needs "
                         "user_id and/or channel_ids, or pass --channel-id)")

        index_path = os.path.join(package, "messages", "index.json")
        index = {}
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as fh:
                index = json.load(fh) or {}
        sender = (people.get("owner", {}) or {}).get("display_name") or ""

        records, sources = [], []
        for chan in selected:
            recs, src = ingest_channel(chan, args.person, sender,
                                       chan["channel"].get("name") or index.get(chan["id"]))
            records.extend(recs)
            sources.append(src)
        media_count = sum(len(r["media"]) for r in records)

        cfg = ac.load_env(args.env_file)
        out = args.out or os.path.join(cfg["ARCHIVE_OUT_DIR"], args.person, "normalized", "discord.json")
        env = ac.envelope(args.person, "discord", records, sources=sources,
                          generated_at=ac.iso_utc(int(time.time())))
        ac.write_json(out, env)
        print(json.dumps({"ok": True, "person": args.person, "count": len(records),
                          "channels": [c["id"] for c in selected], "media_count": media_count,
                          "out": out}, ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        return _fail(str(e))


def _fail(msg: str) -> int:
    print(json.dumps({"ok": False, "error": msg}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
