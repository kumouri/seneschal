#!/usr/bin/env python3
"""Tests for the /setup wizard's env-file writer (setup_env.py).

Stdlib ``unittest`` only. Run:  python -m unittest seneschal.scripts.test_setup_env
"""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import setup_env as se  # noqa: E402

EXAMPLE = """\
# Fake bot credentials — copy to fake.env and fill in.
# Gotcha: no inline comment after a value.

# The token. Required.
FAKE_BOT_TOKEN=

# Your chat id.
FAKE_CHAT_ID=

# Allowlist — usually just your own chat id.
FAKE_ALLOWED_IDS=

# Placeholder-style sample the owner must replace.
FAKE_API_KEY=your-api-key

# A vanilla default that is genuinely usable as-is.
FAKE_URL=http://localhost:11434

# Optional. Uncomment to override the recipient.
# FAKE_TO=+15550000000
"""

ENTRY = {
    "id": "fake",
    "title": "Fake service",
    "required": True,
    "format": "env",
    "path": "scripts/fake.env",
    "example": "scripts/fake.env.example",
    "docs": "scripts/FAKE_SETUP.md",
    "vars": [
        {"name": "FAKE_BOT_TOKEN", "required": True, "secret": True,
         "validate": "^\\d+:[A-Za-z0-9_-]{30,}$"},
        {"name": "FAKE_CHAT_ID", "required": True, "secret": False, "validate": "^-?\\d+$"},
        {"name": "FAKE_ALLOWED_IDS", "required": False, "secret": False,
         "default": "{FAKE_CHAT_ID}"},
        {"name": "FAKE_API_KEY", "required": True, "secret": True},
        {"name": "FAKE_URL", "required": False, "secret": False,
         "default": "http://localhost:11434"},
        {"name": "FAKE_TO", "required": False, "secret": False, "validate": "^\\+\\d{7,15}$"},
    ],
}

JSON_ENTRY = {
    "id": "fake-mcp",
    "required": False,
    "format": "json",
    "handled_by": "mcp",
    "path": "scripts/fake-mcp.json",
    "example": "scripts/fake-mcp.json.example",
    "vars": [],
}

GOOD_TOKEN = "123456789:AAfakefakefakefakefakefakefakefake-00"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "fake.env.example").write_text(EXAMPLE, encoding="utf-8")
        self.manifest_path = self.root / "setup" / "env-manifest.json"
        self.manifest_path.parent.mkdir()
        self.manifest_path.write_text(
            json.dumps({"version": 1, "entries": [ENTRY, JSON_ENTRY]}), encoding="utf-8"
        )
        self.target = self.root / "scripts" / "fake.env"

    def apply(self, values, replace=False):
        return se.apply(ENTRY, values, self.root, replace=replace)


