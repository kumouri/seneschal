#!/usr/bin/env python3
"""Tests for `owi_unknowns.py` — the open-work unknown-owner picker. Stdlib `unittest` only. **No
network, ever** — `ask()`/`on_answer()` are exercised with an injected `api` seam (the grid path, when
`telegram_ask` is installed) or an injected `send_text` seam (the plain-text fallback); a test that
reaches the real Bot API is a bug.

What's covered:
  * `count`/`batch` only ever see rows that are `whose_move: "unknown"`, non-terminal, and not
    already confirmed-Unknown — oldest first, stable order;
  * `apply` writes through `loops.update`'s `whose_move` — never a second writer — and a bad item id
    or unrecognized choice is reported per-item rather than aborting the whole batch;
  * an explicit `Unknown` choice is a resolution (drops out of future batches) even though the row's
    `whose_move` stays `"unknown"` in the register itself (its visibility never changes);
  * a row left untouched by `apply`'s resolution map is left alone;
  * `on_answer` applies, then sends the next batch unless stopped or the pool is empty;
  * the two TERMINAL choices: `Archived` lands `status: "abandoned"` through the OWNER-ONLY
    `loops.owner_abandon` (the owner's answer IS the owner acting), `Done` lands `status: "done"`
    through `loops.resolve`, neither touches `whose_move`, both drop the row from every later batch,
    and the `mirror` report names the store rows the owner should update themselves;
  * the plain-text fallback when `telegram_ask` is absent — one `send_text` call, the batch numbered
    with every choice label, dry-run sends nothing;
  * `stop`/`note_brief_sent` — stop is honoured by `ask`, and a fresh Brief clears both the stop and
    the first-real-message ask window;
  * `is_real_message`/`should_ask`/`mark_asked` — the first-real-message gate: fires at most once per
    Brief cycle, never on a reaction/edit/callback or a bare ack phrase.

Run:  python -m unittest discover -s seneschal/scripts -p "test_owi_unknowns.py"   (from the repo root)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from unittest import mock  # noqa: E402

import loops  # noqa: E402
import owi_unknowns as ou  # noqa: E402
import presence  # noqa: E402

NOW = datetime(2026, 1, 15, 20, 40, tzinfo=timezone.utc)

GOOD = dict(audience="owner", terminal_state="it ships or the owner drops it",
           whose_move="unknown", next_action="unknown", kind="unknown", priority="unknown")

NEEDS_PICKER = "the grid picker needs the optional telegram_ask module (not installed)"


class FakeApi:
    """Records every Bot API call, answers `sendMessage` with a fixed id."""

    def __init__(self, message_id=4242):
        self.calls = []
        self.message_id = message_id

    def __call__(self, c, method, params, timeout=30):
        self.calls.append((method, params))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": self.message_id}}
        return {"ok": True, "result": True}

    def methods(self):
        return [m for m, _ in self.calls]


class FakeSend:
    """The plain-text fallback's `send_text(text, env_file)` seam."""

    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, text, env_file):
        self.calls.append((text, env_file))
        return {"ok": self.ok} if self.ok else {"ok": False, "error": "boom"}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def add(self, text, now=NOW, **over):
        kwargs = dict(GOOD)
        kwargs.update(over)
        return loops.add(self.state, text=text, now=now, **kwargs)

    def send_kwargs(self):
        """Whichever transport seam is live: the grid's `api`, or the text fallback's `send_text`."""
        return {"api": FakeApi()} if ou.ta is not None else {"send_text": FakeSend()}


class CountAndBatch(Base):
    def test_count_is_zero_on_an_empty_store(self):
        self.assertEqual(ou.count(self.state), 0)

    def test_count_only_counts_unknown_whose_move(self):
        self.add("unknown one", whose_move="unknown")
        self.add("assigned already", whose_move="owner")
        self.assertEqual(ou.count(self.state), 1)

    def test_terminal_rows_never_count(self):
        item = self.add("resolved already", whose_move="unknown")
        loops.resolve(self.state, item_id=item["id"], because="shipped")
        self.assertEqual(ou.count(self.state), 0)

    def test_batch_is_oldest_first(self):
        first = self.add("older", now=NOW - timedelta(days=2))
        second = self.add("newer", now=NOW - timedelta(days=1))
        rows = ou.batch(self.state, n=5)
        self.assertEqual([r["id"] for r in rows], [first["id"], second["id"]])

    def test_batch_respects_n(self):
        for i in range(7):
            self.add(f"item {i}")
        self.assertEqual(len(ou.batch(self.state, n=5)), 5)
        self.assertEqual(len(ou.batch(self.state, n=100)), 7)

    def test_batch_is_read_only(self):
        self.add("item")
        before = loops.load(self.state)
        ou.batch(self.state, n=5)
        after = loops.load(self.state)
        self.assertEqual(before, after)

    def test_brief_line_renders_the_count_and_is_none_at_zero(self):
        self.assertIsNone(ou.brief_line(self.state))
        self.add("one")
        self.add("two")
        self.assertIn("2 items with no owner yet", ou.brief_line(self.state))


