#!/usr/bin/env python3
"""Tests for cockpit/server/archons.py — the v5 archon SSO registry + GET-only reverse proxy
(cockpit-spec.md "Archon SSO tiles + proxy"). Pure stdlib, no FastAPI needed, so this runs
unconditionally (mirrors test_governor.py/test_model_config.py/test_health.py). Route-level wiring
(auth gating, the 405 write-block, `GET /api/archons`) is exercised in test_app.py instead.

Uses a tiny real localhost `http.server` as a fake archon backend so `proxy_get`'s `urllib.request`
call is exercised end-to-end, not mocked.

Run: python -m unittest cockpit.server.test_archons
"""
from __future__ import annotations

import gc
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cockpit.server import archons  # noqa: E402


class _FakeArchonHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/boom":
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            body = b"upstream broke"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({"path": self.path}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ErroringArchonHandler(BaseHTTPRequestHandler):
    """Answers EVERY path with a 500. `_probe` always requests `/`, and an error status still counts as
    reachable, so this is the arm where an unclosed response would pile up poll after poll."""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):  # noqa: N802
        body = b"upstream broke"
        self.send_response(500)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _RecordingResponse:
    """A stand-in for what `urlopen` returns, so the success arm's close can be asserted directly — it
    emits no ResourceWarning under CPython refcounting, so the warning can't be the signal there."""

    def __init__(self, close_raises=False):
        self.closed = False
        self._close_raises = close_raises

    def close(self):
        self.closed = True
        if self._close_raises:
            raise OSError("socket already torn down")


class ArchonsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.registry_path = Path(self._tmp.name) / "archon-registry.json"
        self._old_env = os.environ.get("COCKPIT_ARCHON_REGISTRY_PATH")
        os.environ["COCKPIT_ARCHON_REGISTRY_PATH"] = str(self.registry_path)

    def tearDown(self):
        self._tmp.cleanup()
        if self._old_env is None:
            os.environ.pop("COCKPIT_ARCHON_REGISTRY_PATH", None)
        else:
            os.environ["COCKPIT_ARCHON_REGISTRY_PATH"] = self._old_env

    def _write_registry(self, data: dict) -> None:
        with open(self.registry_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)


class LoadRegistryTests(ArchonsTestCase):
    def test_missing_file_is_tolerant(self):
        self.assertEqual(archons.load_registry(), {})

    def test_corrupt_file_is_tolerant(self):
        self.registry_path.write_text("not json", encoding="utf-8")
        self.assertEqual(archons.load_registry(), {})

    def test_loads_valid_registry(self):
        self._write_registry({"alpha-archon": {"title": "Alpha", "port": 9701, "status": "live"}})
        self.assertEqual(archons.load_registry()["alpha-archon"]["port"], 9701)


class ProbeTests(ArchonsTestCase):
    """`_probe` closes what it opens. Uses a real localhost server for the HTTP arms (same posture as
    the proxy tests) and a stub only where a real socket can't show the difference."""

    def _serve(self, handler) -> int:
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def _patch_urlopen(self, resp) -> None:
        original = urllib.request.urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", original)
        urllib.request.urlopen = lambda url, timeout=None: resp

    def test_error_status_still_counts_as_reachable(self):
        self.assertTrue(archons._probe(self._serve(_ErroringArchonHandler)))

    def test_error_response_is_closed(self):
        """An unclosed `HTTPError` surfaces as `ResourceWarning: Implicitly cleaning up <HTTPError
        500: ...>` from `tempfile`'s finalizer — it inherits `urllib.response.addinfourl`."""
        port = self._serve(_ErroringArchonHandler)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.assertTrue(archons._probe(port))
            gc.collect()
        self.assertEqual([str(w.message) for w in caught
                          if issubclass(w.category, ResourceWarning)], [])

    def test_ok_response_is_closed(self):
        resp = _RecordingResponse()
        self._patch_urlopen(resp)
        self.assertTrue(archons._probe(1234))
        self.assertTrue(resp.closed)

    def test_a_failing_close_neither_raises_nor_flips_the_verdict(self):
        """`_probe` promises it never raises, and it closes only after deciding — so a torn-down socket
        can't report a live archon as unreachable."""
        resp = _RecordingResponse(close_raises=True)
        self._patch_urlopen(resp)
        self.assertTrue(archons._probe(1234))
        self.assertTrue(resp.closed)


