#!/usr/bin/env python3
"""Shared plumbing for the assistant's person-centric message archiver. Standard library only.

Every service collector/ingester (Telegram today, Discord/SMS/Email next) emits the **same flat
normalized record** so the aggregator is service-agnostic — it merges records from any source by
``ts_utc`` (integer epoch, UTC) onto one timeline. This module owns:

  * the normalized-record builder + light validation (``make_record`` / ``REQUIRED_FIELDS``),
  * the **person registry** loader (``load_people`` and its lookups) — maps a person key to per-service
    identities plus the owner's own ids, which is what makes message ``direction`` computable,
  * timezone helpers for display (``local_dt`` / ``hhmm`` / ``local_date``) — America/Chicago via a
    hand-rolled DST rule, because this stack runs on a tzdata-less Windows Python where
    ``zoneinfo.ZoneInfo("America/Chicago")`` raises (same reason as ``health_common.central_offset``),
  * media helpers (classify / stable-name / sha1 / copy-dedup),
  * text helpers (service-message HTML → plaintext),
  * an atomic ``write_json`` (mkstemp + os.replace, mirroring ``presence_common``).

No credentials here — this module is I/O-light and pure enough to unit-test with no network. The only
config it reads is non-secret (``ARCHIVE_OUT_DIR``).
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.normpath(os.path.join(HERE, "..", "state"))
DEFAULT_PEOPLE_FILE = os.path.join(STATE_DIR, "archive-people.json")
DEFAULT_OUT_DIR = os.path.join(STATE_DIR, "archives")

SCHEMA_VERSION = "seneschal.archive/1"

#: Services the schema knows about (open-ended; unknown values pass through, never fatal).
SERVICES = ("telegram", "discord", "sms", "email")

#: Normalized media kinds — collectors map their native types into this closed vocabulary.
MEDIA_KINDS = ("photo", "video", "voice", "audio", "sticker", "animation", "file", "unknown")

ENV_KEYS = ("ARCHIVE_OUT_DIR",)
DEFAULTS = {"ARCHIVE_OUT_DIR": DEFAULT_OUT_DIR}


def load_env(env_file: str | None = None) -> dict:
    """Config = DEFAULTS, overridden by an --env-file (KEY=VALUE), then the process env.

    Mirrors ``rag_common.load_env`` precedence (file first, real environment wins).
    """
    cfg = dict(DEFAULTS)
    if env_file and os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in list(cfg):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


# --------------------------------------------------------------------- atomic write

def write_json(path: str, obj) -> None:
    """Atomically write ``obj`` as pretty JSON to ``path`` (mkstemp + os.replace)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".archive-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# --------------------------------------------------------------------- timezone (America/Chicago)

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Mon=0) of ``year``-``month`` (e.g. 2nd Sunday of March)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def central_offset_minutes(dt_utc: datetime) -> int:
    """US Central offset in minutes for a naive-UTC instant: −300 (CDT) or −360 (CST).

    Computed from the current US rule (DST 08:00 UTC 2nd-Sun-Mar → 08:00 UTC 1st-Sun-Nov), because
    ``zoneinfo`` is unavailable on the stock Windows Python here. Same rule as
    ``health_common.central_offset`` — kept local so this module has no cross-script import.
    """
    year = dt_utc.year
    dst_start = datetime.combine(_nth_weekday(year, 3, 6, 2), datetime.min.time()) + timedelta(hours=8)
    dst_end = datetime.combine(_nth_weekday(year, 11, 6, 1), datetime.min.time()) + timedelta(hours=8)
    return -300 if dst_start <= dt_utc < dst_end else -360


def resolve_offset(ts_utc: int, tz: str = "auto") -> int:
    """Offset in minutes to apply for display. ``auto`` = America/Chicago; ``UTC`` = 0; ``±NNN`` = fixed.

    The ``UTC``/fixed forms make renderer tests deterministic regardless of host tz.
    """
    if tz in (None, "", "auto", "chicago", "America/Chicago"):
        dt_utc = datetime.fromtimestamp(int(ts_utc), tz=timezone.utc).replace(tzinfo=None)
        return central_offset_minutes(dt_utc)
    if tz.upper() == "UTC":
        return 0
    try:
        return int(tz)
    except (TypeError, ValueError):
        return 0


def local_dt(ts_utc: int, tz: str = "auto") -> datetime:
    """Epoch seconds (UTC) → aware ``datetime`` in the display timezone."""
    off = resolve_offset(ts_utc, tz)
    return datetime.fromtimestamp(int(ts_utc), tz=timezone(timedelta(minutes=off)))


def hhmm(ts_utc: int, tz: str = "auto") -> str:
    """``HH:MM`` in the display timezone."""
    return local_dt(ts_utc, tz).strftime("%H:%M")


def local_date(ts_utc: int, tz: str = "auto") -> date:
    """The local calendar date of an instant, in the display timezone."""
    return local_dt(ts_utc, tz).date()


def iso_utc(ts_utc: int) -> str:
    """Epoch seconds → ISO-8601 UTC string with trailing ``Z`` (display/debug only)."""
    return datetime.fromtimestamp(int(ts_utc), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------- text helpers

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_BLOCK_RE = re.compile(r"</\s*(p|div)\s*>", re.IGNORECASE)


def html_to_text(s: str | None) -> str:
    """Service-message HTML → readable plaintext: <br>/</p> → newlines, strip tags, unescape entities."""
    if not s:
        return ""
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s)
    return s.strip()


# --------------------------------------------------------------------- media helpers

