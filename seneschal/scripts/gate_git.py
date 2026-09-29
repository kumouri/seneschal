#!/usr/bin/env python3
"""gate_git.py — the ONE place a local check script asks git "what did this branch change?". Stdlib.

## Why this module exists

A gate that builds its diff as `git diff <merge-base>..HEAD` sees **committed changes only**. Run
BEFORE committing — the natural order: gates green, then commit, then push — it is shown an empty
diff, passes vacuously, and the commit then makes the offending change visible to CI, which goes red
on the very gate that just passed locally. Nothing changed between the two runs except `git commit`.

**A local gate must see the WORKING TREE** — staged, unstaged and untracked-but-not-ignored — because
that is the tree the author is about to commit. On CI the checkout is clean, so every function here
answers exactly what the old `..HEAD` form answered; the difference exists only where it matters.

## The three questions, and the shapes that answer them

* `changed_paths(root, base, *pathspecs)` — `git diff --name-only <base> -- <pathspecs>` (no
  `..HEAD`, so the index and the working tree both count) **plus** `git ls-files --others
  --exclude-standard`, because a brand-new file the author has not `git add`ed appears in no diff at
  all and is still about to be committed.
* `added_lines_diff(root, base, *pathspecs)` — `git diff -U0 --no-color <base> -- <pathspecs>` with
  a **synthetic new-file hunk appended for every untracked file** matching the pathspecs, in git's
  own `--- /dev/null` / `+++ b/<path>` / `+line` shape, so a caller's existing `+`-line parser reads
  it unchanged. Bytes are decoded as UTF-8 with replacement — the callers scan prose for words, and
  a `text=True` handle would use the platform's legacy code page on Windows.
* `working_tree_paths(root, *pathspecs)` — `git ls-files` ∪ `git ls-files --others
  --exclude-standard`: every path that will be tracked once the author commits what is in front of
  them. A tree-scanning gate that enumerates `git ls-files` alone reads an un-added `test_x.py` as
  missing (so a pointer to it looks dangling) and never scans an un-added script at all.

`working_tree_summary(root)` is the count a local CI runner can print so a future reader can tell
which tree a run checked: `(modified, untracked)` from `git status --porcelain --untracked-files=all`.

## Degrades to no-answer, never to a guess

Every function returns an empty result when git cannot answer. `--exclude-standard` honours
`.gitignore`, so `seneschal/state/`, `*.env` and the rest of the runtime cache never enter a scan.
"""
from __future__ import annotations

import os
import subprocess

GIT = ["git", "-c", "core.fsmonitor=false"]


def git(root: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess | None:
    """One git invocation, bytes out, `None` if git could not run at all (as opposed to exiting
    non-zero, which the caller judges — a merge-base miss and a real failure are different answers)."""
    try:
        return subprocess.run(GIT + list(args), cwd=root, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _lines(proc: subprocess.CompletedProcess | None) -> list:
    if proc is None or proc.returncode != 0:
        return []
    text = proc.stdout.decode("utf-8", "replace")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def untracked_paths(root: str, *pathspecs: str) -> list:
    """Untracked, NOT ignored paths — the files the author has written but not `git add`ed. Sorted,
    repo-relative, forward-slashed. Empty in a clean checkout, which is what CI has."""
    args = ["ls-files", "--others", "--exclude-standard"]
    if pathspecs:
        args += ["--", *pathspecs]
    return sorted(_lines(git(root, *args)))


def tracked_paths(root: str, *pathspecs: str) -> list:
    args = ["ls-files"]
    if pathspecs:
        args += ["--", *pathspecs]
    return sorted(_lines(git(root, *args)))


def working_tree_paths(root: str, *pathspecs: str) -> set:
    """Tracked ∪ untracked-not-ignored: every path that exists in the tree the author is about to
    commit. A tree-scanning gate that enumerated `git ls-files` alone reads a clean CI checkout
    identically and a dirty local one blind to its newest files."""
    return set(tracked_paths(root, *pathspecs)) | set(untracked_paths(root, *pathspecs))


def changed_paths(root: str, base: str, *pathspecs: str) -> list:
    """Repo-relative paths that differ between `base` and the WORKING TREE — index and unstaged
    edits both, plus untracked-not-ignored files. Sorted, forward-slashed. Never `<base>..HEAD`:
    that form is what lets a change slip past a green pre-commit local run."""
    args = ["diff", "--name-only", base]
    if pathspecs:
        args += ["--", *pathspecs]
    proc = git(root, *args)
    if proc is None or proc.returncode != 0:
        return []
    return sorted(set(_lines(proc)) | set(untracked_paths(root, *pathspecs)))


def _synthetic_new_file_hunk(root: str, rel: str) -> str:
    """`git diff`'s own new-file shape for a file git has never seen, so a `+`-line parser written
    against real diff output reads an untracked file without a second code path."""
    try:
        with open(os.path.join(root, rel.replace("/", os.sep)), "rb") as fh:
            body = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = body.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return ""
    head = (f"diff --git a/{rel} b/{rel}\nnew file mode 100644\n--- /dev/null\n+++ b/{rel}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n")
    return head + "".join("+" + ln + "\n" for ln in lines)


def added_lines_diff(root: str, base: str, *pathspecs: str) -> str | None:
    """`git diff -U0 --no-color <base> -- <pathspecs>` against the WORKING TREE, followed by a
    synthetic new-file hunk per untracked file matching the pathspecs. `None` when the diff itself
    could not be produced (a bad base) — distinct from an empty string, which is a clean tree."""
    args = ["diff", "-U0", "--no-color", base]
    if pathspecs:
        args += ["--", *pathspecs]
    proc = git(root, *args)
    if proc is None or proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", "replace")
    if out and not out.endswith("\n"):
        out += "\n"
    for rel in untracked_paths(root, *pathspecs):
        out += _synthetic_new_file_hunk(root, rel)
    return out


def parse_porcelain(text: str) -> tuple:
    """`(modified, untracked)` from `git status --porcelain` output. A porcelain record is two
    status characters, a space, a path — anything else on a line is not a record and is ignored,
    so a fake or a stray banner cannot inflate the count."""
    modified = untracked = 0
    for raw in text.splitlines():
        if len(raw) < 4 or raw[2] != " ":
            continue
        if raw.startswith("??"):
            untracked += 1
        else:
            modified += 1
    return modified, untracked


def working_tree_summary(root: str) -> tuple | None:
    """`(modified, untracked)` — tracked paths with a staged or unstaged difference from HEAD, and
    untracked-not-ignored files. `(0, 0)` is a clean tree; `None` means git could not say."""
    proc = git(root, "status", "--porcelain", "--untracked-files=all")
    if proc is None or proc.returncode != 0:
        return None
    return parse_porcelain(proc.stdout.decode("utf-8", "replace"))
