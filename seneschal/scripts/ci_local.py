#!/usr/bin/env python3
"""ci_local.py — run EVERY gate CI runs, on this host, before anything is pushed. Stdlib only.

## Why this exists

CI is there to confirm that a machine OTHER than the development machine can complete the given
tasks — not to be the first place a gate runs. A change that ran py_compile, the unit tests and a few
of the `check_*.py` gates, pushed, and then went red on the one gate it had not run cost a round-trip
for nothing. Anything that runs a gate — especially a gate on prose — runs here first.

`check_docs.py` already aggregates the doc-side gates in-process; this is the layer above it — the
WHOLE workflow, every job, in `ci.yml`'s order, each step invoked exactly as CI invokes it (same
script, same flags: `--enforce` where CI enforces, bare where CI only reports). It never
re-implements a gate and never second-guesses a gate's own exit code.

## What a SKIP means — and why it is not green

CI runs on a machine that installs its own toolchains (`npm ci`, `uv sync --extra cockpit`, pwsh, the
Android SDK). This host may not have all of them, and a missing toolchain is not a failure of the code
— so a step whose toolchain is absent is SKIPPED WITH A NAMED REASON that says what to install. **A
skipped step has not been checked.** The summary counts skips separately and says so in words, so a
run with skips can never be read as a full run. Everything CI's python job runs needs only Python +
git, and NONE of it may skip (only `uv lock --check`, which needs uv, and the PowerShell parse, which
needs pwsh, can) — that job is where a prose gate goes red, and it is always runnable here.

## Order, and why the table is hardcoded

`STEPS` is a hardcoded mirror of `ci.yml`, for the same reason `check_docs.py`'s `GATES` is: this repo
has no YAML parser and would not want one for a list this size. The safety net is
`test_ci_local.py::WorkflowMirrorTest`, which scans `ci.yml` for every `seneschal/scripts/*.py`
invocation and its flags and fails if `STEPS` does not carry the same invocation verbatim — so a gate
added to CI without being added here cannot pass silently.

Steps that CI's own `fetch-depth: 0` makes possible (`count_tests.py --compare-to`, the budget
ratchet, `check_rulings.py`'s diff) need `origin/develop`. If it is missing this fetches it once; if
the fetch fails it says so and runs anyway — every one of those gates degrades to "no baseline" rather
than red, which is how CI itself behaves on a missing ref.

## The gates see the WORKING TREE, and the header says which tree was checked

Every diff-against-base gate diffs the base against the working tree — staged, unstaged and
untracked-not-ignored — through one shared helper, `gate_git.py`. A gate that diffed
`<merge-base>..HEAD` would see committed changes only: run before `git commit` (gates green, then
commit, then push — the natural order) it would pass vacuously and go red in CI. This script's header
prints one line naming the tree it checked: `working tree: N modified, M untracked (gates diff the
working tree, not HEAD)`. On CI the tree is clean and that line reads `0 modified, 0 untracked`.

## One temp root for the whole run, and a count of what escaped it

2026-09-29: ~1M leaked `tmp????????` test directories in the host's `%LOCALAPPDATA%\\Temp` stalled
Windows logon for 36-50 minutes. `main()` calls `tmproot.install()` before the first step, so this
process owns one run root and every child — each unittest root, `npm test` — inherits
`TMP`/`TEMP`/`TMPDIR` pointing into it; each root's own `test_0_tmproot.py` adopts it rather than
nesting another. It is removed at exit, and a line on stderr says how many entries the run's tests
left there. After the last step, `temp_note()` prints a `temp:` line counting leak-shaped entries
CREATED in the real temp dir during the run — report-only, because other checkouts on this host can
leak concurrently and the count cannot say whose; the blocking guard is the redirect assertion in
every root's `test_0_tmproot.py`. Why, in full: `tmproot.py`.

USAGE:
  python seneschal/scripts/ci_local.py                 # everything, ci.yml order; exit 1 on any FAIL
  python seneschal/scripts/ci_local.py --list          # the step table, with each step's command
  python seneschal/scripts/ci_local.py --only rulings  # one step (repeatable), for iterating on a fix
  python seneschal/scripts/ci_local.py -v              # show every step's output, not only failures

Runnable from any checkout or worktree: the repo root is derived from this file's location, never
from an absolute host path.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, "..", ".."))
CI_WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
BASE_REF = "origin/develop"
GIT = ["git", "-c", "core.fsmonitor=false"]

sys.path.insert(0, SCRIPT_DIR)

import gate_git  # noqa: E402  — the working-tree helper the gates share
import tmproot  # noqa: E402  — one temp root per run, removed at exit (2026-09-29 Temp flood)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


# --------------------------------------------------------------------------------------------- #
# Host probes — each answers "can this host run that CI job?" and returns a skip reason or None.
# They are plain functions on the step table so a test can swap them for fakes.
# --------------------------------------------------------------------------------------------- #

def _exists(*parts: str) -> bool:
    return os.path.exists(os.path.join(REPO_ROOT, *parts))


def _tool(name: str) -> str | None:
    """`shutil.which`, so `npm` resolves to `npm.cmd` on Windows and a list-form subprocess works."""
    return shutil.which(name)


def skip_if_no_uv() -> str | None:
    return None if _tool("uv") else "`uv` is not on PATH — https://docs.astral.sh/uv/ ; CI pip-installs it"


def skip_if_no_pwsh() -> str | None:
    return None if _tool("pwsh") else "`pwsh` (PowerShell 7) is not on PATH; CI runs it on ubuntu"


def skip_if_no_node_modules(subdir: str):
    def probe() -> str | None:
        if not _tool("npm"):
            return "`npm` is not on PATH (CI uses Node 22)"
        if not _exists(subdir, "node_modules"):
            return "%s/node_modules is absent — run `npm ci` in %s first" % (subdir, subdir)
        return None
    return probe


def cockpit_python() -> str | None:
    """The interpreter that can import the cockpit extras (`fastapi`), or None. The worktree's own
    `.venv` first — that is what `uv sync --extra cockpit --group test` creates and what CI's
    `uv run` would use — then the running interpreter. Never an absolute host path."""
    candidates = [os.path.join(REPO_ROOT, ".venv", "Scripts", "python.exe"),
                  os.path.join(REPO_ROOT, ".venv", "bin", "python"),
                  sys.executable]
    for exe in candidates:
        if not os.path.exists(exe):
            continue
        try:
            rc = subprocess.run([exe, "-c", "import fastapi, uvicorn"], cwd=REPO_ROOT,
                                capture_output=True, timeout=60).returncode
        except (OSError, subprocess.SubprocessError):
            continue
        if rc == 0:
            return exe
    return None


def skip_if_no_cockpit_extras() -> str | None:
    if cockpit_python():
        return None
    return ("cockpit extras (fastapi/uvicorn) not importable from .venv or this interpreter — "
            "run `uv sync --extra cockpit --group test` in this checkout first")


def android_paths_changed() -> bool:
    """`android.yml` is path-filtered: it only runs when `phone/android/**` (or itself) changed.
    Mirror that filter against the merge base with `origin/develop`; if that cannot be computed,
    say the paths changed so the build is attempted rather than silently skipped."""
    try:
        base = subprocess.run(GIT + ["merge-base", BASE_REF, "HEAD"], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=60)
        if base.returncode != 0:
            return True
        diff = subprocess.run(GIT + ["diff", "--name-only", base.stdout.strip(), "--",
                                     "phone/android", ".github/workflows/android.yml"],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        return bool(diff.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return True


def skip_if_no_android() -> str | None:
    if not android_paths_changed():
        return "no change under phone/android/** vs %s — android.yml would not run either" % BASE_REF
    if not _exists("phone", "android", "gradlew"):
        return "phone/android/gradlew is absent"
    if not (os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
            or _exists("phone", "android", "local.properties")):
        return "no Android SDK (ANDROID_HOME / ANDROID_SDK_ROOT / phone/android/local.properties)"
    if not (_tool("java") or os.environ.get("JAVA_HOME")):
        return "no JDK on PATH / JAVA_HOME (CI uses Temurin 17)"
    return None


# --------------------------------------------------------------------------------------------- #
# Command builders — each returns the list of argv lists a step runs, in order. A builder rather
# than a literal where the command depends on the host (which python, the tracked file list).
# --------------------------------------------------------------------------------------------- #

def py(*args: str) -> list:
    return [sys.executable, *args]


def script(name: str, *flags: str) -> list:
    """`python seneschal/scripts/<name>.py <flags>` — the exact shape ci.yml uses."""
    return py(os.path.join("seneschal", "scripts", name + ".py"), *flags)


def tracked_python_files() -> list:
    out = subprocess.run(GIT + ["ls-files", "*.py"], cwd=REPO_ROOT, capture_output=True,
                         text=True, encoding="utf-8", check=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def py_compile_commands() -> list:
    """`git ls-files '*.py' | xargs python -m py_compile`, chunked the way xargs would — Windows
    caps a command line at ~32 k characters."""
    files = tracked_python_files()
    commands, chunk, size = [], [], 0
    for f in files:
        if chunk and size + len(f) + 1 > 24000:
            commands.append(py("-m", "py_compile", *chunk))
            chunk, size = [], 0
        chunk.append(f)
        size += len(f) + 1
    if chunk:
        commands.append(py("-m", "py_compile", *chunk))
    return commands


def discover(root: str) -> list:
    return py("-m", "unittest", "discover", "-s", root, "-p", "test_*.py")


def cockpit_discover(root: str):
    def build() -> list:
        exe = cockpit_python() or sys.executable
        return [[exe, "-m", "unittest", "discover", "-s", root, "-p", "test_*.py"]]
    return build


def npm(*args: str) -> list:
    return [_tool("npm") or "npm", *args]


#: ci.yml's "PowerShell syntax check" step, verbatim in intent: AST-parse every tracked .ps1.
PS_SYNTAX_SCRIPT = (
    "$failed = $false; "
    "foreach ($f in (git ls-files '*.ps1')) { "
    "$errs = $null; "
    "[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $f), [ref]$null, "
    "[ref]$errs) | Out-Null; "
    "if ($errs) { $failed = $true; Write-Host \"PARSE ERRORS in ${f}:\"; "
    "$errs | ForEach-Object { Write-Host \"  $($_.Extent.StartLineNumber): $($_.Message)\" } } "
    "else { Write-Host \"OK $f\" } }; "
    "if ($failed) { exit 1 }")


def ps_syntax_commands() -> list:
    return [[_tool("pwsh") or "pwsh", "-NoProfile", "-NonInteractive", "-Command", PS_SYNTAX_SCRIPT]]


def gradle_commands() -> list:
    # Absolute: on Windows, Popen resolves a relative executable against the PARENT's cwd.
    wrapper = os.path.join(REPO_ROOT, "phone", "android",
                           "gradlew.bat" if os.name == "nt" else "gradlew")
    return [[wrapper, ":app:assembleDebug", "--no-daemon", "--console=plain"]]


# --------------------------------------------------------------------------------------------- #
# The step table — ci.yml, job by job, step by step, flags verbatim.
# --------------------------------------------------------------------------------------------- #

def step(name: str, job: str, commands, cwd: str = "", skip=None, note: str = "") -> dict:
    """`commands` is either a list of argv lists or a zero-arg callable returning one (resolved
    only when the step runs, so `--list` never probes the host). `skip` is a zero-arg callable
    returning a reason string, or None to run. `note` is shown in `--list`."""
    return {"name": name, "job": job, "commands": commands, "cwd": cwd, "skip": skip, "note": note}


STEPS = (
    # ---- python ----------------------------------------------------------------------------- #
    step("py-compile", "python", py_compile_commands,
         note="git ls-files '*.py' | xargs python -m py_compile"),
    step("count-tests", "python",
         [script("count_tests", "--check", "--compare-to", BASE_REF)],
         note="refuses a suite total typed into a router; deltas vs the base are report-only"),
    step("unittest-scripts", "python", [discover("seneschal/scripts")]),
    step("unittest-proteus", "python", [discover("archons/proteus/tools")],
         note="the shipped archon's tool tests, beside the tools"),
    step("uv-lock", "python", lambda: [[_tool("uv") or "uv", "lock", "--check"]],
         skip=skip_if_no_uv, note="uv lock --check"),
    step("placeholders", "python", [script("check_placeholders")],
         note="BLOCKING — no real workspace/account UUID in the tree"),
    step("context-budget", "python", [script("check_context_budget", "--enforce-headroom")],
         note="byte cap report-only; the headroom-shape half blocks"),
    step("context-pointers", "python", [script("check_context_pointers", "--enforce")],
         note="BLOCKING"),
    step("doc-status", "python", [script("check_doc_status", "--enforce")], note="BLOCKING"),
    step("state-writes", "python", [script("check_state_writes", "--enforce")], note="BLOCKING"),
    step("no-utcnow", "python", [script("check_no_utcnow")], note="report-only"),
    step("wall-clock", "python", [script("check_wall_clock", "--enforce")],
         note="BLOCKING — bare wall-clock reads in checkers/tests/CI"),
    step("context-stores", "python", [script("check_context_stores", "--venue", "ci")],
         note="report-only"),
    step("carryover-prose", "python", [script("check_carryover_prose")],
         note="report-only — the Wrap's prose must name carryover_region.py, never a raw "
              "memory_write.py write on carry-over.md"),
    step("rulings", "python", [script("check_rulings", "--enforce")],
         note="BLOCKING — new ruling language must touch seneschal/docs/rulings.md"),
    step("grounding-dates", "python", [script("check_grounding_dates")],
         note="report-only, no --enforce exists"),
    step("ps-syntax", "python", ps_syntax_commands, skip=skip_if_no_pwsh,
         note="AST-parse every tracked .ps1"),
    # ---- references ("Reference data check") ----------------------------------------------- #
    step("json-files", "references", [script("check_json_files", "--enforce")],
         note="every tracked .json parses"),
    # ---- screener / cockpit-web (Node 22) --------------------------------------------------- #
    step("screener-npm", "screener", lambda: [npm("run", "typecheck"), npm("test")], cwd="phone",
         skip=skip_if_no_node_modules("phone"), note="npm run typecheck && npm test"),
    # ---- cockpit-server -------------------------------------------------------------------- #
    step("cockpit-server", "cockpit-server", cockpit_discover("cockpit/server"),
         skip=skip_if_no_cockpit_extras, note="needs the cockpit extras"),
    step("cockpit-decoy", "cockpit-server", cockpit_discover("cockpit/decoy"),
         skip=skip_if_no_cockpit_extras, note="needs the cockpit extras"),
    # breakglass is stdlib-only (ci.yml says so): CI runs it in the extras venv for convenience,
    # but there is no reason to skip it here when the extras are absent — a free check is run.
    step("cockpit-breakglass", "cockpit-server", cockpit_discover("cockpit/breakglass"),
         note="stdlib-only; runs even without the extras"),
    step("cockpit-web-npm", "cockpit-web",
         lambda: [npm("run", "typecheck"), npm("run", "build"), npm("test")], cwd="cockpit/web",
         skip=skip_if_no_node_modules("cockpit/web"),
         note="npm run typecheck && npm run build && npm test"),
    # ---- android.yml (path-filtered) ------------------------------------------------------- #
    step("android-build", "android", gradle_commands, cwd=os.path.join("phone", "android"),
         skip=skip_if_no_android, note="only when phone/android/** changed, like android.yml"),
)

STEP_NAMES = tuple(s["name"] for s in STEPS)

#: The steps that need nothing but Python + git. These may never SKIP: they are the python job, the
#: one a prose gate goes red on, and this host can always run them.
ALWAYS_RUNNABLE = tuple(s["name"] for s in STEPS
                        if s["job"] in ("python", "references") and s["skip"] is None)


# --------------------------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------------------------- #

def _child_env() -> dict:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def subprocess_runner(argv: list, cwd: str) -> tuple:
    """The real runner: one argv list, never a shell string. Returns `(exit_code, output)`."""
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=_child_env())
    except OSError as exc:
        return 127, "could not start %r: %s\n" % (argv[0], exc)
    out = proc.stdout or ""
    if proc.stderr:
        out += ("\n[stderr]\n" if out else "[stderr]\n") + proc.stderr
    return proc.returncode, out


def ensure_base_ref(runner=subprocess_runner, root: str = REPO_ROOT) -> str:
    """Make `origin/develop` exist if it can. Returns a one-line note for the report; never raises
    and never fails the run — the gates that need the ref all degrade to "no baseline" on their
    own, exactly as CI would on a missing ref."""
    rc, _ = runner(GIT + ["rev-parse", "--verify", "--quiet", BASE_REF], root)
    if rc == 0:
        return "%s present" % BASE_REF
    rc, out = runner(GIT + ["fetch", "origin", "develop"], root)
    if rc == 0:
        return "%s fetched" % BASE_REF
    return ("%s MISSING and `git fetch origin develop` failed (exit %d) — count-tests, "
            "context-budget and rulings run without a baseline, as CI would on a missing ref:\n%s"
            % (BASE_REF, rc, out.strip()))


def working_tree_note(runner=subprocess_runner, root: str = REPO_ROOT) -> str:
    """One line naming the tree this run checked. The gates diff the working tree, not HEAD, so a
    reader comparing a local PASS with a CI FAIL can see whether the local run saw the uncommitted
    change — or, if this line says `0 modified, 0 untracked`, that it could not have been a
    working-tree question at all. Never raises; a git failure is reported, not guessed."""
    rc, out = runner(GIT + ["status", "--porcelain", "--untracked-files=all"], root)
    if rc != 0:
        return "working tree: unknown — `git status` exited %d" % rc
    modified, untracked = gate_git.parse_porcelain(out)
    return ("working tree: %d modified, %d untracked (gates diff the working tree, not HEAD)"
            % (modified, untracked))


def run_step(s: dict, runner=subprocess_runner, root: str = REPO_ROOT) -> dict:
    """Run one step: its skip probe first, then each command in order, stopping at the first
    non-zero exit. The verdict is the command's own exit code, never a reading of its output."""
    started = time.monotonic()
    result = {"name": s["name"], "job": s["job"], "status": PASS, "reason": "", "output": "",
              "exit_code": 0, "seconds": 0.0}
    reason = s["skip"]() if s["skip"] else None
    if reason:
        result.update(status=SKIP, reason=reason)
        result["seconds"] = time.monotonic() - started
        return result
    commands = s["commands"]() if callable(s["commands"]) else s["commands"]
    cwd = os.path.join(root, s["cwd"]) if s["cwd"] else root
    chunks = []
    for argv in commands:
        rc, out = runner(argv, cwd)
        chunks.append("$ %s\n%s" % (" ".join(str(a) for a in argv), out))
        if rc != 0:
            result.update(status=FAIL, exit_code=rc)
            break
    result["output"] = "".join(chunks)
    result["seconds"] = time.monotonic() - started
    return result


