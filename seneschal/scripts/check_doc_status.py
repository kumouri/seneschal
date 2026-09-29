#!/usr/bin/env python3
"""Every document in the design record declares a machine-readable build status. Stdlib only. Built
in the shape of `check_context_pointers.py`, deliberately.

## What this is for

A design record answers "what has and hasn't been built?" only if every document says so in a form
something can read. A free-text status vocabulary drifts the moment it is hand-maintained: left alone,
one directory collects `P1 + P2 BUILT`, `PHASES 0, 1 and 3 BUILT`, `ANALYSIS ONLY`, `approved
direction`, `research memo, not a plan of record` — every one a real claim, none of them derivable into
a ledger. So the work is **normalisation plus a gate**, and this module is the gate. Nothing downstream
keeps its own copy of the ledger: everything reads :func:`scan`.

## The vocabulary, and why it is exactly five values

  ``BUILT``       a plan of record, and everything it intends to build exists. **Work deferred BY A
                  DECISION still counts as BUILT** — a deliberately deferred item is not backlog.
  ``PARTIAL``     a plan of record, some parts built. **The qualifier is REQUIRED** and names which,
                  in the document's own vocabulary. This is where a free-text vocabulary loses the
                  most: `PHASE n BUILT` cannot say *which* n, and different documents number their
                  parts different ways (`phases`, `P1`, `T1`, `steps`, `§`-numbered rules).
  ``SPEC-ONLY``   a plan of record, nothing built.
  ``MEMO``        **not** a plan of record — research, a measurement, a census, an investigation, a
                  threat model. Complete when written; there is nothing to build.
  ``REFERENCE``   a living document describing current state, never built or unbuilt — a router, an
                  inventory, a how-it-works page.

**No `SUPERSEDED` token, deliberately.** Supersession in practice is **section**-level — one section
is replaced while the rest of the document stands — and a whole-document token would be a false
statement about both. The qualifier carries it instead. Adding a value nothing uses is how a taxonomy
starts describing itself rather than the tree.

## The qualifier, and why one grammar beats more tokens

`TOKEN(free text)`. Required on `PARTIAL`, optional on everything else. Every tempting extra token —
out-of-tree, host-side-pending, prose-shipped, gated-vs-optional — is a *reason*, not a *state*. A
reader asking "is there work here?" is answered by the token; a reader asking "why is it like that?"
is answered by the qualifier and by the prose that follows it, which this check never touches.

**The token must be BACKTICKED.** Documents open paragraphs with the word BUILT in ordinary prose,
and a bare-word scan would read that as a status claim. Requiring the backtick makes the declaration
syntactically distinct from a sentence that merely uses the word.

## Enforcing, because every document passes

`--enforce` blocks. It is safe to enforce only because the normalisation landed with the gate and
every document passes: a gate that goes red on the day it lands is a gate someone turns off.

## What it deliberately does NOT check

- **Whether the status is TRUE.** No parser can tell `PARTIAL(phases 0, 1)` from `PARTIAL(phase 0)`;
  that takes reading documents against the tree. This checks that the claim is *made*, in a form
  something can read. The honesty is a review question and stays one.
- **The prose after the token.** It is the document's own voice and carries the nuance no token can.

USAGE:
  python check_doc_status.py             # report + a summary table, exit 0
  python check_doc_status.py --enforce   # exit 1 on any malformed or missing status
  python check_doc_status.py --json      # the derived ledger
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))

#: The directory this governs. One directory, deliberately: `seneschal/references/` holds baked-in
#: mappings that are never "built", and `seneschal/state/README.md` documents a runtime cache. Widening
#: this without widening the vocabulary would force a status onto documents the vocabulary cannot
#: describe, which is the failure this module was written to end.
DOCS_DIR = "seneschal/docs"

#: The five values. Ordered from most-done to least, because the summary table and the cockpit
#: ledger both read in that order and a second ordering somewhere else is a second source of truth.
TOKENS = ("BUILT", "PARTIAL", "SPEC-ONLY", "MEMO", "REFERENCE")

#: `PARTIAL` without a qualifier is the old `PHASE n BUILT` bug wearing a new name: it says some of
#: it shipped and refuses to say which. So it is the one token where the qualifier is mandatory.
QUALIFIER_REQUIRED = ("PARTIAL",)

#: `**Status:** `TOKEN` — the rest, untouched`. The backticks are what make the token findable by a
#: reader and unambiguous to a parser: a document whose PROSE mentions the word BUILT does not
#: accidentally declare itself built, which several of them do in the first sentence.
#: The token capture allows DIGITS so that a near-miss like `PHASE-0-BUILT` is diagnosed as an
#: unknown token rather than as a malformed line. Both are findings; only one tells you what to do.
STATUS_RE = re.compile(
    r"^\*\*Status:\*\*\s*`(?P<token>[A-Z][A-Z0-9-]*)(?:\((?P<qualifier>[^`]*)\))?`(?P<rest>.*)$",
    re.MULTILINE)

#: Any line that *looks* like a status declaration, so "malformed" and "absent" stay distinct
#: findings. They have different fixes and lumping them together hides the second one.
#:
#: Loose on purpose, because the pre-normalisation directory declared a status in at least four
#: spellings — `**Status:** X`, `**Status: X**` (the bold spanning the status TEXT), `> **Status
#: (date): X**` inside a blockquote, and `- **Status:** X` as a list item. A recogniser that only
#: knew the canonical one would report all the others as *missing*, which sends the fix the wrong
#: way: they are declarations, just unreadable ones.
LOOSE_STATUS_RE = re.compile(r"^\s*(?:[>*\-+]\s*)*\*{0,2}Status\b\s*[:(]",
                             re.MULTILINE | re.IGNORECASE)

#: How far into a file the status may be. It is a header field; one found in §9 is not a header and
#: would not be seen by a reader skimming the top, which is the only place a status is any use.
HEADER_SCAN_LINES = 40


class Finding:
    __slots__ = ("path", "kind", "detail")

    def __init__(self, path: str, kind: str, detail: str):
        self.path, self.kind, self.detail = path, kind, detail

    def as_dict(self) -> dict:
        return {"path": self.path, "kind": self.kind, "detail": self.detail}


def documents(root: str | None = None) -> list:
    """Every markdown file in the governed directory, repo-relative, sorted.

    Read off the filesystem rather than off a tracked manifest, because a manifest is a second thing
    to keep in step and the whole point of this module is that there is one."""
    root = root or REPO_ROOT
    base = os.path.join(root, DOCS_DIR)
    if not os.path.isdir(base):
        return []
    return sorted(DOCS_DIR + "/" + n for n in os.listdir(base) if n.endswith(".md"))


def parse_status(text: str):
    """`(record, finding_kind)` for one document's text.

    `record` is `{"token", "qualifier"}` when the header carries a well-formed status, else None. The
    second element names why it is None: `missing` (no status-shaped line at all), `malformed` (a
    status line the grammar rejects), `unknown-token`, `qualifier-required`, or `too-deep`."""
    head = "\n".join((text or "").replace("\r\n", "\n").split("\n")[:HEADER_SCAN_LINES])
    m = STATUS_RE.search(head)
    if not m:
        if LOOSE_STATUS_RE.search(head):
            return None, "malformed"
        # A well-formed status further down is a different diagnosis from none at all, and saying
        # "missing" about a status that is merely in the wrong place sends the fix the wrong way.
        if STATUS_RE.search((text or "").replace("\r\n", "\n")):
            return None, "too-deep"
        return None, "missing"
    token = m.group("token")
    qualifier = (m.group("qualifier") or "").strip() or None
    if token not in TOKENS:
        return None, "unknown-token"
    if token in QUALIFIER_REQUIRED and not qualifier:
        return None, "qualifier-required"
    return {"token": token, "qualifier": qualifier}, None


def _summary_line(text: str, record: dict) -> str:
    """The prose immediately after the token, trimmed for a one-line table. Never rewritten."""
    head = "\n".join((text or "").replace("\r\n", "\n").split("\n")[:HEADER_SCAN_LINES])
    m = STATUS_RE.search(head)
    rest = (m.group("rest") if m else "").strip()
    rest = re.sub(r"^[—\-–:·]\s*", "", rest)
    rest = re.sub(r"\*\*|`|\*", "", rest)
    return re.sub(r"\s+", " ", rest).strip()


def scan(root: str | None = None) -> dict:
    """**The one reader.** Everything downstream — the CLI, CI, and the cockpit ledger — calls this.

    Returns `{"root", "documents": [...], "findings": [...], "counts": {token: n}}`. A document that
    fails to parse appears in `findings` and in `documents` with `token: None`, so a consumer
    rendering the ledger shows it as unclassified rather than dropping it. **Silently omitting a
    document nobody could classify is how an inventory reports itself complete while missing things**
    — the failure this whole exercise exists to end."""
    root = root or REPO_ROOT
    docs, findings = [], []
    counts = {t: 0 for t in TOKENS}
    for rel in documents(root):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            findings.append(Finding(rel, "unreadable", str(exc)))
            docs.append({"path": rel, "token": None, "qualifier": None, "summary": ""})
            continue
        record, why = parse_status(text)
        if record is None:
            findings.append(Finding(rel, why, _finding_detail(why)))
            docs.append({"path": rel, "token": None, "qualifier": None, "summary": ""})
            continue
        counts[record["token"]] += 1
        docs.append({"path": rel, "token": record["token"], "qualifier": record["qualifier"],
                     "summary": _summary_line(text, record)})
    return {"root": root, "documents": docs, "findings": [f.as_dict() for f in findings],
            "counts": counts}


def _finding_detail(kind: str) -> str:
    return {
        "missing": "no `**Status:** `TOKEN`` line in the first %d lines" % HEADER_SCAN_LINES,
        "malformed": "a Status line that this grammar cannot read — the token must be backticked, "
                     "e.g. **Status:** `PARTIAL(phases 0, 1)` — the rest of the line is yours",
        "too-deep": "the Status line is below the header. It is a header field; one in §9 is not "
                    "seen by anyone skimming the top",
        "unknown-token": "token is not one of: " + ", ".join(TOKENS),
        "qualifier-required": "PARTIAL must say WHICH parts, in the document's own vocabulary — "
                              "e.g. `PARTIAL(phases 0, 1)`, `PARTIAL(T1, T2)`, `PARTIAL(§1.4 only)`. "
                              "A bare PARTIAL is the old `PHASE n BUILT` bug wearing a new name",
    }.get(kind, kind)


def render(result: dict, out=None) -> None:
    out = out or sys.stdout
    docs, findings = result["documents"], result["findings"]
    out.write("doc-status: %d document(s) in %s\n" % (len(docs), DOCS_DIR))
    out.write("  " + " · ".join("%s %d" % (t, result["counts"][t]) for t in TOKENS) + "\n")
    if findings:
        out.write("\n")
        for f in findings:
            out.write("%s\n    %s: %s\n" % (f["path"], f["kind"], f["detail"]))
    else:
        out.write("  every document declares a readable status.\n")


def render_ledger(result: dict, out=None) -> None:
    """The open-spec ledger, on a terminal. The cockpit renders the same derivation from `--json`;
    this exists so the answer is available without a browser, and so the two cannot disagree about
    what the data says."""
    out = out or sys.stdout
    for token in TOKENS:
        rows = [d for d in result["documents"] if d["token"] == token]
        if not rows:
            continue
        out.write("\n%s (%d)\n" % (token, len(rows)))
        for d in rows:
            name = d["path"].rsplit("/", 1)[-1]
            qual = "  [%s]" % d["qualifier"] if d["qualifier"] else ""
            out.write("  %-46s%s\n" % (name, qual))
    unclassified = [d for d in result["documents"] if d["token"] is None]
    if unclassified:
        out.write("\nUNCLASSIFIED (%d)\n" % len(unclassified))
        for d in unclassified:
            out.write("  %s\n" % d["path"].rsplit("/", 1)[-1])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", default=REPO_ROOT)
    p.add_argument("--enforce", action="store_true", help="exit 1 on any finding")
    p.add_argument("--json", action="store_true", help="the derived ledger, for the cockpit")
    p.add_argument("--ledger", action="store_true", help="group the documents by status")
    args = p.parse_args(argv)

    result = scan(args.root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        render(result)
        if args.ledger:
            render_ledger(result)
    if result["findings"] and args.enforce:
        return 1
    if result["findings"]:
        print("\n  [report-only] %d finding(s), exit 0. --enforce blocks."
              % len(result["findings"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
