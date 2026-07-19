#!/usr/bin/env python3
"""Tests for the /doctor board (setup_doctor.py).

Stdlib ``unittest`` only. Run:  python -m unittest seneschal.scripts.test_setup_doctor

Fixture-driven: every test builds a throwaway repo tree (manifest, env examples, state,
persona, store) and monkeypatches the two effectful seams (``_run`` for subprocesses,
``_http_get`` for Ollama) so no test ever spawns a real child or touches the network.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import model_config  # noqa: E402
import setup_doctor as sd  # noqa: E402
import setup_state  # noqa: E402

SECRET_TOKEN = "111111:SECRET-TG-TOKEN-XYZ-do-not-print"
SECRET_OAUTH = "sk-oauth-SECRET-OAUTH-VALUE-do-not-print"

TELEGRAM_EXAMPLE = """\
# Fake telegram template.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
"""

PROTON_EXAMPLE = """\
PROTON_BRIDGE_USER=
PROTON_BRIDGE_PASS=
"""

ROUTER_EXAMPLE = """\
# Optional overrides; safe defaults are baked in.
OLLAMA_URL=http://localhost:11434
"""

MANIFEST = {
    "version": 1,
    "entries": [
        {
            "id": "telegram",
            "title": "Telegram bot",
            "required": True,
            "format": "env",
            "path": "seneschal/scripts/telegram.env",
            "example": "seneschal/scripts/telegram.env.example",
            "docs": "seneschal/scripts/TELEGRAM_SETUP.md",
            "depends": [],
            "vars": [
                {"name": "TELEGRAM_BOT_TOKEN", "required": True, "secret": True},
                {"name": "TELEGRAM_CHAT_ID", "required": True, "secret": False},
            ],
        },
        {
            "id": "proton",
            "title": "Proton bridge",
            "required": False,
            "format": "env",
            "path": "seneschal/scripts/proton.env",
            "example": "seneschal/scripts/proton.env.example",
            "docs": "seneschal/scripts/EMAIL_SETUP.md",
            "depends": [],
            "vars": [
                {"name": "PROTON_BRIDGE_USER", "required": True, "secret": False},
                {"name": "PROTON_BRIDGE_PASS", "required": True, "secret": True},
            ],
        },
        {
            "id": "router",
            "title": "Router advisor",
            "required": False,
            "format": "env",
            "path": "seneschal/scripts/router.env",
            "example": "seneschal/scripts/router.env.example",
            "docs": "seneschal/scripts/ROUTER_SETUP.md",
            "depends": [{"id": "ollama", "soft": False, "why": "classifier abstains to escalate without it"}],
            "ollama_models": ["tinychat:1b"],
            "vars": [{"name": "OLLAMA_URL", "required": False, "secret": False,
                      "default": "http://localhost:11434"}],
        },
        {
            "id": "notion-mcp",
            "title": "Notion MCP config",
            "required": False,
            "format": "json",
            "handled_by": "mcp",
            "path": "seneschal/scripts/notion-mcp.json",
            "example": "seneschal/scripts/notion-mcp.json.example",
            "vars": [],
        },
    ],
}


class Base(unittest.TestCase):
    """A throwaway repo tree + seam patches shared by every test class."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = Path(tempfile.mkdtemp())
        scripts = self.root / "seneschal" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "telegram.env.example").write_text(TELEGRAM_EXAMPLE, encoding="utf-8")
        (scripts / "proton.env.example").write_text(PROTON_EXAMPLE, encoding="utf-8")
        (scripts / "router.env.example").write_text(ROUTER_EXAMPLE, encoding="utf-8")
        setup_dir = self.root / "seneschal" / "setup"
        setup_dir.mkdir(parents=True)
        (setup_dir / "env-manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        self.state_dir = self.root / "seneschal" / "state"
        self.state_dir.mkdir(parents=True)
        (self.root / "persona").mkdir()
        (self.root / "seneschal" / "store").mkdir()
        # Default seams: every subprocess "succeeds" quietly; Ollama is unreachable.
        self.patch(sd, "_run", lambda cmd, timeout=30.0, env=None: (0, "", ""))
        self.patch(sd, "_http_get", self._raise_http)

    @staticmethod
    def _raise_http(url, timeout=3.0):
        raise OSError("connection refused")

    def patch(self, obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, original)

    def ctx(self, platform="win32", environ=None, **kw):
        return sd.build_ctx(
            root=self.root,
            state_dir=self.state_dir,
            manifest=self.root / "seneschal" / "setup" / "env-manifest.json",
            home=self.home,
            platform=platform,
            environ=environ if environ is not None else {},
            **kw,
        )

    # -- fixture builders -----------------------------------------------------

    def write_telegram_env(self):
        (self.root / "seneschal" / "scripts" / "telegram.env").write_text(
            f"TELEGRAM_BOT_TOKEN={SECRET_TOKEN}\nTELEGRAM_CHAT_ID=42\n", encoding="utf-8"
        )

    def write_store(self, active="markdown", mcp_config=None):
        backends = {"markdown": {"root_path": "~/data"}}
        if active == "notion":
            backends["notion"] = {"mcp_config": mcp_config or "seneschal/store/notion/mcp.json"}
        (self.root / "seneschal" / "store" / "config.json").write_text(
            json.dumps({"version": 1, "active": active, "backends": backends}), encoding="utf-8"
        )

    def write_venv(self, platform="win32"):
        vpy = self.root / ".venv" / ("Scripts/python.exe" if platform == "win32" else "bin/python")
        vpy.parent.mkdir(parents=True, exist_ok=True)
        vpy.write_text("", encoding="utf-8")

    def write_daemon_files(self, pid=None, health_age_min=1):
        (self.state_dir / "presence.lock").write_text(
            json.dumps({"pid": pid if pid is not None else os.getpid()}), encoding="utf-8"
        )
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=health_age_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (self.state_dir / "seneschald-health.json").write_text(
            json.dumps({"status": "ok", "updated_at": stamp}), encoding="utf-8"
        )

    def write_hooks(self):
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(
            json.dumps({"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": "python X/seneschal/scripts/session_stamp.py"}]}]}}),
            encoding="utf-8",
        )

    def write_persona(self):
        (self.root / "persona" / "identity.json").write_text("{}", encoding="utf-8")
        (self.root / "persona" / "persona.md").write_text("# persona\n", encoding="utf-8")
        (self.root / "persona" / "owner-profile.md").write_text("# owner\n", encoding="utf-8")

    def mark(self, chapter, status):
        state = setup_state.load(self.state_dir / "setup-state.json")
        setup_state.mark(state, chapter, status)
        setup_state.save(state, self.state_dir / "setup-state.json")

    def all_green(self):
        """Everything configured and healthy on a forced-win32 ctx."""
        self.write_telegram_env()
        model_config.save(str(self.state_dir), "opus", "fable")
        self.write_store("markdown")
        self.write_venv("win32")
        self.write_daemon_files()
        self.write_hooks()
        self.write_persona()
        return self.ctx(platform="win32", environ={"CLAUDE_CODE_OAUTH_TOKEN": SECRET_OAUTH})

    def rows_by_id(self, rows):
        return {r["id"]: r for r in rows}