def select_steps(only) -> list:
    if not only:
        return list(STEPS)
    unknown = sorted(set(only) - {s["name"] for s in STEPS})
    if unknown:
        raise SystemExit("unknown step(s): %s — see --list" % ", ".join(unknown))
    return [s for s in STEPS if s["name"] in only]


def run(only=None, runner=subprocess_runner, root: str = REPO_ROOT, out=None,
        verbose: bool = False, fetch: bool = True, tree_note: bool = True,
        epilogue=None) -> dict:
    """Run the selected steps in ci.yml order, printing one verdict line per step as it finishes
    (a run is minutes long; a silent wait is the thing that gets Ctrl-C'd). Returns the report.
    `tree_note` prints the working-tree header line (always, from the CLI — `--no-fetch` does not
    suppress it); tests that count the runner's calls turn it off. `epilogue`, a zero-arg callable
    returning one line, is printed after the last step and before the summary (`temp_note`)."""
    out = out or sys.stdout
    steps = select_steps(only)
    if fetch:
        out.write("base: %s\n" % ensure_base_ref(runner, root))
    if tree_note:
        out.write(working_tree_note(runner, root) + "\n")
    results = []
    for s in steps:
        r = run_step(s, runner, root)
        results.append(r)
        out.write(verdict_line(r) + "\n")
        if r["status"] == FAIL or (verbose and r["output"]):
            out.write(indent(r["output"]) + "\n")
        out.flush()
    if epilogue is not None:
        out.write(epilogue() + "\n")
    report = summarize(results, subset=bool(only))
    out.write(report["summary"] + "\n")
    return report


