#!/usr/bin/env python3
"""Tests for the daemon's ownership of the Notion write-behind outbox (presence.py).

**What these pin.** The outbox journals an act-low Notion write locally and then flushes it. If the
only thing ever asked to flush it is a sentence in `modes/chat.md` — *"opportunistically drain any
backlog at a natural point in a turn"* — that is a warm-session instruction, not an owner: a queue can
sit a day with nothing in the daemon's supervised tasks going to touch it, while a 👍 on a reminder
nudge reaches the local ledger and the queue and the ⏰ row is never written.

So the flush has a task, and these tests cover the parts that must not quietly stop working: the
pure-local supersession sweep (which retires backwards writes for free), the *gate* on the spawned
flush (stale enough, and not too often), the backlog alarm (the §7 sensor), and the **Notion-backend
gate** — the outbox is Notion-only, so on a filesystem backend none of this reads, spawns or nudges.

**The presence half lands with the daemon rewrite.** Each class skips until the presence hook it
exercises exists (``hasattr`` guards below), so the file activates piece by piece as the wiring lands.

No live `claude`, no network, no Notion. Stdlib ``unittest``.
Run:  python -m unittest test_outbox_drain   (from seneschal/scripts)
"""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import outbox_common as ob  # noqa: E402
import presence as pr  # noqa: E402
import reminders_acks as ra  # noqa: E402

RID = "00000000-0000-0000-0000-0000000000a7"   # a placeholder ⏰ page id
NOW = datetime(2026, 8, 7, 14, 30, 0, tzinfo=timezone.utc)



class _NotionBackend:
    """Mixin: pin the active store backend to Notion for the duration of a test — the outbox is a
    Notion-backend component, so every drain/alarm path is gated on it."""

    def setUp(self):
        super().setUp()
        real = pr.store_backend_active
        pr.store_backend_active = lambda *a, **k: "notion"
        self.addCleanup(setattr, pr, "store_backend_active", real)


