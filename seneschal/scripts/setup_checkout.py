#!/usr/bin/env python3
"""Is this checkout a git worktree? — the /setup wizard's checkout guard.

Every artifact the wizard writes is gitignored (``persona/identity.json``,
``persona/owner-profile.md``, ``seneschal/state/*``, every ``*.env``) and the daemon
chapter pins absolute paths into its registrations. Run from a **linked worktree** —
which Claude Code's desktop app creates automatically under ``.claude/worktrees/`` —
all of that lands in a disposable folder the app may clean up, not in the checkout
the owner thinks they set up. This module detects that case so preflight can stop and
say so, and records the owner's explicit "use this worktree" in the ledger so the
daemon chapter (and ``render_units.py --apply``) can refuse an unaccepted one.

Detection: ``git rev-parse --git-dir`` differs from ``--git-common-dir`` exactly when
the working tree is a linked worktree. Not a git checkout at all (a tarball) → not a
worktree. Never raises.

Ledger record (``setup-state.json`` top-level ``checkout``)::

    {"worktree": true, "path": "<this worktree>", "main_checkout": "<main checkout>",
     "accepted": true, "checked_at": "..."}

CLI::

    python setup_checkout.py [--root DIR] [--state-file F] check     # JSON verdict; exit 0
    python setup_checkout.py ... accept                              # record "use this worktree"
    python setup_checkout.py ... require                             # exit 3 = unaccepted worktree
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import setup_state

EXIT_UNACCEPTED_WORKTREE = 3


def _rev_parse(root: Path, *flags: str) -> list[str] | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", *flags],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _resolve(root: Path, p: str) -> Path:
    path = Path(p)
    return (path if path.is_absolute() else root / path).resolve()


def detect(root: Path | str = setup_state.REPO_ROOT) -> dict:
    """``{"git": bool, "worktree": bool, "path": str, "main_checkout": str|None}``. Never raises."""
    root = Path(root).resolve()
    out = _rev_parse(root, "--show-toplevel", "--git-dir", "--git-common-dir")
    if not out or len(out) != 3:
        return {"git": False, "worktree": False, "path": str(root), "main_checkout": None}
    toplevel, git_dir, common_dir = Path(out[0]).resolve(), _resolve(root, out[1]), _resolve(root, out[2])
    worktree = git_dir != common_dir
    # A non-bare main checkout keeps its common dir at <main>/.git; a bare one has no work tree.
    main = common_dir.parent if common_dir.name == ".git" else None
    return {
        "git": True,
        "worktree": worktree,
        "path": str(toplevel),
        "main_checkout": str(main) if (worktree and main) else (str(toplevel) if not worktree else None),
    }


def accepted(state: dict, verdict: dict) -> bool:
    """True when this verdict is not a worktree, or the ledger accepted THIS worktree path."""
    if not verdict.get("worktree"):
        return True
    rec = state.get("checkout") or {}
    return bool(rec.get("accepted")) and rec.get("path") == verdict.get("path")


def record(state: dict, verdict: dict, accept: bool = False) -> dict:
    """Write the verdict into the ledger's ``checkout`` block (acceptance is sticky per path)."""
    prev = state.get("checkout") or {}
    keep = bool(prev.get("accepted")) and prev.get("path") == verdict.get("path")
    rec = {
        "worktree": bool(verdict.get("worktree")),
        "path": verdict.get("path"),
        "main_checkout": verdict.get("main_checkout"),
        "accepted": bool(verdict.get("worktree")) and (accept or keep),
        "checked_at": setup_state._now(),
    }
    state["checkout"] = rec
    return rec


def refusal(verdict: dict) -> str:
    return (
        f"You're running inside a git worktree at {verdict['path']}. Setup writes gitignored "
        f"files here and they won't be in {verdict.get('main_checkout') or 'the main checkout'}. "
        "Run /setup from the main checkout instead, or type 'use this worktree' to proceed."
    )


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Detect (and record acceptance of) a git-worktree checkout.")
    p.add_argument("--root", default=str(setup_state.REPO_ROOT), help="checkout to inspect")
    p.add_argument("--state-file", default=None, help="ledger path (default: <root>/seneschal/state/setup-state.json)")
    p.add_argument("cmd", choices=("check", "accept", "require"))
    args = p.parse_args(argv[1:])

    root = Path(args.root)
    state_file = Path(args.state_file) if args.state_file else root / "seneschal" / "state" / "setup-state.json"
    verdict = detect(root)
    state = setup_state.load(state_file)

    if args.cmd == "require":
        ok = accepted(state, verdict)
        if not ok:
            print(refusal(verdict), file=sys.stderr)
        print(json.dumps({**verdict, "accepted": ok}))
        return 0 if ok else EXIT_UNACCEPTED_WORKTREE

    if args.cmd == "accept" and not verdict["worktree"]:
        print(json.dumps({**verdict, "accepted": False, "note": "not a worktree — nothing to accept"}))
        return 0

    rec = record(state, verdict, accept=(args.cmd == "accept"))
    setup_state.save(state, state_file)
    out = {**verdict, "accepted": rec["accepted"]}
    if verdict["worktree"] and not rec["accepted"]:
        out["message"] = refusal(verdict)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
