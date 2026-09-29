#!/usr/bin/env python3
"""The dangling-pointer check. Stdlib only.

## What this is for

The failure mode of a router system is **a pointer to a renamed file**, after which the knowledge is
*unreachable* — worse than duplicated, because duplicated knowledge is at least present. Routing
grounding one hop away (a root router, sub-routers, mode files, leaf docs) raises this bug's blast
radius; this is the harness that makes that responsible to do. The classic instance: a planning
prompt that tells a fresh agent to read `seneschal/CLAUDE.md`, a path that has never existed
(`CLAUDE.md` is at the repo root) — a document whose entire job is routing, pointing at nothing.

## Enforcing — and why the rulings below are the engineering content

`--enforce` blocks (CI runs it). A naive recogniser over this kind of tree reports dozens of findings
of which a handful are real — single-digit precision. That is not a check, it is a wall, and a wall
gets disabled. The skip rules below are what take it from the wall to the handful, and **the gap
between those two numbers is the entire engineering content of this check.**

## The resolution ladder — a pointer is reported only if NO rung matches

- **Rung 0 — bases.** repo-root, then every ancestor of the containing document's directory, then a
  declared base (`../context-pointers.json` → `bases`). **No single base works**: a doc in
  `seneschal/docs/` writing `state/run-log.md` means `seneschal/state/run-log.md`, while the root
  `CLAUDE.md` writing `scripts/presence.py` means `seneschal/scripts/presence.py`.
- **Rung 0′ — an explicitly-relative pointer gets exactly ONE base: its own document's directory.**
  The ladder above is permissive by design, and applied to a leading-`../` token it dissolves the
  check — `../subagents/x` in `seneschal/modes/` resolves through the `seneschal/` ancestor and
  reports clean while meaning `seneschal/subagents/`, which does not exist. **A pointer wrong by
  exactly one level is invisible to a resolver that tries every level.**
- **Rung 1 — tracked.** In the WORKING TREE — `git ls-files` ∪ `git ls-files --others
  --exclude-standard` (`gate_git.working_tree_paths`) — or a directory prefix of such a path. An
  untracked-not-ignored file is about to be tracked by the commit the author is preparing;
  enumerating `ls-files` alone made a pointer to a not-yet-added `test_x.py` read as dangling locally.
  On CI's clean checkout the union IS `ls-files`, so the gate's verdict there is unchanged.
- **Rung 2 — declared runtime.** `git check-ignore` says a tracked `.gitignore` rule covers it. This
  replaces a hand-maintained runtime allowlist and is the better answer: **the declaration already
  exists, in the repo, beside the thing it describes.** `seneschal/state/run-log.md` exists on the
  daemon's box and in no clone; a naive ls-files check calls it dangling. *The honest limit:* this
  proves **intent**, not **existence** — it cannot tell `state/run-log.md` (real) from
  `state/typo-log.md` (never existed). So the guarantee is *"no dangling pointer to a path that would
  be tracked"*, not *"every pointer resolves"*.
- **Rung 3 — allowlist**, `seneschal/context-pointers.json`, `reason` required per entry. A row whose
  pointer has started resolving on its own is named in the report so it gets deleted.

## One asymmetry about `.claude/` worth stating, because it cuts both ways

`.claude/rules/**/*.md` is **scanned** (see `SCAN_GLOBS`), but a pointer *whose target* is under
`.claude/` is still **allowlisted rather than resolved**: `.claude/worktrees/` may be ignored only in
one checkout, via an *untracked* `.git/info/exclude`, so rung 2's answer there depends on the machine
and a blocking gate may not. Scanning a rule file's own prose and resolving a path into `.claude/` are
different questions, and only the second one is machine-dependent. A rule file's pointers are best
written **repo-root-relative**, because its own directory is a useless base for the ancestor walk.

## The traps, every one found by RUNNING `git check-ignore`, not by reasoning about it

**This module is the blocking one of the context checks**: a pointer resolving to nothing is a red
build. Its exit 2 (`OracleFailure`) is a TOOL failure, never a finding — distinct from exit 1, which is
a real dangling pointer under `--enforce`. `seneschal/references/**` and `seneschal/docs/**` are
deliberately UNBUDGETED by the sibling byte check (`check_context_budget.py`), which is what keeps
"move the detail one hop out" available as an escape from a router's size.

The first two traps make the oracle **silently under-report** — i.e. emit false *dangling* findings,
the failure that kills adoption:

1. **`--stdin` must be `-z` (NUL-separated) and written as BYTES.** In text mode on Windows the pipe
   translates `\\n` -> `\\r\\n`, git takes the `\\r` as part of the pathname, quotes its output
   (`"seneschal/state/run-log.md\\r"`) and **drops other entries entirely**.
2. **One malformed path aborts the whole batch.** Feeding `/api/status` exits 128
   (`fatal: Invalid path '/api'`) and everything after it is silently lost, while the output produced
   before it looks like a normal success. So external paths are filtered *before* batching, **and the
   exit code is checked** — anything but 0 or 1 means the oracle failed and must abort loudly, never
   degrade to "nothing is ignored".

**Trap 3 points the OTHER WAY, and that is what makes it survive.** It makes the oracle silently
*over*-report ignored, so a pointer to a directory that does not exist resolves clean — a green check
looking at nothing, which is the failure mode nobody investigates. A `.gitignore` checked out with CRLF
terminators has its trailing `\\r` stripped back off each line when git reads it, leaving a **blank
line as the EMPTY PATTERN instead of as a skipped one**, and the empty pattern matches any path written
with a trailing slash. It is worse than random: a directory holding a tracked file is NOT affected, so
the false pass lands exactly on the untracked and nonexistent directories this check exists to catch.
The fix is `ignored_from_verbose` — read the matching PATTERN via `-v`, not just the verdict — and its
second filter is there because **`-v` widens the result set**, which is the trap inside the fix.

USAGE:
  python check_context_pointers.py            # report, exit 0
  python check_context_pointers.py --enforce  # exit 1 on any dangling pointer (what CI runs)
  python check_context_pointers.py --json
"""
from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, SCRIPT_DIR)