def temp_note(run_root, scan=tmproot.new_leaks) -> str:
    """Report-only, never gates: leak-shaped entries (`tmp????????`, `seneschald_*`) created in the
    REAL temp dir while this run was going. Every process the run started had its temp pointed at
    `run_root`, so a non-zero count means a step bypassed it (a scrubbed env, a hard-coded path) —
    or another process on this host leaked them concurrently, which is why it cannot gate."""
    count, names = scan(run_root.real_tmp, run_root.started)
    if not count:
        return ("temp: 0 leak-shaped entries appeared in %s during the run (every step's temp was %s)"
                % (run_root.real_tmp, run_root.path))
    return ("temp: WARNING — %d leak-shaped entr%s (tmp????????/seneschald_*) appeared in %s during "
            "the run, e.g. %s. Every step's temp was %s, so a step bypassed it or another process on "
            "this host made them; re-run alone to tell which. Report-only."
            % (count, "y" if count == 1 else "ies", run_root.real_tmp, ", ".join(names),
               run_root.path))


def verdict_line(r: dict) -> str:
    tail = ""
    if r["status"] == SKIP:
        tail = " — " + r["reason"]
    elif r["status"] == FAIL:
        tail = " (exit %d)" % r["exit_code"]
    return "%-4s  %-20s %-15s %6.1fs%s" % (r["status"], r["name"], "[%s]" % r["job"],
                                          r["seconds"], tail)


