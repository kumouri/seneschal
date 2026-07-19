#!/usr/bin/env python3
"""Tests for the session registry (phase 2 of live-session awareness) — the state helpers, the two
daemon chokepoints (reminder-fire defer + Watch-peek skip), and the machine-wide stamp hook.

Stdlib ``unittest`` only (matches the Markdown-skill core; CI byte-compiles Python and this runs green
under ``python -m unittest``). Covers the phase-1 guarantees (2026-07-14): while an interactive
``/assistant`` session is live, the daemon **defers** (holds, never drops) non-piercing due nudges and skips
the redundant comms peek; ``Call Me`` + Critical-and-above still pierce; missing/malformed/stale state
is fail-open (reads *not live*, so it can never block a reminder), and it composes with the quiet gate.
Plus the phase-2 registry (2026-07-15): multiple concurrent entries under ``state/sessions/``,
``working_on`` carry-forward, ``build`` sessions visible-but-non-gating, the legacy single-file
fallback, crash-orphan pruning, and ``session_stamp.py``'s hook-event handling.

Run:  python -m unittest seneschal.scripts.test_session_registry   (or)   python test_session_registry.py
"""
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import sentinel as sn  # noqa: E402
import presence as pr  # noqa: E402
import session_stamp as st  # noqa: E402

NOW = datetime(2026, 7, 14, 20, 0, 0, tzinfo=timezone.utc)  # a fixed instant; tests inject it


def _z(dt):
    return dt.isoformat().replace("+00:00", "Z")


class HeartbeatStateUnit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_absent_is_not_live(self):
        self.assertFalse(sn.session_is_live(self.dir, NOW))
        self.assertIsNone(sn.load_session(self.dir))

    def test_write_creates_then_preserves_started_at_on_refresh(self):
        first = sn.write_session_heartbeat(self.dir, "daemon", pid=999, now=NOW)
        self.assertEqual(first["source"], "daemon")
        self.assertEqual(first["pid"], 999)
        self.assertEqual(first["started_at"], first["last_seen"])  # brand new
        # Refresh 30 s later, SAME pid + source: started_at is preserved, last_seen advances.
        second = sn.write_session_heartbeat(self.dir, "daemon", pid=999, now=NOW + timedelta(seconds=30))
        self.assertEqual(second["started_at"], first["started_at"])   # preserved
        self.assertNotEqual(second["last_seen"], first["last_seen"])   # advanced

    def test_new_session_resets_started_at(self):
        first = sn.write_session_heartbeat(self.dir, "daemon", pid=1, now=NOW)
        # A different pid (or source) is a new session — started_at starts fresh.
        second = sn.write_session_heartbeat(self.dir, "daemon", pid=2, now=NOW + timedelta(seconds=30))
        self.assertNotEqual(second["started_at"], first["started_at"])
        self.assertEqual(second["started_at"], second["last_seen"])
        third = sn.write_session_heartbeat(self.dir, "desktop", pid=2, now=NOW + timedelta(seconds=60))
        self.assertNotEqual(third["started_at"], second["started_at"])  # source change = fresh too

    def test_live_within_ttl(self):
        sn.write_session_heartbeat(self.dir, "daemon", now=NOW)
        self.assertTrue(sn.session_is_live(self.dir, NOW + timedelta(seconds=60)))   # within 120 s

    def test_stale_past_ttl_is_not_live(self):
        sn.write_session_heartbeat(self.dir, "daemon", now=NOW)
        self.assertFalse(sn.session_is_live(self.dir, NOW + timedelta(seconds=200)))  # past 120 s
        # A custom shorter TTL ages out sooner.
        self.assertFalse(sn.session_is_live(self.dir, NOW + timedelta(seconds=45), ttl_seconds=30))

    def test_malformed_file_is_not_live(self):
        sn.save_json(os.path.join(self.dir, sn.SESSION_FILE), {"last_seen": "not-a-date"})
        self.assertFalse(sn.session_is_live(self.dir, NOW))  # a broken file must never block a nudge
        # A non-dict / missing last_seen also reads absent.
        sn.save_json(os.path.join(self.dir, sn.SESSION_FILE), {"pid": 5})
        self.assertFalse(sn.session_is_live(self.dir, NOW))

    def test_clear_removes(self):
        sn.write_session_heartbeat(self.dir, "daemon", now=NOW)
        self.assertTrue(sn.clear_session_heartbeat(self.dir))
        self.assertFalse(sn.session_is_live(self.dir, NOW))
        self.assertFalse(sn.clear_session_heartbeat(self.dir))  # idempotent


