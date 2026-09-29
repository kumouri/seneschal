#!/usr/bin/env python3
"""tmproot.py — every test run gets ONE temp directory of its own, and it is always removed. Stdlib only.

## Why this exists

2026-09-29: the host's `%LOCALAPPDATA%\\Temp` held ~1,040,000 entries — ~1,008,000 `tmp????????`
directories (`tempfile.mkdtemp()`'s default shape) plus ~3,700 `<daemon>_test_*`, full of assistant
state fixtures (`med-sets.json`, `pantry.json`, `notion-outbox.sqlite`, `health.db`, ...) — arriving at
20k-170k a day. Windows logon stalled for 36-50 minutes on it: the User Profile Service chokes on a
flooded Temp at sign-in (Application log event 6006, `<Profiles> took 2979 second(s)`). The source was
the upstream assistant repo's suites and this one's (~190 `mkdtemp`/`TemporaryDirectory` call sites
here), many with no matching `rmtree`/`addCleanup` — and the ones that DID clean up still leaked
whenever the tree held a git repo, because git writes its object files read-only and
`shutil.rmtree(path, ignore_errors=True)` silently skips a read-only file on Windows (every surviving
`<daemon>_test_*` was exactly its `.git`, every file in it read-only).

Fixing hundreds of call sites one at a time gets undone by the next test written. So the fix is central: a
run's temp traffic is pointed at one directory that the run owns, and the run deletes it.

## How it attaches — two doors, one mechanism

1. **`test_0_tmproot.py` in every discovery root.** `unittest discover` imports a root's modules in
   sorted filename order, and `test_0_…` sorts before every `test_<letter>…`, so it is imported
   before any other test module's top level runs. It calls `install()`. That covers `ci.yml` and a
   bare `python -m unittest discover -s <root> -p "test_*.py"` typed by anyone. Its tests
   (`guard_tests`) are the BLOCKING half of the regression guard: they fail if the redirect is not in
   force, in-process or in a child, or if the guard stops sorting first; `test_tmproot.py` fails if a
   root `ci.yml` discovers has no guard.
2. **`ci_local.py`** calls `install()` in its own process before the first step, so every child it
   starts — the unittest roots, `npm test` — inherits the root, and each root's own `install()`
   ADOPTS it rather than nesting a second one. After the run it prints `new_leaks()`.

Not covered: one module run by name (`python -m unittest test_jobs`) never imports the guard. That is
a handful of directories, not a suite's thousands; a module that wants the guarantee anyway can call
`tmproot.install()` at its top.

## What install() does

* **Own** — no live `SENESCHAL_TEST_TMPROOT` in the environment: create
  `<system temp>/seneschal-test-runs/<pid>-<random>`, point `tempfile.tempdir` and `TMPDIR`/`TEMP`/`TMP`
  at it (children inherit those; Node's `os.tmpdir()` and .NET's `GetTempPath()` read them too),
  export `SENESCHAL_TEST_TMPROOT`, and at interpreter exit count what the run left behind, remove the root,
  and print one line to stderr if anything was left or could not be removed.
* **Adopt** — `SENESCHAL_TEST_TMPROOT` names an existing directory, so a parent (`ci_local.py`, an
  outer suite) owns it: point this process at the same root and leave removal to the owner. One root per run
  keeps paths short (Windows' 260-character limit) and the leftover count whole.
* **Sweep** — an owner first removes sibling roots under `seneschal-test-runs/` older than
  `STALE_AFTER_SEC`, so a run killed before its exit hook leaves one directory until the next run, not
  thousands forever. It never touches anything outside `seneschal-test-runs/`.

`rmtree()` is the removal to use for any tree a test made (it is what the exit hook uses): read-only
bits cleared, a short retry for Windows sharing violations, never a raise.

`new_leaks()` measures the regression the guard exists for: leak-shaped entries (`tmp????????`,
`seneschald_*`) CREATED in the real temp dir since an instant. It is report-only on purpose. While other
checkouts on this host still run the old suites, entries keep arriving from them (~4-6k an hour when
this was written), and a count cannot say whose they are — a gate red on someone else's leak gets
disabled. The blocking guarantee is the deterministic one above.
"""
from __future__ import annotations

import atexit
import fnmatch
import gc
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

