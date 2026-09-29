#!/usr/bin/env python3
"""Watch a PR's CI to a terminal verdict, then exit. Stdlib + the `gh` CLI.

**Why this exists.** From a warm session that winds down on idle, "I'll watch CI and tell you when
it's green" is hollow — nothing wakes the assistant, so it collapses into "waiting for the owner to
poke me." This is the polling half of the fix; `jobs.py` is the durable half. Run as a background job,
it survives the session that started it and the daemon reports its verdict:

    python seneschal/scripts/jobs.py start --title "CI: PR #214" --wake -- \
        python seneschal/scripts/watch_pr.py 214 --repo owner/name

**What it does NOT do: merge.** It watches and reports, full stop. The standing rule is that a red or
pending PR is never merged — no exceptions, no self-waiving — so the decision stays the owner's (or,
for Dream's own self-merging proposal PRs, stays with the narrow fail-closed carve-out documented in
`seneschal/SKILL.md`). A watcher that could merge would be a watcher that could merge something red.

**What it DOES do: ask.** On a terminal **green** verdict this hands the PR to
`merge_guard.ask_on_green`, which sends the owner the Approve / Not now picker if — and only if — the
guard would block that PR. A guard whose approval path exists but that nothing ever invokes is a gate
with no door; sending the picker the moment a PR turns green is what makes the gate usable.

**On by default, and that is the design rather than an oversight.** A flag an agent has to remember
is the same failure as an agent having to remember to ask, which is the failure being fixed; making
the *gate* the classifier instead of the flag is what keeps it from being a buzzer. `--no-ask-on-green`
turns it off for a watcher started on a PR nobody intends to merge. Nothing about it can widen what
merges: it writes no approval, and a send that fails leaves the guard refusing exactly as it does
otherwise. **It never changes this script's exit code** — the CI verdict is the deliverable, and an
ask that failed may not turn a green report red.

**Reading the rollup.** `gh pr view <n> --json statusCheckRollup` — rather than `gh pr checks --watch`
(which blocks in a way that's awkward to bound and deadline). Two node shapes come back and both are
handled:

  * `CheckRun`      — `status` COMPLETED/IN_PROGRESS/QUEUED + `conclusion` SUCCESS/FAILURE/…
  * `StatusContext` — a legacy commit status with just `state` SUCCESS/PENDING/FAILURE/…

Terminal = every node has finished. **Pending is never terminal and never green** — it's unknown, and
unknown fails closed (the same rule Dream's merge discipline uses).

**An EMPTY rollup is not green either.** A PR opened seconds ago has no checks attached yet, and that
is indistinguishable from a repo with no CI configured. Reading an empty rollup as a pass reports ✅ in
one second on a PR whose CI has not started — the precise class of cheerful lie the jobs feature exists
to prevent. So an empty rollup is treated as **pending** for `--empty-grace-sec`, and if it is still
empty after that, it reports "no checks appeared" and exits **2 (no verdict)** — never 0. Absence of
evidence is not a pass.

Exit codes: **0** every check passed · **1** at least one failed · **2** no verdict (still pending at
the deadline, or no checks ever appeared) · **3** couldn't read the PR at all (no `gh`, no auth, no
such PR). A non-zero exit is what makes `jobs.py` report ❌ rather than ✅, so these are load-bearing.

USAGE:
  python watch_pr.py 214 --repo owner/name [--interval-sec 30] [--deadline-sec 3600]
                         [--empty-grace-sec 300] [--once] [--no-ask-on-green] [--state-dir …]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling import also works when we're imported, not run

DEFAULT_INTERVAL_SEC = 30
DEFAULT_DEADLINE_SEC = 60 * 60
# How long an empty rollup is read as "checks haven't attached yet" rather than "this repo has no CI".
# GitHub can take a minute or two to register workflow runs on a fresh PR.
DEFAULT_EMPTY_GRACE_SEC = 5 * 60

# Conclusions that are finished-and-fine. NEUTRAL/SKIPPED are not failures (a skipped path-filtered
# job is the normal state for, say, a mobile-build workflow on a Python-only PR).
_OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}
# Everything else that is finished is a failure: FAILURE, TIMED_OUT, CANCELLED, ACTION_REQUIRED,
# STARTUP_FAILURE, STALE. Enumerated as "not OK" rather than listed, so a conclusion GitHub adds
# later reads as a failure instead of being silently treated as a pass.


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fetch_rollup(pr: str, repo: str | None = None, runner=subprocess.run) -> tuple[list | None, str]:
    """Return (nodes, error). `nodes` is None when the PR couldn't be read at all — deliberately
    distinct from an empty list, which legitimately means "no checks configured on this PR."

    `runner` is injectable so tests never shell out to a real `gh`."""
    cmd = ["gh", "pr", "view", str(pr), "--json", "statusCheckRollup,state,title"]
    if repo:
        cmd += ["--repo", repo]
    try:
        res = runner(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"gh failed: {e}"
    if res.returncode != 0:
        return None, (res.stderr or res.stdout or "gh returned non-zero").strip()[:300]
    try:
        data = json.loads(res.stdout)
    except (ValueError, TypeError) as e:
        return None, f"unreadable gh output: {e}"
    nodes = data.get("statusCheckRollup")
    if nodes is None:
        return [], ""
    if not isinstance(nodes, list):
        return None, "statusCheckRollup was not a list"
    return nodes, ""


def classify(nodes: list) -> dict:
    """Fold the rollup into {done, passed, failed, pending, failing_names}. Pure — the whole verdict
    is decided here, so the tests never need a clock or a subprocess."""
    passed, failed, pending, failing = 0, 0, 0, []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = node.get("name") or node.get("context") or "(unnamed check)"
        if (node.get("__typename") == "StatusContext"
                or ("state" in node and "status" not in node)):
            state = (node.get("state") or "").upper()
            if state in ("PENDING", "EXPECTED", ""):
                pending += 1
            elif state in _OK:
                passed += 1
            else:
                failed += 1
                failing.append(f"{name} ({state})")
            continue
        status = (node.get("status") or "").upper()
        if status != "COMPLETED":
            pending += 1
            continue
        conclusion = (node.get("conclusion") or "").upper()
        if conclusion in _OK:
            passed += 1
        else:
            failed += 1
            failing.append(f"{name} ({conclusion or 'no conclusion'})")
    return {"done": pending == 0, "passed": passed, "failed": failed, "pending": pending,
            "failing_names": failing, "empty": not any(isinstance(n, dict) for n in nodes)}


def summary_line(pr: str, verdict: dict) -> str:
    """The one line `jobs.py` quotes back as "Last line:" in the completion push — so it has to state
    the verdict on its own, without the reader opening the log."""
    if verdict["failed"]:
        names = ", ".join(verdict["failing_names"][:4])
        more = "" if len(verdict["failing_names"]) <= 4 else f" (+{len(verdict['failing_names']) - 4} more)"
        return f"PR #{pr} CI RED — {verdict['failed']} failing: {names}{more}"
    if verdict["empty"]:
        return f"PR #{pr} — NO CI checks ever appeared, so nothing was verified (not a pass)"
    if not verdict["done"]:
        return f"PR #{pr} CI still pending ({verdict['pending']} unfinished) — no verdict"
    return f"PR #{pr} CI GREEN — all {verdict['passed']} checks passed"


def ask_on_green(pr: str, repo: str | None = None, state_dir: str | None = None,
                 out=print, asker=None) -> dict:
    """Hand a green PR to `merge_guard.ask_on_green`, which decides whether it deserves a picker.

    **The decision is entirely the guard's** — this function contributes no policy, deliberately: a
    second opinion here about what "changes functionality" means is a second classifier that can
    drift from the one doing the blocking. All that lives here is *when* to ask (green, terminal) and
    the promise that asking cannot cost the verdict.

    Wrapped end to end, because `watch()`'s return value is the deliverable. An import that fails, a
    guard that raises, a Telegram outage — each costs the ask and nothing else, and each says so on
    stdout rather than passing quietly. `asker` is the test seam, so no test reaches the real guard's
    subprocess.

    **A MISSING `repo` IS A SKIP, NOT A DEFAULT.** The guard never infers a repository, and this is a
    second door into its ask path: with `repo=None` it would hand the guard nothing, which makes
    `gh pr view <n>` resolve the number against whichever repository this watcher happened to be
    started in — a picker for a stranger's PR with the same number, one tap from being approved. So
    an unnamed repository declines to ask and says so, which is the same polarity as everything else
    here — an ask may never turn a green report red, and a skip costs one ask while a wrong ask costs
    one of the owner's taps. `main` makes the flag required, so the only way to reach this branch is
    a caller in-process."""
    if not repo:
        out(f"[{_stamp()}] no approval picker — #{pr} named no repository, and the merge guard "
            f"does not infer one. Re-run with --repo <owner>/<name>.")
        return {"ok": False, "sent": False,
                "reason": "no repository was named, and a PR number is not an identity"}
    try:
        if asker is None:
            import merge_guard
            asker = merge_guard.ask_on_green
            state_dir = state_dir or merge_guard.DEFAULT_STATE_DIR
        res = asker(int(pr), repo=repo, state_dir=state_dir)
    except Exception as e:  # noqa: BLE001 — an ask may never turn a green report red
        out(f"[{_stamp()}] could not ask about #{pr}: {e!r}")
        return {"ok": False, "sent": False, "reason": repr(e)}
    if res.get("sent"):
        out(f"[{_stamp()}] approval picker sent — {res.get('reason')}")
    else:
        out(f"[{_stamp()}] no approval picker — {res.get('reason')}")
    return res


def watch(pr: str, repo: str | None = None, interval_sec: int = DEFAULT_INTERVAL_SEC,
          deadline_sec: int = DEFAULT_DEADLINE_SEC, once: bool = False,
          empty_grace_sec: int = DEFAULT_EMPTY_GRACE_SEC,
          runner=subprocess.run, sleep=time.sleep, clock=time.monotonic,
          out=print, ask=True, state_dir: str | None = None, asker=None) -> int:
    started = clock()
    while True:
        nodes, err = fetch_rollup(pr, repo, runner=runner)
        if nodes is None:
            out(f"[{_stamp()}] could not read PR #{pr}: {err}")
            return 3
        verdict = classify(nodes)
        elapsed = clock() - started
        out(f"[{_stamp()}] passed={verdict['passed']} failed={verdict['failed']} "
            f"pending={verdict['pending']}{' (no checks yet)' if verdict['empty'] else ''}")
        # An empty rollup within the grace period means "CI hasn't attached checks yet", not "green".
        waiting_for_checks = verdict["empty"] and elapsed < empty_grace_sec and not once
        if (verdict["done"] or once) and not waiting_for_checks:
            out(summary_line(pr, verdict))
            if verdict["failed"]:
                return 1
            # No checks, or still pending — either way there is no verdict, and no verdict is not a
            # pass. Fail closed: only an actually-green rollup returns 0.
            if verdict["done"] and not verdict["empty"]:
                # GREEN, and only here. Pending, empty and red all fall through untouched — the whole
                # point of the ask is that CI has actually finished and passed.
                if ask:
                    ask_on_green(pr, repo, state_dir, out=out, asker=asker)
                return 0
            return 2
        if elapsed >= deadline_sec:
            out(summary_line(pr, verdict))
            out(f"[{_stamp()}] gave up after {deadline_sec}s — CI never reached a verdict")
            return 2
        sleep(interval_sec)


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(description="Watch a PR's CI to a terminal verdict (never merges)")
    p.add_argument("pr", help="PR number")
    p.add_argument("--repo", required=True,
                   help="REQUIRED — owner/name. There is no default and no fallback to the cwd's "
                        "repo: this watcher hands green PRs to the merge guard's ask path, and that "
                        "path never infers a repository from where it was started. A PR number is "
                        "not an identity.")
    p.add_argument("--interval-sec", type=int, default=DEFAULT_INTERVAL_SEC)
    p.add_argument("--deadline-sec", type=int, default=DEFAULT_DEADLINE_SEC)
    p.add_argument("--empty-grace-sec", type=int, default=DEFAULT_EMPTY_GRACE_SEC,
                   help="how long an empty rollup counts as 'checks not attached yet' (never green)")
    p.add_argument("--once", action="store_true", help="one poll, then report whatever is known")
    p.add_argument("--no-ask-on-green", action="store_true",
                   help="do not send the owner the merge-approval picker when this PR goes green. "
                        "The ask is on by default — a flag an agent must remember is the failure it "
                        "exists to fix — and only ever fires for a PR the merge guard would block.")
    p.add_argument("--state-dir", default=None,
                   help="seneschal/state holding the approval + ask ledgers (default: the guard's "
                        "own sibling state/). Point this at the DAEMON's checkout when watching from "
                        "a worktree, or an approval the owner has already given will not be seen.")
    args = p.parse_args(argv)
    return watch(args.pr, args.repo, args.interval_sec, args.deadline_sec, args.once,
                 args.empty_grace_sec, ask=not args.no_ask_on_green, state_dir=args.state_dir)


if __name__ == "__main__":
    sys.exit(main())
