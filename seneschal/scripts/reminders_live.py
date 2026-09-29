#!/usr/bin/env python3
"""**The live reminder row list** — the one thing the id cache structurally cannot answer.

    python reminders_live.py --json

``ack.py``'s stage 1 resolves the owner's words through a **tracked** alias table
(``references/reminder-aliases.json``) into ``state/reminders-id-cache.md``. That is exactly right for
**habit** rows — *"watered the plants"*, *"did my stretches"* are stable vocabulary that recurs for
months. It is exactly wrong for **todo** rows: a todo exists for a few days and then retires, so
hand-authoring an alias for one costs more than the hand lookup it would replace and is permanently
behind. This module is the other half: **it fetches the titles of the reminder rows that are active
right now**, so ``ack.py``'s stage 2 can match a freshly worded todo with nobody maintaining vocabulary.

## Notion backend only — a clean degrade everywhere else

The read is a **Notion-backend** component, gated like the write-behind outbox
(``store/notion/mapping.md``): :func:`store_backend` reads ``seneschal/store/config.json``'s ``active``
key (a legacy ``scripts/notion-mcp.json`` with no config means a pre-``/setup-store`` Notion install),
and on any other backend — ``obsidian``, ``markdown``, or no store at all — :func:`fetch_active_rows`
returns ``(None, reason)`` without spawning anything. That is a **refusal**, not an empty answer, so
``ack.py`` refuses the phrase and says why. The reason for not reading a filesystem store here instead:
on those backends the record identity is the note's path (``store/<backend>/schema.md`` → ``ref``), not
the page id the id cache, the ack ledger and the outbox key on, so a filesystem row matched here would
hand ``ack.py`` an identity the rest of its path does not speak. Until that is designed end to end,
stage 2 on a filesystem backend is simply unavailable and a stage-1 miss refuses.

## Why this cannot read the id cache

``state/reminders-id-cache.md`` is a **page-id** cache. It carries no ``Status`` column, so it cannot
tell an active row from a ``Finished`` one — and matching a retired row would resurrect a dead todo,
which is the specific harm stage 2 has to avoid. It is also only as fresh as its last reconcile. A
cache that is both possibly stale and structurally unable to answer the question is not a fallback; it
is a wrong answer with a fast path. So stage 2 reads **live or not at all**, and an unavailable lookup
is a **refusal**, never a guess off old data.

## …and why it does not write the id cache back

"Refresh the cache as a side effect so the next ack is fast" is **refused**. This module sees three
columns (title, page id, Status); the live cache carries six, and
:func:`reminders_acks.id_cache_titles` reads the **``Cadence``** one to set ``multi_fire`` — the
mirror of a queue entry's ``ack_gate: false`` that keeps the Watch-peek gate from suppressing a
multi-fire roll. Rewriting the file from here would drop that column and silently widen that gate.
Nightly Dream owns reconciliation; this is a read.

## The model is a data pipe, not a resolver

The daemon has no Notion path of its own (its only one is the Notion MCP, reachable from the
``claude`` CLI), so the live read is a **one-shot ``claude -p``** with the Notion MCP config threaded
in — subscription-billed, ``ANTHROPIC_API_KEY`` scrubbed via ``jobs.child_env``, and spawned with
``CREATE_NO_WINDOW`` so a console-less daemon never pops a window. What it is asked for is **one
query, copied out verbatim**: it does not filter, rank, interpret, or choose. Every decision that
could put an ack on the wrong row — which statuses count as active, which titles match the phrase,
whether a near-tie is a pick or a refusal — is made in Python, in ``ack.py``, deterministically and
under test. The Reminders data-source id is **baked in** from the rendered
``store/notion/schema.md`` (``/setup-store`` writes it), never discovered at runtime.

:data:`ACTIVE_STATUSES` is an **allow-list, not a deny-list**, and that direction is load-bearing: an
unrecognised or garbled ``Status`` must read as *not active* (so a retired row can never come back),
where a ``!= "Finished"`` test would read it as active and do the opposite.

``(None, err)`` is not the same value as ``([], None)``: no lookup is not no rows, and a caller that
collapsed the two would read "the live read failed" as "there are no active reminders". The memo that
keeps this to at most one lookup per ``ack.py`` run lives in :mod:`ack`.

``runner`` is ``subprocess.run``-compatible and injectable everywhere, so the tests spawn nothing and
spend nothing. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import jobs  # noqa: E402 — child_env(): the API key is scrubbed, so this stays subscription-billed
import reminders_acks as ra  # noqa: E402 — norm_key + the 32-hex page-id shape, one definition
from job_analysis import extract_json  # noqa: E402 — REUSED: one tolerant JSON reader in this tree

REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))

#: The pluggable store's config and the pre-``/setup-store`` legacy Notion MCP config — the same two
#: sources ``presence.store_backend_active`` reads. Module constants so a test can repoint them.
STORE_CONFIG = os.path.join(REPO_ROOT, "seneschal", "store", "config.json")
LEGACY_NOTION_MCP = os.path.join(SCRIPT_DIR, "notion-mcp.json")

#: The rendered (gitignored) Notion domain map ``/setup-store`` writes from ``schema.template.md``;
#: the Reminders data-source id is read out of its ``### reminders — `collection://…``` heading.
NOTION_SCHEMA = os.path.join(REPO_ROOT, "seneschal", "store", "notion", "schema.md")
_REMINDERS_HEADING_RE = re.compile(r"^#+\s*reminders\b.*?`(collection://[0-9a-fA-F-]{32,36})`",
                                   re.IGNORECASE | re.MULTILINE)

#: The reply shape. Required, not merely hoped for: a model that answered in prose with a stray
#: object in it must be an ERROR, because the caller is about to write a record off these rows.
ROWS_SCHEMA = "seneschal.reminder-rows/1"

#: Deliberately short: this runs **inline inside a chat turn's shell call**, whose own ceiling is
#: typically 120 s. A lookup allowed to run to that ceiling gets the whole of ``ack.py`` killed at the
#: same instant, so the owner receives neither the ack nor the refusal — strictly worse than a
#: refusal. 90 s leaves room to print one.
DEFAULT_TIMEOUT_SEC = 90

#: Matches the daemon's default for every headless one-shot it spawns — a non-interactive child that
#: must call an MCP tool has nowhere to put a permission prompt. Narrowing it to ``--allowedTools
#: <the query tool>`` is refused: the tool name differs between the supported Notion MCP variants, so
#: a hardcoded name would silently disable stage 2 on one of them. Read-only is enforced by what the
#: prompt asks for and by this being the only thing done with the reply.
PERMISSION_MODE = "bypassPermissions"

#: No console window for the child on Windows (the daemon is often console-less); 0 elsewhere.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: **An allow-list.** Every ``Status`` value that means "this row is still a live reminder"
#: (``store/markdown/schema.md`` → reminders, canonical keys). ``finished`` (retired) and ``paused``
#: (manual off-switch, never fires) are absent **and so is anything unrecognised**.
ACTIVE_STATUSES = frozenset({"pending", "reminded", "done", "skipped", "snoozed"})


def fetch_prompt(collection: str) -> str:
    """The one-shot's instruction: one query against ``collection``, copied out verbatim."""
    return (
        "Mechanical data fetch. This is not a conversation: no persona, no chat surface, no message "
        "to the owner, no judgment, and nothing written anywhere.\n"
        "1. Run exactly ONE `notion-query-data-sources` against the Reminders data source "
        f"`{collection}`, returning EVERY row with its `Reminder` title, its page id, and its "
        "`Status`.\n"
        "2. Print ONE JSON object and nothing else:\n"
        '   {"schema": "' + ROWS_SCHEMA + '", "rows": [{"title": "<the Reminder title, VERBATIM>", '
        '"page_id": "<the row\'s Notion page id>", "status": "<the Status value>"}]}\n'
        "Rules. Copy each title **verbatim** — do not tidy, shorten, re-case, translate or fix its "
        "punctuation; the caller matches on those exact words. Do NOT filter, sort, rank, summarise, "
        "interpret or choose among the rows, and do not leave any out: you are a data pipe and the "
        "caller does every bit of the deciding. Do not run a second query. Do not write to Notion."
    )


