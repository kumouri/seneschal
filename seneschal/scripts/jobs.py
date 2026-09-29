#!/usr/bin/env python3
"""Durable background jobs — the mechanism behind "I'll tell you when it's done." Stdlib only.

**Why this exists.** Anything the assistant spawns from inside a warm-session turn is a child of the
`claude` CLI process the daemon holds, and that process dies routinely: an idle wind-down, a turn
error, a merge reload. Half-finished work just stops — and worse, the *promise* to report back lived
in the turn that made it, so even work that survived went unreported. A promise the assistant has to
REMEMBER to keep is not a mechanism. This module makes both halves durable, in code:

  1. **The work outlives the session.** `start_job` spawns DETACHED (`DETACHED_PROCESS |
     CREATE_NEW_PROCESS_GROUP` on Windows, `start_new_session=True` on POSIX), so the child leaves the
     CLI's process tree and nothing that ends the session can reach it. **The grandchild-console
     rule:** on Windows the shim's OWN child (the real command) is spawned with `CREATE_NO_WINDOW`,
     because a console program whose parent has no console is handed a fresh visible one — a new
     terminal window per job. The flag lives on the shim's Popen, not on `start_job`'s spawn: it is
     documented as mutually exclusive with `DETACHED_PROCESS`.
  2. **The ping is owned by a reconcile pass, not by the turn.** Every job is a record on disk
     (`state/jobs/<id>.json`); `reconcile()` detects the ending and fires the completion push itself,
     so a reconciler that restarts mid-job adopts the record from disk and still lands the ping.

**The invariant: NO SILENT PATH.** Every job reaches a terminal state, and every terminal state
notifies exactly once:

  * `done` / `failed` — the shim stamped a real exit code (the normal path).
  * `timed-out`       — `deadline_sec` elapsed. Enforced by the shim (works with the daemon down) AND
                        re-checked in `reconcile` (covers the shim itself being killed).
  * `ended-unknown`   — the outcome is unreadable, and the push names which way: no PID was ever
                        recorded (`never-started`), a real process vanished with no exit stamp
                        (`pid-gone-no-stamp`), or the exit code said 0 while the attempt's own parting
                        lines declared a failure (`log-contradicts-exit-code`, see
                        `FAILURE_ASSERTIONS`). Reported as unreadable rather than guessed as success.
  * `cancelled`       — `jobs.py cancel`. Terminal and UNOVERWRITABLE: `cancel_job` claims the record
                        on disk BEFORE it kills (the kill is what wakes the shim), and `honour_cancel`
                        — inside `save_job`, the one chokepoint every writer goes through — refuses any
                        later write that relabels it. `--analyze` never fires for a cancel.

`reconcile` re-reads each record immediately before acting on `check_terminal`'s verdict, so a shim
that finished for real while an earlier record's git/`gh` checks ate wall-clock time is never
clobbered by the pass's stale snapshot (`../docs/jobs-ended-unknown-spec.md`).

**Delivery.** `reconcile(notify=...)` takes the send as an injected callback. `notified_at` is stamped
ONLY when the send landed — the one deliberately fail-CLOSED thing here, so an outage retries on the
next tick instead of eating the ping. "Exactly once" is enforced by `job_push_ledger` (claimed BEFORE
the send, exactly one writer), not by `notified_at`, which several writers touch. `delivery_of` reads a
callback's `{"ok", "ambiguous"}` phase as well as a bare bool: an ambiguous send (the request went out
and may have landed) is never re-sent. `telegram_notify` is the standalone sender behind
`jobs.py reconcile --send`. **Seam:** the daemon's own tick wiring (`presence.scheduler_task` →
`_reconcile_jobs`, passing `deliver_reply` and the wake enqueue) lands with the presence port in a later
wave and does not exist in this tree yet; until it does, schedule `jobs.py reconcile --send`.

**Why a shim.** `start_job` does not spawn the caller's argv directly; it spawns `jobs.py __run <id>`,
which runs the real command as ITS child, waits, and stamps the exit code — there is no portable way
to learn a detached process's exit code after the fact. The shim also enforces the deadline locally.
Cost: `cancel` has a process tree to kill, so the record carries both `pid` (shim) and `child_pid`, and
`kill_pid` reaps each PID's whole TREE — a job is often `pwsh` → `gradlew` → `java`, and grandchildren
left alive keep running and keep writing the log after the record says `cancelled`.

**The log is live**, because the log's descriptor is handed straight to the child — but stdout to a
file is block-buffered by C stdio, which made a healthy long job and a wedged one look identical and
lost a killed job's output. `child_env` sets `PYTHONUNBUFFERED`; a non-Python child that block-buffers
its own stdout is out of reach, and that limit is named rather than papered over.

**Liveness is conservative.** `pid_alive` never uses `os.kill(pid, 0)` on Windows (CPython maps it to
`TerminateProcess`, so the "harmless" probe would KILL the job); it uses `OpenProcess`/
`GetExitCodeProcess`, and any probe it cannot answer reads as ALIVE. That is safe because
`deadline_sec` always has a real default (6 h): the deadline, not the probe, guarantees every job
eventually reports.

**Fail-open, tolerant throughout** — `sentinel.load_json`'s posture. A missing jobs dir, a malformed
record, an unreadable log, a dead PID and a spawn failure are absorbed into "nothing to do" (or a
terminal, notifiable record) rather than raised.

**Opt-in retry, for transient failures only** (`--retry N`). A failed attempt whose output carries a
recognised `TRANSIENT_SIGNATURES` match is rescheduled instead of reported, up to N extra attempts, with
optional backoff and a wall-clock window. Three things keep it safe: it is off by default; unrecognised
output is TERMINAL first time (`timed-out`, `ended-unknown` and `cancelled` are never retried); and the
wait is state on disk (`retry-pending` + `next_attempt_at`), not a `sleep`, so a restart mid-backoff
loses nothing. The classifier reads the FAILURE END of the attempt (`_CLASSIFY_TAIL_BYTES`, floored at
the attempt's own log offset), so a blip the job recovered from long before it died cannot be matched.
`--retry` is the CALLER'S ASSERTION that the command is safe to re-run from scratch. Still exactly one
push, at the real outcome, naming the attempt count.

**The classification is recorded on every job**, retry or not: each terminal attempt writes its
verdict into `attempts[]`. Recording is not retrying — it is the data that says which jobs SHOULD have
asked for `--retry`, which collecting only from jobs that already opted in could never answer:

    jq -r 'select(.attempts[]? | .classification == "transient" and .retry_enabled == false) | .id' \
       seneschal/state/jobs/*.json

**Every job carries a return address** (`../docs/job-origin-routing-spec.md`). A field a caller must
remember to populate is not a mechanism, so `origin.session_id` is stamped automatically from
`$CLAUDE_CODE_SESSION_ID` (`--origin-session` overrides), `--goal` records one line of intent, and
`source`/`cwd`/`branch` are enrichment from the session registry — never identity. `origin.request`
answers WHO ASKED (`build_request`): `owner` + turn + message id, `owner` + turn, `assistant` + turn (a
positive claim of a self-start, carrying a `reason_class`), or `unresolved` with the reason. A wrong
attribution is worse than none, so nothing is inferred: the daemon's open-turn pointer
(`state/current-turn.json`) is verified or refused, never approximated.

**Analysis is routed back.** `--analyze` (opt-in, requires `--goal`) hands a badly-ended job's log path
and goal line — nothing else — to a fresh-eyes analyst (`job_analysis.py`), files
`state/jobs/<id>.analysis.json`, then routes it: a `--wake` synthetic inbound, or one line in
`state/session-mail/<session_id>.jsonl` for a session with no push channel (a PULL read at orientation,
with a separate per-session read cursor, and unread mail is never age-swept). Routing only ever ADDS a
delivery: the completion push has already fired and is never made conditional on a route.

**One private worktree per job, on request** (`--worktree`, `../docs/delegated-work-isolation-spec.md`).
The job runs in a disposable checkout cut from `origin/develop` (`job_worktree.py`), because the unit
of collision is the checkout, not the branch. Teardown never uses `--force`: a dirty or unpushed tree
is LEFT, recorded as `worktree_leaked`, and named in the push. Lost work costs more than disk.

**A job whose script isn't there is refused, not spawned.** `preflight_refusal` checks every argument
that is definitionally a script path, in the namespace the command will actually run in (a `System32`
bash is WSL's and sees Windows paths only under `/mnt/`), and refuses before any record is written. The
argv is never rewritten. Fail-open on unknown, fail-closed on known-bad; `--no-preflight` is for a
preflight that is wrong, never for a path that is.

**Concurrency caps** — `SOFT_CAP_PER_ORIGIN` per session and `HARD_CAP_GLOBAL` overall, counting only
jobs that make agent/model calls (`job_agent_class`; unknown counts). Enforced at the CLI, never inside
`start_job`, so an analysis or a retry respawn can never be blocked. The soft cap reserves the owner's
interactive headroom and yields to `--over-soft-cap` (stamped on the record); the hard cap has no
override. A refusal prints the in-flight set ranked by kill order, each row with its own `cancel`
line — it never cancels anything itself.

**"Terminal" is not "the work landed"** (`job_completion.py`). For a job with its own working
directory, a success-like ending is checked for uncommitted/unpushed/PR-less work; an incomplete job
is RESUMEd into its own captured session, then RESCUEd onto `rescue/<job-id>` and reported `failed` by
name. The preventive half is `job_background_guard.py`, a `PreToolUse` hook scoped by
`SENESCHAL_JOB_ID`, which `child_env(job_id)` stamps on both the shim and the job's real command
(install: `JOB_BACKGROUND_GUARD_SETUP.md`).

**Seams for testing.** Every side effect is injectable: `start_job(runner=)`, `reconcile(notify=, wake=,
now=, runner=, wt_runner=, completion_runner=, ...)`, the backoff jitter's `rand=`, and the preflight's
`exists=`/`which=`/`is_windows=`. `test_jobs.py` spawns no real process, runs no git, touches no network.

USAGE:
  python jobs.py start --title "tests" --wake -- python -m unittest discover -s seneschal/scripts
  python jobs.py start --title "implement: phase 1" --worktree -- claude -p "<brief>"
  python jobs.py start --title "research" --retry 3 --retry-backoff --retry-window 2h -- <cmd>
  python jobs.py start --title "rag refresh" --goal "why does the nightly refresh write 0 docs" -- <cmd>
  python jobs.py list [--json] [--active]
  python jobs.py status <id> [--json] [--log-lines N]
  python jobs.py cancel <id> [--why "<one line>"]
  python jobs.py reconcile          # one pass; prints the pushes it would send
  python jobs.py reconcile --send   # one pass; really sends them via Telegram (telegram.env)
  python jobs.py analyze <id> --goal "<one line>"   # fresh eyes on a failed job
  python jobs.py mail               # this session's UNREAD mail; records the read
  python jobs.py mail --peek        # ...without recording it     (--all for read mail too)
  python jobs.py prune --days 14 --mail-days 14    # the nightly GC (never sweeps unread mail)

Design + rationale: seneschal/docs/background-jobs-spec.md. Store schema: seneschal/state/README.md."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

import failures  # a durable row for a failure that would otherwise vanish silently
import job_completion  # detect/resume/rescue (§3.13); reads only — never import THIS module back
import job_pr_draft  # a PR opened by a still-running job is a draft until it ends (never imports jobs.py at module scope)
import job_push_ledger  # the "already pushed about this ending" record — the ONLY writer is below
import job_worktree  # the --worktree isolation (its own Runner seam; it must NOT import this module)
import loops  # `--loop <id>`: mark a register work-item in progress — loops.py never imports jobs.py
from sentinel import load_json, parse_iso, save_json  # hardened read/write (Windows handle collisions)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))

JOBS_DIRNAME = "jobs"
SCHEMA = "seneschal.job/1"

# A real default, not None, on purpose: the deadline is what makes "every job eventually reports"
# true even when the liveness probe can't answer (see the module docstring). 6 h is comfortably
# longer than any job we run today (a CI watch is minutes) while still bounding the worst case.
DEFAULT_DEADLINE_SEC = 6 * 3600
# Hard ceiling on how long a --lease job may hold the warm session open against its idle wind-down.
# A wedged job must not pin the session forever; a pending control (graceful restart) overrides this
# regardless of remaining lease — see `lease_active`.
LEASE_MAX_SEC = 30 * 60
# How long a `running` record with no PID is given before it's called a failed start (see
# check_terminal). Generous: it only has to outlast the gap between writing the record and the spawn.
STARTUP_GRACE_SEC = 120

RUNNING = "running"
# Waiting out a retry backoff. NOT terminal (so it never notifies) and NOT running (so no liveness
# probe fires against a shim that has already exited) — but it IS active, see ACTIVE below.
RETRY_PENDING = "retry-pending"
DONE = "done"
FAILED = "failed"
TIMED_OUT = "timed-out"
ENDED_UNKNOWN = "ended-unknown"
CANCELLED = "cancelled"
TERMINAL = (DONE, FAILED, TIMED_OUT, ENDED_UNKNOWN, CANCELLED)
# The statuses §3.13's job-completion guarantee runs DETECT against — "success-like": the exact shape
# every incident that motivated it took (exit 0, or an unreadable-but-not-obviously-failed ending).
# `failed`/`timed-out`/`cancelled` already report themselves honestly and are out of scope on purpose.
COMPLETION_CHECK_STATUSES = (DONE, ENDED_UNKNOWN)
# "Still going," for the lease guard, `list --active` and the cockpit's `jobs_active` count: a job
# sitting in its backoff has not finished, and treating it as finished would let a leased job's
# session wind down mid-retry and make the Jobs panel's active count lie.
ACTIVE = (RUNNING, RETRY_PENDING)
# The fields a CANCEL owns. Once `cancelled` is on disk, no later writer may take these back — see
# `honour_cancel`, which enforces it inside `save_job`. `exit_code` is in the list because a killed
# process still exits with a code (1, on Windows `taskkill /F`), and a top-level `exit 1` under
# `status: cancelled` is the same lie in a smaller font: `wake_text` reads that field. The per-attempt
# record keeps it, which is where an observed exit code belongs.
CANCEL_OWNED_FIELDS = ("status", "ended_at", "exit_code", "cancelled_at", "cancelled_by",
                       "cancel_reason", "cancel_request")

# --------------------------------------------------------------------------- retry knobs
# All opt-in: `retry_config` returns None for `--retry 0`, and a record with no `retry` block takes
# every non-retry code path unchanged.
RETRY_MAX_RETRIES_CAP = 10   # ceiling on --retry N — matches the Claude desktop app's own retry ceiling
RETRY_BASE_SEC = 30          # first delay (and the constant delay when --retry-backoff is off)
RETRY_CAP_SEC = 600          # no single backoff waits longer than 10 min
RETRY_JITTER_FRAC = 0.25     # ±25% so a fleet of jobs retrying off one outage doesn't thunder
# "Died implausibly fast with a recognised signature" — the canonical API-blip failure is about a
# second and a few hundred bytes. Recorded as CORROBORATION only (`fast_fail`/`confidence`): speed
# never earns a retry on its own, because a genuinely broken command also fails fast.
FAST_FAIL_SEC = 15
FAST_FAIL_BYTES = 4096
_ATTEMPT_SCAN_BYTES = 64 * 1024  # the widest span `read_log_span` hands back to any caller
# **The classifier reads the FAILURE END of an attempt, and 64 KiB is not an end.** `read_log_span`
# takes the TAIL of an attempt's span — but only once the span is bigger than the window, and at
# 64 KiB almost no attempt is. So the whole log would reach the classifier, and a transport timeout
# the job hit (and recovered from) an hour before it actually died would classify the run as
# `transient` — and `--retry` decides whether to re-run a command off exactly that field: a stale
# match is a re-run of something that did not fail transiently at all.
#
# 8 KiB is `log_tail_line`'s window — this module's existing definition of "the end of the log" — and
# the attempt's own `log_offset` is still the floor, so this can only ever NARROW what is read.
# **The direction of error is deliberate:** a signature more than 8 KiB above the end of a failing
# attempt is now missed, which classifies TERMINAL, which is the answer §7.2 already gives to
# everything it does not recognise. **The residual limit, named rather than papered over:** the bound
# is BYTES, not time. A quiet attempt whose entire output is under 8 KiB is still classified on all of
# it however many hours it spans, because a job's log carries no per-phase marker to anchor on and
# inventing one would mean parsing every producer's prose.
_CLASSIFY_TAIL_BYTES = 8 * 1024

CLASS_TRANSIENT = "transient"
CLASS_TERMINAL = "terminal"
CLASS_SUCCESS = "success"

# --------------------------------------------------------------------------- the classifier
# THE ONE LIST TO EDIT. Each entry is (name, case-insensitive regex) and each pattern must carry its
# own context — a bare `\b429\b` or a bare `timeout` would match "429 tests passed" and "test timed
# out after 5s", which is exactly the over-eager retry that would make this feature do harm.
#
# The rule, in code and in tests: **anything not matched here is TERMINAL, first time, no retry.**
# Adding a pattern is cheap; adding a loose one is not. Deliberately EXCLUDED, with reasons:
#   * bare `connection refused` / `ECONNREFUSED` — usually a local service that is genuinely down,
#     where retrying just wastes the window.
#   * bare `timeout` / `timed out` — the overwhelmingly common case is a slow test, which is terminal.
#   * bare `internal server error` — a web app's own test fixtures print it; the 5xx forms below
#     cover the real API case.
#   * bare `429` / `5\d\d` with no surrounding marker — far too easy to hit by accident.
TRANSIENT_SIGNATURES = (
    # --- the model API: 5xx, overload, rate limit ------------------------------------------------
    # The verbatim shape the Claude CLI prints for a server-side blip:
    #   API Error: 500 Internal server error. This is a server-side issue, usually temporary — …
    ("api-5xx", r"api[ _-]?error:?\s*5\d\d\b"),
    ("api-429", r"api[ _-]?error:?\s*429\b"),
    ("server-side-temporary", r"server[- ]side issue,?\s*usually temporary"),
    ("overloaded", r"\boverloaded_error\b"),
    ("http-5xx", r"\bhttp(?:/[\d.]+)?[ :]+5\d\d\b"),
    ("status-5xx", r"\bstatus(?:[ _]?code)?\s*[:=]\s*5\d\d\b"),
    ("bad-gateway", r"\bbad gateway\b"),
    ("service-unavailable", r"\bservice unavailable\b"),
    ("gateway-timeout", r"\bgateway time-?out\b"),
    ("too-many-requests", r"\btoo many requests\b"),
    ("rate-limit-error", r"\brate[ _-]?limit(?:_error|_exceeded)\b"),
    ("rate-limited", r"\brate[ _-]?limit(?:ed|\s+(?:error|exceeded|reached|hit|response))\b"),
    # --- the transport layer --------------------------------------------------------------------
    ("api-connection", r"\b(?:apiconnectionerror|apitimeouterror)\b"),
    ("conn-reset", r"\b(?:econnreset|connection reset(?: by peer)?)\b"),
    ("conn-aborted", r"\b(?:econnaborted|connection aborted)\b"),
    ("remote-disconnected",
     r"\b(?:remotedisconnected|socket hang up|server closed (?:the )?connection|"
     r"connection closed by (?:the )?(?:remote|peer))\b"),
    ("fetch-failed", r"\bfetch failed\b"),
    ("dns", r"\b(?:enotfound|eai_again|temporary failure in name resolution|"
            r"name or service not known|nodename nor servname provided)\b"),
    ("tls-handshake", r"\b(?:tls handshake|ssl handshake|sslerror|ssleoferror|"
                      r"unexpected_eof_while_reading|handshake (?:failed|failure|timeout))\b"),
    ("transport-timeout",
     r"\b(?:etimedout|esockettimedout|"
     r"(?:socket|read|connect(?:ion)?|request|handshake|tls|upstream) time(?:d)?[ -]?out|"
     r"timed out (?:while )?(?:connecting|reading))\b"),
    ("upstream", r"\bupstream (?:connect error|request timeout)\b"),
)

_TRANSIENT_RE = tuple((name, re.compile(pattern, re.IGNORECASE))
                      for name, pattern in TRANSIENT_SIGNATURES)

# ------------------------------------------------------- the exit code and the log disagree
# **A job that exited 0 while its own log declares a failure is reported as UNREADABLE, not as
# success.** The shape being caught: a wrapper script prints
#
#     launch failed: remote payload exited 1
#     events: …/launcher-events.jsonl
#
# as its parting words and then exits 0. Without this the owner is pushed a ✅, and because `--analyze`
# fires only on a bad ending, no fresh-eyes analysis is filed either. Two guarantees lost at once.
#
# **The exit code is NOT lost anywhere in this module, and this is not a propagation fix.** A wrapper
# may `exit 0` on purpose (its author may treat a gate FAIL as a verdict rather than an error), and
# every link below it propagates correctly. **So `jobs.py` cannot know the job failed**, and saying
# `failed` here would be this module overruling a command's own stated contract on the strength of
# prose.
#
# What it CAN say is that its two sources of truth disagree, which is exactly what `ended-unknown`
# already exists for (§2: *"an unreadable outcome is reported as unreadable"*). The push says so, and
# `ANALYSABLE` contains `ended-unknown`, so `--analyze` fires — the half that was silently missing.
#
# **THE ONE LIST TO EDIT, and it is guarded harder than TRANSIENT_SIGNATURES**, because a false
# positive here downgrades a job that really did finish. Three things keep it narrow, and the WINDOW
# does most of the work rather than the pattern:
#
#   1. It runs **only when the exit code is 0**. A non-zero exit is already reported honestly.
#   2. It reads only the last `_CONTRADICTION_TAIL_LINES` non-blank lines **of this attempt's own
#      span** — the wrapper's parting words, not the run. A step that failed and was retried
#      successfully has pages of output after it and cannot reach here.
#   3. A match must be a failure assertion **at column 0**, in the shape a program uses to declare
#      its own outcome. Deliberately EXCLUDED, with reasons:
#        * a mention anywhere but line-initial — `3 tests failed, 1 passed`, `see
#          docs/why-the-build-failed.md`;
#        * an INDENTED assertion — indentation is how a wrapper reports a sub-step, and a sub-step
#          failing is compatible with the job succeeding;
#        * a ZERO or `none` count — `Failures: 0`, `failed: none` are success lines;
#        * bare `error` / `ERROR` / `Traceback` — every verbose build prints them while succeeding;
#        * `exit code 1` in prose — a supervisor reporting a child's code is exactly what a working
#          supervisor does;
#        * `"ok": false` — considered and REJECTED, though such wrappers often print one. It is a
#          field-level claim, and a job whose last output is a health
#          summary or an API body legitimately carries one about something other than itself. The
#          line-initial assertion already catches this case, so the second pattern would buy nothing
#          and spend the precision this list has to keep.
FAILURE_ASSERTIONS = (
    # `launch failed: …` / `failed: …` / `build failure! …`, and nothing looser. At most ONE bare word
    # may precede it, and that word may not start with a digit — `3 tests failed, 1 passed` and
    # `retry 1 failed: connection reset` are both two tokens deep and both stay unmatched.
    ("declared-failure",
     r"(?:[^\W\d_][\w.\[\]/+-]{0,23}[ \t]+)?fail(?:ed|ures?)?[ \t]*[:!]"
     r"(?![ \t]*(?:0\b|none\b))"),
)
_FAILURE_ASSERTION_RE = tuple((name, re.compile(pattern, re.IGNORECASE))
                              for name, pattern in FAILURE_ASSERTIONS)
# The wrapper's parting words. Five, because the shape being caught is a script that reports the
# failure and then echoes one or two cleanup lines on its way out; wide enough for that, far too
# narrow for a mid-run failure that was recovered from.
_CONTRADICTION_TAIL_LINES = 5
_CONTRADICTION_SCAN_BYTES = 8 * 1024   # `log_tail_line`'s window — "the end of the log", one meaning

# Windows detached-spawn flags (the standard Win32 values). Defined locally rather than imported so
# this module stays importable by the daemon without a circular import.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
# For the SHIM's child (the real command), never for the shim itself: a console-subsystem program
# (`claude`, `gradlew.bat` via cmd.exe, python.exe) whose parent has no console gets a fresh one from
# Windows — with Windows Terminal as the default terminal, a new tab per job. CREATE_NO_WINDOW gives
# the child a hidden console its descendants inherit instead.
_CREATE_NO_WINDOW = 0x08000000

_TAIL_CHARS = 200  # of the log's last meaningful line, quoted in the completion push
_WHY_CHARS = 200   # of an operator-typed --why, quoted in the completion push. Matches _TAIL_CHARS
                   # deliberately: the log tail is this module's existing precedent for "free text
                   # quoted into a push", and both keep the head far below Telegram's 4096 limit,
                   # where a rejected sendMessage would be a DELIVERY failure rather than a cosmetic
                   # one (docs/cancel-attribution-spec.md §4.3).

# `wake_text`'s log tail: the whole point of waking the session is that it can report
# WITHOUT a second lookup, and for an `ended-unknown`/`failed` job the tail is where the job's own
# final words live. 20 lines is generous next to `_CONTRADICTION_TAIL_LINES`'s 5 (that one hunts a
# single declared-failure line; this one is read by a human) and `_WAKE_TAIL_CHARS` is the hard cap
# on the rendered result regardless of how long those lines run — both stay far below Telegram's
# 4096-char message limit even stacked with the rest of the wake text.
_WAKE_TAIL_LINES = 20
_WAKE_TAIL_SCAN_BYTES = 16 * 1024
_WAKE_TAIL_CHARS = 4000

# Which sources speak in the FIRST PERSON about a cancel (cancel-attribution-spec §5): the owner deals
# with one assistant managing many jobs, so every assistant surface says "I". The rung boundary is an
# assistant surface vs. unattributed, NOT this-session vs. another-session.
#
# `build` is deliberately absent: a session the assistant DISPATCHED is not a surface the owner is
# present at, so it speaks at rung 2 naming its checkout and branch. Adding a future first-person
# surface is a one-line change here and nowhere else.
ASSISTANT_SURFACES = frozenset({"daemon", "desktop"})
SOURCE_ENV_VAR = "SENESCHAL_SESSION_SOURCE"

# --------------------------------------------------------------------------- origin knobs
# The identity of the session that started a job lives in the ENVIRONMENT, not in the session
# registry: every hook-stamped registry entry carries `pid: 0` on purpose (session_stamp.HOOK_PID),
# and `cwd`+`branch` is provably ambiguous with `.claude/worktrees/` in play — so the registry can
# say who is live, never *which one is calling you*. `CLAUDE_CODE_SESSION_ID` is byte-identical to a
# registry filename (session_stamp passes the harness id straight through to
# sentinel._session_id_for), so env and registry already agree on the identifier; env is simply the
# copy that knows which one you are. Registry lookup is therefore ENRICHMENT only.
ORIGIN_ENV_VAR = "CLAUDE_CODE_SESSION_ID"
# "One line" is enforced where it is WRITTEN (start_job), once — not where it is read N times. A
# caller who pastes four paragraphs of hypothesis into --goal gets one line stored, which is the
# §4.1 payload contract made structural rather than aspirational.
ORIGIN_GOAL_MAX = 200
ORIGIN_KEYS = ("session_id", "source", "cwd", "branch", "goal", "stamped_by", "request")

# --------------------------------------------------------------------------- who asked (phase 5)
# `origin.session_id` says WHICH SESSION started a job. It cannot say WHO ASKED IT TO — and those are
# different questions with different answers: one warm session serves hundreds of turns, some of them
# the owner asking for work and some of them the assistant deciding on its own. `origin.request` is the
# second answer (job-origin-routing-spec §3.5), and it has exactly three shapes plus a floor:
#
#   rung 1  the owner asked, on a chat surface -> by "owner" + turn_id + message_id
#   rung 2  the owner asked, no message handle -> by "owner" + turn_id      (cockpit; a picker tap)
#   rung 3  the assistant decided this itself  -> by "assistant" + turn_id
#   rung 0  it could not be resolved           -> by "unresolved" + `why`, and NOTHING is guessed
#
# **A WRONG ATTRIBUTION IS WORSE THAN NO ATTRIBUTION**, and that is the whole design constraint: the
# field is worth having only if "who asked for this?" can be trusted, and one plausible-but-wrong link
# destroys that for every row at once. So every resolution below is a VERIFIED match or an explicit
# `unresolved` with the reason — never the nearest inbound, never a timestamp-proximity join.
# Fail-open on the job, fail-honest on the field.
REQUEST_KEYS = ("by", "surface", "turn_id", "message_id", "reason_class", "resolved_by", "why")
REQUEST_BY = ("owner", "assistant", "unresolved")
REQUEST_RESOLVERS = ("turn-pointer", "flag", "none")

# --------------------------------------------------------- WHEN rung 3 is legitimate (spec §3.5.7)
# Rung 3 says the assistant started the job itself. Three of the four classes below are WANTED
# self-starts, so this vocabulary is not a permission list: **a turn that stops to ask whether it may
# do `merge-repair`, `ruling-durability` or `spec-phase` has misread it**, and the asking is itself the
# cost being avoided.
#
# The polarity and the class-4 prohibition are the rule; the carve-up is a working classification — a
# job can carry two of these at once, and some jobs will fit none. Stamping every rung-3 job is what
# makes the partition checkable against data instead of memory (job-origin-routing-spec §3.5.7).
#
#   merge-repair       repair of something a merge broke (a dirtied PR, a reddened CI). The owner
#                      already spent the tap that asked for the merge.
#   ruling-durability  converting a decision made in chat into a tracked file. The DECISION is the
#                      owner's; only the durability is the assistant's.
#   spec-phase         building the next phase of a spec the owner has already read.
#   floated-idea       ** A VIOLATION, NOT A CATEGORY. ** The owner floated a half-formed idea and it
#                      was converted into a job. Classes 1-3 cost a pull request nobody asked for;
#                      this one costs THE CONVERSATION — the question stops being open and becomes a
#                      brief, and the answer the owner would have reached is replaced by an invented
#                      one. The tell is GRAMMATICAL: stacked alternatives, hedges, a question asked
#                      mid-sentence, a terminology tangent. An idea in exploratory syntax is not a
#                      request; the answer is a question back, not a job id.
#   unstated           the honest floor: a rung-3 job whose turn did not name a class. NOT a default
#                      into an approved one — see `normalize_request`.
#
# **It is representable BECAUSE it is forbidden.** If the only legal values were the three approved
# classes, a class-4 spawn could not be recorded as itself and would be filed as whichever approved
# class sat nearest — which makes the one thing worth seeing the one thing invisible. An
# unrepresentable failure is an undetectable one.
REQUEST_REASON_APPROVED = ("merge-repair", "ruling-durability", "spec-phase")
REQUEST_REASON_VIOLATION = "floated-idea"
REQUEST_REASON_UNSTATED = "unstated"
REQUEST_REASON_CLASSES = REQUEST_REASON_APPROVED + (REQUEST_REASON_VIOLATION,
                                                    REQUEST_REASON_UNSTATED)
REQUEST_REASON_WORDS = {
    "merge-repair": "repairing what a merge broke",
    "ruling-durability": "making a chat ruling durable",
    "spec-phase": "the next phase of a spec the owner has read",
    REQUEST_REASON_VIOLATION: "THE OWNER FLOATED IT, I BUILT IT — a violation, not a category",
    REQUEST_REASON_UNSTATED: "no class stated",
}

# --------------------------------------------------------------------------- the turn pointer
# How `jobs.py` learns which TURN it is being typed inside. The environment cannot say: a warm session
# is one long-lived `claude` process serving many turns, so `CLAUDE_CODE_SESSION_ID` is fixed for the
# session's whole life and there is no per-turn variable to inherit. The registry cannot say either
# (see ORIGIN_ENV_VAR). So the daemon leaves a pointer at the one moment it knows the answer — where it
# mints `turn_id` — and this module reads it.
#
# **One file, no keying, because the daemon runs exactly ONE warm session at a time** — the same
# invariant the daemon's warm-session bookkeeping relies on. If that ever stops being true,
# this must be replaced (keyed per session), not patched.
#
# **The pointer carries ids and never text.** No message body, no preview, no goal. A `!private` turn
# must be as safe to stamp as any other, and the cheapest way to guarantee that is for there to be no
# field a future edit could helpfully put content in.
TURN_POINTER_FILE = "current-turn.json"
TURN_POINTER_SCHEMA = "seneschal.turn-pointer/1"
# Belt and braces on staleness. A turn only matters to a job started INSIDE it, and a session can only
# act inside a turn, so an abandoned pointer is already unreachable in practice: the next turn
# overwrites it, and a job from a different session fails the session-id check. This cap covers the one
# residual path — the daemon killed mid-turn, then RESUMED onto the same session id (resume keeps it),
# with the pointer never closed. Generous, because a long turn is a real thing and a false `unresolved`
# is the cheap error here while a false attribution is the expensive one.
TURN_POINTER_MAX_AGE_SEC = 6 * 3600

# --------------------------------------------------------------------------- analysis knobs
# The staged fresh-eyes artifact, alongside `<id>.json` and `<id>.log`.
ANALYSIS_SUFFIX = ".analysis.json"
# Which outcomes are worth a second pair of eyes. `timed-out` is deliberately NOT here: a job that was
# merely too slow has no readable failure for an analyst to point at.
#
# **`cancelled` is deliberately absent** (spec §3.10). A cancel is not a failure to investigate: the
# analyst is handed a log and a goal line describing work that was *deliberately abandoned*, and it has
# no way to know that — so it would spend a real model one-shot investigating an incident that never
# happened, and file leads about it. The person who cancelled the job already knows why.
# (One line to change if that turns out to be wrong. It governs `jobs.py analyze <id>` too — a manual
# request for fresh eyes on a cancel is refused with the same sentence, deliberately: an operator who
# wants that has the log path and can read it.)
ANALYSABLE = (FAILED, ENDED_UNKNOWN)

# --------------------------------------------------------------------------- the pull mailbox
# Rung 3 of the delivery ladder. There is NO push channel to a session that is not the daemon's warm
# one: the daemon's inbound queue feeds only that session, and a desktop
# `/assistant` or a hook-stamped `build` session has no queue, no drainer and no socket. They can only
# READ. So routing to the originating session is a FILING problem plus a PULL problem — and any
# design that treated it as delivery would specify something that cannot be built.
#
# The precedent is `state/session-distillations.jsonl`, which the `/assistant` command already tells
# every session to tail at orientation. Same shape, same read moment, addressed rather than
# broadcast. (`jobs.py mail` is the read, run by a session at orientation — see the spec's §3.3.1.)
SESSION_MAIL_DIRNAME = "session-mail"
# ONE constant for both sweeps: mailbox retention matches the job retention `prune --days` uses, so
# changing it later is a one-line change and not a hunt.
RETENTION_DAYS = 14

# --------------------------------------------------------------------------- the read receipt
# Without a receipt a mailbox read writes NOTHING, and three things follow: nobody can tell
# delivered-and-read from delivered-and-ignored; every read shows everything forever, so a session
# that orients twice meets its old mail as new; and a sweep can only go by AGE ALONE, which makes a
# nightly Dream step a silent data-loss path in the one feature whose entire purpose is durable
# delivery.
#
# **THE RECEIPT IS A SEPARATE CURSOR FILE, NOT A STAMP INSIDE THE MAILBOX, AND THAT IS THE WHOLE
# DESIGN.** Stamping `read_at` onto the entries means rewriting a file that `append_session_mail` may
# be appending to at that instant — jobs finish whenever they finish — and a read-modify-write racing
# an appender LOSES AN ENTRY, which is strictly worse than the bug being fixed. A per-session cursor
# is written only by the reader and read only by the reader, so it cannot race the appender at all:
# the mailbox stays append-only and single-writer, the receipt stays build-then-`os.replace`.
#
# **The cursor is a HIGH-WATER MARK, and unread is always a SUFFIX** of the mailbox, because the
# mailbox is append-only. That is what makes the advance rule cheap to state and cheap to trust.
MAIL_CURSOR_SUFFIX = ".cursor.json"
MAIL_CURSOR_SCHEMA = "seneschal.session-mail-cursor/1"


# --------------------------------------------------------------------------- paths & records

def jobs_dir(state_dir: str) -> str:
    return os.path.join(state_dir, JOBS_DIRNAME)


def job_path(state_dir: str, job_id: str) -> str:
    return os.path.join(jobs_dir(state_dir), f"{job_id}.json")


def log_path(state_dir: str, job_id: str) -> str:
    return os.path.join(jobs_dir(state_dir), f"{job_id}.log")


def analysis_path(state_dir: str, job_id: str) -> str:
    """Where a fresh-eyes analysis of this job is filed: next to its record and its log.

    Lives here rather than in `job_analysis.py` because this module owns the jobs directory — and
    because `job_analysis` imports `jobs`, so the reverse would be a circular import. `prune` needs
    it too: an analysis must not outlive the job it is about."""
    return os.path.join(jobs_dir(state_dir), f"{job_id}{ANALYSIS_SUFFIX}")


def analysis_cwd(rec) -> str:
    """Where the ANALYSIS job runs — **chosen, never inherited from the job being analysed.**

    Inheriting `rec["cwd"]` is the defect this exists to prevent: for a `--worktree` job that **is the
    private worktree**, and `release_worktree` runs at the terminal transition, *before* the push,
    which is before `request_analysis` — so the directory is gone by the time the analyst is spawned
    (`[WinError 267] The directory name is invalid`). Teardown runs on **every** terminal state, so
    `--analyze --worktree` would fail to spawn essentially always, working only when the removal had
    been REFUSED (a leaked, dirty tree) — and because the push lands either way, nothing would look
    broken: the safety net would silently not exist for exactly the jobs most likely to need it.

    **The analyst needs nothing from that tree.** Its whole input is a log PATH and a goal line
    (`job_analysis.build_delegation`), `--state-dir` is already absolute, and `job_analysis.run_analysis`
    *already* spawns the `claude` one-shot with `cwd=jobs.REPO_ROOT`. The analysis job's own working
    directory was therefore incidental — and depending on a disposable one is the defect.

    The ladder, in order, each candidate verified to be a directory before it is taken:

      1. **`worktree.host`** — the checkout that owns the worktree, recorded by `job_worktree.create`
         and untouched by teardown. This is the host repo for a `--worktree` job even when the
         launcher lived somewhere else.
      2. **`REPO_ROOT`** — where `jobs.py` itself lives, and what a plain job's `cwd` already is.
      3. **the temp dir** — the guaranteed-to-exist floor. A missing directory must never be the
         thing that costs the analysis.

    `REPO_ROOT` is the last resort if even that fails, because there is nothing better to say and it
    is what every other spawn here already defaults to."""
    candidates = []
    wt = rec.get("worktree") if isinstance(rec, dict) else None
    if isinstance(wt, dict) and isinstance(wt.get("host"), str) and wt["host"].strip():
        candidates.append(wt["host"].strip())
    candidates.append(REPO_ROOT)
    candidates.append(tempfile.gettempdir())
    for path in candidates:
        try:
            # Checked HERE rather than trusted, immediately before `start_job` spawns: the whole bug
            # was a path that existed when it was recorded and not when it was used.
            if os.path.isdir(path):
                return os.path.abspath(path)
        except (OSError, ValueError, TypeError):
            continue
    return REPO_ROOT


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def new_job_id(now: datetime | None = None) -> str:
    """Chronologically sortable and human-readable: `20260728-205500-a1b2`. Sorting the jobs dir by
    filename therefore sorts by start time, which is what `list` and `prune` both want."""
    now = now or _utc_now()
    return f"{now.astimezone(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def load_job(state_dir: str, job_id: str) -> dict | None:
    """One record, or None if absent/corrupt/not-a-job. Never raises."""
    rec = load_json(job_path(state_dir, job_id), None)
    return rec if isinstance(rec, dict) and rec.get("id") else None


def honour_cancel(state_dir: str, rec: dict) -> dict:
    """A cancel already on disk wins over whatever `rec` is about to say. Returns the record to write.

    **This closes the lost-race cancel** (spec §3.10). `cancel_job` and the shim are two independent
    writers of the same file, and every writer here is a load-modify-save with no lock — so without
    this the outcome is decided by whichever `save_job` happens to land last, and a cancelled job can
    be recorded (and pushed) as **`failed`, exit 1**, with `cancelled_by` erased. The shim's own
    re-read guard is not enough: it re-reads BEFORE classifying the attempt and writes after, so a
    cancel that saved inside that window would simply be overwritten.

    `cancel_job`'s claim-before-kill (below) closes the ordinary path — the shim cannot wake until the
    child is killed, and by then the claim is on disk. This closes the rest of it, at write time and
    at the ONE chokepoint every writer already goes through, so the cancel wins **regardless of write
    ordering** rather than because of a lucky one.

    Only the fields a cancel OWNS are restored; everything else in `rec` is written as given. That is
    deliberate — `reconcile` still stamps `notified_at`, `release_worktree` still records the teardown,
    and the losing writer's `attempts[]` entry is KEPT as the evidence that the shim did observe an
    exit code. What it may not do is relabel the ending."""
    if not isinstance(rec, dict) or rec.get("status") == CANCELLED:
        return rec
    prior = load_job(state_dir, rec.get("id") or "")
    if prior is None or prior.get("status") != CANCELLED:
        return rec
    out = dict(rec)
    for key in CANCEL_OWNED_FIELDS:
        if key in prior:
            out[key] = prior[key]
        else:
            out.pop(key, None)
    return out


def save_job(state_dir: str, rec: dict) -> None:
    save_json(job_path(state_dir, rec["id"]), honour_cancel(state_dir, rec))


def list_jobs(state_dir: str, active_only: bool = False) -> list:
    """Every readable record, oldest first. A missing dir, a stray file, or a malformed record is
    skipped silently — this is read on the daemon's 5 s tick and must never raise.

    `active_only` means ACTIVE, not RUNNING: a job waiting out a retry backoff is still in flight."""
    out: list = []
    try:
        names = sorted(os.listdir(jobs_dir(state_dir)))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        rec = load_job(state_dir, name[: -len(".json")])
        if rec is None:
            continue
        if active_only and rec.get("status") not in ACTIVE:
            continue
        out.append(rec)
    return out


# --------------------------------------------------------------------------- origin (the address)

def one_line(text, limit: int = ORIGIN_GOAL_MAX) -> str:
    """Collapse any input to a single, capped line. Total: `None`, a number and a four-paragraph
    paste all come out as one short string, because this is the only enforcement point there is."""
    if text is None:
        return ""
    try:
        collapsed = " ".join(str(text).split())
    except Exception:  # noqa: BLE001 — a weird __str__ must not strand a job
        return ""
    return collapsed[: max(0, int(limit))].rstrip()


def _registry_entry(state_dir: str, session_id: str) -> dict | None:
    """The session registry's entry for `session_id`, or None. **Enrichment only** — `cwd`/`branch`/
    `source` make a lead list readable months later, and a job outlives the branch it started on.
    Never identity: see ORIGIN_ENV_VAR's note. Fail-open, like every other read here."""
    if not session_id:
        return None
    try:
        import sentinel  # deferred: a sentinel import problem must not break `jobs.py start`

        # The filename rule is sentinel's to own, so ask sentinel rather than re-deriving it here.
        wanted = sentinel._session_id_for("", session_id)
        for entry in sentinel.load_sessions(state_dir):
            if entry.get("session_id") in (session_id, wanted):
                return entry
    except Exception:  # noqa: BLE001
        return None
    return None


