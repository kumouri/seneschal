#!/usr/bin/env python3
"""**The Watch suppression list** — topics the owner has told the assistant they never want the comms
peek to raise, checked in code on the Watch send path before a message leaves. Stdlib only; never
imports `telegram_*` or `presence`, so the Telegram send path can import it optionally.

**Why this exists.** A comms peek can be right every time it raises something and still be unwanted:
e.g. a registrar's *"your domain expired 30+ days ago and will be permanently deleted"* about a domain
the owner is **deliberately letting expire**. The peek has no way to know the owner *wants* that
outcome, so there is nothing to fix in the peek — and the narrow fix beats turning the peek off.
Nothing here disables, throttles or rate-limits the peek; it drops one message when that message names
a topic the owner has already decided about.

**IT IS THE SECOND QUESTION, NOT A REPLACEMENT FOR THE FIRST.** `reminders_acks
.watch_escalation_blocked` asks *"has the owner already acked the thing this peek is chasing?"*; this
asks *"has the owner said they never want this topic?"* Two different questions about two different
kinds of fact — an event and a standing instruction — and neither can answer the other. Both are
consulted on every Watch send, either can block, and a change that collapses them into one gate has
deleted a real distinction.

**THE LIST IS OWNER DATA, KEPT BESIDE ITS TRACKED EXAMPLE.** The live list is
`../references/watch-suppressions.json` — gitignored, because its entries name the owner's own domains,
vendors and accounts. The tracked `../references/watch-suppressions.example.json` documents the shape
(and ships with no patterns). :func:`default_list_path` reads the live file when it exists and falls
back to the example otherwise, so a fresh install suppresses nothing. It lives under `references/`,
not `state/`, because a suppression is a standing instruction the owner curates by hand, not runtime
state a process rewrites.

**FAIL-OPEN, ABSOLUTELY.** A missing file, an unreadable one, a malformed one, a pattern that is not
a string, a raise anywhere: **suppresses nothing**. Only a *positive* reading of a well-formed entry
against the message text can block, which is `reminders_acks`' doctrine pointed at a second gate —
and the direction of error is the whole argument. A message that slips through costs one nudge the
owner did not need; a gate that fires because a file was half-written costs a message the owner never
learns was withheld.

**MATCHING IS DELIBERATELY DUMB** — a contiguous, case-insensitive substring test, nothing else. No
regex, no globs, no anchors, no token scoring: a pattern is literal text and a metacharacter in one
is just that character. `reminders_acks`' title matcher is the counter-example that licenses this:
it needed coverage scores and a stopword list because it is *inferring* which row a sentence is
about, whereas here the owner has named the string themselves. Where a pattern would be ambiguous,
the file's own rule is to **under-match**.

**🚨 Critical / 🛑 Super-Critical / `Call Me` ARE NOT SUPPRESSIBLE, BY ANY PATTERN, ON ANY PATH.**
They are already structurally out of reach — they fire from `sentinel.check_reminders`, a different
send door that never comes through this one, and **nothing here may ever be "extended" into the
reminder path** — but structural truth that rests on today's call graph is one refactor from being
false, so :data:`NEVER_SUPPRESS` refuses them again here and `test_watch_suppress.py` pins both
halves. Over-refusing is the safe direction: the cost is a nudge the owner did not need.

**EVERY VERDICT IS STILL LOGGED** to `state/watch-gate.jsonl` by the caller, blocked and allowed
alike, with the pattern that matched named in the row. A silent drop would re-create the "nothing
recorded that the peek sent at all" defect in the opposite direction. A suppressed message is **more**
visible in the log than a delivered one, never less.

Usable as a CLI for a quick check:

  python watch_suppress.py --text "Registrar alert: old-shop.example expired…"
  python watch_suppress.py --list
"""
from __future__ import annotations

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REFERENCES_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "references"))

#: The owner's live list — gitignored; see the module docstring.
LIVE_LIST_PATH = os.path.join(REFERENCES_DIR, "watch-suppressions.json")
#: The tracked example — the documented shape, shipping with an empty `patterns` list.
EXAMPLE_LIST_PATH = os.path.join(REFERENCES_DIR, "watch-suppressions.example.json")

