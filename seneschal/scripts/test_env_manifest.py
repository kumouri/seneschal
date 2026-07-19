#!/usr/bin/env python3
"""CI parity gate for ``seneschal/setup/env-manifest.json``.

The manifest is the single machine-readable source the /setup wizard and the
doctor read for every configurable env surface. These tests keep it honest:

  (a) it parses, with unique ids and sane entry shapes;
  (b) every tracked ``*.env.example`` under ``seneschal/scripts/`` and
      ``cockpit/server/`` has a manifest entry (by example path);
  (c) every manifest var name appears in its example file;
  (d) every example VAR line appears in the manifest's vars list (full
      bidirectional parity) — unless the entry declares ``"vars_complete": false``
      with a ``vars_incomplete_reason``;
  (e) required entries precede optional entries in file order;
  (f) vanilla Ollama everywhere: OLLAMA_URL defaults are exactly
      ``http://localhost:11434`` and no private port leaks in;
  (g) every ``docs`` path exists;
  (h) every ``verify.cmd`` / ``discover.cmd`` references a real script.

Stdlib ``unittest`` only. Run:  python -m unittest seneschal.scripts.test_env_manifest
"""
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import setup_env as se  # noqa: E402  (shares the VAR-line grammar)

REPO_ROOT = Path(SCRIPT_DIR).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "seneschal" / "setup" / "env-manifest.json"
EXAMPLE_DIRS = ("seneschal/scripts", "cockpit/server")

MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
ENTRIES = MANIFEST["entries"]
ENV_ENTRIES = [e for e in ENTRIES if e.get("format", "env") == "env"]


def example_var_names(text: str) -> set[str]:
    """VAR names in an example file: active ``NAME=`` lines + commented ``# NAME=`` lines."""
    names: set[str] = set()
    for line in text.splitlines():
        m = se.VAR_LINE.match(line) or se.COMMENTED_VAR_LINE.match(line)
        if m:
            names.add(m.group(1))
    return names


class Shape(unittest.TestCase):
    def test_parses_with_version_and_entries(self):
        self.assertEqual(MANIFEST["version"], 1)
        self.assertIsInstance(ENTRIES, list)
        self.assertGreater(len(ENTRIES), 0)

    def test_ids_unique(self):
        ids = [e["id"] for e in ENTRIES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_entry_has_the_core_fields(self):
        for e in ENTRIES:
            for field in ("id", "title", "required", "format", "path", "example", "docs",
                          "depends", "notes", "vars"):
                self.assertIn(field, e, f"{e.get('id')} missing {field}")
            self.assertIsInstance(e["required"], bool, e["id"])
            self.assertIn(e["format"], ("env", "json"), e["id"])
            self.assertIn("enable_question", e, e["id"])

    def test_example_is_path_plus_example_suffix(self):
        for e in ENTRIES:
            self.assertEqual(e["example"], e["path"] + ".example", e["id"])

    def test_example_files_exist(self):
        for e in ENTRIES:
            self.assertTrue((REPO_ROOT / e["example"]).is_file(), e["example"])

    def test_real_env_paths_are_gitignored(self):
        try:
            # -z (NUL separators) + binary I/O: text mode would CRLF-translate the
            # piped paths on Windows and every lookup would silently miss.
            proc = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "-c", "core.fsmonitor=false",
                 "check-ignore", "--stdin", "-z"],
                input=b"\0".join(e["path"].encode("utf-8") for e in ENTRIES) + b"\0",
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("git unavailable")
        ignored = {p.decode("utf-8") for p in proc.stdout.split(b"\0") if p}
        for e in ENTRIES:
            self.assertIn(e["path"], ignored, f"{e['path']} must be gitignored (it holds secrets)")

    def test_depends_entries_well_formed(self):
        for e in ENTRIES:
            for dep in e["depends"]:
                if isinstance(dep, str):
                    self.assertTrue(dep)
                else:
                    self.assertIsInstance(dep, dict, e["id"])
                    self.assertIn("id", dep, e["id"])
                    self.assertIn("soft", dep, e["id"])
                    self.assertIsInstance(dep["soft"], bool, e["id"])

    def test_var_entries_well_formed(self):
        for e in ENTRIES:
            for var in e["vars"]:
                self.assertRegex(var["name"], r"^[A-Z][A-Z0-9_]*$", e["id"])
                self.assertIsInstance(var["required"], bool, var["name"])
                self.assertIsInstance(var["secret"], bool, var["name"])
                if "validate" in var:
                    re.compile(var["validate"])  # must be a valid pattern
                if "default" in var:
                    self.assertIsInstance(var["default"], str, var["name"])

    def test_handled_by_entries_are_the_expected_ones(self):
        owned = {e["id"]: e.get("handled_by") for e in ENTRIES if "handled_by" in e}
        self.assertEqual(owned, {"cockpit": "cockpit", "slack-mcp": "mcp", "notion-mcp": "mcp"})


