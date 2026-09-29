#!/usr/bin/env python3
"""Context stores — one writable surface per fact, and the update flows. Stdlib only.

## What this is for

Information stored in more than one place gets *partially* pulled instead of fully pulled. The fix is
not "store it once" — some facts legitimately need a copy shaped for a different reader — but: **there
is only ONE place a fact is updated, that update flows everywhere else, and every reader knows which
copy is formatted for it.** Three invariants, and this module is the procedure over them:

- **I1 — one writable surface.** One writer per fact; nothing else writes a projection.
- **I2 — the update flows.** A projection is *regenerated and diffed* against its source, not
  promised to be in sync.
- **I3 — every reader knows its projection.** Every consumer is declared, every declared consumer
  resolves, and a consumer reaching around its projection to the source is a smell worth printing.

**Why a manifest and a procedure rather than a rule in a `CLAUDE.md`.** The obvious fix for two stores
that disagree is a sentence telling a future run to keep them in sync — and a pair held together by a
sentence gives a reader no way to tell *in sync* from *drifted*, so the only available postures are
re-deriving by hand on every read, or trusting it. Everyone trusts it. `../context-stores.json`
declares each pair, and this module's job is to make its state a number.

## REPORT-ONLY. This module cannot fail a build in CI.

`--enforce` flips it, and flipping it is gated on a clean corpus over a sustained period. CI runs this
with `--venue ci` and **no** `--enforce`; nothing here can turn a build red on a finding — *"a gate
red on the day it lands is a gate someone disables."*

**It writes nothing, anywhere, ever.** The one subprocess it can run is a projection's declared
`generator` (I2), and that runs with its cwd set to a throwaway temp directory precisely so a
generator that writes relative to its cwd cannot touch the tree.

## What it reports today, and why `declared-only` is printed on every run

A pair whose projection is hand-maintained or written by an LLM turn has nothing to regenerate-and-diff
against, so it is `verify: "declared-only"` with a required `reason`. That is the obvious way to make
the whole check vacuous, so the declared-only count is printed on **every** run, always, beside the
verified count: an unverifiable pair stays a visible number rather than a hiding place, and a
"0 findings" over zero verified pairs is labelled NOT CHECKED rather than clean.

## The honest limits, stated here rather than implied away

- **I1d is a grep, not a lock.** It catches *named* writers in tracked `*.py` / `*.ps1`. It cannot
  catch an LLM turn that decides to hand-edit a state file, nor a path assembled from parts
  (`rc.STATE / "run-log.md"`), because the literal string never appears. **That gap is closed by I2,
  not by I1** — a hand-edit does not have to be *prevented* to be *caught*, because the next
  regenerate-and-diff reports it. Which means: for a `declared-only` pair, the gap is not closed at all.
- **"Tracked" projection paths.** This module reads "tracked" as *"a path in this repository"*, as
  opposed to a `store:`/`notion:` address, and scans both — reading it as *git-tracked* would exclude
  everything under `seneschal/state/`, which is exactly where drift lives. The **scanned corpus** is
  tracked `*.py` / `*.ps1` either way.
- **I3c is a text match over the consumer.** It finds a consumer that *names* the source; it cannot
  find one that reaches it another way. It is a smell detector and nothing more.
- **A `store:` / `notion:` address is never opened.** Nothing here talks to a store backend. Such a
  projection is declarable and countable; verifying one belongs to the daemon's Dream, in venue
  `dream`, and **that wiring is not built** — so `--venue dream` runs the invariants that need no
  network and reports the rest as declared-only, exactly as it would in CI.

USAGE:
  python check_context_stores.py                # report every venue, exit 0
  python check_context_stores.py --venue ci     # what CI runs
  python check_context_stores.py --venue dream  # what Dream will run, once Dream calls it
  python check_context_stores.py --json         # the same result as one object
  python check_context_stores.py --enforce      # exit 1 on any finding (NOT wired anywhere)
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))

sys.path.insert(0, SCRIPT_DIR)

# The path resolver is IMPORTED, never copied. Rung 2 in particular — "a tracked .gitignore rule
# declares this path" — took three separate git traps to get right (check_context_pointers.py's
# docstring); a second implementation of it would get them wrong again.
import check_context_pointers as ccp  # noqa: E402

MANIFEST_FILE = os.path.join("seneschal", "context-stores.json")
VENUES = ("ci", "dream")
VERIFY_VALUES = ("regenerate-and-diff", "declared-only")

#: How many lines of a drift diff are printed before it is truncated .
DIFF_EXCERPT_LINES = 20

#: I1d's write contexts. A literal projection path on the same line as one of these is a
#: reported write. Deliberately shallow: a line-scoped regex has no false-negative cliff the way a
#: half-parsed AST does, and the docstring says out loud that it is a grep.
WRITE_MARKERS = (
    re.compile(r"open\s*\([^)]*['\"][wax]"),
    re.compile(r"\bwrite_text\b"),
    re.compile(r"\bwrite_bytes\b"),
    re.compile(r"\bSet-Content\b", re.IGNORECASE),
    re.compile(r"\bAdd-Content\b", re.IGNORECASE),
    re.compile(r"\bOut-File\b", re.IGNORECASE),
    re.compile(r"memory_write\.py"),
    re.compile(r">\s*['\"]?$"),
)


class ManifestError(RuntimeError):
    """The manifest is missing or is not the shape `../context-stores.json` documents.

    Structurally different from a finding, and exits differently: a finding is something the tree is
    doing that this check reports; a ManifestError means the check could not run at all. Reporting
    "0 findings" off an unreadable manifest is the `check_context_pointers.py` OracleFailure lesson —
    never degrade a broken oracle into a clean result."""


# --------------------------------------------------------------------------------------- loading


def load_manifest(root: str) -> dict:
    path = os.path.join(root, MANIFEST_FILE)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ManifestError(f"cannot read {MANIFEST_FILE}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError(f"{MANIFEST_FILE} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("stores"), list):
        raise ManifestError(f"{MANIFEST_FILE} must be an object with a `stores` array")
    return data


def store_id(store: dict, index: int) -> str:
    sid = store.get("id")
    return sid if isinstance(sid, str) and sid else f"<store #{index}>"


def venue_of(store: dict) -> str:
    v = store.get("venue")
    return v if v in VENUES else "unassigned"


def projections_of(store: dict) -> list:
    projs = store.get("projections")
    return [p for p in projs if isinstance(p, dict)] if isinstance(projs, list) else []


def readers_of(projection: dict) -> list:
    rs = projection.get("readers")
    return [r for r in rs if isinstance(r, str) and r] if isinstance(rs, list) else []


#: Address schemes for a store this module can declare and count but never open. `store:` names a
#: domain of the active, pluggable store backend (`store:run-log`) without committing to one;
#: `notion:` names a Notion collection directly, for a pair that only exists on that backend.
STORE_SCHEMES = ("store:", "notion:")


def is_notion(path: str) -> bool:
    """An external store address (`store:` or `notion:`) — declarable and countable, never opened.
    The historical name is kept because every caller asks the same question of both schemes."""
    return isinstance(path, str) and path.startswith(STORE_SCHEMES)


def match_token(source: str) -> str:
    """What I3c looks for in a consumer's text.

    The scheme prefix is stripped, because the tree writes these addresses without it (a doc carries
    a bare `collection://…`, not `notion:collection://…`). A FRAGMENT IS KEPT: a consumer that names a
    whole database has named that store's source, and has NOT named a store whose source is one field
    on it. Collapsing the two would report the same consumer twice for two different facts."""
    if is_notion(source):
        return source.split(":", 1)[1]
    return source


# --------------------------------------------------------------------------------- I1 / I2 / I3


def _finding(invariant: str, message: str, **extra) -> dict:
    row = {"invariant": invariant, "message": message}
    row.update(extra)
    return row


def check_i1(root: str, stores: list, all_stores: list) -> list:
    """I1 — one writable surface.

    1a and 1b are asked against **every** store in the manifest, not just the selected venue: "two
    sources claim this path" is a property of the declaration, and a venue filter that could hide it
    would make `--venue ci` structurally blinder than a full run for no reason."""
    findings = []

    claims: dict = {}
    for i, store in enumerate(all_stores):
        for proj in projections_of(store):
            path = proj.get("path")
            if isinstance(path, str) and path:
                claims.setdefault(path, []).append(store_id(store, i))
    for path, ids in sorted(claims.items()):
        if len(ids) > 1:
            findings.append(_finding("I1a", f"two sources claim {path}: {', '.join(ids)}", path=path))

    sources = {}
    for i, store in enumerate(all_stores):
        src = store.get("source")
        if isinstance(src, str) and src:
            sources.setdefault(src, []).append(store_id(store, i))
    for path, ids in sorted(claims.items()):
        if path in sources:
            findings.append(_finding(
                "I1b", f"{path} is both a source and a projection of {', '.join(ids)}", path=path))

    for i, store in enumerate(stores):
        sid = store_id(store, i)
        writer = store.get("writer")
        n = len(writer) if isinstance(writer, list) else (1 if isinstance(writer, str) and writer else 0)
        if n != 1:
            findings.append(_finding("I1c", f"store {sid} declares {n} writers", store=sid))
        if not (isinstance(store.get("source"), str) and store.get("source")):
            findings.append(_finding("I1", f"store {sid} declares no source", store=sid))

    findings.extend(_check_i1d(root, stores))
    return findings


def _check_i1d(root: str, stores: list) -> list:
    """1d — nothing writes a declared projection except its `generator`.

    A grep over tracked `*.py` / `*.ps1`, and the module docstring says plainly that a grep cannot
    prove nothing writes a file. What it catches is a NAMED writer, which is the case worth catching:
    a script that hardcodes a projection path and opens it for writing is a second writer nobody
    declared."""
    targets = {}
    for i, store in enumerate(stores):
        for proj in projections_of(store):
            path = proj.get("path")
            if not (isinstance(path, str) and path) or is_notion(path):
                continue
            gen = proj.get("generator")
            targets[path] = (store_id(store, i), gen if isinstance(gen, list) else None)
    if not targets:
        return []

    findings = []
    for rel in sorted(_tracked_scripts(root)):
        if rel == os.path.join("seneschal", "scripts", "check_context_stores.py").replace(os.sep, "/"):
            continue  # this module names every projection path in its own tests and docstring
        try:
            with open(os.path.join(root, rel), "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            for path, (sid, gen) in targets.items():
                if path not in line:
                    continue
                if not any(m.search(line) for m in WRITE_MARKERS):
                    continue
                if gen and rel in gen:
                    continue
                declared = " ".join(gen) if gen else "(none — this projection is not generated)"
                findings.append(_finding(
                    "I1d", f"{rel}:{n} writes projection {path}, declared generator is {declared}",
                    path=path, store=sid, writer=f"{rel}:{n}"))
    return findings


def _tracked_scripts(root: str) -> list:
    tracked = ccp.tracked_paths(root)
    return [t for t in tracked if t.endswith(".py") or t.endswith(".ps1")]


def _lf(data: bytes) -> bytes:
    """git-object bytes: CRLF normalised to LF, so this box and CI agree exactly and a
    line-ending-only difference can never be reported as drift."""
    return data.replace(b"\r\n", b"\n")


def _committed_bytes(root: str, rel: str) -> bytes | None:
    try:
        out = subprocess.run(["git", "-c", "core.fsmonitor=false", "show", f"HEAD:{rel}"],
                             cwd=root, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _disk_bytes(root: str, rel: str) -> bytes | None:
    try:
        with open(os.path.join(root, rel.replace("/", os.sep)), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _run_generator(root: str, generator: list) -> tuple:
    """Run a projection's generator with stdout captured. Returns `(stdout_bytes, error)`.

    **cwd is a throwaway temp directory, deliberately** — read-only against the source, because of
    the one rail this module is under: a generator that writes
    relative to its cwd must not be able to touch the tree. Repo-relative argv tokens are resolved to
    absolute paths first, so a generator declared the way the manifest writes one
    (`["python", "seneschal/scripts/loops.py", "render"]`) still finds its script."""
    argv = []
    for token in generator:
        if not isinstance(token, str):
            return None, f"generator argv contains a non-string token: {token!r}"
        candidate = os.path.join(root, token.replace("/", os.sep))
        argv.append(candidate if os.path.exists(candidate) else token)
    with tempfile.TemporaryDirectory(prefix="ctxstores-") as tmp:
        try:
            proc = subprocess.run(argv, cwd=tmp, capture_output=True, timeout=300)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"generator could not run: {exc}"
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, f"generator exited {proc.returncode}: {err[-1][:200] if err else 'no stderr'}"
    return proc.stdout, None


def check_i2(root: str, stores: list) -> tuple:
    """I2 — the update flows. Returns `(findings, rows)`; `rows` is the per-pair report."""
    findings, rows = [], []
    for i, store in enumerate(stores):
        sid = store_id(store, i)
        venue = venue_of(store)
        for proj in projections_of(store):
            path = proj.get("path") if isinstance(proj.get("path"), str) else "<no path>"
            verify = proj.get("verify")
            if verify == "declared-only":
                reason = proj.get("reason")
                if not (isinstance(reason, str) and reason.strip()):
                    findings.append(_finding(
                        "I2", f"{sid} → {path}: verify=declared-only with no `reason` "
                              f"(a declared-only pair must say why)", store=sid, path=path))
                    rows.append({"store": sid, "path": path, "status": "declared-only",
                                 "detail": "NO REASON DECLARED"})
                else:
                    rows.append({"store": sid, "path": path, "status": "declared-only",
                                 "detail": reason.strip()})
                continue
            if verify != "regenerate-and-diff":
                findings.append(_finding(
                    "I2", f"{sid} → {path}: verify is {verify!r}, not one of "
                          f"{' / '.join(VERIFY_VALUES)}", store=sid, path=path))
                rows.append({"store": sid, "path": path, "status": "error",
                             "detail": f"unknown verify {verify!r}"})
                continue

            row = _verify_pair(root, sid, path, proj, venue)
            rows.append(row)
            if row["status"] != "ok":
                findings.append(_finding("I2", f"{sid} → {path}: {row['detail']}",
                                         store=sid, path=path, diff=row.get("diff")))
    return findings, rows


def _verify_pair(root: str, sid: str, path: str, proj: dict, venue: str) -> dict:
    generator = proj.get("generator")
    if not (isinstance(generator, list) and generator):
        return {"store": sid, "path": path, "status": "error",
                "detail": "verify=regenerate-and-diff but no `generator` is declared"}
    if is_notion(path):
        return {"store": sid, "path": path, "status": "error",
                "detail": "a notion: address cannot be regenerate-and-diff — nothing here opens Notion"}

    produced, err = _run_generator(root, generator)
    if err:
        return {"store": sid, "path": path, "status": "error", "detail": err}

    committed = _committed_bytes(root, path) if venue == "ci" else _disk_bytes(root, path)
    if committed is None:
        where = "committed at HEAD" if venue == "ci" else "on disk"
        return {"store": sid, "path": path, "status": "error",
                "detail": f"the projection is not {where}, so there is nothing to diff"}

    want = _lf(committed).decode("utf-8", "replace").splitlines()
    got = _lf(produced).decode("utf-8", "replace").splitlines()
    if want == got:
        return {"store": sid, "path": path, "status": "ok", "detail": "regenerated identical"}

    diff = list(difflib.unified_diff(want, got, fromfile=f"{path} (committed)",
                                     tofile=f"{path} (regenerated)", lineterm=""))
    added = sum(1 for d in diff[2:] if d.startswith("+"))
    removed = sum(1 for d in diff[2:] if d.startswith("-"))
    excerpt = diff[:DIFF_EXCERPT_LINES]
    if len(diff) > DIFF_EXCERPT_LINES:
        excerpt.append(f"… {len(diff) - DIFF_EXCERPT_LINES} more lines")
    return {"store": sid, "path": path, "status": "DRIFT",
            "detail": f"DRIFT (+{added} / -{removed} lines)", "added": added, "removed": removed,
            "diff": excerpt}


def check_i3(root: str, stores: list) -> list:
    """I3 — every reader knows its projection."""
    findings = []
    tracked = ccp.tracked_paths(root)
    unresolved = []

    for i, store in enumerate(stores):
        sid = store_id(store, i)
        source = store.get("source") if isinstance(store.get("source"), str) else ""
        token = match_token(source) if source else ""
        for proj in projections_of(store):
            path = proj.get("path") if isinstance(proj.get("path"), str) else "<no path>"
            readers = readers_of(proj)
            if not readers:
                findings.append(_finding(
                    "I3b", f"{path} has no declared reader — generated for nobody",
                    store=sid, path=path))
                continue
            for reader in readers:
                if not _resolves(reader, tracked, root):
                    unresolved.append((sid, path, reader))
                    continue
                if token and reader != source and _names(root, reader, token):
                    findings.append(_finding(
                        "I3c", f"{reader} points at source {token}; its projection is {path}",
                        store=sid, path=path, reader=reader))

    # Rung 2 (`git check-ignore`) is asked ONCE, in a batch, and only for the readers nothing else
    # resolved — the oracle is expensive and every one of its traps is a batching trap.
    if unresolved:
        try:
            ignored = ccp.check_ignored(root, [r for _, _, r in unresolved])
        except ccp.OracleFailure as exc:
            raise ManifestError(f"git check-ignore could not be trusted: {exc}") from exc
        for sid, path, reader in unresolved:
            if reader not in ignored:
                findings.append(_finding(
                    "I3a", f"{sid}/{path}: reader {reader} does not resolve",
                    store=sid, path=path, reader=reader))
    return findings


def _resolves(reader: str, tracked: set, root: str) -> bool:
    """Rungs 0 and 1 of the pointer check's ladder. A manifest reader is repo-root-relative by convention, so
    `candidates_for` is called with no containing document and no extra bases — but it is still check
    the pointer check's function, glob expansion and directory-prefix rule included."""
    return any(ccp.resolves_tracked(c, tracked, root) for c in ccp.candidates_for(reader, "", []))