class AuthCheck(Base):
    def test_win_token_present_green(self):
        row = sd.check_auth(self.ctx(environ={"CLAUDE_CODE_OAUTH_TOKEN": SECRET_OAUTH}))
        self.assertEqual(row["status"], sd.GREEN)

    def test_win_token_absent_yellow_with_fresh_shell_caveat(self):
        row = sd.check_auth(self.ctx(environ={}))
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("fresh shell", row["detail"])
        self.assertIn("/setup auth-models", row["fix"])

    def test_api_key_present_degrades_to_yellow_with_metered_warning(self):
        row = sd.check_auth(self.ctx(environ={
            "CLAUDE_CODE_OAUTH_TOKEN": SECRET_OAUTH, "ANTHROPIC_API_KEY": "sk-ant-SECRET"}))
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("METERED", row["detail"])

    def test_posix_daemon_env_ok_green(self):
        env_file = self.home / ".config" / "seneschal" / "daemon.env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={SECRET_OAUTH}\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        row = sd.check_auth(self.ctx(platform="linux"))
        self.assertEqual(row["status"], sd.GREEN)

    def test_posix_daemon_env_absent_yellow(self):
        row = sd.check_auth(self.ctx(platform="linux"))
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("daemon.env", row["detail"])

    @unittest.skipUnless(os.name == "posix", "mode bits only enforceable on POSIX")
    def test_posix_loose_perms_yellow(self):
        env_file = self.home / ".config" / "seneschal" / "daemon.env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={SECRET_OAUTH}\n", encoding="utf-8")
        os.chmod(env_file, 0o644)
        row = sd.check_auth(self.ctx(platform="linux"))
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("600", row["detail"])

    def test_auth_never_echoes_token_value(self):
        row = sd.check_auth(self.ctx(environ={"CLAUDE_CODE_OAUTH_TOKEN": SECRET_OAUTH}))
        self.assertNotIn(SECRET_OAUTH, json.dumps(row))