def _load_store_config() -> dict | None:
    try:
        with open(STORE_CONFIG, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) else None


def store_backend() -> str | None:
    """The active store backend's name, or ``None`` when no store is configured. Same sources and the
    same never-raises contract as ``presence.store_backend_active`` (not imported: a stdlib helper
    must not pull in the daemon)."""
    try:
        cfg = _load_store_config()
        if cfg is not None:
            active = cfg.get("active")
            return active if isinstance(active, str) and active else None
        return "notion" if os.path.exists(LEGACY_NOTION_MCP) else None
    except Exception:  # noqa: BLE001 — a gate read must never raise into an ack
        return None


def reminders_collection() -> str | None:
    """The Reminders ``collection://…`` id from the rendered ``store/notion/schema.md``, or ``None``
    when the Notion store has not been set up (or the heading can't be found). A shipped placeholder
    id (``00000000-0000-0000-0000-00000000000N`` — its first four groups all zero) counts as not set
    up: querying it could only fail."""
    try:
        with open(NOTION_SCHEMA, encoding="utf-8") as fh:
            m = _REMINDERS_HEADING_RE.search(fh.read())
    except OSError:
        return None
    if not m:
        return None
    ident = m.group(1)
    hex_id = ident.split("//", 1)[1].replace("-", "")
    return None if set(hex_id[:20]) <= {"0"} else ident


