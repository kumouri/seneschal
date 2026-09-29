#!/usr/bin/env python3
"""Append one proposed company-intel finding to the pending queue — the write path Proteus calls
INSTEAD of the ``Write`` tool.

    python record_intel.py --json '{"company": "Acme", "adjust": -5, "tags": ["layoffs"],
                                     "note": "...", "source": "work-up acme-swe"}'

``company-intel.json`` is Proteus's durable company memory — a curated input the owner audits
(gitignored owner data). Proteus never writes it directly: a headless whole-file rewrite is unreliable
and would bypass the ledger's invariants (see ``seneschal/references/archons.md``). So Proteus proposes
here instead; ``promote_intel.py`` is the separate, reviewed path that merges a proposal into the
ledger — locally by default, or by PR (``--via-pr``) for an owner who keeps it in a private fork.

**Appends one JSON line** to ``proteus_paths.INTEL_PENDING_FILE``
(``state/company-intel-pending.jsonl``, gitignored). Append-only, deliberately — never
read-modify-write. That is the whole point: two concurrent recordings can't race each other into
clobbering the file, and a half-written line can never discard everything recorded before it.
``score_jobs.py``'s overlay (``--intel-overlay``) already tolerates a truncated *trailing* line, so a
crash mid-write costs at most the one entry being appended, never the queue.

**Mirrors ``register_workup.py``'s INTERFACE, not its persistence.** Same shape: a single ``--json``
flag carrying the whole entry, exit 2 on bad input, one human-readable confirmation line on stdout,
paths from ``proteus_paths``. But a whole-file rewrite over a manifest whose loader returns an EMPTY
document on a corrupt read can silently discard everything; an append (``open(path, "a")``)
sidesteps that whole class of failure — there is no "current contents" to lose, so there is nothing
to read wrong.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from proteus_paths import INTEL_PENDING_FILE, owner_today

# Mirrors score_jobs.INTEL_ADJUST_MAX (currently 20.0) — restated, not imported. record_intel.py and
# score_jobs.py are separate tools (one runs inside Proteus's headless session, the other in the
# scoring pipeline) and must not gain a runtime coupling just to share one constant; if the scorer's
# ceiling ever moves, THIS clamp staying put only means a stored proposal is slightly more conservative
# than it needs to be, never that an unclamped value reaches the ledger.
INTEL_ADJUST_MAX = 20.0


def validate(entry: object) -> str | None:
    """Return a human-readable error, or ``None`` if ``entry`` is a well-formed intel proposal."""
    if not isinstance(entry, dict):
        return "entry must be a JSON object"
    company = entry.get("company")
    if not isinstance(company, str) or not company.strip():
        return "'company' is required and must be a non-empty string"
    if "adjust" not in entry or entry.get("adjust") is None:
        return "'adjust' is required"
    try:
        float(entry["adjust"])
    except (TypeError, ValueError):
        return "'adjust' must be numeric"
    tags = entry.get("tags")
    if tags is not None and (not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)):
        return "'tags' must be a list of strings"
    for field in ("note", "source"):
        value = entry.get(field)
        if value is not None and not isinstance(value, str):
            return f"'{field}' must be a string"
    return None


def record(entry: dict, now: datetime | None = None) -> dict:
    """Stamp ``entry`` and append it as one JSON line to the pending queue. Returns the stamped record.

    Caller must have already validated ``entry`` (``main`` does). Clamps ``adjust`` to ±INTEL_ADJUST_MAX
    on the way in — the scorer clamps again defensively, but a proposal that already respects the
    ceiling is one less thing for a promotion or an audit to have to notice.

    ``updated`` (the owner-local date) and ``recorded_at`` (UTC) are always derived HERE, from
    ``now`` — never taken from the caller's JSON — so a stray key can't spoof the stamp. ``now``
    defaults to the wall clock and is injectable for tests.
    """
    instant = now or datetime.now(timezone.utc)
    adjust = max(-INTEL_ADJUST_MAX, min(INTEL_ADJUST_MAX, float(entry["adjust"])))
    stamped = {
        "company": entry["company"].strip(),
        "adjust": adjust,
        "tags": list(entry.get("tags") or []),
        "note": entry.get("note", ""),
        "source": entry.get("source", ""),
        "updated": owner_today(instant),
        "recorded_at": instant.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    INTEL_PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Append-only — see module docstring. No read, no rewrite, nothing to clobber.
    with open(INTEL_PENDING_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(stamped, ensure_ascii=False) + "\n")
    return stamped


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Append a proposed company-intel finding to the pending queue.")
    ap.add_argument("--json", required=True, help="the intel entry as a JSON object")
    args = ap.parse_args(argv)

    try:
        entry = json.loads(args.json)
    except ValueError as exc:
        print(f"record_intel: --json is not valid JSON: {exc}", file=sys.stderr)
        return 2

    error = validate(entry)
    if error:
        print(f"record_intel: {error}", file=sys.stderr)
        return 2

    stamped = record(entry, now)
    tags = ", ".join(stamped["tags"]) or "untagged"
    print(f"recorded '{stamped['company']}' ({stamped['adjust']:+g} [{tags}]) to the pending queue "
          f"({INTEL_PENDING_FILE.name}) — promote_intel.py will pick it up for review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