class Apply(Base):
    def test_apply_writes_through_loops_update(self):
        item = self.add("needs an owner")
        ou.apply(self.state, {item["id"]: "Owner"})
        got = loops.get(loops.load(self.state), item["id"])
        self.assertEqual(got["whose_move"], "owner")

    def test_apply_maps_every_choice(self):
        items = {label: self.add(label) for label in ("a", "b", "c", "d", "e")}
        resolution = {items["a"]["id"]: "Owner", items["b"]["id"]: "Assistant",
                     items["c"]["id"]: "Both", items["d"]["id"]: "External",
                     items["e"]["id"]: "Unknown"}
        ou.apply(self.state, resolution)
        store = loops.load(self.state)
        self.assertEqual(store["items"][items["a"]["id"]]["whose_move"], "owner")
        self.assertEqual(store["items"][items["b"]["id"]]["whose_move"], "assistant")
        self.assertEqual(store["items"][items["c"]["id"]]["whose_move"], "both")
        self.assertEqual(store["items"][items["d"]["id"]]["whose_move"], "external")
        self.assertEqual(store["items"][items["e"]["id"]]["whose_move"], "unknown")

    def test_an_unrecognized_choice_is_reported_not_raised(self):
        item = self.add("needs an owner")
        res = ou.apply(self.state, {item["id"]: "Nobody"})
        self.assertFalse(res["results"][item["id"]]["ok"])

    def test_a_bad_item_id_is_reported_not_raised(self):
        res = ou.apply(self.state, {"loop-does-not-exist": "Owner"})
        self.assertFalse(res["results"]["loop-does-not-exist"]["ok"])

    def test_one_bad_id_does_not_abort_the_rest(self):
        item = self.add("needs an owner")
        res = ou.apply(self.state, {"loop-nope": "Owner", item["id"]: "Assistant"})
        self.assertTrue(res["results"][item["id"]]["ok"])
        self.assertFalse(res["results"]["loop-nope"]["ok"])

    def test_untouched_items_are_left_alone(self):
        touched = self.add("touched")
        untouched = self.add("untouched")
        ou.apply(self.state, {touched["id"]: "Owner"})
        got = loops.get(loops.load(self.state), untouched["id"])
        self.assertEqual(got["whose_move"], "unknown")

    def test_explicit_unknown_drops_out_of_the_next_batch(self):
        """An explicit Unknown IS an answer — the register still reads whose_move: unknown (its
        visibility never changes), but this module stops re-offering it."""
        item = self.add("genuinely unclear")
        self.assertEqual(ou.count(self.state), 1)
        ou.apply(self.state, {item["id"]: "Unknown"})
        self.assertEqual(loops.get(loops.load(self.state), item["id"])["whose_move"], "unknown")
        self.assertEqual(ou.count(self.state), 0)
        self.assertEqual(ou.batch(self.state, n=5), [])

    def test_a_confirmed_unknown_that_later_gets_a_real_owner_re_enters_normally(self):
        item = self.add("first unclear, then assigned")
        ou.apply(self.state, {item["id"]: "Unknown"})
        ou.apply(self.state, {item["id"]: "Owner"})
        self.assertEqual(loops.get(loops.load(self.state), item["id"])["whose_move"], "owner")


