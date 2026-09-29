#!/usr/bin/env python3
"""**A `PostToolUse` hook: when a Notion query carried `LIMIT` with no `ORDER BY`, put one true
sentence next to the result.** Report-only by design. Standard library only.

**Notion backend only.** The query it reads is the Notion MCP's `notion-query-data-sources` SQL
mode; the Obsidian and Markdown backends have no such call. On any other active store backend
(`seneschal/store/config.json`'s `active`, the same sources `presence.store_backend_active` reads)
this hook is a strict no-op: it exits 0, prints nothing and writes nothing — see
:func:`store_backend`. With no store configured at all it is a no-op too, unless a legacy
`scripts/notion-mcp.json` marks a pre-`/setup-store` Notion install.

## The failure this exists for

A claim like *"that row never got written"* can be wrong even when the turn behind it looks
thorough: several tool calls, live queries against the correct data source, every instrument built
to catch a claim outrunning its look scoring it clean — and the scope sentence *"I queried your
database"* is **true**, which is exactly what makes the owner trust the claim over their own memory.
The wrong answer comes out of a right tool call against a right table. The defect is one query
*argument*:

    SELECT * FROM "collection://…" WHERE Name LIKE '%something%' LIMIT 4

`LIMIT` with no `ORDER BY`. Four arbitrary rows of however many matched, read as *the* rows — and
the natural next step, "so I'll write it", is a duplicate write.

## Why this shape and not a broader one

The obvious broader check — every figure in an outbound message must trace to a tool result in the
same turn — fires on a large share of turns, is mostly false positives, and still misses the
consequential cases. This is the opposite kind of thing, and that is the whole argument for it:

- It is a **syntactic property of the call**, decidable before the result comes back. No
  normalisation problem, no timezone problem, no 12h/24h problem.
- It has **no self-corroboration problem.** A check whose evidence window reaches past the current
  turn can be satisfied by the assistant's own earlier outbound text read back as a tool result.
  This one reads a single tool input.
- It fires rarely — a small fraction of Notion `SELECT`s — so the annotation stays meaningful.

## What it does, and the two things it must never do

It appends one line to the tool result via `hookSpecificOutput.additionalContext`:

> *this result was truncated without an order; absence from it is not evidence of absence.*

**It annotates. It does not block, and it cannot fail a query.** `PostToolUse` runs *after* the tool
has already returned, so there is nothing left to refuse even in principle; and this module never
uses `decision: "block"`, never exits non-zero, and writes nothing to stderr. The contract — it
cannot cost a reply, and the fail-open posture is untouched — is structural rather than promised.

**It puts the fact where the turn is already looking.** That is the same conclusion
`../docs/ack-system-ownership-spec.md` reaches independently: do not move the flush, move the fact.
The alternative — another line in a prompt-side file telling the assistant to write `ORDER BY` — is
one more written rule, and written rules of that kind are exactly what does not bind.

## Fail open, absolutely, on every path

Unreadable stdin, non-JSON, a missing key, a hostile shape, a raise anywhere: **exit 0, print
nothing.** A hook fires in every Claude Code session on the machine, and the worst thing this may
ever do is not annotate. Note the polarity is the reverse of `bash_path_guard`'s: there, silence is
the dangerous failure, so it blocks via exit 2 where a truncated stdout cannot half-write the signal.
Here silence is the *safe* failure, so the JSON-on-stdout mechanism is correct — a malformed object
fails schema validation and the action simply proceeds unannotated.

## Report-only, and what "report-only" means for something that already blocks nothing

Every fire is logged to `state/query-shape.jsonl` (:func:`log_fire`), because any later decision to
widen or flip this hook should be made on a measured firing rate, and a decision gated on data
nobody collected is a decision that never happens. `--report-only` logs the fire and suppresses the
annotation, for a period of measurement without any behaviour change at all; the default annotates.
**The log is written by this code path, not by a model choosing to write it** — code-written logs
are complete; model-written ones are not.

## Designed for generalisation, shipped for one case

:data:`DETECTORS` is the list. Two more entries are named there and **deliberately disabled**:
`head -N` on a grep, and Notion's default page size (a `view`-mode query returns 100 rows and says
nothing about it). Shipping all three at once would put three unmeasured firing rates behind one
gate; this ships the one that catches the known failure.

## Install

Host-side, the owner's to make — no PR can write `~/.claude/settings.json`. See
`QUERY_SHAPE_SETUP.md` (or `settings_merge.py --guard query-shape`), which includes the one command
that verifies the contract on the live harness rather than assuming it.

USAGE (the hook reads a `PostToolUse` event on stdin and takes no argv):
  echo '<event json>' | python query_shape_hook.py
  python query_shape_hook.py --explain "SELECT * FROM \\"collection://x\\" LIMIT 4"   # what would fire
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
#: The pluggable store's config and the pre-``/setup-store`` legacy Notion MCP config — the same two
#: sources ``presence.store_backend_active`` reads. Module constants so a test can repoint them.
STORE_CONFIG = os.path.join(REPO_ROOT, "seneschal", "store", "config.json")
LEGACY_NOTION_MCP = os.path.join(SCRIPT_DIR, "notion-mcp.json")
#: The only backend this hook has anything to say about.
NOTION_BACKEND = "notion"
#: One row per FIRE, never per query — a handful of lines a day at most.
LOG_FILE = "query-shape.jsonl"
#: Lets a test — and only a test — put the log somewhere that is not the live daemon's state dir.
STATE_DIR_ENV_VAR = "SENESCHAL_STATE_DIR"

_log_lock = threading.Lock()

#: The tool this fires on, matched by SUFFIX so a differently-aliased MCP server
#: (`mcp__notion__…` vs `mcp__notion-work__…`) is still recognised. The suffix is specific enough
#: that widening it this way cannot pull in an unrelated tool.
NOTION_QUERY_TOOL_SUFFIX = "notion-query-data-sources"

#: The one sentence, and then the two facts a reader needs to act on it. Nothing here tells the
#: assistant what to do about it — that would be one more written rule. It states what the result IS.
LIMIT_WITHOUT_ORDER_NOTE = (
    "[query shape] This result was truncated without an order; absence from it is not evidence of "
    "absence. The query carries LIMIT with no ORDER BY, so these are an arbitrary N of however many "
    "rows matched — not the first N, the newest N, or the only N."
)


# --------------------------------------------------------------------------- the query reader

def _strip_literals(sql: str) -> str:
    """Blank out single- and double-quoted runs, so a `LIMIT` inside one is not a `LIMIT`.

    Both matter here and for different reasons: Notion's table names are **double**-quoted
    (`FROM "collection://…"`) and its data is matched with **single**-quoted literals
    (`LIKE '%something%'`). A column literally called `ORDER BY` would otherwise suppress a real
    finding, and a row whose text contains the word LIMIT would otherwise invent one.

    Character-by-character rather than a regex, because the regex for "quoted run, honouring the
    doubled-quote escape, in two quote styles" is the kind that is wrong in a way nobody notices."""
    out, quote, i = [], None, 0
    while i < len(sql):
        ch = sql[i]
        if quote is None:
            if ch in ("'", '"'):
                quote, out = ch, out + [" "]
            else:
                out.append(ch)
        else:
            if ch == quote:
                # A doubled quote is an escaped quote, not the end of the run.
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    i += 1
                else:
                    quote = None
            out.append(" ")
        i += 1
    return "".join(out)


