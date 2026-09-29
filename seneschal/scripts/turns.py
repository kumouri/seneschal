#!/usr/bin/env python3
"""Chat-turn capture, phase 0 — the conversation log, and nothing else. Standard library only.

The deliberate **mirror of `mouth.py`**: same primitive, same fail-open contract, same "phase 0
measures, it does not act". The Mouth records the assistant's half of every conversation; this
records **both halves**, and in particular the owner's.

## What this is

`state/turns.jsonl` is an append-only record of **what was actually said, both sides, verbatim**.
Without it the complete text of something the owner says survives only as long as the warm thread's
short cap (`presence.THREAD_CAP`), after which a truncated prefix lives briefly in the cockpit's ring
buffer and then nothing — and a long message is already truncated inside that window, with no marker
admitting it.

## What this is NOT (phase 0 is one artifact, on purpose)

There is **no index, no query path, no RAG source, no cockpit surface, no orientation read and no
grounding change** here, and nothing about how a turn is handled changes because this module exists.
Later phases build retrieval; this phase only *measures* — including the owner's true message volume,
which no truncating record can recover. Sizing an index on a truncated record would be sizing on a
known undercount. Same discipline as the Mouth's phase 0 and salience's observe-only phase: the
artifact every later step reads, landed first, where it cannot break anything.

## Three invariants, and they are the whole contract

1. **Recording never costs the turn.** `record_turn` returns a bool and *never raises* — a full disk,
   a clobbered file, an exotic value all cost the row, never the message. No caller wraps it in a
   try/except and none should.
2. **`text` is verbatim and uncapped.** This is the entire point. A capture layer that inherited the
   cockpit's 200-character frame budget would reproduce the bug it exists to fix.
3. **Nothing acts on this file in phase 0** beyond the CLI, `stats`, and the narrow presence probe
   :func:`awake_since`. A row can therefore never change what the assistant says, which is what makes
   landing it safe.

## `speaker` vs `origin` — two different questions

`speaker` ∈ `owner | assistant` is **which side of the conversation** a row sits on. `origin` ∈
`human | system` is **whether a person or a machine composed it**. They are not the same question, and
conflating them is a real failure: a meaningful share of inbound on the owner's channel is not the
owner at all — job completion notices, reaction notices and attachment markers the daemon enqueues
into the same channel. A system row on the owner's side is captured (the assistant saw it; it shaped
the reply) and stays honestly labelled, so a later index cannot rank a job log above something the
owner actually said while calling it theirs.

## Retention — indefinite

Retention is indefinite, bounded later by size rather than age. So **Dream does not prune this file**,
and `prune` below defaults to keeping everything. The subcommand exists for future size-threshold work
and for a hand-run sweep; a nightly age-based GC here would delete old conversations on a schedule,
which is the failure this module exists to prevent.

`_SYSTEM_MARKER_RE` is THE ONE PATTERN TO EDIT when a new synthesized inbound shape appears, and the
invariant guarding it is that an unrecognised line is THE OWNER'S: each alternative must match its
producer EXACTLY, because matching more loosely than the thing that writes it is how the pattern
reaches the owner's own words. Widen it only with a test.

USAGE (the CLI mirrors mouth.py's — record for a caller outside Python, tail for reading by hand):
  python turns.py record --surface telegram --speaker owner --text "..."
  python turns.py tail --limit 20
  python turns.py tail --since 2026-08-12 --limit -1     # a whole date window; -1 = no cap
  python turns.py prune --days 400      # nothing calls this; retention is indefinite
  python turns.py stats
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

SCHEMA = "seneschal.turn/1"
TURNS_FILE = "turns.jsonl"

# Retention is INDEFINITE, bounded by size rather than age (future work). `0` means "keep everything" throughout this module — the same sentinel
# `mouth.prune` uses — and it is the default deliberately, so wiring `turns.py prune` into Dream by
# copy-paste from the mouth's sweep would be a no-op rather than a silent data loss.
RETENTION_DAYS = 0

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

# Vocabularies, validated *loosely* — an unknown value is recorded verbatim rather than rejected,
# because a turn that happened must never be lost to this module's opinion about labels. They exist so
# a reader (and phase 1's ingest) knows what to expect.
SURFACES = ("telegram", "discord", "cockpit", "desktop", "call")
SPEAKERS = ("owner", "assistant")
ORIGINS = ("human", "system")

# One small append per row: the one multi-writer-safe primitive available on one machine without a
# lock (mouth-spec.md §3.2). The lock below is for *threads* within one
# process — presence.py records from its drainer coroutine and from `deliver_reply` on a worker
# thread — the same posture as `mouth._append_lock`.
_append_lock = threading.Lock()

# The machine-generated inbound the daemon enqueues into the same channel the owner types into: job
# completion notices, reaction notices, attachment markers. They all open with a synthesized
# `[...]` marker, which is the only signal that survives into the durable queue — the queue carries
# `(channel, text, attempts)` and nothing else, so widening it to flag these would be a schema change
# on the hot path for a field phase 0 does not read.
#
# It is an ALLOW-LIST of the known synthesized openings, not a bare `^\[` sniff, and the asymmetry is
# the reason: a system row mislabelled `human` only adds noise, but a HUMAN row mislabelled `system`
# drops the owner's own words out of the default index — and the owner's raw words must be the last
# thing evicted, never the first. So `[sic] that's what they actually said` and `[note to self] …` are
# **the owner's**, and only these openings are machine. Widen this pattern only alongside a test.
#
# There is deliberately no bare `job ` alternative: `[job finished:` (jobs.py) is the only synthesized
# `[job` line in the tree, and a looser `job ` would match **`[job posting] this one looks decent`** —
# a message the owner plausibly sends — filing it as machine output. A pattern with no producer can
# only cost the owner's words.
# Each alternative is written to match its producer EXACTLY — `the owner reacted ` keeps its trailing
# space because `presence.py`'s notice is always `[the owner reacted {emoji} …]`, never a bare
# `[the owner reacted]`. Matching more loosely than the thing that writes it is how this pattern
# reaches the owner's words.
_SYSTEM_MARKER_RE = re.compile(r"^\s*\[(job finished|the owner reacted |attachment:|force-fable)", re.I)

# `[attachment: document "notes.pdf" saved to <state>/inbox/x.pdf] the caption here`
# and its two failure variants, all built by `presence._attachment_or_text`.
_ATTACHMENT_RE = re.compile(
    r"\[attachment:\s*(?P<kind>[a-z]+)"        # document | photo | voice | audio | video | file
    r"(?:\s+\"(?P<name>[^\"]*)\")?"            # the scrubbed sender filename, when there is one
    r"(?:\s*\([^)]*\))?"                       # the human size, on the over-the-limit variant
    r"(?:\s*saved to (?P<path>[^\]]+))?"       # absent on both failure variants
    r"[^\]]*\]", re.IGNORECASE)


def default_state_dir() -> str:
    return DEFAULT_STATE_DIR


def turns_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or DEFAULT_STATE_DIR, TURNS_FILE)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _row_id(now: datetime | None = None) -> str:
    """`20260804-145526-8f2c` — a sortable stamp plus four hex so two rows in the same second never
    collide. Not a correlator; `turn_id` is what joins the owner's message to its reply."""
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{when:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"


def _parse_stamp(value) -> datetime | None:
    """Tolerant read of an `at` field. Anything unparseable reads as None, and every caller treats that
    as "don't touch this row" — a garbled stamp must never make `prune` delete a conversation."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def classify_origin(text) -> str:
    """`human` for something a person (or the assistant) composed, `system` for machine-generated
    inbound.

    Pure and unit-testable. See `_SYSTEM_MARKER_RE` for why this is a marker match rather than a flag
    threaded through the durable queue."""
    if not isinstance(text, str):
        return "human"
    return "system" if _SYSTEM_MARKER_RE.match(text) else "human"


def parse_attachments(text) -> list:
    """Recover `{kind, name, path}` from a synthesized `[attachment: …]` marker — never the bytes.

    `state/inbox/` already holds the file, and Dream sweeps that nightly, so the row must survive its
    own path going stale: a dangling path that says *"the owner sent a photo at 14:03, captioned
    'look at this'"* is worth incomparably more than no row. The caption is not duplicated
    into a field — it is already in the row's `text`, which is the verbatim line, marker and all.

    Fail-open: anything unrecognised yields `[]`."""
    if not isinstance(text, str) or "[attachment:" not in text:
        return []
    out = []
    try:
        for m in _ATTACHMENT_RE.finditer(text):
            att = {"kind": (m.group("kind") or "file").strip()}
            if m.group("name"):
                att["name"] = m.group("name")
            path = (m.group("path") or "").strip()
            att["path"] = path or None
            out.append(att)
    except Exception:  # noqa: BLE001 — a descriptor is enrichment; never worth losing the row
        return []
    return out


def record_turn(state_dir: str | None = None, *, surface: str, speaker: str, text: str,
                origin: str | None = None, turn_id: str | None = None,
                session_id: str | None = None, reply_to: str | None = None,
                attachments=None, redacted: bool = False, now: datetime | None = None) -> bool:
    """Append one `seneschal.turn/1` row — one side of one turn, verbatim. Returns True iff it hit disk.

    **This never raises** (invariant 1). Every call site is on the live conversation path, and a failed
    append must cost the row and nothing else. Callers do not need a try/except around it, and should
    not add one.

    `redacted=True` is the `!private` / retro-redact tombstone: the row is written **without a
    `text` key at all**, and without attachment descriptors, because a filename and a caption are
    content too. The *fact* of the turn survives — a record that lies by omission is worse than one
    that admits a gap.

    `origin` defaults to `classify_origin(text)`, so a job-completion notice enqueued into the owner's
    channel
    is labelled honestly without every call site having to remember to say so.
    """
    try:
        state_dir = state_dir or DEFAULT_STATE_DIR
        text_str = text if isinstance(text, str) else str(text)
        row = {
            "schema": SCHEMA,
            "id": _row_id(now),
            "at": _stamp(now),
            "surface": str(surface or "unknown"),
            "speaker": str(speaker or "unknown"),
            "origin": str(origin or classify_origin(text_str)),
            "turn_id": turn_id or None,
            "session_id": session_id or None,
            "reply_to": reply_to or None,
            "redacted": bool(redacted),
        }
        if redacted:
            # No `text` key, and no attachment descriptors: `!private` means the content never lands.
            row["attachments"] = []
        else:
            row["text"] = text_str
            row["attachments"] = list(attachments) if attachments is not None \
                else parse_attachments(text_str)
        line = json.dumps(row, ensure_ascii=False) + "\n"
        os.makedirs(state_dir, exist_ok=True)
        with _append_lock:
            with open(turns_path(state_dir), "a", encoding="utf-8") as fh:
                fh.write(line)
        return True
    except Exception:  # noqa: BLE001 — see the docstring: the row is never worth the turn
        return False


def read_turns(state_dir: str | None = None, limit: int | None = None,
               since: datetime | None = None) -> list:
    """Rows oldest-first, malformed lines skipped. `limit` keeps the **newest** N. Fail-open: an absent
    or unreadable file reads empty.

    Nothing in phase 0 calls this except the CLI, `stats` and the tests — the reading surfaces are
    later phases. It exists now because a write-only file nobody can inspect by hand is not measurable, and
    measurement is the entire justification for phase 0."""
    rows: list = []
    try:
        with open(turns_path(state_dir), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if since is not None:
                    at = _parse_stamp(row.get("at"))
                    if at is None or at < since:
                        continue
                rows.append(row)
    except OSError:
        return []
    if limit is not None and limit >= 0:
        rows = rows[-limit:] if limit else []
    return rows


# **The owner's presence markers — the two SYSTEM-origin openings that are still the owner doing
# something.** `_SYSTEM_MARKER_RE` answers "did the owner COMPOSE this?", and for a 👍 or a dropped
# photo the honest answer is no. `awake_since` asks a different question — "was the owner holding
# their phone?" — and there the answer is yes: a reaction and an attachment are both the owner's
# thumb on the screen, just as a picker tap (origin `human`) is. `[job finished:` and `[force-fable`
# are the daemon talking to itself on the owner's channel and are deliberately NOT here. Same
# exact-producer rule as the pattern above: each alternative matches what `presence.py` writes.
_PRESENCE_MARKER_RE = re.compile(r"^\s*\[(the owner reacted |attachment:)", re.I)

#: How much of the file's END `awake_since` reads. The file is append-only and chronological, so the
#: newest rows are the last ones; 1 MiB is several hundred turns — far more than any window worth
#: asking about — against a file that grows without bound (retention is indefinite), which a full
#: read on every overnight pass would pay in full for one timestamp.
AWAKE_TAIL_BYTES = 1 << 20


def is_owner_presence(row) -> bool:
    """Is this row evidence that the owner personally was at a keyboard or a phone when it was written?

    `speaker == "owner"` and either `origin == "human"` (a typed message, a caption, a picker tap —
    all `human` by `classify_origin`) or a reaction / attachment marker. A **redacted** system row has
    no text to tell a reaction from a job notice, so it does not count — the one ambiguity here
    resolves toward NOT awake, which is the quiet side. A redacted `human` row still counts: the
    tombstone keeps the fact that the owner said something, and the fact is all this asks."""
    if not isinstance(row, dict) or row.get("speaker") != "owner":
        return False
    if row.get("origin") == "human":
        return True
    text = row.get("text")
    return isinstance(text, str) and bool(_PRESENCE_MARKER_RE.match(text))


def awake_since(state_dir: str | None = None, now: datetime | None = None,
                window: timedelta = timedelta(minutes=30),
                tail_bytes: int = AWAKE_TAIL_BYTES) -> datetime | None:
    """The newest instant within `window` before `now` at which the owner personally was
    demonstrably present on a channel (:func:`is_owner_presence`), or `None`.

    **`None` is the answer to every failure** — a missing file, an unreadable one, a tail with no
    parseable row, a garbled stamp. A quiet-hours caller reads `None` as *keep the quiet window*, so
    every doubt here lands on the side that does not page the owner at 3 AM.
    A row stamped more than a minute AFTER `now` is ignored too: a clock that disagrees with itself
    is not evidence. Never raises."""
    try:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        floor, ceiling = now - window, now + timedelta(minutes=1)
        with open(turns_path(state_dir), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - max(0, int(tail_bytes)))
            fh.seek(start)
            chunk = fh.read()
        lines = chunk.split(b"\n")
        if start > 0:
            lines = lines[1:]  # the first line is a fragment of a row that started before the tail
        best = None
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not is_owner_presence(row):
                continue
            at = _parse_stamp(row.get("at"))
            if at is None or not (floor <= at <= ceiling):
                continue
            if best is None or at > best:
                best = at
        return best
    except Exception:  # noqa: BLE001 — cannot read ⇒ no evidence ⇒ the quiet window stands
        return None


def prune(state_dir: str | None = None, days: int = RETENTION_DAYS,
          now: datetime | None = None) -> int:
    """Drop rows older than `days` and rewrite the log. Returns how many were dropped.

    **Nothing calls this**, deliberately: retention is indefinite, so `days` defaults to
    `RETENTION_DAYS = 0`, which keeps everything. It is here so future size-threshold work has a
    tested mechanism to reach for, and so a hand-run sweep is possible without an ad-hoc script.

    A row whose `at` is missing or unparseable is **kept**, and a `redacted: true` tombstone is
    **never** dropped whatever its age — a tombstone is bytes-cheap and is the thing that stops
    the record lying by omission. Built-then-replaced, so a crash mid-prune leaves the old file
    intact; an eviction never rewrites destructively in place."""
    if days is None or days <= 0:
        return 0
    path = turns_path(state_dir)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    keep, dropped = [], 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            keep.append(line if line.endswith("\n") else line + "\n")  # unreadable ≠ expired
            continue
        at = _parse_stamp(row.get("at")) if isinstance(row, dict) else None
        tombstone = isinstance(row, dict) and bool(row.get("redacted"))
        if at is not None and at < cutoff and not tombstone:
            dropped += 1
            continue
        keep.append(line if line.endswith("\n") else line + "\n")
    if not dropped:
        return 0
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(keep)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0
    return dropped


# --------------------------------------------------------------------------- CLI

def stats(state_dir: str | None = None) -> dict:
    """Real volume — the measurement phase 0 exists to produce.

    The owner's true message volume **cannot be recovered from any truncating record**, so index
    sizing and a size threshold both need the figures this function exposes: real text-byte growth
    (what an index would actually embed) — **`text_bytes_per_day` / `text_bytes_by_speaker`** — as
    opposed to `bytes_per_day`, which sizes the raw JSONL envelope.

    `days_covered` and every per-day rate read **None until two distinct days exist**, deliberately: a
    single day is not a rate, and extrapolating from one would be sizing on fiction again — the exact
    error the phase was built to end.

    Fail-open like every reader here: an absent or unreadable log reads as zero rows, never an error.
    """
    rows = read_turns(state_dir)
    try:
        size_bytes = os.path.getsize(turns_path(state_dir))
    except OSError:
        size_bytes = 0

    by_surface: dict = {}
    by_speaker: dict = {}
    by_origin: dict = {}
    text_bytes_by_speaker: dict = {}
    days: set = set()
    first_at = last_at = None
    redacted = 0
    text_bytes = 0
    for row in rows:
        for field, bucket in (("surface", by_surface), ("speaker", by_speaker), ("origin", by_origin)):
            key = row.get(field) or "unknown"
            bucket[key] = bucket.get(key, 0) + 1
        if row.get("redacted"):
            redacted += 1
        text = row.get("text")
        if isinstance(text, str):
            n = len(text.encode("utf-8"))
            text_bytes += n
            speaker = row.get("speaker") or "unknown"
            text_bytes_by_speaker[speaker] = text_bytes_by_speaker.get(speaker, 0) + n
        at = _parse_stamp(row.get("at"))
        if at is not None:
            days.add(at.date())
            if first_at is None or at < first_at:
                first_at = at
            if last_at is None or at > last_at:
                last_at = at

    covered = len(days) if len(days) > 1 else None
    return {
        "rows": len(rows),
        "bytes": size_bytes,
        "text_bytes": text_bytes,
        "text_bytes_by_speaker": text_bytes_by_speaker,
        "redacted": redacted,
        "by_surface": by_surface,
        "by_speaker": by_speaker,
        "by_origin": by_origin,
        "first_at": _stamp(first_at) if first_at else None,
        "last_at": _stamp(last_at) if last_at else None,
        "days_covered": covered,
        "bytes_per_day": round(size_bytes / covered, 1) if covered else None,
        "rows_per_day": round(len(rows) / covered, 1) if covered else None,
        "text_bytes_per_day": round(text_bytes / covered, 1) if covered else None,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Chat-turn capture (phase 0): the conversation log.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="append one turn")
    rec.add_argument("--surface", required=True)
    rec.add_argument("--speaker", required=True, choices=list(SPEAKERS))
    rec.add_argument("--text", required=True)
    rec.add_argument("--origin", default=None, choices=list(ORIGINS))
    rec.add_argument("--turn-id", default=None)
    rec.add_argument("--session-id", default=None)
    rec.add_argument("--redacted", action="store_true",
                     help="write the tombstone only — no text, no attachment descriptors")

    tail = sub.add_parser("tail", help="print the newest rows")
    tail.add_argument("--limit", type=int, default=20)
    tail.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                      help="only rows at or after this instant (a bare date means UTC midnight; a "
                           "full ISO stamp also works). Composes with --limit, which is applied "
                           "AFTER the filter and still keeps the NEWEST N — pass a negative --limit "
                           "for the whole window, which is what a date-range read wants.")

    pr = sub.add_parser("prune", help="drop rows older than --days (NOTHING calls this; retention is indefinite)")
    pr.add_argument("--days", type=int, default=RETENTION_DAYS)

    sub.add_parser("stats", help="real volume — what phase 0 exists to measure")

    args = p.parse_args(argv)

    if args.cmd == "record":
        ok = record_turn(args.state_dir, surface=args.surface, speaker=args.speaker, text=args.text,
                         origin=args.origin, turn_id=args.turn_id, session_id=args.session_id,
                         redacted=args.redacted)
        print(json.dumps({"ok": ok}))
        return 0 if ok else 1
    if args.cmd == "tail":
        since = None
        if args.since:
            since = _parse_stamp(args.since)
            if since is None:
                # Loud, not tolerant, and deliberately unlike every read path in this module: a
                # window read that silently widens to "everything" is how a digest built for one
                # session reaches back over months. `_parse_stamp`'s tolerance protects rows that
                # already exist; an argument typed at the command line has no such claim.
                print(json.dumps({"ok": False, "error": f"unparseable --since: {args.since!r}"}))
                return 2
        for row in read_turns(args.state_dir, limit=args.limit, since=since):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.cmd == "stats":
        print(json.dumps(stats(args.state_dir), ensure_ascii=False, indent=2))
        return 0
    dropped = prune(args.state_dir, days=args.days)
    print(json.dumps({"ok": True, "dropped": dropped}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