#: Set by an owning process; a child that finds it pointing at a live directory adopts that root.
ENV_VAR = "SENESCHAL_TEST_TMPROOT"
#: The one directory under the system temp dir that every run root lives in, and the only one swept.
RUNS_DIR = "seneschal-test-runs"
#: A root this old belongs to a run that died before its exit hook; the longest suite runs ~30 min.
STALE_AFTER_SEC = 12 * 3600
#: `tempfile` reads these in this order; children (and Node, and PowerShell) inherit them.
TEMP_ENV_KEYS = ("TMPDIR", "TEMP", "TMP")
#: What a leaked test directory is called in the real temp dir: `mkdtemp()`'s default `tmp` + 8
#: characters, and `test_seneschald_control.py`'s `seneschald_test_` (plus the `_seed_`/`_origin_`
#: shapes the upstream suite uses, should they be ported).
LEAK_SHAPE = re.compile(r"^(tmp[a-z0-9_]{8}|seneschald_(test|seed|origin)_[a-z0-9_]{8})$")

_installed = None


class RunRoot:
    """One run's temp root. `owned` is True only in the process that created it — the one that
    removes it. `real_tmp` is the system temp dir the root was made under."""

    def __init__(self, path: str, real_tmp: str, owned: bool, started: float):
        self.path = path
        self.real_tmp = real_tmp
        self.owned = owned
        self.started = started

    def contains(self, path: str) -> bool:
        root = os.path.normcase(os.path.realpath(self.path))
        other = os.path.normcase(os.path.realpath(path))
        try:
            return os.path.commonpath([root, other]) == root
        except ValueError:  # different drives on Windows
            return False

    def close(self) -> tuple:
        """Remove the root if this process owns it. Returns `(entries_left, paths_not_removed)`:
        what the run's tests left at the top of the root, and anything `rmtree` could not delete.
        An adopter returns `(0, [])` and touches nothing."""
        if not self.owned:
            return 0, []
        try:
            left = len(os.listdir(self.path))
        except OSError:
            left = 0
        gc.collect()  # drop unreferenced sqlite connections/file objects so Windows lets go
        return left, rmtree(self.path)


def rmtree(path: str, attempts: int = 3) -> list:
    """Delete `path` and everything under it; never raises. Read-only files and directories are made
    writable and retried — git's object files are read-only, and `ignore_errors=True` alone silently
    leaves them (the whole `<daemon>_test_*` leak). A few attempts with a short pause ride out Windows
    sharing violations from a handle still closing. Returns the paths that could not be removed."""
    failed: list = []

    def on_error(func, target, _exc):
        try:
            if not os.path.islink(target):  # chmod follows links; never touch a link's target
                os.chmod(target, stat.S_IRWXU)
            parent = os.path.dirname(target)
            if parent:
                os.chmod(parent, stat.S_IRWXU)
            func(target)
        except FileNotFoundError:
            pass
        except OSError:
            failed.append(target)

    for attempt in range(attempts):
        if not os.path.lexists(path):
            return []
        failed.clear()
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=on_error)
        else:  # pragma: no cover - the host runs 3.14; CI's setup-python may not
            shutil.rmtree(path, onerror=on_error)
        if not os.path.lexists(path):
            return []
        if attempt + 1 < attempts:
            time.sleep(0.2 * (attempt + 1))
    return list(failed) or [path]


def sweep_stale(parent: str, now: float, max_age: float = STALE_AFTER_SEC, keep: str = "") -> int:
    """Remove run roots under `parent` whose mtime is older than `max_age` — runs that died before
    their exit hook. Only ever looks inside `parent`. Returns how many it removed."""
    removed = 0
    try:
        entries = list(os.scandir(parent))
    except OSError:
        return 0
    for entry in entries:
        if keep and os.path.normcase(entry.path) == os.path.normcase(keep):
            continue
        try:
            age = now - entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if age > max_age and not rmtree(entry.path):
            removed += 1
    return removed