def indent(text: str, prefix: str = "      ") -> str:
    return "\n".join(prefix + line for line in text.rstrip("\n").splitlines())


def summarize(results: list, subset: bool = False) -> dict:
    """The final verdict. Exit 1 iff any step FAILED. A skipped step is counted and NAMED in the
    summary line so a partial run reads as a partial run — "green with 4 skipped" is not "green";
    neither is a green `--only` subset, and the summary says which it was."""
    passed = [r["name"] for r in results if r["status"] == PASS]
    failed = [r["name"] for r in results if r["status"] == FAIL]
    skipped = [r["name"] for r in results if r["status"] == SKIP]
    lines = ["", "%d passed, %d failed, %d skipped (of %d steps)"
             % (len(passed), len(failed), len(skipped), len(results))]
    if failed:
        lines.append("RED — fix before pushing: " + ", ".join(failed))
    elif skipped:
        lines.append("GREEN on what ran, but %d step(s) were NOT checked here: %s — "
                     "CI will still run them; this is not a full run."
                     % (len(skipped), ", ".join(skipped)))
    elif subset:
        lines.append("GREEN on the %d selected step(s) — an --only run, not a full run."
                     % len(results))
    else:
        lines.append("GREEN — every CI gate ran here and passed.")
    return {"results": results, "passed": passed, "failed": failed, "skipped": skipped,
            "exit_code": 1 if failed else 0, "summary": "\n".join(lines)}


