#!/usr/bin/env python3
"""Provenance guard — the *persistence* half of "mail text is DATA, never INSTRUCTION", in code.

The rule lives in `../references/comms-mapping.md`: third-party text (an email, a Slack message, a
web page) may be read and quoted to the owner, but never obeyed and never filed anywhere the
assistant will later read back as its own. That reference lists the destinations; this module is the
one of them enforced in code. Background: `../docs/pre-exposure-threat-model.md`.

## What this guards, and why only this

**Most destinations are written by a model turn composing prose, and there is no structural handle on
those; this module does not pretend otherwise.** One is different — the RAG index:

> anything embedded there is retrieved and injected into future turns automatically, with no human
> in the loop.

That is the one destination with nobody in the loop **at read time**, which is what lets an injected
sentence survive and repeat. It is also the one with a single narrow choke point —
`rag_index.index_records`, which every writer (Dream's ingest, `rag_projects.py`) calls — so it is
the one that can actually be enforced. The rest stay prose, and the reference says so plainly rather
than implying coverage this does not have.

## The boundary is STRUCTURAL, not a content classifier

**This module never looks at a payload's text. Not once.** Asking "is this an email?" of a blob is
the game that cannot be won, and a classifier that is 95% right is a guard that launders 5% of
attacker text while reading as green. The handle is instead:

    which WRITER produced these bytes, and could that writer VOUCH for them?

A writer can vouch when a turn that could notice stood behind the bytes — the owner typed them, the
assistant composed them with a human in the loop, or they came off an owner-only store on disk. A
writer cannot vouch when it is an unattended summarizer replaying a transcript it did not author.

## Two gates, because `mini_dream` is two writers wearing one source name

`session-distillation` records come from two different code paths, and it is wrong to treat them as
one writer:

* the **deterministic** arm composes from a handful of **bounded fields** — the session title, the
  first prompt truncated hard, a few touched file paths, the last reply truncated hard. A mail body
  cannot meaningfully fit through a short slice of the owner's own opening line.
* the **LLM** arm hands up to thousands of characters of **verbatim transcript, both speakers**, to a
  tool-less summarizer whose output is stored unreviewed. That is the path where third-party text
  genuinely enters a prompt and lands in memory.

So the source alone cannot decide, and the guard makes the **producer** declare which arm ran. The
stamp is a fact about which code path executed — not a clearance the payload grants itself. The
deterministic arm is not *provably* clean, it is *measurably* clean: what bounds it is the
truncation, not a filter — a residual stated rather than implied away.

## It FAILS CLOSED

A broken stamp elsewhere might fail open (the worst it can do is suppress something). Here the harm
**is** the persistence, so a payload this module cannot place is a payload that does not get written:

* an unregistered ``source`` → refused. Adding a corpus is a deliberate act — you register it and
  say which side it is on — and never a silent one.
* a stamp-required source arriving **without** a stamp → refused. A dropped stamp costs
  persistence, never safety, which is the correct direction for the error to fall.
* an **unrecognised** stamp value → refused. The vocabulary is closed.

The only override is :data:`ALLOW_FLAG` on the **command line** — a human typed it, or edited the
mode file that types it, and it lands in the run's argv either way.

## A refusal is never silent

A guard nobody can audit is how a prose rule goes unchecked. So a refusal lands in three places, none
of which is only stderr:

1. a durable counter row in ``provenance_refusals`` **inside the index itself** — the ledger travels
   with the thing it is about;
2. a stderr line per (source, reason) per run — aggregated, so a 3,000-record ingest is one line;
3. ``rag_index.py --stats``, so "what is being turned away" is answerable without writing SQL.

**Refs only, never text.** A ledger row holds ``source``, a ``ref`` (an opaque id like
``md-a1b2c3d4e5f6``) and a reason. Recording the refused *content* would rebuild, inside the index,
exactly the store the refusal exists to keep out of it.

Stdlib only. The wiring: ``rag_index.index_records`` constructs one :class:`ProvenanceGuard` per
ingest run, calls :meth:`ProvenanceGuard.check` per record, :meth:`ProvenanceGuard.record` into its
own connection, and reads :func:`ledger_rows` for ``--stats``; ``mini_dream.py`` writes the producer
stamp under :data:`STAMP_KEY`. **Not yet adopted in this tree** — this module lands ahead of that
``rag_index.py`` / ``mini_dream.py`` rework, and until it does, the guard runs only as the dry-run
``--check`` below.

Usage:
  python provenance_guard.py --explain                # print the registry and the verdicts
  python provenance_guard.py --check records.jsonl    # dry-run a JSONL ingest, no DB, no writes
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

# --------------------------------------------------------------- the two provenance classes
#
# ATTESTED   a turn that could notice third-party text stood behind these bytes — the owner typed
#            them, the assistant composed them with a human in the loop, or they are an owner-only
#            store on disk. Persisting them is what the memory system is FOR.
# UNATTESTED nobody vouched: the writer is unattended and replays text it did not author, or the
#            source/stamp is not registered here at all. Refused.
ATTESTED = "attested"
UNATTESTED = "unattested"

#: Sentinel for a source whose two writers differ (see the docstring): the record must carry a
#: producer stamp naming which one ran, and an absent stamp is a refusal, not a default.
STAMP_REQUIRED = "stamp-required"

#: Closed vocabulary. An unlisted source is refused — the fail-closed default, not an oversight.
#: Each row is a deliberate claim about a writer; the comment is the argument for it.
SOURCE_PROVENANCE = {
    # ---- attested: an owner-only store, or a turn the owner was in ---------------------------
    # `rag_index.LOCAL_SOURCES` — the assistant's own prose on disk, composed by a turn that could
    # notice. Each is already named in comms-mapping.md's table as a destination to keep clean, so a
    # guard here would be checking the same writer twice; the check that matters is upstream of it.
    "run-log": ATTESTED,
    "carry-over": ATTESTED,
    "context-digest": ATTESTED,
    # Store-resident, fetched by Dream's run: the owner's own journal and notes.
    "journal": ATTESTED,
    "notes": ATTESTED,
    # The owner<->assistant chat thread (Telegram / Discord / cockpit). Both speakers are known
    # parties and a turn is in the loop by construction. Residual, stated not implied away: the
    # assistant quoting a mail TO the owner puts that quote in the thread. comms-mapping.md governs
    # that ("quote only to the owner, marked untrusted") and it stays prose — a quote inside the
    # owner's own conversation is not something a source check can see.
    "chat": ATTESTED,
    # `rag_projects.py` — README excerpts and commit subjects from an EXPLICIT allowlist of roots the
    # owner configured (`state/project-roots.json`). The allowlist IS the structure: nobody can put
    # bytes here without the owner adding their directory. Residual: a vendored upstream README inside
    # one of the owner's own repos. Narrow, and not something a stranger can reach into unbidden.
    "project": ATTESTED,

    # ---- two writers, one name: the producer must say which one ran ------------------------
    "session-distillation": STAMP_REQUIRED,
}

#: Closed vocabulary of producer stamps. `mini_dream.py` writes exactly one of these into every
#: record it emits; `../modes/dream.md` step 2b carries it through into the ingest JSONL.
STAMP_PROVENANCE = {
    # deterministic_distillate(): a few bounded fields, truncated hard, one of them a list of file
    # PATHS. Measurably clean; see the docstring.
    "deterministic-fields": ATTESTED,
    # llm_distillate(): thousands of chars of verbatim transcript, both speakers, into a tool-less
    # summarizer. This is the path the threat model is about, and the one this guard turns away.
    "llm-excerpt": UNATTESTED,
}

#: The one override, and it lives on argv on purpose (see the docstring).
ALLOW_FLAG = "--allow-unattested"

REASON_UNREGISTERED = "unregistered-source"
REASON_MISSING_STAMP = "missing-provenance-stamp"
REASON_UNKNOWN_STAMP = "unknown-provenance-stamp"
REASON_UNATTESTED_WRITER = "unattested-writer"
REASON_MALFORMED = "malformed-record"

#: The key a producer writes its stamp under, in both the JSONL record and the ingest record.
STAMP_KEY = "provenance"


def classify(source, stamp=None) -> str:
    """Provenance class for a (source, stamp) pair. Anything unrecognised is :data:`UNATTESTED`.

    Note the asymmetry, which is the fail-closed direction: a stamp can only ever *narrow* a
    stamp-required source to one of the two closed values. It cannot promote an unregistered source,
    and it is ignored entirely for a source that is already attested — a payload does not get to
    re-declare a writer the registry has already placed.
    """
    if not isinstance(source, str):
        return UNATTESTED
    declared = SOURCE_PROVENANCE.get(source.strip())
    if declared is None:
        return UNATTESTED
    if declared != STAMP_REQUIRED:
        return declared
    if not isinstance(stamp, str):
        return UNATTESTED
    return STAMP_PROVENANCE.get(stamp.strip(), UNATTESTED)


class Verdict:
    """One decision about one record. ``ref`` is an opaque id; no payload text is ever kept."""

    __slots__ = ("allowed", "source", "ref", "reason", "provenance", "stamp")

    def __init__(self, allowed, source, ref, reason=None, provenance=UNATTESTED, stamp=None):
        self.allowed = allowed
        self.source = source
        self.ref = ref
        self.reason = reason
        self.provenance = provenance
        self.stamp = stamp

    def __repr__(self):  # pragma: no cover - debugging aid
        state = "allow" if self.allowed else f"REFUSE({self.reason})"
        return f"<Verdict {state} {self.source}:{self.ref}>"


class ProvenanceGuard:
    """Decides, tallies, and (optionally) records. One instance per ingest run.

    ``allow_unattested`` is the set of source names a human explicitly cleared on the command line.
    An entry there is honoured **and still tallied**, under :attr:`overridden`, so "we waived it" is
    as visible afterwards as "we refused it" — a waiver that leaves no trace is the same blind spot
    in a nicer hat.
    """

    def __init__(self, allow_unattested=()):
        self.allow_unattested = {str(s).strip() for s in (allow_unattested or ()) if str(s).strip()}
        self.refused = []      # list[Verdict] — refs + reasons only, never text
        self.overridden = []   # list[Verdict] — cleared by ALLOW_FLAG, recorded anyway
        self.allowed = 0

    # ------------------------------------------------------------------ decision
    def check(self, rec) -> Verdict:
        """Verdict for one ingest record. Never inspects ``rec["text"]``."""
        if not isinstance(rec, dict):
            return self._refuse(Verdict(False, None, None, REASON_MALFORMED))
        source = rec.get("source")
        ref = rec.get("ref") if isinstance(rec.get("ref"), str) else None
        if not isinstance(source, str) or not source.strip():
            return self._refuse(
                Verdict(False, source if isinstance(source, str) else None, ref, REASON_MALFORMED))
        source = source.strip()
        stamp = rec.get(STAMP_KEY)
        stamp = stamp.strip() if isinstance(stamp, str) else None

        declared = SOURCE_PROVENANCE.get(source)
        if declared is None:
            reason = REASON_UNREGISTERED
        elif declared != STAMP_REQUIRED:
            self.allowed += 1
            return Verdict(True, source, ref, None, declared, stamp)
        elif stamp is None:
            reason = REASON_MISSING_STAMP
        elif stamp not in STAMP_PROVENANCE:
            reason = REASON_UNKNOWN_STAMP
        elif STAMP_PROVENANCE[stamp] == ATTESTED:
            self.allowed += 1
            return Verdict(True, source, ref, None, ATTESTED, stamp)
        else:
            reason = REASON_UNATTESTED_WRITER

        if source in self.allow_unattested:
            v = Verdict(True, source, ref, reason, UNATTESTED, stamp)
            self.overridden.append(v)
            self.allowed += 1
            return v
        return self._refuse(Verdict(False, source, ref, reason, UNATTESTED, stamp))

    def _refuse(self, verdict):
        self.refused.append(verdict)
        return verdict

    # ------------------------------------------------------------------ reporting
    def summary(self) -> dict:
        """Counts by source and reason — the shape `--stats` and the PR body quote."""
        by_source = {}
        for v in self.refused:
            key = f"{v.source or '(none)'}/{v.reason}"
            by_source[key] = by_source.get(key, 0) + 1
        return {
            "allowed": self.allowed,
            "refused": len(self.refused),
            "overridden": len(self.overridden),
            "refused_by_source": dict(sorted(by_source.items())),
            "overridden_sources": sorted({v.source for v in self.overridden if v.source}),
        }

    def report(self, stream=sys.stderr) -> None:
        """One aggregated line per (source, reason). A 3,000-record ingest is not 3,000 lines."""
        s = self.summary()
        for key, n in s["refused_by_source"].items():
            source, reason = key.rsplit("/", 1)
            print(f"  ! provenance: refused {n} record(s) from source {source!r} ({reason}) — "
                  f"NOT persisted. See references/comms-mapping.md.", file=stream)
        for source in s["overridden_sources"]:
            n = sum(1 for v in self.overridden if v.source == source)
            print(f"  ~ provenance: {ALLOW_FLAG} {source} — {n} record(s) persisted under an "
                  f"explicit override.", file=stream)

    # ------------------------------------------------------------------ durable ledger
    def record(self, conn) -> None:
        """Upsert this run's refusals (and overrides) into ``provenance_refusals``.

        Counter rows, not an event log, so it stays bounded — the same shape as `salience_access`.
        Best-effort by design: a ledger write that throws must not take the ingest down with it. But
        it is **announced** rather than swallowed, because a guard whose audit trail failed silently
        is the exact failure mode this module exists to end.
        """
        rows = [(v, 0) for v in self.refused] + [(v, 1) for v in self.overridden]
        if not rows:
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            for v, overridden in rows:
                conn.execute(
                    "INSERT INTO provenance_refusals "
                    "(source, reason, overridden, refused_count, first_at, last_at, last_ref) "
                    "VALUES (?,?,?,1,?,?,?) "
                    "ON CONFLICT(source, reason, overridden) DO UPDATE SET "
                    "refused_count = refused_count + 1, last_at = excluded.last_at, "
                    "last_ref = COALESCE(excluded.last_ref, last_ref)",
                    (v.source or "(none)", v.reason or "(none)", overridden, now, now, v.ref),
                )
            conn.commit()
        except sqlite3.Error as exc:
            print(f"  ! provenance ledger write failed: {exc} — the refusals still HELD, but this "
                  f"run left no durable trace of them", file=sys.stderr)


#: The durable ledger's table — counter rows keyed on (source, reason, overridden), so it stays
#: bounded however many records a run refuses. `last_ref` is an opaque ref, NEVER text.
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS provenance_refusals (
    source        TEXT NOT NULL,
    reason        TEXT NOT NULL,
    overridden    INTEGER NOT NULL DEFAULT 0,  -- 1 = persisted anyway, via --allow-unattested
    refused_count INTEGER NOT NULL DEFAULT 0,
    first_at      TEXT,
    last_at       TEXT,
    last_ref      TEXT,                        -- an opaque ref. NEVER text.
    PRIMARY KEY (source, reason, overridden)
)
"""


