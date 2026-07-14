#!/usr/bin/env python3
"""Ingest an SMS Backup & Restore XML export into the normalized archive schema. Standard library only.

"SMS Backup & Restore" (the de-facto Android backup app) writes ``sms-YYYYMMDDHHMMSS.xml`` with an
``<smses>`` root of flat ``<sms>`` elements (``address`` / ``date`` epoch-millis / ``type`` 1=received
2=sent / ``body``) and ``<mms>`` elements (``msg_box`` 1=received 2=sent, addresses in an ``address``
attribute and/or nested ``<addrs>``, media as base64 ``<part>`` blobs). This streams that file with
``iterparse`` (exports run to hundreds of MB), keeps only the messages exchanged with one person, and
emits a normalized ``normalized/sms.json`` for the aggregator — no network, no credentials.

Person matching: each message's number(s) are compared against the registry's
``services.sms.numbers`` list by **last-10-digits** (all non-digits stripped, suffixes compared), so
``+1 (555) 123-4567``, ``555-123-4567``, and ``15551234567`` all match. **International caveat:**
this is a NANP-flavored heuristic — two numbers that share their last ten digits collide, and
short codes (fewer than ten digits) only match when the stripped digits are identical.

Timestamps: ``date`` is epoch **milliseconds** on ``<sms>``; on ``<mms>`` it is seconds in some app
versions and milliseconds in others — values ≥ 10^12 are treated as milliseconds (the same guard is
applied to both element kinds).

MMS media: base64 parts are decoded straight into the person's archive ``media/`` dir via
``copy_media_dedup`` (stable ``sms_<msgid>_<n>.<ext>`` names, byte-identical blobs deduped), and the
record references the decoded file like any other local media. ``text/plain`` parts become the
message body; ``application/smil`` layout parts are dropped.

USAGE:
  python sms_ingest.py --person alex --xml "C:/.../sms-20260101120000.xml"
  python sms_ingest.py --person alex                         # xml_file from the person registry
  python sms_ingest.py --person alex --numbers +15551234567  # override the registry numbers

Prints a one-line JSON result {ok, person, count, media_count, skipped_nonmatch, skipped_other, out}.
Exit 0 on success, non-zero on failure.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

import archive_common as ac

_NON_DIGITS = re.compile(r"\D+")

#: Directions shared by ``<sms type>`` and ``<mms msg_box>`` (1=received, 2=sent; drafts/outbox/
#: failed/queued values are skipped and counted).
_DIRECTION = {"1": "from_them", "2": "from_me"}

#: MMS part content-type → archive file extension (else the part's own name/cl extension, else .bin).
_MIME_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/bmp": ".bmp",
    "video/mp4": ".mp4", "video/3gpp": ".3gp",
    "audio/mpeg": ".mp3", "audio/amr": ".amr", "audio/mp4": ".m4a",
    "text/x-vcard": ".vcf",
}


def norm_number(s) -> str:
    """Strip non-digits and keep the last 10 — the comparison (and thread) key. See module docstring."""
    return _NON_DIGITS.sub("", str(s or ""))[-10:]


def numbers_match(a, b) -> bool:
    """Last-10-digit equality (empty never matches)."""
    na = norm_number(a)
    return bool(na) and na == norm_number(b)


def epoch_seconds(v) -> int:
    """Epoch attribute → seconds. Values ≥ 10^12 are milliseconds (varies by element/app version)."""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return 0
    return n // 1000 if n >= 10 ** 12 else n


def mime_kind(ct: str) -> str:
    """MMS part content-type → normalized media kind."""
    ct = (ct or "").lower()
    if ct == "image/gif":
        return "animation"
    for prefix, kind in (("image/", "photo"), ("video/", "video"), ("audio/", "audio")):
        if ct.startswith(prefix):
            return kind
    return "file"


def part_ext(part) -> str:
    """Archive extension for an MMS part: by content-type, else its name/cl attribute, else .bin."""
    ct = (part.get("ct") or "").lower()
    if ct in _MIME_EXT:
        return _MIME_EXT[ct]
    for attr in ("name", "cl"):
        ext = os.path.splitext(part.get(attr) or "")[1]
        if ext:
            return ext
    return ".bin"


def sms_record(elem, person: str, display: str):
    """One ``<sms>`` element → a normalized record, or None for non-inbox/sent types (drafts etc.)."""
    direction = _DIRECTION.get(elem.get("type"))
    if direction is None:
        return None
    ts = epoch_seconds(elem.get("date"))
    contact = elem.get("contact_name")
    sender = "" if direction == "from_me" else \
        (contact if contact and contact != "(Unknown)" else display)
    return ac.make_record(
        service="sms", service_msg_id=elem.get("date") or f"sms-{ts}", person=person,
        thread_id=norm_number(elem.get("address")), direction=direction, ts_utc=ts,
        sender_name=sender, text=elem.get("body") or "",
        service_meta={"address": elem.get("address"), "date_sent": elem.get("date_sent"),
                      "protocol": elem.get("protocol")})


def mms_addresses(elem) -> list:
    """All numbers on an ``<mms>``: the ``address`` attribute (``~``-joined for groups) + ``<addr>``s."""
    out = [a for a in (elem.get("address") or "").split("~") if a]
    out.extend(a.get("address") for a in elem.iter("addr") if a.get("address"))
    return out


def mms_record(elem, person: str, display: str, thread: str, media_dir: str, seen: dict,
               stats: dict):
    """One ``<mms>`` element → a normalized record (text parts → body, media parts → ``media/``)."""
    direction = _DIRECTION.get(elem.get("msg_box"))
    if direction is None:
        return None
    ts = epoch_seconds(elem.get("date"))
    mid = elem.get("m_id") or elem.get("date") or f"mms-{ts}"
    texts, media = [], []
    for part in elem.iter("part"):
        ct = (part.get("ct") or "").lower()
        if ct == "application/smil":
            continue
        if ct == "text/plain":
            if part.get("text"):
                texts.append(part.get("text"))
            continue
        data = part.get("data")
        if not data:
            continue
        try:
            blob = base64.b64decode(data)
        except ValueError:
            stats["media_errors"] += 1
            continue
        # Materialize the blob, then reuse the shared dedup-copy (stable name, sha1 dedup).
        # The name index is the record's media-list index (NOT the XML part index) so the
        # aggregator's own copy_media_dedup pass regenerates the same name and self-copy no-ops.
        fd, tmp = tempfile.mkstemp(prefix=".sms-part-", suffix=part_ext(part))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
            rel = ac.copy_media_dedup(tmp, media_dir, "sms", str(mid), len(media), seen)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        media.append(ac.make_media(
            kind=mime_kind(ct), local_path=os.path.join(media_dir, os.path.basename(rel)),
            archive_path=rel, mime=ct, nbytes=len(blob),
            meta={"original_name": part.get("name") or part.get("cl")}))
    contact = elem.get("contact_name")
    sender = "" if direction == "from_me" else \
        (contact if contact and contact != "(Unknown)" else display)
    return ac.make_record(
        service="sms", service_msg_id=mid, person=person, thread_id=thread, direction=direction,
        ts_utc=ts, sender_name=sender, text="\n".join(texts), media=media,
        service_meta={"address": elem.get("address"), "m_id": elem.get("m_id"), "mms": True})


def ingest(xml_path: str, person: str, numbers: list, display: str, media_dir: str) -> tuple:
    """Stream the backup XML → (normalized records for this person, stats dict).

    ``iterparse`` + aggressive element clearing keeps memory flat on multi-hundred-MB exports.
    Output is deterministic for a given file and the writer atomically replaces the whole output,
    so re-running is idempotent (same semantics as ``telegram_ingest``).
    """
    stats = {"sms": 0, "mms": 0, "skipped_nonmatch": 0, "skipped_other": 0, "media_errors": 0}
    seen: dict = {}
    records = []
    context = ET.iterparse(xml_path, events=("start", "end"))
    _, root = next(context)  # grab the root so processed children can be dropped from it
    for event, elem in context:
        if event != "end" or elem.tag not in ("sms", "mms"):
            continue
        try:
            if elem.tag == "sms":
                if not any(numbers_match(elem.get("address"), n) for n in numbers):
                    stats["skipped_nonmatch"] += 1
                    continue
                rec = sms_record(elem, person, display)
                key = "sms"
            else:
                peer = next((c for c in mms_addresses(elem)
                             if any(numbers_match(c, n) for n in numbers)), None)
                if peer is None:
                    stats["skipped_nonmatch"] += 1
                    continue
                rec = mms_record(elem, person, display, norm_number(peer), media_dir, seen, stats)
                key = "mms"
            if rec is None:
                stats["skipped_other"] += 1
            else:
                records.append(rec)
                stats[key] += 1
        except Exception:  # noqa: BLE001 - one malformed row never kills the ingest
            stats["skipped_other"] += 1
        finally:
            elem.clear()
            root.clear()
    return records, stats


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest an SMS Backup & Restore XML export into the normalized schema.")
    p.add_argument("--person", required=True)
    p.add_argument("--xml", help="the sms-*.xml backup file (else from the person registry)")
    p.add_argument("--numbers", help="comma-separated override of the person's numbers")
    p.add_argument("--people-file", default=ac.DEFAULT_PEOPLE_FILE)
    p.add_argument("--out", help="output path (default <ARCHIVE_OUT_DIR>/<person>/normalized/sms.json)")
    p.add_argument("--media-dir", help="decoded-MMS dir (default <ARCHIVE_OUT_DIR>/<person>/media)")
    p.add_argument("--env-file")
    args = p.parse_args()

    try:
        people = ac.load_people(args.people_file)
        svc = ac.person_service(people, args.person, "sms")
        xml_path = args.xml or svc.get("xml_file")
        if not xml_path:
            return _fail(f"no SMS xml for {args.person!r} (pass --xml)")
        if not os.path.exists(xml_path):
            return _fail(f"no such file {xml_path!r}")
        numbers = ([n.strip() for n in args.numbers.split(",") if n.strip()] if args.numbers
                   else list(svc.get("numbers") or []))
        if not numbers:
            return _fail(f"no SMS numbers for {args.person!r} (registry services.sms.numbers or --numbers)")

        cfg = ac.load_env(args.env_file)
        out = args.out or os.path.join(cfg["ARCHIVE_OUT_DIR"], args.person, "normalized", "sms.json")
        media_dir = args.media_dir or os.path.join(cfg["ARCHIVE_OUT_DIR"], args.person, "media")

        records, stats = ingest(xml_path, args.person, numbers,
                                ac.person_display(people, args.person), media_dir)
        media_count = sum(len(r["media"]) for r in records)

        env = ac.envelope(args.person, "sms", records, sources=[os.path.abspath(xml_path)],
                          generated_at=ac.iso_utc(int(time.time())))
        ac.write_json(out, env)
        print(json.dumps({"ok": True, "person": args.person, "count": len(records),
                          "media_count": media_count, "skipped_nonmatch": stats["skipped_nonmatch"],
                          "skipped_other": stats["skipped_other"], "out": out}, ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        return _fail(str(e))


def _fail(msg: str) -> int:
    print(json.dumps({"ok": False, "error": msg}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
