#!/usr/bin/env python3
"""The Mouth — the assertions log and the outbound dispatch queue. Standard library only.

Spec: `seneschal/docs/mouth-spec.md` (§3.3 for the record, §7 for the phasing).

## Phase 0: the assertions log

`state/assertions.jsonl` is an append-only record of **what the assistant has actually said to the
owner** — across every surface (telegram / discord / call / cockpit), every door, and every producer.
Without it no hand can answer *"what does the owner currently believe, based on what we have told
them?"*, which has to be answerable before the assistant can speak consistently across surfaces.

Phase 0 only *measures*: nothing about when or whether a message is sent changes because this log
exists. It is the artifact every later phase reads, landed first, where it cannot break anything.

## Two invariants, and they are the whole contract

1. **Recording never costs the message.** `record_assertion` returns a bool and *never raises* — a
   full disk, a clobbered file, an exotic surface value all cost the row, never the send. Every call
   site is already past the point of no return (the message has landed) when it calls in here, so no
   caller wraps it in a try/except and none should.
2. **A row means it landed.** Producers call this only *after* a send they have confirmed, so
   `delivered: true` is a fact rather than an intention. (`delivered: false` exists for the queue's
   drop path — an expired item recorded rather than vanishing — and for a bypass mouth that wants to
   record a failed alert.)

It is written **in code, at the send sites**, not asked for in a prompt: a record specified only in
prompt text tends not to get written at all.

## Where the rows come from

Every hand that speaks to the owner records here after a landed send: reminder nudges, turn replies
and the daemon's own notices, background-job completion pushes, alerts, and landed Telegram pickers
(`kind="question"`, via `telegram_ask`). Bypass mouths — processes that exist to speak *when the
daemon is dead* — keep sending directly and append here **best-effort** through the `record` CLI (spec
§5.1): a failed append costs the append, never the alert.

## The turn correlator and the derived scope

Two optional fields ride the same row: `turn_id`, and a `scope` sub-object derived from that turn's
own tool calls. **Instrumentation only — nothing reads them, and a test asserts no module here does.**
The warm transcript that knows what was looked at is a ring buffer, which is why `scope` is computed
inline at send time rather than joined later — the older half would be gone.

**Both are optional in the strong sense.** A reminder nudge, a job push, a watcher, a bypass mouth:
none of these happen inside a chat turn, so none carry either field, and absence is the honest reading
rather than a gap. A malformed `turn_id` and an uncomputable `scope` each cost THE FIELD, in their own
try, because a raise inside the outer one would cost the ROW. The invariants a later edit must not
undo:

* **`scope` is DERIVED, never interpretive** — the distinct tool names and the read targets
  (`SCOPE_READ_TARGET_KEYS` is the one list, and it holds PLACES — a command/prompt/query is not a
  place) the turn actually asked for, and a count. No claim about what came back, what it meant, or
  whether the look was adequate; tool *results* are out of scope permanently.
* **`tool_calls: 0` is a MEASURED zero and an absent `scope` is "not measured".** Never collapse
  them: "did an error correlate with FEW tool calls or with NARROW ones" is exactly the question a
  conflation would answer wrongly. The accumulator is registered before the turn's first stream event
  for this reason.
* `TurnScope` is fed the RAW stream event, above the cockpit conversion, and stores what it
  EXTRACTED, never the input it came from.

## Phase 1: the queue

`enqueue` / `drain` below: unsolicited speech queues in `state/outbound.jsonl` and the daemon's tick
drains it FIFO with a staleness annotation. See the section comment above `OUTBOUND_SCHEMA`.

USAGE (the CLI exists for the bypass mouths and for reading the log by hand):
  python mouth.py record --surface telegram --kind alert --speaker watchdog --text "..."
  python mouth.py tail --limit 20
  python mouth.py prune --days 30
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

SCHEMA = "seneschal.assertion/1"
ASSERTIONS_FILE = "assertions.jsonl"

# One retention constant, matching `session-distillations.jsonl` and the job-origin mailbox family.
# Dream sweeps it (SKILL.md Dream step 2). Compaction into the RAG index is deliberately NOT part of
# this spec — mouth-spec.md §8.
RETENTION_DAYS = 30

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

# Vocabularies. These are validated *loosely*: an unknown value is recorded verbatim rather than
# rejected, because a producer that speaks must never be blocked by this module's opinion about
# labels. They exist so a reader (and phase 2's shaper) knows what to expect.
SURFACES = ("telegram", "discord", "call", "cockpit")
# `reply` is not in the spec's §3.2 kind list because that list types a *queue item*, and the queue is
# Door A only. The log spans both doors, so Door B needs a name. `unknown` is the honest default for a
# producer that hasn't been told what it is.
# `question` is a landed Telegram picker (`telegram_ask.ask`, ask-provenance-spec.md phase 2). Without
# it the record of what the assistant has said would exclude every question it asked, and the only
# store that holds a picker self-liquidates within days into a tombstone with no wording on it.
KINDS = ("reminder", "job", "alert", "nudge", "digest", "archon", "reply", "question", "unknown")
# In phase 0 every row is `direct`: there is no queue yet. The field is written anyway so the log has
# one shape across phases and a phase-1 reader never has to special-case rows written before it.
DOORS = ("queue", "direct")

# The append is one small write per row, which is the one multi-writer primitive that is safe on one
# machine without a lock (mouth-spec.md §3.2). The lock below is for *threads* inside a single
# process — presence.py appends from its scheduler tick, its chat drainer, and its stdout-reader
# thread.
_append_lock = threading.Lock()

ORIGIN_ENV_VAR = "CLAUDE_CODE_SESSION_ID"

# --------------------------------------------------------------- the derived scope (phase 0)
#
# Bounds, because a row is appended on every landed send and an unbounded list on it is how a log
# stops being cheap. The counts below are generous against a typical turn (a handful of tool calls,
# a few dozen at the very top end), so truncation is the exception.
SCOPE_MAX_TOOLS = 24          # distinct tool NAMES; matches presence.INTERLEAVE_MAX_TOOLS
SCOPE_MAX_READS = 40          # distinct read TARGETS
SCOPE_TARGET_MAX_LEN = 400    # one target; a path or a URL, never a payload

# How deep into a tool input the harvest walks. An MCP tool nests its real arguments one level
# down (`notion-query-data-sources` puts everything under `data`) and `notion-search` nests its
# filters one level below that. 3 reaches both; it is a bound, not a search — see `_harvest`.
SCOPE_INPUT_MAX_DEPTH = 3

# THE ONE LIST TO EDIT when a tool starts naming its target under a different key. Every entry is a
# LOCATION — a file, a directory, a URL, or the id of one — because that is what a scope clause
# would name. Keys carrying a command, a prompt, a query or a body are STILL deliberately absent:
# they are not places, they are often enormous, and reading intent out of them is the
# interpretation this field refuses to do.
#
# The list covers every Notion argument shape and the plural forms. The honest answer to `Bash`'s
# `command` is not to parse a place out of it but to SAY that the call named none — that is
# `unnamed_calls` / :func:`reads_complete` below, the half that stops an empty `reads` from
# implying a complete one.
SCOPE_READ_TARGET_KEYS = (
    # Claude Code's own file tools.
    "file_path", "notebook_path", "path", "paths", "url", "urls",
    # Notion MCP. `data_source_urls` is a LIST of `collection://...` tables, and `id` is what
    # `notion-fetch` calls the page, database or data source it is fetching — a location by
    # another name. Without these, a turn that queried the right database would record no reads.
    "data_source_url", "data_source_urls", "page_url", "view_url", "id",
)


def default_state_dir() -> str:
    return DEFAULT_STATE_DIR


def assertions_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or DEFAULT_STATE_DIR, ASSERTIONS_FILE)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_stamp(value) -> datetime | None:
    """Tolerant read of an `at` field. Anything unparseable reads as None, and every caller treats
    that as "don't touch this row" — a garbled stamp must never make `prune` delete something."""
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


def _registry_source(state_dir: str, session_id: str) -> str | None:
    """The session registry's `source` for `session_id`, or None. **Enrichment only.**

    Deliberately deferred + guarded, exactly like `jobs._registry_entry`: `sentinel` imports *this*
    module at import time, so a module-level `import sentinel` here would be a cycle, and a registry
    problem must cost a field rather than an assertion. Note mouth-spec.md §9.4 — a warm turn reads
    `build`, not `daemon`, which is precisely why `speaker` exists alongside this."""
    if not session_id:
        return None
    try:
        import sentinel

        wanted = sentinel._session_id_for("", session_id)
        for entry in sentinel.load_sessions(state_dir):
            if entry.get("session_id") in (session_id, wanted):
                return entry.get("source") or None
    except Exception:  # noqa: BLE001 — enrichment; never worth an exception
        return None
    return None


def origin_hand(state_dir: str | None = None, speaker: str | None = None, env=None) -> dict:
    """Who is speaking — the `origin_hand` block of mouth-spec.md §3.3.

    `speaker` is the **call site's own label** for itself (`daemon` / `sentinel` / `watchdog` /
    `breakglass` / an archon's name) and is the field to trust: the process always knows what it is, whereas
    `source` comes from the session registry and reads `build` for the warm session (§9.4). Both are
    recorded — the honest one, and the one the spec named — and every field is optional.

    Fail-open: an exotic environment or an unreadable registry degrades to fewer fields, never an
    error."""
    hand: dict = {}
    if speaker:
        hand["speaker"] = speaker
    try:
        session_id = ((env if env is not None else os.environ).get(ORIGIN_ENV_VAR) or "").strip()
    except Exception:  # noqa: BLE001
        session_id = ""
    if session_id:
        hand["session_id"] = session_id
        try:
            source = _registry_source(state_dir or DEFAULT_STATE_DIR, session_id)
        except Exception:  # noqa: BLE001 — belt and braces: enrichment must not reach record_assertion
            source = None
        if source:
            hand["source"] = source
    try:
        hand["cwd"] = os.getcwd()
    except OSError:
        pass
    return hand


class TurnScope:
    """One chat turn's tool calls, accumulated as the turn streams — the raw material for the `scope`
    sub-object (see the module docstring).

    **Fed the RAW claude-CLI stream event**, above `cockpit_pipe.build_chat_event_from_stream`, for
    because the conversion keeps only a 200-character
    `input_preview` of each tool input, and a target parsed back out of a truncated JSON preview is a
    guess. The raw block carries the input as a dict, so the target is read, not reconstructed.

    **It stores what it extracted, never the input it extracted it from** — a `Write` input holds an
    entire file — so the accumulator's size is bounded by the caps above regardless of the turn.

    Threading: `observe` runs on the warm session's stdout-reader thread and `snapshot` on the event
    loop, so both do the smallest possible thing (an append, a copy) and neither takes a lock.

    **Observation may never cost the turn it observes**, so nothing here raises: a stream event in an
    unexpected shape costs whatever it would have contributed and nothing else."""

    def __init__(self) -> None:
        self.tool_calls = 0          # EVERY call, including repeats of one tool on one file
        self.truncated = False       # a cap bit; a reader of the lists must know a cap was hit
        self.unnamed_calls = 0       # calls that named NO place this field can record — see `reads_complete`
        self._tools: list = []       # distinct names, first-seen order
        self._reads: list = []       # distinct targets, first-seen order

    def observe(self, ev) -> None:
        """Count the tool calls in one stream event. Anything else is ignored."""
        try:
            if not isinstance(ev, dict) or ev.get("type") != "assistant":
                return
            blocks = (ev.get("message") or {}).get("content")
            if not isinstance(blocks, list):
                return
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    self._note(block.get("name"), block.get("input"))
        except Exception:  # noqa: BLE001 — see the docstring
            pass

    def _note(self, name, tool_input) -> None:
        self.tool_calls += 1
        if name:
            self._add(self._tools, str(name), SCOPE_MAX_TOOLS)
        # Whether the call NAMED a place is decided before the cap: a call that pointed at one
        # named it, whether or not there was room left to store it. Collapsing those two would
        # make a capped list look like a blind one, and they are different findings.
        if not self._harvest(tool_input, 0):
            self.unnamed_calls += 1

    def _harvest(self, node, depth: int) -> bool:
        """Pull every LOCATION out of one tool input; return whether this call named one at all.

        A bounded walk rather than a top-level scan, because an MCP tool's real arguments sit one
        or two levels down (`{"data": {"data_source_urls": [...]}}`) and a scan of the top level
        sees only the wrapper — which is exactly how a live Notion query contributed nothing.

        It stays MECHANICAL: it looks up known KEYS, it never searches a value for something that
        looks path-shaped, and it never descends past :data:`SCOPE_INPUT_MAX_DEPTH`. That is what
        keeps it on the right side of the line the class docstring draws — this field records
        what was consulted, and interprets nothing."""
        if depth > SCOPE_INPUT_MAX_DEPTH or not isinstance(node, (dict, list, tuple)):
            return False
        named = False
        if isinstance(node, (list, tuple)):
            for item in node:
                named |= self._harvest(item, depth + 1)
            return named
        for key, value in node.items():
            if key in SCOPE_READ_TARGET_KEYS:
                for text in (value if isinstance(value, (list, tuple)) else [value]):
                    if isinstance(text, str) and text.strip():
                        self._add(self._reads, text.strip()[:SCOPE_TARGET_MAX_LEN], SCOPE_MAX_READS)
                        named = True
            elif isinstance(value, (dict, list, tuple)):
                named |= self._harvest(value, depth + 1)
        return named

    def _add(self, bucket: list, value: str, cap: int) -> None:
        if value in bucket:
            return
        if len(bucket) >= cap:
            self.truncated = True
            return
        bucket.append(value)

    def snapshot(self) -> dict:
        """The `scope` sub-object as it will be written. Safe to call mid-turn and repeatedly."""
        return {"tool_calls": self.tool_calls, "tools": list(self._tools),
                "reads": list(self._reads), "truncated": bool(self.truncated),
                "unnamed_calls": self.unnamed_calls,
                "reads_complete": reads_complete(self.unnamed_calls, self.truncated)}


def reads_complete(unnamed_calls, truncated) -> bool:
    """**Is `reads` the whole set of places this turn touched?** One definition in one place, so
    the accumulator and :func:`normalise_scope` cannot drift apart on the only question that
    matters here.

    `True` only when **every** call named at least one place *and* no cap was hit. A turn that ran
    `Bash` once carries `unnamed_calls: 1`, and therefore a `reads` list that is — whatever else
    it holds — **not complete**. It now says so.

    **Why it exists.** `truncated` alone means *a cap was hit* and nothing else, so `truncated:
    false` reads — reasonably — as "this list is the whole look". That is wrong for any turn with a
    call that named no place (a `Bash` command, a search): the call contributes nothing to `reads`
    and the row would still assert completeness. That is not a smaller answer than the truth; it
    is a **wrong** one.

    **`truncated` keeps its old, narrow meaning and is deliberately NOT overloaded to carry this.**
    Widening it would make one bit mean two things and destroy the cap signal. The other option was
    a taxonomy of which tools "count as a look", which is exactly the interpretation this field
    refuses; counting the calls that named nothing needs no taxonomy at all.

    **Rows written without these fields are NOT migrated** — a reader must tolerate both shapes.
    An absent `reads_complete` means *unknown*, never `True`: defaulting it true would re-assert
    the very claim this field exists to stop."""
    try:
        return not bool(truncated) and int(unnamed_calls or 0) <= 0
    except (TypeError, ValueError):
        return False


def normalise_scope(scope) -> dict | None:
    """A `TurnScope`, or a dict shaped like one, as a clean `scope` sub-object — or None.

    Total and defensive on purpose: `record_assertion` calls it on a path where an exception would
    cost the whole row, and a scope that cannot be computed must cost the scope alone. Unknown keys
    are dropped rather than passed through, so the field's shape stays stable for readers."""
    try:
        if scope is None:
            return None
        if hasattr(scope, "snapshot"):
            scope = scope.snapshot()
        if not isinstance(scope, dict):
            return None
        try:
            calls = int(scope.get("tool_calls") or 0)
        except (TypeError, ValueError):
            calls = 0
        # `unnamed_calls` ABSENT is not the same as zero, and the difference is the whole point
        # of this field. A `TurnScope` always reports it; a hand-built dict from a caller that
        # predates the fix cannot, and answering 0 on its behalf would re-assert the exact claim
        # — "nothing went unnamed" — that the field exists to avoid. So it is carried when
        # measured and OMITTED when not, and `reads_complete` follows it.
        measured = isinstance(scope.get("unnamed_calls"), (int, float)) and not isinstance(
            scope.get("unnamed_calls"), bool)
        try:
            unnamed = max(0, int(scope.get("unnamed_calls") or 0))
        except (TypeError, ValueError):
            measured, unnamed = False, 0
        clean: dict = {"tool_calls": max(0, calls), "tools": [], "reads": [],
                       "truncated": bool(scope.get("truncated"))}
        if measured:
            clean["unnamed_calls"] = unnamed
        for key, cap in (("tools", SCOPE_MAX_TOOLS), ("reads", SCOPE_MAX_READS)):
            items = scope.get(key)
            if not isinstance(items, (list, tuple)):
                continue  # a bare string is iterable and would come apart into characters
            for item in items:
                if not isinstance(item, str) or not item.strip():
                    continue
                value = item.strip()[:SCOPE_TARGET_MAX_LEN]
                if value in clean[key]:
                    continue
                if len(clean[key]) >= cap:
                    clean["truncated"] = True
                    break
                clean[key].append(value)
        # DERIVED here, never copied from the input: a caller handing us a dict does not get to
        # assert a completeness this function can compute. The re-capping loop above may have
        # raised `truncated`, and this has to see that.
        if measured:
            clean["reads_complete"] = reads_complete(unnamed, clean["truncated"])
        return clean
    except Exception:  # noqa: BLE001 — the field, never the row
        return None