def _names(root: str, reader: str, token: str) -> bool:
    try:
        with open(os.path.join(root, reader.replace("/", os.sep)), "r",
                  encoding="utf-8", errors="replace") as fh:
            return token in fh.read()
    except OSError:
        return False


# ------------------------------------------------------------------------------------ the scan


def scan(root: str | None = None, venue: str | None = None) -> dict:
    root = root or REPO_ROOT
    manifest = load_manifest(root)
    all_stores = [s for s in manifest["stores"] if isinstance(s, dict)]

    census = {}
    for store in all_stores:
        census[venue_of(store)] = census.get(venue_of(store), 0) + 1
    selected = [s for s in all_stores if venue is None or venue_of(s) == venue]

    i1 = check_i1(root, selected, all_stores)
    i2, i2_rows = check_i2(root, selected)
    i3 = check_i3(root, selected)

    projections = [p for s in selected for p in projections_of(s)]
    verified = sum(1 for r in i2_rows if r["status"] in ("ok", "DRIFT"))
    declared_only = sum(1 for r in i2_rows if r["status"] == "declared-only")
    return {
        "manifest": MANIFEST_FILE.replace(os.sep, "/"),
        "venue": venue,
        "report_only": True,
        "stats": {
            "stores": len(selected),
            "stores_total": len(all_stores),
            "sources": len({s.get("source") for s in selected if s.get("source")}),
            "projections": len(projections),
            "readers": sum(len(readers_of(p)) for p in projections),
            "venues": census,
            "verified": verified,
            "declared_only": declared_only,
            "declared_only_pct": round(declared_only * 100 / len(i2_rows)) if i2_rows else 0,
        },
        "i2_pairs": i2_rows,
        "findings": i1 + i2 + i3,
    }