#: The `reason` a verdict from this gate carries, so a reader of `watch-gate.jsonl` can tell a
#: standing suppression apart from the ack gate's `reminder_acked_today` at a glance.
SUPPRESSED_REASON = "watch_suppression_list"

#: Shorter than this and a pattern identifies nothing — `"ok"` or `"of"` would match nearly every
#: message the assistant sends. A pattern under the floor is DROPPED rather than raising, so a typo in
#: the file costs that entry and never the send path.
MIN_PATTERN_LEN = 4

#: **The refusal that has no pattern.** A message carrying one of these is never suppressible,
#: whatever the list says. The first two are the ⏰ `Importance` markers; the third is the `Call Me`
#: channel. Matched case-insensitively as plain substrings, and over-refusing is the intended
#: direction of error — see the module docstring.
NEVER_SUPPRESS = ("🚨", "🛑", "call me")


def default_list_path() -> str:
    """The live list if it exists, else the tracked example. Resolved per call (not at import), so a
    list the owner creates while the daemon runs is honoured on the very next send."""
    return LIVE_LIST_PATH if os.path.exists(LIVE_LIST_PATH) else EXAMPLE_LIST_PATH


def load_patterns(path: str | None = None) -> list:
    """The list's usable entries: ``[{"pattern", "reason", "added"}, …]``.

    Total and fail-open in the strong sense — a missing file, unreadable bytes, invalid JSON, a
    top-level shape that is not an object, a `patterns` value that is not a list, an entry that is
    not an object, a non-string or too-short pattern: each contributes **nothing** rather than
    raising. An empty list suppresses nothing, which is the same outcome as no file at all."""
    try:
        with open(path or default_list_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — an unreadable list suppresses nothing
        return []
    if not isinstance(data, dict):
        return []
    rows = data.get("patterns")
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pattern = row.get("pattern")
        if not isinstance(pattern, str):
            continue
        pattern = pattern.strip()
        if len(pattern) < MIN_PATTERN_LEN:
            continue
        out.append({"pattern": pattern,
                    "reason": row.get("reason") if isinstance(row.get("reason"), str) else None,
                    "added": row.get("added") if isinstance(row.get("added"), str) else None})
    return out


def never_suppressible(text) -> str | None:
    """The marker that makes ``text`` unsuppressible, or ``None``. See :data:`NEVER_SUPPRESS`.

    A non-string is treated as unsuppressible (``""`` is returned as the marker) — there is nothing
    to match a pattern against, and the fail-open direction here is *send*."""
    if not isinstance(text, str):
        return ""
    low = text.lower()
    for marker in NEVER_SUPPRESS:
        if marker.lower() in low:
            return marker
    return None


def match(text, path: str | None = None) -> dict | None:
    """**The gate.** ``None`` = send it. A dict = this message names a topic the owner has decided
    about::

        {"reason": SUPPRESSED_REASON, "pattern": str, "why": str|None, "added": str|None}

    The FIRST entry that matches wins — the list is small, hand-maintained and ordered as written,
    and picking a "best" match would need a scoring rule this gate deliberately does not have.

    Never raises: every failure path answers ``None``."""
    try:
        if never_suppressible(text) is not None:
            return None
        low = text.lower()
        for row in load_patterns(path):
            if row["pattern"].lower() in low:
                return {"reason": SUPPRESSED_REASON, "pattern": row["pattern"],
                        "why": row["reason"], "added": row["added"]}
        return None
    except Exception:  # noqa: BLE001 — fail-open: this gate may never be the reason the owner wasn't told
        return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Check a message against the Watch suppression list.")
    p.add_argument("--text", help="the message text to test")
    p.add_argument("--list", action="store_true", dest="show", help="print the usable entries")
    p.add_argument("--path", default=None,
                   help="an alternate list file (default: the live list, else the tracked example)")
    args = p.parse_args(argv)
    if args.show:
        print(json.dumps({"path": args.path or default_list_path(),
                          "patterns": load_patterns(args.path)}, ensure_ascii=False, indent=2))
        return 0
    if not args.text:
        print(json.dumps({"ok": False, "error": "no --text provided"}))
        return 2
    verdict = match(args.text, args.path)
    print(json.dumps({"ok": True, "suppressed": bool(verdict), "verdict": verdict},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