_TG_MEDIA_TYPE = {
    "video_file": "video",
    "voice_message": "voice",
    "audio_file": "audio",
    "video_message": "video",
    "sticker": "sticker",
    "animation": "animation",
}


def classify_kind(hint: str | None) -> str:
    """Map a service-native media type/hint to a normalized ``MEDIA_KINDS`` value."""
    if not hint:
        return "unknown"
    h = hint.lower()
    if h in MEDIA_KINDS:
        return h
    if h in _TG_MEDIA_TYPE:
        return _TG_MEDIA_TYPE[h]
    if h in ("gif",):
        return "animation"
    if h in ("image",):
        return "photo"
    return "unknown"


def sha1_file(path: str, _buf: int = 1 << 20) -> str:
    """SHA-1 hex of a file's bytes (used to dedup media across services)."""
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_buf), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_media_name(service: str, msg_id: str, index: int, ext: str) -> str:
    """Deterministic archive filename for one media item: ``<service>_<msgid>_<n><ext>``."""
    ext = ext or ""
    if ext and not ext.startswith("."):
        ext = "." + ext
    safe = re.sub(r"[^A-Za-z0-9._-]", "", f"{service}_{msg_id}_{index}")
    return safe + ext


def copy_media_dedup(local_path: str, dest_dir: str, service: str, msg_id: str, index: int,
                     seen: dict) -> str:
    """Copy one media file into ``dest_dir`` with a stable name, deduping identical bytes.

    ``seen`` maps sha1 → already-written archive filename (relative). Returns the relative
    ``media/<name>`` path to store as the record's ``archive_path``.
    """
    os.makedirs(dest_dir, exist_ok=True)
    digest = sha1_file(local_path)
    if digest in seen:
        return seen[digest]
    ext = os.path.splitext(local_path)[1]
    name = stable_media_name(service, msg_id, index, ext)
    dest = os.path.join(dest_dir, name)
    # A collector that already downloaded straight into dest_dir leaves the file in place —
    # copying it onto itself would raise SameFileError, so only copy when src != dest.
    if os.path.abspath(local_path) != os.path.abspath(dest):
        shutil.copy2(local_path, dest)
    rel = os.path.join(os.path.basename(dest_dir), name).replace("\\", "/")
    seen[digest] = rel
    return rel


# --------------------------------------------------------------------- normalized record

REQUIRED_FIELDS = ("service", "service_msg_id", "person", "thread_id", "direction", "ts_utc")
DIRECTIONS = ("from_me", "from_them", "system")


def make_record(*, service: str, service_msg_id, person: str, thread_id, direction: str,
                ts_utc: int, sender_name: str = "", text: str = "", text_html=None, text_raw=None,
                media=None, price=None, is_tip=False, is_locked=False, edited_ts_utc=None,
                reply_to=None, service_meta=None) -> dict:
    """Build one normalized message record. ``ts_iso`` is derived; ``text`` is never ``None``."""
    if direction not in DIRECTIONS:
        raise ValueError(f"bad direction {direction!r}")
    ts_utc = int(ts_utc)
    return {
        "service": service,
        "service_msg_id": str(service_msg_id),
        "person": person,
        "thread_id": str(thread_id),
        "direction": direction,
        "sender_name": sender_name or "",
        "ts_utc": ts_utc,
        "ts_iso": iso_utc(ts_utc),
        "text": text or "",
        "text_html": text_html,
        "text_raw": text_raw,
        "media": media or [],
        "price": price,
        "is_tip": bool(is_tip),
        "is_locked": bool(is_locked),
        "edited_ts_utc": int(edited_ts_utc) if edited_ts_utc else None,
        "reply_to": str(reply_to) if reply_to is not None else None,
        "service_meta": service_meta or {},
    }


def make_media(*, kind: str, local_path=None, archive_path=None, remote_url=None, mime=None,
               nbytes=None, meta=None) -> dict:
    """Build one normalized media entry."""
    return {
        "kind": classify_kind(kind),
        "local_path": local_path,
        "archive_path": archive_path,
        "remote_url": remote_url,
        "mime": mime,
        "bytes": int(nbytes) if nbytes is not None else None,
        "meta": meta or {},
    }


def envelope(person: str, service: str, records: list, sources: list | None = None,
             generated_at: str | None = None) -> dict:
    """Wrap normalized records in the standard file envelope."""
    return {
        "schema": SCHEMA_VERSION,
        "person": person,
        "service": service,
        "generated_at": generated_at,
        "sources": sources or [],
        "count": len(records),
        "messages": records,
    }


# --------------------------------------------------------------------- person registry

def load_people(path: str | None = None) -> dict:
    """Load the person registry JSON. Missing file → an empty (but valid) registry, never raises."""
    path = path or DEFAULT_PEOPLE_FILE
    if not os.path.exists(path):
        return {"schema": "seneschal.archive.people/1", "owner": {}, "people": {}}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def owner_id(people: dict, service: str):
    """The owner's user id on ``service`` (used to compute message direction). None if unset."""
    return (people.get("owner", {}).get(service, {}) or {}).get("user_id")


def person_entry(people: dict, person: str) -> dict:
    """The registry entry for a person key, or {} if absent."""
    return (people.get("people", {}) or {}).get(person, {}) or {}


def person_service(people: dict, person: str, service: str) -> dict:
    """A person's per-service identity block (chat_id / user_id / username / export_dir), or {}."""
    return (person_entry(people, person).get("services", {}) or {}).get(service, {}) or {}


def person_display(people: dict, person: str) -> str:
    """A person's display name, falling back to the key itself."""
    return person_entry(people, person).get("display_name") or person