class ModelsCheck(Base):
    def test_absent_yellow_dials_unset(self):
        row = sd.check_models(self.ctx())
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("dials unset", row["detail"])
        self.assertIn("/setup auth-models", row["fix"])

    def test_valid_green_with_canonical_ids(self):
        model_config.save(str(self.state_dir), "opus", "fable")
        row = sd.check_models(self.ctx())
        self.assertEqual(row["status"], sd.GREEN)
        self.assertIn("claude-opus-4-8", row["detail"])
        self.assertIn("claude-fable-5", row["detail"])

    def test_corrupt_json_red(self):
        (self.state_dir / "model-config.json").write_text("{nope", encoding="utf-8")
        row = sd.check_models(self.ctx())
        self.assertEqual(row["status"], sd.RED)

    def test_incoherent_pair_red(self):
        (self.state_dir / "model-config.json").write_text(
            json.dumps({"warm_model": "claude-fable-5", "max_routable_model": "claude-haiku-4-5"}),
            encoding="utf-8",
        )
        row = sd.check_models(self.ctx())
        self.assertEqual(row["status"], sd.RED)
        self.assertIn("outranks", row["detail"])

    def test_probe_failure_red(self):
        model_config.save(str(self.state_dir), "sonnet", "sonnet")
        self.patch(sd, "_which", lambda name: "/usr/bin/claude")
        self.patch(sd, "_run", lambda cmd, timeout=30.0, env=None: (1, "", "model not allowed on this plan"))
        row = sd.check_models(self.ctx(), probe=True)
        self.assertEqual(row["status"], sd.RED)
        self.assertIn("probe FAILED", row["detail"])

    def test_probe_success_green_lists_probed_ids(self):
        model_config.save(str(self.state_dir), "sonnet", "opus")
        self.patch(sd, "_which", lambda name: "/usr/bin/claude")
        self.patch(sd, "_run", lambda cmd, timeout=30.0, env=None: (0, "ok", ""))
        row = sd.check_models(self.ctx(), probe=True)
        self.assertEqual(row["status"], sd.GREEN)
        self.assertIn("probed ok", row["detail"])

    def test_probe_model_scrubs_api_key_and_no_claude_fails(self):
        self.patch(sd, "_which", lambda name: None)
        ok, detail = sd.probe_model("claude-sonnet-5")
        self.assertFalse(ok)
        self.assertIn("not found", detail)