def default_mcp_configs() -> list:
    """The Notion MCP config to thread into the one-shot: the store config's
    ``backends.notion.mcp_config`` when that file exists, else the legacy ``scripts/notion-mcp.json``.

    ``[]`` is not a failure: a **user-scoped** Notion server is inherited by the spawned ``claude``
    with no config at all. Whether Notion is actually reachable is answered by the fetch failing."""
    cfg = _load_store_config() or {}
    backends = cfg.get("backends") if isinstance(cfg.get("backends"), dict) else {}
    notion = backends.get("notion") if isinstance(backends.get("notion"), dict) else {}
    path = notion.get("mcp_config")
    if isinstance(path, str) and path:
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(REPO_ROOT, path)
        if os.path.exists(path):
            return [os.path.normpath(path)]
    return [LEGACY_NOTION_MCP] if os.path.exists(LEGACY_NOTION_MCP) else []


def fetch_argv(claude_bin: str = "claude", mcp_configs=None, model: str | None = None,
               prompt: str | None = None, collection: str = "") -> list:
    """The one-shot's argv. A plain ``claude -p`` — subscription-billed, never the metered API."""
    argv = [claude_bin, "-p", prompt if prompt is not None else fetch_prompt(collection),
            "--permission-mode", PERMISSION_MODE]
    cfgs = list(mcp_configs) if mcp_configs is not None else default_mcp_configs()
    if cfgs:
        argv += ["--mcp-config", *cfgs]
    if model:
        argv += ["--model", model]
    return argv