class ListArchonsTests(ArchonsTestCase):
    def test_empty_registry(self):
        self.assertEqual(archons.list_archons(), {"archons": []})

    def test_reserved_entry_has_no_probe(self):
        self._write_registry({"resting-archon": {"title": "Resting", "status": "reserved"}})
        result = archons.list_archons()
        self.assertEqual(result["archons"][0]["id"], "resting-archon")
        self.assertIsNone(result["archons"][0]["reachable"])
        self.assertIsNone(result["archons"][0]["port"])

    def test_live_entry_probes_reachability(self):
        server = HTTPServer(("127.0.0.1", 0), _FakeArchonHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self._write_registry({"alpha-archon": {"title": "Alpha", "port": port, "status": "live"}})
            result = archons.list_archons()
            self.assertTrue(result["archons"][0]["reachable"])
        finally:
            server.shutdown()
            server.server_close()

    def test_live_entry_unreachable_port_reports_false(self):
        # Nothing is listening on this port (bind-then-close to get a free one, then never serve).
        probe = HTTPServer(("127.0.0.1", 0), _FakeArchonHandler)
        free_port = probe.server_address[1]
        probe.server_close()
        self._write_registry({"alpha-archon": {"title": "Alpha", "port": free_port, "status": "live"}})
        result = archons.list_archons()
        self.assertFalse(result["archons"][0]["reachable"])

    def test_malformed_entries_are_skipped(self):
        self._write_registry({"alpha-archon": "not-a-dict", "resting-archon": {"title": "Resting", "status": "reserved"}})
        result = archons.list_archons()
        self.assertEqual(len(result["archons"]), 1)
        self.assertEqual(result["archons"][0]["id"], "resting-archon")

    def test_sorted_by_id(self):
        self._write_registry({
            "zeta": {"title": "Zeta", "status": "reserved"},
            "alpha": {"title": "Alpha", "status": "reserved"},
        })
        ids = [a["id"] for a in archons.list_archons()["archons"]]
        self.assertEqual(ids, ["alpha", "zeta"])


class ProxyGetTests(ArchonsTestCase):
    def setUp(self):
        super().setUp()
        self.server = HTTPServer(("127.0.0.1", 0), _FakeArchonHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def test_unknown_archon_404(self):
        with self.assertRaises(archons.ProxyError) as ctx:
            archons.proxy_get("nope", "index.html")
        self.assertEqual(ctx.exception.status, 404)

    def test_reserved_archon_503(self):
        self._write_registry({"resting-archon": {"title": "Resting", "status": "reserved"}})
        with self.assertRaises(archons.ProxyError) as ctx:
            archons.proxy_get("resting-archon", "")
        self.assertEqual(ctx.exception.status, 503)

    def test_live_entry_missing_port_503(self):
        self._write_registry({"alpha-archon": {"title": "Alpha", "status": "live"}})
        with self.assertRaises(archons.ProxyError) as ctx:
            archons.proxy_get("alpha-archon", "")
        self.assertEqual(ctx.exception.status, 503)

    def test_successful_proxy_roundtrip(self):
        self._write_registry({"alpha-archon": {"title": "Alpha", "port": self.port, "status": "live"}})
        body, status, content_type = archons.proxy_get("alpha-archon", "jobs/42.html")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json")
        self.assertEqual(json.loads(body)["path"], "/jobs/42.html")

    def test_query_string_forwarded(self):
        self._write_registry({"alpha-archon": {"title": "Alpha", "port": self.port, "status": "live"}})
        body, _, _ = archons.proxy_get("alpha-archon", "search", query="q=engineer")
        self.assertEqual(json.loads(body)["path"], "/search?q=engineer")

    def test_upstream_error_status_passed_through(self):
        self._write_registry({"alpha-archon": {"title": "Alpha", "port": self.port, "status": "live"}})
        body, status, content_type = archons.proxy_get("alpha-archon", "boom")
        self.assertEqual(status, 500)
        self.assertEqual(body, b"upstream broke")

    def test_ui_port_preferred_over_a2a_port_for_proxy(self):
        # The hard-won lesson: `port` is the A2A agent endpoint (9701-9749), `ui_port` is the
        # human-facing site. The proxy must hit the UI, not the agent-card.
        dead = HTTPServer(("127.0.0.1", 0), _FakeArchonHandler)
        a2a_port = dead.server_address[1]
        dead.server_close()  # nothing serves the A2A port; only ui_port answers
        self._write_registry({"alpha-archon": {"title": "Alpha", "port": a2a_port,
                                          "ui_port": self.port, "status": "live"}})
        body, status, _ = archons.proxy_get("alpha-archon", "jobs/42.html")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["path"], "/jobs/42.html")

    def test_ui_port_preferred_in_listing(self):
        self._write_registry({"alpha-archon": {"title": "Alpha", "port": 9701,
                                          "ui_port": 8770, "status": "reserved"}})
        self.assertEqual(archons.list_archons()["archons"][0]["port"], 8770)

    def test_unreachable_port_raises_502(self):
        probe = HTTPServer(("127.0.0.1", 0), _FakeArchonHandler)
        free_port = probe.server_address[1]
        probe.server_close()
        self._write_registry({"alpha-archon": {"title": "Alpha", "port": free_port, "status": "live"}})
        with self.assertRaises(archons.ProxyError) as ctx:
            archons.proxy_get("alpha-archon", "")
        self.assertEqual(ctx.exception.status, 502)


if __name__ == "__main__":
    unittest.main()
