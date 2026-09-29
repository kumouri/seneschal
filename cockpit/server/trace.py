"""Read-only, tolerant reader for the cockpit's Trace panel — the session trace
(design: `seneschal/docs/session-trace-spec.md`, phase 1).

**What it is for.** An agent debug log for the assistant's warm sessions. Several files in `state/`
already record nearly every event a session produces; until the session key existed nothing joined
them into a *session*, so *"show me everything that happened in session X"* was unanswerable at any
cost. This serves the join to a browser.

**Deliberately NOT importing `seneschal/scripts/trace.py`.** Same posture as `jobs.py` / `governor.py` /
`model_config.py` here (cockpit-spec.md ruling 3): the cockpit is its own dependency world and its
backend must keep working against a state dir written by a daemon on a *different commit*. So this is
an independent reader of the on-disk shape. That is not duplication for its own sake — it is what
stops a daemon-side field rename from taking the observatory down with it, and it is why the tests
here are written against the on-disk shape rather than by importing the writer.

**Tolerance, same posture as `health.py` / `jobs.py`.** A missing file, a half-written line, a record
from an older or newer daemon — each degrades to less data or an honest `"available": false`, never a
500. Here that is load-bearing rather than polite: **a trace is opened precisely when something has
gone wrong**, which is exactly when its inputs are most likely to be damaged. A reader that raises on
a corrupt line is useless in the only situation it exists for.

**Privacy — the `!private` tombstone is honoured HERE, not only by the writer.** Conversation text
appears in this panel (from `turns.jsonl`, whose rows carry `speaker` ∈ `owner | assistant`), so
`turns.jsonl` marks a redacted turn with `redacted: true` and no `text`; this reader must never surface
text for such a turn and must never reconstruct it from a neighbouring log. A redaction that holds in
one reader and not another is not a redaction. An install without `turns.jsonl` simply shows no
conversation text — every other part of the trace still works.
"""
from __future__ import annotations

import json
from pathlib import Path

TRANSCRIPT = "warm-transcript.jsonl"
METRICS = "metrics.jsonl"
SESSION_STARTS = "session-starts.jsonl"
ASSERTIONS = "assertions.jsonl"
TURNS = "turns.jsonl"

# `turns.jsonl`'s two sides of the conversation.
SPEAKER_OWNER = "owner"
SPEAKER_ASSISTANT = "assistant"

DEFAULT_LIMIT = 40
DEFAULT_EVENT_LIMIT = 1500


def _rows(state_dir: Path, name: str) -> list:
    """Every parseable JSON object in a log, in file order. A corrupt line costs that line only —
    parsing per-line rather than per-file is the difference between "one bad byte" and "no trace"."""
    out: list = []
    try:
        with open(Path(state_dir) / name, "r", encoding="utf-8") as fh:
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


def _spawn_decisions(state_dir: Path) -> list:
    """Pair each `decision` row with the `opened` row that follows it.

    Exact, not a heuristic — the invariant is the daemon's (`presence.mark_session_opened`): one warm
    session at a time, one writer, so nothing interleaves. A decision with no following `opened` is a
    spawn that never came up and is kept unpaired rather than absorbed into the next session's id.
    Rows written before the `kind` field existed carry no `kind`; they are decisions."""
    out, pending = [], None
    for r in _rows(state_dir, SESSION_STARTS):
        if (r.get("kind") or "decision") == "opened":
            if pending is not None:
                pending["session_id"] = r.get("session_id")
                out.append(pending)
                pending = None
            continue
        if pending is not None:
            out.append(pending)
        pending = dict(r, session_id=None)
    if pending is not None:
        out.append(pending)
    return out


# The daemon writes `outcome` capitalised — "Success" / "Failed". Comparing against a lowercase-only
# set would count every successful turn as an error (a quiet session rendering a red error count, and
# the real failures indistinguishable from the noise). Case-fold, and keep the vocabulary in one named
# place so the next synonym is a one-line change rather than another silent miscount.
_OK_OUTCOMES = frozenset({"ok", "success", "succeeded", "done", "complete", "completed"})


def _is_error_outcome(outcome) -> bool:
    """True only for an outcome we can positively read as NOT a success.

    An absent outcome is not an error (older rows predate the field). An unrecognised *string* is,
    deliberately: this is an alarm count, and the tolerant direction for an alarm is to surface
    something unfamiliar rather than to swallow it."""
    if outcome is None:
        return False
    if not isinstance(outcome, str):
        return True
    return outcome.strip().lower() not in _OK_OUTCOMES


