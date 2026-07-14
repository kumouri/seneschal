#!/usr/bin/env python3
"""Append one memory-ablation judgment — salience Phase 4(c), the human-judged oracle. Stdlib only.

The ablation A/B is the **gold standard** the cheap salience proxies are calibrated against
(``../references/salience.md``; plan §1 + §5c): for a sampled recall turn the assistant answers **twice** —
once *with* the retrieved memory, once with it *withheld* — the owner picks the better answer (blind
where practical) **and says why**. Their verdict is ground truth on whether the memory improved the
answer; the "why" is labeled data for *what about* it mattered.

This helper just makes the log durable and well-formed: one JSON line appended to
``state/ablation-judgments.jsonl`` (gitignored; seed ``ablation-judgments.example.jsonl``; schema in
``state/README.md``). The weekly ``salience_rollup.py`` folds the verdicts into its report.

Usage::

    python ablation_log.py --query "when's my next therapy session" \
        --subject "therapy schedule recall" --verdict with \
        --why "the with-memory answer had the actual date; without was generic" \
        --chunk-ref journal:2026-07-05 [--not-blind] [--judged-by owner]

``--verdict with``    the WITH-memory answer was better (keep signal for the memory)
``--verdict without`` the withheld answer was as good or better (disposability signal)
``--verdict tie``     no meaningful difference (mild disposability signal)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE.parent / "state"
DEFAULT_LOG = STATE / "ablation-judgments.jsonl"

VERDICTS = ("with", "without", "tie")


def append_judgment(*, query, verdict, why, subject=None, chunk_refs=(), judged_by="owner",
                    blind=True, log_path: Path | str = DEFAULT_LOG, now=None) -> dict:
    """Validate + append one judgment line; returns the record written.

    Raises ``ValueError`` on a bad verdict or empty why/query — a malformed oracle
    label is worse than none, so this helper refuses rather than coerces.
    """
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    query = (query or "").strip()
    why = (why or "").strip()
    if not query:
        raise ValueError("query must be non-empty")
    if not why:
        raise ValueError("why must be non-empty — the reason IS the labeled data")

    rec = {
        "ts": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "query": query,
        "subject": (subject or "").strip() or query[:80],
        "chunk_refs": list(chunk_refs),
        "verdict": verdict,
        "why": why,
        "judged_by": judged_by,
        "blind": bool(blind),
    }
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description="Append one memory-ablation A/B judgment.")
    ap.add_argument("--query", required=True, help="the recall question that was A/B'd")
    ap.add_argument("--subject", help="short label for what memory was tested (default: the query)")
    ap.add_argument("--verdict", required=True, choices=VERDICTS,
                    help="which answer the owner judged better")
    ap.add_argument("--why", required=True, help="the owner's reason, verbatim-ish — the labeled data")
    ap.add_argument("--chunk-ref", action="append", default=[], dest="chunk_refs",
                    metavar="DOC_ID", help="doc id(s) of the memory tested (repeatable)")
    ap.add_argument("--judged-by", default="owner")
    ap.add_argument("--not-blind", action="store_true",
                    help="the owner knew which answer had the memory (default assumes blind)")
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    args = ap.parse_args(argv)

    try:
        rec = append_judgment(
            query=args.query, subject=args.subject, verdict=args.verdict, why=args.why,
            chunk_refs=args.chunk_refs, judged_by=args.judged_by,
            blind=not args.not_blind, log_path=args.log,
        )
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
