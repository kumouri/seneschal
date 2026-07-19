#!/usr/bin/env python3
"""Tests for the cockpit FastAPI backend (v0 skeleton + v1 read-only monitor).

Requires the `cockpit` uv extras group (fastapi + uvicorn — see root pyproject.toml). SKIPPED, not
errored, when fastapi isn't installed, so a bare-machine run of the root stdlib suite
(`python -m unittest discover -s seneschal/scripts`) stays green — these tests also live outside
seneschal/scripts entirely, so that discovery command never even looks here.

Run:
  uv sync --extra cockpit
  uv run python -m unittest discover -s cockpit/server -p "test_*.py"
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_health_common():
    """Load seneschal/scripts/health_common.py by explicit file path (importlib, not sys.path) — that
    directory is full of generically-named test_*.py files that would collide with unittest's
    bare-module discovery if seneschal/scripts were ever added to sys.path. Stdlib-only, so this doesn't
    need the fastapi guard below — it's what builds the v4 tests' health.db fixtures with the SAME
    schema health_import.py writes. health_common imports its siblings at module top (tz_common →
    identity_common), so the scripts dir joins sys.path ONLY for the duration of this exec, and the
    transiently imported siblings are dropped from sys.modules afterward — discovery never sees
    either, and the cockpit modules' own guarded `import tz_common` stays deterministic."""
    import importlib.util

    scripts_dir = REPO_ROOT / "seneschal" / "scripts"
    path = scripts_dir / "health_common.py"
    spec = importlib.util.spec_from_file_location("seneschal_scripts_health_common", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(scripts_dir))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(scripts_dir))
        sys.modules.pop("tz_common", None)
        sys.modules.pop("identity_common", None)
    return module


hc = _load_health_common()

try:
    from fastapi.testclient import TestClient

    from cockpit.server.app import app

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False