def _session_title(said_by_turn: dict, first_turn_id) -> str | None:
    """The first thing the owner said in the session, standing in for the session-id hash (an 8-hex
    prefix names nothing a human could recognise). Reuses `_turn_text`'s join rather than re-reading
    `turns.jsonl`, so this costs nothing the detail view wasn't already going to pay.

    **The tombstone applies here too.** A redacted first turn yields no title — `_turn_text` already
    empties `said` for one, but the check is explicit anyway: a title is the ONE field in this list
    a human reads before opening anything, so it is the last place a redaction should leak by accident."""
    if not first_turn_id:
        return None
    slot = said_by_turn.get(first_turn_id)
    if not slot or slot.get("redacted"):
        return None
    for s in slot.get("said", []):
        if s.get("speaker") == SPEAKER_OWNER:
            text = s.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def read_sessions(state_dir, limit: int = DEFAULT_LIMIT) -> dict:
    """The sessions list — one entry per warm session, newest first.

    Built from `metrics.jsonl`, **not** the transcript: the transcript is a capped ring, so a session
    that has rolled out of it still happened and must still be listed rather than silently ceasing to
    have existed. `available: false` only when there is no metrics log at all — an empty-but-present
    log is `available: true` with no sessions, because "nothing ran" and "I can't see anything" must
    not look the same."""
    limit = max(1, min(int(limit or DEFAULT_LIMIT), 500))
    path = Path(state_dir) / METRICS
    if not path.is_file():
        return {"available": False, "reason": "no metrics.jsonl", "sessions": []}

    by_id: dict = {}
    for r in _rows(state_dir, METRICS):
        sid = r.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        e = by_id.setdefault(sid, {
            "session_id": sid, "turns": 0, "cost_usd": 0.0, "context_peak": 0,
            "first_at": r.get("ts"), "last_at": r.get("ts"), "model": r.get("model"),
            "errors": 0, "sources": set(), "resumed": None, "start_class": None,
            # The row that CREATED this entry is, by construction, the first row of this session
            # `metrics.jsonl` holds — same assumption `first_at` above already rests on.
            "_first_turn_id": r.get("turn_id"),
        })
        e["turns"] += 1
        if r.get("ts"):
            e["last_at"] = r["ts"]
        if isinstance(r.get("cost_usd"), (int, float)) and not isinstance(r.get("cost_usd"), bool):
            e["cost_usd"] += float(r["cost_usd"])
        ctx = r.get("context_tokens")
        if isinstance(ctx, (int, float)) and not isinstance(ctx, bool) and ctx > e["context_peak"]:
            e["context_peak"] = int(ctx)
        if _is_error_outcome(r.get("outcome")):
            e["errors"] += 1
        if isinstance(r.get("source"), str):
            e["sources"].add(r["source"])
        if isinstance(r.get("model"), str):
            e["model"] = r["model"]

    for d in _spawn_decisions(state_dir):
        e = by_id.get(d.get("session_id"))
        if e is not None:
            e["resumed"] = bool(d.get("resumed"))
            e["start_class"] = d.get("class")
            e["start_why"] = d.get("why")

    out = sorted(by_id.values(), key=lambda e: e.get("last_at") or "", reverse=True)
    said_by_turn = _turn_text(state_dir)
    for e in out:
        e["sources"] = sorted(e["sources"])
        e["cost_usd"] = round(e["cost_usd"], 4)
        e["title"] = _session_title(said_by_turn, e.pop("_first_turn_id", None))
    return {"available": True, "sessions": out[:limit], "total": len(out)}


def _turn_text(state_dir: Path) -> dict:
    """`turn_id` → the verbatim text of both sides, from `turns.jsonl`.

    **The tombstone is honoured here.** A turn marked `redacted: true` yields a marker and *no text* —
    and because the marker is keyed by turn, a redacted turn cannot be silently backfilled from the
    transcript's `reply_preview` either (see `read_session`, which checks this map first)."""
    out: dict = {}
    for r in _rows(state_dir, TURNS):
        tid = r.get("turn_id")
        if not isinstance(tid, str) or not tid:
            continue
        slot = out.setdefault(tid, {"redacted": False, "said": []})
        if r.get("redacted"):
            slot["redacted"] = True
            slot["said"] = []
            continue
        if slot["redacted"]:
            continue
        text = r.get("text")
        if isinstance(text, str) and text:
            slot["said"].append({"speaker": r.get("speaker"), "origin": r.get("origin"),
                                 "text": text, "at": r.get("at")})
    return out