def _append_line(path: str, row: dict) -> None:
    """Append one JSON object as a line, under the module lock.

    Shared by the assertions log and the outbound queue because both are append-only jsonl for the
    same reason: many writers, and `control-queue.json`'s load→append→save is a lost-update race the
    moment there are two. This does NOT swallow errors — each caller decides, and both of them decide
    the same way (return False, never raise), which keeps that contract visible at the call site
    rather than buried here."""
    line = json.dumps(row, ensure_ascii=False) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with _append_lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)


def record_assertion(state_dir: str | None = None, *, surface: str, kind: str, text: str,
                     door: str = "direct", delivered: bool = True, speaker: str | None = None,
                     item_ids=None, superseded=None, reason: str | None = None,
                     origin: dict | None = None, now: datetime | None = None,
                     session_id: str | None = None, turn_id: str | None = None,
                     scope=None) -> bool:
    """Append one `seneschal.assertion/1` row — *what the owner actually received*, in the wording
    they received it in. Returns True iff the row hit disk.

    **This never raises.** Every caller is on a send path that has already succeeded, and a failed
    append must cost the row and nothing else (invariant 1 in the module docstring). Callers do not
    need a try/except around it, and should not add one.

    Call it only **after a landed send** — `delivered` defaults to True because that is what a phase-0
    row means. `item_ids` / `superseded` are always empty in phase 0 (there is no queue and no
    supersede); they are written so the file has one shape across phases.

    `turn_id` and `scope` are the turn-correlator pair (module docstring above),
    and they are **optional in the strong sense**: a caller that is not inside a chat turn passes
    neither, a malformed `turn_id` writes no field, and an uncomputable `scope` writes no field. None
    of those is an error, a warning, or a reason the row does not get written — they are built in
    their own `try` for exactly that reason, since a raise inside the outer one would cost the row.
    """
    try:
        state_dir = state_dir or DEFAULT_STATE_DIR
        row = {
            "schema": SCHEMA,
            "at": _stamp(now),
            "surface": str(surface or "unknown"),
            "door": str(door or "direct"),
            "kind": str(kind or "unknown"),
            "origin_hand": origin if isinstance(origin, dict) else origin_hand(state_dir, speaker),
            "text": text if isinstance(text, str) else str(text),
            "item_ids": list(item_ids or []),
            "superseded": list(superseded or []),
            "delivered": bool(delivered),
        }
        if reason:
            row["reason"] = str(reason)
        try:
            # Omitted, never null, when there is nothing honest to write — the rule
            # `presence._make_stream_tee` already applies to `session_id`: a null would claim we
            # measured "no turn" / "no look", which is a different statement from not having looked
            # at the question. A non-string id writes nothing rather than being coerced: a garbled
            # correlator that still LOOKS like one is worse than an absent one.
            if isinstance(turn_id, str) and turn_id.strip():
                row["turn_id"] = turn_id.strip()
            clean_scope = normalise_scope(scope)
            if clean_scope is not None:
                row["scope"] = clean_scope
        except Exception:  # noqa: BLE001 — belt and braces: these two fields may never cost the row
            pass
        line = json.dumps(row, ensure_ascii=False) + "\n"
        os.makedirs(state_dir, exist_ok=True)
        with _append_lock:
            with open(assertions_path(state_dir), "a", encoding="utf-8") as fh:
                fh.write(line)
        return True
    except Exception:  # noqa: BLE001 — see the docstring: the row is never worth the message
        return False