def parse_rows(obj) -> list:
    """``[{"title", "key", "status"}, ...]`` from the reply object — **strict, and total**.

    ``key`` is the page id normalized by :func:`reminders_acks.norm_key` (32 hex, no dashes), the form
    every id comparison in this tree uses; the caller re-dashes it for what gets *written*.

    A row missing any of the three, or whose id is not a page id, is **dropped rather than repaired**.
    A dropped row can only cost a refusal; a repaired one could cost a wrong ack."""
    out, seen = [], set()
    rows = obj.get("rows") if isinstance(obj, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        title, status = row.get("title"), row.get("status")
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(status, str) or not status.strip():
            continue  # no status ⇒ cannot be shown to be ACTIVE ⇒ not a candidate
        key = ra.norm_key(row.get("page_id"))
        if not ra._HEX32_RE.match(key) or key in seen:
            continue
        seen.add(key)
        out.append({"title": title.strip(), "key": key, "status": status.strip()})
    return out


def active_rows(rows) -> list:
    """The subset of :func:`parse_rows` output that is still a live reminder — :data:`ACTIVE_STATUSES`,
    an allow-list, so an unknown status is excluded rather than assumed active."""
    return [r for r in rows if r["status"].casefold() in ACTIVE_STATUSES]


def unavailable_reason() -> str | None:
    """``None`` when the live lookup can run on this install, else the sentence saying why not —
    the backend gate and the baked-in collection id, checked before anything is spawned."""
    backend = store_backend()
    if backend != "notion":
        return (f"the live reminder lookup is Notion-backend only, and the active store backend is "
                f"{backend or 'unconfigured'} — resolve the phrase through "
                f"references/reminder-aliases.json instead")
    if reminders_collection() is None:
        return ("the Notion store is not set up for the live lookup (no Reminders collection id in "
                "store/notion/schema.md — run /setup-store)")
    return None


def fetch_active_rows(*, runner=None, claude_bin: str = "claude", mcp_configs=None,
                      model: str | None = None, timeout: int = DEFAULT_TIMEOUT_SEC) -> tuple:
    """``(active rows, None)`` or ``(None, "why not")``. **Never raises.**

    The two halves are deliberately distinguishable: an empty list means *the lookup worked and no
    active row is a candidate*, ``None`` means *there was no lookup* — including on a non-Notion
    backend, where nothing is spawned at all. The caller must refuse either way, but only the second
    is worth telling the owner to go fix."""
    reason = unavailable_reason()
    if reason:
        return None, reason
    runner = runner or subprocess.run
    argv = fetch_argv(claude_bin, mcp_configs, model, collection=reminders_collection())
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout,
                      env=jobs.child_env(), cwd=REPO_ROOT, creationflags=_NO_WINDOW)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return None, f"could not run {claude_bin!r}: {e}"
    reply = getattr(proc, "stdout", "") or ""
    obj = extract_json(reply)
    if obj is None:
        rc, err = getattr(proc, "returncode", "?"), (getattr(proc, "stderr", "") or "").strip()
        return None, (f"the reminder lookup returned no JSON object (exit {rc})"
                      + (f": {err[:200]}" if err else ""))
    if obj.get("schema") != ROWS_SCHEMA:
        return None, (f"the reminder lookup answered with schema {obj.get('schema')!r}, not "
                      f"{ROWS_SCHEMA!r} — refusing to match titles against a reply of unknown shape")
    return active_rows(parse_rows(obj)), None


def main(argv=None, runner=None) -> int:
    p = argparse.ArgumentParser(
        description="Print the reminder rows that are active right now (one live Notion read via a "
                    "`claude -p` one-shot; Notion backend only). What ack.py's title-matching stage 2 "
                    "sees.")
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--notion-mcp", action="append", metavar="PATH",
                   help="MCP config to thread in (repeatable). Default: the store config's "
                        "backends.notion.mcp_config, else scripts/notion-mcp.json, else whatever the "
                        "spawned claude inherits at user scope.")
    p.add_argument("--model", help="model for the one-shot; this is a copy-out, not a judgment")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC)
    p.add_argument("--json", action="store_true", help="print only the JSON line")
    args = p.parse_args(argv)

    rows, error = fetch_active_rows(runner=runner, claude_bin=args.claude_bin,
                                    mcp_configs=args.notion_mcp, model=args.model,
                                    timeout=args.timeout)
    if not args.json:
        for row in rows or []:
            print(f"{row['status']:<10} {row['title']}")
        if error:
            print(f"! {error}")
    print(json.dumps({"ok": error is None, "error": error, "count": len(rows or []),
                      "rows": rows or []}, ensure_ascii=False))
    return 0 if error is None else 3


if __name__ == "__main__":
    sys.exit(main())
