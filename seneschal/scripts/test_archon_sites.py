#!/usr/bin/env python3
"""Tests for archon_sites.py — the daemon-side pieces for supervising archons' human-facing sites
(`site.json` discovery, the HTTP health probe, the detached spawn, the adopt-on-reload PID file, and
the best-effort kill).

The presence.py wiring that drives these (the supervised task's gating and reconcile loop, and the
control task's `restart-site` hand-off) is not on this branch yet; its tests land with it.

No real process spawns and no real network — every side-effecting call takes an injectable seam
(`spawn_site`'s `runner`) or is monkeypatched (`urllib.request.urlopen`, `os.kill`).

Run: python -m unittest seneschal.scripts.test_archon_sites   (or)   python test_archon_sites.py
"""
from __future__ import annotations

import gc
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import warnings
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import archon_sites as asites  # noqa: E402


def _write_site_json(archons_dir: str, archon_id: str, **overrides) -> str:
    archon_dir = os.path.join(archons_dir, archon_id)
    os.makedirs(archon_dir, exist_ok=True)
    data = {
        "schema": "seneschal.archon-site/1",
        "id": archon_id,
        "enabled": True,
        "port": 9800,
        "health_path": "/",
        "cmd": "python",
        "args": ["tools/serve_ui.py", "--serve", "--port", "9800"],
        "cwd": ".",
    }
    data.update(overrides)
    path = os.path.join(archon_dir, "site.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return archon_dir


class DiscoverSitesTests(unittest.TestCase):
    def test_missing_archons_dir_is_tolerant(self):
        self.assertEqual(asites.discover_sites(os.path.join(tempfile.mkdtemp(), "no-such-dir")), [])

    def test_archon_dir_without_site_json_is_skipped(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "no-site-archon"))
        self.assertEqual(asites.discover_sites(d), [])

    def test_malformed_json_is_skipped(self):
        d = tempfile.mkdtemp()
        archon_dir = os.path.join(d, "example-archon")
        os.makedirs(archon_dir)
        with open(os.path.join(archon_dir, "site.json"), "w", encoding="utf-8") as fh:
            fh.write("not json at all")
        self.assertEqual(asites.discover_sites(d), [])

    def test_non_dict_json_is_skipped(self):
        d = tempfile.mkdtemp()
        archon_dir = os.path.join(d, "example-archon")
        os.makedirs(archon_dir)
        with open(os.path.join(archon_dir, "site.json"), "w", encoding="utf-8") as fh:
            json.dump(["not", "a", "dict"], fh)
        self.assertEqual(asites.discover_sites(d), [])

    def test_enabled_false_is_skipped(self):
        d = tempfile.mkdtemp()
        _write_site_json(d, "example-archon", enabled=False)
        self.assertEqual(asites.discover_sites(d), [])

    def test_missing_port_is_skipped(self):
        d = tempfile.mkdtemp()
        data_dir = os.path.join(d, "example-archon")
        os.makedirs(data_dir)
        with open(os.path.join(data_dir, "site.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": "example-archon", "cmd": "python", "args": []}, fh)
        self.assertEqual(asites.discover_sites(d), [])

    def test_bool_port_is_skipped(self):
        # isinstance(True, int) is True in Python -- must be explicitly excluded.
        d = tempfile.mkdtemp()
        _write_site_json(d, "example-archon", port=True)
        self.assertEqual(asites.discover_sites(d), [])

    def test_missing_cmd_is_skipped(self):
        d = tempfile.mkdtemp()
        data_dir = os.path.join(d, "example-archon")
        os.makedirs(data_dir)
        with open(os.path.join(data_dir, "site.json"), "w", encoding="utf-8") as fh:
            json.dump({"id": "example-archon", "port": 9800, "args": []}, fh)
        self.assertEqual(asites.discover_sites(d), [])

    def test_blank_cmd_is_skipped(self):
        d = tempfile.mkdtemp()
        _write_site_json(d, "example-archon", cmd="   ")
        self.assertEqual(asites.discover_sites(d), [])

    def test_args_not_a_list_is_skipped(self):
        d = tempfile.mkdtemp()
        _write_site_json(d, "example-archon", args="not-a-list")
        self.assertEqual(asites.discover_sites(d), [])

    def test_valid_descriptor_is_discovered(self):
        d = tempfile.mkdtemp()
        _write_site_json(d, "example-archon")
        specs = asites.discover_sites(d)
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec.id, "example-archon")
        self.assertEqual(spec.port, 9800)
        self.assertEqual(spec.health_path, "/")
        self.assertEqual(spec.cmd, "python")
        self.assertEqual(spec.args, ["tools/serve_ui.py", "--serve", "--port", "9800"])
        self.assertEqual(spec.cwd_rel, ".")
        self.assertTrue(os.path.isabs(spec.archon_dir))

    def test_id_falls_back_to_directory_name(self):
        d = tempfile.mkdtemp()
        archon_dir = os.path.join(d, "example-archon")
        os.makedirs(archon_dir)
        with open(os.path.join(archon_dir, "site.json"), "w", encoding="utf-8") as fh:
            json.dump({"port": 9800, "cmd": "python", "args": []}, fh)
        specs = asites.discover_sites(d)
        self.assertEqual(specs[0].id, "example-archon")

    def test_health_path_defaults_to_slash_when_missing_or_blank(self):
        d = tempfile.mkdtemp()
        _write_site_json(d, "example-archon", health_path="")
        self.assertEqual(asites.discover_sites(d)[0].health_path, "/")

    def test_health_path_without_leading_slash_is_normalized_not_discarded(self):
        d = tempfile.mkdtemp()
        _write_site_json(d, "example-archon", health_path="status")
        self.assertEqual(asites.discover_sites(d)[0].health_path, "/status")

    def test_cwd_defaults_to_dot_when_missing_or_blank(self):
        d = tempfile.mkdtemp()
        _write_site_json(d, "example-archon", cwd="")
        self.assertEqual(asites.discover_sites(d)[0].cwd_rel, ".")

    def test_sorted_by_id(self):
        d = tempfile.mkdtemp()
        _write_site_json(d, "zeta")
        _write_site_json(d, "alpha")
        ids = [s.id for s in asites.discover_sites(d)]
        self.assertEqual(ids, ["alpha", "zeta"])

    def test_one_malformed_sibling_does_not_hide_a_valid_one(self):
        d = tempfile.mkdtemp()
        _write_site_json(d, "example-archon")
        bad_dir = os.path.join(d, "no-site-archon")
        os.makedirs(bad_dir)
        with open(os.path.join(bad_dir, "site.json"), "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        specs = asites.discover_sites(d)
        self.assertEqual([s.id for s in specs], ["example-archon"])


class _FakeHTTPResponse:
    def __init__(self, close_raises=False):
        self.closed = False
        self._close_raises = close_raises

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        self.closed = True
        if self._close_raises:
            raise OSError("socket already torn down")


class SiteHealthyTests(unittest.TestCase):
    def setUp(self):
        self._orig_urlopen = asites.urllib.request.urlopen

    def tearDown(self):
        asites.urllib.request.urlopen = self._orig_urlopen

    def test_connection_refused_is_unhealthy(self):
        def fake_urlopen(url, timeout=None):
            raise urllib.error.URLError("connection refused")
        asites.urllib.request.urlopen = fake_urlopen
        self.assertFalse(asites.site_healthy(9800, "/"))

    def test_timeout_is_unhealthy(self):
        def fake_urlopen(url, timeout=None):
            raise TimeoutError("timed out")
        asites.urllib.request.urlopen = fake_urlopen
        self.assertFalse(asites.site_healthy(9800, "/"))

    def test_2xx_response_is_healthy(self):
        def fake_urlopen(url, timeout=None):
            return _FakeHTTPResponse()
        asites.urllib.request.urlopen = fake_urlopen
        self.assertTrue(asites.site_healthy(9800, "/"))

    def test_http_error_status_still_counts_as_healthy(self):
        # A 404/500 means the server IS answering -- that's "serving," not "down" (mirrors
        # cockpit/server/archons.py's own _probe).
        def fake_urlopen(url, timeout=None):
            raise urllib.error.HTTPError(url, 500, "boom", None, None)
        asites.urllib.request.urlopen = fake_urlopen
        self.assertTrue(asites.site_healthy(9800, "/"))

    def test_http_error_response_is_closed(self):
        """An `HTTPError` IS the response object (it inherits `urllib.response.addinfourl`), so it holds
        its connection until closed. A 4xx/5xx site counts as healthy here, so the reconcile loop
        re-probes it every pass — one leaked response per pass if this regresses."""
        errors = []

        def fake_urlopen(url, timeout=None):
            err = urllib.error.HTTPError(url, 500, "boom", None, None)
            errors.append(err)
            raise err
        asites.urllib.request.urlopen = fake_urlopen
        self.assertTrue(asites.site_healthy(9800, "/"))
        self.assertTrue(errors[0].closed)

    def test_http_error_probe_emits_no_resource_warning(self):
        """The leak's own diagnostic: an unclosed `HTTPError` surfaces as
        `ResourceWarning: Implicitly cleaning up <HTTPError 500: 'boom'>` from `tempfile`'s finalizer."""
        def fake_urlopen(url, timeout=None):
            raise urllib.error.HTTPError(url, 500, "boom", None, None)
        asites.urllib.request.urlopen = fake_urlopen
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.assertTrue(asites.site_healthy(9800, "/"))
            gc.collect()
        self.assertEqual([str(w.message) for w in caught
                          if issubclass(w.category, ResourceWarning)], [])

    def test_2xx_response_is_closed(self):
        resp = _FakeHTTPResponse()
        asites.urllib.request.urlopen = lambda url, timeout=None: resp
        self.assertTrue(asites.site_healthy(9800, "/"))
        self.assertTrue(resp.closed)

    def test_a_failing_close_neither_raises_nor_flips_the_verdict(self):
        """`site_healthy` promises it never raises, and the close happens only AFTER the verdict is
        decided — so a torn-down socket can't turn a serving site into a restart."""
        resp = _FakeHTTPResponse(close_raises=True)
        asites.urllib.request.urlopen = lambda url, timeout=None: resp
        self.assertTrue(asites.site_healthy(9800, "/"))
        self.assertTrue(resp.closed)

    def test_health_path_without_leading_slash_is_normalized(self):
        seen = {}

        def fake_urlopen(url, timeout=None):
            seen["url"] = url
            return _FakeHTTPResponse()
        asites.urllib.request.urlopen = fake_urlopen
        asites.site_healthy(9800, "status")
        self.assertEqual(seen["url"], "http://127.0.0.1:9800/status")


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


class SpawnSiteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.spec = asites.SiteSpec(
            id="example-archon", port=9800, health_path="/", cmd="python",
            args=["tools/serve_ui.py", "--serve", "--port", "9800"],
            archon_dir=self.dir, cwd_rel=".",
        )
        self._orig_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-should-never-reach-the-child"

    def tearDown(self):
        if self._orig_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._orig_key

    def _capturing_runner(self, captured: dict):
        """A `runner` stub that records argv/kwargs. It deliberately does NOT close the real log file
        handle `spawn_site` opens (it always does — it only falls back to DEVNULL if `os.makedirs`/
        `open` itself fails): `spawn_site` closes the parent's copy itself, in a `finally`, so a stub
        that closed it here would mask a regression of that leak instead of surfacing it as an
        unclosed-file ResourceWarning."""

        def fake_runner(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeProc(1)

        return fake_runner

    def test_python_cmd_substitutes_sys_executable(self):
        captured = {}
        pid = asites.spawn_site(self.spec, lambda *_: None, runner=self._capturing_runner(captured))
        self.assertEqual(pid, 1)
        self.assertEqual(captured["argv"][0], sys.executable)
        self.assertEqual(captured["argv"][1:], self.spec.args)

    def test_non_python_cmd_is_used_verbatim(self):
        spec = asites.SiteSpec(id="x", port=1, health_path="/", cmd="/usr/bin/node",
                               args=["server.js"], archon_dir=self.dir, cwd_rel=".")
        captured = {}
        asites.spawn_site(spec, lambda *_: None, runner=self._capturing_runner(captured))
        self.assertEqual(captured["argv"], ["/usr/bin/node", "server.js"])

    def test_cwd_is_archon_dir_joined_with_cwd_rel(self):
        captured = {}
        asites.spawn_site(self.spec, lambda *_: None, runner=self._capturing_runner(captured))
        self.assertEqual(os.path.normpath(captured["kwargs"]["cwd"]), os.path.normpath(self.dir))

    def test_env_is_scrubbed_of_anthropic_api_key(self):
        captured = {}
        asites.spawn_site(self.spec, lambda *_: None, runner=self._capturing_runner(captured))
        # Against the key LIST, never the dict — child_env() copies the real os.environ, so a failure
        # here must never render whatever real secrets sit in this host's shell into the log.
        self.assertNotIn("ANTHROPIC_API_KEY", sorted(captured["kwargs"]["env"].keys()))

    def test_windows_branch_sets_detach_creationflags(self):
        captured = {}
        orig_name = asites.os.name
        asites.os.name = "nt"
        try:
            asites.spawn_site(self.spec, lambda *_: None, runner=self._capturing_runner(captured))
        finally:
            asites.os.name = orig_name
        self.assertEqual(
            captured["kwargs"]["creationflags"],
            asites._DETACHED_PROCESS | asites._CREATE_NEW_PROCESS_GROUP,
        )
        self.assertTrue(captured["kwargs"]["close_fds"])
        self.assertNotIn("start_new_session", captured["kwargs"])

    def test_posix_branch_sets_start_new_session(self):
        captured = {}
        orig_name = asites.os.name
        asites.os.name = "posix"
        try:
            asites.spawn_site(self.spec, lambda *_: None, runner=self._capturing_runner(captured))
        finally:
            asites.os.name = orig_name
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertNotIn("creationflags", captured["kwargs"])

    def test_stdout_stderr_point_at_the_archon_state_logs_file(self):
        captured = {}
        asites.spawn_site(self.spec, lambda *_: None, runner=self._capturing_runner(captured))
        log_path = os.path.join(self.dir, "state", "logs", "site.log")
        self.assertTrue(os.path.isfile(log_path))

    def test_spawn_failure_returns_none_and_logs(self):
        def boom_runner(argv, **kwargs):
            raise OSError("no such file or directory")

        logged = []
        pid = asites.spawn_site(self.spec, lambda msg: logged.append(msg), runner=boom_runner)
        self.assertIsNone(pid)
        self.assertTrue(any("spawn failed" in m for m in logged))

    def test_parent_log_handle_is_closed_after_spawn(self):
        """The parent's copy of `state/logs/site.log` must not outlive the spawn. The child inherits its
        own; the reconcile loop respawns a site every time it finds one down or wedged, so a handle held
        here leaks one per respawn inside a daemon that runs for weeks."""
        captured = {}
        asites.spawn_site(self.spec, lambda *_: None, runner=self._capturing_runner(captured))
        handle = captured["kwargs"]["stdout"]
        self.assertIs(captured["kwargs"]["stderr"], handle)
        self.assertTrue(handle.closed)

    def test_parent_log_handle_is_closed_even_when_spawn_fails(self):
        """The close lives in a `finally`, so it covers the failure arm too — the one a close merely
        tacked on after a successful `runner(...)` would miss, and the arm that repeats fastest when an
        archon's site is genuinely unspawnable."""
        captured = {}

        def boom_runner(argv, **kwargs):
            captured["stdout"] = kwargs.get("stdout")
            raise OSError("no such file or directory")

        pid = asites.spawn_site(self.spec, lambda *_: None, runner=boom_runner)
        self.assertIsNone(pid)
        self.assertTrue(captured["stdout"].closed)


class PidsRoundTripTests(unittest.TestCase):
    def test_missing_file_reads_empty(self):
        self.assertEqual(asites.load_pids(tempfile.mkdtemp()), {})

    def test_corrupt_file_reads_empty(self):
        d = tempfile.mkdtemp()
        with open(asites.pids_path(d), "w", encoding="utf-8") as fh:
            fh.write("not json")
        self.assertEqual(asites.load_pids(d), {})

    def test_non_dict_json_reads_empty(self):
        d = tempfile.mkdtemp()
        with open(asites.pids_path(d), "w", encoding="utf-8") as fh:
            json.dump([1, 2, 3], fh)
        self.assertEqual(asites.load_pids(d), {})

    def test_round_trip(self):
        d = tempfile.mkdtemp()
        asites.save_pids(d, {"example-archon": 4242})
        self.assertEqual(asites.load_pids(d), {"example-archon": 4242})

    def test_non_int_and_bool_values_are_filtered(self):
        d = tempfile.mkdtemp()
        with open(asites.pids_path(d), "w", encoding="utf-8") as fh:
            json.dump({"example-archon": 4242, "bad": "not-a-pid", "worse": True}, fh)
        self.assertEqual(asites.load_pids(d), {"example-archon": 4242})


class KillPidTests(unittest.TestCase):
    def setUp(self):
        self._orig_kill = asites.os.kill
        self._orig_name = asites.os.name

    def tearDown(self):
        asites.os.kill = self._orig_kill
        asites.os.name = self._orig_name

    def test_falsy_pid_never_calls_kill(self):
        calls = []
        asites.os.kill = lambda pid, sig: calls.append((pid, sig))
        asites.kill_pid(None)
        asites.kill_pid(0)
        self.assertEqual(calls, [])

    def test_windows_sends_sigterm_only(self):
        calls = []
        asites.os.name = "nt"
        asites.os.kill = lambda pid, sig: calls.append((pid, sig))
        asites.kill_pid(4242)
        self.assertEqual(calls, [(4242, asites.signal.SIGTERM)])

    def test_posix_sends_sigterm_then_sigkill(self):
        # Windows's stdlib `signal` module has no SIGKILL constant at all (only some real POSIX
        # systems do), so exercising this branch on a Windows test runner needs a fake `signal` with
        # both constants present -- the real module is restored in `finally` either way.
        calls = []
        asites.os.name = "posix"
        asites.os.kill = lambda pid, sig: calls.append((pid, sig))
        orig_signal = asites.signal

        class _FakeSignal:
            SIGTERM = getattr(orig_signal, "SIGTERM", 15)
            SIGKILL = getattr(orig_signal, "SIGKILL", 9)

        asites.signal = _FakeSignal
        try:
            asites.kill_pid(4242)
        finally:
            asites.signal = orig_signal
        self.assertEqual(calls, [(4242, _FakeSignal.SIGTERM), (4242, _FakeSignal.SIGKILL)])

    def test_already_dead_pid_never_raises(self):
        def raising_kill(pid, sig):
            raise OSError("no such process")
        asites.os.kill = raising_kill
        asites.kill_pid(999999)  # must not raise


if __name__ == "__main__":
    unittest.main()