def _words(sql: str) -> list:
    """The statement as upper-case word tokens. `LIMIT4` and `MYLIMIT` are not `LIMIT`."""
    tokens, cur = [], []
    for ch in sql:
        if ch.isalnum() or ch == "_":
            cur.append(ch)
        elif cur:
            tokens.append("".join(cur).upper())
            cur = []
    if cur:
        tokens.append("".join(cur).upper())
    return tokens


def limit_without_order(sql) -> bool:
    """**The one predicate.** True when a SQL statement carries `LIMIT` and no `ORDER BY`.

    Deliberately conservative in the one direction that matters. An `ORDER BY` **anywhere** in the
    statement suppresses the finding, including one that only orders a subquery while the outer
    `LIMIT` stays unordered. That case is real and this misses it, on purpose: an annotation that
    fires where a reader can see an `ORDER BY` right there reads as a bug in the checker, and the
    number which kills a gate is its false-positive rate. A missed subquery case costs one un-annotated
    query; a wrong fire costs the mechanism."""
    if not isinstance(sql, str) or not sql.strip():
        return False
    words = _words(_strip_literals(sql))
    if "LIMIT" not in words:
        return False
    return not any(a == "ORDER" and b == "BY" for a, b in zip(words, words[1:]))


def notion_sql(tool_input):
    """The SQL string out of a `notion-query-data-sources` input, or None.

    The arguments live under `data`, and the tool has two modes: `sql` carries `query`, `view` carries
    a `view_url` and a `page_size` and no SQL at all. Only the first is this detector's business —
    the second is a disabled detector's (see :data:`DETECTORS`)."""
    if not isinstance(tool_input, dict):
        return None
    data = tool_input.get("data")
    if not isinstance(data, dict):
        # Tolerated rather than required: an un-nested input is not the shape the schema documents,
        # but reading it costs nothing and assuming the wrapper is how a hook silently stops firing.
        data = tool_input
    query = data.get("query")
    return query if isinstance(query, str) and query.strip() else None


