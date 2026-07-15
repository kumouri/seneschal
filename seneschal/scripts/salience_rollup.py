#!/usr/bin/env python3
"""Weekly salience rollup — Phase 3 of the what's-safe-to-forget experiment. Stdlib only.

Turns the accumulated evidence — the ``salience_access`` counters (Phase 1) and the
forgetting-events log (Phase 2) — into a per-category **report** on what kinds of memories
matter, and (only behind ``--propose``, only when a category clears the bar) **draft text** for
a gated prune proposal. **Reporting only — nothing here deletes, hides, or edits a memory.**
Dream runs this weekly (alongside the Observability graduation rollup) and carries any proposal
text into ``references/proposed-learnings.md`` via its normal PR path; the owner rules on it there.

The salience function it operationalizes (``../references/salience.md``)::

    salience(category) = f(access frequency) × g(emotional weight)

with the **protect clause** dominating: any category/doc with a forgetting-event of
``|sentiment| ≥ θ_protect`` (default 0.7) is *ineligible for a disposable recommendation, full
stop*, independent of access counts. That is how rare-but-heavy facts (birthdays, the
family-bereavement class) are structurally kept.

Honesty rules baked in:

* **Abstains** below the evidence window (default ≥30 days of tagged data AND ≥50
  disposable-tagged docs) — cold start prints "insufficient data" and recommends nothing.
* Counters mean *returned-above-floor*, a relative comparator — the report compares categories
  against each other, never claims absolute use.
* Reads the sqlite tables directly (never through ``rag_query.search()``), so the rollup can
  never pollute the counters it reads.

CLI::

    python salience_rollup.py                  # markdown report to stdout
    python salience_rollup.py --json           # machine shape
    python salience_rollup.py --propose        # + draft gated-proposal text for confirmed cats
    python salience_rollup.py --min-days 0 --min-tagged 1   # test/debug: shrink the window
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import rag_common as rc

EVENTS_FILE = rc.STATE / "forgetting-events.jsonl"
ABLATION_FILE = rc.STATE / "ablation-judgments.jsonl"

# Evidence window — below either bar the rollup abstains (plan §2.7: ≥30 days / ≥50 entries).
MIN_DAYS = 30
MIN_TAGGED = 50

# Protect clause (plan §3.3): |sentiment| at/above this ⇒ never prune-eligible.
THETA_PROTECT = 0.7

# Bucket thresholds on mean hits per tagged doc over the window. Deliberately conservative:
# the gap between them is an honest "inconclusive", not forced into a verdict.
CONFIRM_BELOW = 0.2   # mean hits/doc under this → prediction confirmed (candidate, gated)
REFUTE_ABOVE = 1.0    # mean hits/doc at/over this → prediction wrong (keep more of this kind)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None


# ------------------------------------------------------------------ evidence loading

def load_events(path: Path | str = EVENTS_FILE):
    """Read the forgetting-events JSONL, tolerantly — bad lines are skipped, never fatal."""
    path = Path(path)
    if not path.exists():
        return []
    events = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            print(f"  ! {path.name}:{lineno} skipped (bad JSON)", file=sys.stderr)
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def category_stats(conn):
    """Per-category access aggregates over the tagged partition (disposable=1) AND the kept rows.

    Doc-level (a "memory" ≈ a doc; chunks are its shards): hits summed across a doc's chunks.
    Returns {salience_cat: {...}} — categories present on any chunk row appear even at zero hits.
    """
    stats = {}
    rows = conn.execute(
        """
        SELECT COALESCE(c.salience_cat, 'untagged') AS cat,
               c.disposable,
               COUNT(DISTINCT c.doc_id)                    AS docs,
               COUNT(*)                                    AS chunks,
               COALESCE(SUM(a.hit_count), 0)               AS hits,
               COUNT(DISTINCT CASE WHEN a.hit_count > 0 THEN c.doc_id END) AS docs_hit,
               COALESCE(MAX(a.max_score), 0.0)             AS best_score,
               MIN(c.predicted_at)                         AS earliest_prediction
        FROM chunks c LEFT JOIN salience_access a ON a.chunk_id = c.id
        GROUP BY cat, c.disposable
        """
    )
    for cat, disposable, docs, chunks, hits, docs_hit, best, earliest in rows:
        entry = stats.setdefault(cat, {
            "docs": 0, "chunks": 0, "hits": 0, "docs_hit": 0, "best_score": 0.0,
            "tagged_docs": 0, "tagged_hits": 0, "tagged_docs_hit": 0,
            "forgotten_docs": 0, "earliest_prediction": None,
        })
        entry["docs"] += docs
        entry["chunks"] += chunks
        entry["hits"] += hits
        entry["docs_hit"] += docs_hit
        entry["best_score"] = max(entry["best_score"], best or 0.0)
        if disposable == 1:
            entry["tagged_docs"] += docs
            entry["tagged_hits"] += hits
            entry["tagged_docs_hit"] += docs_hit
            if earliest and (entry["earliest_prediction"] is None
                             or earliest < entry["earliest_prediction"]):
                entry["earliest_prediction"] = earliest
        elif disposable is not None and disposable >= 2:
            entry["forgotten_docs"] += docs
    return stats


def window_state(stats, now=None, *, min_days=MIN_DAYS, min_tagged=MIN_TAGGED):
    """Is there enough evidence to say anything? → (ok, days_observed, tagged_docs_total)."""
    now = now or _utcnow()
    tagged_total = sum(s["tagged_docs"] for s in stats.values())
    earliest = None
    for s in stats.values():
        ts = _parse_ts(s["earliest_prediction"]) if s["earliest_prediction"] else None
        if ts and (earliest is None or ts < earliest):
            earliest = ts
    days = (now - earliest).days if earliest else 0
    return (days >= min_days and tagged_total >= min_tagged), days, tagged_total


# ------------------------------------------------------------------ verdicts

def protection(events, *, theta=THETA_PROTECT):
    """The protect sets: categories and doc refs with any event of |sentiment| ≥ θ."""
    cats, docs = set(), set()
    for ev in events:
        try:
            weight = abs(float(ev.get("sentiment", 0.0)))
        except (TypeError, ValueError):
            continue
        if weight >= theta:
            if ev.get("salience_cat"):
                cats.add(ev["salience_cat"])
            if ev.get("chunk_ref"):
                docs.add(ev["chunk_ref"])
    return cats, docs


def divergences(events, *, gap=0.4, min_conf=0.5):
    """Opus-vs-classifier weight disagreements (Dream 2c cross-check) — data-quality flags."""
    out = []
    for ev in events:
        try:
            opus = float(ev.get("sentiment"))
            local = float(ev.get("classifier_weight"))
            conf = float(ev.get("classifier_confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if conf >= min_conf and abs(opus - local) >= gap:
            out.append({"subject": ev.get("subject", "?"), "opus": opus,
                        "classifier": local, "confidence": conf})
    return out


def ablation_summary(judgments):
    """Aggregate the ablation A/B oracle verdicts (Phase 4c). None when there's nothing yet.

    Verdicts: ``with`` = the memory improved the answer (keep signal); ``without``/``tie`` =
    disposability evidence. The "why" strings are surfaced verbatim — they're the labeled data
    for *what about* a memory mattered (Phase 4a).
    """
    counts = {"with": 0, "without": 0, "tie": 0}
    rows = []
    for j in judgments:
        v = j.get("verdict")
        if v not in counts:
            continue
        counts[v] += 1
        rows.append({"subject": j.get("subject", "?"), "verdict": v,
                     "why": j.get("why", ""), "blind": bool(j.get("blind", False))})
    if not rows:
        return None
    return {"counts": counts, "n": len(rows), "judgments": rows}


def _render_ablation(summary):
    c = summary["counts"]
    out = [
        "### Ablation A/B oracle (owner-judged ground truth)",
        f"_{summary['n']} judgments: **{c['with']}** with-memory better · "
        f"**{c['without']}** without as good/better · **{c['tie']}** ties._",
    ]
    for r in summary["judgments"][-10:]:  # the freshest few; the file is the full record
        blind = "blind" if r["blind"] else "not blind"
        out.append(f"- [{r['verdict']}] {r['subject']} ({blind}): {r['why']}")
    return "\n".join(out)


def bucket_verdicts(stats, events, *, theta=THETA_PROTECT,
                    confirm_below=CONFIRM_BELOW, refute_above=REFUTE_ABOVE):
    """Per-category verdicts, the three honest buckets (+ observing/inconclusive)."""
    protected_cats, protected_docs = protection(events, theta=theta)
    event_cats = {}
    for ev in events:
        cat = ev.get("salience_cat")
        if cat:
            event_cats.setdefault(cat, []).append(ev)

    verdicts = {}
    for cat, s in sorted(stats.items()):
        v = {
            "stats": s,
            "events": len(event_cats.get(cat, [])),
            "max_event_weight": max(
                (abs(float(e.get("sentiment", 0.0))) for e in event_cats.get(cat, [])
                 if isinstance(e.get("sentiment"), (int, float))),
                default=0.0,
            ),
            "protected": cat in protected_cats,
        }
        if v["protected"]:
            v["bucket"] = "protected"
            v["verdict"] = ("kept regardless of frequency — a forgetting event of "
                            f"|weight| ≥ {theta} protects this category outright")
        elif s["tagged_docs"] == 0:
            v["bucket"] = "observing"
            v["verdict"] = "no disposable predictions in this category — nothing to score"
        else:
            mean_hits = s["tagged_hits"] / s["tagged_docs"]
            v["mean_hits_per_tagged_doc"] = round(mean_hits, 3)
            if mean_hits < confirm_below:
                v["bucket"] = "confirmed"
                v["verdict"] = ("prediction confirmed — near-zero recall over the window; "
                                "candidate for a GATED prune proposal (draft-and-hold, "
                                "the owner rules)")
            elif mean_hits >= refute_above:
                v["bucket"] = "refuted"
                v["verdict"] = ("prediction wrong — this kind gets recalled; stop tagging it "
                                "disposable and keep more of it")
            else:
                v["bucket"] = "inconclusive"
                v["verdict"] = "between thresholds — keep observing"
        verdicts[cat] = v
    return verdicts, protected_docs


# ------------------------------------------------------------------ rendering

def render_report(verdicts, protected_docs, events, window, *, now=None, ablation=None):
    """The markdown report Dream folds into the digest."""
    ok, days, tagged = window
    now = now or _utcnow()
    lines = [
        "# Salience rollup — what's mattering, what isn't",
        f"_As of {now.date().isoformat()} · window: {days} days observed, "
        f"{tagged} disposable-tagged docs · {len(events)} forgetting events on file._",
        "",
    ]
    if not ok:
        lines += [
            f"**Insufficient data — no verdicts, no proposals.** The rollup abstains until "
            f"≥{MIN_DAYS} days of tagged data AND ≥{MIN_TAGGED} tagged docs (currently "
            f"{days}/{tagged}). Accumulation is the phase working as designed.",
        ]
        # The ablation oracle + divergence flags still render under abstention — human-judged
        # ground truth is meaningful from judgment #1, unlike the counters.
        if ablation:
            lines += ["", _render_ablation(ablation)]
        div = divergences(events)
        if div:
            lines += ["", _render_divergences(div)]
        return "\n".join(lines)

    order = {"protected": 0, "refuted": 1, "confirmed": 2, "inconclusive": 3, "observing": 4}
    for cat, v in sorted(verdicts.items(), key=lambda kv: (order[kv[1]["bucket"]], kv[0])):
        s = v["stats"]
        badge = {"protected": "🛡️", "refuted": "❗", "confirmed": "🗑️(gated)",
                 "inconclusive": "…", "observing": "👁"}[v["bucket"]]
        lines.append(f"## {badge} `{cat}` — {v['bucket']}")
        lines.append(f"- {v['verdict']}")
        lines.append(
            f"- access: {s['docs']} docs / {s['chunks']} chunks, {s['hits']} recorded hits "
            f"(best score {s['best_score']:.2f}); tagged partition: {s['tagged_docs']} docs, "
            f"{s['tagged_hits']} hits"
            + (f", mean {v['mean_hits_per_tagged_doc']}/doc"
               if "mean_hits_per_tagged_doc" in v else "")
        )
        if v["events"]:
            lines.append(f"- forgetting events: {v['events']} "
                         f"(max |weight| {v['max_event_weight']:.2f})")
        if s["forgotten_docs"]:
            lines.append(f"- approved-forgotten docs: {s['forgotten_docs']} "
                         "(their continued hits are un-forget evidence)")
        lines.append("")
    if protected_docs:
        lines.append(f"**Doc-level protection** (via `chunk_ref` on a heavy event): "
                     + ", ".join(f"`{d}`" for d in sorted(protected_docs)))
        lines.append("")
    if ablation:
        lines.append(_render_ablation(ablation))
        lines.append("")
    div = divergences(events)
    if div:
        lines.append(_render_divergences(div))
    return "\n".join(lines).rstrip() + "\n"


def _render_divergences(div):
    out = ["### Sentiment cross-check divergences (data-quality flags, not verdicts)"]
    for d in div:
        out.append(f"- {d['subject']}: opus {d['opus']:+.2f} vs classifier "
                   f"{d['classifier']:+.2f} (conf {d['confidence']:.2f})")
    return "\n".join(out)


def render_proposals(verdicts, window):
    """Draft gated-proposal text for confirmed categories — Dream files it via its PR path.

    Emits NOTHING unless the window is met and a category is confirmed. The text follows
    references/proposed-learnings.md's format; applying it remains the owner's call, and the first
    applied form of any prune is the reversible disposable=2 soft mark, never a delete.
    """
    ok, _, _ = window
    if not ok:
        return ""
    blocks = []
    today = _utcnow().date().isoformat()
    for cat, v in sorted(verdicts.items()):
        if v["bucket"] != "confirmed":
            continue
        s = v["stats"]
        blocks.append(
            f"- [ ] {today} — Salience: `{cat}` predictions look safe to soft-prune (gated)\n"
            f"  Pattern: {s['tagged_docs']} disposable-tagged docs in `{cat}` accrued "
            f"{s['tagged_hits']} recall hits over the evidence window "
            f"(mean {v.get('mean_hits_per_tagged_doc', 0)}/doc; no protecting forgetting-event). "
            f"The disposability prediction held.\n"
            f"  Proposal: mark this category's tagged docs older than N days "
            f"disposable=2 (approved-forgotten — SOFT prune: excluded from answers, fully "
            f"reversible, still counted for un-forget evidence). No hard deletion; that would "
            f"be a separate, later gate.\n"
            f"  Applies to: state/rag-index.sqlite chunk rows (via a small marking script) + "
            f"references/salience.md ladder.\n"
            f"  Gate check: internal-only, reversible by flipping the column back, no outbound; "
            f"the θ_protect clause and identity.core ineligibility both held over the window. "
            f"Needs the owner's explicit approval + their choice of the age threshold N."
        )
    return "\n\n".join(blocks)


# ------------------------------------------------------------------ CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="Weekly salience rollup (report-only).")
    ap.add_argument("--db", default=str(rc.DEFAULT_DB))
    ap.add_argument("--events", default=str(EVENTS_FILE))
    ap.add_argument("--ablation", default=str(ABLATION_FILE))
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--propose", action="store_true",
                    help="also draft gated-proposal text for confirmed categories")
    ap.add_argument("--min-days", type=int, default=MIN_DAYS)
    ap.add_argument("--min-tagged", type=int, default=MIN_TAGGED)
    ap.add_argument("--theta-protect", type=float, default=THETA_PROTECT)
    args = ap.parse_args(argv)

    try:
        conn = rc.connect(args.db)
    except Exception as exc:  # a broken index must not crash the Dream run
        print(f"salience rollup: cannot open index ({exc}) — nothing to report", file=sys.stderr)
        return 0

    try:
        stats = category_stats(conn)
        events = load_events(args.events)
        ablation = ablation_summary(load_events(args.ablation))  # same tolerant reader
        window = window_state(stats, min_days=args.min_days, min_tagged=args.min_tagged)
        verdicts, protected_docs = bucket_verdicts(stats, events, theta=args.theta_protect)
    finally:
        conn.close()

    if args.json:
        ok, days, tagged = window
        print(json.dumps({
            "window": {"ok": ok, "days": days, "tagged_docs": tagged},
            "verdicts": verdicts,
            "protected_docs": sorted(protected_docs),
            "divergences": divergences(events),
            "ablation": ablation,
        }, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_report(verdicts, protected_docs, events, window, ablation=ablation))
        if args.propose:
            proposals = render_proposals(verdicts, window)
            if proposals:
                print("\n---\n\n## Draft gated proposals (for proposed-learnings.md via "
                      "Dream's PR — NOT applied)\n")
                print(proposals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