def list_steps(out=None) -> None:
    out = out or sys.stdout
    for s in STEPS:
        note = s["note"]
        if not note and not callable(s["commands"]):
            note = " && ".join(" ".join(os.path.basename(a) if i == 0 else a
                                        for i, a in enumerate(argv)) for argv in s["commands"])
        skip = "  (may SKIP: toolchain-dependent)" if s["skip"] else ""
        out.write("%-20s [%s]%s\n    %s%s\n" % (s["name"], s["job"],
                                                 (" cwd=" + s["cwd"]) if s["cwd"] else "",
                                                 note, skip))


def main(argv=None, runner=subprocess_runner) -> int:
    p = argparse.ArgumentParser(
        prog="ci_local.py",
        description="Run every gate .github/workflows/ci.yml runs, here, before a push.",
        epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list", action="store_true", help="print the step table and exit")
    p.add_argument("--only", action="append", metavar="STEP",
                   help="run only this step (repeatable); names from --list")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print every step's output, not only a failing step's")
    p.add_argument("--no-fetch", action="store_true",
                   help="do not fetch origin/develop when it is missing")
    args = p.parse_args(argv)
    if args.list:
        list_steps()
        return 0
    run_root = tmproot.install()
    # A faked runner started no processes, so there is no temp traffic to measure.
    epilogue = (lambda: temp_note(run_root)) if runner is subprocess_runner else None
    report = run(only=args.only, runner=runner, verbose=args.verbose, fetch=not args.no_fetch,
                 epilogue=epilogue)
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