class TerminalChoices(Base):
    """`Archived` / `Done` — the two terminal choices beside the five ownership choices."""

    def test_choices_keep_the_five_and_append_two(self):
        self.assertEqual(ou.CHOICES[:5], ["Owner", "Assistant", "Both", "External", "Unknown"])
        self.assertEqual(ou.CHOICES[5:], ["Archived", "Done"])
        self.assertEqual(ou.CHOICES, ou.OWNER_CHOICES + ou.TERMINAL_CHOICES)

    def test_archived_lands_abandoned_through_owner_abandon(self):
        item = self.add("a long-finished imported task")
        with mock.patch.object(loops, "owner_abandon", wraps=loops.owner_abandon) as ca, \
                mock.patch.object(loops, "update", wraps=loops.update) as up:
            res = ou.apply(self.state, {item["id"]: "Archived"}, now=NOW)
        self.assertTrue(res["results"][item["id"]]["ok"])
        self.assertEqual(ca.call_count, 1)
        self.assertEqual(up.call_count, 0)  # never a whose_move write, never a second writer
        got = loops.get(loops.load(self.state), item["id"])
        self.assertEqual(got["status"], "abandoned")
        self.assertIsNone(got.get("notes"))  # abandoned carries no reason — that absence is its content

    def test_done_lands_done_through_resolve(self):
        item = self.add("finished already")
        with mock.patch.object(loops, "resolve", wraps=loops.resolve) as rs, \
                mock.patch.object(loops, "owner_abandon", wraps=loops.owner_abandon) as ca:
            res = ou.apply(self.state, {item["id"]: "Done"}, now=NOW)
        self.assertTrue(res["results"][item["id"]]["ok"])
        self.assertEqual(rs.call_count, 1)
        self.assertEqual(ca.call_count, 0)
        self.assertEqual(loops.get(loops.load(self.state), item["id"])["status"], "done")

    def test_terminal_choices_leave_whose_move_untouched(self):
        a = self.add("archived one", whose_move="unknown")
        d = self.add("done one", whose_move="unknown")
        ou.apply(self.state, {a["id"]: "Archived", d["id"]: "Done"}, now=NOW)
        store = loops.load(self.state)
        self.assertEqual(store["items"][a["id"]]["whose_move"], "unknown")
        self.assertEqual(store["items"][d["id"]]["whose_move"], "unknown")

    def test_terminal_rows_never_re_batch(self):
        a = self.add("archived one")
        d = self.add("done one")
        live = self.add("still open")
        self.assertEqual(ou.count(self.state), 3)
        ou.apply(self.state, {a["id"]: "Archived", d["id"]: "Done"}, now=NOW)
        self.assertEqual(ou.count(self.state), 1)
        self.assertEqual([r["id"] for r in ou.batch(self.state, n=5)], [live["id"]])

    def test_a_terminal_tap_on_a_confirmed_unknown_clears_the_confirmed_store(self):
        item = self.add("confirmed then archived")
        ou.apply(self.state, {item["id"]: "Unknown"}, now=NOW)
        self.assertIn(item["id"], ou._load_confirmed(self.state))
        ou.apply(self.state, {item["id"]: "Archived"}, now=NOW)
        self.assertNotIn(item["id"], ou._load_confirmed(self.state))

    def test_a_terminal_tap_on_a_missing_id_is_reported_not_raised(self):
        res = ou.apply(self.state, {"loop-nope": "Archived"}, now=NOW)
        self.assertFalse(res["results"]["loop-nope"]["ok"])

    def test_mirror_names_the_store_rows_the_owner_should_update(self):
        a = self.add("archived one", renders_elsewhere="https://store.example/aaa")
        d = self.add("done one", renders_elsewhere="https://store.example/ddd")
        o = self.add("owner only")
        res = ou.apply(self.state, {a["id"]: "Archived", d["id"]: "Done", o["id"]: "Owner"}, now=NOW)
        self.assertEqual([(m["id"], m["choice"], m["renders_elsewhere"]) for m in res["mirror"]],
                         [(a["id"], "Archived", "https://store.example/aaa"),
                          (d["id"], "Done", "https://store.example/ddd")])
        line = ou.mirror_line(res["mirror"])
        self.assertIn("mirror in your data store yourself (2 rows)", line)
        self.assertIn("archived one → Archived: https://store.example/aaa", line)
        self.assertIn("done one → Done: https://store.example/ddd", line)

    def test_mirror_line_is_empty_when_nothing_renders_elsewhere(self):
        item = self.add("a PR row, no store page")
        res = ou.apply(self.state, {item["id"]: "Done"}, now=NOW)
        self.assertEqual(len(res["mirror"]), 1)  # recorded as terminal…
        self.assertEqual(ou.mirror_line(res["mirror"]), "")  # …but nothing for the owner to update

    def test_on_answer_carries_the_mirror_line(self):
        a = self.add("archived one", renders_elsewhere="https://store.example/aaa")
        self.add("next batch")
        res = ou.on_answer(self.state, None, {a["id"]: "Archived"}, now=NOW, **self.send_kwargs())
        self.assertIn("https://store.example/aaa", res["mirror"])
        self.assertTrue(res["next_sent"])

    @unittest.skipIf(ou.ta is None, NEEDS_PICKER)
    def test_ask_sends_all_seven_choices(self):
        self.add("needs an owner")
        api = FakeApi()
        res = ou.ask(self.state, None, api=api, now=NOW, dry_run=True)
        flat = [t for row in res["rows"] for t in row]
        for label in ou.CHOICES:
            self.assertIn(label, flat)