# --------------------------------------------------------------------------- the backend gate

def _load_store_config() -> dict | None:
    try:
        with open(STORE_CONFIG, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) else None


def store_backend() -> str | None:
    """The active store backend's name, or ``None`` when no store is configured. Same sources and the
    same never-raises contract as ``presence.store_backend_active`` (not imported: a hook that runs
    after every tool call must not pull in the daemon). A parsed `store/config.json` answers with its
    `active` key; with no config, a legacy `scripts/notion-mcp.json` means a Notion install."""
    try:
        cfg = _load_store_config()
        if cfg is not None:
            active = cfg.get("active")
            return active if isinstance(active, str) and active else None
        return NOTION_BACKEND if os.path.exists(LEGACY_NOTION_MCP) else None
    except Exception:  # noqa: BLE001 — a gate read must never raise into a hook
        return None


def notion_backend_active() -> bool:
    """Is the Notion backend the active store? Anything else — another backend, no store, an
    unreadable config — is ``False``, and the hook then does nothing at all."""
    return store_backend() == NOTION_BACKEND


# --------------------------------------------------------------------------- the detectors

def _detect_notion_limit_without_order(tool_name: str, tool_input) -> str | None:
    if not tool_name.endswith(NOTION_QUERY_TOOL_SUFFIX):
        return None
    sql = notion_sql(tool_input)
    return LIMIT_WITHOUT_ORDER_NOTE if limit_without_order(sql) else None


#: THE LIST TO EXTEND. An entry is `(id, enabled, fn)`; `fn` takes the tool name and its input and
#: returns the line to inject, or None.
#:
#: The two disabled rows are named here rather than left to be rediscovered, because they are *the
#: same bug in other clothes* and the next person to meet one should find the seam already cut:
#:
#:   * ``grep_head_n`` — `head -N` on a grep. Same shape, same sentence, and an unmeasured firing rate.
#:   * ``notion_default_page_size`` — a `view`-mode query returns **100 rows and says nothing about
#:     it**, which is the silent version of the same truncation and probably the more common one.
#:
#: They ship disabled because several unmeasured firing rates behind one gate is how a gate gets
#: switched off in a week, and because one detector is enough to catch the known failure.
DETECTORS = (
    ("notion_limit_without_order", True, _detect_notion_limit_without_order),
    ("grep_head_n", False, None),
    ("notion_default_page_size", False, None),
)


def detect(tool_name, tool_input):
    """`(detector_id, line)` for the first enabled detector that fires, else `(None, None)`."""
    if not isinstance(tool_name, str) or not tool_name:
        return None, None
    for det_id, enabled, fn in DETECTORS:
        if not enabled or fn is None:
            continue
        line = fn(tool_name, tool_input)
        if line:
            return det_id, line
    return None, None