import gate_git  # noqa: E402  — the working-tree enumeration every local gate shares
ALLOWLIST_FILE = os.path.join("seneschal", "context-pointers.json")

# Start narrow, widen on evidence. Widening blind produces a several-hundred-finding wall.
#
# The mode bodies (`seneschal/modes/**`) are in scope because they are EXECUTED rather than read: a run
# dispatches into them by an imperative read, so a mode whose pointer to its subagent is wrong by one
# directory level (`../subagents/<name>/SKILL.md` resolving to `seneschal/subagents/`, which does not
# exist) is the worst place for this bug to hide. Guarding the docs and skipping the instructions
# checks the prose and not the code.
SCAN_GLOBS = ("CLAUDE.md", "seneschal/SKILL.md", "persona/*.md", "phone/CLAUDE.md",
              "subagents/**/SKILL.md", "seneschal/docs/**/*.md", "seneschal/references/**/*.md",
              "seneschal/modes/**/*.md",
              # every sub-router, including ones a later change may add — a glob that matches nothing
              # today costs nothing and closes the gap before the file exists.
              "archons/CLAUDE.md", "cockpit/CLAUDE.md", "seneschal/scripts/CLAUDE.md",
              # Prospective, deliberately: no rule file ships, but a `.claude/rules/*.md` with
              # `paths:` frontmatter is an *executed* instruction file in exactly the sense
              # `seneschal/modes/**` is — obeyed as instruction rather than read as reference. The
              # gap's shape is already known, so it is closed before the first file exists.
              # `ScanScopeTests` carries the paired fixture test that proves this entry works without
              # waiting for a live file to point at.
              ".claude/rules/**/*.md")

MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
PATH_SHAPED_RE = re.compile(r"^[A-Za-z0-9_.~*<>/@-]+$")