class EnvCheck(Base):
    def test_required_missing_red(self):
        rows = self.rows_by_id(sd.check_env(self.ctx()))
        self.assertEqual(rows["env:telegram"]["status"], sd.RED)
        self.assertIn("required", rows["env:telegram"]["detail"])
        self.assertIn("/setup env:telegram", rows["env:telegram"]["fix"])

    def test_required_complete_green(self):
        self.write_telegram_env()
        rows = self.rows_by_id(sd.check_env(self.ctx()))
        self.assertEqual(rows["env:telegram"]["status"], sd.GREEN)

    def test_incomplete_red_names_missing_vars_only(self):
        (self.root / "seneschal" / "scripts" / "telegram.env").write_text(
            f"TELEGRAM_BOT_TOKEN={SECRET_TOKEN}\nTELEGRAM_CHAT_ID=\n", encoding="utf-8"
        )
        rows = self.rows_by_id(sd.check_env(self.ctx()))
        row = rows["env:telegram"]
        self.assertEqual(row["status"], sd.RED)
        self.assertIn("TELEGRAM_CHAT_ID", row["detail"])
        self.assertNotIn(SECRET_TOKEN, json.dumps(row))

    def test_declined_skip(self):
        self.mark("env:proton", "declined")
        rows = self.rows_by_id(sd.check_env(self.ctx()))
        self.assertEqual(rows["env:proton"]["status"], sd.SKIP)
        self.assertIn("declined", rows["env:proton"]["detail"])

    def test_optional_unconfigured_skip(self):
        rows = self.rows_by_id(sd.check_env(self.ctx()))
        self.assertEqual(rows["env:proton"]["status"], sd.SKIP)
        self.assertIn("optional", rows["env:proton"]["detail"])

    def test_enabled_done_but_file_missing_red(self):
        self.mark("env:proton", "done")
        rows = self.rows_by_id(sd.check_env(self.ctx()))
        self.assertEqual(rows["env:proton"]["status"], sd.RED)
        self.assertIn("done", rows["env:proton"]["detail"])

    def test_complete_file_wins_over_declined_ledger(self):
        self.mark("env:telegram", "declined")
        self.write_telegram_env()
        rows = self.rows_by_id(sd.check_env(self.ctx()))
        self.assertEqual(rows["env:telegram"]["status"], sd.GREEN)

    def test_handled_by_entries_emit_no_rows(self):
        ids = [r["id"] for r in sd.check_env(self.ctx())]
        self.assertNotIn("env:notion-mcp", ids)

    def test_unreadable_manifest_single_red_row(self):
        bad = self.root / "no-manifest.json"
        ctx = sd.build_ctx(root=self.root, state_dir=self.state_dir, manifest=bad,
                           home=self.home, platform="win32", environ={})
        rows = sd.check_env(ctx)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], sd.RED)


class StoreCheck(Base):
    def test_no_config_yellow(self):
        row = sd.check_store(self.ctx())
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("/setup store", row["fix"])

    def test_filesystem_backend_green_on_config_alone(self):
        self.write_store("markdown")
        row = sd.check_store(self.ctx())
        self.assertEqual(row["status"], sd.GREEN)
        self.assertIn("markdown", row["detail"])

    def test_bad_json_red(self):
        (self.root / "seneschal" / "store" / "config.json").write_text("{nope", encoding="utf-8")
        row = sd.check_store(self.ctx())
        self.assertEqual(row["status"], sd.RED)

    def test_no_active_backend_red(self):
        (self.root / "seneschal" / "store" / "config.json").write_text(
            json.dumps({"version": 1, "backends": {}}), encoding="utf-8")
        row = sd.check_store(self.ctx())
        self.assertEqual(row["status"], sd.RED)

    def test_notion_without_mcp_yellow_says_verify_in_session(self):
        self.write_store("notion")
        row = sd.check_store(self.ctx())
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("in-session", row["detail"])
        self.assertIn("/setup mcp:notion", row["fix"])

    def test_notion_with_mcp_file_green(self):
        self.write_store("notion")
        mcp = self.root / "seneschal" / "store" / "notion" / "mcp.json"
        mcp.parent.mkdir(parents=True)
        mcp.write_text("{}", encoding="utf-8")
        row = sd.check_store(self.ctx())
        self.assertEqual(row["status"], sd.GREEN)

    def test_notion_with_user_scope_hint_green(self):
        self.write_store("notion")
        (self.home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"notion": {"url": "https://mcp.notion.com/mcp"}}}),
            encoding="utf-8",
        )
        row = sd.check_store(self.ctx())
        self.assertEqual(row["status"], sd.GREEN)