class ExampleParity(unittest.TestCase):
    def test_every_tracked_example_has_a_manifest_entry(self):  # (b)
        by_example = {e["example"] for e in ENTRIES}
        found = []
        for d in EXAMPLE_DIRS:
            for p in sorted((REPO_ROOT / d).glob("*.env.example")):
                found.append(p.relative_to(REPO_ROOT).as_posix())
        self.assertTrue(found, "no example files found — wrong repo root?")
        missing = [p for p in found if p not in by_example]
        self.assertEqual(missing, [], f"env examples with no manifest entry: {missing}")

    def test_manifest_vars_exist_in_example(self):  # (c)
        for e in ENV_ENTRIES:
            names = example_var_names((REPO_ROOT / e["example"]).read_text(encoding="utf-8"))
            for var in e["vars"]:
                self.assertIn(var["name"], names, f"{e['id']}: {var['name']} not in {e['example']}")

    def test_example_vars_exist_in_manifest(self):  # (d)
        for e in ENV_ENTRIES:
            if e.get("vars_complete") is False:
                self.assertTrue(e.get("vars_incomplete_reason"),
                                f"{e['id']}: vars_complete: false needs a vars_incomplete_reason")
                continue
            names = example_var_names((REPO_ROOT / e["example"]).read_text(encoding="utf-8"))
            declared = {v["name"] for v in e["vars"]}
            undeclared = sorted(names - declared)
            self.assertEqual(undeclared, [],
                             f"{e['id']}: example vars missing from the manifest: {undeclared}")

    def test_json_entries_declare_vars_incomplete(self):
        for e in ENTRIES:
            if e.get("format") == "json":
                self.assertIs(e.get("vars_complete"), False, e["id"])
                self.assertTrue(e.get("vars_incomplete_reason"), e["id"])


class Ordering(unittest.TestCase):
    def test_required_entries_precede_optional(self):  # (e)
        flags = [e["required"] for e in ENTRIES]
        first_optional = flags.index(False) if False in flags else len(flags)
        self.assertNotIn(True, flags[first_optional:],
                         "a required entry appears after an optional one")

    def test_telegram_is_the_only_required_entry(self):
        self.assertEqual([e["id"] for e in ENTRIES if e["required"]], ["telegram"])