class RegistryUnit(unittest.TestCase):
    """Phase-2 registry semantics: per-session entries, gating vs awareness, carry-forward, pruning."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_entry_written_under_sessions_dir(self):
        state = sn.write_session_heartbeat(self.dir, "daemon", now=NOW)
        self.assertEqual(state["session_id"], "daemon")  # id defaults to the source (singleton surfaces)
        self.assertTrue(os.path.exists(os.path.join(self.dir, sn.SESSIONS_DIR, "daemon.json")))

    def test_build_session_is_visible_but_never_gates(self):
        sn.write_session_heartbeat(self.dir, "build", session_id="abc-123", now=NOW,
                                   working_on="seneschal @ feature-branch")
        self.assertFalse(sn.session_is_live(self.dir, NOW))  # awareness-only: a coding session still buzzes
        live = sn.list_live_sessions(self.dir, NOW)
        self.assertEqual([s["session_id"] for s in live], ["abc-123"])
        self.assertEqual(live[0]["working_on"], "seneschal @ feature-branch")

    def test_multiple_sessions_coexist_and_gate_independently(self):
        sn.write_session_heartbeat(self.dir, "build", session_id="b1", now=NOW)
        sn.write_session_heartbeat(self.dir, "desktop", now=NOW)
        sn.write_session_heartbeat(self.dir, "daemon", now=NOW - timedelta(seconds=300))  # stale
        self.assertTrue(sn.session_is_live(self.dir, NOW))          # fresh desktop gates
        self.assertEqual(len(sn.list_live_sessions(self.dir, NOW)), 3)  # all visible in the 1 h window
        sn.clear_session_heartbeat(self.dir, "desktop")
        self.assertFalse(sn.session_is_live(self.dir, NOW))         # build alone never gates

    def test_working_on_carried_forward_on_bare_refresh(self):
        sn.write_session_heartbeat(self.dir, "desktop", pid=7, now=NOW, working_on="phase-2 design")
        second = sn.write_session_heartbeat(self.dir, "desktop", pid=7, now=NOW + timedelta(seconds=30))
        self.assertEqual(second["working_on"], "phase-2 design")     # a bare refresh never wipes it
        third = sn.write_session_heartbeat(self.dir, "desktop", pid=7, now=NOW + timedelta(seconds=60),
                                           working_on="registry build")
        self.assertEqual(third["working_on"], "registry build")      # a new value replaces it

    def test_legacy_single_file_still_gates(self):
        sn.save_json(os.path.join(self.dir, sn.SESSION_FILE),
                     {"pid": 1, "source": "daemon", "started_at": sn._session_stamp(NOW),
                      "last_seen": sn._session_stamp(NOW)})
        self.assertTrue(sn.session_is_live(self.dir, NOW + timedelta(seconds=60)))  # transition insurance

    def test_prune_removes_crash_orphans_but_keeps_fresh(self):
        sn.write_session_heartbeat(self.dir, "build", session_id="dead", now=NOW - timedelta(hours=25))
        sn.write_session_heartbeat(self.dir, "build", session_id="alive", now=NOW)  # write prunes too
        ids = {s["session_id"] for s in sn.load_sessions(self.dir)}
        self.assertEqual(ids, {"alive"})

    def test_session_id_sanitized_to_safe_filename(self):
        state = sn.write_session_heartbeat(self.dir, "build", session_id="..\\evil/../x", now=NOW)
        self.assertNotIn("/", state["session_id"])
        self.assertNotIn("\\", state["session_id"])
        names = os.listdir(os.path.join(self.dir, sn.SESSIONS_DIR))
        self.assertEqual(len(names), 1)  # landed inside the registry dir, nowhere else


class SessionStampUnit(unittest.TestCase):
    """The machine-wide hook writer: event JSON in → a build entry written/refreshed/cleared, silently."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cwd = tempfile.mkdtemp()  # not a git repo → branch resolves to None, label = dir name

    def _event(self, name, sid="sess-uuid-1"):
        return {"hook_event_name": name, "session_id": sid, "cwd": self.cwd}

    def test_prompt_event_writes_active_build_entry(self):
        self.assertEqual(st.handle_event(self._event("UserPromptSubmit"), self.dir), "written")
        (entry,) = sn.list_live_sessions(self.dir, ttl_seconds=10**9)
        self.assertEqual(entry["source"], "build")
        self.assertEqual(entry["phase"], "active")
        self.assertEqual(entry["cwd"], self.cwd)
        self.assertEqual(entry["working_on"], os.path.basename(os.path.normpath(self.cwd)))
        self.assertFalse(sn.session_is_live(self.dir))  # hook entries never gate delivery

    def test_stop_and_start_mark_idle_and_preserve_started_at(self):
        st.handle_event(self._event("SessionStart"), self.dir)
        (first,) = sn.load_sessions(self.dir)
        st.handle_event(self._event("Stop"), self.dir)
        (entry,) = sn.load_sessions(self.dir)
        self.assertEqual(entry["phase"], "idle")
        self.assertEqual(entry["started_at"], first["started_at"])  # marker pid keeps the window stable

    def test_session_end_clears_the_entry(self):
        st.handle_event(self._event("UserPromptSubmit"), self.dir)
        self.assertEqual(st.handle_event(self._event("SessionEnd"), self.dir), "cleared")
        self.assertEqual(sn.load_sessions(self.dir), [])

    def test_unknown_or_anonymous_events_ignored(self):
        self.assertEqual(st.handle_event(self._event("PreToolUse"), self.dir), "ignored")
        self.assertEqual(st.handle_event({"hook_event_name": "Stop", "cwd": self.cwd}, self.dir),
                         "ignored")  # no session_id → nothing to key the entry on
        self.assertEqual(sn.load_sessions(self.dir), [])