def _args(state_dir, **over):
    base = dict(state_dir=state_dir, stub_brain=False, fake_inbox=None, claude_bin="claude",
                permission_mode="bypassPermissions", notion_mcp=None, slack_mcp=None,
                watch_model=None, no_outbox=False, no_outbox_drain=False,
                outbox_stale_min=pr.OUTBOX_STALE_SEC // 60,
                outbox_live_stale_min=pr.OUTBOX_LIVE_SESSION_STALE_SEC // 60,
                outbox_drain_interval_min=pr.OUTBOX_DRAIN_INTERVAL_SEC // 60)
    base.update(over)
    return types.SimpleNamespace(**base)


def _enqueue_ack(state_dir, date, rid=RID, created=None):
    conn = ob.connect(state_dir)
    try:
        payload = {"reminder_id": rid, "status": "Done", "last_acknowledged": date,
                   "consecutive_misses": 0, "untick_ack": True}
        return ob.enqueue(conn, "ack_reminder", "page", rid, payload, ob.ack_key(rid, date),
                          now=created or NOW)
    finally:
        conn.close()


class BacklogReading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_empty_store_reads_empty(self):
        b = pr.outbox_backlog(self.dir)
        self.assertEqual((b["pending"], b["dead"], b["resolved"]), (0, 0, 0))
        self.assertIsNone(b["oldest_sec"])

    def test_counts_pending_and_ages_the_oldest(self):
        _enqueue_ack(self.dir, "2026-08-07", created=datetime.now(timezone.utc) - timedelta(hours=2))
        b = pr.outbox_backlog(self.dir)
        self.assertEqual(b["pending"], 1)
        self.assertGreater(b["oldest_sec"], 3600)

    def test_reading_sweeps_a_superseded_ack_for_free(self):
        """The cheap half. When the stuck rows have already been acked by hand for a LATER date, this
        pass alone clears the queue — with no Notion call at all."""
        _enqueue_ack(self.dir, "2026-08-06")
        ra.record_ack(self.dir, RID, "2026-08-07")
        b = pr.outbox_backlog(self.dir)
        self.assertEqual(b["resolved"], 1)
        self.assertEqual(b["pending"], 0)

    def test_a_current_ack_is_not_swept(self):
        _enqueue_ack(self.dir, "2026-08-07")
        ra.record_ack(self.dir, RID, "2026-08-07")
        b = pr.outbox_backlog(self.dir)
        self.assertEqual((b["resolved"], b["pending"]), (0, 1))

    def test_a_dead_letter_carries_its_op_and_target_for_the_nudge_to_name(self):
        conn = ob.connect(self.dir)
        try:
            r = ob.enqueue(conn, ob.TASK_STATUS, "page", "task-page-1", {"status": "Done"},
                           ob.task_status_key("task-page-1", "Done"))
            ob.mark_failed(conn, r["id"], "operation not supported", status_code=None)
        finally:
            conn.close()
        b = pr.outbox_backlog(self.dir)
        self.assertEqual(len(b["dead_letters"]), 1)
        d = b["dead_letters"][0]
        self.assertEqual(d["op"], ob.TASK_STATUS)
        self.assertEqual(d["target_id"], "task-page-1")
        self.assertIn("operation not supported", d["last_error"])

    def test_an_unreadable_store_reads_as_empty_and_never_raises(self):
        """Fail-open in the observer only — nothing here drops an entry; the store's own fail-CLOSED
        retry doctrine is untouched. A broken read costs this reading, never the scheduler tick."""
        with open(os.path.join(self.dir, ob.DB_FILE), "w", encoding="utf-8") as fh:
            fh.write("this is not a sqlite database")
        b = pr.outbox_backlog(self.dir)
        self.assertEqual(b["pending"], 0)

    def test_an_unreadable_store_is_distinguishable_from_a_healthy_one(self):
        """A bare {"pending": 0, "dead": 0} on a raise reads exactly like a quiet, healthy queue.
        `read_failed` is what a watchdog checks instead of a bare zero."""
        with open(os.path.join(self.dir, ob.DB_FILE), "w", encoding="utf-8") as fh:
            fh.write("this is not a sqlite database")
        broken = pr.outbox_backlog(self.dir)
        self.assertTrue(broken["read_failed"])
        healthy = pr.outbox_backlog(tempfile.mkdtemp())
        self.assertNotIn("read_failed", healthy)


class DrainGate(unittest.TestCase):
    """`outbox_drain_due` is pure so the two conditions can be pinned without spawning anything."""

    def test_fresh_backlog_never_spawns(self):
        """Option (a) stays first: a turn gets its chance before the daemon spends one."""
        self.assertFalse(pr.outbox_drain_due({"oldest_sec": 60}, None, NOW))

    def test_stale_backlog_with_no_prior_drain_is_due(self):
        self.assertTrue(pr.outbox_drain_due({"oldest_sec": 3600}, None, NOW))

    def test_recent_drain_holds_the_next_one_off(self):
        recent = (NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        self.assertFalse(pr.outbox_drain_due({"oldest_sec": 3600}, recent, NOW))

    def test_old_drain_lets_the_next_one_through(self):
        old = (NOW - timedelta(hours=4)).isoformat().replace("+00:00", "Z")
        self.assertTrue(pr.outbox_drain_due({"oldest_sec": 3600}, old, NOW))

    def test_missing_or_unreadable_age_is_not_due(self):
        for backlog in ({}, {"oldest_sec": None}, {"oldest_sec": "ages"}):
            self.assertFalse(pr.outbox_drain_due(backlog, None, NOW))

    def test_unreadable_stamp_does_not_latch_the_drain_off_forever(self):
        self.assertTrue(pr.outbox_drain_due({"oldest_sec": 3600}, "not-a-date", NOW))

    def test_a_live_session_gets_the_longer_bound_not_a_veto(self):
        """§9 (a‴): the live-session gate is a DELAY. Past the plain 20-min stale bound but still
        under the live-session bound (60 min default), a live session still gets first refusal."""
        self.assertFalse(pr.outbox_drain_due({"oldest_sec": 40 * 60}, None, NOW, session_live=True))

    def test_a_live_session_past_its_own_longer_bound_is_due_anyway(self):
        """The whole point of the delay: leaning on the session forever lets one ack sit for half a
        day. Once THIS bound passes, the daemon drains regardless of session state."""
        self.assertTrue(pr.outbox_drain_due({"oldest_sec": 90 * 60}, None, NOW, session_live=True))

    def test_the_live_bound_is_configurable_and_defaults_longer_than_the_plain_one(self):
        self.assertGreater(pr.OUTBOX_LIVE_SESSION_STALE_SEC, pr.OUTBOX_STALE_SEC)
        self.assertTrue(pr.outbox_drain_due({"oldest_sec": 50 * 60}, None, NOW, session_live=True,
                                            live_stale_sec=40 * 60))

    def test_a_dead_session_uses_the_plain_bound_even_when_a_stale_stamp_argument_is_passed(self):
        """`session_live=False` (the default) must not accidentally pick up the live bound."""
        self.assertTrue(pr.outbox_drain_due({"oldest_sec": 21 * 60}, None, NOW,
                                            stale_sec=20 * 60, live_stale_sec=60 * 60))


class DrainSpawn(_NotionBackend, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.spawned = []
        self.mcp = os.path.join(self.dir, "notion-mcp.json")
        with open(self.mcp, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {}}, fh)

    def _launch(self, args, children=None, warm_busy=False, backlog=None, live=False):
        backlog = backlog if backlog is not None else {"pending": 3, "oldest_sec": 90 * 60}

        def fake_popen(cmd, **kw):
            self.spawned.append(cmd)
            return types.SimpleNamespace(poll=lambda: None, pid=1)

        real_popen, real_live = pr.subprocess.Popen, pr.session_is_live
        pr.subprocess.Popen = fake_popen
        pr.session_is_live = lambda *a, **k: live
        try:
            return pr.maybe_drain_outbox(self.dir, args, lambda *_: None, children, warm_busy, backlog)
        finally:
            pr.subprocess.Popen, pr.session_is_live = real_popen, real_live

    def test_spawns_a_claude_with_the_notion_config(self):
        self.assertTrue(self._launch(_args(self.dir, notion_mcp=self.mcp)))
        self.assertEqual(len(self.spawned), 1)
        cmd = self.spawned[0]
        self.assertIn("--mcp-config", cmd)
        self.assertIn(self.mcp, cmd)
        self.assertIn("outbox.py pull", " ".join(cmd))

    def test_prompt_forbids_the_query_path(self):
        """`notion-update-page` by page id routes around the collection router; the SQL query path is
        the one that 429s (references/notion-rate-limits.md). The drain must never go looking."""
        self._launch(_args(self.dir, notion_mcp=self.mcp))
        prompt = self.spawned[0][2]
        self.assertIn("notion-update-page", prompt)
        self.assertIn("data-source query", prompt)
        self.assertNotIn("notion-search", prompt)

    def test_prompt_names_the_correct_create_parent_shape(self):
        """A `'db'` create's `target_id` is a Notion data-source id (a `collection://…` URL), not a
        database_id — passing it as `database_id` 404s every time, and a batch of creates dead-letters
        on a caller bug. The prompt must say so explicitly rather than leaving the drain turn to guess
        the parent shape, and the string must survive `.format(limit=...)` (a literal `{`/`}` in the
        prompt template breaks that call unless doubled)."""
        self._launch(_args(self.dir, notion_mcp=self.mcp))
        prompt = self.spawned[0][2]
        self.assertIn('parent: {"data_source_id": target_id}', prompt)
        self.assertIn("never a database_id", prompt)

    def test_prompt_requires_a_status_code_on_dead_letter(self):
        """The drain turn cannot verify a 4xx is genuinely permanent
        (it is forbidden from querying, per `test_prompt_forbids_the_query_path`), so it must pass the
        real status code through rather than letting `--dead-letter` default to "permanent"."""
        self._launch(_args(self.dir, notion_mcp=self.mcp))
        prompt = self.spawned[0][2]
        self.assertIn("--status-code", prompt)
        self.assertIn("CALLER bug", prompt)

    def test_no_notion_config_means_no_spawn(self):
        """Without Notion wired the child could not write anything — it would burn a turn and mark
        nothing, which is worse than leaving the durable entries where they are."""
        self.assertFalse(self._launch(_args(self.dir)))
        self.assertEqual(self.spawned, [])

    def test_empty_backlog_never_spawns(self):
        self.assertFalse(self._launch(_args(self.dir, notion_mcp=self.mcp),
                                      backlog={"pending": 0, "oldest_sec": None}))

    def test_deferred_while_a_warm_turn_or_another_headless_child_runs(self):
        args = _args(self.dir, notion_mcp=self.mcp)
        self.assertFalse(self._launch(args, warm_busy=True))
        running = [types.SimpleNamespace(poll=lambda: None)]
        self.assertFalse(self._launch(args, children=running))
        self.assertEqual(self.spawned, [])

    def test_deferred_while_a_live_session_is_still_within_its_own_bound(self):
        """A live session still gets first refusal — that IS option (a) — but per §9 (a‴) this is a
        DELAY, not a veto: the backlog here (90 min) is stale past the plain bound but still under
        the live-session bound (60 min default is exceeded — use a backlog inside it explicitly)."""
        backlog = {"pending": 3, "oldest_sec": 40 * 60}  # past 20-min plain bound, under 60-min live one
        self.assertFalse(self._launch(_args(self.dir, notion_mcp=self.mcp), backlog=backlog, live=True))

    def test_drains_anyway_once_a_live_session_has_had_its_own_longer_bound(self):
        """Leaning on the live session forever lets one ack sit for half a day. Past
        OUTBOX_LIVE_SESSION_STALE_SEC the daemon drains regardless of session state."""
        backlog = {"pending": 3, "oldest_sec": pr.OUTBOX_LIVE_SESSION_STALE_SEC + 60}
        self.assertTrue(self._launch(_args(self.dir, notion_mcp=self.mcp), backlog=backlog, live=True))

    def test_no_outbox_drain_flag_keeps_the_flush_off(self):
        self.assertFalse(self._launch(_args(self.dir, notion_mcp=self.mcp, no_outbox_drain=True)))

    def test_the_cadence_stamp_is_written_before_the_spawn(self):
        """A launch that throws must still burn its slot, or a permanently-failing spawn forks a
        `claude` every tick."""
        args = _args(self.dir, notion_mcp=self.mcp)
        real_popen = pr.subprocess.Popen
        real_live = pr.session_is_live
        pr.subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        pr.session_is_live = lambda *a, **k: False
        try:
            self.assertFalse(pr.maybe_drain_outbox(self.dir, args, lambda *_: None, None, False,
                                                   {"pending": 1, "oldest_sec": 90 * 60}))
        finally:
            pr.subprocess.Popen, pr.session_is_live = real_popen, real_live
        self.assertTrue(os.path.exists(os.path.join(self.dir, "last-outbox-drain")))

    def test_second_call_inside_the_interval_does_not_spawn_again(self):
        args = _args(self.dir, notion_mcp=self.mcp)
        self.assertTrue(self._launch(args))
        self.assertFalse(self._launch(args))
        self.assertEqual(len(self.spawned), 1)

    def test_a_filesystem_backend_never_spawns(self):
        """The outbox is a Notion-backend component: obsidian/markdown write locally and atomically and
        never journal here, so even a stale backlog (a leftover store from a backend switch) with MCP
        configs wired must not launch a Notion flush."""
        for backend in ("obsidian", "markdown", None):
            pr.store_backend_active = lambda *a, _b=backend, **k: _b
            self.assertFalse(self._launch(_args(self.dir, notion_mcp=self.mcp)))
        self.assertEqual(self.spawned, [])


class BacklogAlarm(unittest.TestCase):
    """The sensor the spec designed (§7) — without it a stuck backlog grows for a day unremarked."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.logs = []

    def _queued(self):
        path = os.path.join(self.dir, "outbound.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_a_fresh_backlog_says_nothing(self):
        pr.maybe_nudge_outbox_backlog(self.dir, self.logs.append,
                                      {"pending": 2, "oldest_sec": 300, "dead": 0})
        self.assertEqual(self._queued(), [])

    def test_a_stale_backlog_reaches_the_owner(self):
        pr.maybe_nudge_outbox_backlog(self.dir, self.logs.append,
                                      {"pending": 8, "oldest_sec": 23.4 * 3600, "dead": 0})
        items = self._queued()
        self.assertEqual(len(items), 1)
        self.assertIn("8 un-landed", items[0]["text"])
        self.assertEqual(items[0]["supersede_key"], "outbox-backlog")

    def test_a_dead_letter_is_nudged_regardless_of_age(self):
        """It has STOPPED retrying, so waiting for it to age is waiting for nothing to happen."""
        pr.maybe_nudge_outbox_backlog(self.dir, self.logs.append,
                                      {"pending": 0, "oldest_sec": None, "dead": 1})
        self.assertEqual(len(self._queued()), 1)

    def test_a_dead_letter_names_its_op_and_row(self):
        """An aggregate "1 dead-letter" nudge names neither the op nor the row, so reading the alert
        on a phone tells you *that* something needs attention and nothing about *what*."""
        pr.maybe_nudge_outbox_backlog(self.dir, self.logs.append, {
            "pending": 0, "oldest_sec": None, "dead": 1,
            "dead_letters": [{"id": "3dac5a13e2434", "op": "task_status",
                              "target_id": "00000000-…", "last_error": "operation not supported"}],
        })
        text = self._queued()[0]["text"]
        self.assertIn("task_status", text)
        self.assertIn("3dac5a13", text)
        self.assertIn("operation not supported", text)

    def test_a_dead_letter_with_no_detail_still_nudges(self):
        """`dead_letters` is optional — an older/partial `backlog` dict (or a caller that only knows
        the count) must not KeyError; it just gets the aggregate line with no named row."""
        pr.maybe_nudge_outbox_backlog(self.dir, self.logs.append,
                                      {"pending": 0, "oldest_sec": None, "dead": 1})
        self.assertIn("1 dead-letter", self._queued()[0]["text"])

    def test_many_dead_letters_are_capped_with_a_tail(self):
        dead_letters = [{"id": f"id{i:03d}", "op": "task_status", "target_id": f"t{i}",
                         "last_error": "boom"} for i in range(pr.DEAD_LETTER_NUDGE_MAX + 2)]
        pr.maybe_nudge_outbox_backlog(self.dir, self.logs.append, {
            "pending": 0, "oldest_sec": None, "dead": len(dead_letters), "dead_letters": dead_letters,
        })
        text = self._queued()[0]["text"]
        self.assertEqual(text.count("task_status"), pr.DEAD_LETTER_NUDGE_MAX)
        self.assertIn("+2 more", text)

    def test_at_most_once_per_local_day(self):
        backlog = {"pending": 8, "oldest_sec": 23.4 * 3600, "dead": 0}
        day = datetime(2026, 8, 7, 9, 40)
        pr.maybe_nudge_outbox_backlog(self.dir, self.logs.append, backlog, day)
        pr.maybe_nudge_outbox_backlog(self.dir, self.logs.append, backlog, day.replace(hour=18))
        self.assertEqual(len(self._queued()), 1)
        pr.maybe_nudge_outbox_backlog(self.dir, self.logs.append, backlog, day + timedelta(days=1))
        self.assertEqual(len(self._queued()), 2)

    def test_observed_at_is_when_the_backlog_started_not_now(self):
        """A day-old backlog must READ as a day old (mouth-spec §3.2) — otherwise every repeat of a
        long-standing fact arrives dressed as fresh news."""
        pr.maybe_nudge_outbox_backlog(self.dir, self.logs.append,
                                      {"pending": 8, "oldest_sec": 23.4 * 3600, "dead": 0})
        item = self._queued()[0]
        observed = datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
        queued = datetime.fromisoformat(item["queued_at"].replace("Z", "+00:00"))
        self.assertGreater((queued - observed).total_seconds(), 20 * 3600)

    def test_a_failed_enqueue_does_not_stamp_the_day(self):
        """Stamping on a message nobody got would suppress the alarm for 24 h — the same
        silent-success shape the alarm exists to break."""
        real = pr.mouth.enqueue
        pr.mouth.enqueue = lambda *a, **k: None
        try:
            pr.maybe_nudge_outbox_backlog(self.dir, self.logs.append,
                                          {"pending": 8, "oldest_sec": 4 * 3600, "dead": 0})
        finally:
            pr.mouth.enqueue = real
        self.assertFalse(os.path.exists(os.path.join(self.dir, "outbox-nudged.json")))
        self.assertTrue(any("retry next tick" in line for line in self.logs))


class ActuallyOwnedByTheTick(_NotionBackend, unittest.TestCase):
    """The part that actually matters.

    Every function above can be perfect and the bug still ships if nothing calls them — that is
    precisely what happened to the drain the spec documented in `modes/chat.md`. So this pins the
    wiring itself: the scheduler tick calls `_tend_outbox`, and `_tend_outbox` reads, sweeps, alarms
    and flushes."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.logs = []

    def test_the_scheduler_tick_calls_it(self):
        import inspect
        src = inspect.getsource(pr.scheduler_task)
        self.assertIn("_tend_outbox", src)

    def test_it_is_outside_the_no_reminders_gate(self):
        """A Notion write that has stopped landing is not a nudge the owner scheduled, so `--no-reminders`
        must not be able to switch the alarm off — the same posture as the Dream-step alarm."""
        import inspect
        lines = inspect.getsource(pr.scheduler_task).splitlines()
        gate = next(ln for ln in lines if "if not args.no_reminders:" in ln)
        call = next(ln for ln in lines if "await _tend_outbox" in ln)
        self.assertEqual(len(call) - len(call.lstrip()), len(gate) - len(gate.lstrip()),
                         "must sit at tick level, not nested inside the no_reminders branch")

    def test_one_pass_reads_sweeps_and_alarms(self):
        import asyncio
        _enqueue_ack(self.dir, "2026-08-06", created=datetime.now(timezone.utc) - timedelta(hours=23))
        ra.record_ack(self.dir, RID, "2026-08-07")
        state = types.SimpleNamespace(outbox={}, outbox_checked_at=0.0, headless_children=[])
        asyncio.run(pr._tend_outbox(state, _args(self.dir), self.logs.append, False))
        self.assertEqual(state.outbox["resolved"], 1)      # the stale ack retired, write-free
        self.assertEqual(state.outbox["pending"], 0)
        self.assertTrue(any("superseded" in line for line in self.logs))

    def test_the_read_is_throttled_below_the_tick_rate(self):
        """The tick is ~5 s; opening sqlite that often to learn a number that changes in hours is
        waste. The reading is cached on `state` like `jobs_active`."""
        import asyncio
        state = types.SimpleNamespace(outbox={}, outbox_checked_at=0.0, headless_children=[])
        args = _args(self.dir)
        calls = []
        real = pr.outbox_backlog
        pr.outbox_backlog = lambda d: (calls.append(d), real(d))[1]
        try:
            for _ in range(4):
                asyncio.run(pr._tend_outbox(state, args, self.logs.append, False))
        finally:
            pr.outbox_backlog = real
        self.assertEqual(len(calls), 1)

    def test_no_outbox_turns_the_whole_thing_off(self):
        import asyncio
        state = types.SimpleNamespace(outbox={}, outbox_checked_at=0.0, headless_children=[])
        asyncio.run(pr._tend_outbox(state, _args(self.dir, no_outbox=True), self.logs.append, False))
        self.assertEqual(state.outbox, {})

    def test_a_filesystem_backend_turns_the_whole_thing_off(self):
        """Notion-backend only: on obsidian/markdown there is no outbox to own — the tick must not
        open the store, sweep, alarm or spawn."""
        import asyncio
        pr.store_backend_active = lambda *a, **k: "markdown"
        _enqueue_ack(self.dir, "2026-08-06", created=datetime.now(timezone.utc) - timedelta(hours=23))
        state = types.SimpleNamespace(outbox={}, outbox_checked_at=0.0, headless_children=[])
        asyncio.run(pr._tend_outbox(state, _args(self.dir), self.logs.append, False))
        self.assertEqual(state.outbox, {})
        self.assertFalse(os.path.exists(os.path.join(self.dir, "outbound.jsonl")))


class StatusLine(unittest.TestCase):
    """`!status` is answered locally, without a warm session — so it still answers when the warm
    session IS the problem. Un-landed Notion writes belong there for exactly that reason."""

    def test_backlog_shows_up_in_the_status_reply(self):
        line = pr.render_status_reply({"session_up": False, "model": "claude-opus-5",
                                       "outbox_pending": 8, "outbox_oldest_sec": 23.4 * 3600})
        self.assertIn("8 un-landed Notion writes", line)
        self.assertIn("23.4 h", line)

    def test_dead_letters_show_up(self):
        line = pr.render_status_reply({"session_up": False, "outbox_pending": 0, "outbox_dead": 2})
        self.assertIn("2 dead-letters", line)

    def test_a_clean_outbox_adds_no_line(self):
        """A permanently-visible '0 pending' is a gauge the owner learns to stop reading."""
        line = pr.render_status_reply({"session_up": False, "outbox_pending": 0, "outbox_dead": 0})
        self.assertNotIn("Outbox", line)

    def test_an_old_snapshot_without_the_fields_still_renders(self):
        self.assertIn("Warm session", pr.render_status_reply({"session_up": False}))


if __name__ == "__main__":
    unittest.main()