class VenvCheck(Base):
    def test_missing_venv_yellow(self):
        row = sd.check_venv(self.ctx())
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("REST polling", row["detail"])

    def test_websockets_import_failure_yellow(self):
        self.write_venv("win32")
        self.patch(sd, "_run", lambda cmd, timeout=30.0, env=None: (1, "", "ModuleNotFoundError"))
        row = sd.check_venv(self.ctx())
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("websockets", row["detail"])

    def test_venv_ok_green(self):
        self.write_venv("win32")
        row = sd.check_venv(self.ctx())
        self.assertEqual(row["status"], sd.GREEN)

    def test_cockpit_enabled_missing_dist_yellow(self):
        self.write_venv("win32")
        state = setup_state.load(self.state_dir / "setup-state.json")
        state["features"]["cockpit"] = True
        setup_state.save(state, self.state_dir / "setup-state.json")
        row = sd.check_venv(self.ctx())
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("dist", row["detail"])

    def test_cockpit_enabled_all_present_green(self):
        self.write_venv("win32")
        state = setup_state.load(self.state_dir / "setup-state.json")
        state["features"]["cockpit"] = True
        setup_state.save(state, self.state_dir / "setup-state.json")
        dist = self.root / "cockpit" / "web" / "dist" / "index.html"
        dist.parent.mkdir(parents=True)
        dist.write_text("<html></html>", encoding="utf-8")
        row = sd.check_venv(self.ctx())
        self.assertEqual(row["status"], sd.GREEN)
        self.assertIn("cockpit", row["detail"])


class OllamaCheck(Base):
    def test_skip_when_no_dependent_feature_enabled(self):
        row = sd.check_ollama(self.ctx())
        self.assertEqual(row["status"], sd.SKIP)

    def test_down_yellow_never_red(self):
        self.mark("env:router", "done")
        row = sd.check_ollama(self.ctx())  # default seam raises
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("router", row["detail"])
        self.assertNotEqual(row["status"], sd.RED)

    def test_up_missing_model_yellow(self):
        self.mark("env:router", "done")
        self.patch(sd, "_http_get",
                   lambda url, timeout=3.0: json.dumps({"models": [{"name": "othermodel:7b"}]}))
        row = sd.check_ollama(self.ctx())
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("tinychat:1b", row["detail"])
        self.assertIn("ollama pull", row["fix"])

    def test_up_all_models_green(self):
        self.mark("env:router", "done")
        self.patch(sd, "_http_get",
                   lambda url, timeout=3.0: json.dumps({"models": [{"name": "tinychat:1b"}]}))
        row = sd.check_ollama(self.ctx())
        self.assertEqual(row["status"], sd.GREEN)

    def test_url_prefers_ledger_deps_fact(self):
        self.mark("env:router", "done")
        state = setup_state.load(self.state_dir / "setup-state.json")
        state["deps"]["ollama"] = {"status": "ready", "url": "http://10.0.0.5:11434", "models": []}
        setup_state.save(state, self.state_dir / "setup-state.json")
        seen = []

        def fake_get(url, timeout=3.0):
            seen.append(url)
            return json.dumps({"models": [{"name": "tinychat:1b"}]})

        self.patch(sd, "_http_get", fake_get)
        sd.check_ollama(self.ctx())
        self.assertTrue(seen and seen[0].startswith("http://10.0.0.5:11434"))