@unittest.skipUnless(_FASTAPI_AVAILABLE, "fastapi/uvicorn not installed (uv sync --extra cockpit)")
class CockpitAppTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        (self.state_dir / "sessions").mkdir(parents=True, exist_ok=True)

        self._old_state = os.environ.get("SENESCHAL_STATE_DIR")
        self._old_auth = os.environ.get("COCKPIT_DEV_NO_AUTH")
        os.environ["SENESCHAL_STATE_DIR"] = str(self.state_dir)
        os.environ.pop("COCKPIT_DEV_NO_AUTH", None)

        self.client = TestClient(app)

    def tearDown(self):
        self._tmp.cleanup()
        if self._old_state is None:
            os.environ.pop("SENESCHAL_STATE_DIR", None)
        else:
            os.environ["SENESCHAL_STATE_DIR"] = self._old_state
        if self._old_auth is None:
            os.environ.pop("COCKPIT_DEV_NO_AUTH", None)
        else:
            os.environ["COCKPIT_DEV_NO_AUTH"] = self._old_auth

    def _allow(self):
        os.environ["COCKPIT_DEV_NO_AUTH"] = "1"

    def _write_json(self, name: str, data) -> None:
        with open(self.state_dir / name, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _write_session(self, filename: str, **fields) -> None:
        with open(self.state_dir / "sessions" / filename, "w", encoding="utf-8") as fh:
            json.dump(fields, fh)

    # --- health: public, no auth needed ---------------------------------------------------------

    def test_health_is_public_and_echoes_config(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["state_dir"], str(self.state_dir))
        self.assertTrue(body["state_dir_exists"])
        self.assertFalse(body["dev_no_auth"])

    # --- auth seam -------------------------------------------------------------------------------

    def test_gated_route_503s_without_dev_auth(self):
        resp = self.client.get("/api/sessions")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["detail"], "auth not configured")

    def test_gated_route_allows_with_dev_auth(self):
        self._allow()
        resp = self.client.get("/api/sessions")
        self.assertEqual(resp.status_code, 200)

    def test_localhost_only_middleware_allows_testclients_sentinel_host(self):
        # Starlette's TestClient always reports its synthetic client as ("testclient", ...); this is
        # exactly why app.py allow-lists that host alongside the real loopback addresses — every
        # other test in this file is implicitly exercising the pass-through path of that middleware.
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)

    # --- sessions --------------------------------------------------------------------------------

    def test_sessions_empty_dir(self):
        self._allow()
        resp = self.client.get("/api/sessions")
        self.assertEqual(resp.json(), {"sessions": [], "count": 0})

    def test_sessions_live_vs_stale(self):
        self._allow()
        now = datetime.now(timezone.utc)
        self._write_session(
            "daemon.json", source="daemon", last_seen=now.isoformat(), working_on="warm chat"
        )
        stale = (now - timedelta(seconds=999)).isoformat()
        self._write_session(
            "desktop.json", source="desktop", last_seen=stale, working_on="old /assistant chat"
        )
        resp = self.client.get("/api/sessions")
        self.assertEqual(resp.status_code, 200)
        by_file = {s["file"]: s for s in resp.json()["sessions"]}
        self.assertTrue(by_file["daemon.json"]["live"])
        self.assertFalse(by_file["desktop.json"]["live"])
        self.assertTrue(by_file["daemon.json"]["gates_delivery"])

    def test_sessions_build_source_uses_awareness_ttl(self):
        self._allow()
        # 10 minutes old: stale for a gating source (120s TTL) but still "live" for an awareness-only
        # build/scheduled source (3600s TTL) — it should show up as live under the wider window.
        ten_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self._write_session("build-1.json", source="build", last_seen=ten_min_ago, working_on="ci run")
        resp = self.client.get("/api/sessions")
        entry = resp.json()["sessions"][0]
        self.assertFalse(entry["gates_delivery"])
        self.assertTrue(entry["live"])

    def test_sessions_tolerates_garbage_file(self):
        self._allow()
        (self.state_dir / "sessions" / "broken.json").write_text("not json", encoding="utf-8")
        resp = self.client.get("/api/sessions")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["sessions"], [])

    def test_sessions_dir_missing_entirely(self):
        self._allow()
        import shutil

        shutil.rmtree(self.state_dir / "sessions")
        resp = self.client.get("/api/sessions")
        self.assertEqual(resp.json(), {"sessions": [], "count": 0})

    # --- oneiroi ---------------------------------------------------------------------------------

    def test_oneiroi_tail_newest_first_and_limit(self):
        self._allow()
        path = self.state_dir / "session-distillations.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(5):
                fh.write(json.dumps({"id": i, "title": f"session {i}"}) + "\n")
            fh.write("not json\n")
        resp = self.client.get("/api/oneiroi?limit=2")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual([r["id"] for r in body["oneiroi"]], [4, 3])

    def test_oneiroi_default_limit_is_20(self):
        self._allow()
        path = self.state_dir / "session-distillations.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(30):
                fh.write(json.dumps({"id": i}) + "\n")
        resp = self.client.get("/api/oneiroi")
        self.assertEqual(resp.json()["count"], 20)

    def test_oneiroi_missing_file(self):
        self._allow()
        resp = self.client.get("/api/oneiroi")
        self.assertEqual(resp.json(), {"oneiroi": [], "count": 0})

    # --- seneschald-health -----------------------------------------------------------------------------

    def test_seneschald_health_passthrough_with_age(self):
        self._allow()
        last_ok = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._write_json("seneschald-health.json", {"status": "ok", "last_ok": last_ok, "branch": "develop"})
        resp = self.client.get("/api/seneschald-health")
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["branch"], "develop")
        self.assertIn("last_ok_age_seconds", body)
        self.assertGreaterEqual(body["last_ok_age_seconds"], 0)

    def test_seneschald_health_missing(self):
        self._allow()
        resp = self.client.get("/api/seneschald-health")
        self.assertEqual(resp.json(), {"available": False})

    # --- presence ----------------------------------------------------------------------------------

    def test_presence_passthrough(self):
        self._allow()
        self._write_json(
            "presence-context.json", {"at_place": "home", "activity": "still", "asleep": False}
        )
        resp = self.client.get("/api/presence")
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["at_place"], "home")

    def test_presence_missing(self):
        self._allow()
        resp = self.client.get("/api/presence")
        self.assertEqual(resp.json(), {"available": False})

    # --- reminders ---------------------------------------------------------------------------------

    def test_reminders_summary(self):
        self._allow()
        self._write_json(
            "reminders.json",
            [
                {"id": "a", "text": "one", "due_at": "2026-07-20T00:00:00Z", "fired_at": None},
                {"id": "b", "text": "two", "due_at": "2026-07-19T00:00:00Z", "fired_at": None},
                {
                    "id": "c",
                    "text": "done already",
                    "due_at": "2026-07-01T00:00:00Z",
                    "fired_at": "2026-07-01T00:00:01Z",
                },
            ],
        )
        resp = self.client.get("/api/reminders")
        body = resp.json()
        self.assertEqual(body["pending_count"], 2)
        self.assertEqual(body["total_count"], 3)
        self.assertEqual(body["next"][0]["id"], "b")  # earlier due_at sorts first

    def test_reminders_missing_file(self):
        self._allow()
        resp = self.client.get("/api/reminders")
        self.assertEqual(resp.json(), {"pending_count": 0, "total_count": 0, "next": []})

    def test_reminders_tolerates_non_list_garbage(self):
        self._allow()
        self._write_json("reminders.json", {"not": "a list"})
        resp = self.client.get("/api/reminders")
        self.assertEqual(resp.json(), {"pending_count": 0, "total_count": 0, "next": []})

    # --- usage -------------------------------------------------------------------------------------

    def test_usage_aggregation_by_day_and_model(self):
        self._allow()
        path = self.state_dir / "metrics.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": "2026-07-06T13:31:00Z", "model": "opus"}) + "\n")
            fh.write(json.dumps({"ts": "2026-07-06T18:30:00Z", "model": "haiku"}) + "\n")
            fh.write("garbage\n")
        resp = self.client.get("/api/usage")
        body = resp.json()
        self.assertTrue(body["estimated"])
        self.assertFalse(body["tokens_available"])
        self.assertEqual(body["totals"]["turns"], 2)
        self.assertEqual(len(body["days"]), 1)
        self.assertEqual(body["days"][0]["by_model"], {"opus": 1, "haiku": 1})

    def test_usage_extracts_tokens_when_present(self):
        self._allow()
        path = self.state_dir / "metrics.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": "2026-07-06T13:31:00Z", "model": "opus", "tokens": 500}) + "\n")
        resp = self.client.get("/api/usage")
        body = resp.json()
        self.assertTrue(body["tokens_available"])
        self.assertEqual(body["totals"]["tokens"], 500)

    def test_usage_missing_file(self):
        self._allow()
        resp = self.client.get("/api/usage")
        body = resp.json()
        self.assertEqual(body["totals"], {"turns": 0, "tokens": 0})
        self.assertEqual(body["days"], [])

    # --- status ------------------------------------------------------------------------------------

    def test_status_no_daemon_entry(self):
        self._allow()
        resp = self.client.get("/api/status")
        self.assertFalse(resp.json()["available"])

    def test_status_with_daemon_entry(self):
        self._allow()
        now = datetime.now(timezone.utc)
        self._write_session(
            "daemon.json", source="daemon", last_seen=now.isoformat(),
            working_on="warm chat with the owner", phase="active",
        )
        resp = self.client.get("/api/status")
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["working_on"], "warm chat with the owner")
        self.assertEqual(body["phase"], "active")
        self.assertTrue(body["live"])

    # --- control/restart -----------------------------------------------------------------------------

    def test_restart_requires_auth(self):
        resp = self.client.post("/api/control/restart")
        self.assertEqual(resp.status_code, 503)

    def test_restart_enqueues_and_audits(self):
        self._allow()
        resp = self.client.post("/api/control/restart")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["already_queued"])

        queue = json.loads((self.state_dir / "control-queue.json").read_text(encoding="utf-8"))
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["action"], "restart")
        self.assertTrue(queue[0]["defer_until_idle"])
        self.assertIn("requested_at", queue[0])

        audit_lines = (
            (self.state_dir / "cockpit-audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
        )
        self.assertEqual(len(audit_lines), 1)
        audit = json.loads(audit_lines[0])
        self.assertEqual(audit["action"], "control.restart")
        self.assertIn("ts", audit)

    def test_restart_dedupes_pending_action_but_still_audits_the_call(self):
        self._allow()
        self.client.post("/api/control/restart")
        resp = self.client.post("/api/control/restart")
        body = resp.json()
        self.assertTrue(body["already_queued"])

        queue = json.loads((self.state_dir / "control-queue.json").read_text(encoding="utf-8"))
        self.assertEqual(len(queue), 1)  # still just the one queued restart

        audit_lines = (
            (self.state_dir / "cockpit-audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
        )
        self.assertEqual(len(audit_lines), 2)  # every mutating CALL is audited, dedupe or not

    # --- transcript (v2 backfill) ------------------------------------------------------------------

    def test_transcript_backfill_tail(self):
        self._allow()
        path = self.state_dir / "warm-transcript.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(5):
                fh.write(json.dumps({"type": "chat.event", "kind": "turn_done", "i": i}) + "\n")
        resp = self.client.get("/api/transcript?limit=2")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([e["i"] for e in resp.json()["events"]], [3, 4])

    def test_transcript_missing_file(self):
        self._allow()
        resp = self.client.get("/api/transcript")
        self.assertEqual(resp.json(), {"events": []})

    def test_transcript_requires_auth(self):
        resp = self.client.get("/api/transcript")
        self.assertEqual(resp.status_code, 503)

    # --- emotes (v2) -------------------------------------------------------------------------------

    def test_emotes_off_by_default(self):
        self._allow()
        resp = self.client.get("/api/emotes")
        self.assertEqual(resp.json(), {"emotes": []})

    def test_emotes_lists_and_serves_configured_directory(self):
        self._allow()
        emote_dir = Path(tempfile.mkdtemp())
        (emote_dir / "pog.png").write_bytes(b"fake-png-bytes")
        old = os.environ.get("COCKPIT_EMOTE_DIR")
        os.environ["COCKPIT_EMOTE_DIR"] = str(emote_dir)
        try:
            resp = self.client.get("/api/emotes")
            self.assertEqual(resp.json(), {"emotes": [{"shortcode": "pog", "file": "pog.png"}]})
            file_resp = self.client.get("/api/emotes/pog.png")
            self.assertEqual(file_resp.status_code, 200)
            self.assertEqual(file_resp.content, b"fake-png-bytes")
        finally:
            if old is None:
                os.environ.pop("COCKPIT_EMOTE_DIR", None)
            else:
                os.environ["COCKPIT_EMOTE_DIR"] = old

    def test_emote_file_404_for_unknown(self):
        self._allow()
        resp = self.client.get("/api/emotes/nope.png")
        self.assertEqual(resp.status_code, 404)

    # --- model-config (v3) -------------------------------------------------------------------------

    def test_model_config_requires_auth(self):
        resp = self.client.get("/api/model-config")
        self.assertEqual(resp.status_code, 503)

    def test_model_config_get_tolerant_when_missing(self):
        self._allow()
        resp = self.client.get("/api/model-config")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"warm_model": None, "max_routable_model": None, "updated_at": None})

    def test_model_config_put_writes_validated_and_audits(self):
        self._allow()
        resp = self.client.put("/api/model-config",
                              json={"warm_model": "opus", "max_routable_model": "fable"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["warm_model"], "claude-opus-4-8")
        self.assertEqual(body["max_routable_model"], "claude-fable-5")
        self.assertIn("updated_at", body)

        get_resp = self.client.get("/api/model-config")
        self.assertEqual(get_resp.json()["warm_model"], "claude-opus-4-8")

        audit_lines = (
            (self.state_dir / "cockpit-audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
        )
        self.assertEqual(len(audit_lines), 1)
        audit = json.loads(audit_lines[0])
        self.assertEqual(audit["action"], "model_config.update")
        self.assertEqual(audit["detail"]["warm_model"], "claude-opus-4-8")

    def test_model_config_put_rejects_incoherent_pair(self):
        self._allow()
        resp = self.client.put("/api/model-config",
                              json={"warm_model": "fable", "max_routable_model": "sonnet"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("outranks", resp.json()["detail"])
        # a rejected write must not land, and must not be audited as a success
        self.assertFalse((self.state_dir / "model-config.json").exists())

    def test_model_config_put_rejects_unrecognized_id(self):
        self._allow()
        resp = self.client.put("/api/model-config",
                              json={"warm_model": "gpt-5", "max_routable_model": "fable"})
        self.assertEqual(resp.status_code, 400)

    def test_model_config_put_requires_auth(self):
        resp = self.client.put("/api/model-config",
                              json={"warm_model": "opus", "max_routable_model": "fable"})
        self.assertEqual(resp.status_code, 503)

    def test_model_config_put_missing_field_is_422(self):
        self._allow()
        resp = self.client.put("/api/model-config", json={"warm_model": "opus"})
        self.assertEqual(resp.status_code, 422)  # pydantic validation, not our ValueError path

    # --- router-stats (v3) -------------------------------------------------------------------------

    def test_router_stats_requires_auth(self):
        resp = self.client.get("/api/router-stats")
        self.assertEqual(resp.status_code, 503)

    def test_router_stats_empty_when_missing(self):
        self._allow()
        resp = self.client.get("/api/router-stats")
        self.assertEqual(resp.json(), {"counts": {}, "recent": [], "total": 0})

    def test_router_stats_counts_by_arm_and_verdict(self):
        self._allow()
        path = self.state_dir / "router-log.jsonl"
        rows = [
            {"ts": "2026-07-17T00:00:00Z", "channel": "telegram", "text_preview": "took my meds",
             "arm": "triage", "verdict": "trivial", "category": "ack", "confidence": 0.9, "model": "m"},
            {"ts": "2026-07-17T00:01:00Z", "channel": "telegram", "text_preview": "draft an email",
             "arm": "triage", "verdict": "escalate", "category": "other", "confidence": 0.0, "model": "m"},
            {"ts": "2026-07-17T00:02:00Z", "channel": "telegram", "text_preview": "help me plan a migration",
             "arm": "fable", "verdict": "fable", "confidence": 0.85, "reason": "long-horizon", "model": "m"},
            # legacy row written before the `arm` field existed — must bucket under "triage"
            {"ts": "2026-07-17T00:03:00Z", "channel": "discord", "text_preview": "what's next?",
             "verdict": "trivial", "category": "status", "confidence": 0.9, "model": "m"},
        ]
        with open(path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
            fh.write("not json\n")
        resp = self.client.get("/api/router-stats")
        body = resp.json()
        self.assertEqual(body["total"], 4)
        self.assertEqual(body["counts"]["triage"], {"trivial": 2, "escalate": 1})
        self.assertEqual(body["counts"]["fable"], {"fable": 1})
        # newest first — the legacy (no "arm" field) row was written LAST, so it's recent[0]
        self.assertEqual(body["recent"][0]["arm"], "triage")
        self.assertEqual(body["recent"][0]["ts"], "2026-07-17T00:03:00Z")
        self.assertEqual(body["recent"][-1]["ts"], "2026-07-17T00:00:00Z")
        self.assertIn("fable", [r["arm"] for r in body["recent"]])

    def test_router_stats_respects_limit(self):
        self._allow()
        path = self.state_dir / "router-log.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(10):
                fh.write(json.dumps({"ts": f"t{i}", "arm": "triage", "verdict": "trivial"}) + "\n")
        resp = self.client.get("/api/router-stats?limit=3")
        body = resp.json()
        self.assertEqual(len(body["recent"]), 3)
        self.assertEqual(body["total"], 10)  # limit only trims `recent`, not the totals

    # --- governor-config (v3.5, Oikonomos) ----------------------------------------------------------

    def test_governor_config_requires_auth(self):
        resp = self.client.get("/api/governor-config")
        self.assertEqual(resp.status_code, 503)

    def test_governor_config_get_tolerant_when_missing_returns_defaults_schema_and_rollups(self):
        self._allow()
        resp = self.client.get("/api/governor-config")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["config"]["fable_oneshots_per_day"], 5)
        self.assertIn("fable_oneshots_per_day", body["schema"])
        self.assertEqual(body["schema"]["fable_oneshots_per_day"]["kind"], "rail")
        self.assertIn("day", body["rollups"])
        self.assertEqual(body["rollups"]["fable_oneshots"], {"day": 0, "week": 0})

    def test_governor_config_put_partial_update_writes_validated_and_audits(self):
        self._allow()
        resp = self.client.put("/api/governor-config", json={"updates": {"fable_oneshots_per_day": 9}})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["config"]["fable_oneshots_per_day"], 9)
        # untouched knobs keep their defaults
        self.assertEqual(body["config"]["fable_concurrency_max"], 1)

        get_resp = self.client.get("/api/governor-config")
        self.assertEqual(get_resp.json()["config"]["fable_oneshots_per_day"], 9)

        audit_lines = (
            (self.state_dir / "cockpit-audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
        )
        self.assertEqual(len(audit_lines), 1)
        audit = json.loads(audit_lines[0])
        self.assertEqual(audit["action"], "governor_config.update")
        self.assertEqual(audit["detail"]["updates"], {"fable_oneshots_per_day": 9})

    def test_governor_config_put_rejects_unknown_knob(self):
        self._allow()
        resp = self.client.put("/api/governor-config", json={"updates": {"not_a_real_knob": 1}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("unknown knob", resp.json()["detail"])
        self.assertFalse((self.state_dir / "governor-config.json").exists())

    def test_governor_config_put_rejects_out_of_range_value(self):
        self._allow()
        resp = self.client.put("/api/governor-config", json={"updates": {"fable_concurrency_max": 0}})
        self.assertEqual(resp.status_code, 400)

    def test_governor_config_put_requires_auth(self):
        resp = self.client.put("/api/governor-config", json={"updates": {"fable_oneshots_per_day": 1}})
        self.assertEqual(resp.status_code, 503)

    def test_governor_config_rollups_reflect_the_ledger(self):
        self._allow()
        path = self.state_dir / "governor-ledger.jsonl"
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now_iso, "kind": "tokens", "model": "claude-opus-4-8",
                                 "tokens": 250}) + "\n")
        resp = self.client.get("/api/governor-config")
        body = resp.json()
        self.assertEqual(body["rollups"]["tokens_by_model"]["day"].get("claude-opus-4-8"), 250)

    # --- health / workout / meal panels (v4) --------------------------------------------------------
    # Fixtures use health_common.connect() so these exercise the REAL health_import.py schema, not a
    # hand-rolled guess. Deeper coverage of grouping/rollup/tolerance logic lives in test_health.py —
    # these just prove the routes are wired, gated, and return that module's shapes end-to-end.

    def _health_db(self):
        from cockpit.server import health as health_mod
        from cockpit.server.test_health import _LIVE_FEED_DDL

        conn = hc.connect(str(self.state_dir / health_mod.DB_FILE))
        # The live-feed workouts/nutrition tables aren't in health_common.SCHEMA in this repo —
        # see test_health._LIVE_FEED_DDL's note; the fixture shape is shared from there.
        conn.executescript(_LIVE_FEED_DDL)
        return conn

    def test_health_sleep_requires_auth(self):
        resp = self.client.get("/api/health/sleep")
        self.assertEqual(resp.status_code, 503)

    def test_health_sleep_missing_db_is_tolerant(self):
        self._allow()
        resp = self.client.get("/api/health/sleep")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"available": False, "nights": [], "count": 0})

    def test_health_sleep_returns_grouped_nights(self):
        self._allow()
        conn = self._health_db()
        conn.execute(
            "INSERT INTO sleep_session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("s1", "2026-07-15", "2026-07-15T02:00:00", "2026-07-15T10:00:00", -300,
             "2026-07-15T02:00:00", "2026-07-15T10:00:00", 480.0, 92.0, 88.0,
             None, None, None, None, None, None, None),
        )
        conn.commit()
        conn.close()
        resp = self.client.get("/api/health/sleep?days=5")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["nights"][0]["night"], "2026-07-15")
        self.assertEqual(body["nights"][0]["total_duration_min"], 480.0)

    def test_health_workouts_requires_auth(self):
        resp = self.client.get("/api/health/workouts")
        self.assertEqual(resp.status_code, 503)

    def test_health_workouts_missing_db_is_tolerant(self):
        self._allow()
        resp = self.client.get("/api/health/workouts")
        self.assertEqual(resp.json(), {"available": False, "sessions": [], "weekly": []})

    def test_health_workouts_weekly_rollup(self):
        self._allow()
        from cockpit.server import health as health_mod

        today = health_mod.local_today().isoformat()
        conn = self._health_db()
        conn.execute(
            "INSERT INTO workouts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("w1", f"{today}T07:00:00", f"{today}T07:30:00", -300, f"{today}T07:00:00",
             f"{today}T07:30:00", today, 30.0, "running", "Morning run", None, 300.0, 5000.0),
        )
        conn.commit()
        conn.close()
        resp = self.client.get("/api/health/workouts")
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertEqual(len(body["sessions"]), 1)
        self.assertEqual(body["weekly"][0]["count"], 1)

    def test_health_nutrition_requires_auth(self):
        resp = self.client.get("/api/health/nutrition")
        self.assertEqual(resp.status_code, 503)

    def test_health_nutrition_groups_by_day(self):
        self._allow()
        from cockpit.server import health as health_mod

        today = health_mod.local_today().isoformat()
        conn = self._health_db()
        conn.execute(
            "INSERT INTO nutrition VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("n1", f"{today}T12:00:00", None, -300, today, "lunch", "Chicken salad", 540.0, 42.0, 30.0, 18.0),
        )
        conn.commit()
        conn.close()
        resp = self.client.get("/api/health/nutrition")
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["days"][0]["total_kcal"], 540.0)
        self.assertEqual(len(body["days"][0]["entries"]), 1)

    def test_health_summary_requires_auth(self):
        resp = self.client.get("/api/health/summary")
        self.assertEqual(resp.status_code, 503)

    def test_health_summary_empty_when_no_db(self):
        self._allow()
        resp = self.client.get("/api/health/summary")
        body = resp.json()
        self.assertFalse(body["available"])
        self.assertIsNone(body["sleep"]["last_night"])
        self.assertIsNone(body["workouts"]["this_week"])
        self.assertIsNone(body["nutrition"]["today"])

    def test_meals_requires_auth(self):
        resp = self.client.get("/api/meals")
        self.assertEqual(resp.status_code, 503)

    def test_meals_missing_file_is_tolerant(self):
        self._allow()
        resp = self.client.get("/api/meals")
        self.assertEqual(resp.json(), {"available": False, "staged_at": None, "plans": []})

    def test_meals_happy_path(self):
        self._allow()
        self._write_json("meals.json", {
            "staged_at": "2026-07-17T22:00:00Z",
            "plans": [{"title": "Meal prep week", "url": "https://notion.so/abc",
                       "summary": "protein-forward", "tags": ["batch"]}],
        })
        resp = self.client.get("/api/meals")
        body = resp.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["staged_at"], "2026-07-17T22:00:00Z")
        self.assertEqual(body["plans"][0]["title"], "Meal prep week")

    def test_meals_corrupt_file_is_tolerant(self):
        self._allow()
        (self.state_dir / "meals.json").write_text("not json", encoding="utf-8")
        resp = self.client.get("/api/meals")
        self.assertEqual(resp.json(), {"available": False, "staged_at": None, "plans": []})

    # --- v5: GET /api/auth/status (public — no auth gate) -----------------------------------------

    def test_auth_status_unconfigured(self):
        resp = self.client.get("/api/auth/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"mode": "unconfigured", "authenticated": False, "user": None})

    def test_auth_status_dev_mode(self):
        self._allow()
        resp = self.client.get("/api/auth/status")
        self.assertEqual(resp.json(), {"mode": "dev", "authenticated": True, "user": None})

    def test_auth_status_oidc_configured_wins_over_dev(self):
        self._allow()
        os.environ["COCKPIT_OIDC_CLIENT_ID"] = "some-client"
        try:
            body = self.client.get("/api/auth/status").json()
            self.assertEqual(body["mode"], "oidc")
            self.assertFalse(body["authenticated"])
        finally:
            os.environ.pop("COCKPIT_OIDC_CLIENT_ID", None)

    def test_root_serves_or_404s_when_not_oidc_mode(self):
        # Outside "oidc" mode, "/" either serves the built frontend (when this checkout happens to
        # carry a cockpit/web/dist — e.g. a dev box that ran `npm run build`) or falls through to its
        # own honest 404. The assertion that matters is the same either way: NEVER the auth redirect.
        resp = self.client.get("/", follow_redirects=False)
        self.assertIn(resp.status_code, (200, 404))

    # --- v5: CSRF on mutating routes (no-op outside oidc mode) ------------------------------------

    def test_csrf_not_required_in_dev_mode(self):
        self._allow()
        resp = self.client.post("/api/control/restart")  # no CSRF header at all
        self.assertEqual(resp.status_code, 200)

    # --- archon tiles + proxy ----------------------------------------------------------------------

    def _registry_env(self, path):
        """Point COCKPIT_ARCHON_REGISTRY_PATH at `path` for one test; returns a restore callable."""
        old = os.environ.get("COCKPIT_ARCHON_REGISTRY_PATH")
        os.environ["COCKPIT_ARCHON_REGISTRY_PATH"] = str(path)

        def restore():
            if old is None:
                os.environ.pop("COCKPIT_ARCHON_REGISTRY_PATH", None)
            else:
                os.environ["COCKPIT_ARCHON_REGISTRY_PATH"] = old

        return restore

    def test_archons_requires_auth(self):
        resp = self.client.get("/api/archons")
        self.assertEqual(resp.status_code, 503)

    def test_archons_absent_registry_is_tolerant(self):
        # The real archon-registry.json is per-install (gitignored) — a fresh checkout has none, and
        # the roster must read honestly empty rather than erroring.
        self._allow()
        restore = self._registry_env(self.state_dir / "no-such-registry.json")
        try:
            resp = self.client.get("/api/archons")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"archons": []})
        finally:
            restore()

    def test_archons_tracked_example_registry_parses(self):
        # The tracked seed (archon-registry.example.json) must stay loadable — it's what installs
        # copy to the real (gitignored) archon-registry.json.
        self._allow()
        example = Path(__file__).resolve().parent / "archon-registry.example.json"
        restore = self._registry_env(example)
        try:
            resp = self.client.get("/api/archons")
            self.assertEqual(resp.status_code, 200)
            archons = resp.json()["archons"]
            self.assertEqual([a["id"] for a in archons], ["example-archon"])
            self.assertEqual(archons[0]["status"], "reserved")
            self.assertIsNone(archons[0]["reachable"])  # reserved entries are never probed
        finally:
            restore()

    def test_archon_proxy_requires_auth(self):
        resp = self.client.get("/archons/some-archon/index.html")
        self.assertEqual(resp.status_code, 503)

    def test_archon_proxy_unknown_id_404(self):
        self._allow()
        restore = self._registry_env(self.state_dir / "no-such-registry.json")
        try:
            resp = self.client.get("/archons/nope/index.html")
            self.assertEqual(resp.status_code, 404)
        finally:
            restore()

    def test_archon_proxy_reserved_archon_503(self):
        self._allow()
        registry_path = self.state_dir / "archon-registry.json"
        with open(registry_path, "w", encoding="utf-8") as fh:
            json.dump({"resting-archon": {"title": "Resting", "status": "reserved"}}, fh)
        restore = self._registry_env(registry_path)
        try:
            resp = self.client.get("/archons/resting-archon/anything")
            self.assertEqual(resp.status_code, 503)
        finally:
            restore()

    def test_archon_proxy_write_methods_405(self):
        self._allow()
        for method in ("post", "put", "delete", "patch"):
            resp = getattr(self.client, method)("/archons/some-archon/index.html")
            self.assertEqual(resp.status_code, 405, method)
            self.assertIn("GET-only", resp.json()["detail"])

    def test_archon_proxy_streams_a_live_upstream(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _FakeArchon(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                body = b"<html>example archon site</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), _FakeArchon)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self._allow()
            registry_path = self.state_dir / "archon-registry.json"
            with open(registry_path, "w", encoding="utf-8") as fh:
                json.dump({"example-archon": {"title": "Example", "port": port, "status": "live"}}, fh)
            restore = self._registry_env(registry_path)
            try:
                resp = self.client.get("/archons/example-archon/")
                self.assertEqual(resp.status_code, 200)
                self.assertIn("example archon site", resp.text)
            finally:
                restore()
        finally:
            server.shutdown()
            server.server_close()

    # --- /api/ws auth gate (unconfigured mode; oidc-mode gating lands with the deferred auth PR) ---

    def test_ws_closes_when_auth_unconfigured(self):
        os.environ.pop("COCKPIT_DEV_NO_AUTH", None)
        with self.assertRaises(Exception):
            with self.client.websocket_connect("/api/ws") as ws:
                ws.receive_json()

if __name__ == "__main__":
    unittest.main()
