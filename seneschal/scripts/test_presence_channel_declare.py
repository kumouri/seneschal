#!/usr/bin/env python3
"""Tests for the reply-path channel-declaration forcing function's wiring into `presence.py`'s
`drainer_task` — `seneschal/docs/message-routing-spec.md` §7 (phase 2: the declared purpose routes), AND its
generalization to a second required line, `seneschal/docs/reply-marker-forcing-function-spec.md`. The
pure parse/strip/resolve/classify logic (both lines) is tested in `test_channel_declare.py`; this
file is about the one thing that module cannot exercise on its own: **the retry loop's exit paths,
and the guarantee that none of them can lose the substantive reply — now for two required lines
sharing ONE retry budget, never two.**

What's covered, each tied to a named clause of the spec's hard rail (§3):

  * **declared-first-try** — the model complies immediately; exactly one `send()` call.
  * **declared-after-retry(1)** — the model omits it once, the corrective ask fixes it; exactly two
    `send()` calls, and the SUBSTANTIVE reply (obtained on the first call) is what gets delivered —
    never the corrective's own bare marker line.
  * **the dead corrective ask** (`corrective is None`) — the warm session hangs or dies answering the
    *channel-check itself*. This must never be read as the substantive reply having failed: `reply`
    was already obtained before the retry ever ran, and it is still what reaches the owner.
  * **a session that died on the SUBSTANTIVE turn** — the retry loop must not even attempt to ask a
    dead session a corrective question (`state.session is not None` gates it), and the synthesized
    apology still gets a log row (`defaulted-main`) since it, too, is a Telegram reply.
  * **non-Telegram channels never retry** — no routing decision exists to retry for on Discord/the
    cockpit (§3 scope); the marker is still stripped unconditionally (§1) if one somehow appears.
  * **a logging failure never costs the reply** — `record_outcome`'s own fail-open contract, exercised
    end to end through the drainer rather than just unit-tested in isolation.
  * **routing follows the declared purpose** — `deliver_reply`'s destination is the resolved,
    declared purpose, unconditionally winning over the inbound thread; a resolution failure falls
    back to the inbound thread, then main; Discord/the cockpit never receive a `channel_purpose` at
    all.

Also covers the GROUNDING/RESUME_PREAMBLE template wiring: the instruction paragraph and the
per-turn topic line are Telegram-only, and the topic line is refreshed on a WARM turn exactly the
way the existing clock line already is (§8 fork 4).

Offline throughout: `--stub-send` plus a temp dir, so nothing here can reach a real Telegram chat.

Run:  python -m unittest seneschal.scripts.test_presence_channel_declare
  (or) python test_presence_channel_declare.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import channel_declare as cd  # noqa: E402
import presence as pr  # noqa: E402
import telegram_topics as tt  # noqa: E402


def _args(state_dir, **over):
    base = dict(state_dir=state_dir, telegram_env="unused.env", call_env=None, discord_env=None,
                no_discord=True, no_discord_gateway=False, stub_brain=True, stub_send=True,
                fake_inbox=None, router_mode="off",
                no_reminders=True, no_peek=True, no_slots=True, max_iterations=0,
                poll_timeout=1, discord_poll_sec=0.05, tick_sec=0.05, idle_min=0.0,
                model=None, slot_model=None, slot_catchup_min=180, claude_bin="claude",
                notion_mcp=None, slack_mcp=None, permission_mode="bypassPermissions",
                peek_interval_min=0, watch_prompt=None, watch_cmd=None, watch_model=None)
    base.update(over)
    return argparse.Namespace(**base)


def _sent(state_dir):
    path = os.path.join(state_dir, "sent.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().splitlines() if line.strip()]


def _log_rows(state_dir):
    path = os.path.join(state_dir, cd.CHANNEL_DECLARE_LOG_FILENAME)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh.read().splitlines() if line.strip()]


class _ScriptedSession(pr.StubWarmSession):
    """Returns one scripted raw reply per `.send()` call, in order. An entry of `None` means the
    session died answering that call (mirrors a real `WarmSession` producing no result event) —
    the caller sees `None` back, exactly like a hang or a crash. Running out of scripted replies is
    a test-authoring error, not a thing this fixture should paper over."""

    def __init__(self, *a, replies=(), **kw):
        super().__init__(*a, **kw)
        self._replies = list(replies)
        self.sent_texts: list = []

    def send(self, text, on_event=None, cold_retry_text=None):
        self.sent_texts.append(text)
        self.turns += 1
        self.turns_served += 1
        reply = self._replies.pop(0)
        if reply is not None and on_event is not None:
            try:
                on_event({"type": "result", "is_error": False, "result": reply,
                          "usage": {"input_tokens": 1, "output_tokens": 1}, "num_turns": 1})
            except Exception:  # noqa: BLE001
                pass
        return reply


async def _run_drainer_until(state, args, make_session, cond, timeout=5.0):
    task = asyncio.ensure_future(pr.drainer_task(state, args, lambda *_: None, make_session, 600.0))
    try:
        async with asyncio.timeout(timeout):
            while not cond():
                await asyncio.sleep(0.01)
    finally:
        state.stop.set()
        state.pending_event.set()
        await asyncio.wait_for(task, timeout=5.0)


class RetryLoopExitPaths(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.args = _args(self.dir)

    async def test_declared_first_try(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "what's the build status", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["[[channel:pull-requests]]\n"
                                             "*(answering your build status question)*\n"
                                             "Here's the build status."])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 1, "no retry when both lines are there first try")
        sent = _sent(self.dir)
        # The channel line is stripped; the reply-marker line is NOT (§2) — it reaches the owner verbatim.
        self.assertEqual(sent[0]["text"],
                         "*(answering your build status question)*\nHere's the build status.")
        self.assertNotIn("[[channel:", sent[0]["text"])
        rows = _log_rows(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "declared-first-try")
        self.assertEqual(rows[0]["declared_purpose"], "pull-requests")
        self.assertEqual(rows[0]["resolved_purpose"], "pull-requests")
        self.assertEqual(rows[0]["retries_used"], 0)
        self.assertIs(rows[0]["reply_marker_required"], True)
        self.assertIs(rows[0]["reply_marker_present"], True)
        self.assertEqual(rows[0]["reply_marker_retries_used"], 0)

    async def test_declared_after_one_retry_channel_only(self):
        """The marker is already present (so only the channel is missing) — the retry prompt stays
        the UNCHANGED single-line channel-only corrective, exactly as before the marker existed."""
        state = pr.DaemonState()
        state.pending = [("telegram", "log the meal", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["*(answering your meal log)*\nlogged — 410 kcal",
                                             "[[channel:decisions]]\n"])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 2)
        self.assertEqual(session.sent_texts[1],
                         cd.retry_prompt(missing_channel=True, missing_marker=False))
        sent = _sent(self.dir)
        # The SUBSTANTIVE reply (obtained on the FIRST call) is what the owner gets — never the corrective
        # ask's own bare channel line — and the marker it already carried is untouched.
        self.assertEqual(sent[0]["text"], "*(answering your meal log)*\nlogged — 410 kcal")
        rows = _log_rows(self.dir)
        self.assertEqual(rows[0]["outcome"], "declared-after-retry(1)")
        self.assertEqual(rows[0]["declared_purpose"], "decisions")
        self.assertEqual(rows[0]["resolved_purpose"], "decisions")
        self.assertEqual(rows[0]["retries_used"], 1)
        self.assertIs(rows[0]["reply_marker_present"], True)
        self.assertEqual(rows[0]["reply_marker_retries_used"], 0, "the marker never needed a retry")

    async def test_marker_only_missing_prepends_the_recovered_line(self):
        """The channel is fine first try; only the marker is missing. §3/§6's key property: the
        retry that happens must NOT read as a channel-declaration rescue — `retries_used` (channel)
        stays 0 and `outcome` stays `declared-first-try` even though a retry genuinely occurred,
        because it was spent entirely on the marker."""
        state = pr.DaemonState()
        state.pending = [("telegram", "plain question", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["[[channel:main]]\nplain reply, no marker",
                                             "*(answering the plain reply)*"])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 2)
        self.assertEqual(session.sent_texts[1],
                         cd.retry_prompt(missing_channel=False, missing_marker=True))
        sent = _sent(self.dir)
        # The corrective's own text is what gets PREPENDED — the one case a corrective's text reaches
        # delivery (module docstring §2).
        self.assertEqual(sent[0]["text"], "*(answering the plain reply)*\nplain reply, no marker")
        rows = _log_rows(self.dir)
        self.assertEqual(rows[0]["declared_purpose"], "main")
        self.assertEqual(rows[0]["retries_used"], 0, "the channel was never missing — no channel rescue")
        self.assertEqual(rows[0]["outcome"], "declared-first-try")
        self.assertIs(rows[0]["reply_marker_present"], True)
        self.assertEqual(rows[0]["reply_marker_retries_used"], 1)

    async def test_both_missing_costs_one_round_trip_not_two(self):
        """§3's hard constraint: a reply missing BOTH required lines gets ONE bounce naming both,
        never two round trips."""
        state = pr.DaemonState()
        state.pending = [("telegram", "log the meal", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["logged — 410 kcal",
                                             "[[channel:decisions]]\n*(answering your meal log)*"])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 2, "ONE combined retry, not two round trips")
        self.assertEqual(session.sent_texts[1],
                         cd.retry_prompt(missing_channel=True, missing_marker=True))
        sent = _sent(self.dir)
        self.assertEqual(sent[0]["text"], "*(answering your meal log)*\nlogged — 410 kcal")
        rows = _log_rows(self.dir)
        self.assertEqual(rows[0]["outcome"], "declared-after-retry(1)")
        self.assertEqual(rows[0]["declared_purpose"], "decisions")
        self.assertEqual(rows[0]["retries_used"], 1)
        self.assertIs(rows[0]["reply_marker_present"], True)
        self.assertEqual(rows[0]["reply_marker_retries_used"], 1)

    async def test_a_stray_control_line_buried_in_the_original_never_duplicates(self):
        """The failure shape, reproduced: the original reply's OWN attempt at the two control lines is
        buried mid-message (narration first, so position-0 parsing calls both "missing"), and the
        retry's freshly recovered marker must REPLACE that stray attempt on delivery, never stack
        beside it — exactly one marker line, no raw `[[channel:` token, reaching the owner."""
        state = pr.DaemonState()
        state.pending = [("telegram", "is the flake fixed", 0)]
        state.pending_event.set()
        original = ('Both jobs still running. Now the reply.\n\n[[channel:main]]\n'
                    '*(answering "that one\'s a flake we can\'t fix")*\n\n'
                    '**You\'re right on the definition …**')
        session = _ScriptedSession(replies=[original,
                                             "[[channel:main]]\n*(answering \"that one's a flake "
                                             "we can't fix\")*"])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 2, "both lines read missing at position 0 — one retry")
        sent = _sent(self.dir)
        self.assertEqual(len(sent), 1)
        delivered = sent[0]["text"]
        self.assertEqual(delivered.count("*(answering"), 1, "exactly one marker line, never two")
        self.assertNotIn("[[channel:", delivered, "the stray, orphaned token must not reach the owner")
        self.assertIn("Both jobs still running.", delivered)
        self.assertIn("You're right on the definition", delivered)
        rows = _log_rows(self.dir)
        self.assertEqual(rows[0]["declared_purpose"], "main")
        self.assertIs(rows[0]["reply_marker_present"], True)

    async def test_fuzzy_matched_is_logged_distinctly(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "typo the topic", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["[[channel:pullrequests]]\n*(answering your typo test)*\n"
                                             "reply text"])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 1)  # a declaration was made — no retry needed
        rows = _log_rows(self.dir)
        self.assertEqual(rows[0]["declared_purpose"], "pullrequests")
        self.assertEqual(rows[0]["resolved_purpose"], "pull-requests")
        self.assertEqual(rows[0]["outcome"], "fuzzy-matched(pullrequests->pull-requests)")
        self.assertIs(rows[0]["reply_marker_present"], True)

    async def test_dead_corrective_ask_does_not_lose_the_substantive_reply(self):
        """The rail's own hardest case: the channel-check itself hangs/dies. `reply` was already
        obtained before the retry ran and must still be delivered — a failure here is scoped to the
        channel/marker question alone, never to the answer."""
        state = pr.DaemonState()
        state.pending = [("telegram", "urgent question", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["the real answer, undeclared", None])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 2)  # the retry WAS attempted
        sent = _sent(self.dir)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["text"], "the real answer, undeclared")  # never lost, never touched
        rows = _log_rows(self.dir)
        self.assertEqual(rows[0]["outcome"], "defaulted-main")
        self.assertEqual(rows[0]["declared_purpose"], None)
        self.assertEqual(rows[0]["resolved_purpose"], tt.TOPIC_MAIN_CHAT)
        self.assertEqual(rows[0]["retries_used"], 1)
        self.assertIs(rows[0]["reply_marker_present"], False)
        self.assertEqual(rows[0]["reply_marker_retries_used"], 1)

    async def test_retries_exhausted_defaults_to_main_and_still_delivers(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "keeps forgetting", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["undeclared answer", "still no marker"])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 1 + cd.CHANNEL_DECLARE_MAX_RETRIES)
        sent = _sent(self.dir)
        self.assertEqual(sent[0]["text"], "undeclared answer")
        rows = _log_rows(self.dir)
        self.assertEqual(rows[0]["outcome"], "defaulted-main")
        self.assertEqual(rows[0]["retries_used"], cd.CHANNEL_DECLARE_MAX_RETRIES)
        self.assertIs(rows[0]["reply_marker_present"], False)
        self.assertEqual(rows[0]["reply_marker_retries_used"], cd.CHANNEL_DECLARE_MAX_RETRIES)

    async def test_a_session_dead_on_the_substantive_turn_never_gets_asked_a_corrective(self):
        """The OTHER dead-session case: the substantive send itself returns None. `state.session` is
        already `None` by the time the declare logic runs (the existing mid-turn-death handling), so
        the retry loop must not even try — asking a `None` session would crash it, not fail open."""
        state = pr.DaemonState()
        state.pending = [("telegram", "hello?", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=[None])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 1, "no corrective attempted on a dead session")
        sent = _sent(self.dir)
        self.assertIn("hit a snag", sent[0]["text"])
        rows = _log_rows(self.dir)
        self.assertEqual(len(rows), 1, "the synthesized apology is still a Telegram reply and is logged")
        self.assertEqual(rows[0]["outcome"], "defaulted-main")
        self.assertEqual(rows[0]["retries_used"], 0)
        self.assertIs(rows[0]["reply_marker_present"], False)
        self.assertEqual(rows[0]["reply_marker_retries_used"], 0)

    async def test_empty_reply_sends_nothing_and_is_not_retried(self):
        """The bug this guards: an empty `session.send()` result used to sail past `reply is None`,
        get read as missing BOTH required lines, and come back out as a bare corrective marker line
        with nothing behind it. The decision: send NOTHING, don't retry the machinery,
        don't apologize — and don't write a channel-declare-log row claiming an outcome for a reply
        that was never sent."""
        state = pr.DaemonState()
        state.pending = [("telegram", "did the thing get done", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=[""])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 1, "no corrective retry spent on an empty reply")
        self.assertEqual(_sent(self.dir), [], "nothing was sent to Telegram")
        self.assertEqual(_log_rows(self.dir), [],
                          "no channel-declare-log row — this turn asked no routing question")

    async def test_whitespace_only_reply_counts_as_empty(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "quick check", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["   \n\t  "])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 1, "no corrective retry spent on whitespace")
        self.assertEqual(_sent(self.dir), [], "nothing was sent to Telegram")
        self.assertEqual(_log_rows(self.dir), [])

    async def test_empty_reply_is_not_scoped_to_telegram(self):
        """The decision is general — an empty reply on Discord ships nothing either, even though the
        channel/marker machinery below never runs for Discord regardless."""
        state = pr.DaemonState()
        state.pending = [("discord", "anyone there", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=[""])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 1)
        self.assertEqual(_sent(self.dir), [], "nothing was sent to Discord")

    async def test_non_telegram_channel_never_retries_or_logs(self):
        state = pr.DaemonState()
        state.pending = [("discord", "no declaration here", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["plain reply, no marker at all"])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)

        self.assertEqual(len(session.sent_texts), 1, "no routing decision to retry for on discord")
        sent = _sent(self.dir)
        self.assertEqual(sent[0]["text"], "plain reply, no marker at all")
        self.assertEqual(_log_rows(self.dir), [], "phase 1 logs Telegram replies only")

    async def test_a_stray_marker_is_stripped_on_a_non_telegram_channel_too(self):
        """§1 is unconditional on every channel — a marker must never leak even where the retry
        escalation (§3) does not apply."""
        state = pr.DaemonState()
        state.pending = [("cockpit", "hi", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["[[channel:main]]\nhello from the cockpit"])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)
        self.assertEqual(len(session.sent_texts), 1)

    async def test_logging_failure_never_costs_the_reply(self):
        state = pr.DaemonState()
        state.pending = [("telegram", "log me if you can", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["[[channel:main]]\n*(answering whatever you need)*\n"
                                             "fine either way"])
        with mock.patch("stateio.append_jsonl", side_effect=OSError("disk full")):
            await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)
        sent = _sent(self.dir)
        self.assertEqual(sent[0]["text"], "*(answering whatever you need)*\nfine either way")
        self.assertEqual(_log_rows(self.dir), [], "the append itself failed, as scripted")

    async def test_routing_follows_the_declared_purpose_not_the_inbound_thread(self):
        """Phase 2: the declared, resolved purpose WINS over the inbound thread
        — replying from inside a topic does not auto-satisfy the requirement, so a turn that arrived
        via thread 12345 but declares `pull-requests` (with an already-known thread for it) is routed to
        THAT thread, not the inbound one."""
        tt.save_state(self.dir, {"topics": {"pull-requests": {"message_thread_id": 777}}})
        state = pr.DaemonState()
        state.pending = [("telegram", "reply from main, declares pull-requests", 0, 12345)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["[[channel:pull-requests]]\n"
                                             "*(answering an unrelated question)*\n"
                                             "unrelated to the build"])
        with mock.patch.object(pr, "deliver_reply_result", wraps=pr.deliver_reply_result) as spy:
            await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)
        delivered_text = "*(answering an unrelated question)*\nunrelated to the build"
        delivered_calls = [c for c in spy.call_args_list if c.args and c.args[1] == delivered_text]
        self.assertTrue(delivered_calls)
        # The call still carries the inbound topic (12345, the fallback signal) AND the declared
        # purpose — but the ACTUAL destination (both the stub-send row and the returned "thread") is
        # pull-requests' own thread, 777, not the inbound one.
        self.assertEqual(delivered_calls[0].kwargs.get("topic"), 12345)
        self.assertEqual(delivered_calls[0].kwargs.get("channel_purpose"), "pull-requests")
        sent = _sent(self.dir)
        self.assertEqual(sent[0]["topic"], 777)

    async def test_declared_main_from_inside_a_topic_goes_to_the_main_chat(self):
        """The inbound-thread interaction, argued not assumed (spec's own section): replying from
        inside a topic does NOT exempt a turn from declaring, and a turn that declares `main` from
        inside one goes to the main chat — never silently staying in the topic it arrived in."""
        state = pr.DaemonState()
        state.pending = [("telegram", "reply from pull-requests, declares main", 0, 12345)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["[[channel:main]]\n"
                                             "*(answering from inside a topic)*\n"
                                             "this belongs in the main chat"])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)
        sent = _sent(self.dir)
        self.assertNotIn("topic", sent[0], "declared main means no thread, regardless of the inbound one")

    async def test_purpose_resolution_failure_falls_back_to_the_inbound_thread_then_main(self):
        """A declared purpose with no already-known thread (topics off, unreadable state, or simply
        never created) is a resolution failure, not a routing decision the message may be lost to —
        it falls back to the INBOUND thread first, then main, exactly as `deliver_reply_result`'s own
        docstring promises. No `telegram-topics.json` is seeded here, so `decisions` cannot resolve."""
        state = pr.DaemonState()
        state.pending = [("telegram", "declares decisions, unresolvable", 0, 12345)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["[[channel:decisions]]\n"
                                             "*(answering your question)*\n"
                                             "here you go"])
        await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)
        sent = _sent(self.dir)
        self.assertEqual(sent[0]["topic"], 12345, "resolution failed — the inbound thread is used")

    async def test_discord_never_gets_a_channel_purpose(self):
        """Discord/cockpit paths stay byte-for-byte unchanged (spec scope): `channel_purpose` is only
        ever computed for a Telegram turn, so a Discord reply's `deliver_reply_result` call always
        carries `channel_purpose=None`, no matter what the (unretried, unresolved) reply contains."""
        state = pr.DaemonState()
        state.pending = [("discord", "no declaration here", 0)]
        state.pending_event.set()
        session = _ScriptedSession(replies=["plain reply, no marker at all"])
        with mock.patch.object(pr, "deliver_reply_result", wraps=pr.deliver_reply_result) as spy:
            await _run_drainer_until(state, self.args, lambda: session, lambda: not state.pending)
        delivered_calls = [c for c in spy.call_args_list
                           if c.args and c.args[1] == "plain reply, no marker at all"]
        self.assertTrue(delivered_calls)
        self.assertIsNone(delivered_calls[0].kwargs.get("channel_purpose"))
        self.assertEqual(_sent(self.dir)[0]["text"], "plain reply, no marker at all")


class GroundingTemplateWiring(unittest.TestCase):
    """The GROUNDING/RESUME_PREAMBLE template plumbing — Telegram-only, refreshed every turn."""

    def _grounding(self, **over):
        base = dict(channel="Telegram", now="2026-09-03 10:00", read_first="", thread="",
                    channel_declare_instruction="", current_topic_line="", msg="hey")
        base.update(over)
        return pr.GROUNDING.format(**base)

    def test_instruction_paragraph_names_the_format_and_main(self):
        instruction = cd.grounding_instruction()
        self.assertIn("[[channel:PURPOSE]]", instruction)
        self.assertIn("main", instruction)
        self.assertIn("pull-requests", instruction)  # a real, shipped topic name

    def test_instruction_paragraph_also_names_the_reply_marker(self):
        # reply-marker-forcing-function-spec.md §5 — same slot, no new template placeholder.
        instruction = cd.grounding_instruction()
        self.assertIn("*(answering", instruction)

    def test_current_topic_line_is_always_non_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(cd.current_topic_line(tmp, None).strip())
            self.assertTrue(cd.current_topic_line(tmp, 999999).strip())

    def test_cold_prompt_carries_both_before_the_owners_message(self):
        prompt = self._grounding(channel_declare_instruction=cd.grounding_instruction(),
                                 current_topic_line=cd.current_topic_line(tempfile.mkdtemp(), None))
        self.assertIn("[[channel:PURPOSE]]", prompt)
        self.assertIn("main chat", prompt)
        self.assertIn("*(answering", prompt)
        self.assertLess(prompt.index("[[channel:PURPOSE]]"), prompt.index("The owner just said:"))
        self.assertLess(prompt.index("main chat"), prompt.index("The owner just said:"))
        self.assertLess(prompt.index("*(answering"), prompt.index("The owner just said:"))

    def test_non_telegram_cold_prompt_carries_neither(self):
        prompt = self._grounding(channel="Discord")
        self.assertNotIn("[[channel:PURPOSE]]", prompt)

    def test_resume_preamble_carries_the_topic_line(self):
        prompt = pr.RESUME_PREAMBLE.format(now="N", msg="hey",
                                           current_topic_line="(This message arrived in Telegram's "
                                                              "main chat — no named topic.)")
        self.assertIn("no named topic", prompt)
        self.assertLess(prompt.index("no named topic"), prompt.index("The owner just said:"))

    def test_resume_preamble_empty_topic_line_is_byte_identical_to_before(self):
        with_line = pr.RESUME_PREAMBLE.format(now="N", msg="hey", current_topic_line="")
        self.assertIn("The owner just said: hey", with_line)


if __name__ == "__main__":
    unittest.main()
