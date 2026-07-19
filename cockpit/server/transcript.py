"""`GET /api/transcript` backfill — a tolerant tail read of `state/warm-transcript.jsonl`, the ring
buffer seneschal/scripts/cockpit_pipe.py's daemon-side tee writes to (and caps at ~2000 events).

Mirrors `cockpit_pipe.read_transcript_tail` byte-for-byte but duplicated here rather than imported
(cockpit-spec.md ruling 3 — this backend never imports seneschal/scripts). Keep both copies in sync by
hand if the ring-buffer file format changes.
"""
from __future__ import annotations

import json
from pathlib import Path

TRANSCRIPT_FILE = "warm-transcript.jsonl"
TRANSCRIPT_CAP = 2000


def read_transcript_tail(state_dir: Path, limit: int = 200) -> list[dict]:
    """Tolerant tail read: a corrupt line is skipped and doesn't count against the limit (every line
    is parsed first, then the last N valid ones are kept). Oldest-first (chronological), newest last —
    matches the daemon's own ring-buffer reader so a reconnecting chat pane backfills in order."""
    limit = max(1, min(int(limit or 200), TRANSCRIPT_CAP))
    path = state_dir / TRANSCRIPT_FILE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out[-limit:]