# --- skip rules. Each exists because of a measured false-positive class. ----------------------------
# Schema identifiers: `seneschal.job-leads/1` — the largest single FP class without this rule.
SCHEMA_ID_RE = re.compile(r"^[a-z][a-z0-9.-]*/\d+$")
# Git refs: `origin/develop`, `feat/archon-tiresias`. Narrow on purpose — `seneschal/SKILL.md` has an
# extension and is NOT skipped by this.
BRANCHY_FIRST = ("feat", "fix", "docs", "chore", "feature", "port", "rescue")
KNOWN_TLDS = (".com", ".org", ".net", ".io", ".dev", ".ai", ".sh", ".md")  # `.md` guards `foo.md/bar`
# Prose that happens to contain a slash.
PROSE_STOPLIST = {"and/or", "they/them", "try/except", "a/b", "n/a", "w/o", "i/o",
                  "read/write", "input/output", "yes/no", "on/off", "in/out", "±20/day"}


def _is_url(token: str) -> bool:
    if "://" in token or token.startswith(("http:", "https:", "mailto:")):
        return True
    first = token.split("/", 1)[0].lower()
    return "." in first and any(first.endswith(t) for t in KNOWN_TLDS[:-1])


def _is_leading_dotdot(token: str) -> bool:
    """A DOCUMENT-RELATIVE pointer: a leading run of `../`, and no `..` after that.

    `../../persona/persona.default.md`, `../subagents/reminders/SKILL.md`. Resolved against the
    containing document's directory by `candidates_for`, and against nothing else — see there."""
    parts = token.split("/")
    if parts[0] != "..":
        return False
    i = 0
    while i < len(parts) and parts[i] == "..":
        i += 1
    return ".." not in parts[i:]


def _is_external(token: str) -> bool:
    """Deliberately outside this repo — skipped, never reported. ALSO an implementation requirement:
    feeding one of these to `git check-ignore` aborts the whole batch (trap 2).

    `..` is checked as a SEGMENT AFTER A REAL ONE, not merely as a prefix. The first real run of this
    module hit `SCRIPT_DIR/../..` — a code-shaped token quoted in prose — which escapes the repo from
    the middle and made git exit 128, aborting the batch. The oracle guard caught it loudly, which is
    what it is for; that is the trap this half of the rule holds.

    **A leading `../` is NOT external.** Treating ANY `..` segment as external skips every
    explicitly-relative pointer unexamined — the dominant way this repo writes a cross-directory
    pointer (`subagents/*/SKILL.md` reach the persona as `../../persona/persona.default.md`,
    `seneschal/references/*.md` reach `seneschal/scripts/` as `../scripts/`), so a rule meant to skip
    *another repo* would skip most of *this* one, including a mode's pointer wrong by exactly one level.

    A leading run of `../` is a pointer to resolve, not an escape to ignore. Whether it *does*
    escape is a question about the document it was written in, so it is answered in `candidates_for`
    where the document is known — a token that normalises above the repo root is external after all,
    and is skipped there. `./` is deliberately still external: nothing in the tree writes it, so
    narrowing it would buy no coverage and widen the batch fed to check-ignore for nothing."""
    if "\\" in token:
        return True
    if token.startswith(("~/", "/")):
        return True
    if token.startswith("./"):
        return True
    if ".." in token.split("/") and not _is_leading_dotdot(token):
        return True
    return len(token) > 1 and token[1] == ":"  # drive letter


def _git_batch_safe(token: str) -> bool:
    """Will `git check-ignore` accept this pathname without aborting the batch (trap 2)?

    **A DIFFERENT QUESTION FROM `_is_external`.** Once a leading `../` is a pointer rather than an
    escape, "skipped by the rules" and "refused by git" stop being the same set: `../outside` is not
    external, but handed to git it exits 128 and silently truncates the batch into a partial result
    that looks like a normal success. `OracleTests.test_trap_2_...` pins that.

    So this keeps the strict rule and owes nothing to the skip rules: **any `..` segment at all is
    unsafe.** Nothing is lost by being strict here — `candidates_for` hands this function normalised
    paths, which contain no `..` — and the failure it prevents is invisible when it happens."""
    if "\\" in token or token.startswith(("~/", "./", "/")):
        return False
    if ".." in token.split("/"):
        return False
    return not (len(token) > 1 and token[1] == ":")  # drive letter


def _is_prose(token: str) -> bool:
    if token.lower() in PROSE_STOPLIST:
        return True
    if token.startswith("mcp__"):
        return True
    parts = token.split("/")
    # all-short-lowercase-words with no extension is prose, not a path
    if "." not in token and all(p.isalpha() and p.islower() and len(p) <= 8 for p in parts if p):
        return True
    return False