class FireGateUnit(unittest.TestCase):
    """The reminder-fire chokepoint: defer non-piercing into a live session, pierce Call Me / Critical."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._orig = sn.send_telegram
        self.sent = []
        sn.send_telegram = lambda text, env: (self.sent.append(text) or {"ok": True})

    def tearDown(self):
        sn.send_telegram = self._orig

    def _write(self, rows):
        sn.save_json(os.path.join(self.dir, "reminders.json"), rows)

    def _rows(self):
        return sn.load_json(os.path.join(self.dir, "reminders.json"), [])

    def test_live_defers_plain_but_pierces_critical_and_call(self):
        sn.write_session_heartbeat(self.dir, "daemon", now=NOW)  # live
        self._write([
            {"id": "alex", "text": "Check messages from Alex.", "due_at": _z(NOW), "channel": "telegram", "fired_at": None},
            {"id": "meds", "text": "Evening meds.", "due_at": _z(NOW), "channel": "telegram", "pierce_quiet": True, "fired_at": None},
            {"id": "ring", "text": "Meds, please.", "due_at": _z(NOW), "channel": "call", "fired_at": None},
        ])
        signals = sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")
        kinds = {s["id"]: s["kind"] for s in signals}
        self.assertEqual(kinds["alex"], "reminder_deferred_session")
        self.assertEqual(kinds["meds"], "reminder_fired")
        self.assertEqual(kinds["ring"], "reminder_fired")
        rows = {r["id"]: r for r in self._rows()}
        # Deferred = HELD, not dropped: nothing stamped, so it re-checks next tick.
        self.assertIsNone(rows["alex"].get("fired_at"))
        self.assertIsNone(rows["alex"].get("suppressed_at"))
        self.assertIsNone(rows["alex"].get("acked_at"))
        self.assertTrue(rows["meds"].get("fired_at"))
        self.assertNotIn("Alex", " ".join(self.sent))  # the low-stakes buzz never landed mid-chat

    def test_deferred_then_fires_when_session_ends(self):
        sn.write_session_heartbeat(self.dir, "daemon", now=NOW)
        self._write([{"id": "alex", "text": "Check Alex.", "due_at": _z(NOW), "channel": "telegram", "fired_at": None}])
        sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")   # held while live
        self.assertEqual(self.sent, [])
        sn.clear_session_heartbeat(self.dir)                            # session winds down
        later = sn.check_reminders(self.dir, NOW + timedelta(minutes=1), fire=True, telegram_env="x")
        self.assertEqual(later[0]["kind"], "reminder_fired")            # fires naturally, never dropped
        self.assertEqual(len(self.sent), 1)

    def test_no_session_plain_fires_normally(self):
        self._write([{"id": "alex", "text": "Check Alex.", "due_at": _z(NOW), "channel": "telegram", "fired_at": None}])
        signals = sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")
        self.assertEqual(signals[0]["kind"], "reminder_fired")
        self.assertEqual(len(self.sent), 1)

    def test_composes_with_quiet_still_no_drops_piercing_still_pierces(self):
        # Both gates active: quiet drops the non-piercer (consumed), session gate never gets to it; the
        # piercing item clears BOTH and fires. No double-handling, piercing still pierces.
        sn.set_quiet(self.dir, NOW + timedelta(hours=8))
        sn.write_session_heartbeat(self.dir, "daemon", now=NOW)
        self._write([
            {"id": "alex", "text": "Check Alex.", "due_at": _z(NOW), "channel": "telegram", "fired_at": None},
            {"id": "ring", "text": "Meds, please.", "due_at": _z(NOW), "channel": "call", "fired_at": None},
        ])
        signals = sn.check_reminders(self.dir, NOW, fire=True, telegram_env="x")
        kinds = {s["id"]: s["kind"] for s in signals}
        self.assertEqual(kinds["alex"], "reminder_suppressed_quiet")   # quiet wins (drop), checked first
        self.assertEqual(kinds["ring"], "reminder_fired")               # Call Me pierces both gates


class PeekSkipUnit(unittest.TestCase):
    """The Watch-peek chokepoint: skip the redundant comms-peek while a session is live."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _args(self):
        # Minimal stand-in with just the attrs maybe_peek reads; stub_brain avoids any real subprocess.
        return types.SimpleNamespace(
            peek_interval_min=5, watch_prompt="Run Watch.", watch_cmd=None, stub_brain=True,
            claude_bin="claude", permission_mode="bypassPermissions", notion_mcp=None, watch_model=None)

    def test_peek_runs_when_no_session(self):
        ran = pr.maybe_peek(self.dir, self._args(), log=lambda *_: None, children=[], warm_busy=False)
        self.assertTrue(ran)  # cadence due, nothing live → the peek proceeds (stubbed)

    def test_peek_skipped_while_session_live(self):
        sn.write_session_heartbeat(self.dir, "daemon")  # live now
        ran = pr.maybe_peek(self.dir, self._args(), log=lambda *_: None, children=[], warm_busy=False)
        self.assertFalse(ran)  # a human is engaged — the peek is redundant and skipped this cycle
        # And it didn't consume the cadence (no last-peek stamp written), so it can run once live ends.
        self.assertIsNone(sn.load_json(os.path.join(self.dir, "last-peek"), None))


