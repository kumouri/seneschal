#!/usr/bin/env python3
"""The session trace reader — phase 0 of `../docs/session-trace-spec.md`.

**This is a join, not an instrumentation layer.** The daemon already emits nearly every event an
agent debug log would show; they are split across several files, keyed inconsistently, and read by
nothing. Phase 0 adds the one missing key — `session_id` on the transcript — and this module is what
turns that into an answer to *"show me everything that happened in session X"*, which otherwise
cannot be answered at any cost (spec §2.1a).

**No UI, and nothing here changes behaviour.** Every reader is tolerant: a missing, truncated, or
corrupt file yields less data, never an exception. That posture is load-bearing rather than polite — a
trace is consulted precisely when something has gone wrong, which is exactly when its inputs are most
likely to be damaged, and a reader that raises on a half-written line is useless in the only situation
it exists for.

**What it deliberately does NOT do:** invent a session boundary from timestamps. The pairing of a
spawn decision to the session it produced comes from an explicit `opened` row (the daemon's
session-opened marker), not from proximity. Timestamp-proximity joins are the thing this spec exists
to remove — `../docs/spend-levers-spec.md` §2.2 is the precedent, where two rows written milliseconds
apart by the same function could only be paired by guessing.

The files it reads, all under the state dir, all append-only JSONL, all optional:

| File | Rows it uses | Key fields |
|---|---|---|
| `warm-transcript.jsonl` | the cockpit ring's `chat.event` rows | `ts`, `kind`, `turn_id`, `session_id` (newer rows), `tool_uses`, `reply_preview` |
| `metrics.jsonl` | one row per warm turn | `ts`, `session_id`, `turn_id`, `model`, `cost_usd`, `context_tokens`, `outcome`, `source` |
| `session-starts.jsonl` | `decision` rows (no `kind` on older rows) then an `opened` row | decision: `at`, `resumed`, `class`, `why`, `reason`; opened: `kind: "opened"`, `at`, `session_id` |
| `assertions.jsonl` | what the assistant said, per send site | `at`, `kind`, `session_id` |

The cockpit's own trace endpoint (`cockpit/server/`) reads the same files for the web Trace panel
(a collapsible section under the chat pane, the TURN as its unit); this CLI is the scripts-side door.

CLI:
    python trace.py sessions [--state-dir DIR] [--limit N]
    python trace.py show <session_id> [--state-dir DIR] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import paths

TRANSCRIPT = "warm-transcript.jsonl"
METRICS = "metrics.jsonl"
SESSION_STARTS = "session-starts.jsonl"
ASSERTIONS = "assertions.jsonl"


def _rows(state_dir: str, name: str) -> list:
    """Every parseable JSON object in a log, in file order. A corrupt line is skipped, never raised —
    and skipped *individually*, so one bad byte costs one row rather than the rest of the file."""
    out = []
    try:
        with open(os.path.join(state_dir, name), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def spawn_decisions(state_dir: str) -> list:
    """Pair each `decision` row with the `opened` row that follows it.

    **Exact, not heuristic** — the invariant belongs to the daemon's session-opened marker: the daemon
    runs exactly one warm session at a time and this file has exactly one writer, so nothing can
    interleave between a decision and the open it caused. A decision with no following `opened` is
    kept with `session_id: None`: that is a spawn that never came up, which the trace should show
    rather than hide.

    Rows written before the `opened` kind existed have no `kind`; they are decisions, and are read as
    such."""
    out, pending = [], None
    for r in _rows(state_dir, SESSION_STARTS):
        kind = r.get("kind") or "decision"
        if kind == "opened":
            if pending is not None:
                pending["session_id"] = r.get("session_id")
                pending["opened_at"] = r.get("at")
                out.append(pending)
                pending = None
            continue
        if pending is not None:
            out.append(pending)          # the previous spawn never opened
        pending = dict(r, session_id=None)
    if pending is not None:
        out.append(pending)
    return out


def sessions(state_dir: str, limit: int = 50) -> list:
    """One entry per warm session, newest first — the Trace panel's sessions list (spec §4).

    Built from `metrics.jsonl` (whose per-turn rows carry `session_id`) enriched with the spawn
    decision. Deliberately NOT built from the transcript: the transcript is a capped ring, so a
    session that has rolled out of it still existed and should still be listed, greyed out, rather
    than silently ceasing to have happened."""
    by_id: dict = {}
    for r in _rows(state_dir, METRICS):
        sid = r.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        e = by_id.setdefault(sid, {
            "session_id": sid, "turns": 0, "cost_usd": 0.0, "context_peak": 0,
            "first_at": r.get("ts"), "last_at": r.get("ts"), "model": r.get("model"),
            "errors": 0, "sources": set(),
        })
        e["turns"] += 1
        e["last_at"] = r.get("ts") or e["last_at"]
        if isinstance(r.get("cost_usd"), (int, float)):
            e["cost_usd"] += float(r["cost_usd"])
        ctx = r.get("context_tokens")
        if isinstance(ctx, (int, float)) and ctx > e["context_peak"]:
            e["context_peak"] = int(ctx)
        if r.get("outcome") not in (None, "ok", "success"):
            e["errors"] += 1
        if r.get("source"):
            e["sources"].add(r["source"])

    for d in spawn_decisions(state_dir):
        e = by_id.get(d.get("session_id"))
        if e is not None:
            e["resumed"] = bool(d.get("resumed"))
            e["start_class"] = d.get("class")
            e["start_why"] = d.get("why")
            e["prior_reason"] = d.get("reason")

    out = sorted(by_id.values(), key=lambda e: e.get("last_at") or "", reverse=True)
    for e in out:
        e["sources"] = sorted(e["sources"])
        e["cost_usd"] = round(e["cost_usd"], 4)
    return out[:max(1, int(limit or 50))]


def session_events(state_dir: str, session_id: str, limit: int = 2000) -> list:
    """Every event belonging to one session, chronological — the Logs view's backing data.

    Newer transcript rows carry `session_id`; older rows carry `turn_id` alone. Rather than guess,
    this joins those through `metrics.jsonl`, whose rows carry BOTH keys —
    so a turn is attributed to a session because a row says so, never because the clocks were close.
    Rows older than either key are simply not attributable, and are omitted rather than approximated."""
    if not session_id:
        return []
    turn_ids = {r.get("turn_id") for r in _rows(state_dir, METRICS)
                if r.get("session_id") == session_id and r.get("turn_id")}

    events = []
    for r in _rows(state_dir, TRANSCRIPT):
        if r.get("session_id") == session_id or (r.get("turn_id") in turn_ids and turn_ids):
            events.append({"src": "transcript", "ts": r.get("ts"), "kind": r.get("kind"),
                           "turn_id": r.get("turn_id"), "row": r})
    for r in _rows(state_dir, ASSERTIONS):
        if r.get("session_id") == session_id:
            events.append({"src": "assertion", "ts": r.get("at"), "kind": r.get("kind"), "row": r})
    for d in spawn_decisions(state_dir):
        if d.get("session_id") == session_id:
            events.append({"src": "spawn", "ts": d.get("at"), "kind": d.get("class"), "row": d})

    events.sort(key=lambda e: e.get("ts") or "")
    return events[:max(1, int(limit or 2000))]


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["sessions", "show"])
    ap.add_argument("session_id", nargs="?")
    ap.add_argument("--state-dir", default=None,
                    help="state directory (default: SENESCHAL_STATE_DIR, else seneschal/state)")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    sd = os.path.abspath(paths.state_dir(a.state_dir))

    if a.command == "sessions":
        rows = sessions(sd, limit=a.limit or 50)
        if not rows:
            print("no sessions found (no metrics.jsonl rows carrying a session_id)")
            return 0
        print(f"{'session':<40} {'turns':>5} {'cost':>8} {'ctx peak':>9}  start")
        for e in rows:
            start = e.get("start_class") or "—"
            print(f"{e['session_id'][:38]:<40} {e['turns']:>5} "
                  f"{e['cost_usd']:>8.2f} {e['context_peak']:>9,}  {start}")
        return 0

    if not a.session_id:
        print("show requires a session id", file=sys.stderr)
        return 2
    evs = session_events(sd, a.session_id, limit=a.limit or 2000)
    if not evs:
        print(f"no events for {a.session_id}")
        return 0
    for e in evs:
        row = e["row"]
        detail = ""
        if e["src"] == "transcript" and row.get("tool_uses"):
            detail = " ".join(t.get("name", "?") for t in row["tool_uses"])
        elif e["src"] == "transcript" and row.get("reply_preview"):
            detail = str(row["reply_preview"])[:70]
        elif e["src"] == "spawn":
            detail = str(row.get("why", ""))[:70]
        print(f"{(e.get('ts') or '')[:19]:<20} {e['src']:<11} {str(e.get('kind') or ''):<18} {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