@unittest.skipUnless(hasattr(presence, "_owi_unknowns_clause"), "presence wiring lands in wave 26")
class DaemonReportLine(Base):
    """`presence._owi_unknowns_clause` — the answered-batch continuation's report line carries
    `mirror` so the assistant can tell the owner which store rows to update themselves. Driven with
    the subprocess FAKED at `subprocess.run` (the clause's one seam), never a real child."""

    def _clause(self, out: dict, answers: dict) -> str:
        class Args:
            state_dir = self.state
            telegram_env = None

        class Proc:
            stdout = json.dumps(out)
            returncode = 0
        res = {"ok": True, "answered": True, "question_id": "q1", "grid_answers": answers,
               "meta": {"kind": ou.ASK_META_KIND, "batch": len(answers)}}
        with mock.patch.object(presence.subprocess, "run", return_value=Proc()):
            return presence._owi_unknowns_clause(Args(), res, lambda line: None)

    def test_mirror_is_appended_when_present(self):
        line = self._clause({"ok": True, "remaining": 3, "next_sent": True,
                             "mirror": "mirror in your data store yourself (1 row): x → Archived: "
                                       "https://n/x"},
                            {"loop-a": "Archived"})
        self.assertIn("sent the next batch of 5 — mirror in your data store yourself (1 row): "
                      "x → Archived: https://n/x]", line)

    def test_no_mirror_means_the_old_line_byte_for_byte(self):
        line = self._clause({"ok": True, "remaining": 0, "next_sent": False, "mirror": ""},
                            {"loop-a": "Owner"})
        self.assertEqual(line, "[recorded 1 owner assignment(s); no more unknowns queued right now]")

    def test_not_an_owi_tap_is_inert(self):
        class Args:
            state_dir = self.state
            telegram_env = None
        res = {"ok": True, "answered": True, "meta": {"kind": "merge-approval"}}
        self.assertEqual(presence._owi_unknowns_clause(Args(), res, lambda line: None), "")