class Render(Base):
    def test_preserves_comments_and_order(self):
        self.apply({"FAKE_BOT_TOKEN": GOOD_TOKEN, "FAKE_CHAT_ID": "42"})
        text = self.target.read_text(encoding="utf-8")
        self.assertIn("# Fake bot credentials", text)
        self.assertIn("# Gotcha: no inline comment", text)
        names = [line.split("=", 1)[0] for line in text.splitlines()
                 if se.VAR_LINE.match(line)]
        self.assertEqual(
            names,
            ["FAKE_BOT_TOKEN", "FAKE_CHAT_ID", "FAKE_ALLOWED_IDS", "FAKE_API_KEY", "FAKE_URL"],
        )

    def test_provided_values_written(self):
        self.apply({"FAKE_BOT_TOKEN": GOOD_TOKEN, "FAKE_CHAT_ID": "42"})
        live = se.parse_env_text(self.target.read_text(encoding="utf-8"))
        self.assertEqual(live["FAKE_BOT_TOKEN"], GOOD_TOKEN)
        self.assertEqual(live["FAKE_CHAT_ID"], "42")

    def test_default_template_expands_from_other_var(self):
        report = self.apply({"FAKE_CHAT_ID": "42"})
        live = se.parse_env_text(self.target.read_text(encoding="utf-8"))
        self.assertEqual(live["FAKE_ALLOWED_IDS"], "42")
        self.assertIn("FAKE_ALLOWED_IDS", report["vars_defaulted"])

    def test_default_template_with_unset_ref_goes_empty_with_warning(self):
        report = self.apply({})
        live = se.parse_env_text(self.target.read_text(encoding="utf-8"))
        self.assertEqual(live["FAKE_ALLOWED_IDS"], "")
        self.assertTrue(any("FAKE_ALLOWED_IDS" in w for w in report["warnings"]))

    def test_plain_default_applied(self):
        self.apply({})
        live = se.parse_env_text(self.target.read_text(encoding="utf-8"))
        self.assertEqual(live["FAKE_URL"], "http://localhost:11434")

    def test_example_value_kept_when_no_default(self):
        self.apply({})
        live = se.parse_env_text(self.target.read_text(encoding="utf-8"))
        self.assertEqual(live["FAKE_API_KEY"], "your-api-key")

    def test_commented_var_stays_commented_when_unset(self):
        self.apply({})
        text = self.target.read_text(encoding="utf-8")
        self.assertIn("# FAKE_TO=+15550000000", text)
        self.assertNotIn("\nFAKE_TO=", text)

    def test_commented_var_activated_when_provided(self):
        self.apply({"FAKE_TO": "+15551234567"})
        text = self.target.read_text(encoding="utf-8")
        self.assertIn("\nFAKE_TO=+15551234567", text)
        self.assertNotIn("# FAKE_TO=", text)

    def test_report_names_only_no_values(self):
        report = self.apply({"FAKE_BOT_TOKEN": GOOD_TOKEN, "FAKE_CHAT_ID": "42"})
        blob = json.dumps(report)
        self.assertNotIn(GOOD_TOKEN, blob)
        self.assertIn("FAKE_BOT_TOKEN", report["vars_set"])
        self.assertIn("FAKE_CHAT_ID", report["vars_set"])
        self.assertEqual(report["ok"], True)
        for key in ("path", "vars_set", "vars_kept", "vars_defaulted", "warnings"):
            self.assertIn(key, report)

    def test_trailing_newline_and_lf(self):
        self.apply({})
        raw = self.target.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r\n", raw)


class Merge(Base):
    def setUp(self):
        super().setUp()
        self.apply({"FAKE_BOT_TOKEN": GOOD_TOKEN, "FAKE_CHAT_ID": "42"})

    def test_rerun_keeps_unprovided_existing_values(self):
        report = self.apply({"FAKE_CHAT_ID": "77"})
        live = se.parse_env_text(self.target.read_text(encoding="utf-8"))
        self.assertEqual(live["FAKE_BOT_TOKEN"], GOOD_TOKEN)  # kept, not clobbered
        self.assertEqual(live["FAKE_CHAT_ID"], "77")           # overwritten
        self.assertIn("FAKE_BOT_TOKEN", report["vars_kept"])

    def test_existing_wins_over_default(self):
        # hand-set a var that has a manifest default, then re-run with nothing
        text = self.target.read_text(encoding="utf-8").replace(
            "FAKE_URL=http://localhost:11434", "FAKE_URL=http://10.0.0.5:11434"
        )
        self.target.write_text(text, encoding="utf-8")
        self.apply({})
        live = se.parse_env_text(self.target.read_text(encoding="utf-8"))
        self.assertEqual(live["FAKE_URL"], "http://10.0.0.5:11434")

    def test_owner_added_var_kept_with_warning(self):
        with open(self.target, "a", encoding="utf-8") as fh:
            fh.write("FAKE_EXTRA=handmade\n")
        report = self.apply({})
        live = se.parse_env_text(self.target.read_text(encoding="utf-8"))
        self.assertEqual(live["FAKE_EXTRA"], "handmade")
        self.assertTrue(any("FAKE_EXTRA" in w for w in report["warnings"]))

    def test_replace_ignores_existing(self):
        report = self.apply({"FAKE_CHAT_ID": "99"}, replace=True)
        live = se.parse_env_text(self.target.read_text(encoding="utf-8"))
        self.assertEqual(live["FAKE_BOT_TOKEN"], "")  # back to the example's empty value
        self.assertEqual(live["FAKE_CHAT_ID"], "99")
        self.assertEqual(report["vars_kept"], [])

    def test_activated_commented_var_survives_rerun(self):
        self.apply({"FAKE_TO": "+15551234567"})
        self.apply({})
        live = se.parse_env_text(self.target.read_text(encoding="utf-8"))
        self.assertEqual(live["FAKE_TO"], "+15551234567")