class VanillaDefaults(unittest.TestCase):
    def test_ollama_url_defaults_are_vanilla(self):  # (f)
        seen = 0
        for e in ENTRIES:
            for var in e["vars"]:
                if var["name"] == "OLLAMA_URL":
                    seen += 1
                    self.assertEqual(var.get("default"), "http://localhost:11434",
                                     f"{e['id']}: OLLAMA_URL default must be the vanilla port")
        self.assertEqual(seen, 3)  # router, rag, sentiment

    def test_no_nonstandard_ollama_port_anywhere(self):
        # Guard against a non-default Ollama port from any contributor's machine
        # leaking into the shipped defaults. (Built by concatenation so identity
        # sweeps of the repo never match the guard itself.) Non-Ollama localhost
        # ports (e.g. the cockpit OIDC issuer) are legitimate; the per-var
        # OLLAMA_URL checks above pin the Ollama ones to 11434.
        nonstandard = "119" + "99"
        self.assertNotIn(nonstandard, MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_localhost_defaults_with_ollama_models_use_11434(self):
        for e in ENTRIES:
            if not e.get("ollama_models"):
                continue
            for var in e["vars"]:
                default = var.get("default", "")
                if "localhost" in default:
                    self.assertIn(":11434", default, f"{e['id']}: {var['name']}")

    def test_ollama_models_declared_where_expected(self):
        models = {e["id"]: e.get("ollama_models") for e in ENTRIES if e.get("ollama_models")}
        self.assertEqual(models, {
            "router": ["qwen3.5:4b"],
            "rag": ["nomic-embed-text"],
            "sentiment": ["qwen3.5:4b"],
        })


class DocsAndCommands(unittest.TestCase):
    def test_docs_paths_exist(self):  # (g)
        for e in ENTRIES:
            self.assertTrue((REPO_ROOT / e["docs"]).is_file(), f"{e['id']}: docs {e['docs']}")

    def _assert_cmd_scripts_exist(self, entry_id: str, cmd: str):
        tokens = cmd.replace('"', " ").split()
        py_tokens = [t for t in tokens if t.endswith(".py")]
        if not py_tokens:
            # the sanctioned scriptless form is an inline import check
            self.assertIn("python -c", cmd, f"{entry_id}: verify cmd has no script and is not python -c")
            return
        for t in py_tokens:
            self.assertTrue((REPO_ROOT / t).is_file(), f"{entry_id}: {t} does not exist")

    def test_verify_cmd_scripts_exist(self):  # (h)
        verified = 0
        for e in ENTRIES:
            verify = e.get("verify")
            if not verify:
                continue
            verified += 1
            self.assertIn("expect", verify, e["id"])
            self._assert_cmd_scripts_exist(e["id"], verify["cmd"])
        self.assertGreaterEqual(verified, 10)

    def test_discover_cmd_scripts_exist(self):
        for e in ENTRIES:
            for var in e["vars"]:
                discover = var.get("discover")
                if discover:
                    self.assertIn("say", discover, var["name"])
                    self.assertIn("extract", discover, var["name"])
                    self._assert_cmd_scripts_exist(e["id"], discover["cmd"])

    def test_verify_path_placeholder_matches_entry(self):
        for e in ENTRIES:
            cmd = (e.get("verify") or {}).get("cmd", "")
            if "--env-file {path}" in cmd:
                self.assertEqual(e.get("format", "env"), "env", e["id"])


class SecretHygiene(unittest.TestCase):
    def test_known_credentials_are_marked_secret(self):
        expected_secret = {
            "TELEGRAM_BOT_TOKEN", "PROTON_BRIDGE_PASS", "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET", "DISCORD_BOT_TOKEN", "HA_TOKEN",
            "PUSH_CALL_SECRET", "PUSH_SMS_SECRET",
        }
        marked = {v["name"] for e in ENTRIES for v in e["vars"] if v["secret"]}
        self.assertEqual(marked, expected_secret)

    def test_telegram_token_validate_is_the_botfather_shape(self):
        telegram = next(e for e in ENTRIES if e["id"] == "telegram")
        token = next(v for v in telegram["vars"] if v["name"] == "TELEGRAM_BOT_TOKEN")
        self.assertEqual(token["validate"], "^\\d+:[A-Za-z0-9_-]{30,}$")
        chat = next(v for v in telegram["vars"] if v["name"] == "TELEGRAM_CHAT_ID")
        self.assertEqual(chat["validate"], "^-?\\d+$")
        self.assertIn("discover", chat)

    def test_no_var_default_looks_like_a_credential(self):
        for e in ENTRIES:
            for var in e["vars"]:
                if var["secret"]:
                    self.assertEqual(var.get("default", ""), "",
                                     f"{e['id']}: secret {var['name']} must not ship a default")


if __name__ == "__main__":
    unittest.main()