class BranchClaimUnit(unittest.TestCase):
    """`branch_is_claimed` — the guard seneschald-update asks before reclaiming a checkout a session
    parked off the deploy branch. Unlike every other registry read, this one is fail-CLOSED: it
    authorizes *moving someone else's branch*, so "I can't tell" must read as "claimed"."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _stamp(self, sid, branch, source="build", now=NOW, pid=1):
        return sn.write_session_heartbeat(self.dir, source, pid=pid, now=now, session_id=sid,
                                          branch=branch, cwd="C:/repo", working_on=f"x @ {branch}")

    # --- fail-CLOSED: absence of proof is NOT proof of absence ---

    def test_no_registry_dir_reads_claimed(self):
        # The registry was never created (hook not installed / fresh machine). We cannot prove the
        # branch is free, so we must not license a reclaim. Contrast session_is_live, which fails OPEN.
        self.assertTrue(sn.branch_is_claimed(self.dir, "feature/x", NOW))
        self.assertFalse(sn.session_is_live(self.dir, NOW))  # the other direction, same empty state

    def test_empty_branch_reads_claimed(self):
        self._stamp("s1", "main")
        for falsy in ("", None):
            self.assertTrue(sn.branch_is_claimed(self.dir, falsy, NOW))

    def test_undateable_entry_on_the_branch_reads_claimed(self):
        self._stamp("s1", "feature/x")
        path = os.path.join(self.dir, "sessions", "s1.json")
        data = sn.load_json(path, {})
        data["last_seen"] = "not-a-timestamp"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(sn.json.dumps(data))
        # An entry sitting ON the branch that we can't date → assume live rather than yank it.
        self.assertTrue(sn.branch_is_claimed(self.dir, "feature/x", NOW))

    # --- the healable case: registry present, nobody on the branch ---

    def test_registry_present_but_branch_unclaimed_is_free(self):
        self._stamp("s1", "main")
        self._stamp("s2", "other/thing")
        # The multi-hour-outage shape: the session that parked the checkout has ENDED (SessionEnd
        # removed its entry), so nothing claims the branch and the reclaim is safe.
        self.assertFalse(sn.branch_is_claimed(self.dir, "feature/discord-gateway-ws", NOW))

    def test_live_session_on_the_branch_is_claimed(self):
        self._stamp("s1", "claude/session-registry-work")
        self.assertTrue(sn.branch_is_claimed(self.dir, "claude/session-registry-work", NOW))

    def test_stale_entry_stops_claiming_after_the_awareness_window(self):
        self._stamp("s1", "feature/x", now=NOW - timedelta(seconds=sn.SESSION_VISIBLE_TTL_SEC + 60))
        # A crashed session's entry lingers; after the 1 h awareness window it stops blocking the heal.
        self.assertFalse(sn.branch_is_claimed(self.dir, "feature/x", NOW))
        # ...but inside the window it still owns it.
        self.assertTrue(sn.branch_is_claimed(self.dir, "feature/x",
                                             NOW - timedelta(seconds=sn.SESSION_VISIBLE_TTL_SEC - 60)))

    def test_idle_session_still_owns_its_branch(self):
        # 10 min idle: far past the 120 s *gating* TTL, well inside the 1 h *awareness* window. A
        # session thinking between prompts must not have its branch moved.
        self._stamp("s1", "feature/x", now=NOW - timedelta(minutes=10))
        self.assertFalse(sn.session_is_live(self.dir, NOW))          # would not defer a nudge...
        self.assertTrue(sn.branch_is_claimed(self.dir, "feature/x", NOW))  # ...but still owns the branch

    def test_any_source_claims_not_just_gating_ones(self):
        # `build` never gates delivery, but it's exactly the session type sitting on a feature branch.
        self._stamp("s1", "feature/x", source="build")
        self.assertNotIn("build", sn.GATING_SOURCES)
        self.assertTrue(sn.branch_is_claimed(self.dir, "feature/x", NOW))

    def test_other_sessions_on_other_branches_do_not_claim(self):
        self._stamp("s1", "main", source="daemon")
        self._stamp("s2", "feature/y", source="build")
        self.assertFalse(sn.branch_is_claimed(self.dir, "feature/x", NOW))


if __name__ == "__main__":
    unittest.main()
