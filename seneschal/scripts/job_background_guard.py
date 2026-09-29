#!/usr/bin/env python3
"""A `PreToolUse` hook: refuse a Bash call asking to `run_in_background` inside a `jobs.py`-launched
process tree. `../docs/background-jobs-spec.md` §3.14.

**The defect this exists for** (the same stranded-work failure `job_completion.py`'s docstring
describes): a job's own `claude -p` agent launches a verification step — the test suite, most often —
with `run_in_background` and then ends its turn to wait for a completion notification. But `claude -p`
is a single-prompt process: it dies the instant the turn ends, there is no next turn to deliver the
notification to, and the backgrounded command's result is gone. The job still exits 0 with nothing
landed.

**§3.13's DETECT/RESUME/RESCUE (`job_completion.py`) already cleans up after this happens** — it is the
"mechanism outside the turn" this whole tree favours over a prose rule. This hook is the other half:
instead of diagnosing the mess afterward, it refuses the ONE tool call that causes it, before it ever
runs. The obvious alternative — "add an explicit no-background-waiting rule to the standard job
brief" — is a prose instruction inside the brief, exactly the shape that fails silently and that
`job_completion.py`'s own docstring already argues against for this same failure. A hook is code, not
a sentence the run might not honour.

**Scoped to a job process, never an ordinary interactive session.** `run_in_background` is perfectly
safe when there is a next turn to receive the notification — every ordinary multi-turn chat session.
What is never safe is pairing it with a single-prompt process that has no next turn. `jobs.py`'s
`child_env` stamps `SENESCHAL_JOB_ID` on every child it spawns (the real job command, and any
`--resume` respawn `job_completion.py` triggers) — this hook refuses ONLY when that env var is present
on ITS OWN process (inherited from the job) and the call is Bash with `tool_input.run_in_background`
truthy. Absent that var — every normal session, including one running this repo's own tests by hand —
it is a complete no-op.

**Fail-open, because this hook fires on every Bash call inside a job, and the failure it is least
allowed to have is blocking work it has no real evidence against** — while its own silent
non-firing merely falls back to `job_completion.py`'s cleanup. Exit 2 + stderr blocks; unreadable
stdin, a missing key, an unexpected schema, a raise anywhere — all exit 0, silent, and the command
proceeds.

Install (user-level `~/.claude/settings.json`, which no repo change can write):
`JOB_BACKGROUND_GUARD_SETUP.md`.
"""
from __future__ import annotations

import json
import os
import sys

GUARDED_TOOL = "Bash"

# Set by `jobs.child_env` on every child it spawns (the job's own real command, and any resume). Its
# mere presence is the whole scoping signal — see the module docstring.
JOB_ENV_VAR = "SENESCHAL_JOB_ID"

REJECTION_REASON = """\
Blocked: this Bash command asks to run in the background (run_in_background: true) inside a
delegated job.

This process is a single-prompt `claude -p` run: it ends the instant your turn ends, so a
backgrounded command can never report its result back to you -- there is no next turn to read it on.
That is exactly the mistake that strands a delegated job's work uncommitted (a test suite launched
in the background, the turn ended waiting for a result that could never arrive).

Run the command in the FOREGROUND instead -- drop run_in_background and wait for its real output --
and end your turn only once you have actually seen the result and, if it's clean, committed and
pushed."""


def should_block(tool_name, tool_input, env):
    """Should a `PreToolUse` hook refuse this call? `True` only for a Bash call requesting
    `run_in_background` while `env` carries `JOB_ENV_VAR` — see the module docstring for why the env
    var alone is the correct scope (a job process, never an ordinary session)."""
    if tool_name != GUARDED_TOOL or not isinstance(tool_input, dict):
        return False
    try:
        job_id = env.get(JOB_ENV_VAR)
    except AttributeError:  # not a mapping at all — no evidence of a job, so no block
        return False
    if not job_id:
        return False
    return bool(tool_input.get("run_in_background"))


def decide(event, env):
    """One `PreToolUse` event -> whether to block. Never raises on a hostile shape; a payload this
    hook cannot understand is a payload it has no opinion about."""
    if not isinstance(event, dict):
        return False
    tool_input = event.get("tool_input")
    return should_block(event.get("tool_name"), tool_input, env)


def main(stdin=None, stderr=None, env=None):
    """Hook entrypoint. `0` = allow (print nothing), `2` = block with the reason on stderr.

    The whole read-and-decide path is wrapped: the only way out of this function that is not
    `return 0` is a decision that positively identified the pattern."""
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        environ = env if env is not None else os.environ
        blocked = decide(json.loads(raw), environ) if raw and raw.strip() else False
    except Exception:  # noqa: BLE001 -- fail open by contract; see the module docstring
        return 0
    if not blocked:
        return 0
    try:
        (stderr if stderr is not None else sys.stderr).write(REJECTION_REASON + "\n")
    except Exception:  # noqa: BLE001 -- a block we cannot explain is still a correct block
        pass
    return 2


if __name__ == "__main__":
    sys.exit(main())