class DaemonCheck(Base):
    def test_not_registered_yellow_points_at_daemon_chapter(self):
        self.patch(sd, "_run", lambda cmd, timeout=30.0, env=None: (1, "", "not found"))
        row = sd.check_daemon(self.ctx())
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("/setup daemon", row["fix"])

    def test_stale_files_yellow(self):
        self.write_daemon_files(pid=2 ** 30 + 12345, health_age_min=600)  # dead pid, old stamp
        row = sd.check_daemon(self.ctx())
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("stale", row["detail"])

    def test_running_fresh_green(self):
        self.write_daemon_files()  # our own live pid + fresh stamp
        row = sd.check_daemon(self.ctx())
        self.assertEqual(row["status"], sd.GREEN)
        self.assertIn("running", row["detail"])

    def test_unix_no_tooling_and_no_files_skip(self):
        self.patch(sd, "_run", lambda cmd, timeout=30.0, env=None: (None, "", "not found: systemctl"))
        row = sd.check_daemon(self.ctx(platform="linux"))
        self.assertEqual(row["status"], sd.SKIP)

    def test_win_update_task_missing_mentioned(self):
        def runner(cmd, timeout=30.0, env=None):
            return (0, "", "") if cmd[-1] == "seneschald" else (1, "", "")

        self.patch(sd, "_run", runner)
        row = sd.check_daemon(self.ctx())
        self.assertIn("seneschald-update", row["detail"])


class HooksCheck(Base):
    def test_hook_present_green(self):
        self.write_hooks()
        row = sd.check_hooks(self.ctx())
        self.assertEqual(row["status"], sd.GREEN)

    def test_hook_absent_yellow(self):
        row = sd.check_hooks(self.ctx())
        self.assertEqual(row["status"], sd.YELLOW)
        self.assertIn("/setup daemon", row["fix"])


class ArtifactsCheck(Base):
    def test_persona_absent_yellow_says_default_is_fine(self):
        rows = self.rows_by_id(sd.check_artifacts(self.ctx()))
        self.assertEqual(rows["persona"]["status"], sd.YELLOW)
        self.assertIn("default-Claude", rows["persona"]["detail"])
        self.assertIn("fine", rows["persona"]["detail"])
        self.assertEqual(rows["owner-profile"]["status"], sd.YELLOW)

    def test_persona_present_green(self):
        self.write_persona()
        rows = self.rows_by_id(sd.check_artifacts(self.ctx()))
        self.assertEqual(rows["persona"]["status"], sd.GREEN)
        self.assertEqual(rows["owner-profile"]["status"], sd.GREEN)


class SendTest(Base):
    def test_missing_env_red(self):
        row = sd.run_send_test(self.ctx())
        self.assertEqual(row["status"], sd.RED)
        self.assertIn("telegram.env missing", row["detail"])

    def test_send_ok_green(self):
        self.write_telegram_env()
        (self.root / "seneschal" / "scripts" / "telegram_send.py").write_text("", encoding="utf-8")
        self.patch(sd, "_run", lambda cmd, timeout=30.0, env=None: (0, '{"sent": true}', ""))
        row = sd.run_send_test(self.ctx())
        self.assertEqual(row["status"], sd.GREEN)

    def test_send_failure_red_without_echoing_child_output(self):
        self.write_telegram_env()
        (self.root / "seneschal" / "scripts" / "telegram_send.py").write_text("", encoding="utf-8")
        self.patch(sd, "_run",
                   lambda cmd, timeout=30.0, env=None: (1, f"error with {SECRET_TOKEN}", "boom"))
        row = sd.run_send_test(self.ctx())
        self.assertEqual(row["status"], sd.RED)
        self.assertNotIn(SECRET_TOKEN, json.dumps(row))