def read_assertions(state_dir: str | None = None, limit: int | None = None,
                    since: datetime | None = None) -> list:
    """Rows oldest-first, malformed lines skipped. `limit` keeps the **newest** N (a tail is what
    every reader of this log wants). Fail-open: an absent or unreadable file reads empty, which
    downgrades a hand to today's behaviour — speaking without the tail — rather than blocking it
    (mouth-spec.md §4.3)."""
    rows: list = []
    try:
        with open(assertions_path(state_dir), encoding="utf-8") as fh:
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


def prune(state_dir: str | None = None, days: int = RETENTION_DAYS,
          now: datetime | None = None) -> int:
    """Drop rows older than `days` and rewrite the log. Returns how many were dropped.

    Dream runs this (`SKILL.md` Dream step 2), alongside the presence / inbox / jobs sweeps. `days <=
    0` keeps everything. A row whose `at` is missing or unparseable is **kept** — this GC must never
    be the thing that loses a record of something the assistant said.

    Rewrite-in-place is safe here in a way it would not be for the *queue*: pruning happens once a
    night in Dream, not on a hot path, and a concurrent appender's line landing during the rewrite is
    the only loss window. Built-then-replaced so a crash mid-prune leaves the old file intact."""
    if days is None or days <= 0:
        return 0
    path = assertions_path(state_dir)
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
        if at is not None and at < cutoff:
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