def normalise_why(value) -> str | None:
    """An operator-typed `--why`, made safe to quote into a Telegram send. Stored normalised AT THE
    WRITE, once, so every reader is safe by construction (spec §4.3).

    1. **Non-strings are rejected outright** — no coercion. `str(x)` of a stray object is how
       `<object at 0x…>` ends up in a push.
    2. **Flattened to ONE line.** Unicode `Cc`/`Cf` controls (covering newline, carriage return, tab
       and the bidi/zero-width characters) and every whitespace run collapse to a single space. This
       is load-bearing rather than tidy: `notify_text` joins its parts with newlines, so an embedded one
       would not merely wrap — it would make the tail of the reason indistinguishable from a
       `retry_note` or a `Last line:` part. A pasted multi-line log is the realistic accident here.
    3. **Bounded** to `_WHY_CHARS`, ellipsised.
    4. **Empty is ABSENT** — `--why ""`, `--why "   "`, or a value of only control characters writes
       no key at all, so the push is byte-identical to the no-`--why` case: absent degrades to
       exactly the flat copy, never to a dangling `: `.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(" " if (unicodedata.category(ch) in ("Cc", "Cf") or ch.isspace()) else ch
                      for ch in value)
    cleaned = " ".join(cleaned.split()).strip()
    if not cleaned:
        return None
    if len(cleaned) > _WHY_CHARS:
        cleaned = cleaned[:_WHY_CHARS - 1] + "…"
    return cleaned


def build_cancelled_by(state_dir: str, *, session_id: str | None = None, env=None) -> dict:
    """Who performed the cancel (spec §4.1). `origin`'s shape minus `goal`, which is a property of
    STARTING work and meaningless for stopping it.

    **A new top-level field rather than folding into `origin`, because they are different actors.**
    `origin` is the address of the session that started the job; a cancel is very often performed by
    a different session entirely (started by a warm turn, killed hours later from a build session).
    Folding them together would make `origin` mean "whoever touched this last", destroying the
    return-address property it exists to provide. **Reuse the mechanism, not the field.**

    `source` resolves `SENESCHAL_SESSION_SOURCE` -> the registry entry -> absent. The env var exists
    because the registry CANNOT answer this (spec §9.1): it is keyed by session id only for
    hook-stamped `build` entries, while the daemon's own entry is the literal `daemon.json`, so a
    lookup by UUID can return `build` or nothing and **never `daemon`** — the one value the wording
    ladder turns on.

    Fail-open throughout, and the caller must treat a raise as impossible: losing attribution is
    cosmetic, failing to cancel is real."""
    env = os.environ if env is None else env
    sid = (session_id or "").strip() or None
    stamped_by = "flag" if sid else None
    if sid is None:
        try:
            sid = (env.get(ORIGIN_ENV_VAR) or "").strip() or None
        except Exception:  # noqa: BLE001
            sid = None
        if sid:
            stamped_by = "env"
    if not sid:
        return {}
    out: dict = {"session_id": sid}
    try:
        source = (env.get(SOURCE_ENV_VAR) or "").strip() or None
    except Exception:  # noqa: BLE001
        source = None
    entry = _registry_entry(state_dir, sid) or {}
    if not source:
        source = entry.get("source") or None
    if source:
        out["source"] = source
    for key in ("cwd", "branch"):          # enrichment only, never identity
        if entry.get(key):
            out[key] = entry[key]
    out["stamped_by"] = stamped_by or "none"
    return out


def build_origin(state_dir: str, *, session_id: str | None = None, goal=None,
                 env=None, request_turn: str | None = None,
                 requested_by: str | None = None, reason_class: str | None = None,
                 now=None) -> dict:
    """The `origin` block for a new job — the return address, plus the one line of context an analyst
    needs to know what "working" would have looked like.

    ```
    origin.session_id  =  session_id (the --origin-session flag)   # explicit, wins  -> stamped_by "flag"
                       |  $CLAUDE_CODE_SESSION_ID                  # auto, the default path -> "env"
                       |  (absent)                                 # -> {} , today's behaviour exactly
    ```

    **Auto is primary on purpose.** An opt-in `origin` stays `{}` on nearly every record in practice:
    a field a caller must REMEMBER to populate is not a mechanism. `jobs.py start` is typed by a model
    mid-turn, and the model that forgets `--wake` will forget `--origin-session`.

    `request` (phase 5) is the *second* address: not which session, but **who asked**. It is attached
    only when there is already an origin to attach it to — with neither an id nor a goal this still
    returns `{}`, and a `request` block hanging off nothing would be a return address for no letter.

    Fail-open throughout: an unreadable env, an absent registry and a garbled entry all degrade to
    less context, never to an error. With neither an id nor a goal this returns `{}` — byte-for-byte
    what every caller wrote before this existed."""
    env = os.environ if env is None else env
    sid = (session_id or "").strip() or None
    stamped_by = "flag" if sid else None
    if sid is None:
        try:
            sid = (env.get(ORIGIN_ENV_VAR) or "").strip() or None
        except Exception:  # noqa: BLE001 — an exotic mapping must not cost the job
            sid = None
        if sid:
            stamped_by = "env"
    goal_line = one_line(goal)
    if not sid and not goal_line:
        return {}
    origin: dict = {}
    if sid:
        origin["session_id"] = sid
        entry = _registry_entry(state_dir, sid) or {}
        for key in ("source", "cwd", "branch"):
            val = entry.get(key)
            if val:
                origin[key] = val
    if goal_line:
        origin["goal"] = goal_line
    # Cheap provenance: it is how you find out, six weeks in, whether the auto-stamp is actually
    # firing — before anything depends on the answer.
    origin["stamped_by"] = stamped_by or "none"
    origin["request"] = build_request(state_dir, session_id=sid, turn_id=request_turn,
                                      requested_by=requested_by, reason_class=reason_class,
                                      now=now)
    return origin


def normalize_origin(origin) -> dict:
    """Whatever a caller handed us, reduced to the §3.2 shape: known keys only, one capped line of
    goal, `{}` for anything unusable. Enforced HERE — at the single write point — rather than at
    every read, and `{}` and missing stay indistinguishable, which is the rule every future reader
    of this field must follow.

    `request` is the one nested key and is normalized by `normalize_request` rather than `str()`d —
    a `str()` of a dict would write `"{'by': 'owner', …}"` into the record, which round-trips as a
    string and reads as data. An unusable `request` is dropped entirely, never coerced."""
    if not isinstance(origin, dict) or not origin:
        return {}
    out = {}
    for key in ORIGIN_KEYS:
        val = origin.get(key)
        if val is None or val == "":
            continue
        if key == "request":
            req = normalize_request(val)
            if req:
                out[key] = req
            continue
        out[key] = one_line(val) if key == "goal" else str(val)
    return out


# --------------------------------------------------------------- the requester (spec §3.5, phase 5)

def turn_pointer_path(state_dir: str) -> str:
    return os.path.join(state_dir, TURN_POINTER_FILE)


def read_turn_pointer(state_dir: str) -> dict:
    """The daemon's open-turn pointer, or `{}`. Tolerant — a missing,
    truncated, half-written or wrong-shaped file yields no answer, never an exception. That posture is
    load-bearing rather than polite: this is read on the spawn path of every job, and a stamp must
    never be why work fails to start. `load_json` already absorbs the ordinary failures (absent,
    corrupt, a Windows handle collision); the outer catch covers everything else, because the
    guarantee this owes its callers is TOTAL rather than a list of anticipated exceptions."""
    try:
        ptr = load_json(turn_pointer_path(state_dir), None)
    except Exception:  # noqa: BLE001 — the attribution, never the job
        return {}
    if not isinstance(ptr, dict) or not ptr.get("turn_id"):
        return {}
    return ptr


def write_turn_pointer(state_dir: str, *, turn_id: str, by: str, session_id: str | None = None,
                       surface: str | None = None,
                       message_id=None, message_why: str | None = None, now=None) -> bool:
    """Open the pointer for a turn. Called by the daemon where `turn_id` is minted, so the identifier a
    job stamps is **the same string** the trace panel, the interleave log and the transcript already
    key on — not a second id that has to be joined to them.

    `session_id` is allowed to be None: on a COLD spawn the CLI has not reported one yet (it arrives in
    the `system/init` event, mid-send), and `stamp_turn_pointer_session` fills it in moments later. A
    pointer with no session id resolves to `unresolved`, never to a match — an unverified pointer is
    exactly the guess this feature refuses to make.

    **`by` is REQUIRED and has no default**, deliberately. A default of `"assistant"` would be
    convenient and wrong: rung 3 is a *positive claim* that the assistant started the work itself, so
    a caller who simply forgot to say would be silently making one. An unrecognised value is stored as `"unresolved"` for
    the same reason — coercing it to either real answer invents the one thing this must never invent.

    **Never raises**: a turn that could not write its pointer costs the attribution of any job started
    in it, and nothing else. The bool is for tests and for a caller that wants to log; the drainer
    ignores it, which is the house rule for every `state/` writer here."""
    try:
        row = {"schema": TURN_POINTER_SCHEMA, "turn_id": str(turn_id),
               "session_id": str(session_id) if session_id else None,
               "surface": str(surface) if surface else None,
               "by": by if by in ("owner", "assistant") else "unresolved",
               "opened_at": _stamp(now or _utc_now()), "closed_at": None}
        if message_id not in (None, ""):
            row["message_id"] = str(message_id)
        elif message_why:
            row["message_why"] = one_line(message_why)
        save_json(turn_pointer_path(state_dir), row)
        return True
    except Exception:  # noqa: BLE001 — the pointer, never the turn
        return False


def stamp_turn_pointer_session(state_dir: str, session_id: str) -> bool:
    """Fill in the session id the pointer could not know at open time (the cold-spawn case above).

    Guarded rather than unconditional: it writes ONLY into a pointer that is still open and has no
    session id yet. A pointer that already names a session is never re-pointed at another one — that
    would be a way to hand one session's turn to a different session, which is the wrong-attribution
    failure wearing a helpful face. Never raises."""
    try:
        ptr = read_turn_pointer(state_dir)
        if not ptr or ptr.get("closed_at") or ptr.get("session_id") or not session_id:
            return False
        ptr["session_id"] = str(session_id)
        save_json(turn_pointer_path(state_dir), ptr)
        return True
    except Exception:  # noqa: BLE001
        return False


def close_turn_pointer(state_dir: str, turn_id: str | None = None, now=None) -> bool:
    """Close the pointer when the turn ends. A closed pointer resolves to `unresolved`, so this is what
    stops a job started outside any turn from inheriting the last one.

    `turn_id`, when given, must MATCH: closing by id means a late close from a turn that already lost
    the pointer to its successor cannot close the successor's. Never raises."""
    try:
        ptr = read_turn_pointer(state_dir)
        if not ptr or ptr.get("closed_at"):
            return False
        if turn_id and ptr.get("turn_id") != str(turn_id):
            return False
        ptr["closed_at"] = _stamp(now or _utc_now())
        save_json(turn_pointer_path(state_dir), ptr)
        return True
    except Exception:  # noqa: BLE001
        return False


def _pointer_refusal(ptr: dict, session_id: str | None, now=None) -> str | None:
    """Why this pointer may NOT be used for this job, as a sentence — or None when it may.

    Split out and returning a *reason* rather than a bool on purpose: the reason is the product here.
    An `unresolved` row that says which check failed is auditable; one that just says no is a shrug,
    and a shrug is what makes people start guessing again."""
    if not ptr:
        return ("no open turn pointer — this job was not started from inside a warm-session turn "
                "(a desktop or build session has no turn to point at)")
    if not session_id:
        return "this job carries no session id, so no turn could be matched to it"
    if ptr.get("by") not in ("owner", "assistant"):
        return "the open turn did not record who composed the line that opened it"
    if not ptr.get("session_id"):
        return ("the open turn has not been told its session id yet, so it cannot be matched to this "
                "job")
    if str(ptr.get("session_id")) != str(session_id):
        return "the open turn belongs to a different session than the one that started this job"
    if ptr.get("closed_at"):
        return "the last turn this session served had already ended when the job was started"
    try:
        opened = parse_iso(ptr.get("opened_at"))
    except (TypeError, ValueError, AttributeError):
        return "the open turn has no readable open time, so its freshness cannot be checked"
    age = ((now or _utc_now()) - opened).total_seconds()
    if age > TURN_POINTER_MAX_AGE_SEC or age < -60:
        return (f"the open turn is {int(age)}s old, outside the "
                f"{TURN_POINTER_MAX_AGE_SEC}s window a live turn can occupy")
    return None


def build_request(state_dir: str, *, session_id: str | None = None, turn_id: str | None = None,
                  requested_by: str | None = None, reason_class: str | None = None,
                  now=None) -> dict:
    """**Who asked for this job** — the §3.5 block. Always returns something: the honest answer when
    nothing resolves is `{"by": "unresolved", …, "why": <the reason>}`, not an absence.

    ```
    by           =  --requested-by            # asserted -> resolved_by "flag"
                 |  the verified turn pointer # matched  -> resolved_by "turn-pointer"
                 |  "unresolved"              # with `why` naming the check that failed
    turn_id      =  --request-turn  |  the verified pointer's turn  |  absent
    message_id   =  the verified pointer's, and ONLY when `by` is "owner"  |  absent + `why`
    reason_class =  --reason-class, and ONLY when `by` is "assistant"  |  "unstated"
    ```

    `reason_class` is WHY the assistant started it itself (§3.5.7) and it is a different question from
    `why`, which is why a *resolution* failed. Neither may ever be written into the other's key.
    **It is not part of `resolved_by`'s scope**, deliberately: `resolved_by` says how the IDENTITY
    fields were learned, and identity is the thing that can be verified. A reason class never can be
    — it is a judgement the turn makes about its own behaviour — so naming one must not downgrade a
    genuinely pointer-verified row to `flag`. Recorded as the assertion it is, and nothing more.

    Three properties are the whole point and an edit must keep all three:

    * **Nothing is inferred.** No nearest-inbound, no timestamp proximity, no "the only message in the
      last minute". Either the pointer passes every check in `_pointer_refusal` or the row says
      unresolved and says why.
    * **`resolved_by` is conservative.** If any field was ASSERTED by a flag it reads `flag`, even when
      another field was verified — a provenance marker that over-claims verification is worse than
      none, and this is the `stamped_by` argument one level down.
    * **A message id is never attached to an `assistant` request.** Rung 3 is a positive claim that
      the assistant started this itself; hanging one of the owner's message ids off it would say the
      opposite.
    """
    asserted_by = requested_by if requested_by in ("owner", "assistant") else None
    asserted_turn = (turn_id or "").strip() or None
    ptr = read_turn_pointer(state_dir)
    refusal = _pointer_refusal(ptr, session_id, now=now)
    out: dict = {}
    whys: list = []

    if asserted_by:
        out["by"] = asserted_by
    elif refusal is None:
        out["by"] = ptr["by"]      # _pointer_refusal already rejected anything else
    else:
        out["by"] = "unresolved"
        whys.append(refusal)

    if refusal is None and ptr.get("surface"):
        out["surface"] = str(ptr["surface"])

    if asserted_turn:
        out["turn_id"] = asserted_turn
    elif refusal is None:
        out["turn_id"] = str(ptr["turn_id"])
    elif out["by"] != "unresolved":
        # Asserted who, but nothing verified where. Say so rather than let the absence read as "there
        # was no turn" — the two are different and only one of them is a gap.
        whys.append(refusal)

    if out["by"] == "owner" and out.get("turn_id"):
        if refusal is None and ptr.get("message_id"):
            out["message_id"] = str(ptr["message_id"])
        else:
            whys.append(one_line(ptr.get("message_why")) if (refusal is None and ptr.get("message_why"))
                        else "no message id could be tied to this request")

    if out["by"] == "assistant":
        # Rung 3 ALWAYS carries a class, even when the turn named none. An absent key would make the
        # unnamed case indistinguishable from a row written before this existed, and the whole point
        # of §3.5.7 is that a self-start is countable. An unrecognised value becomes `unstated` for
        # exactly the reason `by` does: guessing it into an approved class is the failure.
        out["reason_class"] = (reason_class if reason_class in REQUEST_REASON_CLASSES
                               else REQUEST_REASON_UNSTATED)

    out["resolved_by"] = ("flag" if (asserted_by or asserted_turn)
                          else "turn-pointer" if refusal is None else "none")
    if whys:
        out["why"] = one_line("; ".join(whys), limit=ORIGIN_GOAL_MAX * 2)
    return out


def normalize_request(request) -> dict:
    """The §3.5 shape or `{}`: known keys only, `by` and `resolved_by` from their closed vocabularies,
    everything else one capped line. Enforced HERE, at the single write point, like `normalize_origin`
    — and a `by` outside the vocabulary becomes `unresolved` rather than being dropped, because a row
    with a turn id and no requester would read as a rung the record cannot support."""
    if not isinstance(request, dict) or not request:
        return {}
    out: dict = {}
    for key in REQUEST_KEYS:
        val = request.get(key)
        if val is None or val == "":
            continue
        out[key] = one_line(val, limit=ORIGIN_GOAL_MAX * 2)
    if not out:
        return {}
    if out.get("by") not in REQUEST_BY:
        out["by"] = "unresolved"
    if out.get("resolved_by") not in REQUEST_RESOLVERS:
        out["resolved_by"] = "none"
    if out["by"] == "unresolved":
        out.pop("message_id", None)   # an id with nobody behind it is the wrong-attribution shape
    if out["by"] == "assistant":
        out.pop("message_id", None)   # rung 3 says the assistant started it; an owner message says otherwise
        if out.get("reason_class") not in REQUEST_REASON_CLASSES:
            # Never coerced toward an approved class, and never dropped. A rung-3 row with no class
            # is a real state — the assistant self-started and did not say why — and it has to read as that
            # rather than as silence (§3.5.7).
            out["reason_class"] = REQUEST_REASON_UNSTATED
    else:
        # A reason class on an `owner` or `unresolved` row would claim a self-start that the `by`
        # beside it denies. One of the two is wrong, and the class is the one with no evidence.
        out.pop("reason_class", None)
    return out


def job_request(rec: dict) -> dict:
    """The `request` block of a record, tolerant of every legacy shape: no `origin`, `origin: {}`, an
    `origin` with no `request`. All of them are `{}` — the §3.4 rule that missing and empty must stay
    indistinguishable, applied one level in."""
    origin = rec.get("origin") if isinstance(rec, dict) else None
    if not isinstance(origin, dict):
        return {}
    req = origin.get("request")
    return req if isinstance(req, dict) else {}


def request_rung(rec: dict) -> int:
    """Which of the three request rungs this record actually reached — 1, 2, 3, or 0 for unresolved.

    **Derived, never stored.** A stored rung is a second source of truth that can disagree with the
    fields it summarises, and the first time it does, the summary is what someone quotes."""
    req = job_request(rec)
    by, turn = req.get("by"), req.get("turn_id")
    if by == "owner" and req.get("message_id"):
        return 1
    if by == "owner" and turn:
        return 2
    if by == "assistant" and turn:
        return 3
    return 0


REQUEST_RUNG_WORDS = {
    1: "the owner asked, in this message",
    2: "the owner asked, in this turn",
    3: "the assistant started this itself, in this turn",
    0: "who asked is unresolved",
}


def request_reason(rec: dict) -> str | None:
    """The §3.5.7 class on a rung-3 record, or None. Derived from the stored key rather than
    re-inferred: there is nothing to infer it FROM, which is the point."""
    req = job_request(rec)
    if req.get("by") != "assistant":
        return None
    cls = req.get("reason_class")
    return cls if cls in REQUEST_REASON_CLASSES else REQUEST_REASON_UNSTATED


def request_is_violation(rec: dict) -> bool:
    """Did this job convert an idea the owner was still thinking about into a brief? (§3.5.7
    class 4.)

    A one-call predicate on purpose: "when does the assistant do that?" is the question this field
    exists to answer, and an answer that requires knowing the vocabulary by heart is an answer nobody
    runs."""
    return request_reason(rec) == REQUEST_REASON_VIOLATION


def session_mail_dir(state_dir: str) -> str:
    return os.path.join(state_dir, SESSION_MAIL_DIRNAME)


def _mail_filename(session_id: str) -> str:
    """The registry's own filename rule, asked of sentinel rather than re-derived — a mailbox is
    addressed by exactly the id `state/sessions/` uses, and two rules would drift."""
    try:
        import sentinel

        return sentinel._session_id_for("", session_id)
    except Exception:  # noqa: BLE001
        return "".join(ch if (ch.isalnum() or ch in "._-") else "-"
                       for ch in str(session_id or "session"))[:80]


def session_mail_path(state_dir: str, session_id: str) -> str:
    return os.path.join(session_mail_dir(state_dir), f"{_mail_filename(session_id)}.jsonl")


def session_mail_cursor_path(state_dir: str, session_id: str) -> str:
    """The read receipt beside the mailbox it belongs to. Same filename rule for the same reason —
    one address per session — with a different suffix so the sweep can tell them apart."""
    return os.path.join(session_mail_dir(state_dir),
                        f"{_mail_filename(session_id)}{MAIL_CURSOR_SUFFIX}")


