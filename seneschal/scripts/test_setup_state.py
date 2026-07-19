#!/usr/bin/env python3
"""Tests for the /setup wizard's resumability ledger (setup_state.py).

Stdlib ``unittest`` only. Run:  python -m unittest seneschal.scripts.test_setup_state
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import setup_state as ss  # noqa: E402


def _write_manifest(root: Path, entries: list[dict]) -> Path:
    path = root / "seneschal" / "setup" / "env-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
    return path


class LoadSave(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state_file = self.dir / "setup-state.json"

    def test_absent_file_loads_fresh(self):
        state = ss.load(self.state_file)
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["chapters"], {})
        for key in ("platform", "started_at", "updated_at", "features", "deps", "models"):
            self.assertIn(key, state)

    def test_corrupt_file_saved_aside_and_fresh(self):
        self.state_file.write_text("{not json", encoding="utf-8")
        state = ss.load(self.state_file)
        self.assertEqual(state["chapters"], {})
        self.assertTrue((self.dir / "setup-state.json.bak").is_file())
        self.assertEqual((self.dir / "setup-state.json.bak").read_text(encoding="utf-8"), "{not json")

    def test_non_object_body_saved_aside_and_fresh(self):
        self.state_file.write_text("[1, 2, 3]", encoding="utf-8")
        state = ss.load(self.state_file)
        self.assertEqual(state["chapters"], {})
        self.assertTrue((self.dir / "setup-state.json.bak").is_file())

    def test_load_never_raises_on_missing_containers(self):
        self.state_file.write_text(json.dumps({"version": 1, "chapters": {}}), encoding="utf-8")
        state = ss.load(self.state_file)
        self.assertEqual(state["features"], {})
        self.assertEqual(state["models"], {})

    def test_save_round_trip_and_atomic(self):
        state = ss.fresh()
        ss.mark(state, "env:telegram", "in-progress", summary="walking")
        ss.save(state, self.state_file)
        loaded = ss.load(self.state_file)
        self.assertEqual(loaded["chapters"]["env:telegram"]["status"], "in-progress")
        leftovers = [p for p in self.dir.iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])

    def test_save_creates_parent_dirs(self):
        deep = self.dir / "nested" / "state.json"
        ss.save(ss.fresh(), deep)
        self.assertTrue(deep.is_file())


class Mark(unittest.TestCase):
    def setUp(self):
        self.state = ss.fresh()

    def test_unknown_status_rejected(self):
        with self.assertRaises(ValueError):
            ss.mark(self.state, "persona", "finished")

    def test_empty_chapter_rejected(self):
        with self.assertRaises(ValueError):
            ss.mark(self.state, "", "done")

    def test_done_sets_completed_at_and_drops_step(self):
        ss.mark(self.state, "env:telegram", "in-progress", step="asking for the token")
        ch = ss.mark(self.state, "env:telegram", "done", summary="connected")
        self.assertIn("completed_at", ch)
        self.assertNotIn("step", ch)
        self.assertEqual(ch["summary"], "connected")

    def test_regressing_from_done_drops_completed_at(self):
        ss.mark(self.state, "store", "done")
        ch = ss.mark(self.state, "store", "in-progress")
        self.assertNotIn("completed_at", ch)

    def test_dotted_and_colon_chapter_ids(self):
        for chapter in ("env:telegram", "mcp:calendar", "env:push-call"):
            ch = ss.mark(self.state, chapter, "declined")
            self.assertEqual(ch["status"], "declined")
            self.assertNotIn("completed_at", ch)

    def test_artifacts_recorded(self):
        ch = ss.mark(self.state, "persona", "done", artifacts=["persona/identity.json"])
        self.assertEqual(ch["artifacts"], ["persona/identity.json"])

    def test_explicit_mark_clears_inferred_flag(self):
        self.state["chapters"]["store"] = {"status": "done", "inferred": True}
        ch = ss.mark(self.state, "store", "done")
        self.assertNotIn("inferred", ch)

    def test_only_whitelisted_fields_stored(self):
        ch = ss.mark(self.state, "env:ha", "blocked", summary="HA not stood up yet")
        self.assertLessEqual(set(ch), {"status", "summary", "step", "artifacts", "answers_hash", "completed_at", "inferred"})


class HashArtifacts(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "a.txt").write_bytes(b"alpha")
        (self.root / "b.txt").write_bytes(b"beta")

    def test_format_and_determinism(self):
        h1 = ss.hash_artifacts(["a.txt", "b.txt"], self.root)
        h2 = ss.hash_artifacts(["a.txt", "b.txt"], self.root)
        self.assertEqual(h1, h2)
        self.assertRegex(h1, r"^sha256:[0-9a-f]{16}$")

    def test_order_matters(self):
        self.assertNotEqual(
            ss.hash_artifacts(["a.txt", "b.txt"], self.root),
            ss.hash_artifacts(["b.txt", "a.txt"], self.root),
        )

    def test_content_change_changes_hash(self):
        before = ss.hash_artifacts(["a.txt"], self.root)
        (self.root / "a.txt").write_bytes(b"alpha2")
        self.assertNotEqual(before, ss.hash_artifacts(["a.txt"], self.root))

    def test_missing_artifact_raises(self):
        with self.assertRaises(OSError):
            ss.hash_artifacts(["nope.txt"], self.root)


class KnownArtifacts(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_builtins_present_even_without_manifest(self):
        rules = ss.known_artifacts(self.root / "missing.json")
        self.assertEqual(rules["persona"], ["persona/identity.json", "persona/persona.md"])
        self.assertEqual(rules["store"], ["seneschal/store/config.json"])

    def test_env_rules_read_from_manifest(self):
        manifest = _write_manifest(self.root, [{"id": "foo", "path": "seneschal/scripts/foo.env"}])
        rules = ss.known_artifacts(manifest)
        self.assertEqual(rules["env:foo"], ["seneschal/scripts/foo.env"])

    def test_corrupt_manifest_falls_back_to_builtins(self):
        manifest = self.root / "bad.json"
        manifest.write_text("nope", encoding="utf-8")
        rules = ss.known_artifacts(manifest)
        self.assertIn("persona", rules)
        self.assertNotIn("env:foo", rules)

    def test_real_manifest_yields_env_rules(self):
        rules = ss.known_artifacts()  # the tracked seneschal/setup/env-manifest.json
        self.assertEqual(rules["env:telegram"], ["seneschal/scripts/telegram.env"])
        self.assertIn("env:rag", rules)


class Infer(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.manifest = _write_manifest(self.root, [{"id": "foo", "path": "scripts/foo.env"}])
        self.env = self.root / "scripts" / "foo.env"
        self.env.parent.mkdir(parents=True, exist_ok=True)

    def test_pending_with_artifacts_present_becomes_inferred_done(self):
        self.env.write_text("X=1\n", encoding="utf-8")
        state = ss.fresh()
        changes = ss.infer(state, self.root, self.manifest)
        ch = state["chapters"]["env:foo"]
        self.assertEqual(ch["status"], "done")
        self.assertTrue(ch["inferred"])
        self.assertEqual(ch["artifacts"], ["scripts/foo.env"])
        self.assertEqual(len(changes), 1)

    def test_absent_artifacts_stay_pending_and_unrecorded(self):
        state = ss.fresh()
        changes = ss.infer(state, self.root, self.manifest)
        self.assertEqual(changes, [])
        self.assertNotIn("env:foo", state["chapters"])

    def test_done_with_missing_artifacts_goes_stale(self):
        state = ss.fresh()
        ss.mark(state, "env:foo", "done", artifacts=["scripts/foo.env"])
        changes = ss.infer(state, self.root, self.manifest)
        self.assertEqual(state["chapters"]["env:foo"]["status"], "stale")
        self.assertNotIn("completed_at", state["chapters"]["env:foo"])
        self.assertEqual(len(changes), 1)

    def test_done_with_hash_mismatch_goes_stale(self):
        self.env.write_text("X=1\n", encoding="utf-8")
        state = ss.fresh()
        good = ss.hash_artifacts(["scripts/foo.env"], self.root)
        ss.mark(state, "env:foo", "done", artifacts=["scripts/foo.env"], answers_hash=good)
        self.env.write_text("X=2\n", encoding="utf-8")
        ss.infer(state, self.root, self.manifest)
        self.assertEqual(state["chapters"]["env:foo"]["status"], "stale")

    def test_done_with_matching_hash_untouched(self):
        self.env.write_text("X=1\n", encoding="utf-8")
        state = ss.fresh()
        good = ss.hash_artifacts(["scripts/foo.env"], self.root)
        ss.mark(state, "env:foo", "done", artifacts=["scripts/foo.env"], answers_hash=good)
        self.assertEqual(ss.infer(state, self.root, self.manifest), [])
        self.assertEqual(state["chapters"]["env:foo"]["status"], "done")

    def test_infer_is_idempotent(self):
        self.env.write_text("X=1\n", encoding="utf-8")
        state = ss.fresh()
        first = ss.infer(state, self.root, self.manifest)
        second = ss.infer(state, self.root, self.manifest)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_non_pending_non_done_statuses_untouched(self):
        self.env.write_text("X=1\n", encoding="utf-8")
        for status in ("declined", "blocked", "in-progress", "awaiting-auth-restart", "stale"):
            state = ss.fresh()
            ss.mark(state, "env:foo", status)
            self.assertEqual(ss.infer(state, self.root, self.manifest), [], status)
            self.assertEqual(state["chapters"]["env:foo"]["status"], status)

    def test_builtin_persona_rule(self):
        (self.root / "persona").mkdir()
        (self.root / "persona" / "identity.json").write_text("{}", encoding="utf-8")
        state = ss.fresh()
        self.assertEqual(ss.infer(state, self.root, self.manifest), [])  # only one of two artifacts
        (self.root / "persona" / "persona.md").write_text("# me", encoding="utf-8")
        changes = ss.infer(state, self.root, self.manifest)
        self.assertEqual(state["chapters"]["persona"]["status"], "done")
        self.assertEqual(len(changes), 1)


class Board(unittest.TestCase):
    def test_empty_board_message(self):
        lines = ss.board_lines(ss.fresh())
        self.assertEqual(len(lines), 1)
        self.assertIn("no chapters", lines[0])

    def test_rows_aligned_with_glyphs(self):
        state = ss.fresh()
        ss.mark(state, "env:telegram", "done", summary="connected")
        ss.mark(state, "persona", "in-progress", summary="mid-interview")
        ss.mark(state, "env:ha", "declined")
        lines = ss.board_lines(state)
        self.assertEqual(len(lines), 3)
        joined = "\n".join(lines)
        self.assertIn("[x] done", joined)
        self.assertIn("[~] in-progress", joined)
        self.assertIn("[-] declined", joined)
        self.assertIn("connected", joined)
        # alignment: the status column starts at the same offset on every line
        offsets = {line.index("[") for line in lines}
        self.assertEqual(len(offsets), 1)

    def test_every_status_has_a_glyph(self):
        self.assertEqual(set(ss.GLYPHS), set(ss.STATUSES))


class Cli(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.state_file = self.root / "state" / "setup-state.json"
        self.manifest = _write_manifest(self.root, [{"id": "foo", "path": "scripts/foo.env"}])
        (self.root / "scripts").mkdir(parents=True, exist_ok=True)

    def run_cli(self, *args: str) -> tuple[int, str]:
        argv = [
            "setup_state.py",
            "--state-file", str(self.state_file),
            "--root", str(self.root),
            "--manifest", str(self.manifest),
            *args,
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ss._main(argv)
        return rc, buf.getvalue()

    def test_mark_get_board_round_trip(self):
        rc, out = self.run_cli("mark", "env:foo", "in-progress", "--summary", "walking", "--step", "token")
        self.assertEqual(rc, 0)
        self.assertIn('"ok": true', out)
        rc, out = self.run_cli("get", "env:foo")
        self.assertEqual(rc, 0)
        record = json.loads(out)
        self.assertEqual(record["status"], "in-progress")
        self.assertEqual(record["step"], "token")
        rc, out = self.run_cli("board")
        self.assertEqual(rc, 0)
        self.assertIn("env:foo", out)
        self.assertIn("[~] in-progress", out)

    def test_get_unknown_chapter_reports_pending(self):
        rc, out = self.run_cli("get", "env:nope")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"status": "pending"})

    def test_mark_hash_artifacts_and_infer_idempotent(self):
        (self.root / "scripts" / "foo.env").write_text("X=1\n", encoding="utf-8")
        rc, _ = self.run_cli(
            "mark", "env:foo", "done", "--artifacts", "scripts/foo.env", "--hash-artifacts"
        )
        self.assertEqual(rc, 0)
        rc, out = self.run_cli("get", "env:foo")
        self.assertRegex(json.loads(out)["answers_hash"], r"^sha256:[0-9a-f]{16}$")
        rc, out = self.run_cli("infer")
        self.assertEqual(rc, 0)
        self.assertIn("(no changes)", out)

    def test_mark_hash_without_artifacts_errors(self):
        buf_err = io.StringIO()
        with contextlib.redirect_stderr(buf_err):
            rc, _ = self.run_cli("mark", "env:foo", "done", "--hash-artifacts")
        self.assertEqual(rc, 2)
        self.assertIn("--hash-artifacts", buf_err.getvalue())

    def test_infer_reports_and_persists_change(self):
        (self.root / "scripts" / "foo.env").write_text("X=1\n", encoding="utf-8")
        rc, out = self.run_cli("infer")
        self.assertEqual(rc, 0)
        self.assertIn("env:foo: pending -> done (inferred", out)
        rc, out = self.run_cli("get", "env:foo")
        self.assertEqual(json.loads(out)["status"], "done")


if __name__ == "__main__":
    unittest.main()