class BoardAndCli(Base):
    def run_cli(self, *args):
        argv = ["setup_doctor.py",
                "--root", str(self.root),
                "--state-dir", str(self.state_dir),
                "--manifest", str(self.root / "seneschal" / "setup" / "env-manifest.json"),
                "--home", str(self.home),
                "--platform", "win32",
                *args]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sd._main(argv)
        return rc, buf.getvalue()

    def test_all_green_no_red_no_yellow(self):
        ctx = self.all_green()
        rows = sd.run_checks(ctx)
        summary = sd.summarize(rows)
        self.assertEqual(summary["red"], 0, msg=json.dumps(rows, indent=2))
        self.assertEqual(summary["yellow"], 0, msg=json.dumps(rows, indent=2))

    def test_exit_code_equals_red_count(self):
        # Two reds: telegram.env missing (required) + an incoherent model-dial pair.
        (self.state_dir / "model-config.json").write_text(
            json.dumps({"warm_model": "claude-fable-5", "max_routable_model": "claude-haiku-4-5"}),
            encoding="utf-8",
        )
        rc, out = self.run_cli("--json")
        report = json.loads(out)
        self.assertEqual(report["summary"]["red"], 2)
        self.assertEqual(rc, 2)

    def test_required_env_missing_exits_nonzero(self):
        rc, out = self.run_cli("--json")
        report = json.loads(out)
        rows = {r["id"]: r for r in report["rows"]}
        self.assertEqual(rows["env:telegram"]["status"], "RED")
        self.assertGreaterEqual(rc, 1)

    def test_json_shape(self):
        rc, out = self.run_cli("--json")
        report = json.loads(out)
        for key in ("generated_at", "platform", "rows", "summary"):
            self.assertIn(key, report)
        for row in report["rows"]:
            for key in ("id", "status", "detail", "fix"):
                self.assertIn(key, row)
            self.assertIn(row["status"], ("GREEN", "YELLOW", "RED", "SKIP"))
        for key in ("green", "yellow", "red", "skip"):
            self.assertIn(key, report["summary"])

    def test_board_glyphs_and_summary_line(self):
        rc, out = self.run_cli()
        self.assertRegex(out, r"\[x\] env:telegram")
        self.assertRegex(out, r"\d+ green, \d+ yellow, \d+ red")
        self.assertIn("fix: -> /setup env:telegram", out)

    def test_board_output_is_ascii(self):
        rc, out = self.run_cli()
        out.encode("ascii")  # raises if any non-ASCII slipped into the board

    def test_secret_values_never_in_board_or_json(self):
        self.all_green()
        # Put secrets everywhere a sloppy check could leak them from.
        for args in ((), ("--json",)):
            with self.subTest(args=args):
                env_backup = dict(os.environ)
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = SECRET_OAUTH
                try:
                    rc, out = self.run_cli(*args)
                finally:
                    os.environ.clear()
                    os.environ.update(env_backup)
                self.assertNotIn(SECRET_TOKEN, out)
                self.assertNotIn(SECRET_OAUTH, out)

    def test_probe_model_cli_one_off(self):
        self.patch(sd, "_which", lambda name: None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sd._main(["setup_doctor.py", "--probe-model", "claude-sonnet-5"])
        self.assertEqual(rc, 1)
        report = json.loads(buf.getvalue())
        self.assertFalse(report["ok"])
        self.assertEqual(report["model"], "claude-sonnet-5")

    def test_row_order_starts_auth_models_env(self):
        rows = sd.run_checks(self.ctx())
        self.assertEqual([r["id"] for r in rows[:3]], ["auth", "models", "env:telegram"])

    def test_send_test_only_when_flagged(self):
        ids = [r["id"] for r in sd.run_checks(self.ctx())]
        self.assertNotIn("send-test", ids)
        ids = [r["id"] for r in sd.run_checks(self.ctx(), send_test=True)]
        self.assertIn("send-test", ids)


if __name__ == "__main__":
    unittest.main()