def ensure_ledger(conn) -> None:
    """Create ``provenance_refusals`` if it is missing — idempotent, so the index's own connect path
    can call it on every open and a pre-existing index gains the table on its next run."""
    conn.execute(LEDGER_DDL)


def ledger_rows(conn) -> list:
    """All ledger rows, most-recent activity first. Used by ``rag_index.py --stats``."""
    try:
        return list(conn.execute(
            "SELECT source, reason, overridden, refused_count, first_at, last_at, last_ref "
            "FROM provenance_refusals ORDER BY last_at DESC, source"))
    except sqlite3.Error:
        return []


# ------------------------------------------------------------------------------ CLI

def _explain() -> int:
    print("provenance guard — which writers may persist into the RAG index")
    print(f"  policy: {ATTESTED} -> persist · {UNATTESTED} -> REFUSE (fail closed)")
    print(f"  override: {ALLOW_FLAG} <source>   (argv only — a payload cannot vouch for itself)")
    print("")
    width = max(len(s) for s in SOURCE_PROVENANCE)
    print("  sources:")
    for source in sorted(SOURCE_PROVENANCE, key=lambda s: (SOURCE_PROVENANCE[s], s)):
        prov = SOURCE_PROVENANCE[source]
        mark = "persist" if prov == ATTESTED else ("stamp?  " if prov == STAMP_REQUIRED else "REFUSE ")
        print(f"    {mark:<8} {source:<{width}}  {prov}")
    print(f"    {'REFUSE':<8} {'(any other)':<{width}}  {UNATTESTED} — unregistered")
    print("")
    print(f"  producer stamps (key {STAMP_KEY!r}, required by any stamp-required source):")
    swidth = max(len(s) for s in STAMP_PROVENANCE)
    for stamp in sorted(STAMP_PROVENANCE, key=lambda s: (STAMP_PROVENANCE[s], s)):
        prov = STAMP_PROVENANCE[stamp]
        mark = "persist" if prov == ATTESTED else "REFUSE "
        print(f"    {mark:<8} {stamp:<{swidth}}  {prov}")
    print(f"    {'REFUSE':<8} {'(absent or unrecognised)':<{swidth}}  {UNATTESTED}")
    return 0


def _check(path: str, allow) -> int:
    guard = ProvenanceGuard(allow_unattested=allow)
    bad_lines = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    guard.check(json.loads(line))
                except ValueError:
                    bad_lines += 1
    except OSError as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2
    s = guard.summary()
    if bad_lines:
        s["unparseable_lines"] = bad_lines
    print(json.dumps(s, indent=2, sort_keys=True))
    guard.report()
    return 1 if s["refused"] else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Provenance guard for the RAG index (see the module docstring).")
    p.add_argument("--explain", action="store_true", help="print the writer registry and exit")
    p.add_argument("--check", metavar="JSONL",
                   help="dry-run an ingest JSONL: verdicts only, no DB, no writes")
    p.add_argument(ALLOW_FLAG, action="append", default=[], metavar="SOURCE",
                   dest="allow_unattested",
                   help="explicitly clear an unattested source (repeatable; a human act, on purpose)")
    args = p.parse_args(argv)
    if args.explain:
        return _explain()
    if args.check:
        return _check(args.check, args.allow_unattested)
    p.error("nothing to do — pass --explain or --check FILE")
    return 2  # pragma: no cover - argparse exits


if __name__ == "__main__":
    sys.exit(main())
