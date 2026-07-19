#!/usr/bin/env python3
"""Tests for render_units.py — the per-machine daemon-asset renderer."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import render_units as ru  # noqa: E402
import setup_state  # noqa: E402

REAL_REPO = HERE.parents[1]


def scan_powershell_structure(text: str) -> list[str]:
    """A deliberately small structural scanner for a rendered .ps1 (pwsh itself is not
    spawnable on AMSI-wedged hosts; CI's pwsh parse step covers tracked scripts, and this
    covers the RENDERED, gitignored one): balanced {} / () outside strings and comments,
    no unterminated single/double-quoted string, no here-strings (the renderer avoids
    them on purpose — they'd need their own parser). Returns a list of problems."""
    problems: list[str] = []
    if "@'" in text or '@"' in text:
        problems.append("here-string marker found (renderer contract: none)")
    brace = paren = 0
    state = "code"  # code | squote | dquote | comment
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if state == "code":
            if ch == "#":
                state = "comment"
            elif ch == "'":
                state = "squote"
            elif ch == '"':
                state = "dquote"
            elif ch == "{":
                brace += 1
            elif ch == "}":
                brace -= 1
                if brace < 0:
                    problems.append(f"unmatched '}}' at offset {i}")
                    brace = 0
            elif ch == "(":
                paren += 1
            elif ch == ")":
                paren -= 1
                if paren < 0:
                    problems.append(f"unmatched ')' at offset {i}")
                    paren = 0
        elif state == "comment":
            if ch == "\n":
                state = "code"
        elif state == "squote":
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 1  # doubled quote = escaped
                else:
                    state = "code"
        elif state == "dquote":
            if ch == "`":
                i += 1  # backtick escapes the next char
            elif ch == '"':
                state = "code"
        i += 1
    if state == "squote":
        problems.append("unterminated single-quoted string")
    if state == "dquote":
        problems.append("unterminated double-quoted string")
    if brace != 0:
        problems.append(f"unbalanced braces (depth {brace} at EOF)")
    if paren != 0:
        problems.append(f"unbalanced parens (depth {paren} at EOF)")
    return problems


class Base(unittest.TestCase):
    """A fixture repo carrying the REAL tracked templates (so substitutions are tested
    against what actually ships) + its own wizard ledger."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "checkout"
        scripts = self.repo / "seneschal" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("run-presence.cmd", "run-presence.sh"):
            shutil.copy(REAL_REPO / "seneschal" / "scripts" / name, scripts / name)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def write_models(self, **models):
        state = setup_state.fresh()
        state["models"] = models
        path = self.repo / "seneschal" / "state" / "setup-state.json"
        setup_state.save(state, path)

    def run_cli(self, *extra) -> int:
        argv = ["render_units.py", "--repo", str(self.repo), "--home", str(self.home)] + list(extra)
        return ru._main(argv)

    def setup_dir(self) -> Path:
        return self.repo / "seneschal" / "state" / "setup"

    def plan_texts(self, platform, **kw) -> dict[str, str]:
        models = ru.load_models(self.repo)
        return {i["name"]: i["text"] for i in ru.build_plan(platform, self.repo, models, **kw)}


class WindowsRender(Base):
    def test_ps1_registers_the_doctor_expected_task_names(self):
        ps1 = self.plan_texts("win32")["register-tasks.ps1"]
        self.assertIn("Register-ScheduledTask -TaskName 'seneschald'", ps1)
        self.assertIn("Register-ScheduledTask -TaskName 'seneschald-update'", ps1)
        self.assertNotIn("seneschal-health-listener'", ps1)  # only with the flag

    def test_ps1_mirrors_scheduling_md_task_parameters(self):
        ps1 = self.plan_texts("win32")["register-tasks.ps1"]
        self.assertIn("-LogonType S4U", ps1)
        self.assertIn("-MultipleInstances IgnoreNew", ps1)
        self.assertIn("-RestartCount 999", ps1)
        self.assertIn("-RestartInterval (New-TimeSpan -Minutes 1)", ps1)
        self.assertIn("-ExecutionTimeLimit ([TimeSpan]::Zero)", ps1)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", ps1)
        self.assertIn("run-seneschald-update-hidden.vbs", ps1)  # windowless updater
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 10)", ps1)
        self.assertIn("register-tasks.result.json", ps1)

    def test_ps1_action_points_at_the_rendered_local_launcher(self):
        ps1 = self.plan_texts("win32")["register-tasks.ps1"]
        want = str(self.repo).replace("/", "\\") + "\\seneschal\\state\\setup\\run-presence.local.cmd"
        self.assertIn(want, ps1)

    def test_user_level_variant_swaps_the_principal(self):
        ps1 = self.plan_texts("win32", user_level=True)["register-tasks.ps1"]
        self.assertIn("-LogonType Interactive", ps1)
        self.assertNotIn("-LogonType S4U", ps1)
        self.assertIn("user_level   = $true", ps1)
        self.assertIn("logged on", ps1)  # the honest session-bound caveat

    def test_health_listener_included_only_on_flag(self):
        ps1 = self.plan_texts("win32", with_health_listener=True)["register-tasks.ps1"]
        self.assertIn("Register-ScheduledTask -TaskName 'seneschal-health-listener'", ps1)
        self.assertIn("run-health-listener.cmd", ps1)

    def test_ps1_structure_balanced(self):
        for kw in ({}, {"user_level": True}, {"with_health_listener": True}):
            ps1 = self.plan_texts("win32", **kw)["register-tasks.ps1"]
            self.assertEqual(scan_powershell_structure(ps1), [], kw)

    def test_cmd_local_pins_absolute_paths(self):
        cmd = self.plan_texts("win32")["run-presence.local.cmd"]
        repo_win = str(self.repo).replace("/", "\\")
        self.assertNotIn("%~dp0", cmd)  # every self-relative path is pinned
        self.assertIn(f'cd /d "{repo_win}"', cmd)
        self.assertIn(repo_win + "\\seneschal\\scripts\\presence.py", cmd)
        self.assertIn(repo_win + "\\.venv\\Scripts\\python.exe", cmd)
        self.assertIn("RENDERED LOCAL", cmd)
        self.assertIn("\r\n", cmd)  # cmd.exe files stay CRLF


class ModelSubstitution(Base):
    def test_absent_ledger_keeps_template_defaults(self):
        cmd = self.plan_texts("win32")["run-presence.local.cmd"]
        self.assertIn("--watch-model claude-haiku-4-5-20251001", cmd)
        # No --slot-model ARG line (the template's comment mentioning the flag is fine).
        self.assertNotRegex(cmd, r"(?m)^  --slot-model ")

    def test_watch_dial_substituted_and_canonicalized(self):
        self.write_models(watch="haiku", slots=None)
        cmd = self.plan_texts("win32")["run-presence.local.cmd"]
        self.assertIn("--watch-model claude-haiku-4-5 ^", cmd)
        self.assertNotIn("claude-haiku-4-5-20251001", cmd)

    def test_slots_dial_adds_slot_model_line_both_launchers(self):
        self.write_models(watch=None, slots="sonnet")
        cmd = self.plan_texts("win32")["run-presence.local.cmd"]
        sh = self.plan_texts("linux")["run-presence.local.sh"]
        self.assertIn("--slot-model claude-sonnet-5 ^\r\n", cmd)
        self.assertIn("--slot-model claude-sonnet-5 \\\n", sh)

    def test_unknown_id_passed_through_verbatim(self):
        self.write_models(watch="claude-custom-6")
        cmd = self.plan_texts("win32")["run-presence.local.cmd"]
        self.assertIn("--watch-model claude-custom-6", cmd)


class PosixRender(Base):
    def test_sh_local_sources_daemon_env_before_the_api_key_scrub(self):
        sh = self.plan_texts("linux")["run-presence.local.sh"]
        self.assertIn('DAEMON_ENV="${XDG_CONFIG_HOME:-$HOME/.config}/seneschal/daemon.env"', sh)
        self.assertIn('. "$DAEMON_ENV"', sh)
        self.assertLess(sh.index('. "$DAEMON_ENV"'), sh.index("unset ANTHROPIC_API_KEY"))
        self.assertIn('SCRIPT_DIR="' + str(self.repo).replace("\\", "/") + '/seneschal/scripts"', sh)
        self.assertIn("RENDERED LOCAL", sh)

    def test_systemd_units_load_bearing_lines(self):
        texts = self.plan_texts("linux")
        svc = texts["seneschald.service"]
        repo_posix = str(self.repo).replace("\\", "/")
        self.assertIn(f"ExecStart={repo_posix}/seneschal/state/setup/run-presence.local.sh", svc)
        self.assertIn("EnvironmentFile=-%h/.config/seneschal/daemon.env", svc)
        self.assertIn("Restart=on-failure", svc)
        self.assertIn("OnUnitActiveSec=10min", texts["seneschald-update.timer"])
        self.assertIn("Unit=seneschald-update.service", texts["seneschald-update.timer"])
        self.assertIn("run-seneschald-update.local.sh", texts["seneschald-update.service"])

    def test_update_script_is_ff_only_and_graceful(self):
        upd = self.plan_texts("linux")["run-seneschald-update.local.sh"]
        self.assertIn("pull --ff-only", upd)
        self.assertIn("request_control.py", upd)
        self.assertIn("uv sync --frozen", upd)
        self.assertIn("core.fsmonitor=false", upd)

    def test_launchd_plists_labels_and_schedules(self):
        texts = self.plan_texts("darwin")
        presence = texts["com.seneschal.presence.plist"]
        update = texts["com.seneschal.update.plist"]
        self.assertIn("<string>com.seneschal.presence</string>", presence)
        self.assertIn("<key>KeepAlive</key>", presence)
        self.assertIn("<key>RunAtLoad</key>", presence)
        self.assertIn("<string>com.seneschal.update</string>", update)
        self.assertIn("<integer>600</integer>", update)
        for text in (presence, update):  # both source daemon.env via the sh -c wrapper
            self.assertIn("/bin/sh", text)
            self.assertIn(".config/seneschal/daemon.env", text)

    def test_doctor_expected_names_all_present(self):
        # The names setup_doctor.check_daemon probes must be exactly what we render.
        self.assertEqual(ru.TASK_DAEMON, "seneschald")
        self.assertEqual(ru.TASK_UPDATE, "seneschald-update")
        self.assertEqual(ru.UNIT_DAEMON, "seneschald.service")
        self.assertEqual(ru.LABEL_PRESENCE, "com.seneschal.presence")
        self.assertIn("seneschald.service", self.plan_texts("linux"))
        self.assertIn("com.seneschal.presence.plist", self.plan_texts("darwin"))


class CliBehavior(Base):
    def test_dry_run_default_writes_nothing(self):
        for platform in ru.PLATFORMS:
            self.assertEqual(self.run_cli("--platform", platform), 0)
        self.assertFalse(self.setup_dir().exists())

    def test_install_respects_dry_run(self):
        self.assertEqual(self.run_cli("--platform", "linux", "--install"), 0)
        self.assertFalse((self.home / ".config").exists())

    def test_apply_writes_the_platform_assets(self):
        self.assertEqual(self.run_cli("--platform", "win32", "--apply"), 0)
        self.assertTrue((self.setup_dir() / "run-presence.local.cmd").is_file())
        self.assertTrue((self.setup_dir() / "register-tasks.ps1").is_file())

    def test_apply_install_copies_units_to_home(self):
        self.assertEqual(self.run_cli("--platform", "linux", "--apply", "--install"), 0)
        unit = self.home / ".config" / "systemd" / "user" / "seneschald.service"
        self.assertTrue(unit.is_file())
        staged = self.setup_dir() / "seneschald.service"
        self.assertEqual(unit.read_text(encoding="utf-8"), staged.read_text(encoding="utf-8"))

    def test_apply_install_copies_plists_on_darwin(self):
        self.assertEqual(self.run_cli("--platform", "darwin", "--apply", "--install"), 0)
        plist = self.home / "Library" / "LaunchAgents" / "com.seneschal.presence.plist"
        self.assertTrue(plist.is_file())

    @unittest.skipUnless(os.name == "posix", "executable bits are POSIX-only")
    def test_sh_locals_are_executable(self):
        self.run_cli("--platform", "linux", "--apply")
        for name in ("run-presence.local.sh", "run-seneschald-update.local.sh"):
            mode = os.stat(self.setup_dir() / name).st_mode
            self.assertTrue(mode & 0o111, name)

    def test_plan_marks_sh_locals_executable(self):
        plan = ru.build_plan("linux", self.repo, {})
        by_name = {i["name"]: i for i in plan}
        self.assertTrue(by_name["run-presence.local.sh"]["executable"])
        self.assertTrue(by_name["run-seneschald-update.local.sh"]["executable"])
        self.assertFalse(by_name["seneschald.service"]["executable"])

    def test_missing_template_is_a_clear_error(self):
        (self.repo / "seneschal" / "scripts" / "run-presence.cmd").unlink()
        self.assertEqual(self.run_cli("--platform", "win32", "--apply"), 2)

    def test_state_file_override_reads_that_ledger(self):
        alt = self.repo / "alt-ledger.json"
        state = setup_state.fresh()
        state["models"] = {"watch": "opus"}
        setup_state.save(state, alt)
        models = ru.load_models(self.repo, alt)
        self.assertEqual(models, {"watch": "claude-opus-4-8"})


class GitignoreCoverage(unittest.TestCase):
    def test_state_setup_renders_are_ignored(self):
        # seneschal/state/* covers the setup/ renders; the re-include patterns are
        # top-level README/examples only, so nothing under setup/ can leak into git.
        gitignore = (REAL_REPO / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("seneschal/state/*", gitignore)
        for line in gitignore.splitlines():
            if line.startswith("!seneschal/state/"):
                self.assertNotIn("setup", line, f"re-include must not expose setup/: {line}")


class ScannerSelfTest(unittest.TestCase):
    def test_scanner_catches_seeded_faults(self):
        self.assertTrue(scan_powershell_structure("if ($x) { foo"))
        self.assertTrue(scan_powershell_structure("$s = 'unterminated"))
        self.assertTrue(scan_powershell_structure('$s = "unterminated'))
        self.assertTrue(scan_powershell_structure("foo }"))
        self.assertEqual(scan_powershell_structure(
            "# comment with { and ' unmatched\n$a = '{'\n$b = \"}`\"\"\nif ($a) { $b }\n"), [])


if __name__ == "__main__":
    unittest.main()