def _text_anchors(rows: list) -> dict:
    """`turn_id` → the row index that carries each speaker's text.

    A turn is ONE exchange but MANY transcript rows — one per step, and a busy turn runs to dozens of
    them. Stapling the turn's whole text onto every one of those rows would render the owner's message
    and the assistant's reply once per row, and most of a session payload would be duplicate bytes.

    Each side's text lands where it actually happened: **the owner's on `turn_started`, the
    assistant's on `turn_done`**. The middle rows keep their own tool calls and nothing else, which is
    what they are.

    **The fallbacks exist so nothing is ever dropped.** A truncated or partial turn may have no
    `turn_started` (or no `turn_done`) row at all; rather than lose that side's words, it falls back
    to the turn's first (or last) row. A turn with exactly one row therefore still carries both
    sides, which the tombstone tests rely on."""
    first: dict = {}
    last: dict = {}
    started: dict = {}
    done: dict = {}
    for i, r in enumerate(rows):
        tid = r.get("turn_id")
        if not isinstance(tid, str) or not tid:
            continue
        first.setdefault(tid, i)
        last[tid] = i
        kind = r.get("kind")
        if kind == "turn_started":
            started.setdefault(tid, i)
        elif kind == "turn_done":
            done[tid] = i
    return {tid: {SPEAKER_OWNER: started.get(tid, first[tid]),
                  SPEAKER_ASSISTANT: done.get(tid, last[tid])}
            for tid in first}


def _anchor_index(anchor, speaker) -> int:
    """Which row a segment belongs on. Anything that isn't the assistant rides the turn's head — an
    unrecognised speaker must still surface somewhere rather than vanish between the two anchors."""
    if not anchor:
        return -1
    return anchor[SPEAKER_ASSISTANT] if speaker == SPEAKER_ASSISTANT else anchor[SPEAKER_OWNER]


def read_session(state_dir, session_id: str, limit: int = DEFAULT_EVENT_LIMIT) -> dict:
    """One session's events, chronological — the Logs view's backing data.

    Transcript rows written before the session key existed carry `turn_id` alone and are attributed
    **through `metrics.jsonl`**, which carries both keys. A row asserts the link; two close timestamps
    never do. Rows with neither key are simply not attributable and are omitted rather than
    approximated."""
    if not isinstance(session_id, str) or not session_id.strip():
        return {"available": False, "reason": "no session id", "events": []}
    limit = max(1, min(int(limit or DEFAULT_EVENT_LIMIT), 10_000))

    turn_ids = {r.get("turn_id") for r in _rows(state_dir, METRICS)
                if r.get("session_id") == session_id and r.get("turn_id")}
    said = _turn_text(state_dir)

    rows = [r for r in _rows(state_dir, TRANSCRIPT)
            if r.get("session_id") == session_id or (turn_ids and r.get("turn_id") in turn_ids)]
    anchors = _text_anchors(rows)

    events: list = []
    for i, r in enumerate(rows):
        tid = r.get("turn_id")
        ev = {"src": "transcript", "ts": r.get("ts"), "kind": r.get("kind"), "turn_id": tid,
              "model": r.get("model"), "source": r.get("source")}
        if r.get("tool_uses"):
            ev["tool_uses"] = r["tool_uses"]
        slot = said.get(tid) if tid else None
        anchor = anchors.get(tid) if tid else None
        if slot and slot["redacted"]:
            # The marker rides the anchors, so a redacted turn says so where its text would have
            # been rather than on every one of its rows. The SUPPRESSION is unconditional though: no
            # row of a redacted turn carries a preview, anchor or not. That half is the invariant.
            if anchor is None or i in (anchor[SPEAKER_OWNER], anchor[SPEAKER_ASSISTANT]):
                ev["redacted"] = True
        else:
            if slot and slot["said"]:
                segs = [s for s in slot["said"] if _anchor_index(anchor, s.get("speaker")) == i]
                if segs:
                    ev["said"] = segs
            if r.get("reply_preview"):
                ev["reply_preview"] = r["reply_preview"]
        for k in ("usage", "total_cost_usd", "duration_ms", "is_error", "num_turns"):
            if r.get(k) is not None:
                ev[k] = r[k]
        events.append(ev)

    for r in _rows(state_dir, ASSERTIONS):
        if r.get("session_id") == session_id:
            events.append({"src": "assertion", "ts": r.get("at"), "kind": r.get("kind"),
                           "surface": r.get("surface"), "delivered": r.get("delivered")})

    for d in _spawn_decisions(state_dir):
        if d.get("session_id") == session_id:
            events.append({"src": "spawn", "ts": d.get("at"), "kind": d.get("class"),
                           "why": d.get("why"), "resumed": d.get("resumed")})

    events.sort(key=lambda e: e.get("ts") or "")
    tools = sum(len(e.get("tool_uses") or []) for e in events)
    return {"available": True, "session_id": session_id, "events": events[:limit],
            "total": len(events), "tool_calls": tools,
            "truncated": len(events) > limit}