# --------------------------------------------------------------------------------------- output


_LABELS = (("I1", "one writable surface"), ("I2", "the update flows"),
           ("I3", "every reader knows its projection"))
_LABEL_WIDTH = 39


def _plural(n: int) -> str:
    return f"{n} finding" if n == 1 else f"{n} findings"


def _leader(prefix: str, label: str) -> str:
    text = f"{prefix} {label} "
    return text + "." * max(1, _LABEL_WIDTH - len(text))


def render(result: dict) -> str:
    s = result["stats"]
    out = [f"context stores ({result['manifest']})   REPORT-ONLY", ""]

    census = ", ".join(f"{k} {v}" for k, v in sorted(s["venues"].items()))
    if result["venue"]:
        skipped = ", ".join(f"{k} {v}" for k, v in sorted(s["venues"].items())
                            if k != result["venue"])
        out.append(f"  venue {result['venue']} — {s['stores']} of {s['stores_total']} stores "
                   f"selected; not run here: {skipped or 'nothing'}")
        census = ", ".join(f"{k} {v}" for k, v in sorted(s["venues"].items())
                           if k == result["venue"])
    out.append(f"  stores {s['stores']} · sources {s['sources']} · projections {s['projections']} · "
               f"readers {s['readers']} · venues: {census}")
    if s["venues"].get("unassigned"):
        out.append(f"  NOTE: {s['venues']['unassigned']} store(s) declare no recognised venue — "
                   f"they are verified NOWHERE.")
    out.append("")

    by_inv = {code: [f for f in result["findings"] if f["invariant"].startswith(code)]
              for code, _ in _LABELS}
    for code, label in _LABELS:
        rows = by_inv[code]
        line = f"  {_leader(code, label)} {_plural(len(rows))}"
        if code == "I2":
            line += (f"   ({s['verified']} verified, {s['declared_only']} declared-only, "
                     f"{s['declared_only_pct']}%)")
        out.append(line)
        for row in rows:
            out.append(f"      {row['invariant']}  {row['message']}")
            for dline in row.get("diff") or []:
                out.append(f"          {dline}")
        # The declared-only count prints on EVERY run, beside the verified count, so an
        # unverifiable pair is a visible number and not a hiding place. When NOTHING in this venue is
        # verified, "0 findings" is not coverage and must not read as coverage — the ratio is still a
        # future --enforce question and nothing is refused on it here.
        if code == "I2" and s["verified"] == 0 and result["i2_pairs"]:
            out.append(f"      NOTE  0 of {len(result['i2_pairs'])} pair(s) in this venue are "
                       f"machine-verified. A finding count of 0 here means NOT CHECKED, not clean.")

    # The per-pair table. Columns are aligned because the whole point of it is that one glance
    # tells you which pairs are actually verified and which are only declared.
    if result["i2_pairs"]:
        out.append("")
        wid = max(len(r["store"]) for r in result["i2_pairs"])
        wpath = max(len(r["path"]) for r in result["i2_pairs"])
        for row in result["i2_pairs"]:
            head = f"  I2  {row['store'].ljust(wid)} → {row['path'].ljust(wpath)}  "
            if row["status"] == "declared-only":
                detail = row["detail"]
                out.append(f"{head} declared-only "
                           f"(\"{detail[:56]}{'…' if len(detail) > 56 else ''}\")")
            else:
                out.append(f"{head} {row['detail']}")
        out.append("")

    n = len(result["findings"])
    out.append(f"  {_plural(n)}. Report-only: this module cannot fail a build. --enforce flips it.")
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Context stores — one writable surface per fact (report-only).")
    p.add_argument("--root", default=REPO_ROOT)
    p.add_argument("--venue", choices=VENUES,
                   help="run only this venue's stores. CI passes `ci`; Dream will pass `dream`.")
    p.add_argument("--json", action="store_true")
    p.add_argument("--enforce", action="store_true",
                   help="exit 1 on any finding (not wired into CI — this check is report-only)")
    args = p.parse_args(argv)

    try:
        result = scan(args.root, args.venue)
    except ManifestError as exc:
        # Loud, and non-zero even while report-only. A manifest that cannot be read is not a clean tree, and
        # a check that reports "0 findings" off one is worse than no check at all. This is NOT a
        # finding — no `--enforce` gate applies to it.
        print(f"MANIFEST ERROR: {exc}\n  (refusing to report — see context-stores.json's _note)",
              file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2) if args.json else render(result))
    n = len(result["findings"])
    if n and not args.enforce:
        print(f"\n  [report-only] {n} finding(s) reported, exit 0 by design.")
    return 1 if (args.enforce and n) else 0


if __name__ == "__main__":
    sys.exit(main())