def resolve(environ=None, base: str = "") -> RunRoot:
    """Decide own-or-adopt and, when owning, create the root under `base` (default: the system temp
    dir). Does not redirect anything — `redirect()` does that — so it can be tested with a dict."""
    environ = os.environ if environ is None else environ
    started = time.time()
    inherited = environ.get(ENV_VAR, "")
    if inherited and os.path.isdir(inherited):
        real_tmp = os.path.dirname(os.path.dirname(os.path.abspath(inherited)))
        return RunRoot(inherited, real_tmp, owned=False, started=started)
    real_tmp = base or tempfile.gettempdir()
    parent = os.path.join(real_tmp, RUNS_DIR)
    os.makedirs(parent, exist_ok=True)
    sweep_stale(parent, now=started)
    path = tempfile.mkdtemp(prefix="%d-" % os.getpid(), dir=parent)
    return RunRoot(path, real_tmp, owned=True, started=started)


def redirect(path: str, environ=None) -> None:
    """Point this process's `tempfile` and its children's environment at `path`."""
    environ = os.environ if environ is None else environ
    tempfile.tempdir = path
    for key in TEMP_ENV_KEYS:
        environ[key] = path
    environ[ENV_VAR] = path


def exit_report(root: RunRoot, left: int, failed: list) -> str:
    """The one stderr line an owner prints at exit — empty when the run left nothing behind."""
    lines = []
    if left:
        lines.append("tmproot: swept %d entr%s this run's tests left in %s"
                     % (left, "y" if left == 1 else "ies", root.path))
    if failed:
        lines.append("tmproot: could not remove %d path(s) (still open?), e.g. %s — the next run "
                     "removes the root once it is %dh old" % (len(failed), failed[0],
                                                                STALE_AFTER_SEC // 3600))
    return "\n".join(lines)


def _at_exit(root: RunRoot) -> None:
    left, failed = root.close()
    line = exit_report(root, left, failed)
    if line:
        try:
            sys.stderr.write(line + "\n")
        except (OSError, ValueError):  # stderr already closed at interpreter shutdown
            pass


def install() -> RunRoot:
    """Idempotent per process: own or adopt a run root, redirect temp into it, and (when owning)
    remove it at exit. Every test root's `test_0_tmproot.py` calls this at import."""
    global _installed
    if _installed is None:
        root = resolve()
        redirect(root.path)
        if root.owned:
            atexit.register(_at_exit, root)
        _installed = root
    return _installed


def new_leaks(real_tmp: str, since: float, sample: int = 3) -> tuple:
    """Leak-shaped entries in `real_tmp` created at or after `since`. Returns `(count, first_names)`.
    Report-only — see the module docstring for why it never gates."""
    count, names = 0, []
    try:
        entries = os.scandir(real_tmp)
    except OSError:
        return 0, []
    with entries:
        for entry in entries:
            if not LEAK_SHAPE.match(entry.name):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            created = getattr(st, "st_birthtime", None) or st.st_ctime
            if created >= since - 2:  # filesystem timestamp granularity
                count += 1
                if len(names) < sample:
                    names.append(entry.name)
    return count, names


def guard_tests(guard_file: str):
    """The cases every root's `test_0_tmproot.py` exposes, bound to that file. They are the blocking
    regression guard: each fails if the run's temp is not actually inside the run root."""
    guard_dir = os.path.dirname(os.path.abspath(guard_file))
    guard_name = os.path.basename(guard_file)

    class RunRootInForceTest(unittest.TestCase):
        def setUp(self):
            self.root = install()

        def test_tempfile_resolves_inside_the_run_root(self):
            self.assertTrue(self.root.contains(tempfile.gettempdir()),
                            "%s is outside %s" % (tempfile.gettempdir(), self.root.path))

        def test_mkdtemp_lands_inside_the_run_root(self):
            made = tempfile.mkdtemp()
            self.addCleanup(rmtree, made)
            self.assertTrue(self.root.contains(made), "%s is outside %s" % (made, self.root.path))

        def test_a_child_process_inherits_the_run_root(self):
            out = subprocess.run([sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
                                 capture_output=True, text=True, timeout=60, check=True).stdout.strip()
            self.assertTrue(self.root.contains(out), "child resolved %s, outside %s"
                            % (out, self.root.path))

        def test_this_guard_is_imported_before_every_other_test_module(self):
            names = sorted(n for n in os.listdir(guard_dir) if fnmatch.fnmatchcase(n, "test_*.py"))
            self.assertEqual(names[0], guard_name,
                             "discover imports %s before %s, so its top level runs un-redirected"
                             % (names[0], guard_name))

    return RunRootInForceTest