def _is_branchy(token: str) -> bool:
    if token.startswith("origin/"):
        return True
    first = token.split("/", 1)[0]
    return "." not in token and first in BRANCHY_FIRST


def _is_placeholder(token: str) -> bool:
    """A placeholder in the **first segment** — `<cwd>/seneschal/state/presence.lock`,
    `<repos>/seneschal-worktrees/<job-id>`, `<out_dir>/memory.db`.

    The placeholder stands in for the ROOT, so the path is not repo-relative and nothing here can
    resolve it. **Deliberately not "any angle bracket anywhere":** `state/jobs/<id>.json` is a real
    pointer whose directory exists, and `expand()` already turns `<id>` into `*` and fnmatches it
    against tracked files. The first version of this rule swallowed exactly that, and
    `RulingTests.test_real_pointers_are_NOT_skipped` pins that — which is what that test is for."""
    first = token.split("/", 1)[0]
    return bool(re.search(r"[<>{}]", first))


def _is_field_list(token: str) -> bool:
    """`title/argv/cwd/channel/wake/lease/deadline_sec/retry`, `save/load_daemon_state`,
    `session_id/pid/source/started_at/...`.

    Prose enumerating FIELDS or FUNCTIONS with a slash as "and/or". `_is_prose` misses these because
    the parts are long or contain underscores. The discriminator is that no segment looks like a
    filename — no extension anywhere, and more segments than any real path in this repo has.
    `save/load_daemon_state` is the motivating case: two function names, not a directory."""
    if "." in token:
        return False
    parts = [p for p in token.split("/") if p]
    if len(parts) < 2:
        return False
    # Four-plus segments with no extension anywhere is an enumeration; no path in this repo is that
    # deep without a filename at the end.
    if len(parts) >= 4:
        return True
    # An underscored identifier as a segment: `save/load_daemon_state` is two function names. A
    # directory named like a Python identifier and containing no file would be a pointer to nothing
    # anyway, so this cannot mask a real one.
    return any("_" in p for p in parts)


def _is_ratio(token: str) -> bool:
    """`0/7`, `Multiple/day`, `2/3`.

    A ratio or a rate. Found by this check reporting `0/7` three times from a phases table — the most
    obviously-not-a-path finding in the set, and one a shape rule would never catch because `0/7` is
    perfectly path-shaped."""
    parts = token.split("/")
    if len(parts) != 2:
        return False
    a, b = parts
    if a.isdigit() and b.isdigit():
        return True
    # "Multiple/day", "3/week" — a unit on the right and no extension anywhere
    return "." not in token and b.lower() in {"day", "week", "month", "year", "hour", "turn", "run"}


def _is_git_internal(token: str) -> bool:
    """`.git/info/exclude`, `.git/worktrees/*/gitdir`.

    Real files that git itself owns. They exist, they are never tracked, and they can never be
    allowlisted honestly because their presence depends on the checkout's own state."""
    return token.startswith(".git/")


def should_skip(token: str) -> str | None:
    """Return the ruling that skips this token, or None if it is a real pointer to resolve.

    **`external` is tested FIRST, before path-shape.** Order matters here for a reason that is not
    cosmetic: a Windows path (`C:\\Users\\...`) and an HTTP route with a placeholder
    (`/api/archons/{id}/restart`) both contain characters outside the path-shaped charset, so a
    shape-first ordering rules them "not path-shaped" — technically harmless, but it means the
    external ruling is never exercised on exactly the inputs trap 2 says must be filtered before
    they reach `git check-ignore`. Rule them external, and the filter and the ruling agree."""
    if _is_external(token):
        return "external"
    if not PATH_SHAPED_RE.match(token) or "/" not in token:
        return "not path-shaped"
    if _is_url(token):
        return "url"
    if SCHEMA_ID_RE.match(token):
        return "schema-identifier"
    if _is_branchy(token):
        return "git-ref"
    if _is_prose(token):
        return "prose"
    if _is_placeholder(token):
        return "placeholder"
    if _is_ratio(token):
        return "ratio"
    if _is_field_list(token):
        return "field-list"
    if _is_git_internal(token):
        return "git-internal"
    return None