# ======================================================================================
# Phase 1 — the queue and a dumb dispatcher (docs/mouth-spec.md §3.2, §3.4, §7)
# ======================================================================================
#
# **FIFO and staleness annotation ONLY.** No supersede, no digest — those are phase 2 and one of them
# needs the owner's eye on the wording. Reminders and calls pass straight through untouched (§3.5: one
# reminder per push nudge, always; a shaper that merged two would look like a feature and be a
# regression).
#
# **Two doors, one log.** Unsolicited speech queues here and the daemon's tick drains it. **Turn
# replies stay direct** and are not routed through this: `deliver_reply` is synchronous and its
# landed-boolean feeds the poison-pill guard, so queueing a reply would break both. Both doors append
# to `assertions.jsonl` — the queue is a scheduling change, never a change to what gets recorded.
#
# **Append-only jsonl, not a JSON list, deliberately.** `control-queue.json` is load→append→save,
# which is a lost-update race the moment there are two writers; the Mouth has many by definition. A
# single sub-4 kB line append is the one multi-writer primitive that is safe here without a lock.

OUTBOUND_SCHEMA = "seneschal.outbound/1"
OUTBOUND_FILE = "outbound.jsonl"

# Older than this and the delivered text is stamped with when the fact became true, rather than being
# presented as news. Ten minutes is the spec's default.
STALE_AFTER_SEC = 10 * 60