def new_mail_id() -> str:
    """A fresh entry id. Opaque on purpose: ORDER comes from position in an append-only file, never
    from the id, so nothing downstream may sort or compare these."""
    return "mail-" + uuid.uuid4().hex[:16]


def mail_entry_id(entry: dict) -> str:
    """The stable identity of one mail entry — its own `id`, or a content hash for the entries that
    predate ids.

    **The synthesized half is what makes the receipt work on mail that already exists.** An entry
    written by an appender that predates ids is real mail, not a fixture: it must be markable without
    rewriting the file it sits in. A hash of its own
    content is stable precisely because the mailbox is append-only — the line cannot change under us —
    so it needs no migration write, and the ONE thing the cursor must be able to do (name an entry and
    find it again later) works on it identically."""
    if isinstance(entry, dict):
        val = entry.get("id")
        if isinstance(val, str) and val.strip():
            return val.strip()
    try:
        blob = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = repr(entry)
    return "legacy-" + hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:16]


def append_session_mail(state_dir: str, session_id: str, entry: dict) -> bool:
    """Append one line to a session's mailbox. Returns whether it landed.

    **Append-only** — never truncate-written, per the standing rule for `state/` (it is gitignored,
    so there is no backup anywhere); only the prune sweep rewrites, and it builds-then-`os.replace`s.
    Fail-open: an unwritable mailbox costs the extra delivery, never the completion push that has
    already fired.

    **Stamps an `id` when the caller didn't**, so the read receipt has something stable
    to point at. Additive only: WHEN and WHETHER mail is filed is untouched, the caller's dict is
    copied rather than mutated, and an entry that already carries an id keeps it."""
    if not session_id or not isinstance(entry, dict):
        return False
    if not isinstance(entry.get("id"), str) or not entry["id"].strip():
        entry = dict(entry)
        entry["id"] = new_mail_id()
    try:
        os.makedirs(session_mail_dir(state_dir), exist_ok=True)
        with open(session_mail_path(state_dir, session_id), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def read_session_mail(state_dir: str, session_id: str, limit: int = 20) -> list:
    """A session's mail, oldest first, newest `limit` entries. Malformed lines are skipped — this is
    read at orientation and must never be what breaks a session.

    **Reads nothing about read-ness and writes nothing.** The receipt lives in `session_mail_view`,
    which composes this with the cursor; keeping the raw read pure is what lets `--peek` exist and
    what keeps every caller that only wants the contents free of a write."""
    out: list = []
    try:
        with open(session_mail_path(state_dir, session_id), "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out[-limit:] if limit and limit > 0 else out


def read_mail_cursor(state_dir: str, session_id: str) -> dict:
    """This session's read receipt, or `{}`. Never raises: an absent, corrupt or half-written cursor
    reads as *"nothing has been read"*, which shows mail again. That direction is deliberate — a
    re-shown entry costs a duplicate paragraph, a hidden one costs the delivery."""
    if not session_id:
        return {}
    try:
        with open(session_mail_cursor_path(state_dir, session_id), "r", encoding="utf-8") as fh:
            cursor = json.load(fh)
    except (OSError, ValueError):
        return {}
    return cursor if isinstance(cursor, dict) else {}


def write_mail_cursor(state_dir: str, session_id: str, cursor: dict) -> bool:
    """Write the receipt, build-then-`os.replace`. Returns whether it landed; **never raises.**

    Fail-open is the hard constraint here, not a nicety: `read_session_mail`'s own contract is that it
    "must never be what breaks a session", and a receipt is strictly less important than the mail it
    describes. An unwritable cursor costs the confirmation and the unread filtering — the mail is
    still returned in full, exactly as before any of this existed."""
    if not session_id or not isinstance(cursor, dict):
        return False
    try:
        os.makedirs(session_mail_dir(state_dir), exist_ok=True)
    except OSError:
        return False
    return _write_cursor_file(session_mail_cursor_path(state_dir, session_id), cursor)


def _write_cursor_file(path: str, cursor: dict) -> bool:
    """Build-then-`os.replace` one cursor, by path. Split out because the sweep re-anchors cursors it
    only knows by filename — it never reverses a session id out of one."""
    if not isinstance(cursor, dict):
        return False
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cursor, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def split_session_mail(entries: list, cursor: dict | None) -> tuple:
    """Split a mailbox into `(already_read, unread)`, oldest first, against a cursor.

    Two ways to locate the high-water mark, tried in that order:

      1. **By id.** The cursor names the last entry it read; everything after it in the file is
         unread. Exact, and immune to clock skew and to two entries sharing a timestamp.
      2. **By `at`, when that id is no longer in the file** — it was swept, or the mailbox was
         rebuilt. Entries stamped at or before the recorded instant read as read.

    **Every remaining ambiguity resolves to UNREAD.** No cursor, an unparseable `at` on either side, a
    marker that matches nothing and no recorded instant: all show the mail. Showing an entry twice is
    a wasted paragraph; hiding one is the bug this exists to fix."""
    entries = [e for e in entries if isinstance(e, dict)]
    if not isinstance(cursor, dict) or not cursor or not entries:
        return [], entries
    marker = cursor.get("read_through_id")
    if isinstance(marker, str) and marker.strip():
        marker = marker.strip()
        for i, entry in enumerate(entries):
            if mail_entry_id(entry) == marker:
                return entries[:i + 1], entries[i + 1:]
    try:
        through = parse_iso(cursor.get("read_through_at"))
    except (ValueError, TypeError, AttributeError):
        return [], entries
    already, unread = [], []
    for entry in entries:
        try:
            stamp = parse_iso(entry.get("at"))
        except (ValueError, TypeError, AttributeError):
            stamp = None
        (already if stamp is not None and stamp <= through else unread).append(entry)
    return already, unread


def mark_session_mail_read(state_dir: str, session_id: str, entries: list,
                           now: datetime | None = None) -> bool:
    """Record that everything up to and including the last of `entries` has been read.

    Returns whether the receipt landed — and **a False is not an error the caller may propagate.**
    Every caller here treats it as information to print, never as a reason to withhold mail."""
    if not session_id or not entries:
        return False
    last = None
    for entry in entries:
        if isinstance(entry, dict):
            last = entry
    if last is None:
        return False
    previous = read_mail_cursor(state_dir, session_id)
    try:
        seen = int(previous.get("entries_read") or 0)
    except (TypeError, ValueError):
        seen = 0
    at = last.get("at")
    return write_mail_cursor(state_dir, session_id, {
        "schema": MAIL_CURSOR_SCHEMA,
        "session_id": str(session_id),
        "read_through_id": mail_entry_id(last),
        "read_through_at": at if isinstance(at, str) and at.strip() else None,
        "read_at": _stamp(now or _utc_now()),
        "entries_read": seen + len([e for e in entries if isinstance(e, dict)]),
    })


def session_mail_view(state_dir: str, session_id: str, *, limit: int = 20, show_all: bool = False,
                      peek: bool = False, now: datetime | None = None) -> dict:
    """The `mail` read, as data: what to show, how much is new, and whether the receipt landed.

    Returns `{session_id, entries, shown, unread, total, held_back, advanced, receipt}` where
    `receipt` is True (landed), False (write failed) or None (not attempted).

    **The advance rule: the cursor moves to the newest entry SHOWN, and only when no unread entry was
    withheld.** Unread is always a suffix of an append-only file, so "withheld" is a prefix test and
    the rule is exact rather than heuristic. Two consequences worth stating: the default view takes
    the **oldest** unread first, so a backlog drains in order instead of the newest N burying the
    rest; and `--all --limit N` on a mailbox with more unread than N advances **nothing**, because
    marking mail read that the reader never saw is the same data loss with better manners.

    `peek=True` is the ONLY mode that shows without advancing."""
    entries = read_session_mail(state_dir, session_id, limit=0)
    already, unread = split_session_mail(entries, read_mail_cursor(state_dir, session_id))
    cap = limit if isinstance(limit, int) and limit > 0 else 0
    if show_all:
        # Newest-N-truncated, which is what a "show me everything" view has always meant here.
        start = max(0, len(entries) - cap) if cap else 0
        shown = entries[start:]
        # Unread occupies indices [len(already), len(entries)); the withheld ones are the prefix of
        # that range falling before `start`.
        held_back = max(0, min(start, len(entries)) - len(already))
    else:
        # Oldest unread first: the advance can then never skip an entry nobody saw.
        start = len(already)
        shown = unread[:cap] if cap else list(unread)
        held_back = len(unread) - len(shown)
    result = {"session_id": session_id, "entries": shown, "shown": len(shown),
              "unread": len(unread), "total": len(entries), "held_back": max(0, held_back),
              "advanced": False, "receipt": None}
    if peek or not shown or not unread or start > len(already):
        return result
    result["receipt"] = mark_session_mail_read(state_dir, session_id, shown, now=now)
    result["advanced"] = bool(result["receipt"])
    return result


def mail_summary(entry: dict) -> str:
    """One printable line for a mail entry — its `text` when it has one, and something BUILT from the
    rest when it does not.

    A naive `entry.get('text') or entry.get('kind')` renders a text-less entry as a single word like
    `handoff` — the delivery's TYPE and nothing whatever about its content. A mailbox entry is a
    delivery; the renderer's job is to say enough that the reader knows whether to open it."""
    if not isinstance(entry, dict):
        return one_line(entry, limit=400)
    text = entry.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    kind = str(entry.get("kind") or "mail").strip() or "mail"
    head = ""
    for key in ("subject", "summary", "title", "goal"):
        val = entry.get(key)
        if isinstance(val, str) and val.strip():
            head = one_line(val, limit=400)
            break
    bits = [f"{key}={one_line(entry[key], limit=300)}"
            for key in ("job_id", "status", "from", "artifact", "brief")
            if isinstance(entry.get(key), str) and entry[key].strip()]
    if head and bits:
        return f"{kind}: {head} — {'; '.join(bits)}"
    if head:
        return f"{kind}: {head}"
    if bits:
        return f"{kind} — {'; '.join(bits)}"
    others = sorted(k for k in entry if k not in ("at", "kind", "id"))
    if others:
        return f"{kind} — no text; fields: {', '.join(others)}"
    return f"{kind} — no text and no other fields"


def prune_session_mail(state_dir: str, days: int = RETENTION_DAYS,
                       now: datetime | None = None) -> dict:
    """Sweep old mail (Dream's nightly step). Returns
    `{removed, held_unread, mailboxes, cursors_removed}`.

    **UNREAD MAIL IS NEVER AGE-SWEPT, AND THE HOLD IS REPORTED.** An age-only sweep would make
    `jobs.py prune --mail-days` — which runs nightly, unattended — silently delete mail nobody had ever
    read, in the one feature whose entire purpose is durable delivery. Nothing should be dropped
    silently, and the *stronger* answer available here is not to drop it at all, because with a
    receipt, **age is only a proxy for the question the cursor actually answers.** Sweeping-and-logging would still destroy the delivery and merely
    narrate it afterwards, into a nightly step nobody reads line by line.

    The cost is named rather than hidden: a mailbox addressed to a session that never comes back
    grows without bound. That is bounded in practice by how rarely rung 3 fires at all, it is visible
    (`held_unread` in this report and in `prune`'s own output), and the remedy — a human deleting a
    mailbox for a session that is definitively gone — destroys nothing that was ever going to be
    read. A GC that eats undelivered mail has no such remedy.

    **Build-then-`os.replace`, never truncate-in-place** — `save_json`'s posture, for the same
    reason: `state/` is gitignored and a half-written file has no backup anywhere. An entry whose
    `at` won't parse is KEPT rather than dropped, for the same reason unread entries are. An emptied
    mailbox is removed outright, and its cursor goes with it — as do any cursors already orphaned,
    since a receipt for a mailbox that no longer exists describes nothing."""
    now = now or _utc_now()
    report = {"removed": 0, "held_unread": 0, "mailboxes": 0, "cursors_removed": 0}
    mail_dir = session_mail_dir(state_dir)
    try:
        names = sorted(os.listdir(mail_dir))
    except OSError:
        return report
    live_mailboxes = set()
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(mail_dir, name)
        live_mailboxes.add(name)
        report["mailboxes"] += 1
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        # The cursor is addressed by the same base name, so it is read straight off the filename
        # rather than reverse-engineering a session id out of it.
        cursor_path = os.path.join(mail_dir, name[: -len(".jsonl")] + MAIL_CURSOR_SUFFIX)
        try:
            with open(cursor_path, "r", encoding="utf-8") as fh:
                cursor = json.load(fh)
        except (OSError, ValueError):
            cursor = {}
        parsed = []
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                entry = None
            parsed.append((line, entry if isinstance(entry, dict) else None))
        read_ids = {mail_entry_id(e) for e in
                    split_session_mail([e for _, e in parsed if e is not None],
                                       cursor if isinstance(cursor, dict) else {})[0]}
        keep, dropped, held = [], 0, 0
        for line, entry in parsed:
            try:
                age_days = (now - parse_iso((entry or {}).get("at"))).total_seconds() / 86400.0
            except (ValueError, TypeError, AttributeError):
                keep.append((line, entry))
                continue
            if age_days < days:
                keep.append((line, entry))
            elif entry is None or mail_entry_id(entry) not in read_ids:
                keep.append((line, entry))
                held += 1
            else:
                dropped += 1
        report["held_unread"] += held
        if not dropped:
            continue
        report["removed"] += dropped
        try:
            if not keep:
                os.remove(path)
                live_mailboxes.discard(name)
                continue
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(line for line, _ in keep) + "\n")
            os.replace(tmp, path)
        except OSError:
            continue
        # **RE-ANCHOR THE CURSOR ONTO WHAT SURVIVED.** The sweep can delete the very entry the cursor
        # points at, and a marker naming nothing falls back to comparing timestamps — where two
        # entries filed in the same second are indistinguishable, so a surviving UNREAD entry can read
        # as read and be swept on the next pass. That is the original bug wearing the fix's clothes.
        # The mailbox is append-only, so what survived is authoritative: re-point at the newest
        # surviving READ entry, or at nothing when none survived, in which case everything left is
        # unread and "nothing read" is exactly true.
        if not cursor:
            continue
        anchor = None
        for _, entry in keep:
            if entry is not None and mail_entry_id(entry) in read_ids:
                anchor = entry
        at = anchor.get("at") if isinstance(anchor, dict) else None
        _write_cursor_file(cursor_path, {
            **cursor,
            "read_through_id": mail_entry_id(anchor) if anchor is not None else None,
            "read_through_at": at if isinstance(at, str) and at.strip() else None,
            "reanchored_at": _stamp(now),
        })
    for name in names:
        if not name.endswith(MAIL_CURSOR_SUFFIX):
            continue
        if name[: -len(MAIL_CURSOR_SUFFIX)] + ".jsonl" in live_mailboxes:
            continue
        try:
            os.remove(os.path.join(mail_dir, name))
            report["cursors_removed"] += 1
        except OSError:
            pass
    return report


def origin_goal(rec: dict) -> str:
    """The goal line a job was started with, or "". Tolerant of `origin` being `{}`, missing, or —
    since `load_job` requires only `id` — something that isn't a dict at all."""
    origin = rec.get("origin") if isinstance(rec, dict) else None
    if not isinstance(origin, dict):
        return ""
    return one_line(origin.get("goal"))


# --------------------------------------------------------------------------- process helpers

def child_env(job_id: str | None = None) -> dict:
    """Env for a spawned job: scrub ANTHROPIC_API_KEY, **unbuffer Python children**, and — for §3.14's
    `job_background_guard.py` — stamp `SENESCHAL_JOB_ID` when `job_id` is given.

    **`SENESCHAL_JOB_ID` is the whole scoping signal for the background-guard hook.** It fires only when
    this var is present on ITS OWN process — inherited straight down from here through the job's real
    command (a `claude -p` invocation most often) to any Bash tool call that command makes — and never
    for an ordinary interactive session, which never has it set. Passed at both call sites that matter:
    `_spawn_attempt` (the shim itself) and `_run_shim` (the job's real argv) — the shim's own copy is
    harmless (it never calls a hook) but keeps the value on disk-to-process path uniform rather than
    depending on inheritance alone. `job_id` is optional and defaults to unset for any caller that
    predates this — nothing here required it before.

    The scrub is duplicated from `presence.child_env` rather than imported (presence imports this
    module), keeping the invariant "nothing under the daemon's supervision ever inherits a stray API
    key" true without exception — a job may well be a `claude -p` run, and those must stay
    subscription-billed. It is the SAME scrub (pop `ANTHROPIC_API_KEY`, nothing else removed), so a job
    and a daemon-spawned `claude` see identical credentials. Platform-neutral: `os.environ.copy()` is
    the only source, on Windows and POSIX alike.

    **`PYTHONUNBUFFERED` is here because a job's log is written to a FILE, and C stdio block-buffers
    to a file.** `jobs.py` never buffers a job's output — the log's file descriptor is handed straight
    to the child, so every byte the child actually writes lands immediately. What buffers is the
    CHILD, in its own address space, and that is invisible from here: a plain
    `python -c "print(...); sleep(40)"` job's log stays **0 bytes** until the child exits, while the
    same child under `-u` shows its first line at once.

    Two real consequences, and they are the same defect wearing different clothes:

      1. **You could not watch a job.** `jobs.py status` on a healthy long-running Python job was
         byte-identical to one that was wedged — both an empty log — so the one question the log
         exists to answer could not be asked until it no longer mattered.
      2. **A killed job lost everything it had produced.** Not because `cancel` destroys anything —
         it demonstrably does not; already-written bytes survive it untouched — but because on
         Windows there is no signal that lets a doomed process flush. `os.kill` maps to
         `TerminateProcess`, so whatever sat in the child's buffer died with it. `--analyze` exists
         precisely for jobs that ended badly, and **a timeout is a kill**, so the one path built to
         investigate a bad ending was handed a 0-byte file in exactly the case it exists for.

    So the fix is to stop the output being buffered rather than to try to rescue it afterwards: there
    is no way to rescue it afterwards. This covers every Python child — which is most of what runs
    here (`job_analysis.py`, the repo's own scripts, the shim itself) — and is inert for
    everything else. **A non-Python child that block-buffers its own stdout still writes nothing until
    it flushes, and nothing outside that process can change it**; that limit is real and is named in
    the spec rather than papered over."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env["PYTHONUNBUFFERED"] = "1"
    if job_id:
        env["SENESCHAL_JOB_ID"] = str(job_id)
    return env


def spawnable_argv(argv, *, which=None, is_windows=None) -> list:
    """`argv` as it must be handed to `Popen` on this host — the same program, resolved the way a
    shell would resolve it. Never raises; anything it cannot improve comes back unchanged.

    **Why:** on Windows, `CreateProcess` searches PATH only for `.exe` when a bare name carries no
    extension, so an npm-installed `claude` (which is `claude.cmd`) fails to spawn as `["claude",
    …]` with `FileNotFoundError` — a job that "failed" before running a line. A bare first token is
    therefore resolved through `shutil.which` (which honours `PATHEXT`) and substituted **only when it
    resolves to a `.cmd`/`.bat`** — the case bare `Popen` cannot handle. A `.exe`, a token that
    already names a path, and every POSIX host (where `execvp` searches PATH itself) are untouched.

    This does not contradict the preflight's "the argv is never rewritten": it is the identical
    program the caller named, found where the caller's own shell would find it, and the RECORD keeps
    the argv exactly as given — only the spawn sees the resolved path. `which`/`is_windows` are the
    test seams, the same shape `preflight_refusal` takes."""
    try:
        args = list(argv or [])
        is_windows = _HOST_IS_WINDOWS if is_windows is None else is_windows
        if not args or not is_windows:
            return args
        head = str(args[0])
        if "/" in head or "\\" in head or os.path.splitext(head)[1]:
            return args
        resolved = (which or _default_which)(head)
        if resolved and str(resolved).lower().endswith((".cmd", ".bat")):
            return [str(resolved)] + args[1:]
        return args
    except Exception:  # noqa: BLE001 — resolution is a convenience; the spawn decides the rest
        return list(argv or [])


def pid_alive(pid) -> bool:
    """Is `pid` still running? Conservative by design: anything we cannot answer reads as ALIVE, so a
    probe failure can never fire a false "it ended" ping. The deadline is the backstop that keeps
    that from meaning "never reports" (module docstring).

    NEVER `os.kill(pid, 0)` on Windows: CPython maps `os.kill` there to `TerminateProcess`, so the
    conventional POSIX liveness probe would kill the very job we are asking about."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False  # no such process (or it's gone) — the one case we can answer cleanly
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True  # can't read it -> assume alive
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 — ctypes unavailable/blocked: assume alive, never false-ping
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True  # e.g. EPERM — it exists, we just can't signal it
    except Exception:  # noqa: BLE001
        return True


def kill_pid(pid, *, runner=subprocess.run) -> None:
    """Best-effort kill of a process **and its descendants**, tolerant of None / already-dead /
    access-denied / a reused PID — every failure swallowed, never raised.

    **The tree, not the process.** The shim runs the job's argv as its child, and that child very
    often has children of its own: `pwsh -File build.ps1` runs `gradlew.bat` which runs `java`;
    `claude -p` runs whatever it is told to. Signalling only the two PIDs on the record leaves every
    one of those grandchildren running — still executing, and **still appending to the job's log**,
    minutes after the record says `cancelled`. Three symptoms follow, all of which read as unrelated
    bugs from the outside:

      * a "cancelled" job that is still burning CPU and still doing whatever it was told to do;
      * a `--worktree` job whose teardown fails with `Permission denied`, because a live grandchild
        still holds a handle on the directory — recorded as a `worktree_leaked` for a tree that only
        leaked because nothing killed what was sitting in it;
      * a terminal record whose log keeps growing, so `notify_text`'s `Last line:` and `--analyze`'s
        input are both read out from under a writer nobody stopped.

    The mechanism: `taskkill /T` on Windows, and on POSIX a `killpg` that **refuses to signal our own
    process group** — the shim is
    spawned `start_new_session=True`, so it leads its own group and the whole tree goes with it, but a
    child that somehow never got one would take this process down with it.

    Still no wait-then-escalate loop, and still SIGTERM rather than SIGKILL on POSIX: a graceful
    signal is the only chance a child gets to flush anything (Windows offers no such chance at all —
    `os.kill` there is `TerminateProcess`, which is why `child_env`'s `PYTHONUNBUFFERED` and not a
    grace period is what actually keeps a cancelled job's output). `runner` is the seam that keeps
    `taskkill` out of the tests. **The PID-reuse caveat the module already accepted widens here**:
    where a stale PID used to cost one wrong process, it now costs one wrong tree. It stays
    acceptable for the same reason — this is only ever called against a record we still believe is
    `running`."""
    if not pid:
        return
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if os.name == "nt":
        try:
            runner(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
            return
        except Exception:  # noqa: BLE001 — taskkill missing/hung: fall back to the single process
            pass
    else:
        try:
            pgid = os.getpgid(pid)
            if pgid != os.getpgid(0):
                os.killpg(pgid, signal.SIGTERM)
                return
        except (OSError, AttributeError):
            pass  # no such process, or no group of its own — the single-process kill below is right
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, TypeError, ValueError):
        pass


def log_tail_line(path: str, limit: int = _TAIL_CHARS) -> str:
    """The last non-empty line of a job's log, truncated — quoted in the completion push so the ping
    carries the outcome, not just the fact of it (for `watch_pr.py` that line IS the verdict).
    Reads at most the final 8 KB. Unreadable/absent/binary → empty string, never raises."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > 8192:
                fh.seek(-8192, os.SEEK_END)
            blob = fh.read()
    except (OSError, ValueError):
        return ""
    try:
        text = blob.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def read_log_span(path: str, start: int = 0, limit: int = _ATTEMPT_SCAN_BYTES) -> str:
    """The log written from byte `start` onward — i.e. ONE attempt's output, since each attempt
    records the offset it began at. Reads at most `limit` bytes, taking the **tail** of the span
    (a transport error is the last thing a dying command prints). Unreadable → "", never raises.

    **`limit` is the caller's to choose, and it is not decoration.** "The tail of the span" is only
    true when the span exceeds `limit`; below that this returns the span whole, from `start`. The
    classifier therefore passes `_CLASSIFY_TAIL_BYTES` rather than taking this default — see that
    constant for the stale match the default would buy."""
    try:
        start = max(0, int(start or 0))
        size = os.path.getsize(path)
        if size <= start:
            return ""
        with open(path, "rb") as fh:
            fh.seek(max(start, size - limit))
            blob = fh.read()
    except (OSError, ValueError, TypeError):
        return ""
    return blob.decode("utf-8", errors="replace")


def log_tail_lines(path: str, start: int = 0, count: int = _CONTRADICTION_TAIL_LINES,
                   limit: int = _CONTRADICTION_SCAN_BYTES) -> list:
    """The last `count` non-blank COMPLETE lines this attempt wrote — the end of the log, as lines.

    Distinct from `read_log_span` in the one way that matters to `done_contradiction`: when the
    bounded read lands mid-line, that leading fragment is **discarded** rather than handed back as a
    line. The assertions are anchored at the start of a line, and a fragment's first character is not
    the start of one — matching it would be a false refusal manufactured by wherever the seek
    happened to land.

    Lines keep their leading whitespace (indentation is load-bearing: it is how a wrapper reports a
    SUB-step, and a sub-step failing is compatible with the job succeeding). Unreadable, absent or
    empty → `[]`, never raises — this runs at the end of every attempt, and an unreadable log must
    not be able to manufacture a verdict."""
    try:
        start = max(0, int(start or 0))
        size = os.path.getsize(path)
        if size <= start:
            return []
        begin = max(start, size - max(1, int(limit)))
        with open(path, "rb") as fh:
            fh.seek(begin)
            blob = fh.read()
        text = blob.decode("utf-8", errors="replace")
    except (OSError, ValueError, TypeError):
        return []
    except Exception:  # noqa: BLE001 — a pathological decode must not strand a job
        return []
    if begin > start:
        text = text.split("\n", 1)[1] if "\n" in text else ""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-max(1, int(count)):] if lines else []


def done_contradiction(lines) -> dict | None:
    """Did this attempt DECLARE a failure in its own parting lines, having exited 0? The matched
    signature and line, or None — which is the answer for the overwhelming majority of jobs.

    **It never says the job failed.** It says the two things that can be known about the outcome
    disagree, and the caller's only move is `ended-unknown` — the state §2 already reserves for an
    outcome that cannot be read. Claiming `failed` on the strength of a log line would overrule a
    command's own exit contract, and the incident that motivated this is precisely a wrapper whose
    author meant its `exit 0` (see FAILURE_ASSERTIONS).

    Total, like `_ended_dt` and `worktree_note`: a bad `lines`, a non-string element or an exploding
    pattern all read as "no contradiction". This runs in the shim, where a raise would strand the
    attempt with no stamped outcome at all — strictly worse than the bug."""
    try:
        for line in lines or []:
            if not isinstance(line, str):
                continue
            for name, rx in _FAILURE_ASSERTION_RE:
                if rx.match(line):
                    return {"signature": name, "line": one_line(line, _TAIL_CHARS)}
    except Exception:  # noqa: BLE001
        return None
    return None


def log_span_bytes(path: str, start: int = 0) -> int:
    """How many bytes THIS attempt has written — `size - start`, never negative. Unreadable → 0.

    Separate from `read_log_span` because the two answer different questions, and conflating them is
    a real (if quiet) wrong number: the classifier reads a bounded FAILURE-END window, while
    `FAST_FAIL_BYTES` asks whether the attempt printed almost nothing **at all**. Measuring the
    corroborator off the window would make "a few hundred bytes" mean "the last 161 bytes I bothered to read",
    which is true of every attempt ever run."""
    try:
        return max(0, int(os.path.getsize(path)) - max(0, int(start or 0)))
    except (OSError, ValueError, TypeError):
        return 0


# --------------------------------------------------------------------------- transient classifier

def transient_signature(text: str) -> str | None:
    """The name of the first `TRANSIENT_SIGNATURES` pattern this output matches, or None.

    None is the answer for the overwhelming majority of failures and that is the point: the default
    is terminal. Never raises — a classifier that blew up on odd bytes would strand the job."""
    if not text:
        return None
    try:
        for name, rx in _TRANSIENT_RE:
            if rx.search(text):
                return name
    except Exception:  # noqa: BLE001 — pathological input must not strand a job; unmatched = terminal
        return None
    return None


def classify_failure(text: str, *, duration_sec: float | None = None,
                     output_bytes: int | None = None) -> dict:
    """Was this failure transient (worth another go) or terminal (report it now)?

    Signature-driven, deliberately: `fast_fail` corroborates but never decides, because a broken
    command also dies in a second. `confidence` is `high` when a recognised signature landed on a
    fast, tiny failure (the canonical API-blip shape: ~1 s, a few hundred bytes, one
    `API Error: 500` line),
    `medium` for a signature alone, and `none` when nothing matched."""
    sig = transient_signature(text or "")
    try:
        fast = (duration_sec is not None and float(duration_sec) <= FAST_FAIL_SEC
                and (output_bytes is None or int(output_bytes) <= FAST_FAIL_BYTES))
    except (TypeError, ValueError):
        fast = False
    return {
        "classification": CLASS_TRANSIENT if sig else CLASS_TERMINAL,
        "signature": sig,
        "fast_fail": bool(fast),
        "confidence": ("high" if sig and fast else "medium" if sig else "none"),
    }


# --------------------------------------------------------------------------- retry policy

def parse_duration(text) -> int:
    """`90` / `90s` / `45m` / `2h` / `1d` / `1h30m` → seconds. Raises ValueError on anything else."""
    if isinstance(text, (int, float)):
        return int(text)
    raw = str(text or "").strip().lower().replace(" ", "")
    if not raw:
        raise ValueError("empty duration")
    if raw.isdigit():
        return int(raw)
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    parts = re.findall(r"(\d+)([smhd])", raw)
    if not parts or "".join(n + u for n, u in parts) != raw:
        raise ValueError(f"not a duration: {text!r} (try 90s / 45m / 2h / 1h30m)")
    return sum(int(n) * units[u] for n, u in parts)


def retry_config(max_retries: int = 0, *, backoff: bool = False, window_sec=None,
                 base_sec: int = RETRY_BASE_SEC, cap_sec: int = RETRY_CAP_SEC) -> dict | None:
    """The `retry` block for a record, or **None** when retry is off — which is the default, and the
    thing that keeps every pre-existing caller on the exact code path it had before."""
    try:
        n = int(max_retries or 0)
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(n, RETRY_MAX_RETRIES_CAP))
    if n <= 0:
        return None
    try:
        window = int(window_sec) if window_sec else None
    except (TypeError, ValueError):
        window = None
    return {"max_retries": n, "backoff": bool(backoff),
            "window_sec": window if (window or 0) > 0 else None,
            "base_sec": max(1, int(base_sec or RETRY_BASE_SEC)),
            "cap_sec": max(1, int(cap_sec or RETRY_CAP_SEC))}


def backoff_delay(cfg: dict, attempt: int, rand=random.random) -> float:
    """Seconds to wait after `attempt` (1-based) failed. Exponential from `base_sec`, capped at
    `cap_sec`, ±`RETRY_JITTER_FRAC` jitter; a constant `base_sec` when `--retry-backoff` is off.

    `rand` is injected so the schedule is exactly assertable in tests — jitter that can't be pinned
    is jitter that can't be regression-tested."""
    cfg = cfg or {}
    base = max(1, int(cfg.get("base_sec") or RETRY_BASE_SEC))
    cap = max(1, int(cfg.get("cap_sec") or RETRY_CAP_SEC))
    try:
        n = max(1, int(attempt or 1))
    except (TypeError, ValueError):
        n = 1
    raw = base * (2 ** min(n - 1, 20)) if cfg.get("backoff") else base
    raw = min(float(raw), float(cap))
    try:
        spread = RETRY_JITTER_FRAC * (2.0 * float(rand()) - 1.0)
    except Exception:  # noqa: BLE001 — a broken rand seam must not strand the retry
        spread = 0.0
    return max(1.0, raw * (1.0 + spread))


def retry_window_end(rec: dict) -> datetime | None:
    """Absolute end of the retry window, measured from `created_at` — the whole job's wall clock, so
    an overnight job can't still be flailing at noon. None when no window was asked for."""
    cfg = rec.get("retry") or {}
    window = cfg.get("window_sec")
    if not window:
        return None
    try:
        return parse_iso(rec.get("created_at") or rec.get("started_at")) + timedelta(seconds=int(window))
    except (ValueError, TypeError, AttributeError):
        return None


def plan_retry(rec: dict, *, status: str, output: str, duration_sec: float | None = None,
               output_bytes: int | None = None, now: datetime | None = None,
               rand=random.random) -> dict:
    """Should this attempt be retried, and when? Pure — no I/O, no side effects, so the crux of the
    feature is testable without spawning anything.

    Returns `{retry, reason, classification, signature, fast_fail, confidence, delay_sec,
    next_attempt_at}`. `reason` is recorded on the job so a reader can reconstruct the decision from
    the record alone, which is the whole point of writing it down.

    **Classification happens before the retry-enabled check, deliberately.** A failure is classified
    whether or not `--retry` was asked for; only the *decision* depends on the config. That is what
    lets a non-retry job record "this died on an API 500" without changing what it does about it —
    see the module docstring's "Always record the classification"."""
    now = now or _utc_now()
    cfg = rec.get("retry") or None
    verdict = {"retry": False, "reason": "", "classification": CLASS_TERMINAL, "signature": None,
               "fast_fail": False, "confidence": "none", "delay_sec": None, "next_attempt_at": None}

    if status == DONE:
        return {**verdict, "classification": CLASS_SUCCESS, "reason": "succeeded"}
    if status == FAILED:
        # Unconditional: the verdict is the diagnostic, and it is worth exactly as much on a job that
        # did NOT opt into retry — arguably more, since that is the job someone may want to opt in.
        verdict.update(classify_failure(output, duration_sec=duration_sec,
                                        output_bytes=output_bytes))
    if not cfg:
        # Recorded, not acted on: no retry config means no retry, whatever the classification says.
        return {**verdict, "reason": "retry-not-enabled"}
    if status != FAILED:
        # timed-out / ended-unknown / cancelled. The first means the command was too slow (a retry
        # makes that strictly worse); the other two have no readable outcome to classify, and
        # guessing "transient" there would re-run work whose damage we cannot even see.
        return {**verdict, "reason": f"non-retryable-outcome:{status}"}
    if verdict["classification"] != CLASS_TRANSIENT:
        return {**verdict, "reason": "no-transient-signature"}

    try:
        attempt = max(1, int(rec.get("attempt") or 1))
    except (TypeError, ValueError):
        attempt = 1
    if attempt > int(cfg.get("max_retries") or 0):
        # attempt N has run; retries used = N-1; so N > max_retries means the budget is spent.
        return {**verdict, "reason": "retries-exhausted"}

    delay = backoff_delay(cfg, attempt, rand=rand)
    next_at = now + timedelta(seconds=delay)
    end = retry_window_end(rec)
    if end is not None and next_at > end:
        # Checked against the SCHEDULED time, not just "now": a 10-minute backoff that would land
        # past the window is a retry that must not be scheduled at all.
        return {**verdict, "reason": "retry-window-exhausted"}
    return {**verdict, "retry": True, "reason": "transient-signature",
            "delay_sec": round(delay, 3), "next_attempt_at": _stamp(next_at)}


# --------------------------------------------------------------------------- starting a job

# --------------------------------------------------------------------------- the spawn preflight
# **Refuse to spawn a job whose script isn't there.** The failure this catches is not in `jobs.py` or
# in the job: the argv is mangled by the shell that launched `jobs.py`, before this module sees it.
# Two shapes recur on Windows:
#
#   1. `pwsh -NoProfile -File C:\…\run.ps1`, launched from Git Bash. Bash eats every backslash, so pwsh
#      receives `C:Users…run.ps1` and exits 64.
#   2. `wsl.exe -d <distro> bash /mnt/c/…/job.sh`, launched from Git Bash. MSYS path translation
#      rewrites the POSIX-looking argument on its way to a native `.exe`, so the bash inside WSL
#      receives `C:/Program Files/Git/mnt/c/…/job.sh` and exits 127.
#
# Both spawn "successfully", die within seconds, and spend a completion push on a failure the owner
# then has to read and re-derive. **Both are detectable BEFORE the spawn, because in both cases the
# file the command was told to run does not exist.**
#
# **This is a shape, not a rule.** A prose rule about quoting Windows paths is addressed to the
# operator and does not prevent either of these. So the guard is something a mangled argv cannot fit
# through, rather than something a caller has to remember.
#
# **FAIL-OPEN ON UNKNOWN, FAIL-CLOSED ON KNOWN-BAD.** Only the shapes where an argument is
# *definitionally* the path of a file that must already exist are inspected; everything else is left
# alone. So is a
# RELATIVE path, deliberately: what it resolves against is the job's `cwd`, and a `--worktree` job's
# `cwd` does not exist yet when this runs. **A false refusal that blocks a legitimate job is worse
# than the bug being fixed** — hence `--no-preflight`, which is for a preflight that is wrong and
# never for a path that is.
#
# It runs at the CLI, not inside `start_job`, because the CLI is the only caller whose argv came
# through a shell: `request_analysis` and the retry respawn both build their argv in code, and neither
# should be blockable by this.
#
# ------------------------------------------------------------ and it tests in the RIGHT NAMESPACE
#
# **The check has to run in the namespace the command will actually execute in.** An argv of
# `["bash", "C:/…/probe.sh"]` dies at exit 127 in under a second with
#
#     /bin/bash: C:/…/probe.sh: No such file or directory
#
# while the script is sitting right there: on a Windows host with WSL, bare `bash` usually resolves to
# `C:\Windows\System32\bash.exe`, which is **WSL's** bash, and WSL reaches the Windows filesystem only
# under `/mnt/`. A check that asks *Windows* whether the file exists, while the command is going to
# ask *WSL*, catches an ordinary typo and is structurally blind to the more common failure — and a job
# reported `failed` naming a real file as missing sends the reader to the file instead of to the
# interpreter.
#
# **The namespace is answered from EVIDENCE, never assumed**, because being wrong in the other
# direction refuses working jobs — and *"a false refusal that blocks a legitimate job is worse than
# the bug being fixed"* is this guard's own rule. `bash.exe` under `System32` is WSL's launcher; a
# Git-for-Windows `bash.exe` is not, and Git Bash reads `C:/…` perfectly well. Hence:
#
#   * a `wsl.exe` prefix               → WSL, by construction, whatever follows it;
#   * a bare `bash` / `sh` on Windows  → WSL **only if** it resolves into `System32` (asked of
#                                        `shutil.which`, the same PATH lookup the spawn is about to
#                                        do). Anything else it resolves to, or a resolution that
#                                        fails, is UNKNOWN;
#   * `pwsh` / `powershell` / `python` / a `.ps1`, `.py`, `.bat`, `.cmd`, `.exe` token, not behind a
#     `wsl.exe` prefix, on a Windows host → the Windows namespace;
#   * everything else, and **every command on a non-Windows host** → UNKNOWN, which is fail-open and
#     byte-for-byte the behaviour that was there before this existed.
#
# Both directions are refused, because `/mnt/c/…` handed to a native Windows process is the mirror
# image of the same bug (`/mnt/c` there is a directory called `\mnt\c` on whatever drive is current,
# not the C: drive). **THE ARGV IS NEVER REWRITTEN.** Rewriting a caller's command is a surprise, and
# a job that runs something slightly different from what was asked is exactly the failure class this
# repo keeps getting bitten by — so the refusal names the namespace, the path as given, the path it
# would have to be, and both fixes, and then stops.

# Options on a `wsl.exe` prefix that consume the token after them, and the ones after which the real
# command begins. Anything outside both lists is unknown → stop looking.
_WSL_VALUE_OPTS = frozenset({"-d", "--distribution", "-u", "--user", "--cd"})
_WSL_END_OPTS = frozenset({"-e", "--exec", "--"})
# Flags that change nothing about WHICH argument is the script. Anything else — `bash -c "…"`,
# `python -m …`, `-lc`, an option we've never seen — means we stop looking rather than guess, because
# the argument after those is a PROGRAM, not a path.
_BASH_SAFE_FLAGS = frozenset({"-l", "--login", "-e", "-x", "-u", "-v"})
_PYTHON_SAFE_FLAGS = frozenset({"-u", "-b", "-B", "-E", "-I", "-O", "-OO", "-s", "-S", "-q"})

_MNT_RE = re.compile(r"^/mnt/([A-Za-z])(/.*)?$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
# The MSYS signature: `/mnt/` that is NOT at the start of the string, i.e. Git Bash prefixed its own
# install root onto a POSIX argument bound for a native `.exe`. Named specifically because the
# resulting error is otherwise baffling — the next reader should not have to re-derive it.
_MSYS_MNT_RE = re.compile(r".+/mnt/[A-Za-z]/")

# The three namespaces a script path can be read in. UNKNOWN is not a failure of the classifier — it
# is the answer for everything this cannot PROVE, and it is by far the most common one.
NS_WSL = "wsl"
NS_WINDOWS = "windows"
NS_UNKNOWN = "unknown"

_HOST_IS_WINDOWS = os.name == "nt"

# `bash.exe` in System32 is WSL's launcher — that is the evidence, and it is the whole distinction
# from a Git-for-Windows bash, which lives under its own install root and reads `C:/…` fine.
# SysWOW64/Sysnative are the same directory seen from a WOW64 or a 32-bit process, so all three count.
_SYSTEM32_RE = re.compile(r"/(?:system32|sysnative|syswow64)/", re.IGNORECASE)
# Windows-absolute BY SHAPE: a drive letter and a separator. Deliberately NOT the separator-less
# `C:Users…`, which is the eaten-backslash mangling and has its own, better message (`_mangling_cause`).
_WINDOWS_ABS_RE = re.compile(r"^([A-Za-z]):[\\/]")
# Commands that are native Windows processes when they are not behind a `wsl.exe` prefix. `python` is
# matched by prefix (python3, python3.12) rather than listed.
_WINDOWS_COMMANDS = frozenset({"pwsh", "powershell", "cmd"})
_WINDOWS_SUFFIXES = (".ps1", ".py", ".bat", ".cmd", ".exe")


def _cmd_name(arg) -> str:
    """The bare command a token names, lowercased and de-`.exe`'d:
    `C:\\Program Files\\PowerShell\\7\\pwsh.exe` → `pwsh`.

    Splits on BOTH separators by hand rather than using `os.path.basename`, which does not treat a
    backslash as one on a POSIX host — and arguments that crossed a shell boundary are the entire
    subject here, so the check must read the same on the daemon's Windows box and on CI's Linux."""
    try:
        text = str(arg).replace("\\", "/").rstrip("/")
    except Exception:  # noqa: BLE001 — a weird argv element is unknown, and unknown is fail-open
        return ""
    name = text.rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _mnt_to_windows(raw: str) -> str | None:
    """`/mnt/c/x` → `C:/x`, or None when it is not a `/mnt/<drive>/…` path at all. The one place the
    WSL→Windows spelling is defined, so the existence check and the namespace refusal cannot drift."""
    m = _MNT_RE.match(raw or "")
    return f"{m.group(1).upper()}:{m.group(2) or '/'}" if m else None


def _windows_to_mnt(raw: str) -> str | None:
    """`C:/x` or `C:\\x` → `/mnt/c/x`, or None when it is not a drive-rooted Windows path. The inverse
    of `_mnt_to_windows`, and the other half of "test it in the namespace it will run in"."""
    m = _WINDOWS_ABS_RE.match(raw or "")
    return f"/mnt/{m.group(1).lower()}{raw[2:]}".replace("\\", "/") if m else None


def _default_which(token):
    """The PATH lookup, wrapped in one named place **so the CLI path is testable**: `main` calls
    `preflight_refusal` with no seams at all, so a test that wants to pin the namespace verdict has
    to be able to replace this and `_HOST_IS_WINDOWS` rather than pass arguments. Both are read at
    CALL time for the same reason."""
    return shutil.which(token)


def _interpreter_path(token, which):
    """Where a command token actually resolves: the token itself when it already names a path,
    otherwise the PATH lookup — **the same one the spawn is about to do**, which is what makes this
    evidence rather than a guess. None when it cannot be resolved; never raises."""
    try:
        text = str(token)
    except Exception:  # noqa: BLE001 — an unresolvable interpreter is unknown, and unknown is open
        return None
    if "/" in text or "\\" in text:
        return text
    try:
        return which(text)
    except Exception:  # noqa: BLE001
        return None


def _command_namespace(token, *, which, is_windows) -> tuple:
    """`(namespace, interpreter)` for the command that will read the script path.

    **WSL only on evidence, Windows only on a command that is a native Windows process by definition,
    UNKNOWN for everything else** — and unknown is fail-open, i.e. exactly the behaviour that existed
    before namespaces were considered at all. A wrong WSL verdict would refuse a working Git-Bash job;
    a wrong Windows verdict would refuse a working POSIX one. Both are worse than the bug."""
    name = _cmd_name(token)
    if name in ("bash", "sh"):
        if not is_windows:
            # On a POSIX host the host's own shell IS the namespace: there is no second one to be
            # mismatched against, so there is nothing here to prove and nothing to refuse.
            return NS_UNKNOWN, ""
        resolved = _interpreter_path(token, which)
        if not resolved:
            return NS_UNKNOWN, ""            # not on PATH ⇒ we cannot say WHICH bash ⇒ we say nothing
        text = str(resolved)
        if _SYSTEM32_RE.search(text.replace("\\", "/")):
            return NS_WSL, text
        return NS_UNKNOWN, text              # Git-for-Windows, MSYS, busybox: `C:/…` is fine for them
    if not is_windows:
        return NS_UNKNOWN, ""
    try:
        looks_windows = str(token).lower().endswith(_WINDOWS_SUFFIXES)
    except Exception:  # noqa: BLE001
        looks_windows = False
    if name in _WINDOWS_COMMANDS or name.startswith("python") or looks_windows:
        return NS_WINDOWS, str(token)
    return NS_UNKNOWN, ""


def _script_path_args(argv, *, which=None, is_windows=None) -> list:
    """Every argument that is definitionally the path of a script the command REQUIRES, as
    `(index, raw, posix, namespace, interpreter)`.

    `posix` means the argument is *spelled* as a POSIX path, so `/mnt/c/…` is how an ordinary Windows
    path is written; `namespace` is where the command will actually RUN, which is a different question
    and the one the wrong-namespace failure turns on (a bash argument is POSIX-spelled either way, but
    only a WSL bash is unable to open `C:/…`).

    Returns `[]` for everything it does not recognise, which is most argvs (`git status`, `claude -p
    "…"`, `npm ci`). The recognised shapes, and nothing else:

      * `pwsh` / `powershell` … `-File <path>` (or `-f`)
      * `bash` / `sh` <path>, including after a `wsl.exe [-d <distro>]` prefix
      * `python` / `python3` <path>`.py`
    """
    which = _default_which if which is None else which
    is_windows = _HOST_IS_WINDOWS if is_windows is None else is_windows
    try:
        args = [str(a) for a in (argv or [])]
    except Exception:  # noqa: BLE001
        return []
    if not args:
        return []

    i, posix = 0, False
    ns, interp = NS_UNKNOWN, ""
    if _cmd_name(args[0]) == "wsl":
        # Walk past the launcher's own options to reach the command it will run INSIDE the distro.
        # An option we don't know means we stop entirely: guessing which token is the command is how
        # a legitimate job gets refused.
        i, posix = 1, True
        ns, interp = NS_WSL, args[0]      # by construction, whatever command follows the launcher
        while i < len(args):
            tok = args[i]
            low = tok.lower()
            if low in _WSL_VALUE_OPTS:
                i += 2
                continue
            if low in _WSL_END_OPTS:
                i += 1
                break
            if tok.startswith("-"):
                return []
            break
    if i >= len(args):
        return []

    name = _cmd_name(args[i])
    rest, base = args[i + 1:], i + 1
    if ns != NS_WSL:
        # Inside a distro the launcher has already answered this; otherwise ask the command itself.
        ns, interp = _command_namespace(args[i], which=which, is_windows=is_windows)

    if name in ("pwsh", "powershell"):
        for k, tok in enumerate(rest):
            if tok.lower() in ("-file", "-f") and k + 1 < len(rest):
                return [(base + k + 1, rest[k + 1], posix, ns, interp)]
        return []

    if name in ("bash", "sh"):
        # **A shell's script argument is ALWAYS read as a POSIX path**, whether that shell is the WSL
        # bash a detached `bash <script>` usually gets on a Windows host or a Git Bash one. So
        # `/mnt/c/x` translates and anything else POSIX-absolute —
        # `/c/Users/…`, `/home/<user>/x.sh` — is untestable from a Windows process rather than
        # missing, which is precisely the distinction that keeps this from refusing a working job.
        for k, tok in enumerate(rest):
            if tok == "--":
                return [(base + k + 1, rest[k + 1], True, ns, interp)] if k + 1 < len(rest) else []
            if tok.startswith("-"):
                if tok in _BASH_SAFE_FLAGS:
                    continue
                return []
            return [(base + k, tok, True, ns, interp)]
        return []

    if name.startswith("python"):
        for k, tok in enumerate(rest):
            if tok == "--":
                nxt = rest[k + 1] if k + 1 < len(rest) else ""
                return [(base + k + 1, nxt, posix, ns, interp)] if nxt.endswith(".py") else []
            if tok.startswith("-"):
                if tok in _PYTHON_SAFE_FLAGS:
                    continue
                return []            # -c / -m and everything unrecognised: the argument isn't a path
            return [(base + k, tok, posix, ns, interp)] if tok.endswith(".py") else []
        return []

    return []


def _resolve_for_check(raw: str, posix: bool) -> str | None:
    """The path to test on disk, or **None when it cannot be tested from here** — which is fail-open
    and must stay the common answer for anything unusual.

    Three cases, in order:

      1. **The MSYS signature** — checked first, and checked even under `posix`, because a mangled
         WSL argument is a Windows-shaped string that no longer starts with `/mnt/`. It is exactly
         what it looks like, so it is tested verbatim.
      2. **Inside WSL** — only `/mnt/<drive>/…` translates (`/mnt/c/x` → `C:/x`). A path in the
         distro's own filesystem (`/home/<user>/x.sh`) is unreachable from a Windows process and is
         therefore untestable, not missing.
      3. **Natively** — only a path that was MEANT to be absolute: a leading separator, or a drive
         letter (with or without the separator a shell may have eaten). A relative path is untestable
         because the job's `cwd` decides it.

    The `/mnt/` translation is unconditional rather than gated on `os.name`, so the verdict is the
    same on the daemon's Windows host and on CI."""
    if not raw:
        return None
    if _MSYS_MNT_RE.match(raw):
        return raw
    if posix:
        return _mnt_to_windows(raw)
    if raw[0] in "/\\" or _DRIVE_RE.match(raw):
        return raw
    return None


def _mangling_cause(raw: str) -> str:
    """The "likely cause" half of the refusal — the sentence that stops the next reader having to
    re-derive an error message that makes no sense on its face."""
    if _MSYS_MNT_RE.match(raw):
        return ("`/mnt/` appears MID-STRING, which is the MSYS path-translation signature: Git Bash "
                "rewrote a POSIX argument on its way to a native .exe and prefixed its own install "
                "root. The path was mangled BEFORE jobs.py saw it. Launch this from PowerShell, or "
                "set MSYS_NO_PATHCONV=1 in the Git Bash you are launching from.")
    if _DRIVE_RE.match(raw) and raw[2:3] not in ("\\", "/"):
        return ("the drive letter has no separator after it, which is the signature of a shell that "
                "ate the backslashes — Git Bash treats `\\` as an escape, so `C:\\Users\\…` arrives "
                "as `C:Users…`. The path was mangled BEFORE jobs.py saw it. Launch this from "
                "PowerShell, or single-quote the path so the launching shell cannot rewrite it.")
    return ("no shell-mangling signature — the path looks intact, so it is most likely genuinely "
            "missing: a typo, a scratch file already swept, or a path that only exists in another "
            "checkout.")


# Shared by both refusals, so the escape hatch is spelled once and reads the same either way.
_REFUSAL_FOOTER = ("Nothing was started and no job record was written. If the PREFLIGHT is wrong — an "
                   "unusual argv it misread — re-run with --no-preflight.")
# The half that belongs only to a namespace mismatch: the argv is NOT rewritten, and the reason why.
_NO_REWRITE = ("The argv is NOT rewritten for you: a job that runs something slightly different from "
               "what was asked is the failure class this preflight exists to prevent.")


def _namespace_refusal(index, raw, namespace, interpreter) -> str | None:
    """A path spelled for the WRONG NAMESPACE — the WSL-bash failure and its mirror image — as a
    message meant to be read, or None. Never raises.

    Refuses only what it can PROVE is broken: a drive-rooted Windows path handed to something that
    provably runs inside WSL (which reaches `C:` only under `/mnt/`), or a `/mnt/<drive>/…` path
    handed to something that is provably a native Windows process (where `/mnt/c` is a directory
    called `\\mnt\\c` on whatever drive is current). An UNKNOWN namespace refuses nothing at all."""
    try:
        if namespace == NS_WSL:
            needed = _windows_to_mnt(raw)
            if needed is None:
                return None
            return (
                f"argv[{index}] is a WINDOWS path, but this command runs in the WSL namespace, which "
                f"reaches the Windows filesystem only under /mnt/. Nothing is missing: the "
                f"interpreter cannot see the path it was handed, so this job would die in about a "
                f"second with `No such file or directory` naming a file that IS there.\n"
                f"  namespace:   WSL{f' ({interpreter})' if interpreter else ''}\n"
                f"  as given:    {raw}\n"
                f"  would need:  {needed}\n"
                f"  fix, either: run it in the WINDOWS namespace — "
                f"pwsh -NoProfile -File {raw}\n"
                f"          or:  hand the interpreter the WSL form — {needed}\n"
                f"{_NO_REWRITE}\n{_REFUSAL_FOOTER}")
        if namespace == NS_WINDOWS:
            needed = _mnt_to_windows(raw)
            if needed is None:
                return None
            return (
                f"argv[{index}] is a WSL path, but this command runs in the WINDOWS namespace, where "
                f"/mnt/c is not the C: drive — it is a directory called \\mnt\\c on whatever drive "
                f"happens to be current. This is the mirror image of the WSL-bash failure: the "
                f"path is spelled for the other interpreter.\n"
                f"  namespace:   Windows{f' ({interpreter})' if interpreter else ''}\n"
                f"  as given:    {raw}\n"
                f"  would need:  {needed}\n"
                f"  fix, either: pwsh -NoProfile -File {needed}\n"
                f"          or:  keep the {raw} form and run it under WSL — "
                f"wsl.exe bash {raw}\n"
                f"{_NO_REWRITE}\n{_REFUSAL_FOOTER}")
    except Exception:  # noqa: BLE001 — a refusal that raises would block every job. Unknown = open.
        return None
    return None


def preflight_refusal(argv, *, exists=os.path.exists, which=None, is_windows=None) -> str | None:
    """Why this argv must not be spawned, as a message meant to be read, or **None**. Never raises.

    Two refusals, in this order, and the ORDER IS THE POINT: *"the interpreter cannot see this path"*
    is a different — and far more actionable — answer than *"this path is not there"*, and the file
    the second one names is, in the WSL-bash case, sitting right where it was put. The MSYS
    signature outranks both: that string is Windows-shaped without ever having been meant to be, and
    its own message is what stops the next reader re-deriving `exit 127`.

    Pure and seamed the way the rest of this module is (`now=`, `runner=`, `prober=`): `exists`,
    `which` and `is_windows` are the only contact with the world, so a test needs no real files, no
    PATH and no particular host. Both namespace seams default to **None** and resolve at call time
    (`_default_which`, `_HOST_IS_WINDOWS`) rather than being bound into the signature, because `main`
    calls this with no arguments and the CLI path has to be pinnable too. Separate from the spawn so
    the refusal can carry its reason — a refusal is a judgment, and a caller should not have to infer
    it from a bare False."""
    try:
        candidates = _script_path_args(argv, which=which, is_windows=is_windows)
    except Exception:  # noqa: BLE001 — a preflight that raises would block every job. Unknown = open.
        return None
    for index, raw, posix, namespace, interpreter in candidates:
        try:
            mangled = bool(_MSYS_MNT_RE.match(raw))
        except Exception:  # noqa: BLE001
            mangled = False
        if not mangled:
            why = _namespace_refusal(index, raw, namespace, interpreter)
            if why:
                return why
        resolved = _resolve_for_check(raw, posix)
        if resolved is None:
            continue
        try:
            if exists(resolved):
                continue
        except Exception:  # noqa: BLE001 — an unanswerable probe is unknown, and unknown is fail-open
            continue
        return (f"argv[{index}] names a script that does not exist, so this job would die seconds "
                f"after spawning.\n"
                f"  argument: {raw}\n"
                f"  checked:  {resolved}\n"
                f"  cause:    {_mangling_cause(raw)}\n"
                f"{_REFUSAL_FOOTER}")
    return None


# ------------------------------------------------------------------------- the concurrency caps
# **At most 3 agent jobs in flight per warm session (soft), and at most 5 in total (hard).** At the hard
# cap, an urgent new job is not squeezed in: the owner is shown the running set in recommended kill
# order and decides.
#
# **The two caps are different KINDS of limit and the code keeps them apart.** The 3 reserves the
# OWNER'S headroom — the concurrency left for their own interactive (desktop) sessions — so it is a
# claim about what they want available, not about what the machine can carry, and overrunning it can
# be the right call for a job that matters. So the soft cap yields to judgement — **one named flag,
# recorded on the record** — while the 5 does not yield at all.
#
# **WHY THIS IS CODE.** A rule in a grounding prompt is a rule the next turn may simply not apply, and
# the caps would otherwise have to be written into every prompt that can type `jobs.py start` —
# including user-level command files no PR can reach. A gate here binds whichever prompt typed the
# command.
#
# **THE REFUSAL AT 5 IS THE FEATURE, NOT THE CAP.** A gate that only says "no" makes the owner go and
# reconstruct the running set themselves — which is the expensive part, not the typing. So the refusal
# prints the in-flight set ranked by recommended kill order, each row carrying the signals that placed
# it and the `cancel` line that acts on it. The owner picks; nothing here ever cancels anything.
#
# **ONLY AGENT JOBS COUNT.** Both caps protect MODEL concurrency — the interactive sessions the 3
# reserves room for are agent sessions — so a transcription, a download, a gradle build or a print
# watcher spends none of it and must not take a slot. **Both caps, both populations**: `concurrency`
# filters through `counts_toward_cap` before either number is taken, and a NEW job that is itself
# non-agent is not checked against either cap at the CLI, since starting it adds nothing to a count.
#
# **Classification is read off the record as launched** (`job_agent_class`), in this order:
#   1. the explicit stamp — `start --agent` / `--no-agent` writes `agent: true|false` onto the record,
#      and it always wins, both ways, because the heuristic below is guessing and the caller knows;
#   2. positive agent evidence in the argv — `claude` as the command (by far the commonest agent
#      job), or a script known to call a model (`fable_delegate.py`, `job_analysis.py`, …);
#   3. positive NON-agent evidence — a command or repo script known to make no model call
#      (`gradlew`, `ffmpeg`, `python -m unittest`, `state_backup.py`, …); then a basename naming a
#      model provider (`my-claude-leg.py`, `--model claude-opus-5`) labels it `agent`;
#   4. **everything else is `unknown`, and unknown COUNTS.** Fail closed: a wrapper (`pwsh -File x.ps1`,
#      `bash x.sh`, `wsl … bash …`, `cmd /c`, `python -c`) hides what it runs, and treating an opaque
#      job as free is how an agent job slips past the cap unseen. An over-count costs one `--no-agent`;
#      an under-count costs the headroom the caps exist to protect.
SOFT_CAP_PER_ORIGIN = 3
HARD_CAP_GLOBAL = 5

AGENT = "agent"
NON_AGENT = "non-agent"
AGENT_UNKNOWN = "unknown"

# Commands that ARE an agent call. Matched on `_cmd_name`, so `C:\…\claude.exe` is `claude`.
_AGENT_COMMANDS = frozenset({"claude", "ollama", "codex", "gemini"})
# Scripts (by basename) known to make model calls — subscription, API or local (Ollama).
_AGENT_SCRIPTS = frozenset({
    "fable_delegate.py", "job_analysis.py", "router.py", "interleave.py",
    "rag_index.py", "rag_query.py", "rag_projects.py", "sentiment.py", "mini_dream.py",
})
# A token naming a model provider in an argv element's basename (a script called
# `my-claude-leg.py`, `--model claude-opus-5`). Positive evidence only — its absence proves nothing.
_AGENT_TOKEN_RE = re.compile(r"claude|anthropic|openai|ollama|fable", re.IGNORECASE)
# Commands known to make no model call. Deliberately short: a name belongs here only when there is
# no plausible way for it to be an agent job, because a wrong entry here is an uncounted agent.
_NON_AGENT_COMMANDS = frozenset({
    "gradlew", "gradlew.bat", "ffmpeg", "ffprobe", "yt-dlp", "whisper", "whisper-ctranslate2",
})
# Repo scripts (by basename) known to make no model call.
_NON_AGENT_SCRIPTS = frozenset({
    "watch_pr.py", "health_import.py", "state_backup.py",
})
# `python -m <module>` for a module that is a test runner, not a model caller.
_NON_AGENT_PY_MODULES = frozenset({"unittest", "pytest"})


def _python_target(argv: list) -> tuple:
    """`(script_basename, module)` for a `python …` / `uv run python …` argv, else `("", "")`.
    `-c` code is opaque and returns neither — an inline program is `unknown`, never cleared."""
    toks = [str(a) for a in argv]
    if len(toks) >= 2 and _cmd_name(toks[0]) == "uv" and toks[1] == "run":
        toks = toks[2:]
    if not toks or not _cmd_name(toks[0]).startswith(("python", "py")):
        return "", ""
    rest = toks[1:]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "-c":
            return "", ""
        if tok == "-m":
            return "", (rest[i + 1] if i + 1 < len(rest) else "")
        if tok.startswith("-"):
            i += 1
            continue
        return _cmd_name(tok), ""
    return "", ""


def job_agent_class(rec: dict) -> tuple:
    """`(class, why)` — `agent`, `non-agent` or `unknown`, and the evidence that decided it.

    Total by construction: this runs inside `concurrency`, which runs inside a refusal, and a
    classifier that raised on one malformed record would cost every count. A record it cannot read is
    `unknown`, which counts — the fail-closed direction the comment block above names."""
    try:
        stamped = rec.get("agent") if isinstance(rec, dict) else None
        if stamped is True:
            return AGENT, "stamped --agent"
        if stamped is False:
            return NON_AGENT, "stamped --no-agent"
        argv = rec.get("argv") if isinstance(rec, dict) else None
        if not isinstance(argv, list) or not argv:
            return AGENT_UNKNOWN, "no argv on the record"
        cmd = _cmd_name(argv[0])
        script, module = _python_target(argv)
        if cmd in _AGENT_COMMANDS:
            return AGENT, f"runs {cmd}"
        if script in _AGENT_SCRIPTS:
            return AGENT, f"runs {script}"
        if cmd in _NON_AGENT_COMMANDS:
            return NON_AGENT, f"runs {cmd}"
        if script in _NON_AGENT_SCRIPTS:
            return NON_AGENT, f"runs {script}"
        if module in _NON_AGENT_PY_MODULES:
            return NON_AGENT, f"runs python -m {module}"
        # Basenames only, never whole paths: a scratch script under `…\Temp\claude\…` or a worktree
        # under `.claude\worktrees\` is not an agent call because of where it lives. Only reached
        # once the command's identity decided nothing, so it changes a label, never a count —
        # `unknown` counts too.
        if any(_AGENT_TOKEN_RE.search(_cmd_name(a)) for a in argv):
            return AGENT, "argv names a model provider"
        return AGENT_UNKNOWN, "command shape not recognised — counted (fail closed)"
    except Exception:  # noqa: BLE001 — unknown counts; see the docstring
        return AGENT_UNKNOWN, "unreadable record — counted (fail closed)"


def counts_toward_cap(rec: dict) -> bool:
    """True unless the record is positively `non-agent`. `unknown` counts."""
    return job_agent_class(rec)[0] != NON_AGENT


def in_flight(state_dir: str, now: datetime | None = None) -> list:
    """Every job that is **still actually in flight**, oldest first — the population both caps count
    FROM. `concurrency` then keeps only the agent jobs (`counts_toward_cap`).

    **Not `list_jobs(active_only=True)`, and the difference is the whole reliability of the gate.**
    `active_only` reads the STATUS FIELD, and that field is written by the daemon's reconcile pass; a
    job whose shim died overnight reads `running` on disk until a tick notices. Counting those would
    make a stale record refuse live work, and a cap that refuses for reasons its owner cannot see is a
    cap that gets deleted rather than obeyed.

    So a record is dropped when the module can already PROVE it is over, using the same two
    side-effect-free predicates `reconcile` uses to decide the same thing — `check_terminal` (deadline
    elapsed / no PID past the startup grace / PID gone) and `check_retry_due` returning `"expired"`.
    Nothing is written: `start` must not transition another job's record as a side effect of counting.

    **The polarity, named:** proof-of-over drops, everything else counts. `pid_alive` is deliberately
    conservative — an unanswerable probe reads as alive — so the count errs toward being FULL, which is
    the same direction `reconcile` errs and the opposite of most of this module. That is the right way
    round for a cap whose refusal is one flag or one `cancel` away, and the residual is worth saying
    out loud: a recycled PID number counts as its old job."""
    now = now or _utc_now()
    out = []
    for rec in list_jobs(state_dir, active_only=True):
        if check_terminal(rec, now) is not None:
            continue
        if check_retry_due(rec, now) == "expired":
            continue
        out.append(rec)
    return out


def origin_session_of(rec: dict) -> str:
    """The session a job was started from, or `""`. `origin` is `{}` on every record written without a
    session, and those must stay indistinguishable from a missing key
    (§3.4) — so the floor is the empty string and never `None`."""
    origin = rec.get("origin") if isinstance(rec, dict) else None
    if not isinstance(origin, dict):
        return ""
    sid = origin.get("session_id")
    return sid.strip() if isinstance(sid, str) else ""


def concurrency(state_dir: str, session_id: str | None = None, now: datetime | None = None) -> dict:
    """`{"global": n, "session": n, "session_id": …, "in_flight": [rec, …], "uncounted": [rec, …]}` —
    one read, both counts.

    **Only agent jobs are counted** (the comment block above `SOFT_CAP_PER_ORIGIN`).
    `in_flight` here is the COUNTED set — what both numbers are taken over and what a refusal ranks —
    and `uncounted` is the live non-agent remainder, kept so a refusal can say how many are running
    without being counted rather than pretend they aren't there.

    **`origin.session_id` is what makes the two caps different questions**, and it is already on the
    record: the per-session count is the subset sharing the caller's session id, the global count is
    everything. A caller with no session id (a cron shim, a hand-run command, an `--analyze` job, which
    is origin-less in code) counts toward the global number and toward NO session — which is correct,
    since the 3 reserves headroom for interactive sessions and an unaddressed job is not one of them."""
    now = now or _utc_now()
    live = in_flight(state_dir, now)
    recs = [r for r in live if counts_toward_cap(r)]
    sid = (session_id or "").strip()
    return {"global": len(recs), "session_id": sid, "in_flight": recs,
            "uncounted": [r for r in live if not counts_toward_cap(r)],
            "session": sum(1 for r in recs if origin_session_of(r) == sid) if sid else 0}


def _uncounted_line(counts: dict) -> str:
    """One line naming the non-agent jobs running outside the cap, or `""` when there are none."""
    n = len(counts.get("uncounted") or [])
    if not n:
        return ""
    return (f"  Also running, NOT counted (no agent calls): {n} "
            f"non-agent job{'s' if n != 1 else ''}. They are not listed as kill candidates.\n")


# ----------------------------------------------------------------------- the kill-order ranking
# The hard-cap refusal presents the running jobs in recommended kill order — so the ranking has to be
# defensible line by line, because the owner is going to act on it.
#
# **It is a LEXICOGRAPHIC SORT, not a score.** A weighted score invents magnitudes ("a violation is
# worth 30 and a wake waiter is worth -15") that nobody can defend and that silently trade one signal
# against another. A tuple sort claims only an ORDER, which is the only thing actually argued below,
# and it cannot produce a surprise where two mild signals outvote a decisive one.
#
# **Five keys, most-killable first, each read off a field that is already on the record:**
#
#   1. `status == retry-pending` — **nothing is running.** A job in its backoff has no child process
#      at all (§7: the shim exits and the record carries `next_attempt_at`), so cancelling it discards
#      no in-flight work by construction. Strictly the cheapest thing to take, on evidence, not taste.
#   2. `reason_class == floated-idea` — the module's OWN vocabulary calls this a violation, not a
#      category (`REQUEST_REASON_VIOLATION`): the owner was still thinking out loud and it became a
#      brief.
#      A job that should not have been started is the next-best thing to take.
#   3. **who asked** — `assistant` (2) over `unresolved` (1) over `owner` (0). Rung 3 is a POSITIVE
#      claim that the assistant started it itself, so nobody is waiting on the answer; `owner` means
#      the owner asked. `unresolved` sits between them deliberately and honestly: it MIGHT be the
#      owner's, so it is safer to take than one known to be the assistant's and less safe than one
#      known to be the owner's.
#   4. **is a session waiting on this specific result** — `--wake` or `--lease`. Below "who asked", and
#      the reason is checkable rather than a feeling: **`--wake`'s own contract is that the push fires
#      either way** (its help text, and §2's invariant table — `cancelled` notifies like every other
#      terminal state), so cancelling a `--wake` job costs the re-entry, never the report. Who asked
#      for the work is the question the owner would actually decide on; who is waiting to hear is a delivery
#      preference on top of it.
#   5. **youngest first** — least work discarded. A job three hours in has three hours to lose; one
#      three minutes in has three minutes. Last key, so it only ever breaks ties inside a band.
#
# **What is deliberately NOT a key: the title and the goal line.** They are prose, and ranking on prose
# would be exactly the invented score this avoids — a keyword list deciding that "fix" outranks "feat"
# is a judgement with no evidence behind it. They are PRINTED instead, on every row, because they are
# what makes the list decidable by the one reader who can weigh them.
_REQUESTER_KILL_RANK = {"assistant": 2, "unresolved": 1, "owner": 0}


def kill_rank(rec: dict, now: datetime | None = None) -> tuple:
    """The lexicographic sort key above. Higher sorts earlier, i.e. kill this one first.

    Total by construction — every component has a floor — because this runs inside a refusal, and a
    refusal that raises on one malformed record costs the whole ranked list."""
    now = now or _utc_now()
    by = job_request(rec).get("by")
    return (
        1 if rec.get("status") == RETRY_PENDING else 0,
        1 if request_is_violation(rec) else 0,
        _REQUESTER_KILL_RANK.get(by, 1),
        0 if (rec.get("wake") or rec.get("lease")) else 1,
        -_elapsed_since_created(rec, now),
    )


def kill_signals(rec: dict, now: datetime | None = None) -> list:
    """The reasons this row sits where it does — one short clause per key, in key order. **Printed with
    every row, so the ranking argues for itself** rather than asking the owner to trust an order whose
    basis they cannot see. The last three always speak, including when the answer is "nothing here":
    who asked is reported even when it is `unresolved`, because an absent attribution is a fact the
    owner would weigh."""
    now = now or _utc_now()
    out = []
    if rec.get("status") == RETRY_PENDING:
        out.append("waiting out a retry backoff — nothing is running")
    if request_is_violation(rec):
        out.append(f"reason_class {REQUEST_REASON_VIOLATION} — "
                   f"{REQUEST_REASON_WORDS[REQUEST_REASON_VIOLATION]}")
    by = job_request(rec).get("by")
    if by == "assistant":
        out.append("the assistant started this itself (rung 3) — the owner didn't ask for it")
    elif by == "owner":
        out.append("the owner asked for this")
    else:
        out.append("who asked is unresolved — it MIGHT be the owner's")
    waiters = [w for w, on in (("--wake", rec.get("wake")), ("--lease", rec.get("lease"))) if on]
    if waiters:
        out.append(f"{' + '.join(waiters)} — a session is waiting on this result "
                   "(the completion push still fires if you cancel it)")
    out.append(f"running {_human_duration(_elapsed_since_created(rec, now))} — "
               "that much work is discarded")
    return out


def rank_kill_order(recs: list, now: datetime | None = None) -> list:
    """The in-flight set, most-killable first. Pure — it never cancels anything and never writes."""
    now = now or _utc_now()
    return sorted(recs, key=lambda r: kill_rank(r, now), reverse=True)


def kill_order_report(recs: list, now: datetime | None = None) -> str:
    """The ranked list, as the owner reads it: rank, id, age, title, the signals that placed it, the goal
    line it was started with, and **the `cancel` command that acts on the row.**

    The command is on every row on purpose. The whole complaint this feature answers is that a bare
    refusal hands the reconstruction work back to the owner; a row they then have to translate into a
    job id and a flag order is the same cost in a smaller font."""
    now = now or _utc_now()
    lines = []
    for n, rec in enumerate(rank_kill_order(recs, now), 1):
        age = _human_duration(_elapsed_since_created(rec, now))
        lines.append(f"  {n}. {rec.get('id')}  [{age}]  {one_line(rec.get('title'))}")
        for signal in kill_signals(rec, now):
            lines.append(f"       · {signal}")
        goal = origin_goal(rec)
        if goal:
            lines.append(f"       goal: {goal}")
        lines.append(f"       cancel: python jobs.py cancel {rec.get('id')} "
                     f'--why "making room for <the new job>"')
    return "\n".join(lines)


_RANKED_ON = ("  Ranked on: retry-pending state, origin.request.reason_class, origin.request.by, "
              "--wake/--lease, and age. Titles and goals are printed, never ranked on.\n"
              "  Nothing was started and no record was written.")


def hard_cap_refusal(state_dir: str, now: datetime | None = None,
                     cap: int = HARD_CAP_GLOBAL) -> str | None:
    """The 5, across every origin. Refusal text, or None.

    **No override exists and none should be added.** That is the single thing distinguishing it from
    the 3, and an escape hatch here would make the two caps one cap with two spellings."""
    now = now or _utc_now()
    counts = concurrency(state_dir, None, now)
    if counts["global"] < cap:
        return None
    return (f"{counts['global']} agent jobs are already in flight and the hard cap is {cap} "
            f"(only jobs that make agent calls count).\n"
            f"There is no flag for this one. Cancel one of these and start yours again — ranked by "
            f"recommended kill order, most-killable first:\n"
            f"{kill_order_report(counts['in_flight'], now)}\n"
            f"{_uncounted_line(counts)}"
            f"{_RANKED_ON}")


def soft_cap_refusal(state_dir: str, session_id: str | None, now: datetime | None = None,
                     cap: int = SOFT_CAP_PER_ORIGIN) -> str | None:
    """The 3, per ORIGIN SESSION. Refusal text, or None.

    **It stops, and it names the one flag that gets past it in the same breath.** A cap that printed a
    warning and started the job anyway would be a prose rule with a compile step, because nothing about
    it can refuse. A cap with no way past it would be the 5 again, and overrunning this one can be
    RIGHT for the job that matters. `--over-soft-cap` is the difference: deliberate, one flag,
    and stamped on the record so how often it is used is a measurement rather than a memory.

    **A caller with no session id is never held** — the reservation is for the owner's interactive
    sessions, and a job with no session address is not competing for one. **That property is guarded
    TWICE and the duplication is deliberate but worth naming**: the early return below, and
    `concurrency`'s own `if sid else 0`. They mutually mask — deleting either one alone changes no
    answer, so neither is killable on its own and no test can pin the pair by exercising this
    function. The property therefore belongs to `concurrency` and is pinned
    THERE (`test_concurrency_zeroes_the_session_count_for_a_caller_with_no_session_id`); the early
    return is readability, so that this function states its own rule rather than deferring it."""
    now = now or _utc_now()
    sid = (session_id or "").strip()
    if not sid:
        return None
    counts = concurrency(state_dir, sid, now)
    if counts["session"] < cap:
        return None
    mine = [r for r in counts["in_flight"] if origin_session_of(r) == sid]
    return (f"this session already has {counts['session']} agent jobs in flight and the soft cap "
            f"is {cap} "
            f"(it reserves room for the owner's interactive sessions).\n"
            f"THIS IS HEADROOM, NOT MACHINE LOAD — it is the owner's concurrency being spent, not the "
            f"box's. If this job is worth one of those slots, say so and it runs: add --over-soft-cap. "
            f"Overrunning it can be the right call.\n"
            f"This session's in-flight jobs, ranked as kill candidates in case one of them is the "
            f"cheaper thing to drop:\n"
            f"{kill_order_report(mine, now)}\n"
            f"  Global: {counts['global']} of {HARD_CAP_GLOBAL} in flight.\n"
            f"{_uncounted_line(counts)}"
            f"{_RANKED_ON}")


def over_soft_cap_note(state_dir: str, session_id: str | None, now: datetime | None = None,
                       cap: int = SOFT_CAP_PER_ORIGIN) -> dict | None:
    """What `--over-soft-cap` writes onto the record — or None when the flag was passed but the cap was
    never reached.

    **Absent rather than false**, the same posture as `retry` / `analyze` / `worktree`: a record must
    never imply a policy it doesn't have, and a job started at one-of-three that happened to carry a
    spare flag did not override anything. That is what makes the field countable — every one of these
    on disk is a real overrun with the owner's headroom actually spent."""
    now = now or _utc_now()
    sid = (session_id or "").strip()
    if not sid:
        return None
    counts = concurrency(state_dir, sid, now)
    if counts["session"] < cap:
        return None
    return {"at": _stamp(now), "cap": cap, "session_active": counts["session"],
            "global_active": counts["global"]}


def start_job(state_dir: str, title: str, argv: list, *, cwd: str | None = None,
              channel: str = "telegram", wake: bool = False, lease: bool = False,
              deadline_sec: int = DEFAULT_DEADLINE_SEC, origin: dict | None = None,
              runner=subprocess.Popen, now: datetime | None = None,
              python_bin: str | None = None, retry: dict | None = None,
              analyze: bool = False, analysis_for: str | None = None,
              worktree: bool = False, worktree_root: str | None = None,
              wt_runner=None, over_soft_cap: dict | None = None,
              agent: bool | None = None) -> dict:
    """Write the record, then spawn the detached shim that runs `argv`. Returns the record.

    The record is written BEFORE the spawn and re-written after, so a crash in between leaves a
    visible job rather than an invisible orphan process. A spawn that raises is recorded as a
    terminal `failed` job — which means it still notifies, rather than vanishing.

    `runner` is injectable (tests pass a stub) — `subprocess.Popen`-compatible: called as
    `runner(argv, **kwargs)` and expected to expose `.pid`.

    `retry` is a `retry_config()` block or None. **None is the default and means the old behaviour
    exactly**: no per-attempt log delimiter, one attempt, one push, terminal on the first failure.
    It still gets an `attempts[]` — one entry, carrying that failure's classification — because
    recording is not retrying (module docstring). `deadline_sec` stays PER ATTEMPT (each attempt gets
    the full deadline); the whole-job bound is the retry window.

    `origin` is the return address (`build_origin`), normalized on the way in so the one-line goal cap
    is enforced once, here, rather than at every read. `None`/`{}` is the default and means exactly
    what it has always meant: nobody said who was calling.

    `analysis_for` marks this job as the fresh-eyes analysis OF another job, and **an analysis job is
    refused an origin, in code** — the recursion guard (§4.3), the same shape as `session_stamp.py`'s
    `RECURSION_ENV` (*never dream the dreamer*). An analysis with no address cannot be routed, so an
    analysis-of-an-analysis cannot close a loop. Two agents that can address each other is a very
    expensive chat room, and the module's whole guarantee is that every job reaches a terminal
    state — which a conversation never does.

    `over_soft_cap` is `over_soft_cap_note()`'s block when the per-session soft cap was actually
    overrun and the caller said so with `--over-soft-cap`, and **None every other time** — including
    when the flag was passed at one-of-three, where nothing was overridden. Recorded here rather than
    at the CLI so it lands in the same write as the rest of the record. **THE CAPS THEMSELVES ARE NOT
    CHECKED HERE**, deliberately, for `preflight_refusal`'s reason one paragraph up: `request_analysis`
    and the retry respawn both call this in code, and neither may be blockable. An analysis eaten by a
    cap is the fresh eyes a job asked for, silently cancelled — and a retry
    respawn is not a new job at all: its record already exists and was already counted when it started.

    `worktree=True` runs the job in a private, disposable checkout cut from `origin/develop`
    (`job_worktree.create`) instead of in whatever tree the caller was sitting in. The isolation is
    established BEFORE the spawn, deliberately: an agent that creates its own worktree does it after
    it has already started reading and editing in the shared tree, which is exactly where collisions
    do their damage. **A worktree that cannot be created FAILS the job** — a
    terminal, notifiable `failed` record with nothing spawned, exactly like a failed spawn. It must
    never fall back to the shared tree, because a job silently running in the wrong directory is the
    failure this flag exists to remove.

    `agent` is the caller's explicit answer to "does this job make agent/model calls" (`--agent` /
    `--no-agent`), stamped as `agent: true|false` and preferred over the argv heuristic by
    `job_agent_class`. **None is the default and writes nothing** — the heuristic decides, and an
    unrecognised shape counts toward the caps."""
    if analysis_for and normalize_origin(origin):
        raise ValueError("an analysis job may not carry an origin — one pass, by construction "
                         "(job-origin-routing-spec §4.3)")
    now = now or _utc_now()
    job_id = new_job_id(now)
    wt, wt_error = None, None
    if worktree:
        try:
            wt = job_worktree.create(job_id, root=worktree_root, runner=wt_runner, now=now)
        except Exception as e:  # noqa: BLE001 — every failure mode is "the job must not run here"
            wt_error = job_worktree.one_line(e) or e.__class__.__name__
    # §3.13's RESUME layer needs a session id set up FRONT — there is no way to learn one from
    # outside a `claude -p` process once it has already started. Additive and silent for everything
    # that isn't a bare single-prompt `claude` invocation with no session id of its own already.
    argv, claude_session_id = job_completion.prepare_argv(argv)
    rec = {
        "schema": SCHEMA,
        "id": job_id,
        "title": title or job_id,
        "argv": list(argv),
        # The worktree IS the cwd when there is one — that is the whole feature. `--worktree` and
        # `--cwd` are refused together at the CLI, so this is never a silent override.
        "cwd": (wt["path"] if wt else os.path.abspath(cwd or REPO_ROOT)),
        "log_path": log_path(state_dir, job_id),
        "status": RUNNING,
        "pid": None,
        "child_pid": None,
        "created_at": _stamp(now),
        "started_at": _stamp(now),
        "ended_at": None,
        "exit_code": None,
        "deadline_sec": int(deadline_sec) if deadline_sec else DEFAULT_DEADLINE_SEC,
        "lease": bool(lease),
        "wake": bool(wake),
        "notify": {"channel": channel or "telegram"},
        "notified_at": None,
        # The return address (§3.2). Normalized HERE, at the one write point, so the one-line goal
        # cap is structural. `{}` when nobody said who was calling, which must keep reading
        # identically to a missing key.
        "origin": normalize_origin(origin),
        # Always present, always the same shape: one entry per attempt that reached a terminal
        # outcome, carrying that attempt's transient/terminal verdict. A non-retry job ends with
        # exactly one. Reader-simple beats caller-invisible — see the module docstring.
        "attempts": [],
    }
    if over_soft_cap:
        # Absent rather than false, like `retry`/`analyze`/`worktree`: every one of these on disk is a
        # real overrun of the owner's interactive headroom, so the field stays countable instead of being a
        # flag someone passed out of habit. `over_soft_cap_note` is what decides that, not the flag.
        rec["over_soft_cap"] = dict(over_soft_cap)
    if agent is not None:
        # Absent unless the caller SAID — so every stamp on disk is a deliberate classification, and
        # a record without one is visibly the heuristic's call (`job_agent_class`).
        rec["agent"] = bool(agent)
    if analyze:
        # Opt-in per job (it is a model call) and, like `retry`, absent from a plain record rather than
        # written false — a record must never imply a policy it doesn't have.
        rec["analyze"] = True
    if analysis_for:
        rec["analysis_for"] = analysis_for
    if retry:
        # The retry BOOKKEEPING is still only present when asked for, so a plain job's record carries
        # no fields that imply a retry policy it doesn't have.
        rec["retry"] = dict(retry)
        rec["attempt"] = 1
        rec["next_attempt_at"] = None
        rec["retry_outcome"] = None
    if wt:
        # Same posture as `retry`/`analyze`: absent from a plain job's record rather than written
        # empty, so a record never implies isolation it doesn't have.
        rec["worktree"] = dict(wt)
    if claude_session_id:
        # Same posture again: absent for every job that isn't a bare `claude -p` invocation, so
        # `job_completion.can_resume` reading its absence as "cannot resume" needs no separate flag.
        rec["claude_session_id"] = claude_session_id
    if wt_error:
        # Born already finished, like a failed spawn — terminal, so `reconcile` still pushes it, and
        # NOTHING was spawned. The distinction that matters is in `argv` never having run: a
        # `--worktree` job that could not get one has not started somewhere else, it has not started.
        rec["worktree"] = {"requested": True, "path": None, "error": wt_error}
        rec["status"] = FAILED
        rec["ended_at"] = _stamp(now)
        rec["error"] = f"worktree setup failed: {wt_error}"
        if rec.get("retry"):
            rec["retry_outcome"] = "worktree-failed"
        save_job(state_dir, rec)
        return rec
    return _spawn_attempt(state_dir, rec, runner=runner, python_bin=python_bin, now=now)


def _spawn_attempt(state_dir: str, rec: dict, *, runner=subprocess.Popen,
                   python_bin: str | None = None, now: datetime | None = None) -> dict:
    """(Re)spawn the detached shim for this record's current attempt, and save it either side.

    Shared by `start_job` and the retry path in `reconcile` deliberately: a second copy of the
    detach flags / API-key scrub / log-handle dance would be the obvious place for the two to drift,
    and the retry path's whole safety argument is that a retried attempt is spawned *identically* to
    a first one."""
    now = now or _utc_now()
    rec["status"] = RUNNING
    rec["started_at"] = _stamp(now)
    rec["pid"] = None
    rec["child_pid"] = None
    rec["ended_at"] = None
    rec["exit_code"] = None
    if rec.get("retry"):
        rec["next_attempt_at"] = None
    save_job(state_dir, rec)

    # `--state-dir` is a TOP-LEVEL argument, so it must precede the subcommand — argparse rejects it
    # after `__run`, and the shim then dies instantly with a usage error (which the job would then
    # correctly report as `ended-unknown` rather than silently passing).
    shim = [python_bin or sys.executable, os.path.join(SCRIPT_DIR, "jobs.py"),
            "--state-dir", state_dir, "__run", rec["id"]]
    kwargs: dict = {"cwd": rec.get("cwd") or REPO_ROOT, "env": child_env(rec.get("id"))}
    log_fh = None
    try:
        os.makedirs(jobs_dir(state_dir), exist_ok=True)
        log_fh = open(rec["log_path"], "a", encoding="utf-8")
        kwargs["stdout"] = log_fh
        kwargs["stderr"] = log_fh
    except OSError:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    kwargs["stdin"] = subprocess.DEVNULL  # a detached job has no console to read from
    if os.name == "nt":
        # The standard detached pair — no CREATE_NO_WINDOW on top: DETACHED_PROCESS
        # already means "no console", and MSDN documents the two flags as mutually exclusive.
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True

    try:
        proc = runner(shim, **kwargs)
        rec["pid"] = proc.pid
    except (OSError, ValueError) as e:
        # A spawn that never happened is still a job that must report — terminal + notifiable, not a
        # silent no-op. This is the one place a job can be born already finished. It applies to a
        # RETRY spawn too: a failed respawn ends the job rather than leaving it pending forever.
        rec["status"] = FAILED
        rec["ended_at"] = _stamp(now)
        rec["exit_code"] = None
        rec["error"] = f"spawn failed: {e}"
        if rec.get("retry"):
            rec["retry_outcome"] = "spawn-failed"
    finally:
        # Our copy of the handle has served its purpose (the child holds its own duplicate). Closing
        # it matters because `start_job` is importable and may be called in-process by a long-lived
        # caller, where one leaked handle per job would accumulate.
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                pass
    save_job(state_dir, rec)
    return rec


# --------------------------------------------------------------------------- fresh-eyes analysis

def request_analysis(state_dir: str, job_id: str, *, goal=None, analyst: str = "warm",
                     runner=subprocess.Popen, python_bin: str | None = None,
                     now: datetime | None = None) -> dict:
    """Spawn the analysis of a failed job: a **second, origin-less job** whose command delegates,
    validates and stages `state/jobs/<job_id>.analysis.json`. Returns the analysis job's record.

    Raises `ValueError` — with a message meant to be read by whoever asked — rather than silently
    doing nothing, because every refusal here is a real answer:

      * the job doesn't exist, or isn't one of `ANALYSABLE`;
      * **it already has an analysis** (one pass, and a second delegation is real spend);
      * **it IS an analysis** (§4.3's recursion guard, checked on both sides: an analysis carries no
        origin, so it could not be routed anyway, but refusing here says why);
      * **there is no goal line**. An analysis with no goal is an analyst
        guessing what "working" would have looked like, and the goal is half the input contract —
        so this is refused in code rather than left to a caller's judgment.

    The analysis is a JOB rather than an inline call on purpose: it is a model one-shot that can take
    minutes, and everything this module exists for — detached survival, one guaranteed push at the
    outcome, a record that outlives the session — applies to it exactly as much as to the work it is
    analysing."""
    rec = load_job(state_dir, job_id)
    if rec is None:
        raise ValueError(f"no such job: {job_id}")
    if rec.get("analysis_for"):
        raise ValueError(f"{job_id} IS an analysis — the analyst answers once, and an analysis of an "
                         "analysis has nowhere to go (spec §4.3)")
    if rec.get("status") not in ANALYSABLE:
        raise ValueError(f"{job_id} is {rec.get('status')}; analysis is for "
                         f"{' / '.join(ANALYSABLE)}")
    if rec.get("analysis_job"):
        raise ValueError(f"{job_id} already has an analysis job ({rec['analysis_job']})")
    line = one_line(goal) or origin_goal(rec)
    if not line:
        raise ValueError(f"{job_id} has no goal line — pass --goal, or start the job with one. An "
                         "analysis without it is an analyst guessing what 'working' would have "
                         "looked like.")
    try:
        import job_analysis  # deferred: job_analysis imports THIS module, so a top-level import
                             # would be circular. Same posture as `_registry_entry`'s sentinel.
    except Exception:  # noqa: BLE001 — the executor re-checks, so a broken import costs the
        pass           # up-front message, never the guard itself
    else:
        if analyst not in job_analysis.ANALYSTS:
            # Refused HERE, before spawning: an unknown analyst must not become a job that can
            # only fail (and then push a failure the owner has to read).
            raise ValueError(f"unknown analyst {analyst!r} (known: "
                             f"{', '.join(job_analysis.ANALYSTS)})")

    argv = [python_bin or sys.executable, os.path.join(SCRIPT_DIR, "job_analysis.py"),
            "--state-dir", state_dir, "run", "--job", job_id, "--goal", line,
            "--analyst", analyst]
    # NOT `rec["cwd"]`. For a `--worktree` job that path is the private worktree, and it has already
    # been torn down by the time reconcile reaches here — see `analysis_cwd`, which picks the host
    # repo and verifies it exists rather than inheriting a directory scheduled for deletion.
    sub = start_job(state_dir, f"analysis: {rec.get('title') or job_id}", argv,
                    cwd=analysis_cwd(rec),
                    channel=(rec.get("notify") or {}).get("channel") or "telegram",
                    analysis_for=job_id, runner=runner, python_bin=python_bin, now=now)
    # Recorded on the TARGET, so "has this been analysed?" is answerable from the job you actually
    # care about — and so a second request is refused rather than quietly spending again.
    rec["analysis_job"] = sub["id"]
    save_job(state_dir, rec)
    return sub


def attempt_header(attempt: int, total: int, when: datetime) -> str:
    """The per-attempt delimiter written into the log, so N attempts' output stays distinguishable
    instead of blurring into one blindly-appended stream. Only written for retry-enabled jobs — a
    plain job's log keeps exactly the shape it had before this existed."""
    return f"===== attempt {attempt}/{total} · {_stamp(when)} =====\n"


def _open_attempt_log(rec: dict, attempt: int, when: datetime) -> tuple:
    """Open the job log for append and, for a retry-enabled job, stamp this attempt's header.

    Returns `(handle_or_None, start_offset, error_or_None)` where `start_offset` is the byte position
    the attempt's own output begins at — recorded on the attempt so the classifier reads THIS
    attempt's output and not attempt 1's stale error.

    **The error is returned rather than swallowed.** A handle we cannot open sends the
    real command's output to `DEVNULL`, which is a total, permanent loss of the log — and it used to
    look identical from the outside to a child that simply hadn't printed yet. Whoever reads a 0-byte
    log next should not have to re-derive which of those two happened, so `_run_shim` stamps it on the
    record. The job itself still runs: an unwritable log is worth the output, never the work."""
    try:
        out = open(rec.get("log_path") or os.devnull, "a", encoding="utf-8")
    except OSError as e:
        return None, 0, f"log unwritable ({rec.get('log_path')}): {e}"
    offset = 0
    cfg = rec.get("retry") or None
    try:
        if cfg:
            if out.tell():
                out.write("\n")
            out.write(attempt_header(attempt, int(cfg.get("max_retries") or 0) + 1, when))
            out.flush()
        offset = out.tell()
    except (OSError, ValueError):
        offset = 0
    return out, offset, None


def _run_shim(state_dir: str, job_id: str, *, rand=random.random, clock=_utc_now) -> int:
    """The detached child: run the job's real argv, enforce its deadline, stamp the outcome — and,
    for a retry-enabled job, decide whether the outcome earns another attempt.

    Runs the deadline itself (rather than leaving it to the daemon) so a job is bounded even while
    the daemon is down — the daemon's own deadline check in `reconcile` is the backstop for the shim
    being killed, not the primary. The retry DECISION lives here for the same reason: the exit code
    and this attempt's fresh output are both in hand at the moment of failure. The retry WAIT does
    not — the shim writes `retry-pending` + `next_attempt_at` and **exits**, so nothing is sleeping in
    a process a restart can kill; `reconcile` spawns the next attempt when it comes due.

    The real command's stdout/stderr are pointed at the log file **explicitly** rather than left to
    inherit the shim's. Inheritance silently loses the output on Windows: `subprocess` only builds
    the `handle_list` that survives `close_fds=True` when handles are passed explicitly, so an
    un-redirected grandchild of a `DETACHED_PROCESS` parent writes to nothing at all — a 0-byte log,
    and the log is what `notify_text`'s "Last line:" and every
    `--wake` read depend on, so an empty one guts the feature. On Windows that same Popen also
    carries `CREATE_NO_WINDOW` — the shim is DETACHED (no console), so a console-subsystem command
    would otherwise be handed a fresh visible console, i.e. a terminal window per job.

    `clock` is called exactly twice — once at the start of the attempt, once at the end — so a test
    can pin both the attempt's measured duration and the instant the next attempt is scheduled for
    (without it, the backoff schedule and the retry window are wall-clock and untestable)."""
    rec = load_job(state_dir, job_id)
    if rec is None:
        return 2
    started = clock()
    attempt = 1
    if rec.get("retry"):
        try:
            attempt = max(1, int(rec.get("attempt") or 1))
        except (TypeError, ValueError):
            attempt = 1
    rec["started_at"] = _stamp(started)
    rec["pid"] = os.getpid()
    save_job(state_dir, rec)

    deadline = rec.get("deadline_sec") or DEFAULT_DEADLINE_SEC
    status, code = FAILED, None
    proc = None
    offset = 0
    try:
        out, offset, log_error = _open_attempt_log(rec, attempt, started)
        if log_error:
            # Say so on the record, now, rather than leaving a 0-byte log to be explained later. This
            # is the ONE case where the log is empty for a reason nothing else can tell you.
            rec["log_error"] = log_error
            save_job(state_dir, rec)
        try:
            child_kwargs: dict = {
                "cwd": rec.get("cwd") or REPO_ROOT, "env": child_env(job_id),
                "stdout": out or subprocess.DEVNULL, "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL}
            if os.name == "nt":
                # The grandchild-console rule (module docstring): the shim is DETACHED_PROCESS, so
                # without this a console-subsystem command is allocated a fresh, VISIBLE console —
                # one terminal window per job. The hidden console is inherited downward.
                child_kwargs["creationflags"] = _CREATE_NO_WINDOW
            proc = subprocess.Popen(spawnable_argv(rec["argv"]), **child_kwargs)
            rec["child_pid"] = proc.pid
            save_job(state_dir, rec)
            code = proc.wait(timeout=deadline)
            status = DONE if code == 0 else FAILED
        finally:
            if out is not None:
                out.close()
    except subprocess.TimeoutExpired:
        status = TIMED_OUT
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    except (OSError, ValueError) as e:
        status, code = FAILED, None
        rec["error"] = f"run failed: {e}"

    ended = clock()
    # Re-read before stamping: `cancel` may have written a terminal state while we ran, and it wins.
    # This is RELIABLE rather than hopeful — `cancel_job` claims the record BEFORE it kills, and the
    # kill is what let us out of `proc.wait()`, so the claim is always already on disk by here. Kill-
    # first would make this a TOCTOU that a cancel could lose (spec §3.10); `honour_cancel`
    # backstops the remaining interleavings at the write itself.
    latest = load_job(state_dir, job_id) or rec
    if latest.get("status") in TERMINAL:
        return code if isinstance(code, int) else 1
    latest["child_pid"] = rec.get("child_pid")
    if rec.get("error"):
        latest["error"] = rec["error"]

    # ONE path for every job, retry-enabled or not: classify, record, then decide. The classifier is
    # the same single copy either way — `plan_retry` simply refuses to act on the verdict when there
    # is no retry config. A plain job comes out of here exactly as terminal as it always was.
    retry_enabled = bool(latest.get("retry"))
    duration = max(0.0, (ended - started).total_seconds())
    log_file = latest.get("log_path") or ""
    # **An exit-0 attempt whose own parting lines declare a failure is UNREADABLE, not done.**
    # Placed before `plan_retry` so `attempts[]` records the real outcome, and it can
    # only ever move `done` → `ended-unknown`: never the reverse, never to `failed`, and never for an
    # attempt that already reported a failure of its own. `ended-unknown` is not retryable, so
    # nothing new re-runs because of this — and it IS in `ANALYSABLE`, so `--analyze` finally fires
    # for the case it was added for.
    if status == DONE:
        contradiction = done_contradiction(log_tail_lines(log_file, offset))
        if contradiction:
            status = ENDED_UNKNOWN
            latest["unknown_reason"] = {"kind": "log-contradicts-exit-code",
                                        "exit_code": code, **contradiction}
    # **Classified from the failure END**, floored at this attempt's own offset: `_CLASSIFY_TAIL_BYTES`
    # is the window, not `read_log_span`'s 64 KiB default, which for any log under 64 KiB means "from
    # byte 0" and can match a signature from long before the job died. `output_bytes` is measured over the WHOLE
    # attempt span instead, because `fast_fail` means "this attempt printed almost nothing" — a
    # property of the attempt, not of the window the classifier happens to read.
    output = read_log_span(log_file, offset, limit=_CLASSIFY_TAIL_BYTES)
    plan = plan_retry(latest, status=status, output=output, duration_sec=duration,
                      output_bytes=log_span_bytes(log_file, offset),
                      now=ended, rand=rand)
    attempts = latest.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
    attempts.append({
        "attempt": attempt,
        "started_at": _stamp(started),
        "ended_at": _stamp(ended),
        "duration_sec": round(duration, 3),
        "exit_code": code,
        "outcome": status,
        # Whether retry was on AT THE TIME. Without it the record can't distinguish "transient, and
        # we chose not to retry" from "transient, and we had no policy" — which is the whole
        # question this field exists to answer.
        "retry_enabled": retry_enabled,
        "classification": plan["classification"],
        "signature": plan["signature"],
        "fast_fail": plan["fast_fail"],
        "confidence": plan["confidence"],
        "decision": plan["reason"],
        "log_offset": offset,
        "next_attempt_at": plan["next_attempt_at"],
    })
    latest["attempts"] = attempts
    latest["exit_code"] = code
    if plan["retry"]:
        # Not terminal, so nothing notifies. `pid` is cleared because this shim is about to exit and a
        # stale PID would either read as a live job or (worse, after PID reuse) as somebody else's.
        latest["status"] = RETRY_PENDING
        latest["attempt"] = attempt + 1
        latest["next_attempt_at"] = plan["next_attempt_at"]
        latest["ended_at"] = None
        latest["pid"] = None
    else:
        latest["status"] = status
        latest["ended_at"] = _stamp(ended)
        if retry_enabled:
            # Retry bookkeeping stays off a plain job's record — it never had a policy, so there is
            # no "how did retry end" to report.
            latest["next_attempt_at"] = None
            latest["retry_outcome"] = _retry_outcome(status, attempt, plan["reason"])
    save_job(state_dir, latest)
    return code if isinstance(code, int) else 1


def _retry_outcome(status: str, attempt: int, reason: str) -> str | None:
    """The one-word "how did retry end" field, so the record explains itself without the log."""
    if status == DONE:
        return "succeeded-after-retry" if attempt > 1 else "succeeded-first-try"
    return {"retries-exhausted": "exhausted",
            "retry-window-exhausted": "window-expired",
            "no-transient-signature": "not-transient"}.get(reason, reason or None)


# --------------------------------------------------------------------------- terminal detection

def _elapsed_sec(rec: dict, now: datetime) -> float:
    """Since the CURRENT attempt started. That is what `deadline_sec` is measured against — each
    attempt gets the full deadline; the whole-job bound is `retry.window_sec`."""
    try:
        return max(0.0, (now - parse_iso(rec.get("started_at") or rec.get("created_at"))).total_seconds())
    except (ValueError, TypeError, AttributeError):
        return 0.0


def _elapsed_since_created(rec: dict, now: datetime) -> float:
    """Since the job was CREATED — total wall clock across every attempt. Used by the lease guard and
    by the push's duration, both of which mean "how long has this been going," not "how long has this
    attempt been going": the shim re-stamps `started_at` on every retry."""
    try:
        return max(0.0, (now - parse_iso(rec.get("created_at") or rec.get("started_at"))).total_seconds())
    except (ValueError, TypeError, AttributeError):
        return 0.0


def attempt_count(rec: dict) -> int:
    """How many attempts have actually run. 1 for every job that isn't retry-enabled."""
    attempts = rec.get("attempts")
    if isinstance(attempts, list) and attempts:
        return len(attempts)
    try:
        return max(1, int(rec.get("attempt") or 1))
    except (TypeError, ValueError):
        return 1


def _ended_dt(rec: dict) -> datetime:
    """`ended_at` as a datetime, falling back to now on anything unparseable. Deliberately total: a
    garbled timestamp must not raise out of `notify_text`, because `reconcile` would then log the
    error and skip the push — and it would do so again on every subsequent pass, turning a cosmetic
    corruption into a permanently silent job. The duration in the message is the only casualty."""
    try:
        return parse_iso(rec["ended_at"])
    except (KeyError, ValueError, TypeError, AttributeError):
        return _utc_now()


def _human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def check_terminal(rec: dict, now: datetime) -> dict | None:
    """Decide whether a `running` record has actually ended, WITHOUT side effects (so it's trivially
    testable). Returns the fields to merge, or None if it's genuinely still running.

    Order matters: deadline first (a wedged job that still holds a live PID must still be reported),
    then liveness. `pid_alive` is conservative, so the deadline is what closes the loop when the
    probe can't answer — see the module docstring.

    **`unknown_reason.kind` names which of the two `ended-unknown` shapes this is** —
    `notify_text`/`wake_text` read it to say WHICH HALF is unreadable, the same posture
    `contradiction_of`'s `log-contradicts-exit-code` kind already has. `"never-started"` (no PID was
    ever recorded — nothing ran) reads very differently from `"pid-gone-no-stamp"` (a real process
    WAS running and vanished without a trace); a bare "no exit code" wearing either one's clothes
    devalues the alarm."""
    if rec.get("status") != RUNNING:
        return None
    elapsed = _elapsed_sec(rec, now)
    deadline = rec.get("deadline_sec") or DEFAULT_DEADLINE_SEC
    if elapsed >= deadline:
        return {"status": TIMED_OUT, "ended_at": _stamp(now)}
    pid = rec.get("pid")
    if not pid:
        # A `running` record with no PID means the spawn never got far enough to record one (the
        # daemon or the machine died between `save_job` and `runner(...)`). Without this it would sit
        # `running` until the 6 h deadline — not silent, but uselessly slow for an obvious failure.
        return ({"status": ENDED_UNKNOWN, "ended_at": _stamp(now),
                 "unknown_reason": {"kind": "never-started"}}
                if elapsed >= STARTUP_GRACE_SEC else None)
    if not pid_alive(pid):
        # The shim exits only after stamping a terminal status OR `retry-pending`, so a gone PID with
        # the record still on `running` means the shim itself died (killed, machine crash, daemon-wide
        # kill) — the outcome is genuinely unreadable. Say so; never assume success.
        return {"status": ENDED_UNKNOWN, "ended_at": _stamp(now),
                "unknown_reason": {"kind": "pid-gone-no-stamp"}}
    return None


def check_retry_due(rec: dict, now: datetime) -> str | None:
    """For a `retry-pending` record: `"due"` (spawn the next attempt now), `"expired"` (stop retrying
    and report a failure), or None (still waiting out the backoff). No side effects.

    An unreadable `next_attempt_at` reads as **due**, not as "wait forever": a garbled timestamp must
    cost the backoff, never the job. Same total-function posture as `_ended_dt`."""
    if rec.get("status") != RETRY_PENDING:
        return None
    cfg = rec.get("retry") or {}
    try:
        attempt = max(1, int(rec.get("attempt") or 1))
    except (TypeError, ValueError):
        attempt = 1
    if attempt > int(cfg.get("max_retries") or 0) + 1:
        return "expired"  # belt-and-braces: the shim already refuses past the budget
    end = retry_window_end(rec)
    if end is not None and now >= end:
        return "expired"
    try:
        due = parse_iso(rec.get("next_attempt_at"))
    except (ValueError, TypeError, AttributeError):
        due = None
    if due is None:
        return "due"
    return "due" if now >= due else None


def retry_note(rec: dict) -> str:
    """The one line the push adds when a job actually took more than one attempt — so a job that
    succeeded on try 4 never reads as a clean first-try success, and an exhausted one says so.

    Returns "" for a single-attempt job, which is every job that didn't ask for `--retry`."""
    cfg = rec.get("retry") or None
    if not cfg:
        return ""
    n = attempt_count(rec)
    outcome = rec.get("retry_outcome")
    if n <= 1 and outcome not in ("window-expired", "exhausted"):
        # One attempt and nothing retry-shaped happened — it behaved like an ordinary job, so say
        # nothing. (`window-expired` CAN land on attempt 1: a backoff that would fall outside the
        # window is refused, and that decision is worth reporting rather than hiding.)
        return ""
    allowed = int(cfg.get("max_retries") or 0) + 1
    attempts = rec.get("attempts") if isinstance(rec.get("attempts"), list) else []
    transient = sum(1 for a in attempts
                    if isinstance(a, dict) and a.get("classification") == CLASS_TRANSIENT)
    sigs = [a.get("signature") for a in attempts
            if isinstance(a, dict) and a.get("signature")]
    seen = " · ".join(dict.fromkeys(sigs)) if sigs else ""
    if rec.get("status") == DONE:
        line = (f"Took {n} attempts of {allowed} — I absorbed "
                f"{transient} transient failure{'' if transient == 1 else 's'} getting there.")
    elif outcome == "window-expired":
        line = (f"Stopped retrying after the "
                f"{_human_duration(cfg.get('window_sec') or 0)} retry window "
                f"({n} attempt{'' if n == 1 else 's'}).")
    else:
        line = f"Gave up after {n} attempts of {allowed} ({transient} transient)."
    return f"{line} [{seen}]" if seen else line


def worktree_note(rec: dict) -> str:
    """The one line the push adds when a job's private worktree was **left on disk** — the path, and
    git's own reason for refusing to remove it.

    Returns "" for every job that had no worktree and every one whose worktree came back cleanly,
    which between them is almost all of them. It says nothing on the happy path deliberately: the
    isolation working is not news, and a line per job would train the eye past the one that matters.

    **Total, like `_ended_dt` and for the same reason.** This is called from `notify_text`, which runs
    inside `reconcile`'s per-record try — a raise here means the job never notifies at all, on this
    pass and every later one, turning a cosmetic corruption into a permanently silent job. So every
    malformed shape (`worktree` missing, not a dict, no path, a non-string reason) reads as "".
    """
    if not isinstance(rec, dict) or not rec.get("worktree_leaked"):
        return ""
    wt = rec.get("worktree")
    wt = wt if isinstance(wt, dict) else {}
    path = wt.get("path")
    path = path.strip() if isinstance(path, str) else ""
    if not path:
        return ""
    reason = one_line(wt.get("leak_reason")) if isinstance(wt.get("leak_reason"), str) else ""
    return (f"I kept its worktree — {path} — {reason or 'git refused to remove it'}. "
            "Nothing in it was deleted.")


def pr_draft_note(rec: dict) -> str:
    """The line the push adds when this job's branch had an open PR `job_pr_draft.py` drafted (or
    tried to un-draft) on its behalf. Returns "" for every job that never touched a drafted PR — the
    overwhelming majority — matching `worktree_note`'s silent-happy-path contract immediately above,
    and, for the identical reason, **total**: called from `notify_text` inside `reconcile`'s per-record
    try, so a malformed shape reads as "" rather than taking the whole push down with it."""
    if not isinstance(rec, dict):
        return ""
    entries = rec.get("pr_draft")
    if not isinstance(entries, list) or not entries:
        return ""
    lines = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        repo, pr, action = e.get("repo"), e.get("pr"), e.get("action")
        if not repo or pr is None:
            continue
        if action == "readied":
            lines.append(f"{repo} #{pr} is ready for review now that this finished cleanly.")
        elif action == "left-draft":
            lines.append(f"{repo} #{pr} stays a draft — this job didn't end cleanly.")
        elif action == "ready-failed":
            lines.append(f"{repo} #{pr} is still a draft — I couldn't reach gh to mark it ready "
                         f"({e.get('detail') or 'unknown error'}); it needs a manual `gh pr ready "
                         f"{pr}`.")
    return " ".join(lines)


def completion_of(rec) -> dict:
    """The `completion` block §3.13's guarantee wrote, or `{}`. Total, like `contradiction_of` beside
    it and for the identical reason — read from inside `notify_text`'s per-record try."""
    if not isinstance(rec, dict):
        return {}
    completion = rec.get("completion")
    return completion if isinstance(completion, dict) else {}


def completion_note(rec) -> str:
    """The line the push adds when §3.13's job-completion guarantee had to act — silent on the happy
    path, exactly like `worktree_note`/`retry_note` beside it. Returns "" whenever DETECT found
    nothing, and also whenever a RESCUE happened, because `notify_text`'s head line already carries
    that story in full (a rescued job's head line and this note would otherwise say the same thing
    twice)."""
    completion = completion_of(rec)
    reasons = completion.get("reasons")
    reasons = [r for r in reasons if isinstance(r, str)] if isinstance(reasons, list) else []
    if not reasons or isinstance(completion.get("rescue"), dict):
        return ""
    n = len(completion.get("resume_attempts") or [])
    if not n:
        return ""
    words = ", ".join(job_completion.REASON_TEXT.get(r, r) for r in reasons)
    return (f"First pass left work unfinished ({words}) — it took {n} more "
            f"turn{'' if n == 1 else 's'} to actually land it.")


def resume_permission_mismatch(rec) -> str | None:
    """One line naming it when a resume attempt ran under a different `--permission-mode` than the
    original launch — the exact gap that stranded job `20260913-214117-4562` (the pre-fix
    `resume_argv` silently dropped `--permission-mode bypassPermissions`, so the resumed session's
    `git push`/`gh`/`python` calls were all refused with no human present to approve them). `None` on
    a job that never resumed, and on a resume that carried the same mode forward — the normal case
    since `resume_argv` started carrying flags forward — so this exists to make a FUTURE regression
    visible, not to narrate routine behaviour."""
    completion = completion_of(rec)
    attempts = completion.get("resume_attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    original = job_completion.permission_mode_of(rec.get("original_argv") or rec.get("argv") or [])
    for a in attempts:
        if not isinstance(a, dict):
            continue
        resumed = job_completion.permission_mode_of(a.get("argv") or [])
        if resumed != original:
            return (f'resumed under permission-mode "{resumed or "default"}" — '
                    f'the original ran "{original or "default"}"')
    return None


def contradiction_of(rec) -> dict:
    """The `unknown_reason` block when this record is an exit-0-but-the-log-says-otherwise job, else
    `{}`. One reader for the three places that ask (the head line, the extra part, the wake line), so
    a future fourth kind of `unknown_reason` is one edit and not a hunt.

    **Total**, like `worktree_note` and for the same reason: it is read from inside `notify_text`,
    which runs inside `reconcile`'s per-record try — a raise there means the job never notifies at
    all, on this pass and every later one."""
    if not isinstance(rec, dict):
        return {}
    why = rec.get("unknown_reason")
    if not isinstance(why, dict) or why.get("kind") != "log-contradicts-exit-code":
        return {}
    return why


def never_started(rec) -> bool:
    """Is this `ended-unknown` record `check_terminal`'s `"never-started"` shape — no PID was ever
    recorded, so nothing ran — rather than a process that genuinely started and then vanished
    (`"pid-gone-no-stamp"`) or an older record with no `unknown_reason` at all (which reads as the
    latter, unchanged from before this distinction existed)?

    Total, like `contradiction_of` beside it and for the identical reason: read from inside
    `notify_text`/`wake_text`, both inside `reconcile`'s per-record try."""
    if not isinstance(rec, dict):
        return False
    why = rec.get("unknown_reason")
    return isinstance(why, dict) and why.get("kind") == "never-started"


def contradiction_note(rec) -> str:
    """The line the push adds quoting what the job said about itself. "" for every other job.

    It is worth its own part rather than being folded into `Last line:` because it is usually NOT the
    last line — a wrapper declares the failure and then echoes its cleanup on the way out, which is
    the whole shape being caught."""
    line = one_line(contradiction_of(rec).get("line"), _TAIL_CHARS)
    return f"Its log says: {line}" if line else ""


def _cancel_clause(why: str | None) -> str:
    """The reason clause, or "". It REPLACES its rung's terminal period and stays inside the head
    line — a reason is worth reading first, because Telegram's notification preview truncates and
    "why" is the part that turns an alert into an acknowledgement, so it must not become a fourth
    newline-joined part below the fold (spec §5)."""
    if not why:
        return ""
    tail = why if why[-1] in ".!?…" else why + "."
    return f": {tail}"


def cancelled_by_of(rec: dict) -> dict:
    """`rec['cancelled_by']` as a dict, or `{}` for anything else (absent, a string, a stale shape
    from a hand-edited record). The one place every reader of this field re-validates it, rather
    than trusting `isinstance` checks scattered at each call site."""
    by = rec.get("cancelled_by") if isinstance(rec, dict) else None
    return by if isinstance(by, dict) else {}


def cancel_request_of(rec: dict) -> dict:
    """`rec['cancel_request']` as a dict, or `{}`. Same posture as `cancelled_by_of`, and the same
    reason: a record round-trips unknown keys, so nothing here may assume the shape survived."""
    req = rec.get("cancel_request") if isinstance(rec, dict) else None
    return req if isinstance(req, dict) else {}


def _assistant_name() -> str:
    """The assistant's configured name for owner-facing push text (`identity_common`, which never
    raises and defaults to "the assistant"). Total: this is read from inside `notify_text`, so any
    failure falls back to the default rather than costing the push."""
    try:
        import identity_common

        return identity_common.assistant_name(identity_common.load_identity()) or "the assistant"
    except Exception:  # noqa: BLE001 — a name is cosmetic; the push is not
        return "the assistant"


def cancel_wake_suppressed(rec: dict) -> bool:
    """Should this CANCELLED job skip the wake entirely, and get the short "cancelled by <assistant>"
    push instead of the ordinary rung-1 wording? A cancel the assistant decided on and explained in
    the same turn must not come back as a wake that re-reads the log and "tells the owner plainly what
    happened" about something they were already told.

    **True exactly when an assistant surface cancelled this on its own initiative** — `cancelled_by`
    names a session AND its `source` is in `ASSISTANT_SURFACES` (the warm daemon session or a desktop
    `/assistant`, the singular first-person "I") AND `cancel_request.by != "owner"`. The turn that made
    the cancel *is* the report; a push and a wake that repeat it are the same fact told twice.

    **False for everything else, on purpose:**
    - `cancel_request.by == "owner"` — the owner asked for this (a picker tap, a message), so a normal
      confirming push and wake are the right read, not a suppression. This is what keeps
      `cancel_wake_suppressed` from ever hiding a cancel the owner is actually waiting to hear back on.
    - a delegated/build session (rung 2) — something the assistant *dispatched*, not the assistant
      *speaking* (`cancel-attribution-spec.md` §3). The owner may not have been watching it at all, so
      a cancel from one is news, not a repeat.
    - no session attribution at all (rung 3) — the shim genuinely cannot tell who cancelled this, so
      it falls back to `wake_text`'s own appended dedupe clause instead of guessing here.

    Reused by `_cancelled_head` (the wording) and `notify_text`/presence's wake enqueue (whether the
    wake fires) so the two can never drift apart about which cancels this covers."""
    if not isinstance(rec, dict) or rec.get("status") != CANCELLED:
        return False
    by = cancelled_by_of(rec)
    if not by.get("session_id") or by.get("source") not in ASSISTANT_SURFACES:
        return False
    return cancel_request_of(rec).get("by") != "owner"


def _cancelled_head(rec: dict, title: str, dur: str) -> str:
    """The wording for a cancel's push head line (cancel-attribution-spec §5 and §12).

    **Rung 3 is the default, not the exception**, and it is byte-identical to the string this module
    shipped with. Unknown attribution degrades to today's wording, never to a guess: the push must
    never say "that was me" about something it cannot show was. `cancelled_by` absent, `{}`, not a
    dict, or carrying no `session_id` all land here — treated identically, which is today's behaviour
    exactly.

    Everything after the head line (`retry_note`, the log tail, the wake line) is untouched, EXCEPT
    when `cancel_wake_suppressed` — see `notify_text`."""
    by = cancelled_by_of(rec)
    why = rec.get("cancel_reason")
    why = why if isinstance(why, str) else None      # never a dict, never a list
    clause = _cancel_clause(why)

    if cancel_wake_suppressed(rec):
        # The shorter rung (§12): the assistant decided this itself and already told the owner in
        # the same turn, so the push is an acknowledgement of something already said, not a second
        # telling — hence no duration, no "That was me" (that phrasing is reserved for a cancel the
        # owner actually asked for, where a fuller confirmation is still the right read).
        reason = why or "no reason recorded"
        if reason[-1] not in ".!?…":
            reason += "."
        return f'🛑 "{title}" — cancelled by {_assistant_name()} — {reason}'

    if not by.get("session_id"):                                        # rung 3 — no attribution
        if clause:
            return f'🛑 "{title}" cancelled after {dur} — {clause[2:]}'
        return f'🛑 "{title}" cancelled after {dur}.'

    source = by.get("source")
    if source in ASSISTANT_SURFACES:                                   # rung 1 — the owner asked for it
        return f'🛑 "{title}" — stopped, {dur} in. That was me{clause or "."}'

    # rung 2 — attributed, but not a surface the owner is present at. The copy leans on the checkout and
    # branch rather than the source word deliberately: outside the daemon `source` is best-effort and
    # reads `build` for nearly everything, while the checkout is both true and useful.
    cwd, branch = by.get("cwd"), by.get("branch")
    if cwd and branch:
        # Normalise separators before taking the basename: a Windows cwd arrives backslashed, and
        # os.path.basename on POSIX (where CI runs) would return the whole string unchanged.
        base = os.path.basename(str(cwd).replace("\\", "/").rstrip("/")) or str(cwd)
        where = f"{base} @ {branch}"
        return f'🛑 "{title}" cancelled after {dur} — from {where}{clause or "."}'
    return f'🛑 "{title}" cancelled after {dur} — from another session ({source or "unknown"}){clause or "."}'


def notify_text(rec: dict) -> str:
    """The completion push, in the assistant's voice — pre-written, no LLM (this fires from the daemon, which
    has no brain). Carries the log's last meaningful line where there is one, so the ping states the
    outcome rather than merely announcing that there is one.

    Exactly one of these is sent per job, at the real outcome — a job mid-retry is `retry-pending`,
    which isn't terminal and so never reaches here."""
    title = rec.get("title") or rec.get("id")
    status = rec.get("status")
    multi = attempt_count(rec) > 1
    # A retried job's duration is the WHOLE job's, not the last attempt's — "failed after 4s" would
    # be a strange thing to read about something that spent six minutes trying.
    dur = _human_duration((_elapsed_since_created if multi else _elapsed_sec)(rec, _ended_dt(rec)))
    tail = log_tail_line(rec.get("log_path") or "")
    rescue_info = completion_of(rec).get("rescue")
    rescue_info = rescue_info if isinstance(rescue_info, dict) else None
    if rescue_info is not None:
        # §3.13's RESCUE ran — this job never actually landed its work, whatever its exit code says.
        # Overrides every other head-line branch below: a job in this state is FAILED by construction
        # (reconcile sets it), but "failed (exit 0)" read alone is a contradiction, not an answer.
        code = rec.get("exit_code")
        code_s = f"exit {code}" if code is not None else "no exit code"
        words = ", ".join(job_completion.REASON_TEXT.get(r, r)
                          for r in completion_of(rec).get("reasons") or []) or "unfinished work"
        if rescue_info.get("pushed"):
            head = (f'⚠️ "{title}" ({code_s}) never actually finished — {words}. I rescued what it had '
                    f'onto `{rescue_info["branch"]}`; nothing was lost, but it needs a human to land it.')
        else:
            head = (f'❌ "{title}" ({code_s}) never actually finished — {words} — and I could NOT '
                    f'rescue it: {rescue_info.get("skipped_reason") or "the rescue push failed"}.')
    elif status == DONE:
        head = f'✅ Finished — "{title}" ({dur}).'
    elif status == FAILED:
        code = rec.get("exit_code")
        code_s = f"exit {code}" if code is not None else (rec.get("error") or "no exit code")
        head = f'❌ "{title}" failed after {dur} ({code_s}).'
    elif status == TIMED_OUT:
        head = f'⏳ "{title}" hit its {_human_duration(rec.get("deadline_sec") or 0)} limit, so I stopped it.'
    elif status == ENDED_UNKNOWN and contradiction_of(rec):
        # Same state, different reason — and the shipped wording would be simply WRONG here, since
        # the exit code was perfectly readable and said 0. Naming which of the two is unreadable is
        # the honest version of "I can't tell", not a softer one.
        head = (f'⚠️ "{title}" exited 0 after {dur}, but its own log declares a failure — so I '
                f"genuinely can't tell you whether it finished.")
    elif status == ENDED_UNKNOWN and never_started(rec):
        # The OTHER half `check_terminal` can mean by `ended-unknown`: no PID was ever recorded, so
        # nothing ran at all — not a process that ran and then vanished. Naming that distinction is
        # the whole point: identical wording for both would make a benign "this never got going" read
        # exactly like the alarming "something WAS running and is now unaccounted for."
        head = (f'⚠️ "{title}" never reported starting, {dur} in — it looks like it never actually '
                f"ran, not that it finished and vanished.")
    elif status == ENDED_UNKNOWN:
        head = (f'⚠️ "{title}" ended after {dur}, but not cleanly enough for me to read an exit code '
                f"— so I genuinely can't tell you whether it finished.")
    elif status == CANCELLED:
        head = _cancelled_head(rec, title, dur)
    else:
        head = f'"{title}" — {status}.'
    parts = [head]
    # §12: an assistant-surface self-cancel already got explained in the turn that made it, so
    # its push stays the one short line `_cancelled_head` just built — no restated notes, no tail, and
    # (below) no "Reading it properly now.", because there IS no wake to read anything for. The
    # worktree leak note is the one exception: it is actionable ops information about THIS record, not
    # a restatement of why the job was cancelled, and dropping it would silently hide a leak.
    suppressed = status == CANCELLED and cancel_wake_suppressed(rec)
    if not suppressed:
        said = contradiction_note(rec)
        if said:
            parts.append(said)
        note = retry_note(rec)
        if note:
            parts.append(note)
        resumed = completion_note(rec)
        if resumed:
            parts.append(resumed)
        mismatch = resume_permission_mismatch(rec)
        if mismatch:
            parts.append(mismatch)
    leak = worktree_note(rec)
    if leak:
        parts.append(leak)
    drafted = pr_draft_note(rec)
    if drafted:
        parts.append(drafted)
    if not suppressed:
        if tail:
            parts.append(f"Last line: {tail}")
        if rec.get("wake"):
            parts.append("Reading it properly now.")
        elif rec.get("log_path"):
            parts.append(f"Log: {rec['log_path']}")
    return "\n".join(parts)


def default_wake(origin: dict | None) -> bool:
    """Should a job with this origin wake the warm session by default, absent an explicit
    `--wake`/`--no-wake`? Yes exactly when `origin.request.by == "owner"` — a job the owner asked for.

    **Why a default, when `--wake` already existed.** "The assistant will remember to pass --wake" is
    a prose countermeasure, and a forgotten flag means the completion push lands but the session is
    never handed the job to read — so nothing is said about it until the owner asks. Making the wake
    depend on provenance already captured on the record (`origin.request.by`), rather than on a flag
    someone has to remember, is the mechanism instead of the promise.

    **Deliberately narrow.** A Dream step, a scheduled sweep, or any other daemon-internal job has no
    owner request behind it (`request.by` is `"assistant"` or `"unresolved"`) and keeps the default of
    no wake — this must not make the daemon wake itself for its own routine cadence, only for work
    the owner actually asked for. `origin` is whatever `build_origin`/`normalize_origin` produced; `{}` or
    anything not carrying a `request` block reads as no-wake, same as an absent origin always has."""
    if not isinstance(origin, dict):
        return False
    request = origin.get("request")
    if not isinstance(request, dict):
        return False
    return request.get("by") == "owner"


def wake_text(rec: dict, artifact: str | None = None) -> str:
    """The synthetic inbound a `--wake` job enqueues. Deliberately a bracketed system line, matching
    the `[attachment: …]` convention in `modes/chat.md` — it is NOT the owner speaking, and the
    grounding prompt frames inbound as the owner's words, so it must not pretend to be. It describes;
    the assistant decides.

    `artifact` is rung 2 of the delivery ladder: when a fresh-eyes analysis has been filed for this
    job, the same mechanism carries its path instead of a second one being invented. The wording is
    different on purpose — a lead list is a set of **places to look**, not an answer, and a woken
    session that reads it as a diagnosis has been given exactly the thing the analyst was kept
    ignorant to avoid producing.

    **It also carries the job's DURATION and a bounded TAIL of its log.** Waking the session is the
    mechanism that replaces "the assistant will remember to check the job" — and a wake with nothing
    behind it just moves that same failure one hop later (the session must go and look it up). For an
    `ended-unknown`/`failed` job the tail is where the job's own final
    words live, which is the whole point of reading it back rather than only the status word. Capped
    at `_WAKE_TAIL_LINES` lines / `_WAKE_TAIL_CHARS` chars so a runaway log can't blow up the inbound;
    the full story still lives at `log_path`.

    **A cancelled job with no session attribution at all carries one more sentence** (§12,
    `cancel_wake_suppressed`'s own docstring): the shim genuinely cannot tell whether THIS cancel is
    one the woken session already explained to the owner, so rather than guess either way it hands
    the question to the one place that can actually answer it — the woken session's own transcript. A
    wake that DOES carry attribution never gets this clause: an attributed assistant-surface
    cancel is suppressed outright (never reaches here) and everything else (rung 2, or `cancel_request
    .by == "owner"`) is a case where the wake is already known to be warranted."""
    code = rec.get("exit_code")
    if code is None:
        # Same distinction `notify_text` makes: a job that never reported a PID never
        # ran, which is a different story from one that ran and vanished without a stamp — the
        # woken session reads this line, so it must not read the two alike.
        code_s = ", never started" if never_started(rec) else ", no exit code"
    else:
        code_s = ", exit {0}".format(code)
        if contradiction_of(rec):
            # A bare ", exit 0" on an `ended-unknown` record is the one place this feature could put a
            # cheerful lie back in: the woken session reads the code, not the status word. So the code
            # never travels unqualified once the log has contradicted it.
            code_s += " — which its own log contradicts"
    n = attempt_count(rec)
    tries = "" if n <= 1 else f", after {n} attempts"
    dur = _human_duration((_elapsed_since_created if n > 1 else _elapsed_sec)(rec, _ended_dt(rec)))
    tail_lines = log_tail_lines(rec.get("log_path") or "", count=_WAKE_TAIL_LINES,
                               limit=_WAKE_TAIL_SCAN_BYTES)
    tail_text = "\n".join(tail_lines)
    if len(tail_text) > _WAKE_TAIL_CHARS:
        tail_text = tail_text[-_WAKE_TAIL_CHARS:]
    tail_block = (f"\nLog tail (last {len(tail_lines)} line{'s' if len(tail_lines) != 1 else ''}, "
                 f"capped at {_WAKE_TAIL_CHARS} chars):\n{tail_text}") if tail_text else ""
    if artifact:
        return ('[fresh-eyes analysis filed for "{title}" — the job ended {status}{code}, {dur}. '
                "Leads: {art} · Log: {log}] "
                "This is a background job you started, not a message from the owner. The leads are "
                "ranked PLACES TO LOOK with the evidence for each — deliberately not a diagnosis, "
                "and the analyst was given only the log path and your one-line goal, nothing of your "
                "reasoning. An empty list is a real answer. Read them, check what they point at, "
                "then tell the owner plainly what you found."
                ).format(title=rec.get("title"), status=rec.get("status"), code=code_s, dur=dur,
                         art=artifact, log=rec.get("log_path")) + tail_block
    base = ('[job finished: "{title}" — status {status}{code}{tries}, {dur}. Log: {log}] '
           "This is a background job you started, not a message from the owner. Read the log, then "
           "tell the owner plainly what happened — including if it failed or the outcome is "
           "unreadable."
           ).format(title=rec.get("title"), status=rec.get("status"), code=code_s, tries=tries,
                    dur=dur, log=rec.get("log_path"))
    if rec.get("status") == CANCELLED and not cancelled_by_of(rec).get("session_id"):
        base += (" If you cancelled this job yourself and already told the owner why, say NOTHING "
                "(no message).")
    return base + tail_block


# --------------------------------------------------------------------------- the delivery ladder

def route_analysis(state_dir: str, rec: dict, now: datetime | None = None) -> dict | None:
    """Deliver a finished analysis back toward the session that asked for the work (spec §3.3).

    Called for an ANALYSIS job as it reaches a terminal state — `rec` is the analysis job, the
    address is on its target. Returns what happened (`{rung, session_id, artifact, wake_text}`) or
    None when `rec` isn't an analysis at all.

    **The ladder is a ladder of ADDITIONS, and the invariant it must not break is the parent spec's
    NO SILENT PATH.** By the time this runs, the completion push has already fired — twice, in fact:
    once for the work and once for the analysis job itself. So:

      1. **File it.** Already done, unconditionally, by `job_analysis.stage_analysis` before this
         is reached. Disk is the durable surface; delivery is the optional part.
      2. **Daemon-shaped origin (or a `--wake` job)** → the existing synthetic inbound, carrying the
         artifact path. Already-built mechanism; this is the path, not a new one.
      3. **Any other session** → append one line to `state/session-mail/<session_id>.jsonl`, because
         there is no channel to push at (see SESSION_MAIL_DIRNAME). It is a pull, and a pull needs a
         reader: a session that closes before its next orientation never sees its mail. That is
         acceptable *because* rung 4 already happened.
      4. **No address, or nothing worked** → nothing extra, which is today's behaviour in full.

    Routing may only ever ADD a delivery. It must never make the push conditional on a successful
    route and must never move it — "don't buzz the owner when the session got it" would be a separate
    decision with a separate argument, and doing it by accident is how the guarantee dies."""
    target_id = rec.get("analysis_for") if isinstance(rec, dict) else None
    if not target_id:
        return None
    now = now or _utc_now()
    target = load_job(state_dir, target_id)
    artifact = analysis_path(state_dir, target_id)
    staged = os.path.exists(artifact)
    if target is None:
        # The job was pruned or removed under us. Nothing to address; the artifact (if any) is still
        # on disk, and both pushes already landed.
        return {"rung": 4, "session_id": None, "artifact": artifact if staged else None,
                "reason": "target-gone"}
    origin = target.get("origin") if isinstance(target.get("origin"), dict) else {}
    session_id = origin.get("session_id") or None
    result = {"rung": 4, "session_id": session_id,
              "artifact": artifact if staged else None, "wake_text": None}

    # `--wake` counts as daemon-shaped regardless of what the registry called the origin: a job that
    # asked to be read back in voice is by definition addressed at the warm session. It also covers
    # the real case the spec's `source == "daemon"` test misses — a job started from a warm turn is
    # stamped `build` by the machine-wide hook, since the warm session is itself a Claude Code
    # session and the hook does not exempt it.
    if origin.get("source") == "daemon" or target.get("wake"):
        result.update({"rung": 2,
                       "wake_text": wake_text(target, artifact=artifact if staged else None)})
        return result
    if session_id:
        entry = {
            "at": _stamp(now),
            "kind": "job-analysis",
            "job_id": target_id,
            "title": target.get("title"),
            "status": target.get("status"),
            "goal": origin_goal(target),
            "artifact": artifact if staged else None,
            "analysis_job": rec.get("id"),
            "analysis_status": rec.get("status"),
            "text": (f'Fresh-eyes leads for "{target.get("title")}" ({target.get("status")}): '
                     f"{artifact} — ranked places to look, not a diagnosis."
                     if staged else
                     f'The fresh-eyes analysis of "{target.get("title")}" did not produce a valid '
                     f"artifact ({rec.get('status')}). Log: {rec.get('log_path')}"),
        }
        if append_session_mail(state_dir, session_id, entry):
            result["rung"] = 3
        else:
            result["reason"] = "mailbox-unwritable"
    return result


# --------------------------------------------------------------------------- the reconcile pass

def release_worktree(state_dir: str, rec: dict, *, wt_runner=None, log=lambda *_: None,
                     now: datetime | None = None) -> dict | None:
    """Give a finished job's worktree back — or leave it and record that we did. Returns
    `job_worktree.teardown`'s result, or None when there is nothing to release.

    **Called from `reconcile`, once, at the terminal transition and BEFORE the push**, so the push can
    name a leak. That placement is what makes the four paths nobody plans for need no new machinery:

      * the job **fails** or **times out** → terminal → here, normally with unpushed work, so
        normally a loud leak;
      * it is **cancelled** → `cancel_job` stamps terminal and the next tick lands here (a cancel 22
        seconds in leaves a clean tree and reclaims it);
      * the **daemon restarts mid-run** → nothing was held in memory; the path is in the record and
        the successor daemon reads it off disk, exactly as it already does for the completion push.

    `torn_down_at` makes it idempotent: reconcile re-reads un-notified terminal records on every tick
    (a Telegram outage can mean many), and a second `git worktree remove` against a reclaimed path
    would report a fresh "leak" for a directory that is simply gone.

    **It can never cost the push.** Every failure is swallowed into a recorded leak, and the caller
    runs it in its own guard as well: the completion push is the guarantee this whole module exists
    for, and cleanup may not become a way for a job to go silent."""
    wt = rec.get("worktree") if isinstance(rec, dict) else None
    if not isinstance(wt, dict) or not wt.get("path") or wt.get("torn_down_at"):
        return None
    now = now or _utc_now()
    try:
        result = job_worktree.teardown(rec, runner=wt_runner, now=now)
    except Exception as e:  # noqa: BLE001 — belt and braces; teardown already swallows its own
        result = {"path": wt.get("path"), "removed": False, "leaked": True,
                  "reason": job_worktree.one_line(f"teardown raised: {e}")}
    if not result:
        return None
    wt["torn_down_at"] = _stamp(now)
    wt["removed"] = bool(result.get("removed"))
    wt["leaked"] = bool(result.get("leaked"))
    if result.get("leaked"):
        wt["leak_reason"] = result.get("reason") or ""
        # Top-level as well as inside the block: this is the field a `jq` over `state/jobs/` looks
        # for, and "how many worktrees are we sitting on?" must be one query, not an archaeology
        # exercise (§5.6.4).
        rec["worktree_leaked"] = True
        log(f"! job {rec.get('id')} worktree LEFT ON DISK: {wt.get('path')} — {wt['leak_reason']}")
    else:
        rec.pop("worktree_leaked", None)
        log(f"• job {rec.get('id')} worktree released ({wt.get('path')})")
    save_job(state_dir, rec)
    return result


def resolve_job_pr_draft(state_dir: str, rec: dict, *, gh_runner=None,
                         log=lambda *_: None) -> list:
    """Un-draft (or leave draft) any PR `job_pr_draft.py` drafted on this job's behalf, at the
    terminal transition — called from `reconcile`, once, alongside `release_worktree` and in its own
    guard for the identical reason: a `gh` hiccup here must never be why a job goes silent.

    Almost every job never opened a PR at all, or finished before anything else noticed its branch, so
    the common case is `job_pr_draft.entries_for_job` returning `[]` and this function costing one
    cheap store read and nothing else — no `gh` call, no save.

    **Success is `done` with exit code 0.** A job that fails, times out, is cancelled, or ends unknown
    leaves its PR a draft, because the work it was doing when it opened that PR never actually
    finished — the rule this exists to enforce: a PR opened by a still-running job is a draft until
    that job finishes. Never raises."""
    job_id = rec.get("id") if isinstance(rec, dict) else None
    if not isinstance(job_id, str) or not job_id.strip():
        return []
    if not job_pr_draft.entries_for_job(state_dir, job_id):
        return []
    success = rec.get("status") == DONE and rec.get("exit_code") == 0
    try:
        results = job_pr_draft.resolve_for_job(state_dir, job_id, success=success, runner=gh_runner)
    except Exception as e:  # noqa: BLE001 — belt and braces; resolve_for_job already swallows its own
        log(f"! job {job_id} PR-draft resolution failed: {e}")
        return []
    for r in results:
        action = r.get("action")
        if action == "readied":
            log(f"• job {job_id} marked PR #{r.get('pr')} ready")
        elif action == "left-draft":
            log(f"• job {job_id} left PR #{r.get('pr')} a draft ({rec.get('status')})")
        else:
            log(f"! job {job_id} could not mark PR #{r.get('pr')} ready: {r.get('detail')}")
    return results


def delivery_of(res) -> str:
    """Read a `notify` callback's return value as a delivery phase.

    A bare bool is where a send's phase goes to die: a failed send is either pre-delivery (provably
    nothing was sent, so a retry is safe) or AMBIGUOUS (the request went out and Telegram may already
    have acted on it, so a retry is the duplicate). Collapsing both to `False` reads as "nothing was
    delivered, send it again next tick" — a blind retry any phase-aware send layer below would refuse.

    So a callback may return a mapping (`{"ok": bool, "ambiguous": bool}`) as well as a bool. **A bool
    still means what it always meant**, and a plain `False` from a caller with no phase information
    retries — the conservative direction only because such a caller never had the answer."""
    if isinstance(res, dict):
        if res.get("ok"):
            return job_push_ledger.LANDED
        return job_push_ledger.AMBIGUOUS if res.get("ambiguous") else job_push_ledger.FAILED
    return job_push_ledger.LANDED if res else job_push_ledger.FAILED


def telegram_notify(channel: str, text: str, *, telegram_env: str | None = None,
                    sender=None) -> dict:
    """The standalone completion-push sender: `reconcile`'s `notify` callback for
    `jobs.py reconcile --send`, delivering through `sentinel.send_telegram` (which shells out to
    `telegram_send.py`). Returns `{"ok": bool, "ambiguous": bool, ...}` for `delivery_of`. Never raises.

    **Why it exists.** `reconcile` only ever sends through an injected callback, and the daemon's own
    callback (`presence.deliver_reply`, via the scheduler tick) lands with the presence port in a later
    wave. Until then this is the path that makes the push real: schedule `jobs.py reconcile --send`.

    **Channels.** Every job's `notify.channel` is honoured only for `telegram`. `discord` and `cockpit`
    have no standalone sender here, so they FALL BACK TO TELEGRAM rather than report not-landed: a
    not-landed result retries every tick forever, and the module's invariant is that every job's
    ending reaches the owner — a push on the default channel beats a push that never leaves. The
    result carries `channel_fallback` so the substitution is visible to a caller that logs it.

    **The phase, honestly.** `ambiguous` is copied from the send result and is True only when the
    send layer itself says so; `telegram_send.py` does not classify a failure's phase today, so in
    practice every failure reads as pre-delivery and is retried next tick. The residual this leaves is
    named, not hidden: `sentinel.send_telegram`'s 60 s subprocess timeout can fire after the request
    already reached Telegram, and that case is reported as a plain failure — a possible duplicate
    push, never a lost one. `job_push_ledger` still guarantees one push per landed send.

    `telegram_env` defaults to `sentinel.DEFAULT_TELEGRAM_ENV` (`seneschal/scripts/telegram.env`, the
    convention every sender here uses). `sender(text, env_path) -> dict` is the test seam."""
    env_path = telegram_env
    if sender is None or env_path is None:
        try:
            import sentinel  # deferred, like `_registry_entry`: a sentinel problem costs the send only

            sender = sender or sentinel.send_telegram
            env_path = env_path or sentinel.DEFAULT_TELEGRAM_ENV
        except Exception as e:  # noqa: BLE001 — provably nothing was sent, so a retry is safe
            return {"ok": False, "ambiguous": False, "error": f"sentinel unavailable: {e}"}
    out: dict = {}
    if channel and channel != "telegram":
        out["channel_fallback"] = f"{channel} -> telegram"
    try:
        res = sender(text, env_path)
    except Exception as e:  # noqa: BLE001 — send_telegram never raises; a custom sender might
        return {**out, "ok": False, "ambiguous": False, "error": f"send raised: {e}"}
    if not isinstance(res, dict):
        return {**out, "ok": bool(res), "ambiguous": False}
    ok = bool(res.get("ok"))
    out.update({"ok": ok, "ambiguous": (not ok) and bool(res.get("ambiguous"))})
    if not ok and res.get("error"):
        out["error"] = str(res.get("error"))
    return out


def reconcile(state_dir: str, *, notify=None, wake=None, log=lambda *_: None,
              now: datetime | None = None, runner=subprocess.Popen,
              python_bin: str | None = None, wt_runner=None,
              completion_runner=None, completion_gh_runner=None,
              pr_draft_gh_runner=None) -> list:
    """One pass: move ended jobs to terminal, launch any retry that has come due, then notify every
    terminal job that hasn't been notified yet. Designed to run on the daemon's ~5 s scheduler tick
    (**seam:** that wiring — `presence.scheduler_task` → `_reconcile_jobs`, passing `deliver_reply`
    and the wake enqueue — lands with the presence port and is not in this tree yet); until then it
    runs as a one-shot, `jobs.py reconcile --send`, which passes `telegram_notify`.

    `notify(channel, text)` MUST report whether the send actually landed (`telegram_notify` does).
    `notified_at` is stamped only on a landed send, so a Telegram
    outage retries next tick rather than eating the ping — fail-CLOSED, deliberately, unlike
    everything else in this module. It may return a bool (the original contract, unchanged) or a
    mapping carrying the send's *phase* — see `delivery_of`, and why the boolean lost the one bit
    that decides whether a retry is safe.

    **The once-only gate is `job_push_ledger`, not `notified_at`.** The record has other writers and
    one of them erased the stamp; the ledger has exactly one writer, which is the block below. A
    refusal is LOGGED — `DUPLICATE completion push PREVENTED` — because a dedupe nobody can audit is
    how a silent-drop defect ships.

    Notifying is driven off the RECORD, not off this pass's own detection, which is what makes a
    daemon restart mid-job harmless: the successor reads the same un-notified terminal record from
    disk and sends. **A pending retry is durable for exactly the same reason** — it is a
    `retry-pending` record with a `next_attempt_at`, so the shim never sleeps and the successor
    daemon launches the attempt the dead one was waiting on. Returns the records notified this pass.

    `runner`/`python_bin` are the retry spawn's injectable seam (tests pass a stub); production takes
    the default `subprocess.Popen`.

    `completion_runner`/`completion_gh_runner` are §3.13's git/`gh` seam for `job_completion.evaluate`
    and `.rescue` — tests pass a fake; production leaves both `None`, which is `job_completion` opening
    its own real `job_worktree.Runner()` per call.

    `pr_draft_gh_runner` is `job_pr_draft.py`'s own seam (a tuple-returning `(argv) -> (code, out,
    err)` callable — NOT `completion_gh_runner`'s object shape, because `job_pr_draft.py` shares no
    runner convention with
    `job_completion.py`, deliberately: see its own module docstring). Production leaves it `None`,
    which shells out to a real `gh` directly."""
    now = now or _utc_now()
    notified: list = []
    for rec in list_jobs(state_dir):
        try:
            changes = check_terminal(rec, now)
            if changes:
                # `rec` is `list_jobs()`'s snapshot from the TOP of this tick — and the record's own
                # shim can finish for real (a genuine exit code, a populated `attempts[]`, or a
                # `retry-pending` backoff) in the gap between that snapshot and this record's turn in
                # the loop. That gap is not theoretical: `job_completion.evaluate` below runs git/`gh`
                # calls for every OTHER already-terminal record earlier in the same pass, and a job
                # that finished in that window would otherwise have its real completion clobbered by
                # a stale ENDED_UNKNOWN with `exit_code: None` and `attempts: []` computed from the
                # pre-completion snapshot. Re-read immediately before
                # acting, and let whichever save is actually current win — the same rule
                # `honour_cancel` already enforces for a cancel racing the shim (§3.10), applied here
                # to the ordinary finish instead. `_run_shim`'s own re-read at its finish (`latest =
                # load_job(...) or rec; if latest.get("status") in TERMINAL: return ...`) is this
                # race's mirror in the other direction; this closes the missing half.
                fresh = load_job(state_dir, rec["id"]) or rec
                if fresh.get("status") != RUNNING:
                    rec = fresh
                else:
                    if changes["status"] == TIMED_OUT:
                        kill_pid(fresh.get("child_pid"))
                        kill_pid(fresh.get("pid"))
                    rec = fresh
                    rec.update(changes)
                    save_job(state_dir, rec)
                    log(f"• job {rec['id']} → {rec['status']} ({rec.get('title')})")
            if rec.get("status") == RETRY_PENDING:
                step = check_retry_due(rec, now)
                if step is None:
                    continue  # still waiting out the backoff
                if step == "due":
                    rec = _spawn_attempt(state_dir, rec, runner=runner, python_bin=python_bin,
                                         now=now)
                    log(f"• job {rec['id']} → retry attempt {rec.get('attempt')} "
                        f"({rec.get('title')})")
                    if rec.get("status") not in TERMINAL:
                        continue  # relaunched; a failed respawn falls through and notifies
                else:
                    # The window closed while we were waiting. This is a real failure and must be
                    # reported like one — a job must never end its life sitting in `retry-pending`.
                    rec["status"] = FAILED
                    rec["ended_at"] = _stamp(now)
                    rec["next_attempt_at"] = None
                    rec["retry_outcome"] = "window-expired"
                    save_job(state_dir, rec)
                    log(f"• job {rec['id']} → failed (retry window closed) ({rec.get('title')})")
            # §3.13 — the job-completion guarantee. Runs BEFORE worktree teardown, deliberately: a
            # resume needs the same working directory (branch checked out, staged changes intact) the
            # original attempt left behind, and tearing it down first would either lose that state or
            # (for a clean-but-unpushed branch) hand the resumed session nothing to pick up from.
            if rec.get("status") in COMPLETION_CHECK_STATUSES:
                outcome = job_completion.evaluate(rec, repo_root=REPO_ROOT, now=now,
                                                  runner=completion_runner,
                                                  gh_runner=completion_gh_runner)
                rec["completion"] = outcome["completion"]
                if outcome["action"] == "resume":
                    attempts = list(rec["completion"].get("resume_attempts") or [])
                    attempts.append({"at": _stamp(now), "attempt": len(attempts) + 1,
                                     "argv": outcome["argv"]})
                    rec["completion"]["resume_attempts"] = attempts
                    rec.setdefault("original_argv", list(rec.get("argv") or []))
                    rec["argv"] = outcome["argv"]
                    save_job(state_dir, rec)
                    rec = _spawn_attempt(state_dir, rec, runner=runner, python_bin=python_bin, now=now)
                    log(f"• job {rec['id']} incomplete ({', '.join(outcome['completion']['reasons'])}) "
                        f"— resume attempt {len(attempts)}/{job_completion.MAX_RESUME_ATTEMPTS} spawned")
                    if rec.get("status") not in TERMINAL:
                        continue  # relaunched; a failed respawn falls through and notifies
                elif outcome["action"] == "rescue":
                    rescue_result = job_completion.rescue(rec, outcome.get("detected"), now=now,
                                                          runner=completion_runner)
                    rec["completion"]["rescue"] = rescue_result
                    rec["status"] = FAILED
                    rec["ended_at"] = _stamp(now)
                    rec["error"] = ("incomplete work (" + ", ".join(outcome["completion"]["reasons"])
                                    + (f") — rescued to {rescue_result['branch']}"
                                       if rescue_result.get("pushed")
                                       else f") — rescue failed: {rescue_result.get('skipped_reason')}"))
                    save_job(state_dir, rec)
                    log(f"! job {rec['id']} incomplete — " +
                        (f"rescued to {rescue_result['branch']}" if rescue_result.get("pushed")
                         else f"RESCUE FAILED: {rescue_result.get('skipped_reason')}"))
                else:
                    save_job(state_dir, rec)
            if rec.get("status") not in TERMINAL or rec.get("notified_at"):
                continue
            # Before the push, so a leak can be named in it — and in its own guard, so a cleanup
            # problem can never be why a job goes silent (§5.6, and phase 3's regression test).
            try:
                release_worktree(state_dir, rec, wt_runner=wt_runner, log=log, now=now)
            except Exception as e:  # noqa: BLE001
                log(f"! job {rec['id']} worktree teardown failed: {e}")
            # Same reasoning, same placement: a PR this job's branch was drafted for gets resolved
            # before the push, in its own guard, so a `gh` hiccup here can never be why a job goes
            # silent (job_pr_draft.py: a job's PR stays a draft until the job ends cleanly).
            try:
                pr_draft_result = resolve_job_pr_draft(state_dir, rec, gh_runner=pr_draft_gh_runner,
                                                       log=log)
                if pr_draft_result:
                    rec["pr_draft"] = pr_draft_result
                    save_job(state_dir, rec)
            except Exception as e:  # noqa: BLE001
                log(f"! job {rec['id']} PR-draft resolution failed: {e}")
            if notify is None:
                continue
            # THE DEDUPE, and it is by identity: this job, this ending. `notified_at` alone could
            # never carry this because the job record has more than one writer and a cancel's
            # post-kill re-assert could erase the stamp between two ticks (job_push_ledger's
            # docstring has the mechanism). The ledger has exactly one writer: these lines.
            claimed = job_push_ledger.claim(state_dir, rec["id"], rec["status"], now=now)
            if not claimed["granted"]:
                log(f"! job {rec['id']} DUPLICATE completion push PREVENTED "
                    f"({claimed['reason']}"
                    + (f", already {claimed['delivery']} at {claimed['delivered_at']}"
                       if claimed.get("delivery") else "")
                    + f"; prevented {claimed['prevented']}x)")
                if claimed.get("delivery") in job_push_ledger.DELIVERED:
                    # Repair the record the clobber damaged, from the ORIGINAL instant rather than
                    # now — so `ended_at` → `notified_at` stays the honest lag it was measured on.
                    rec["notified_at"] = claimed["delivered_at"] or _stamp(now)
                    rec["notify_delivery"] = claimed["delivery"]
                    save_job(state_dir, rec)
                continue  # `in-flight-elsewhere` simply waits: the next tick asks again
            delivery = delivery_of(notify(rec.get("notify", {}).get("channel") or "telegram",
                                          notify_text(rec)))
            job_push_ledger.resolve(state_dir, rec["id"], delivery, now=now)
            if delivery == job_push_ledger.FAILED:
                log(f"! job {rec['id']} completion push did NOT land — retrying next tick")
                continue  # leave notified_at unset: the retry IS the guarantee
            if delivery == job_push_ledger.AMBIGUOUS:
                # The request went out and Telegram may already have delivered it. Treated as
                # notified, and SAID SO on the record (`notify_delivery`), because the alternative —
                # sending again next tick — is the guaranteed duplicate. Same policy as the Telegram
                # send layers' own refuse-to-retry-an-ambiguous-send rule; making it again here is the
                # point, not the redundancy.
                log(f"! job {rec['id']} completion push was AMBIGUOUS — the request went out and "
                    f"may have been delivered; NOT re-sending, because a blind retry is the duplicate")
            rec["notified_at"] = _stamp(now)
            rec["notify_delivery"] = delivery
            save_job(state_dir, rec)
            notified.append(rec)
            if rec.get("wake") and wake is not None:
                try:
                    wake(wake_text(rec))
                except Exception as e:  # noqa: BLE001 — the push already landed; the wake is a bonus
                    log(f"! job {rec['id']} wake enqueue failed: {e}")
            if rec.get("analyze") and rec.get("status") in ANALYSABLE and not rec.get("analysis_job"):
                # AFTER the push, in its own guard: an analysis is an addition, and a refusal or a
                # spawn failure must cost the analysis, never the notification that already landed.
                try:
                    sub = request_analysis(state_dir, rec["id"], runner=runner,
                                           python_bin=python_bin, now=now)
                    log(f"• job {rec['id']} → analysis {sub['id']}")
                except Exception as e:  # noqa: BLE001
                    log(f"! job {rec['id']} analysis not started: {e}")
            if rec.get("analysis_for"):
                # The delivery ladder. Strictly an ADDITION, in its own guard, AFTER the push — a
                # route that fails costs the route. `notified_at` is already stamped and saved by
                # this point, so nothing here can make a job notify twice or not at all.
                try:
                    route = route_analysis(state_dir, rec, now=now)
                    if route and route.get("wake_text") and wake is not None:
                        wake(route["wake_text"])
                    if route:
                        log(f"• job {rec['id']} analysis routed → rung {route['rung']}"
                            f"{' (' + route['session_id'] + ')' if route.get('session_id') else ''}")
                except Exception as e:  # noqa: BLE001
                    log(f"! job {rec['id']} analysis routing failed: {e}")
        except Exception as e:  # noqa: BLE001 — one bad record must never stall the others (or the tick)
            log(f"! job reconcile error on {rec.get('id')}: {e}")
    return notified


# --------------------------------------------------------------------------- the session lease

def lease_active(state_dir: str, now: datetime | None = None) -> bool:
    """Is any active `--lease` job still within `LEASE_MAX_SEC`? The daemon's idle wind-down is meant
    to consult this before winding the warm session down, so work that genuinely cannot detach can
    hold the session open until it's done. (**Seam:** that consultation lands with the presence port;
    until then `--lease` is recorded but nothing reads it.)

    Bounded on purpose: past the cap this returns False and the session winds down normally (the job
    keeps running and still notifies — a lease governs the SESSION, never the job). A pending
    control still overrides it entirely, on the drainer's side: a job must never block a deploy.
    Fail-open (any error → False): a bookkeeping problem must not pin the session open forever.

    Measured from `created_at`, not `started_at`: the shim re-stamps `started_at` on every retry, so a
    per-attempt clock would let a retrying job renew its 30-minute lease indefinitely. The cap is a
    cap on the JOB. (Identical for every non-retry job, where the two stamps are the same instant.)"""
    now = now or _utc_now()
    try:
        for rec in list_jobs(state_dir, active_only=True):
            if rec.get("lease") and _elapsed_since_created(rec, now) < LEASE_MAX_SEC:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


# --------------------------------------------------------------------------- cancel & prune

def cancel_job(state_dir: str, job_id: str, now: datetime | None = None,
               why=None, cancelled_by_session: str | None = None,
               env=None, requested_by: str | None = None,
               reason_class: str | None = None, request_turn: str | None = None) -> dict | None:
    """Claim the job as `cancelled` on disk, THEN kill the shim and its child (a terminated shim gets
    no cleanup handler on Windows, so its child would otherwise orphan). A cancel still notifies, like
    every other terminal state. Already-terminal jobs are returned untouched.

    It also records WHO cancelled and WHY (`../docs/cancel-attribution-spec.md`), so the
    completion push can read as an acknowledgement rather than an alert. **The push itself is
    unchanged: it still fires, still exactly once, still only stamped after the send lands.** The fix
    is tone, not suppression — anyone finding an opt-out here has found a bug, not a feature.

    **THE CLAIM GOES TO DISK BEFORE THE KILL, AND THAT ORDER IS THE FIX** (spec §3.10). Kill-first,
    save-last is what makes a cancel *raceable at all*: the kill is precisely the event that wakes the
    shim out of `proc.wait()`, so killing first hands the shim a head start on the same file, and the
    record says whatever the loser of that race said — a cancelled job recorded (and pushed) as
    **`failed`, exit 1** with its `cancelled_by` erased. Claiming first inverts it: the child
    cannot die before the claim lands, so the shim's existing "cancel wins" re-read finds it every
    time. `honour_cancel` covers the residue (a job that ended on its own in the same instant, a
    `reconcile` backstop mid-flight); neither mechanism alone is sufficient, and the belt is the
    cheaper one to reason about.

    **Attribution stays inside its own try, and the kill still runs whatever happens.** Losing
    attribution is cosmetic; failing to cancel is real. So is the claim's own write: it is guarded
    too, and re-asserted after the kill, because an unwritable record must not cost the kill either.

    `cancelled_at` and `ended_at` are stamped from the same instant and mean different things:
    `ended_at` is "the job stopped", which every terminal state has, and `cancelled_at` is "somebody
    asked for this", which only a cancel has. Without `cancelled_at`, a reader could not tell a
    deliberate stop from a spontaneous one without parsing `cancelled_by`, which rung 3 legitimately
    omits.

    An already-terminal job returns untouched, so a `--why` on a finished job writes nothing and
    pushes nothing — there is no un-notified terminal transition left to describe. That is correct;
    it is spelled out here so nobody "fixes" it later.

    **A third block: `cancel_request`.** `cancelled_by` says WHO pressed the button;
    `cancel_request` says WHOSE IDEA IT WAS — built with `build_request`, the identical mechanism
    `start` uses for `origin.request` (§3.5), just pointed at the cancelling session instead of the
    job's. This is what lets the completion push and the wake tell an assistant-decided cancel apart
    from one the owner asked for (a picker tap, a message): the former already got explained in the
    turn that made it, the latter has not been confirmed to the owner yet and a normal push/wake is
    the right read. See `cancel_wake_suppressed` and `../docs/cancel-attribution-spec.md`
    §12. Built from `requested_by`/`reason_class`/`request_turn`, falling back to the daemon's own
    open-turn pointer exactly as `start` does when none are asserted — and, like `cancelled_by`, it
    sits in its own `try`: losing it is cosmetic, failing to cancel is not."""
    rec = load_job(state_dir, job_id)
    if rec is None or rec.get("status") in TERMINAL:
        return rec
    stamp = _stamp(now or _utc_now())
    rec["status"] = CANCELLED
    rec["ended_at"] = stamp
    rec["cancelled_at"] = stamp
    try:
        by = build_cancelled_by(state_dir, session_id=cancelled_by_session, env=env)
        if by:
            rec["cancelled_by"] = by
        reason = normalise_why(why)
        if reason:
            rec["cancel_reason"] = reason
    except Exception:  # noqa: BLE001 — attribution is never worth the cancel
        pass
    try:
        env_ = os.environ if env is None else env
        sid = (cancelled_by_session or "").strip() or None
        if sid is None:
            try:
                sid = (env_.get(ORIGIN_ENV_VAR) or "").strip() or None
            except Exception:  # noqa: BLE001 — an exotic mapping must not cost the cancel
                sid = None
        request = build_request(state_dir, session_id=sid, turn_id=request_turn,
                                requested_by=requested_by, reason_class=reason_class, now=now)
        if request:
            rec["cancel_request"] = request
    except Exception:  # noqa: BLE001 — attribution is never worth the cancel
        pass
    try:
        save_job(state_dir, rec)   # the claim: on disk before anything can wake the shim
    except Exception as e:  # noqa: BLE001 — nor is the claim's write worth the kill
        failures.record(state_dir, "jobs.cancel_job", "cancel_claim_save_failed",
                        detail=f"job_id={job_id} error={e}")
    kill_pid(rec.get("child_pid"))
    kill_pid(rec.get("pid"))
    return reassert_cancel(state_dir, rec)   # merge, never clobber — see the function


def reassert_cancel(state_dir: str, rec: dict) -> dict:
    """Re-assert a cancel's own fields onto whatever is on disk NOW. `cancel_job`'s last act.

    **A plain `save_job(state_dir, rec)` here sends the owner the same push twice.** The two
    `kill_pid` calls above are `taskkill /PID … /T /F` with a 30 s ceiling each and they can take tens
    of seconds. The ~5 s reconcile tick runs in that window, sees a terminal un-notified record,
    pushes, stamps `notified_at`, and saves. Then the taskkills return here, and writing back the
    **in-memory record from before the kill** — which has no `notified_at` and no
    `worktree.torn_down_at` — makes the next tick read an un-notified terminal job and push again.
    Cancels are the only path with a writer this slow, so they are where duplicates concentrate.

    **It cannot simply be deleted, and that is why it is a merge.** The line exists so that a
    claim whose write failed (§3.10's `save_job` is inside its own `try`) still lands — an unwritable
    record must not cost the kill *or* the cancel. So: re-read, apply only `CANCEL_OWNED_FIELDS` on
    top, write that. A record that is missing entirely from disk falls back to writing `rec` whole,
    which is exactly the failed-claim case.

    `honour_cancel` does not and must not cover this. Its docstring is explicit that it restores only
    the cancel-owned fields and writes everything else *as given*, deliberately, so `notified_at` and
    the worktree teardown still land on a cancelled record — and it early-returns entirely when the
    incoming record already says `cancelled`, which this one does. **The guard's stated exception is
    the hole.** The fix belongs at the writer, not in the shared chokepoint."""
    fresh = load_job(state_dir, rec.get("id") or "")
    if fresh is None:
        save_job(state_dir, rec)   # nothing on disk to merge with: this write IS the claim
        return rec
    for key in CANCEL_OWNED_FIELDS:
        if key in rec:
            fresh[key] = rec[key]
        else:
            fresh.pop(key, None)
    save_job(state_dir, fresh)
    return fresh


def prune(state_dir: str, days: int = RETENTION_DAYS, now: datetime | None = None) -> int:
    """Drop terminal, ALREADY-NOTIFIED jobs (and their logs) older than `days`. Dream calls this
    nightly, matching `telegram_poll.py --prune-days` / `presence_import.py --prune-days`. A terminal
    job that never got its ping is deliberately never pruned — the ping outlives the GC."""
    now = now or _utc_now()
    removed = 0
    for rec in list_jobs(state_dir):
        if rec.get("status") not in TERMINAL or not rec.get("notified_at"):
            continue
        try:
            age_days = (now - parse_iso(rec["notified_at"])).total_seconds() / 86400.0
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if age_days < days:
            continue
        for path in (job_path(state_dir, rec["id"]), rec.get("log_path") or "",
                     analysis_path(state_dir, rec["id"])):
            try:
                if path:
                    os.remove(path)
            except OSError:
                pass
        # The push ledger keeps exactly the retention of the record it describes: once the job is
        # gone nothing can re-read it as un-notified, so the "already pushed" entry has no work left.
        job_push_ledger.forget(state_dir, rec["id"])
        removed += 1
    return removed


# --------------------------------------------------------------------------- CLI

def _duration_arg(text: str) -> int:
    try:
        return parse_duration(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _print_job(rec: dict, log_lines: int = 0, attempts: bool = False,
               state_dir: str | None = None) -> None:
    at = _ended_dt(rec) if rec.get("ended_at") else _utc_now()
    dur = _human_duration((_elapsed_since_created if attempt_count(rec) > 1
                           else _elapsed_sec)(rec, at))
    bits = [rec["id"], rec.get("status", "?"), dur, rec.get("title", "")]
    if rec.get("exit_code") is not None:
        bits.insert(2, f"exit {rec['exit_code']}")
    if rec.get("retry"):
        bits.insert(2, f"try {rec.get('attempt') or 1}/"
                       f"{int((rec.get('retry') or {}).get('max_retries') or 0) + 1}")
    print("  ".join(str(b) for b in bits))
    if rec.get("next_attempt_at"):
        print(f"    next attempt due {rec['next_attempt_at']}")
    wt = rec.get("worktree") if isinstance(rec.get("worktree"), dict) else None
    if wt and rec.get("worktree_leaked"):
        # Printed by `list` as well as `status`, deliberately: a leak that only shows when you
        # already know which job to ask about is not visible, and a pile-up has to be.
        print(f"    worktree LEFT ON DISK: {wt.get('path')} — "
              f"{wt.get('leak_reason') or 'removal refused'}")
    elif wt and attempts:
        state = ("released" if wt.get("removed") else
                 "setup failed" if wt.get("error") else "live")
        print(f"    worktree ({state}): {wt.get('path') or wt.get('error')}")
    if request_is_violation(rec):
        # Printed by `list` as well as `status`, for the leaked-worktree reason one clause up: the
        # thing worth seeing is "when did the assistant start a job nobody asked for", and an answer
        # you only get by already knowing which job to ask about is not an answer.
        print(f"    SELF-STARTED OFF AN IDEA THE OWNER WAS STILL THINKING ABOUT "
              f"({REQUEST_REASON_VIOLATION}) — spec §3.5.7 class 4")
    mismatch = resume_permission_mismatch(rec)
    if mismatch:
        # Printed by `list` as well as `status`, same reasoning as the two blocks above: this is a
        # regression signal (§3.13's RESUME dropping a permission mode it should have carried
        # forward), and it must be visible without already knowing which job to ask about.
        print(f"    {mismatch}")
    origin = rec.get("origin") if isinstance(rec.get("origin"), dict) else {}
    if attempts and origin:
        where = " · ".join(str(origin[k]) for k in ("source", "branch", "cwd") if origin.get(k))
        print(f"    origin: {origin.get('session_id') or '(no session)'}"
              f"{' (' + where + ')' if where else ''} [{origin.get('stamped_by') or '?'}]")
        if origin.get("goal"):
            print(f"    goal: {origin['goal']}")
        req = job_request(rec)
        if req:
            # The rung is spelled out in words, not printed as a bare number: `rung 2` means nothing
            # to someone reading `status` for the first time, and this line exists to answer exactly
            # one question in one glance — who asked for this?
            rung = request_rung(rec)
            bits = [f"{REQUEST_RUNG_WORDS[rung]} (rung {rung})"]
            for label, key in (("turn", "turn_id"), ("message", "message_id"),
                               ("via", "surface")):
                if req.get(key):
                    bits.append(f"{label} {req[key]}")
            reason = request_reason(rec)
            if reason:
                bits.append(REQUEST_REASON_WORDS.get(reason, reason))
            print(f"    asked by: {' · '.join(bits)} [{req.get('resolved_by') or '?'}]")
            if req.get("why"):
                print(f"      why not: {req['why']}")
    if attempts and rec.get("analysis_for"):
        print(f"    analysis of: {rec['analysis_for']}")
    if attempts and rec.get("analysis_job"):
        print(f"    analysis job: {rec['analysis_job']}")
    if attempts and state_dir:
        # Rung 1 of the delivery ladder: disk is the durable surface, so `status` always names the
        # artifact even when every delivery route failed.
        art = analysis_path(state_dir, rec["id"])
        if os.path.exists(art):
            print(f"    analysis: {art}")
    for a in (rec.get("attempts") or []) if attempts else []:
        if isinstance(a, dict):
            print(f"    attempt {a.get('attempt')}: {a.get('outcome')} "
                  f"exit {a.get('exit_code')} in {a.get('duration_sec')}s — "
                  f"{a.get('classification')}"
                  f"{' (' + a['signature'] + ')' if a.get('signature') else ''}"
                  f" → {a.get('decision')}")
    if log_lines:
        try:
            with open(rec.get("log_path") or "", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh.read().splitlines()[-log_lines:]:
                    print(f"    {line}")
        except OSError:
            pass


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(description="Durable background jobs for the assistant")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser(
        "start", help="spawn a detached job the daemon will report on",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Spawn a detached job. The daemon — not the session that started it — pushes the "
                    "outcome when it ends, whatever the outcome is.",
        epilog=(
            "RETRY (opt-in, off by default)\n"
            "  --retry N asserts THIS COMMAND IS SAFE TO RUN AGAIN FROM SCRATCH. That is your\n"
            "  assertion, not something jobs.py can verify: a retry re-runs the whole argv, so if\n"
            "  attempt 1 got far enough to create a branch, open a PR or send a mail before dying,\n"
            "  attempt 2 may do it a second time. Only use it for idempotent or trivially re-doable\n"
            "  work.\n"
            "\n"
            "  Only RECOGNISED TRANSIENT failures are retried — API 5xx / overloaded / 429, and\n"
            "  connection-reset / DNS / TLS / transport-timeout signatures (jobs.py's\n"
            "  TRANSIENT_SIGNATURES). Everything else — a test failure, a syntax error, any non-zero\n"
            "  exit whose output isn't recognised — is TERMINAL the first time, no retry. So is a\n"
            "  timed-out, cancelled or unreadable job.\n"
            "\n"
            "  --deadline-sec bounds EACH ATTEMPT; --retry-window bounds the whole job.\n"
            "  You still get exactly ONE push, at the real outcome, and it says how many attempts\n"
            "  it took.\n"
            "\n"
            "  THE CLASSIFICATION IS RECORDED EITHER WAY. Every terminal failure writes its\n"
            "  transient/terminal verdict (plus the signature that matched and whether retry was\n"
            "  on) into the job record's attempts[] — with or without --retry. Recording is not\n"
            "  retrying: a job without --retry still fails terminally, first time. `jobs.py status\n"
            "  <id>` prints it, and it is how you find out which jobs SHOULD have asked for\n"
            "  --retry rather than only measuring the ones that already did.\n"
            "\n"
            "  python jobs.py start --title research --retry 3 --retry-backoff --retry-window 2h \\\n"
            "      -- claude -p \"...\"\n"))
    s.add_argument("--title", required=True, help="what to call this in the completion push")
    s.add_argument("--notify", default="telegram", choices=["telegram", "discord", "cockpit"],
                   help="the completion push's channel (the standalone `reconcile --send` sender "
                        "delivers discord/cockpit via telegram)")
    s.add_argument("--wake", action="store_true",
                   help="also wake the warm session to read the result (the push fires either way). "
                        "Defaults ON when this job resolves to origin.request.by == \"owner\" — pass "
                        "this explicitly only to force it on for an assistant/unresolved-origin job.")
    s.add_argument("--no-wake", action="store_true",
                   help="force the wake OFF even though the owner asked for this job (the noisy/"
                        "automated case). Refused together with --wake.")
    s.add_argument("--lease", action="store_true",
                   help="hold the warm session open until this finishes (bounded; a restart still wins)")
    s.add_argument("--deadline-sec", type=int, default=DEFAULT_DEADLINE_SEC,
                   help="per-ATTEMPT time limit (default 6 h)")
    s.add_argument("--retry", type=int, default=0, metavar="N",
                   help=f"up to N EXTRA attempts (max {RETRY_MAX_RETRIES_CAP}) on a recognised "
                        "transient failure only — API 5xx/overloaded/429, connection reset, DNS, "
                        "TLS, transport timeout. Anything else is terminal first time. "
                        "IMPLIES YOU ASSERT THE COMMAND IS SAFE TO RE-RUN FROM SCRATCH "
                        "(see the notes below). Default 0 = never retry.")
    s.add_argument("--retry-backoff", action="store_true",
                   help=f"exponential backoff with jitter ({RETRY_BASE_SEC}s doubling, capped at "
                        f"{RETRY_CAP_SEC // 60} min) instead of a constant {RETRY_BASE_SEC}s wait")
    s.add_argument("--retry-window", type=_duration_arg, default=None, metavar="DURATION",
                   help="stop retrying this long after the job STARTED, whatever N says "
                        "(90s / 45m / 2h / 1h30m) — so an overnight job isn't still flailing at noon")
    s.add_argument("--origin-session", default=None, metavar="ID",
                   help=f"the session this job is for. Defaults to ${ORIGIN_ENV_VAR} (auto), which "
                        "is right for every caller that IS a session; pass this for the ones that "
                        "aren't (a cron shim, a hand-run command) or to address another session.")
    s.add_argument("--goal", default=None,
                   help="ONE LINE: what this work is trying to achieve. Stored on the job "
                        f"(capped at {ORIGIN_GOAL_MAX} chars, newlines collapsed) and it is half "
                        "the input contract for any later analysis of a failure.")
    s.add_argument("--request-turn", default=None, metavar="TURN_ID",
                   help="the turn this job was asked for in. Defaults to the daemon's open-turn "
                        "pointer, which is right for every job started from inside a warm-session "
                        "turn; pass this only when you KNOW the turn and the pointer cannot say so. "
                        "It is recorded as ASSERTED, not verified.")
    s.add_argument("--requested-by", default=None, choices=["owner", "assistant"],
                   help="who asked for this job — `owner` if an owner message did, `assistant` if "
                        "the assistant decided it itself. Defaults to the open turn's own answer. "
                        "Only assert this when the turn pointer cannot; a wrong attribution is worse "
                        "than the honest `unresolved` you get for free. PASS `assistant` WHENEVER YOU "
                        "DECIDED THIS YOURSELF, including inside a turn the owner opened — the "
                        "pointer records who wrote the line, and an owner's line is not consent to "
                        "the job you chose to start while reading it.")
    s.add_argument("--reason-class", default=None, choices=list(REQUEST_REASON_APPROVED)
                   + [REQUEST_REASON_VIOLATION],
                   help="WHY you started this yourself, when nobody asked you to (spec §3.5.7). "
                        "Recorded only on an `assistant` request; a rung-3 job with none reads "
                        f"`{REQUEST_REASON_UNSTATED}`. The first three are WANTED self-starts — do "
                        f"them and stamp them, do not stop to ask. `{REQUEST_REASON_VIOLATION}` is "
                        "a VIOLATION and exists so that it can be seen: the owner was still thinking "
                        "out loud (stacked alternatives, hedges, a terminology tangent) and it became "
                        "a brief. If that is the class you are reaching for, the job is the wrong "
                        "move — ask the owner which alternative they meant instead.")
    s.add_argument("--analyze", action="store_true",
                   help="if this job ends badly, hand its log to a fresh pair of eyes (a second, "
                        "origin-less job) and file the ranked leads next to it. Opt-in per job — "
                        "it is a model call. REQUIRES --goal. Fires on "
                        f"{' / '.join(ANALYSABLE)} only: a job you CANCELLED is not a failure to "
                        "investigate, and the analyst has no way to tell the difference.")
    s.add_argument("--worktree", action="store_true",
                   help="run this job in a PRIVATE, disposable checkout cut from origin/develop at "
                        f"{job_worktree.WORKTREE_DIRNAME}/<job-id> (override the parent with "
                        f"${job_worktree.WORKTREE_ROOT_ENV}). Use it for ANY delegated work that "
                        "will touch git: the unit of collision is the checkout, not the branch — "
                        "jobs on different branches that share one tree can silently commit into "
                        "each other's branch. Removed when the "
                        "job ends, never with --force: if git refuses because the tree is dirty or "
                        "holds unpushed commits, it is LEFT and the push says so.")
    s.add_argument("--cwd", default=None,
                   help="where to run it. Defaults to THIS CHECKOUT — which for the daemon is the "
                        "live one it runs and auto-updates from, never a tree to branch in. Prefer "
                        "--worktree for "
                        "anything that touches git; --cwd is for work that must run in a specific "
                        "existing tree (and the two are refused together).")
    s.add_argument("--agent", action="store_true",
                   help="this job makes agent/model calls (claude, an API, Ollama) — it COUNTS "
                        "toward the concurrency caps. Stamped on the record; beats the argv "
                        "heuristic. Use it when the command hides a model call behind a wrapper.")
    s.add_argument("--no-agent", action="store_true",
                   help="this job makes NO agent/model calls (a transcription, a download, a build, "
                        "a test run) — it does not count toward either cap and is not held by them. "
                        "Stamped on the record; beats the argv heuristic. "
                        "Without either flag, an unrecognised command shape COUNTS (fail closed).")
    s.add_argument("--over-soft-cap", action="store_true",
                   help=f"start this even though this session already has "
                        f"{SOFT_CAP_PER_ORIGIN} jobs in flight. THE SOFT CAP IS THE OWNER'S "
                        f"INTERACTIVE HEADROOM, not machine load, so this is a judgement call that "
                        f"can be right — but make it deliberately: the overrun is "
                        f"stamped on the record. It does NOT touch the hard cap of "
                        f"{HARD_CAP_GLOBAL} across all sessions, which has no flag.")
    s.add_argument("--loop", default=None, metavar="ID",
                   help="the register work-item this job is doing (`loops.py show <id>`). Calls "
                        "`loops.py start` for it — one explicit signal, never inferred from the job's "
                        "own existence or from --goal naming the id in passing. A refusal (unknown "
                        "id, already "
                        "terminal) is printed but never fails the job start — the job already ran.")
    s.add_argument("--no-preflight", action="store_true",
                   help="skip the spawn preflight — the check that any script path the argv names "
                        "actually exists IN THE NAMESPACE THE COMMAND WILL RUN IN (on Windows, a "
                        "bare `bash` is often WSL's bash, which cannot see C:/...). IT IS FOR THE "
                        "CASE WHERE THE PREFLIGHT IS WRONG (an unusual argv it misreads), NEVER for "
                        "the case where the path is wrong: a job whose script isn't there dies in "
                        "seconds and spends a completion push on it either way — typically because "
                        "the launching shell mangled the path, or it names a real file in the wrong "
                        "namespace (see jobs.py).")
    s.add_argument("cmd_argv", nargs=argparse.REMAINDER,
                   help="the command to run, after a bare --")

    ls = sub.add_parser("list", help="list jobs")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--active", action="store_true")

    st = sub.add_parser("status", help="one job")
    st.add_argument("job_id")
    st.add_argument("--json", action="store_true")
    st.add_argument("--log-lines", type=int, default=0)

    c = sub.add_parser("cancel", help="kill a running job (still notifies)")
    c.add_argument("job_id")
    c.add_argument("--why", default=None,
                   help="one line on WHY, quoted into the completion push so a deliberate stop reads "
                        "as an acknowledgement rather than an alert. Absent degrades to exactly the "
                        "old flat wording (docs/cancel-attribution-spec.md)")
    c.add_argument("--cancelled-by-session", default=None,
                   help="override the cancelling session id; defaults to $CLAUDE_CODE_SESSION_ID")
    c.add_argument("--requested-by", default=None, choices=["owner", "assistant"],
                   help="whose idea THIS CANCEL was — `owner` if the owner asked for it (a picker "
                        "tap, a message) or `assistant` if you decided to stop it yourself. Defaults "
                        "to the open turn's own answer, the identical resolver `start` uses. Get this "
                        "right: `owner` keeps the full push and wake (the owner asked, a "
                        "confirmation is right); `assistant`/unresolved from an assistant surface "
                        "shortens the push to one line and skips the wake — the owner does not need "
                        "to be told twice why you stopped something you decided to stop yourself.")
    c.add_argument("--reason-class", default=None, choices=list(REQUEST_REASON_APPROVED)
                   + [REQUEST_REASON_VIOLATION],
                   help="WHY you cancelled this yourself, when it was your call — same vocabulary as "
                        "`start`'s flag of the same name. Recorded only when this resolves to "
                        "`--requested-by assistant`.")
    c.add_argument("--request-turn", default=None, metavar="TURN_ID",
                   help="the turn this cancel happened in. Defaults to the daemon's open-turn "
                        "pointer, same resolver `start` uses; pass this only when you KNOW the turn "
                        "and the pointer cannot say so.")

    an = sub.add_parser("analyze", help="hand a failed job's log to a fresh pair of eyes")
    an.add_argument("job_id")
    an.add_argument("--goal", default=None,
                    help="ONE LINE: what the work was trying to achieve. Required unless the job "
                         "was started with --goal — an analysis without it is an analyst guessing "
                         "what 'working' would have looked like.")
    an.add_argument("--analyst", default="warm",
                    help="who analyses. `warm` (a fresh `claude -p` one-shot) is the only analyst.")

    rc = sub.add_parser("reconcile",
                        help="one reconcile pass: terminal transitions, due retries, completion pushes")
    rc.add_argument("--send", action="store_true",
                    help="really deliver the completion pushes via Telegram (telegram_notify). "
                         "Without it the pushes are printed and marked delivered — a dry pass for "
                         "a human reading the terminal. Schedule `reconcile --send` until the "
                         "daemon's own tick runs reconcile.")
    rc.add_argument("--telegram-env", default=None, metavar="PATH",
                    help="Telegram env file for --send (default: seneschal/scripts/telegram.env)")

    pr = sub.add_parser("prune", help="GC notified terminal jobs (and swept session mail)")
    pr.add_argument("--days", type=int, default=RETENTION_DAYS)
    pr.add_argument("--mail-days", type=int, default=RETENTION_DAYS,
                    help=f"also sweep state/{SESSION_MAIL_DIRNAME}/ of entries older than this "
                         f"(default {RETENTION_DAYS}, matching --days)")

    m = sub.add_parser("mail", help="this session's UNREAD mail — and the read is recorded")
    m.add_argument("--session", default=None,
                   help=f"whose mailbox (default ${ORIGIN_ENV_VAR}, i.e. yours)")
    m.add_argument("--limit", type=int, default=20)
    m.add_argument("--json", action="store_true")
    m.add_argument("--all", action="store_true", dest="show_all",
                   help="show read mail too (still records the read, unless it would mark unread "
                        "mail --limit withheld)")
    m.add_argument("--peek", action="store_true",
                   help="show without recording the read — the ONLY non-advancing mode")

    r = sub.add_parser("__run", help=argparse.SUPPRESS)  # internal: the detached shim
    r.add_argument("job_id")

    args = p.parse_args(argv)

    if args.cmd == "__run":
        return _run_shim(args.state_dir, args.job_id)

    if args.cmd == "start":
        cmd_argv = [a for a in args.cmd_argv if a != "--"] if args.cmd_argv else []
        if not cmd_argv:
            print("nothing to run — put the command after a bare --", file=sys.stderr)
            return 2
        if args.retry_window and not args.retry:
            print("note: --retry-window does nothing without --retry N", file=sys.stderr)
        if args.analyze and not one_line(args.goal):
            # Ruling 3, refused at the earliest and clearest point: the goal line is half the input
            # contract, so asking for analysis without one is asking for a guess.
            print("--analyze requires --goal: one line saying what this work is trying to achieve. "
                  "Without it the analyst has to guess what 'working' would have looked like.",
                  file=sys.stderr)
            return 2
        if args.worktree and args.cwd:
            # Refused rather than silently ranked: one says "make me a tree", the other says "use
            # this one", and guessing which the caller meant is how a job ends up somewhere nobody
            # intended — the exact class of failure --worktree exists to remove.
            print("--worktree and --cwd contradict each other: --worktree CREATES the working "
                  "directory. Pass one.", file=sys.stderr)
            return 2
        if args.wake and args.no_wake:
            print("--wake and --no-wake contradict each other. Pass one.", file=sys.stderr)
            return 2
        if not args.no_preflight:
            # Last of the start-time refusals, and deliberately the last thing before anything is
            # written: a refused job must leave no record, because it never started. Exit 2 like the
            # two refusals above it rather than a code of its own — this is the same kind of answer
            # ("you asked for something that cannot work"), and a fourth number would only make it
            # look like a different kind.
            why = preflight_refusal(cmd_argv)
            if why:
                print(f"refusing to start: {why}", file=sys.stderr)
                return 2
        origin = build_origin(args.state_dir, session_id=args.origin_session, goal=args.goal,
                              request_turn=args.request_turn, requested_by=args.requested_by,
                              reason_class=args.reason_class)
        # --no-wake wins outright; --wake is honoured as-is (nothing existing changes meaning);
        # otherwise default ON for a job the owner asked for (`default_wake`) and off for everything
        # else — an assistant-decided or unresolved-origin job keeps the no-wake default.
        wake = False if args.no_wake else (args.wake or default_wake(origin))
        # The concurrency caps, AFTER the preflight and BEFORE anything is written. Both orderings are
        # deliberate. After the preflight, because a mangled argv is wrong whatever the load is, and
        # telling the owner to cancel a live job to make room for one that would die in a second is worse
        # advice than no advice. Before the write, because these are `preflight_refusal`'s kind of
        # answer — you asked for something that cannot happen — and a refused job must leave no record:
        # it never started, so it must not read as failed. Exit 2, same as the four refusals above.
        #
        # The session id comes off the ORIGIN that is about to be stamped, not from a second read of
        # the environment, so the cap counts against exactly the identity the record will carry.
        session_id = origin.get("session_id")
        if args.agent and args.no_agent:
            print("--agent and --no-agent contradict each other. Pass one.", file=sys.stderr)
            return 2
        agent_flag = True if args.agent else (False if args.no_agent else None)
        # Only agent jobs count — so a job that is itself non-agent adds nothing to
        # either count and is not held by either cap. Classified exactly the way it will be counted
        # once it is on disk: the same function over the same fields.
        new_is_agent = counts_toward_cap({"argv": cmd_argv, "agent": agent_flag})
        blocked = hard_cap_refusal(args.state_dir) if new_is_agent else None
        if blocked is None and new_is_agent and not args.over_soft_cap:
            blocked = soft_cap_refusal(args.state_dir, session_id)
        if blocked:
            print(f"refusing to start: {blocked}", file=sys.stderr)
            return 2
        rec = start_job(args.state_dir, args.title, cmd_argv, cwd=args.cwd, channel=args.notify,
                        wake=wake, lease=args.lease, deadline_sec=args.deadline_sec,
                        origin=origin, analyze=args.analyze, worktree=args.worktree,
                        over_soft_cap=(over_soft_cap_note(args.state_dir, session_id)
                                       if args.over_soft_cap and new_is_agent else None),
                        agent=agent_flag,
                        retry=retry_config(args.retry, backoff=args.retry_backoff,
                                           window_sec=args.retry_window))
        loop_started = None
        if args.loop:
            # One explicit signal, never inferred. The job
            # has already started by this point — a refusal here is reported, never fatal to the job.
            try:
                loops.start(args.state_dir, item_id=args.loop)
                loop_started = args.loop
            except loops.LoopsError as exc:
                print(f"note: --loop {args.loop} did not start — {exc}", file=sys.stderr)
        print(json.dumps({"id": rec["id"], "status": rec["status"], "pid": rec.get("pid"),
                          "log_path": rec["log_path"], "retry": rec.get("retry"),
                          "origin": rec.get("origin"), "cwd": rec.get("cwd"),
                          "worktree": rec.get("worktree"),
                          "over_soft_cap": rec.get("over_soft_cap"),
                          "agent_class": job_agent_class(rec)[0],
                          "loop_started": loop_started}))
        return 0 if rec["status"] == RUNNING else 1

    if args.cmd == "list":
        recs = list_jobs(args.state_dir, active_only=args.active)
        if args.json:
            print(json.dumps(recs, indent=2))
        else:
            for rec in recs:
                _print_job(rec)
            if not recs:
                print("no jobs")
        return 0

    if args.cmd == "status":
        rec = load_job(args.state_dir, args.job_id)
        if rec is None:
            print(f"no such job: {args.job_id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(rec, indent=2))
        else:
            _print_job(rec, args.log_lines, attempts=True, state_dir=args.state_dir)
        return 0

    if args.cmd == "analyze":
        try:
            sub = request_analysis(args.state_dir, args.job_id, goal=args.goal,
                                   analyst=args.analyst)
        except ValueError as e:
            print(f"refusing: {e}", file=sys.stderr)
            return 1
        print(json.dumps({"analysis_job": sub["id"], "of": args.job_id,
                          "status": sub["status"], "log_path": sub["log_path"],
                          "artifact": analysis_path(args.state_dir, args.job_id)}))
        return 0 if sub["status"] == RUNNING else 1

    if args.cmd == "cancel":
        rec = cancel_job(args.state_dir, args.job_id, why=args.why,
                         cancelled_by_session=args.cancelled_by_session,
                         requested_by=args.requested_by, reason_class=args.reason_class,
                         request_turn=args.request_turn)
        if rec is None:
            print(f"no such job: {args.job_id}", file=sys.stderr)
            return 1
        print(f"{rec['id']} {rec['status']}")
        return 0

    if args.cmd == "reconcile":
        if args.send:
            # The standalone push path (see `telegram_notify`). No `wake`: a synthetic inbound needs
            # the daemon's queue, which is the presence port's seam — the push still fires, and a
            # `--wake` job's wake is simply not delivered on this path.
            def _notify(channel, text):
                res = telegram_notify(channel, text, telegram_env=args.telegram_env)
                if res.get("channel_fallback"):
                    print(f"  (channel {res['channel_fallback']})")
                return res
        else:
            def _notify(channel, text):
                print(f"[{channel}] {text}")
                return True
        sent = reconcile(args.state_dir, notify=_notify, log=print)
        print(f"notified {len(sent)} job(s)")
        return 0

    if args.cmd == "prune":
        swept = prune_session_mail(args.state_dir, args.mail_days)
        held = (f", held {swept['held_unread']} UNREAD past retention" if swept["held_unread"]
                else "")
        print(f"pruned {prune(args.state_dir, args.days)} job(s), "
              f"{swept['removed']} mail entr(ies){held}")
        return 0

    if args.cmd == "mail":
        session = args.session or (os.environ.get(ORIGIN_ENV_VAR) or "").strip()
        if not session:
            print(f"no session id — pass --session or set ${ORIGIN_ENV_VAR}", file=sys.stderr)
            return 2
        view = session_mail_view(args.state_dir, session, limit=args.limit,
                                 show_all=args.show_all, peek=args.peek)
        if args.json:
            print(json.dumps(view, indent=2))
            return 0
        if not view["total"]:
            # Kept verbatim: an empty mailbox is a different fact from an unread count of zero, and
            # this exact string + exit 0 is what the watcher spec's §1.2 inventory pins.
            print("no mail")
            return 0
        print(f"{view['unread']} new, {view['total']} total")
        for entry in view["entries"]:
            print(f"{entry.get('at')}  {mail_summary(entry)}")
        if not view["entries"]:
            print("(nothing new — --all to see what's already been read)")
        if view["held_back"]:
            print(f"({view['held_back']} unread entr(ies) not shown — raise --limit; "
                  f"the read was NOT recorded)" if not view["advanced"] else
                  f"({view['held_back']} unread entr(ies) not shown — raise --limit or run again)")
        if view["receipt"] is False:
            print("(the read receipt could not be written — the mail above still stands unread)")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
