#!/usr/bin/env python3
"""Spend levers, phase 1 — *which lever* made a turn cost what it cost. Standard library only.

Spec: `../docs/spend-levers-spec.md` (§3 for the twelve levers, §5.1 for the record, §8 for the
phasing). Phase 0 is the join key (`turn_id` on the governor-ledger row). This phase lands the levers
that **exist nowhere at all** — the ones the stream tee already sees and throws away (§3, "observed
and discarded").

## What this measures, and where

The daemon's per-turn stream tee builds one `on_event` callback per turn, and every stream-json line
the `claude` CLI emits passes through it. That callback hands each line to
`cockpit_pipe.build_chat_event_from_stream`, which **drops `user` events entirely** (they are
uninteresting to a chat pane) — and `user` events are exactly where tool *results* live. So the most
expensive thing in the window passes through a function the daemon already runs, is parsed, and is
discarded one line later.

`TurnLevers` is that counting site: fed the **raw** event, before the cockpit conversion, accumulating
into three integers and flushed at `turn_done` onto the ledger row's `levers` block.

| Field | Lever | What it is |
|---|---|---|
| `tool_calls` | L5 | `tool_use` blocks the assistant emitted this turn |
| `tool_result_bytes` | L5 | UTF-8 bytes of tool-result **text** that entered the window |
| `tool_result_images` | L6 | image blocks that came back in a tool result |

**L5 is the compounding one** (§3.1). A 40 kB read on turn 3 is not a turn-3 cost; the whole
conversation is re-sent every turn, so it is charged on turns 3..13 and shows up as ten unremarkable
cache-read totals with nothing anywhere naming what was in them. `tool_result_bytes` is the field that
names it.

## Three things this deliberately does NOT record, and why

1. **`attachments` (§5.1's proposed field) is not written, because the daemon cannot produce one.**
   The warm session's user message is always `[{"type": "text", "text": text}]` — text, always — and
   an inbound photo becomes a *descriptor naming a saved path*, never an image block. So an inbound
   attachment costs a few dozen tokens of text and nothing more; the image only ever enters the window
   if the assistant chooses to `Read` it, which arrives as a tool result. That is why the field here
   is `tool_result_images` and not `attachments`: it is measured where the cost actually is. Writing
   `attachments` off the inbound queue would be a confidently wrong attribution.
2. **`turns_served` and `server_tools` are not copied here.** Both already exist on the
   `metrics.jsonl` row for the same turn (`turns_served`, `usage.server_tool_use`), and phase 0's join
   key reaches them. Duplicating them into the ledger is spec §11 **Q1** (fat rows vs. a join), an
   open decision this phase does not pre-empt.
3. **Image *bytes* are not added to `tool_result_bytes`.** An image's base64 payload has no stable
   relationship to what it costs in the window (that is a function of its dimensions), so summing b64
   characters into a byte total would make the one field anyone regresses against quietly wrong.
   Images are counted, not weighed.

## Invariants

- **`observe` never raises.** Same contract as `governor.append_spend` (spec R5, fail-open at the
  field level): a malformed event costs the count, never the row, and the row never costs the turn.
  The stream tee already swallows a raising callback, but a counter that relies on its caller's
  try/except is one refactor from silently zeroing.
- **A counter that was never fed reports nothing** — `snapshot()` returns `None`, and the ledger row
  then omits `levers` altogether rather than writing zeros (spec R4: an absent field, never a 0,
  because a 0 reads as *measured, and free*). A `0` that IS written means observed-and-genuinely-zero.
- **`flush()` resets.** A turn can meter more than once — a first-turn fallback that re-sends after a
  failed resume ends each attempt in its own `result` event, so each writes its own ledger row.
  Resetting means every row describes the span it covers instead of the second one re-reporting the
  first one's reads.
- **Report-only.** Nothing here gates, throttles, refuses or selects a model. Enforcement is
  `governor.py`'s (Oikonomos) and the two must not grow into each other — spec R1. Diagnosis that
  quietly grows a gate would route around the governor's own rails rather than implement them;
  `governor.py` remains the only place a rail may live.

THREE more invariants, each a wrong number if broken: it is fed the RAW event, ABOVE
`build_chat_event_from_stream`; a `tool_result`'s `content` is a bare string in the common case, so a
counter written only against the block-list shape reads every ordinary file read as 0 bytes while
looking healthy; and `flush()` resets, because an OVER-reporting lever is worse than an absent one.

The ledger rows it reads may predate the join key or the billable breakdown: a row with no `turn_id`
is reported as unattributed, and a row with no `billable_tokens` falls back to its raw `tokens`.

USAGE (the reader is the join phase 0 made possible):
  python spend_levers.py report                 # today (the owner's local day), dearest turns first
  python spend_levers.py report --days 7 --limit 40
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import governor
import paths

METRICS_FILE = "metrics.jsonl"


# ------------------------------------------------------------------------------- the counting site

class TurnLevers:
    """Per-turn accumulator over raw claude-CLI stream-json events. Pure and side-effect-free, so it
    unit-tests with plain dicts and no live `claude` process — the same property that makes
    `cockpit_pipe.build_chat_event_from_stream` testable."""

    __slots__ = ("tool_calls", "tool_result_bytes", "tool_result_images", "_observed")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.tool_calls = 0
        self.tool_result_bytes = 0
        self.tool_result_images = 0
        self._observed = False

    def observe(self, ev) -> None:
        """Count one stream-json line. NEVER raises (see the invariants above)."""
        try:
            if not isinstance(ev, dict):
                return
            self._observed = True
            kind = ev.get("type")
            if kind not in ("assistant", "user"):
                return
            message = ev.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            if not isinstance(blocks, list):
                return
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if kind == "assistant" and btype == "tool_use":
                    self.tool_calls += 1
                elif kind == "user" and btype == "tool_result":
                    self._measure_result(block.get("content"))
        except Exception:  # noqa: BLE001 — a counter must never cost the turn it is counting
            pass

    def _measure_result(self, content) -> None:
        """One tool result's contribution. The CLI sends `content` as a bare string for the common case
        and as a block list when the result is mixed (text + images) — both shapes are real and both
        were read off live transcripts, not assumed."""
        if isinstance(content, str):
            self.tool_result_bytes += _utf8_len(content)
            return
        if not isinstance(content, list):
            return
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                self.tool_result_bytes += _utf8_len(part.get("text"))
            elif ptype == "image":
                self.tool_result_images += 1

    def snapshot(self) -> dict | None:
        """The `levers` block for this turn, or None if nothing was ever observed — in which case the
        ledger row carries no `levers` key at all (R4)."""
        if not self._observed:
            return None
        return {"tool_calls": self.tool_calls,
                "tool_result_bytes": self.tool_result_bytes,
                "tool_result_images": self.tool_result_images}

    def flush(self) -> dict | None:
        """Snapshot, then reset — so a turn that meters twice does not double-report the first row's
        reads on the second."""
        snap = self.snapshot()
        self.reset()
        return snap


def _utf8_len(value) -> int:
    if not isinstance(value, str) or not value:
        return 0
    try:
        return len(value.encode("utf-8"))
    except (UnicodeError, MemoryError):
        return len(value)  # a byte count that is off by the multibyte share beats losing the row


# --------------------------------------------------------------------------------------- the reader

def read_metrics(state_dir) -> list[dict]:
    """Tolerant JSONL read of `metrics.jsonl`, mirroring `governor._read_ledger`: a corrupt line is
    skipped, a missing file is []."""
    try:
        with open(os.path.join(state_dir, METRICS_FILE), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def join_turns(state_dir, days: int = 1, now=None) -> dict:
    """Join the spend ledger to the per-turn metrics on `turn_id` — the query phase 0's join key exists
    for, and which nothing had actually run.

    Windowed on the **owner's local** calendar days (the house rule; `governor._to_local_date` already
    owns that arithmetic, including the machine-local fallback). Returns both halves of the honest picture:
    `turns`, the joined rows, and `unattributed`, the in-window ledger spend that names no turn — a
    Fable delegation or a job child. A report that silently dropped those would understate the day."""
    today = governor._to_local_date(now) if now is not None else governor._local_today()
    metrics_by_turn: dict[str, dict] = {}
    for row in read_metrics(state_dir):
        tid = row.get("turn_id")
        if isinstance(tid, str) and tid:
            metrics_by_turn[tid] = row

    turns: list[dict] = []
    unattributed: list[dict] = []
    for row in governor._read_ledger(state_dir):
        ts = governor._parse_iso(row.get("ts"))
        if ts is None or (today - governor._to_local_date(ts)).days >= days:
            continue
        billable = row.get("billable_tokens")
        if not isinstance(billable, (int, float)) or isinstance(billable, bool):
            billable = row.get("tokens") if isinstance(row.get("tokens"), int) else None
        entry = {"ts": row.get("ts"), "kind": row.get("kind"), "model": row.get("model"),
                 "billable": billable, "levers": row.get("levers") if isinstance(row.get("levers"), dict) else None}
        tid = row.get("turn_id")
        if isinstance(tid, str) and tid:
            entry["turn_id"] = tid
            entry["metrics"] = metrics_by_turn.get(tid)
            turns.append(entry)
        else:
            unattributed.append(entry)
    return {"day": today.isoformat(), "days": days, "turns": turns, "unattributed": unattributed}


def _kb(n) -> str:
    return f"{n / 1024.0:,.1f}" if isinstance(n, (int, float)) and not isinstance(n, bool) else "—"


def _int(n) -> str:
    return f"{n:,}" if isinstance(n, (int, float)) and not isinstance(n, bool) else "—"


def render_report(joined: dict, limit: int = 20) -> str:
    """The per-turn diagnosis as text. Sorted dearest-first, because the question this answers is
    *'why was today expensive'* and the answer is usually two rows, not the distribution."""
    turns = joined["turns"]
    lines = [f"spend levers — last {joined['days']} local day(s), through {joined['day']}",
             f"{len(turns)} metered turn(s), {len(joined['unattributed'])} row(s) with no turn to name",
             ""]
    priced = sorted((t for t in turns if isinstance(t.get("billable"), (int, float))),
                    key=lambda t: t["billable"], reverse=True)
    unpriced = [t for t in turns if not isinstance(t.get("billable"), (int, float))]

    lines.append(f"{'time':<17}{'model':<20}{'billable':>11}{'turn':>6}{'tools':>7}"
                 f"{'result KB':>11}{'img':>5}  source")
    for t in priced[:limit]:
        lev = t.get("levers") or {}
        met = t.get("metrics") or {}
        ts = (t.get("ts") or "")[:16].replace("T", " ")
        model = (t.get("model") or "—")[:19]
        lines.append(
            f"{ts:<17}{model:<20}{_int(t.get('billable')):>11}"
            f"{_int(met.get('turns_served')):>6}"
            f"{_int(lev.get('tool_calls')) if lev else '—':>7}"
            f"{_kb(lev.get('tool_result_bytes')) if lev else '—':>11}"
            f"{_int(lev.get('tool_result_images')) if lev else '—':>5}"
            f"  {met.get('source') or '—'}")
    if len(priced) > limit:
        lines.append(f"… {len(priced) - limit} more turn(s) not shown (--limit)")
    if unpriced:
        lines.append(f"({len(unpriced)} turn row(s) carried no token count — metered 'unavailable')")

    with_levers = [t for t in turns if t.get("levers")]
    lines.append("")
    lines.append(f"levers present on {len(with_levers)} of {len(turns)} turn(s) "
                 "(rows written before spend-levers phase 1 carry none)")
    if with_levers:
        total_bytes = sum(t["levers"].get("tool_result_bytes") or 0 for t in with_levers)
        total_calls = sum(t["levers"].get("tool_calls") or 0 for t in with_levers)
        total_images = sum(t["levers"].get("tool_result_images") or 0 for t in with_levers)
        lines.append(f"  tool calls {total_calls:,} · tool-result text {total_bytes / 1024.0:,.1f} KB "
                     f"· images {total_images:,}")
    unattrib_total = sum(e["billable"] for e in joined["unattributed"]
                         if isinstance(e.get("billable"), (int, float)))
    if joined["unattributed"]:
        lines.append(f"  unattributed spend (delegations / job children): {unattrib_total:,.0f} billable "
                     "— not a warm turn, so it names no turn_id by design")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Spend levers — why a turn cost what it cost (read-only).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rep = sub.add_parser("report", help="join the spend ledger to per-turn metrics and print the levers")
    rep.add_argument("--state-dir", default=None,
                     help="state directory (default: SENESCHAL_STATE_DIR, else seneschal/state)")
    rep.add_argument("--days", type=int, default=1, help="owner-local days back (default 1 = today)")
    rep.add_argument("--limit", type=int, default=20, help="turns to print (default 20)")
    rep.add_argument("--json", action="store_true", help="emit the joined rows as JSON instead")
    args = parser.parse_args(argv)

    joined = join_turns(paths.state_dir(args.state_dir), days=max(1, args.days))
    if args.json:
        print(json.dumps(joined, ensure_ascii=False, indent=2))
    else:
        print(render_report(joined, limit=max(1, args.limit)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