# Kinds the dispatcher may never reorder or hold. Reminders are here by standing policy, not by
# convenience.
PASSTHROUGH_KINDS = ("reminder", "call")


def outbound_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or default_state_dir(), OUTBOUND_FILE)


def enqueue(state_dir: str | None = None, *, surface: str, kind: str, text: str,
            observed_at=None, tone: str = "normal", pierce: bool = False,
            supersede_key: str | None = None, expires_at=None, speaker: str | None = None,
            now: datetime | None = None) -> dict | None:
    """Queue one unsolicited message. Returns the item, or None if it could not be written.

    **`observed_at` is when the FACT became true, not when the message was built** — for a job that is
    `ended_at`, not `notified_at`. This one field is what makes staleness detectable at all, and
    getting it wrong makes the whole feature a silent no-op (spec §3.2, §6). It defaults to *now*,
    which is right for something that just happened and wrong for anything replayed; a caller
    replaying a fact must pass it.

    Never raises — same contract as `record_assertion`. A queue that can throw is a queue that can
    cost a message, and the entire point of the Mouth is that nothing the assistant says goes missing."""
    try:
        now = now or datetime.now(timezone.utc)
        item = {
            "schema": OUTBOUND_SCHEMA,
            "id": f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}",
            "surface": surface,
            "kind": kind if kind in KINDS or kind == "call" else "unknown",
            "text": text,
            "origin_hand": origin_hand(state_dir, speaker=speaker),
            "observed_at": _stamp(observed_at) if observed_at is not None else _stamp(now),
            "queued_at": _stamp(now),
            "supersede_key": supersede_key,
            "tone": tone,
            "pierce": bool(pierce) or tone == "critical",
            "expires_at": _stamp(expires_at) if expires_at is not None else None,
            "state": "pending",
        }
        _append_line(outbound_path(state_dir), item)
        return item
    except Exception:  # noqa: BLE001 — the row, never the message
        return None