class Validation(Base):
    def test_secret_value_never_echoed_on_reject(self):
        bad_secret = "totally-not-a-token-value"
        with self.assertRaises(se.SetupEnvError) as ctx:
            self.apply({"FAKE_BOT_TOKEN": bad_secret})
        msg = str(ctx.exception)
        self.assertIn("FAKE_BOT_TOKEN", msg)
        self.assertIn("<redacted>", msg)
        self.assertNotIn(bad_secret, msg)
        self.assertFalse(self.target.exists())  # rejected write never lands

    def test_non_secret_reject_names_var(self):
        with self.assertRaises(se.SetupEnvError) as ctx:
            self.apply({"FAKE_CHAT_ID": "not-a-number"})
        self.assertIn("FAKE_CHAT_ID", str(ctx.exception))

    def test_unknown_var_rejected(self):
        with self.assertRaises(se.SetupEnvError) as ctx:
            self.apply({"FAKE_NOPE": "x"})
        self.assertIn("FAKE_NOPE", str(ctx.exception))

    def test_newline_value_rejected(self):
        with self.assertRaises(se.SetupEnvError):
            self.apply({"FAKE_CHAT_ID": "42\nFAKE_EVIL=1"})

    def test_non_string_value_rejected(self):
        with self.assertRaises(se.SetupEnvError):
            self.apply({"FAKE_CHAT_ID": 42})

    def test_json_format_entry_rejected(self):
        with self.assertRaises(se.SetupEnvError) as ctx:
            se.apply(JSON_ENTRY, {}, self.root)
        self.assertIn("fake-mcp", str(ctx.exception))

    @unittest.skipIf(os.name == "nt", "0o600 mode bits are POSIX-only (Windows: user-profile ACLs)")
    def test_posix_file_mode_is_0600(self):
        self.apply({"FAKE_CHAT_ID": "42"})
        mode = stat.S_IMODE(os.stat(self.target).st_mode)
        self.assertEqual(mode, 0o600)


class Check(Base):
    def test_absent_file_all_required_missing(self):
        result = se.check(ENTRY, self.root)
        self.assertFalse(result["ok"])
        self.assertFalse(result["exists"])
        self.assertEqual(
            result["missing"], ["FAKE_BOT_TOKEN", "FAKE_CHAT_ID", "FAKE_API_KEY"]
        )

    def test_complete_file_ok(self):
        self.apply({"FAKE_BOT_TOKEN": GOOD_TOKEN, "FAKE_CHAT_ID": "42",
                    "FAKE_API_KEY": "real-key-material"})
        result = se.check(ENTRY, self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["missing"], [])

    def test_empty_required_var_missing(self):
        self.apply({"FAKE_CHAT_ID": "42", "FAKE_API_KEY": "real-key-material"})
        result = se.check(ENTRY, self.root)
        self.assertEqual(result["missing"], ["FAKE_BOT_TOKEN"])

    def test_unchanged_placeholder_counts_as_missing(self):
        self.apply({"FAKE_BOT_TOKEN": GOOD_TOKEN, "FAKE_CHAT_ID": "42"})
        result = se.check(ENTRY, self.root)  # FAKE_API_KEY still "your-api-key"
        self.assertIn("FAKE_API_KEY", result["missing"])

    def test_names_only_no_values_in_report(self):
        self.apply({"FAKE_BOT_TOKEN": GOOD_TOKEN, "FAKE_CHAT_ID": "42",
                    "FAKE_API_KEY": "real-key-material"})
        blob = json.dumps(se.check(ENTRY, self.root))
        self.assertNotIn(GOOD_TOKEN, blob)
        self.assertNotIn("real-key-material", blob)


