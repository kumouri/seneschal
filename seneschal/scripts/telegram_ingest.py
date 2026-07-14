#!/usr/bin/env python3
"""Ingest a Telegram Desktop chat export into the normalized archive schema. Standard library only.

Telegram Desktop's "Export chat history" (format **JSON**, include media) produces a ``result.json`` plus
media subfolders (``photos/``, ``video_files/``, ``files/``, ``voice_messages/``, ``stickers/``) with the
files already downloaded locally. This reads that export and emits a normalized ``normalized/telegram.json``
for the aggregator — no network, no credentials (identities come from the person registry).

USAGE:
  python telegram_ingest.py --person alex                          # export dir from the registry
  python telegram_ingest.py --person alex --export-dir "C:/.../ChatExport_YYYY-MM-DD"
  python telegram_ingest.py --person alex --skip-service            # drop join/call service rows

Prints a one-line JSON result {ok, person, count, media_count, out}. Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import archive_common as ac

_DIGITS = re.compile(r"\d+")


def tg_uid(from_id) -> str:
    """`"user2000000002"` / `123` → `"2000000002"` (digits only) for id comparison."""
    if from_id is None:
        return ""
    m = _DIGITS.search(str(from_id))
    return m.group(0) if m else ""


def flatten_text(msg: dict) -> tuple[str, object]:
    """Return (plaintext, raw) for a message. Handles string OR entity-list ``text``.

    Prefers ``text_entities`` (the canonical reconstruction); keeps the original list as ``text_raw``
    when ``text`` was a list (lossless).
    """
    ents = msg.get("text_entities")
    raw = None
    if isinstance(ents, list) and ents:
        plain = "".join(e.get("text", "") for e in ents if isinstance(e, dict))
        if isinstance(msg.get("text"), list):
            raw = msg["text"]
        return plain, raw
    t = msg.get("text")
    if isinstance(t, str):
        return t, None
    if isinstance(t, list):
        parts = []
        for el in t:
            if isinstance(el, str):
                parts.append(el)
            elif isinstance(el, dict):
                parts.append(el.get("text", ""))
        return "".join(parts), t
    return "", None


def resolve_media(msg: dict, export_dir: str) -> list:
    """Build normalized media entries from a Telegram message (files already local)."""
    out = []
    rel = None
    kind_hint = None
    if msg.get("photo"):
        rel, kind_hint = msg["photo"], "photo"
    elif msg.get("file"):
        rel = msg["file"]
        kind_hint = msg.get("media_type") or ("animation" if msg.get("mime_type", "").startswith("video") else "file")
    if not rel:
        return out
    local = os.path.abspath(os.path.join(export_dir, rel))
    exists = os.path.exists(local)
    nbytes = os.path.getsize(local) if exists else None
    thumb = msg.get("thumbnail")
    meta = {
        "duration_seconds": msg.get("duration_seconds"),
        "sticker_emoji": msg.get("sticker_emoji"),
        "width": msg.get("width"), "height": msg.get("height"),
        "original_name": os.path.basename(rel),
        "thumbnail": (os.path.abspath(os.path.join(export_dir, thumb)) if thumb else None),
        "missing": not exists,
    }
    out.append(ac.make_media(kind=kind_hint, local_path=(local if exists else None),
                             mime=msg.get("mime_type"), nbytes=nbytes, meta=meta))
    return out


def ingest(export_dir: str, person: str, owner_id: str, peer_id: str, chat_id: str,
           skip_service: bool = False) -> list:
    """Parse ``result.json`` → normalized records."""
    with open(os.path.join(export_dir, "result.json"), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    chat_id = chat_id or str(data.get("id") or "")
    records = []
    for msg in data.get("messages", []):
        try:
            ts = int(msg.get("date_unixtime") or 0)
            if msg.get("type") == "service":
                if skip_service:
                    continue
                action = msg.get("action", "event")
                records.append(ac.make_record(
                    service="telegram", service_msg_id=msg.get("id"), person=person,
                    thread_id=chat_id, direction="system", ts_utc=ts,
                    text=f"— {action.replace('_', ' ')} —",
                    service_meta={k: v for k, v in msg.items() if k not in ("text", "text_entities")}))
                continue
            fid = tg_uid(msg.get("from_id"))
            direction = "from_me" if fid == tg_uid(owner_id) else "from_them"
            plain, raw = flatten_text(msg)
            media = resolve_media(msg, export_dir)
            edited = int(msg.get("edited_unixtime") or 0) or None
            records.append(ac.make_record(
                service="telegram", service_msg_id=msg.get("id"), person=person, thread_id=chat_id,
                direction=direction, ts_utc=ts, sender_name=msg.get("from") or "",
                text=plain, text_raw=raw, media=media, edited_ts_utc=edited,
                reply_to=msg.get("reply_to_message_id"),
                service_meta={"forwarded_from": msg.get("forwarded_from")}))
        except Exception:  # noqa: BLE001 - one malformed message never kills the ingest
            continue
    return records


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest a Telegram Desktop export into the normalized schema.")
    p.add_argument("--person", required=True)
    p.add_argument("--export-dir", help="the ChatExport_* dir (else from the person registry)")
    p.add_argument("--people-file", default=ac.DEFAULT_PEOPLE_FILE)
    p.add_argument("--owner-id", help="override the owner's Telegram user id")
    p.add_argument("--peer-id", help="override the peer's Telegram user id")
    p.add_argument("--out", help="output path (default <ARCHIVE_OUT_DIR>/<person>/normalized/telegram.json)")
    p.add_argument("--env-file")
    p.add_argument("--skip-service", action="store_true")
    args = p.parse_args()

    try:
        people = ac.load_people(args.people_file)
        svc = ac.person_service(people, args.person, "telegram")
        export_dir = args.export_dir or svc.get("export_dir")
        if not export_dir:
            return _fail(f"no Telegram export_dir for {args.person!r} (pass --export-dir)")
        if not os.path.exists(os.path.join(export_dir, "result.json")):
            return _fail(f"no result.json in {export_dir!r}")
        owner_id = args.owner_id or ac.owner_id(people, "telegram") or ""
        peer_id = args.peer_id or svc.get("user_id") or ""
        chat_id = svc.get("user_id") or ""

        records = ingest(export_dir, args.person, owner_id, peer_id, chat_id, args.skip_service)
        media_count = sum(len(r["media"]) for r in records)

        cfg = ac.load_env(args.env_file)
        out = args.out or os.path.join(cfg["ARCHIVE_OUT_DIR"], args.person, "normalized", "telegram.json")
        env = ac.envelope(args.person, "telegram", records,
                          sources=[os.path.join(export_dir, "result.json")],
                          generated_at=ac.iso_utc(int(time.time())))
        ac.write_json(out, env)
        print(json.dumps({"ok": True, "person": args.person, "count": len(records),
                          "media_count": media_count, "out": out}, ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        return _fail(str(e))


def _fail(msg: str) -> int:
    print(json.dumps({"ok": False, "error": msg}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