def read_outbound(state_dir: str | None = None) -> list:
    """Every parseable item, in file order. A corrupt line costs that line only."""
    out = []
    try:
        with open(outbound_path(state_dir), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(obj, dict) and obj.get("id"):
                    out.append(obj)
    except OSError:
        return []
    return out


def pending(state_dir: str | None = None) -> list:
    """Items not yet delivered or dropped, oldest first — the FIFO the dispatcher walks.

    Terminal state is recorded by APPENDING a `state` row for the same id rather than rewriting the
    file: an append-only log with many writers cannot be safely rewritten in place, and the last
    word for an id wins."""
    final = {}
    order = []
    for it in read_outbound(state_dir):
        if it["id"] not in final:
            order.append(it["id"])
        prev = final.get(it["id"], {})
        final[it["id"]] = {**prev, **it}
    return [final[i] for i in order if final[i].get("state") == "pending"]


def _mark(state_dir, item_id: str, state: str, reason: str = "") -> bool:
    try:
        row = {"schema": OUTBOUND_SCHEMA, "id": item_id, "state": state,
               "settled_at": _stamp(), }
        if reason:
            row["reason"] = reason
        _append_line(outbound_path(state_dir), row)
        return True
    except Exception:  # noqa: BLE001
        return False


def _owner_local(instant: datetime) -> datetime:
    """`instant` on the owner's wall clock. Fail-open to machine-local: an unreadable identity or
    zone must cost the precision of the stamp, never the message it prefixes."""
    try:
        import tz_common

        return tz_common.to_local(instant)
    except Exception:  # noqa: BLE001
        return instant.astimezone()


def staleness_prefix(item: dict, now: datetime | None = None,
                     stale_after_sec: int = STALE_AFTER_SEC) -> str:
    """`"(from 14:42) "` when the fact is older than the threshold, else `""`.

    An as-of stamp rather than a duration, because *"(from 14:42)"* is checkable against the owner's
    own memory of the day and *"(17 minutes ago)"* is not. Rendered in the owner's local time
    (`tz_common.to_local`, machine-local when unconfigured) for the same reason."""
    seen = _parse_stamp(item.get("observed_at"))
    if seen is None:
        return ""
    now = now or datetime.now(timezone.utc)
    if (now - seen).total_seconds() <= stale_after_sec:
        return ""
    return f"(from {_owner_local(seen).strftime('%H:%M')}) "


def drain(state_dir: str | None = None, *, send, now: datetime | None = None,
          stale_after_sec: int = STALE_AFTER_SEC, log=None) -> dict:
    """Deliver the pending queue, oldest first. `send(surface, text, item) -> bool`.

    **One clock, so the owner never gets B-then-A when A-then-B happened** — the single ordering
    guarantee phase 1 buys, and the reason producers stop each calling `send_telegram` directly.

    Semantics that are load-bearing:

    * A **failed send leaves the item pending.** At-least-once, retried on the next tick; the
      assertions log is what dedupes, because a row is only written on a landed send.
    * An **expired item is dropped and RECORDED** — `delivered: false` with a reason. Nothing vanishes
      unrecorded; that is the whole difference between a queue and a memory leak.
    * A **raising `send` is caught per item**, so one bad surface cannot stop the rest of the queue.
      Same posture as `jobs.reconcile`'s per-record try.
    """
    now = now or datetime.now(timezone.utc)
    sent = dropped = failed = 0
    for item in pending(state_dir):
        exp = _parse_stamp(item.get("expires_at"))
        if exp is not None and now > exp:
            _mark(state_dir, item["id"], "expired", "expires_at passed before the drain reached it")
            record_assertion(state_dir, surface=item.get("surface", "unknown"),
                             kind=item.get("kind", "unknown"), text=item.get("text", ""),
                             door="queue", delivered=False,
                             reason="expired before delivery")
            dropped += 1
            continue

        text = staleness_prefix(item, now, stale_after_sec) + (item.get("text") or "")
        try:
            landed = bool(send(item.get("surface"), text, item))
        except Exception as exc:  # noqa: BLE001 — one surface must not stop the queue
            landed = False
            if log:
                log(f"! mouth drain: {item.get('surface')} raised — {exc}")
        if landed:
            _mark(state_dir, item["id"], "sent")
            record_assertion(state_dir, surface=item.get("surface", "unknown"),
                             kind=item.get("kind", "unknown"), text=text,
                             door="queue", delivered=True)
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "dropped": dropped, "failed": failed}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="The Mouth: the assistant's assertions log.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="append one assertion (used by the PowerShell bypass mouth)")
    rec.add_argument("--surface", required=True, choices=list(SURFACES))
    rec.add_argument("--kind", default="unknown")
    rec.add_argument("--text", required=True)
    rec.add_argument("--door", default="direct", choices=list(DOORS))
    rec.add_argument("--speaker", default=None, help="the calling process's own label for itself")
    rec.add_argument("--not-delivered", action="store_true",
                     help="record a message that did NOT land (nothing vanishes unrecorded)")
    rec.add_argument("--reason", default=None)

    tail = sub.add_parser("tail", help="print the newest rows")
    tail.add_argument("--limit", type=int, default=20)

    pr = sub.add_parser("prune", help="drop rows older than --days (Dream runs this)")
    pr.add_argument("--days", type=int, default=RETENTION_DAYS)

    args = p.parse_args(argv)

    if args.cmd == "record":
        ok = record_assertion(args.state_dir, surface=args.surface, kind=args.kind, text=args.text,
                              door=args.door, delivered=not args.not_delivered,
                              speaker=args.speaker, reason=args.reason)
        print(json.dumps({"ok": ok}))
        return 0 if ok else 1
    if args.cmd == "tail":
        for row in read_assertions(args.state_dir, limit=args.limit):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    dropped = prune(args.state_dir, days=args.days)
    print(json.dumps({"ok": True, "dropped": dropped}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