class Cli(Base):
    def run_cli(self, *args: str, stdin: str | None = None) -> tuple[int, str]:
        argv = ["setup_env.py", "--manifest", str(self.manifest_path), "--root", str(self.root), *args]
        buf = io.StringIO()
        old_stdin = sys.stdin
        try:
            if stdin is not None:
                sys.stdin = io.StringIO(stdin)
            with contextlib.redirect_stdout(buf):
                rc = se._main(argv)
        finally:
            sys.stdin = old_stdin
        return rc, buf.getvalue()

    def test_stdin_payload_round_trip(self):
        payload = json.dumps({"manifest_id": "fake",
                              "values": {"FAKE_BOT_TOKEN": GOOD_TOKEN, "FAKE_CHAT_ID": "42"}})
        rc, out = self.run_cli(stdin=payload)
        self.assertEqual(rc, 0)
        report = json.loads(out)
        self.assertTrue(report["ok"])
        self.assertNotIn(GOOD_TOKEN, out)
        self.assertTrue(self.target.is_file())

    def test_check_flag(self):
        rc, out = self.run_cli("--check", "fake")
        self.assertEqual(rc, 0)
        result = json.loads(out)
        self.assertFalse(result["ok"])
        self.assertIn("FAKE_BOT_TOKEN", result["missing"])

    def test_unknown_manifest_id_fails_cleanly(self):
        rc, out = self.run_cli(stdin=json.dumps({"manifest_id": "nope", "values": {}}))
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_bad_stdin_fails_cleanly(self):
        rc, out = self.run_cli(stdin="{not json")
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["ok"])

    def test_validation_failure_redacts_secret_in_cli_output(self):
        bad_secret = "super-secret-but-invalid"
        rc, out = self.run_cli(stdin=json.dumps(
            {"manifest_id": "fake", "values": {"FAKE_BOT_TOKEN": bad_secret}}))
        self.assertEqual(rc, 1)
        self.assertNotIn(bad_secret, out)
        self.assertIn("<redacted>", out)


class RealManifestSmoke(unittest.TestCase):
    """setup_env against the REAL tracked manifest + example, in a temp root."""

    def setUp(self):
        self.repo = Path(SCRIPT_DIR).resolve().parents[1]
        self.root = Path(tempfile.mkdtemp())
        scripts = self.root / "seneschal" / "scripts"
        scripts.mkdir(parents=True)
        real_example = self.repo / "seneschal" / "scripts" / "telegram.env.example"
        (scripts / "telegram.env.example").write_text(
            real_example.read_text(encoding="utf-8"), encoding="utf-8"
        )

    def test_real_telegram_entry_renders_and_checks(self):
        manifest = se.load_manifest(self.repo / "seneschal" / "setup" / "env-manifest.json")
        entry = se.find_entry(manifest, "telegram")
        token = "123456789:AAfakefakefakefakefakefakefakefake-00"
        report = se.apply(entry, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": "5551234"},
                          self.root)
        self.assertTrue(report["ok"])
        live = se.parse_env_text(
            (self.root / "seneschal" / "scripts" / "telegram.env").read_text(encoding="utf-8"))
        self.assertEqual(live["TELEGRAM_ALLOWED_CHAT_IDS"], "5551234")  # recommended default
        self.assertEqual(live["TELEGRAM_API_BASE"], "https://api.telegram.org")
        result = se.check(entry, self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["missing"], [])


if __name__ == "__main__":
    unittest.main()
