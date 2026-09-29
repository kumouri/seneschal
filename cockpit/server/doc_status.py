"""Read-only, tolerant reader for the cockpit's **open-spec ledger** — the build status of every
document in `seneschal/docs/`: what has and hasn't been completed, served to one panel.

## The ledger is DERIVED. There is no second copy, and that is the whole design.

A hand-maintained status vocabulary drifts — prose conventions like *BUILT* / *PHASE n BUILT* /
*SPEC ONLY* end up spelled a dozen different ways across a docs tree. **A hand-maintained ledger
drifts exactly the way that prose vocabulary does**, so nothing here keeps a list, a cache, or a copy:
every field is computed from the documents' own `**Status:**` headers by
`seneschal/scripts/check_doc_status.py`, which is the same parser CI enforces. If a header changes,
this changes. If nobody updates a header, CI goes red before this ever renders a stale row.

## Why this one IMPORTS the daemon-side module when `jobs.py` beside it deliberately does not

`cockpit/server/jobs.py` reimplements its reader on purpose: it parses **state files** that a daemon
on a *different commit* may have written, so a shared library would couple the backend's correctness
to the writer's version. **That argument does not apply here and its inverse does.** The parser and
the documents ship in the same commit — this backend is running out of the same checkout it is
reading — so there is no skew to tolerate, and a duplicated parser would be precisely the second
source of truth the ledger exists to abolish. Two parsers that disagreed about what `PARTIAL(phases
0, 1)` means would put the cockpit and CI at odds, and the cockpit would be the one nobody checked.

The import is guarded anyway: if `seneschal/scripts/` is not reachable — a stripped deployment, a
moved tree — the panel reports `available: false` and says why, the same posture as every other reader
here. It never raises, and it never guesses.

## What it deliberately does NOT do

- **It does not judge whether a status is TRUE.** No parser can tell `PARTIAL(phases 0, 1)` from
  `PARTIAL(phase 0)`; that takes reading the document against the tree. This serves the claim the
  document makes. The honesty is a review question and stays one.
- **It is read-only, like every v1 monitor panel.** There is no route to edit a status. A status is
  changed by editing the document that declares it, in the PR that changes the thing.
- **It does not drop what it cannot classify.** A document with no readable status is served with
  `token: null` and counted, because silently omitting it is how an inventory reports itself complete
  while missing things.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

#: `cockpit/server/doc_status.py` -> the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "seneschal" / "scripts"

#: Order the ledger renders in: most-done to least. The browser keeps no ordering of its own
#: (cockpit ruling 3) and takes this list as given.
DEFAULT_ORDER = ("BUILT", "PARTIAL", "SPEC-ONLY", "MEMO", "REFERENCE")

#: What each token means, in one line, served to the browser rather than restated there. A legend
#: hand-written in TSX is a third copy of the vocabulary and would be the first thing to go stale.
GLOSS = {
    "BUILT": "everything it intends to build exists; work deferred by a decision counts",
    "PARTIAL": "some parts built — the qualifier says which",
    "SPEC-ONLY": "a plan of record, nothing built",
    "MEMO": "not a plan of record — research, a measurement, a census. Complete when written",
    "REFERENCE": "a living document describing current state; never built or unbuilt",
}

#: The tokens that represent OPEN work. This is the answer to the question the ledger exists for
#: — *"what hasn't been completed"* — and it is computed here rather than in the browser so the panel
#: and any other consumer cannot disagree about it. MEMO and REFERENCE are not open work: a memo is
#: complete when written, and a router is never finished by construction.
OPEN_TOKENS = ("PARTIAL", "SPEC-ONLY")


def _unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason, "documents": [], "counts": {},
            "order": list(DEFAULT_ORDER), "gloss": dict(GLOSS), "open_tokens": list(OPEN_TOKENS),
            "open_count": 0, "unclassified_count": 0, "total": 0}


def _checker():
    """`seneschal/scripts/check_doc_status`, or None. Guarded and cached by `sys.modules`."""
    try:
        path = str(SCRIPTS_DIR)
        if path not in sys.path:
            sys.path.insert(0, path)
        import check_doc_status  # noqa: PLC0415 — deliberately lazy; see the module docstring

        return check_doc_status
    except Exception:  # noqa: BLE001 — a panel may never be the thing that 500s the cockpit
        return None


def read_status(root: Optional[Any] = None) -> dict:
    """The ledger payload. **Tolerant on every path**: a missing parser, an unreadable tree or an
    empty directory each degrade to an honest answer rather than an exception.

    Note the distinction `jobs.py` makes and this keeps: a *present but empty* docs directory is
    `available: true` with no documents, which is a different statement from "I cannot see the design
    record at all". They should not look the same to a reader."""
    checker = _checker()
    if checker is None:
        return _unavailable("seneschal/scripts/check_doc_status.py is not importable from this "
                            "deployment, so the ledger cannot be derived")
    try:
        result = checker.scan(str(root or REPO_ROOT))
    except Exception as exc:  # noqa: BLE001
        return _unavailable("the design record could not be read: %s" % exc)

    docs = []
    for d in result.get("documents") or []:
        path = str(d.get("path") or "")
        docs.append({
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "token": d.get("token"),
            "qualifier": d.get("qualifier"),
            "summary": d.get("summary") or "",
        })

    findings = {f.get("path"): f for f in (result.get("findings") or []) if isinstance(f, dict)}
    for doc in docs:
        f = findings.get(doc["path"])
        # A document nobody could classify carries WHY, so the panel can say "this needs a status"
        # rather than showing an unexplained blank. CI is already red at this point, by construction.
        doc["finding"] = (f.get("kind") if f else None)

    counts = dict(result.get("counts") or {})
    order = [t for t in DEFAULT_ORDER if t in counts] or list(DEFAULT_ORDER)
    return {
        "available": True,
        "reason": None,
        "documents": docs,
        "counts": counts,
        "order": order,
        "gloss": {t: GLOSS.get(t, "") for t in order},
        "open_tokens": list(OPEN_TOKENS),
        "open_count": sum(counts.get(t, 0) for t in OPEN_TOKENS),
        "unclassified_count": sum(1 for d in docs if d["token"] is None),
        "total": len(docs),
    }