# --------------------------------------------------------------------------- the fire log

def log_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or os.environ.get(STATE_DIR_ENV_VAR) or DEFAULT_STATE_DIR, LOG_FILE)


def log_fire(detector: str, event: dict, annotated: bool, state_dir: str | None = None) -> bool:
    """One row per fire. Returns whether it landed; **no caller may care**, and none does.

    What it records is what phase 2 needs to decide the flip and nothing more: when, which detector,
    which tool, which session, and whether the line was actually injected. **It does not record the
    query.** A Notion SQL string carries the owner's column values — possibly health, finance or
    third-party text — and a fire log is not a place for that (`../references/comms-mapping.md`'s
    standing limit: private text stays out of anything replayed to a model). The `turn_id`-less join is good enough:
    the session and the timestamp locate the call in the transcript, which is where the query already
    is.

    Same append-under-a-lock primitive as `mouth._append_line`, and the same posture: a logging
    failure costs the row, never the annotation."""
    try:
        # Built INSIDE the try, deliberately. A hostile `event` reaching the row builder is exactly
        # the shape that would otherwise raise past this function and up into `decide` — where the
        # blanket catch would then cost the annotation, which is the one thing the log may not do.
        ev = event if isinstance(event, dict) else {}
        row = {"at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
               "detector": detector, "annotated": bool(annotated),
               "tool": str(ev.get("tool_name") or "")[:200],
               "session_id": str(ev.get("session_id") or "")[:100] or None}
        path = log_path(state_dir)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _log_lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return True
    except Exception:  # noqa: BLE001 — the row, never the annotation
        return False


# --------------------------------------------------------------------------- the hook

def build_output(line: str) -> dict:
    """The `PostToolUse` success payload. `additionalContext` is the ONLY field used.

    `decision: "block"` also exists on this event and is deliberately never emitted: it feeds a
    reason back as a correction and would make an annotation read as a failure. There is no code path
    in this module that can produce one."""
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": line}}


def decide(event, report_only: bool = False, state_dir: str | None = None):
    """One event -> the line to inject, or None. Logs the fire either way.

    **Notion backend only**: when the active store is anything else, this returns None and logs
    nothing (:func:`notion_backend_active`). The backend is read only once a detector has fired, so
    the overwhelming majority of tool calls — which match nothing — never touch the disk.

    Never raises on a hostile shape: an event this cannot understand is one it has no opinion
    about."""
    try:
        if not isinstance(event, dict):
            return None
        det_id, line = detect(event.get("tool_name"), event.get("tool_input"))
        if not line:
            return None
        if not notion_backend_active():
            return None
        log_fire(det_id, event, annotated=not report_only, state_dir=state_dir)
        return None if report_only else line
    except Exception:  # noqa: BLE001 — see the module docstring
        return None


def main(argv=None, stdin=None, stdout=None) -> int:
    """Entrypoint. **Always returns 0.** The only variation is whether anything is printed."""
    argv = list(sys.argv[1:] if argv is None else argv)
    out = stdout if stdout is not None else sys.stdout

    if argv and argv[0] == "--explain":
        # The install-time verification, and the only way to run this without a hook event. Prints
        # what WOULD be injected for a query, so `QUERY_SHAPE_SETUP.md`'s check is one command and
        # does not need the live harness to be already working. It judges the SQL alone and does not
        # consult the store backend; `notion_backend` reports that separately.
        sql = argv[1] if len(argv) > 1 else ""
        fires = limit_without_order(sql)
        out.write(json.dumps({"fires": fires,
                              "line": LIMIT_WITHOUT_ORDER_NOTE if fires else None,
                              "notion_backend": notion_backend_active()},
                             ensure_ascii=False) + "\n")
        return 0

    report_only = "--report-only" in argv
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        event = json.loads(raw) if raw and raw.strip() else None
    except Exception:  # noqa: BLE001 — fail open by contract
        return 0
    line = decide(event, report_only=report_only)
    if not line:
        return 0
    try:
        out.write(json.dumps(build_output(line), ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — an annotation we cannot print is simply not printed
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
