#!/usr/bin/env python3
"""Tests for cockpit_site.py — the daemon-side supervision pieces for the cockpit backend (the
dependency probe, the `.git` HEAD reader behind the deploy bounce, the env the backend is spawned
with, the frontend build plan, the detached spawn, and the adopt-on-reload PID record).

The presence.py wiring that drives these (the supervised task's gating, reconcile, deploy-bounce and
build-then-bounce) is not on this branch yet; its tests land with it.

No real process spawns, no real network, no npm, no git subprocess — every side-effecting call is
either monkeypatched or takes an injectable seam (`spawn_backend`'s `runner`, `build_steps`/
`npm_available`'s `which`, `deps_available`'s `find_spec`), the same invariant test_archon_sites.py
holds to.

Run: python -m unittest seneschal.scripts.test_cockpit_site   (or)   python test_cockpit_site.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import cockpit_site as cs  # noqa: E402

def _make_git(repo_root: str, *, sha="a" * 40, branch="develop", packed=False, detached=False,
              gitdir_file=False) -> str:
    """Build a minimal `.git` good enough for read_head_rev. Returns the sha written."""
    gitdir = os.path.join(repo_root, ".git")
    if gitdir_file:
        real = os.path.join(repo_root, "realgit")
        os.makedirs(real, exist_ok=True)
        with open(gitdir, "w", encoding="utf-8") as fh:
            fh.write(f"gitdir: {real}\n")
        gitdir = real
    else:
        os.makedirs(gitdir, exist_ok=True)
    if detached:
        with open(os.path.join(gitdir, "HEAD"), "w", encoding="utf-8") as fh:
            fh.write(sha + "\n")
        return sha
    ref = f"refs/heads/{branch}"
    with open(os.path.join(gitdir, "HEAD"), "w", encoding="utf-8") as fh:
        fh.write(f"ref: {ref}\n")
    if packed:
        with open(os.path.join(gitdir, "packed-refs"), "w", encoding="utf-8") as fh:
            fh.write("# pack-refs with: peeled fully-peeled sorted\n")
            fh.write(f"{sha} {ref}\n")
            fh.write("^" + "b" * 40 + "\n")
    else:
        loose = os.path.join(gitdir, *ref.split("/"))
        os.makedirs(os.path.dirname(loose), exist_ok=True)
        with open(loose, "w", encoding="utf-8") as fh:
            fh.write(sha + "\n")
    return sha


def _make_web(repo_root: str, *, package_json=True, node_modules=False, dist=False) -> str:
    web = cs.web_dir(repo_root)
    os.makedirs(os.path.join(web, "src"), exist_ok=True)
    with open(os.path.join(web, "src", "main.tsx"), "w", encoding="utf-8") as fh:
        fh.write("// source\n")
    if package_json:
        with open(os.path.join(web, "package.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
    if node_modules:
        os.makedirs(os.path.join(web, "node_modules"), exist_ok=True)
    if dist:
        os.makedirs(os.path.join(web, "dist"), exist_ok=True)
        with open(os.path.join(web, "dist", "index.html"), "w", encoding="utf-8") as fh:
            fh.write("<html></html>")
        # Make dist unambiguously newer than the sources written above.
        future = time.time() + 60
        os.utime(os.path.join(web, "dist", "index.html"), (future, future))
    return web


class DepsAvailableTests(unittest.TestCase):
    def test_true_when_both_extras_resolve(self):
        self.assertTrue(cs.deps_available(find_spec=lambda name: object()))

    def test_false_when_one_is_missing(self):
        self.assertFalse(cs.deps_available(find_spec=lambda name: None if name == "uvicorn" else object()))

    def test_a_raising_probe_fails_closed(self):
        def boom(_name):
            raise ImportError("broken sys.path entry")
        self.assertFalse(cs.deps_available(find_spec=boom))


class ReadHeadRevTests(unittest.TestCase):
    def test_loose_ref(self):
        d = tempfile.mkdtemp()
        sha = _make_git(d)
        self.assertEqual(cs.read_head_rev(d), sha)

    def test_packed_refs_fallback(self):
        d = tempfile.mkdtemp()
        sha = _make_git(d, packed=True)
        self.assertEqual(cs.read_head_rev(d), sha)

    def test_detached_head_is_the_sha_itself(self):
        d = tempfile.mkdtemp()
        sha = _make_git(d, detached=True)
        self.assertEqual(cs.read_head_rev(d), sha)

    def test_gitdir_file_indirection_worktree(self):
        d = tempfile.mkdtemp()
        sha = _make_git(d, gitdir_file=True)
        self.assertEqual(cs.read_head_rev(d), sha)

    def test_missing_git_is_none(self):
        self.assertIsNone(cs.read_head_rev(tempfile.mkdtemp()))

    def test_corrupt_head_is_none_not_raise(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".git"))
        with open(os.path.join(d, ".git", "HEAD"), "w", encoding="utf-8") as fh:
            fh.write("ref: refs/heads/nope\n")
        self.assertIsNone(cs.read_head_rev(d))


class LoadEnvTests(unittest.TestCase):
    def test_parses_comments_blanks_and_quotes(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "cockpit.env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# a comment\n\nCOCKPIT_OIDC_CLIENT_ID=\"abc123\"\nCOCKPIT_ALLOWED_USER='owner'\n")
        self.assertEqual(cs.load_env(path),
                         {"COCKPIT_OIDC_CLIENT_ID": "abc123", "COCKPIT_ALLOWED_USER": "owner"})

    def test_missing_file_is_empty(self):
        self.assertEqual(cs.load_env(os.path.join(tempfile.mkdtemp(), "nope.env")), {})

    def test_none_is_empty(self):
        self.assertEqual(cs.load_env(None), {})


class ChildEnvTests(unittest.TestCase):
    def test_scrubs_api_key_and_points_at_this_daemon(self):
        repo = tempfile.mkdtemp()
        env = cs.child_env(repo, r"C:\state", 8471,
                           base_env={"ANTHROPIC_API_KEY": "sk-leak", "PATH": "/usr/bin"})
        self.assertNotIn("ANTHROPIC_API_KEY", sorted(env.keys()))
        self.assertEqual(env["SENESCHAL_STATE_DIR"], r"C:\state")
        self.assertEqual(env["COCKPIT_PIPE_PORT"], "8471")
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_dev_no_auth_when_unconfigured(self):
        repo = tempfile.mkdtemp()
        env = cs.child_env(repo, "/state", 8471, base_env={})
        self.assertEqual(env["COCKPIT_DEV_NO_AUTH"], "1")

    def test_cockpit_env_wins_and_suppresses_the_dev_stub(self):
        repo = tempfile.mkdtemp()
        os.makedirs(os.path.join(repo, "cockpit", "server"))
        with open(os.path.join(repo, cs.COCKPIT_ENV_RELPATH), "w", encoding="utf-8") as fh:
            fh.write("COCKPIT_OIDC_CLIENT_ID=real-client\nCOCKPIT_ALLOWED_USER=owner\n")
        env = cs.child_env(repo, "/state", 8471, base_env={})
        self.assertEqual(env["COCKPIT_OIDC_CLIENT_ID"], "real-client")
        self.assertNotIn("COCKPIT_DEV_NO_AUTH", sorted(env.keys()))

    def test_pipe_port_follows_the_daemons_own_flag(self):
        env = cs.child_env(tempfile.mkdtemp(), "/state", 9999, base_env={})
        self.assertEqual(env["COCKPIT_PIPE_PORT"], "9999")


class BuildNeededTests(unittest.TestCase):
    def test_missing_dist_needs_a_build(self):
        d = tempfile.mkdtemp()
        _make_web(d)
        self.assertTrue(cs.build_needed(d))

    def test_dist_without_index_html_needs_a_build(self):
        d = tempfile.mkdtemp()
        web = _make_web(d)
        os.makedirs(os.path.join(web, "dist"))
        self.assertTrue(cs.build_needed(d))

    def test_fresh_dist_does_not(self):
        d = tempfile.mkdtemp()
        _make_web(d, dist=True)
        self.assertFalse(cs.build_needed(d))

    def test_source_newer_than_dist_needs_a_rebuild(self):
        d = tempfile.mkdtemp()
        web = _make_web(d, dist=True)
        future = time.time() + 3600
        src = os.path.join(web, "src", "main.tsx")
        os.utime(src, (future, future))
        self.assertTrue(cs.build_needed(d))


class BuildStepsTests(unittest.TestCase):
    def test_no_package_json_is_an_empty_plan(self):
        d = tempfile.mkdtemp()
        _make_web(d, package_json=False)
        self.assertEqual(cs.build_steps(d, which=lambda _n: "/usr/bin/npm"), [])

    def test_no_npm_is_an_empty_plan(self):
        d = tempfile.mkdtemp()
        _make_web(d)
        self.assertEqual(cs.build_steps(d, which=lambda _n: None), [])

    def test_cold_node_modules_gets_ci_then_build(self):
        d = tempfile.mkdtemp()
        web = _make_web(d)
        steps = cs.build_steps(d, which=lambda _n: "npm")
        self.assertEqual([s.name for s in steps], ["ci", "build"])
        self.assertEqual(steps[0].argv, ["npm", "ci"])
        self.assertEqual(steps[1].argv, ["npm", "run", "build"])
        self.assertEqual(steps[0].cwd, web)

    def test_warm_node_modules_skips_the_slow_install(self):
        d = tempfile.mkdtemp()
        _make_web(d, node_modules=True)
        self.assertEqual([s.name for s in cs.build_steps(d, which=lambda _n: "npm")], ["build"])

    def test_npm_available_reflects_both_halves(self):
        d = tempfile.mkdtemp()
        _make_web(d)
        self.assertTrue(cs.npm_available(d, which=lambda _n: "npm"))
        self.assertFalse(cs.npm_available(d, which=lambda _n: None))


class SpawnBackendTests(unittest.TestCase):
    class _FakeProc:
        pid = 4321

    def test_argv_env_and_cwd(self):
        repo, state = tempfile.mkdtemp(), tempfile.mkdtemp()
        seen = {}

        def runner(argv, **kwargs):
            seen["argv"], seen["kwargs"] = argv, kwargs
            return self._FakeProc()

        pid = cs.spawn_backend(repo, state, 8760, 8471, lambda *_: None, runner=runner)
        self.assertEqual(pid, 4321)
        self.assertEqual(seen["argv"][:2], [sys.executable, "-m"])
        self.assertIn("uvicorn", seen["argv"])
        self.assertIn("cockpit.server.app:app", seen["argv"])
        self.assertEqual(seen["argv"][-4:], ["--host", "127.0.0.1", "--port", "8760"])
        self.assertNotIn("--reload", seen["argv"])
        self.assertEqual(seen["kwargs"]["cwd"], repo)
        self.assertEqual(seen["kwargs"]["env"]["SENESCHAL_STATE_DIR"], state)
        self.assertEqual(seen["kwargs"]["env"]["COCKPIT_PIPE_PORT"], "8471")
        # child_env(base_env=None) here copies the real os.environ — assert against the key LIST,
        # never the dict, so a genuine scrub regression can't dump real secrets into the failure log.
        self.assertNotIn("ANTHROPIC_API_KEY", sorted(seen["kwargs"]["env"].keys()))

    def test_detached_flags_match_the_platform(self):
        repo, state = tempfile.mkdtemp(), tempfile.mkdtemp()
        seen = {}

        def runner(argv, **kwargs):
            seen.update(kwargs)
            return self._FakeProc()

        cs.spawn_backend(repo, state, 8760, 8471, lambda *_: None, runner=runner)
        if os.name == "nt":
            self.assertEqual(seen["creationflags"],
                             cs._DETACHED_PROCESS | cs._CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(seen["start_new_session"])

    def test_spawn_failure_is_logged_not_raised(self):
        logged = []

        def runner(_argv, **_kwargs):
            raise OSError("no interpreter")

        pid = cs.spawn_backend(tempfile.mkdtemp(), tempfile.mkdtemp(), 8760, 8471,
                               logged.append, runner=runner)
        self.assertIsNone(pid)
        self.assertTrue(any("spawn failed" in m for m in logged))


class RecordRoundTripTests(unittest.TestCase):
    def test_round_trip(self):
        d = tempfile.mkdtemp()
        cs.save_record(d, 99, "abc123", 8760)
        self.assertEqual(cs.load_record(d), {"pid": 99, "rev": "abc123", "port": 8760})

    def test_missing_file_is_empty(self):
        self.assertEqual(cs.load_record(tempfile.mkdtemp()), {})

    def test_corrupt_file_is_empty(self):
        d = tempfile.mkdtemp()
        with open(cs.pid_path(d), "w", encoding="utf-8") as fh:
            fh.write("{{{ not json")
        self.assertEqual(cs.load_record(d), {})

    def test_non_int_pid_is_rejected(self):
        d = tempfile.mkdtemp()
        with open(cs.pid_path(d), "w", encoding="utf-8") as fh:
            json.dump({"pid": "nope", "rev": "x", "port": 8760}, fh)
        self.assertEqual(cs.load_record(d), {})

    def test_bool_pid_is_rejected(self):
        d = tempfile.mkdtemp()
        with open(cs.pid_path(d), "w", encoding="utf-8") as fh:
            json.dump({"pid": True, "rev": "x", "port": 8760}, fh)
        self.assertEqual(cs.load_record(d), {})

    def test_clear_is_tolerant_of_absence(self):
        d = tempfile.mkdtemp()
        cs.clear_record(d)  # must not raise
        cs.save_record(d, 5, None, 8760)
        cs.clear_record(d)
        self.assertFalse(os.path.exists(cs.pid_path(d)))



if __name__ == "__main__":
    unittest.main()