@unittest.skipIf(ou.ta is None, NEEDS_PICKER)
class AskGrid(Base):
    def test_ask_sends_the_next_batch(self):
        self.add("needs an owner")
        api = FakeApi()
        res = ou.ask(self.state, None, api=api, now=NOW)
        self.assertTrue(res["sent"])
        self.assertEqual(res["items"], 1)
        self.assertIn("sendMessage", api.methods())

    def test_ask_with_nothing_to_send_does_not_send(self):
        api = FakeApi()
        res = ou.ask(self.state, None, api=api, now=NOW)
        self.assertFalse(res["sent"])
        self.assertEqual(api.calls, [])

    def test_ask_honors_stop(self):
        self.add("needs an owner")
        ou.stop(self.state, now=NOW)
        api = FakeApi()
        res = ou.ask(self.state, None, api=api, now=NOW)
        self.assertFalse(res["sent"])
        self.assertEqual(res["reason"], "stopped — waiting for the next Brief")
        self.assertEqual(api.calls, [])

    def test_ask_dry_run_sends_nothing(self):
        self.add("needs an owner")
        api = FakeApi()
        res = ou.ask(self.state, None, api=api, now=NOW, dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(api.calls, [])


class AskTextFallback(Base):
    """With `telegram_ask` absent, `ask` degrades to ONE plain-text message — forced here by patching
    `ou.ta` to `None` so the fallback is covered whether or not the module is installed."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(ou, "ta", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ask_sends_one_numbered_text_message_with_every_choice(self):
        first = self.add("first thing", now=NOW - timedelta(days=1))
        self.add("second thing")
        send = FakeSend()
        res = ou.ask(self.state, "tg.env", now=NOW, send_text=send)
        self.assertTrue(res["sent"])
        self.assertEqual(res["mode"], "text")
        self.assertEqual(res["items"], 2)
        self.assertEqual(len(send.calls), 1)
        text, env = send.calls[0]
        self.assertEqual(env, "tg.env")
        self.assertIn("1. first thing", text)
        self.assertIn("2. second thing", text)
        for label in ou.CHOICES:
            self.assertIn(label, text)
        self.assertEqual(ou._load_cursor(self.state)["last_batch_ids"][0], first["id"])

    def test_dry_run_sends_nothing_and_returns_the_body(self):
        self.add("needs an owner")
        send = FakeSend()
        res = ou.ask(self.state, None, now=NOW, dry_run=True, send_text=send)
        self.assertTrue(res["dry_run"])
        self.assertIn("1. needs an owner", res["body"])
        self.assertEqual(send.calls, [])

    def test_a_failed_send_is_reported_and_leaves_the_cursor_alone(self):
        self.add("needs an owner")
        res = ou.ask(self.state, None, now=NOW, send_text=FakeSend(ok=False))
        self.assertFalse(res["ok"])
        self.assertFalse(res["sent"])
        self.assertEqual(ou._load_cursor(self.state)["last_batch_ids"], [])

    def test_ask_honors_stop(self):
        self.add("needs an owner")
        ou.stop(self.state, now=NOW)
        send = FakeSend()
        res = ou.ask(self.state, None, now=NOW, send_text=send)
        self.assertFalse(res["sent"])
        self.assertEqual(send.calls, [])


class OnAnswer(Base):
    def test_applies_then_sends_the_next_batch(self):
        first = self.add("first")
        second = self.add("second")
        res = ou.on_answer(self.state, None, {first["id"]: "Owner"}, now=NOW, **self.send_kwargs())
        self.assertTrue(res["applied"]["results"][first["id"]]["ok"])
        self.assertTrue(res["next_sent"])
        self.assertEqual(res["remaining"], 1)
        self.assertEqual(loops.get(loops.load(self.state), second["id"])["whose_move"], "unknown")

    def test_stops_sending_once_the_pool_is_empty(self):
        item = self.add("only one")
        res = ou.on_answer(self.state, None, {item["id"]: "Owner"}, now=NOW, **self.send_kwargs())
        self.assertFalse(res["next_sent"])
        self.assertEqual(res["remaining"], 0)

    def test_stops_sending_once_the_owner_says_stop(self):
        first = self.add("first")
        self.add("second")
        ou.stop(self.state, now=NOW)
        res = ou.on_answer(self.state, None, {first["id"]: "Owner"}, now=NOW, **self.send_kwargs())
        self.assertFalse(res["next_sent"])
        self.assertGreater(res["remaining"], 0)


class StopAndBriefReset(Base):
    def test_note_brief_sent_clears_a_prior_stop(self):
        ou.stop(self.state, now=NOW)
        ou.note_brief_sent(self.state, now=NOW)
        self.add("needs an owner")
        res = ou.ask(self.state, None, now=NOW, **self.send_kwargs())
        self.assertTrue(res["sent"])

    def test_note_brief_sent_resets_asked_at(self):
        ou.note_brief_sent(self.state, now=NOW)
        self.add("needs an owner")
        self.assertTrue(ou.should_ask(self.state))
        ou.mark_asked(self.state, now=NOW)
        self.assertFalse(ou.should_ask(self.state))
        ou.note_brief_sent(self.state, now=NOW + timedelta(hours=24))
        self.assertTrue(ou.should_ask(self.state))


class FirstRealMessageGate(Base):
    def test_should_ask_is_false_before_any_brief(self):
        self.add("needs an owner")
        self.assertFalse(ou.should_ask(self.state))

    def test_should_ask_is_false_with_nothing_to_ask_about(self):
        ou.note_brief_sent(self.state, now=NOW)
        self.assertFalse(ou.should_ask(self.state))

    def test_should_ask_is_true_after_a_brief_with_unknowns_pending(self):
        ou.note_brief_sent(self.state, now=NOW)
        self.add("needs an owner")
        self.assertTrue(ou.should_ask(self.state))

    def test_mark_asked_fires_at_most_once_per_cycle(self):
        ou.note_brief_sent(self.state, now=NOW)
        self.add("needs an owner")
        self.assertTrue(ou.should_ask(self.state))
        ou.mark_asked(self.state, now=NOW)
        self.assertFalse(ou.should_ask(self.state))

    def test_is_real_message_excludes_reaction_edit_and_callback(self):
        for kind in ("reaction", "edit", "callback"):
            self.assertFalse(ou.is_real_message({"kind": kind, "text": "hi"}))

    def test_is_real_message_excludes_bare_acks(self):
        for phrase in ("done", "Done!", "took'em", "ok", "yep", "👍"):
            self.assertFalse(ou.is_real_message({"text": phrase}), phrase)

    def test_is_real_message_excludes_empty_text(self):
        self.assertFalse(ou.is_real_message({"text": ""}))
        self.assertFalse(ou.is_real_message({}))

    def test_is_real_message_accepts_ordinary_text(self):
        self.assertTrue(ou.is_real_message({"text": "hey can you check something for me"}))

    def test_a_message_that_merely_contains_an_ack_word_still_counts(self):
        """The exclusion is against the WHOLE message, not a substring — "done with the thing" is a
        real message that happens to contain the word "done"."""
        self.assertTrue(ou.is_real_message({"text": "done with the thing, what's next"}))


if __name__ == "__main__":
    unittest.main()