# --------------------------------------------------------------------------- the oracle

def _git(root: str, *args) -> str | None:
    try:
        out = subprocess.run(["git", "-c", "core.fsmonitor=false", *args],
                             cwd=root, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _git_bytes(root: str, *args) -> bytes | None:
    """As `_git`, but WITHOUT decoding — a line-terminator question cannot be asked through a text
    handle, which would translate the very bytes being measured."""
    try:
        out = subprocess.run(["git", "-c", "core.fsmonitor=false", *args],
                             cwd=root, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def tracked_paths(root: str) -> set:
    out = _git(root, "ls-files")
    return set(out.split("\n")) - {""} if out else set()


class OracleFailure(RuntimeError):
    """`git check-ignore` did not behave. Abort loudly; never degrade to 'nothing is ignored' — that
    silently converts every runtime path into a false dangling finding (traps 1 and 2)."""


def ignored_from_verbose(raw: bytes) -> set:
    """Parse `check-ignore -v --stdin -z` output into the set that is ACTUALLY ignored.

    The wire format is four NUL-separated fields per record — source, linenum, pattern, pathname —
    and BOTH filters below exist because a record is not the same thing as a verdict:

    **Filter 1 — an EMPTY pattern is the CRLF phantom (trap 3), and it is why this function exists.**
    A `.gitignore` checked out with CRLF terminators has its trailing `\\r` stripped back off each
    line when git reads it, which turns a *blank line* into an **empty pattern** rather than into a
    skipped one — and the empty pattern matches every path spelled with a trailing slash. Measured on
    this checkout: `seneschal/nonexistent-thing/` resolved against blank line 70. **An empty pattern is
    never a real declaration**, so dropping it is correct on every host, not a Windows workaround.

    **Filter 2 — a `!`-prefixed pattern means NOT ignored, and `-v` is what makes it visible.**
    This one is a trap, and the obvious fix walks straight into it: **`-v` WIDENS the result set.**
    Plain mode prints only ignored paths; `-v` prints the final matching pattern *including a
    negation*, so `keep.log` under `*.log` + `!keep.log` appears in `-v` output and not in plain
    output, and `git check-ignore -v keep.log` exits **0** where the plain form exits 1. A parser
    that read "-v emitted a record" as "ignored" would therefore trade the phantom for a brand-new
    false positive — and this repo's `.gitignore` carries ten-plus negations
    (`!seneschal/state/README.md`, `!archons/*/state/*.example.json`, …), so it would have fired at once.

    Together these two reconstruct plain-mode semantics EXACTLY, minus the phantom."""
    fields = raw.decode("utf-8", "replace").split("\0")
    if fields and fields[-1] == "":
        fields.pop()  # trailing NUL, not a field
    if len(fields) % 4:
        raise OracleFailure(
            f"git check-ignore -v returned {len(fields)} fields, not a multiple of 4 — the record "
            f"framing is not what this parser was written against; refusing to guess")
    ignored = set()
    for i in range(0, len(fields), 4):
        _source, _linenum, pattern, pathname = fields[i:i + 4]
        if not pattern or pattern.startswith("!"):
            continue
        ignored.add(pathname)
    return ignored


def check_ignored(root: str, candidates: list) -> set:
    """Which of these are covered by a tracked .gitignore rule. Batched, NUL-separated, BYTES, and
    read through `-v` so the MATCHING PATTERN is visible rather than only the verdict.

    Every one of those details is load-bearing, and every one was found by running the command rather
    than reasoning about it — see the traps in this module's docstring. `-v` is the later
    addition: without the pattern text there is no way to tell a real rule from the CRLF phantom,
    because the *verdict* is identical. `ignored_from_verbose` is where that judgment lives."""
    candidates = [c for c in candidates if c and _git_batch_safe(c)]
    if not candidates:
        return set()
    payload = ("\0".join(candidates) + "\0").encode("utf-8")
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "check-ignore", "-v", "--stdin", "-z"],
            cwd=root, input=payload, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OracleFailure(f"git check-ignore could not run: {exc}") from exc
    # 0 = some path had a final matching pattern, 1 = none did. ANYTHING else means the batch aborted
    # and the output is a silent partial — the exact failure that would report 264 real paths as
    # dangling. NOTE the exit code is used ONLY to detect that abort: under `-v` a 0 no longer means
    # "something is ignored" (a negation scores 0 too), so the verdict comes from the patterns.
    if proc.returncode not in (0, 1):
        raise OracleFailure(
            f"git check-ignore exited {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return ignored_from_verbose(proc.stdout)


def crlf_ignore_files(root: str) -> list:
    """Tracked ignore/attributes files whose COMMITTED BLOB carries a CR. Sorted paths.

    **The blob, deliberately, not the working tree.** The working tree is legitimately CRLF on any
    host with `core.autocrlf=true` and there is nothing wrong with that per se; what must never
    happen is a CR reaching the *shared* object, because then every checkout everywhere inherits the
    trap-3 phantom and `.gitattributes` cannot undo it. Reading the blob asks the portable question and
    gives the same answer on every machine, which is what makes it usable as a gate.

    Returns [] when `root` is not a git repo — this is a hygiene probe, not an oracle, and it may
    never be the thing that fails a run for an unrelated reason."""
    listing = _git(root, "ls-files", "-z", "*.gitignore", ".gitignore",
                   "*.gitattributes", ".gitattributes")
    if listing is None:
        return []
    offenders = []
    for rel in sorted({p for p in listing.split("\0") if p}):
        blob = _git_bytes(root, "cat-file", "blob", f"HEAD:{rel}")
        if blob is not None and b"\r" in blob:
            offenders.append(rel)
    return offenders


# --------------------------------------------------------------------------- resolution

def expand(token: str) -> str:
    """`<id>` placeholders behave as globs. A placeholder expanding to nothing is exactly the bug."""
    return re.sub(r"<[^>]*>", "*", token)


def resolve_relative(token: str, doc_rel: str) -> str | None:
    """A leading-`../` pointer, normalised against its own document's directory. None if it escapes
    the repo (`../sibling-repo` from a doc at depth 1 is a sibling checkout, external after all).

    **THE ANCESTOR WALK IS DELIBERATELY NOT APPLIED HERE, and that is what makes this class
    checkable at all.** `candidates_for`'s base ladder is permissive by design — it is what takes 619
    findings down to ~2 — but applied to an explicitly-relative token it dissolves the very thing
    being checked: `../subagents/x` from `seneschal/modes/ask.md` would resolve through the `seneschal/`
    ancestor as `seneschal/../subagents/x` = `subagents/x`, and report clean, when the pointer as written
    means `seneschal/subagents/x` and loads nothing. A pointer wrong by exactly one level is invisible to
    a ladder that tries every level.

    The ruling: **the author wrote the traversal, so the traversal is the answer.** `../` means "up
    from *here*", and "here" is the file the reader is holding. If any base is allowed, `../` means
    nothing at all. Every other writer in this tree already obeys it — `subagents/*/SKILL.md` at depth
    2 reach the persona as `../../persona/persona.default.md`, `subagents/journal-steward/*/SKILL.md` at depth 3
    as `../../../seneschal/scripts/`, `seneschal/references/*.md` as `../../persona/` and `../state/`."""
    doc_dir = os.path.dirname(doc_rel)
    joined = f"{doc_dir}/{token}" if doc_dir else token
    norm = os.path.normpath(joined).replace(os.sep, "/")
    if norm == ".." or norm.startswith("../"):
        return None
    # `normpath` drops a trailing slash, and the slash is load-bearing for rung 2: a `.gitignore`
    # rule written `dir/` matches only a DIRECTORY, and `git check-ignore` reads a slash-less path
    # that does not exist as a file. So `../../archons/stable/` (a declared-runtime directory) would
    # lose its declaration in normalisation and read as dangling. Keep the author's slash.
    if token.endswith("/") and not norm.endswith("/"):
        norm += "/"
    return norm


def candidates_for(token: str, doc_rel: str, bases: list) -> list:
    """Rung 0 — the bases to try: repo-root, then every ANCESTOR directory of the document, then any
    declared base. **Except for a leading-`../` token, which resolves against its own document and
    nothing else** — see `resolve_relative` for why the ladder must not apply there.

    The ancestor walk is what makes this work without a per-file bases table. Measured across the
    tree, 18.1% of pointers resolve relative to `seneschal/` rather than to the repo root or to their own
    directory — because a doc in `seneschal/docs/` writing `state/run-log.md` means `seneschal/state/run-log.md`,
    the subtree it is describing. The first real run of this module reported **233 such pointers as
    dangling** out of 399 findings; walking ancestors is the general rule that class was asking for,
    and it is why no single base works."""
    if _is_leading_dotdot(token):
        resolved = resolve_relative(token, doc_rel)
        return [resolved] if resolved else []
    out = [token]
    doc_dir = os.path.dirname(doc_rel)
    while doc_dir:
        out.append(f"{doc_dir}/{token}")
        doc_dir = os.path.dirname(doc_dir)
    for base in bases:
        out.append(f"{base.rstrip('/')}/{token}")
    seen, uniq = set(), []
    for c in out:
        c = c.replace("//", "/")
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def resolves_tracked(cand: str, tracked: set, root: str) -> bool:
    pattern = expand(cand)
    if pattern != cand or "*" in pattern:
        if glob.glob(os.path.join(root, pattern.replace("/", os.sep)), recursive=True):
            return True
        return any(_fnmatch_tracked(pattern, tracked))
    if cand in tracked:
        return True
    prefix = cand.rstrip("/") + "/"
    return any(t.startswith(prefix) for t in tracked)


def _fnmatch_tracked(pattern: str, tracked: set):
    import fnmatch
    for t in tracked:
        if fnmatch.fnmatch(t, pattern):
            yield True


def hint_for(token: str, tracked: set) -> str | None:
    """A basename match, offered only when EXACTLY one candidate exists. Suggestion, never an edit —
    a check that rewrites prose will one day rewrite the wrong prose."""
    base = os.path.basename(token)
    if not base or "*" in base:
        return None
    hits = [t for t in tracked if os.path.basename(t) == base]
    if len(hits) == 1 and hits[0] != token:
        return hits[0]
    close = difflib.get_close_matches(token, list(tracked), n=1, cutoff=0.9)
    return close[0] if close and close[0] != token else None


# --------------------------------------------------------------------------- scan

def iter_pointers(text: str):
    """(line_no, token) for every markdown link target and backticked token. Bare prose paths are
    deliberately NOT recognised — opening the recogniser to them multiplies the candidate set by ~10x
    for a style this repo does not write, and false positives kill adoption faster than false
    negatives."""
    for i, line in enumerate(text.splitlines(), 1):
        for m in MD_LINK_RE.finditer(line):
            yield i, m.group(1).split("#", 1)[0].strip()
        for m in BACKTICK_RE.finditer(line):
            yield i, m.group(1).strip()


def scan(root: str | None = None) -> dict:
    root = root or REPO_ROOT
    try:
        with open(os.path.join(root, ALLOWLIST_FILE), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    doc_bases = cfg.get("bases", {})
    allow = {e["pointer"] for e in cfg.get("allow", []) if isinstance(e, dict) and e.get("pointer")}

    # Rung 1 reads the WORKING TREE, not the index alone — see the docstring's rung list and
    # `gate_git.py` for why.
    tracked = tracked_paths(root) | set(gate_git.untracked_paths(root))
    files = []
    for pattern in SCAN_GLOBS:
        for hit in glob.glob(os.path.join(root, pattern.replace("/", os.sep)), recursive=True):
            rel = os.path.relpath(hit, root).replace(os.sep, "/")
            if os.path.isfile(hit) and rel in tracked:
                files.append(rel)
    files = sorted(set(files))

    pending, findings = [], []
    stale_allow: set = set()
    stats = {"files": len(files), "tokens": 0, "candidates": 0,
             "skipped": {}, "tracked": 0, "ignored": 0, "allowlisted": 0}

    for rel in files:
        try:
            with open(os.path.join(root, rel.replace("/", os.sep)), encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        for line_no, token in iter_pointers(text):
            stats["tokens"] += 1
            reason = should_skip(token)
            if reason:
                stats["skipped"][reason] = stats["skipped"].get(reason, 0) + 1
                continue
            stats["candidates"] += 1
            bases = doc_bases.get(rel, [])
            if token in allow:
                stats["allowlisted"] += 1
                # A row whose pointer now resolves on its own is dead weight — typically a forward
                # pointer whose target has since landed. Named in the report so it gets deleted;
                # never a finding, because an unneeded exemption hides nothing.
                if any(resolves_tracked(c, tracked, root) for c in candidates_for(token, rel, bases)):
                    stale_allow.add(token)
                continue
            cands = candidates_for(token, rel, bases)
            if not cands:
                # A leading-`../` token that normalises above the repo root — `../sibling-repo` from a
                # doc at depth 1 is the sibling repo. External after all, but only answerable here,
                # where the containing document is known. Counted as the ruling it is, not reported.
                stats["candidates"] -= 1
                stats["skipped"]["external"] = stats["skipped"].get("external", 0) + 1
                continue
            if any(resolves_tracked(c, tracked, root) for c in cands):
                stats["tracked"] += 1
                continue
            pending.append({"file": rel, "line": line_no, "pointer": token, "candidates": cands})

    # Rung 2 in ONE batch — see the two traps.
    to_test = [c for row in pending for c in row["candidates"]]
    ignored = check_ignored(root, to_test)
    for row in pending:
        if any(c in ignored for c in row["candidates"]):
            stats["ignored"] += 1
            continue
        row["hint"] = hint_for(row["pointer"], tracked)
        findings.append(row)

    stats["dangling"] = len(findings)
    return {"stats": stats, "findings": findings, "stale_allow": sorted(stale_allow)}


def render(result: dict) -> str:
    s = result["stats"]
    lines = [f"context-pointers: {s['files']} files · {s['tokens']:,} tokens examined · "
             f"{s['candidates']} path-shaped candidates"]
    lines.append(f"  resolved: {s['tracked']} tracked · {s['ignored']} declared-runtime · "
                 f"{s['allowlisted']} allowlisted")
    if s["skipped"]:
        skipped = " · ".join(f"{k} {v}" for k, v in sorted(s["skipped"].items(), key=lambda kv: -kv[1])
                             if k != "not path-shaped")
        lines.append(f"  skipped by ruling: {skipped}")
    for row in result["findings"]:
        lines.append(f"\n{row['file']}:{row['line']}")
        lines.append(f"    pointer: `{row['pointer']}`")
        lines.append(f"    tried:   {' | '.join(row['candidates'])}")
        lines.append("    reason:  not tracked; not covered by any .gitignore rule; not allowlisted")
        if row.get("hint"):
            lines.append(f"    hint:    did you mean `{row['hint']}`?")
    if not result["findings"]:
        lines.append("  no dangling pointers.")
    stale = result.get("stale_allow") or []
    if stale:
        lines.append(f"\n  note: allowlist entries in {ALLOWLIST_FILE.replace(os.sep, '/')} that now "
                     f"resolve on their own ({len(stale)}) — delete them (report-only, never a "
                     f"finding):")
        lines.extend(f"    `{p}`" for p in stale)
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Dangling pointers in the routed grounding (CI runs --enforce).")
    p.add_argument("--root", default=REPO_ROOT)
    p.add_argument("--json", action="store_true")
    p.add_argument("--enforce", action="store_true",
                   help="exit 1 on any dangling pointer (what CI runs; without it this only reports)")
    args = p.parse_args(argv)

    try:
        result = scan(args.root)
    except OracleFailure as exc:
        # Loud, and non-zero even without --enforce: a broken oracle is not a clean tree, and the
        # whole point of traps 1-2 is that this failure is invisible unless it shouts.
        print(f"ORACLE FAILURE: {exc}\n  (refusing to report — a partial check-ignore batch reports "
              f"real runtime paths as dangling)", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2) if args.json else render(result))
    n = result["stats"]["dangling"]
    if n and not args.enforce:
        print(f"\n  [report-only] {n} dangling pointer(s) reported, exit 0 without --enforce.")
    return 1 if (args.enforce and n) else 0


if __name__ == "__main__":
    sys.exit(main())
