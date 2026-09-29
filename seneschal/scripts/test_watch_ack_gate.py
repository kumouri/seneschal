#!/usr/bin/env python3
"""Tests for the **Watch-peek gate predicates** in `reminders_acks` — the reminder-ack half
(`watch_escalation_blocked`), the verdict log (`log_watch_gate`), and dedupe (`fact_key` /
`watch_duplicate_blocked`).

The defect these guard: a reminder row is acked, the ack lands everywhere (the store, `state/acks.json`,
the queue dequeues its re-nudges) — **the reminder queue behaves perfectly** — and then the headless
Watch comms peek wanders out of its email/Slack/calendar lane into the ⏰ Reminders domain, matches
`Importance` + `Nag Until Done`, and pushes *"… due 2 hours ago"* anyway, **without ever reading an
ack**. Two senders, one gate.

**Why every title input below is UGLY.** The gate once shipped green against a hand-tidied row title,
and then failed in production: `reminders.json` never holds the tidy title, it holds the whole nudge
sentence — *"<row> — it's due today (day 5). — ack in the store or tell me."* — whose boilerplate
tokens (`day`, `ack`, `store`) swamp the denominator until a real chase of an acked row scores 0.333
against a 0.6 knob. So `seed_queue` writes the template-shaped text a real queue holds, `seed_id_cache`
writes the row name the way `reminders-id-cache.md` really formats it, and
`TitleSourceIsTheRowNotTheNudge` pins real-shaped messages against those. A test here that hand-tidies
its title input is not testing the gate.

The other load-bearing asymmetry, and the one to argue with before "fixing": **fail-open on unknown,
fail-closed on acked.** A missed Super-Critical item costs incomparably more than a duplicate nudge, so
an unreadable ledger, an unknown title or a raise anywhere means the push goes out. Only a positive
ack-today reading suppresses. If a change here makes `FailOpen` red, the change is wrong.

**The clock rule for this file.** `NOW` is the only instant, and every date below is derived from it
through `ra.local_today` — the same function the gate itself calls — so the seeded ack and the runtime
lookup cannot disagree, in any timezone, on any date. Nothing here reads the wall clock.

The end-to-end cases drive `telegram_send.main` on the watch surface (`WiringIsWhatBinds` and the
classes after it), so the gate is pinned at the real chokepoint, not only as a predicate. The daemon's
peek stamp and the notion-outbox arm of the ack reading live with the modules they need.

Run:  python -m unittest test_watch_ack_gate   (from seneschal/scripts)
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import reminders_acks as ra  # noqa: E402
import telegram_send as ts  # noqa: E402
import watch_ack as wa  # noqa: E402
import watch_reconcile as wr  # noqa: E402

NOW = datetime(2026, 8, 7, 19, 30, 0, tzinfo=timezone.utc)
TODAY = ra.local_today(NOW)
YESTERDAY = ra.local_today(NOW.replace(day=6))  # a whole day earlier, so one local day earlier

# A synthetic ⏰ row and the real-SHAPED title sources a host holds for it: `QUEUE_TEXT` is what
# `reminders.json` holds (template and all); `ROW_TITLE` is the row's name as `reminders-id-cache.md`
# holds it. Nothing on a real host ever contains a tidied version of either.
RID = "00000000-0000-0000-0000-000000000001"
OTHER_RID = "00000000-0000-0000-0000-000000000002"
ROLL_RID = "00000000-0000-0000-0000-000000000003"
ROW_TITLE = "Orchid Fertilizer (feeding)"
QUEUE_TEXT = "Orchid fertilizer feeding — it's due today (day 5). — ack in the store or tell me."
#: A peek's chase of the row — names two of its three distinctive tokens.
PEEK_PUSH = ("🔴 Fertilizer feeding due 2 hours ago (12:30). Super-Critical plant care. "
             "Are you able to do it now? I can walk you through it if needed.")
#: A second chase, worded differently — the one a whole-sentence title scored at 0.333.
GATE_MISS_PUSH = ("Owner — fertilizer feeding is 3.5 hours overdue (due 12:30). "
                  "Do it now if you haven't. —Assistant")
#: Names the same row, chases nothing. Must always send — see `NAG_CUES`.
MENTION_ONLY = "Heads up: your orchid fertilizer refill is ready for pickup at the garden shop."


def _write(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


class GateFixture(unittest.TestCase):
    """A state dir holding the row's title (the fired queue entry) and, optionally, an ack."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def seed_queue(self, ack_gate=None, text=QUEUE_TEXT, reminder_id=RID):
        """A fired queue entry — **with the nudge template on it**, which is the only form
        `reminders.json` ever holds."""
        entry = {"id": "rmd-2026-08-07-orchid", "text": text, "reminder_id": reminder_id,
                 "due_at": "2026-08-07T17:30:00Z", "fired_at": "2026-08-07T17:30:04Z"}
        if ack_gate is not None:
            entry["ack_gate"] = ack_gate
        _write(os.path.join(self.dir, ra.REMINDERS_FILE), [entry])

    def seed_id_cache(self, title=ROW_TITLE, reminder_id=RID, cadence="Every 5 days"):
        """`state/reminders-id-cache.md` in its real Markdown-table shape, header and separator and
        all — the row's genuine name, which is the best title source a stdlib process has."""
        with open(os.path.join(self.dir, ra.ID_CACHE_FILE), "w", encoding="utf-8") as fh:
            fh.write(
                "# ⏰ Reminders — row-ID cache (live table)\n\n"
                "## Rows (page id ← Reminder)\n\n"
                "| Reminder | Page ID | Type | Window | Cadence | Importance |\n"
                "|----------|---------|------|--------|---------|------------|\n"
                f"| {title} | `{reminder_id}` | Recurring Habit | Midday | {cadence} "
                "| 🛑 Super-Critical |\n"
                f"| Water the plants (AM) | `{OTHER_RID}` | Recurring Habit "
                "| Morning | Daily | ⭐ High |\n")

    def seed_ledger(self, date=TODAY, reminder_id=RID):
        ra.record_ack(self.dir, reminder_id, date)

    def gate_log(self):
        path = os.path.join(self.dir, ra.WATCH_GATE_LOG)
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]


class TitleMatching(GateFixture):
    """Identity from prose: a Watch push carries no reminder id, so the row is recognised by title."""

    def test_the_real_push_covers_the_real_title(self):
        """Against the row's REAL name — 2 of {orchid, fertilizer, feeding} = 0.667."""
        for msg in (PEEK_PUSH, GATE_MISS_PUSH):
            self.assertAlmostEqual(ra.title_coverage(ROW_TITLE, msg), 2 / 3, places=3)
            self.assertGreaterEqual(ra.title_coverage(ROW_TITLE, msg), ra.TITLE_MATCH_MIN_COVERAGE)

    def test_emoji_and_punctuation_are_not_identity(self):
        self.assertEqual(ra.significant_tokens("🌸 Orchid Fertilizer (feeding)"),
                         {"orchid", "fertilizer", "feeding"})

    def test_a_title_of_only_stopwords_matches_nothing(self):
        # "Take it now" has no distinctive token; if it matched, it would gate every message.
        self.assertEqual(ra.title_coverage("Take it now", "anything at all"), 0.0)

    def test_a_text_of_only_template_strips_to_nothing_and_matches_nothing(self):
        """The stripper's own fail-safe: strip everything away and the title is unidentifiable, which
        `title_coverage` already reads as "never match" rather than "match everything"."""
        self.assertEqual(ra.strip_nudge_template("⏰ Reminder: — ack in the store or tell me."), "")
        self.assertEqual(ra.title_coverage(ra.strip_nudge_template("— ack in the store or tell me."),
                                           GATE_MISS_PUSH), 0.0)

    def test_unrelated_message_does_not_match(self):
        for title in (ROW_TITLE, ra.strip_nudge_template(QUEUE_TEXT)):
            self.assertLess(ra.title_coverage(title, "Standup moved to 10:30, per a colleague."),
                            ra.TITLE_MATCH_MIN_COVERAGE)

    def test_nudge_prefix_is_stripped_from_the_message_map(self):
        self.assertEqual(ra.strip_nudge_prefix("⏰ Reminder: " + ROW_TITLE), ROW_TITLE)

    def test_chase_cue_required_and_present_in_the_real_pushes(self):
        self.assertTrue(ra.message_chases(PEEK_PUSH))
        self.assertTrue(ra.message_chases(GATE_MISS_PUSH))
        self.assertFalse(ra.message_chases(MENTION_ONLY))
        self.assertFalse(ra.message_chases("Your orchid fertilizer refill is ready for pickup."))

    def test_title_learned_from_the_message_map_alone(self):
        """No queue entry at all — the row's name survives in what was actually sent. The map stores
        the delivered nudge, prefix and template included, exactly as the sender wrote it."""
        _write(os.path.join(self.dir, ra.MESSAGE_MAP_FILE),
               {"schema": "seneschal.telegram.message-map/1",
                "messages": {"41": {"kind": "nudge", "text": "⏰ Reminder: " + QUEUE_TEXT,
                                    "reminder_id": RID, "sent_at": "2026-08-07T17:30:04Z"}}})
        titles = ra.known_reminder_titles(self.dir)
        self.assertIn("Orchid fertilizer feeding", titles[ra.norm_key(RID)]["titles"])


class TitleSourceIsTheRowNotTheNudge(GateFixture):
    """**The whole-sentence regression.** Every title input here is what a host really stores. If a
    change makes these green only because a test tidied its input, it has re-shipped the bug."""

    def test_tokenizing_the_raw_nudge_sentence_scores_0333(self):
        """The measurement, pinned: tokenizing the queue text WHOLE — boilerplate included — is what
        scored 0.333 and sent."""
        as_whole = {"orchid", "fertilizer", "feeding", "day", "ack", "store"}
        overlap = as_whole & ra.significant_tokens(GATE_MISS_PUSH)
        self.assertEqual(overlap, {"fertilizer", "feeding"})
        self.assertAlmostEqual(len(overlap) / len(as_whole), 1 / 3, places=3)
        self.assertLess(1 / 3, ra.TITLE_MATCH_MIN_COVERAGE)  # ...which is why it sent

    def test_the_template_is_not_part_of_the_row_name(self):
        self.assertEqual(ra.strip_nudge_template(QUEUE_TEXT), "Orchid fertilizer feeding")
        self.assertEqual(ra.significant_tokens(ra.strip_nudge_template(QUEUE_TEXT)),
                         {"orchid", "fertilizer", "feeding"})

    def test_the_second_push_is_blocked_from_the_real_queue_text(self):
        """No id cache — the queue's own template-shaped text is the only title source, and it is
        enough."""
        self.seed_queue()
        self.seed_ledger()
        blocked = ra.watch_escalation_blocked(self.dir, GATE_MISS_PUSH, now=NOW)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["matched_by"], "title")
        self.assertEqual(blocked["matched_title"], "Orchid fertilizer feeding")
        self.assertGreaterEqual(blocked["coverage"], ra.TITLE_MATCH_MIN_COVERAGE)

    def test_the_first_push_is_blocked_from_the_real_queue_text(self):
        self.seed_queue()
        self.seed_ledger()
        self.assertIsNotNone(ra.watch_escalation_blocked(self.dir, PEEK_PUSH, now=NOW))

    def test_the_id_cache_alone_names_the_row(self):
        """Queue empty (pruned, or the machine rebooted) — the ⏰ row-id cache still holds the real
        row name, and both pushes are recognised by it."""
        self.seed_id_cache()
        self.seed_ledger()
        for msg in (GATE_MISS_PUSH, PEEK_PUSH):
            blocked = ra.watch_escalation_blocked(self.dir, msg, now=NOW)
            self.assertIsNotNone(blocked)
            self.assertEqual(blocked["matched_title"], ROW_TITLE)

    def test_the_best_of_the_two_sources_wins(self):
        """Both sources present: the gate scores every candidate and keeps the highest coverage, so a
        weak title can only fail to match — never mask a good one."""
        self.seed_queue()
        self.seed_id_cache()
        self.seed_ledger()
        titles = ra.known_reminder_titles(self.dir)[ra.norm_key(RID)]["titles"]
        self.assertEqual(sorted(titles), ["Orchid Fertilizer (feeding)",
                                          "Orchid fertilizer feeding"])
        blocked = ra.watch_escalation_blocked(self.dir, GATE_MISS_PUSH, now=NOW)
        best = max(ra.title_coverage(t, GATE_MISS_PUSH) for t in titles)
        self.assertAlmostEqual(blocked["coverage"], round(best, 3), places=3)

    def test_a_mention_still_sends_against_the_real_title_sources(self):
        """It names the row from the id cache and chases nothing. Still goes out — the conjunction is
        what keeps an ordinary notice from being eaten."""
        self.seed_queue()
        self.seed_id_cache()
        self.seed_ledger()
        self.assertGreaterEqual(ra.title_coverage(ROW_TITLE, MENTION_ONLY), 0.0)
        self.assertFalse(ra.message_chases(MENTION_ONLY))
        self.assertIsNone(ra.watch_escalation_blocked(self.dir, MENTION_ONLY, now=NOW))

    def test_unreadable_ack_state_still_sends_with_the_title_now_matching(self):
        """Fail-open, re-asserted where it BITES: with the title matching, the fail-open branches are
        now the only reason these send."""
        self.seed_queue()
        self.seed_id_cache()
        with open(ra.acks_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("not json{")  # unreadable ledger
        self.assertIsNone(ra.watch_escalation_blocked(self.dir, GATE_MISS_PUSH, now=NOW))
        self.assertIn("ledger", ra.reminder_acked_today(self.dir, RID, NOW)["unreadable"])

    def test_absent_ack_state_still_sends_with_the_title_now_matching(self):
        self.seed_queue()
        self.seed_id_cache()
        self.assertIsNone(ra.watch_escalation_blocked(self.dir, GATE_MISS_PUSH, now=NOW))

    def test_a_multi_fire_cadence_in_the_id_cache_is_exempt(self):
        """`Cadence = Multiple/day` is the cache's mirror of a queue entry's `ack_gate: false`.
        Without this, adding the cache as a title source would newly gate a row the fire-time gate has
        always exempted, on a day it happens not to be in the queue."""
        self.seed_id_cache(title="Check messages from the landlord", reminder_id=ROLL_RID,
                           cadence="Multiple/day")
        self.seed_ledger(reminder_id=ROLL_RID)
        cache = ra.id_cache_titles(self.dir)
        self.assertTrue(cache[ra.norm_key(ROLL_RID)]["multi_fire"])
        self.assertIsNone(ra.watch_escalation_blocked(
            self.dir, "Have you checked messages from the landlord yet? Still outstanding.", now=NOW))

    def test_the_id_cache_header_and_separator_are_not_rows(self):
        """Parsed by shape — a second cell that normalizes to a 32-hex page id — so the table's own
        header and `|---|` rule fall out without the parser knowing where the table starts."""
        self.seed_id_cache()
        cache = ra.id_cache_titles(self.dir)
        self.assertEqual(sorted(v["title"] for v in cache.values()),
                         ["Orchid Fertilizer (feeding)", "Water the plants (AM)"])

    def test_a_garbled_id_cache_contributes_nothing_rather_than_raising(self):
        with open(os.path.join(self.dir, ra.ID_CACHE_FILE), "wb") as fh:
            fh.write(b"\xff\xfe not | even | markdown \x00")
        self.assertEqual(ra.id_cache_titles(self.dir), {})
        self.seed_queue()
        self.seed_ledger()
        self.assertIsNotNone(ra.watch_escalation_blocked(self.dir, GATE_MISS_PUSH, now=NOW))

    def test_an_absent_id_cache_is_silent(self):
        self.assertEqual(ra.id_cache_titles(self.dir), {})

    def test_boilerplate_words_identify_nothing(self):
        """The stopword half of the same fix: `ack`/`notion`/`store`/`day` sit on every live queue
        entry, so a message quoting them must not thereby name a row."""
        for word in ("ack", "acked", "notion", "store", "tick", "day", "days"):
            self.assertEqual(ra.significant_tokens(word), set(), word)


class AckedTodaySuppresses(GateFixture):
    """The incident itself: an acked row cannot be re-escalated."""

    def test_ledger_ack_today_blocks_the_real_push(self):
        self.seed_queue()
        self.seed_ledger()
        blocked = ra.watch_escalation_blocked(self.dir, PEEK_PUSH, now=NOW)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["reason"], "reminder_acked_today")
        self.assertEqual(blocked["key"], ra.norm_key(RID))
        self.assertEqual(blocked["date"], TODAY)
        self.assertEqual(blocked["matched_by"], "title")
        self.assertEqual(blocked["matched_title"], ra.strip_nudge_template(QUEUE_TEXT))
        self.assertEqual(blocked["sources"], ["ledger"])

    def test_explicit_reminder_id_needs_no_heuristics(self):
        """A named row is checked directly — no title, no chase cue, no queue."""
        self.seed_ledger()
        blocked = ra.watch_escalation_blocked(self.dir, "anything at all", reminder_id=RID, now=NOW)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["matched_by"], "reminder_id")


class NotAckedPassesThrough(GateFixture):
    """The far more expensive error: a real Super-Critical chase must still reach the owner."""

    def test_no_ack_at_all_sends(self):
        self.seed_queue()
        self.assertIsNone(ra.watch_escalation_blocked(self.dir, PEEK_PUSH, now=NOW))

    def test_yesterdays_ack_does_not_gate_today(self):
        """Mirrors the fire-time gate: only *today's* acks gate, so the next occurrence is chased."""
        self.seed_queue()
        self.seed_ledger(date=YESTERDAY)
        self.assertIsNone(ra.watch_escalation_blocked(self.dir, PEEK_PUSH, now=NOW))

    def test_a_different_acked_row_does_not_gate_this_one(self):
        self.seed_queue()
        self.seed_ledger(reminder_id=OTHER_RID)
        self.assertIsNone(ra.watch_escalation_blocked(self.dir, PEEK_PUSH, now=NOW))

    def test_a_mention_is_not_a_chase(self):
        """The acked row is named, but nothing is being chased — a real escalation, still sent."""
        self.seed_queue()
        self.seed_ledger()
        self.assertIsNone(ra.watch_escalation_blocked(
            self.dir, "Email from the garden shop: your orchid fertilizer feeding refill shipped.",
            now=NOW))

    def test_multi_fire_roll_is_never_blocked(self):
        """`ack_gate: false` — one "checked messages" ack must not silence the rest of the day.
        Exactly the exemption `entry_acked` makes, so the two gates agree by construction."""
        self.seed_queue(ack_gate=False,
                        text="Check messages from the landlord — anything new? — ack in the store or "
                             "tell me.")
        self.seed_ledger()
        self.assertIsNone(ra.watch_escalation_blocked(
            self.dir, "Have you checked messages from the landlord yet? Still outstanding.", now=NOW))

    def test_no_title_source_means_no_identity_means_send(self):
        self.seed_ledger()  # acked, but nothing on this host can name the row
        self.assertIsNone(ra.watch_escalation_blocked(self.dir, PEEK_PUSH, now=NOW))


class FailOpen(GateFixture):
    """**Fail-open on unknown.** Every one of these is acked-or-maybe-acked and every one still sends.
    Argue with this class before making it green."""

    def test_corrupt_ledger_sends(self):
        self.seed_queue()
        with open(ra.acks_path(self.dir), "w", encoding="utf-8") as fh:
            fh.write("not json{")
        self.assertIsNone(ra.watch_escalation_blocked(self.dir, PEEK_PUSH, now=NOW))
        reading = ra.reminder_acked_today(self.dir, RID, NOW)
        self.assertFalse(reading["acked"])
        self.assertIn("ledger", reading["unreadable"])  # honestly reported, still fails open

    def test_ledger_holding_a_non_dict_sends(self):
        self.seed_queue()
        _write(ra.acks_path(self.dir), ["not", "a", "map"])
        self.assertIsNone(ra.watch_escalation_blocked(self.dir, PEEK_PUSH, now=NOW))

    def test_missing_state_dir_sends(self):
        self.assertIsNone(ra.watch_escalation_blocked(
            os.path.join(self.dir, "nope"), PEEK_PUSH, now=NOW))

    def test_absent_sources_are_silent_not_unreadable(self):
        """Nothing recorded is a *readable* nothing — the log line should not cry corruption."""
        reading = ra.reminder_acked_today(self.dir, RID, NOW)
        self.assertEqual(reading["unreadable"], [])
        self.assertFalse(reading["acked"])

    def test_garbage_queue_file_sends(self):
        with open(os.path.join(self.dir, ra.REMINDERS_FILE), "w", encoding="utf-8") as fh:
            fh.write("{{{not json")
        self.seed_ledger()
        self.assertIsNone(ra.watch_escalation_blocked(self.dir, PEEK_PUSH, now=NOW))
        self.assertEqual(ra.known_reminder_titles(self.dir), {})


class GateLog(GateFixture):
    """The other half of the defect: a Watch push once left no trace anywhere. Both verdicts are
    recorded."""

    def test_blocked_and_allowed_are_both_logged(self):
        ra.log_watch_gate(self.dir, {"blocked": True, "surface": "telegram"})
        ra.log_watch_gate(self.dir, {"blocked": False, "surface": "telegram"})
        rows = self.gate_log()
        self.assertEqual([r["blocked"] for r in rows], [True, False])
        self.assertTrue(all(r["ts"] and r["schema"] == "seneschal.watch-gate/1" for r in rows))

    def test_logging_never_raises(self):
        os.mkdir(os.path.join(self.dir, ra.WATCH_GATE_LOG))  # a directory squatting on the log path
        ra.log_watch_gate(self.dir, {"blocked": True})       # must not raise into the send path


class FactKeyDedupe(GateFixture):
    """`reminders_acks.fact_key`, the normalizer behind `watch_duplicate_blocked`.

    The four lines below are SYNTHETIC restatements of ONE fact (one account, one overdraft, escalated
    four times with a growing fee and a growing "how overdue" clause), shaped like a real re-escalation
    run so the normalizer is pinned against the SHAPE of the problem."""

    BANK_1 = "🚨 Financial alert: Northwind checking x1234 is overdrawn by $42.17, as of 01:15 UTC."
    BANK_2 = ("Northwind checking x1234 is still overdrawn — down $58.32 as of 05:00 UTC "
              "(about 4 hours now).")
    BANK_3 = ("🚨 Financial alert: Northwind checking x1234 remains overdrawn by $58.32, overdue since "
              "01:15 UTC — roughly 7 hours.")
    BANK_4 = ("Northwind checking x1234 has been overdrawn for 16 hours (since 01:15 UTC), still down "
              "$58.32 as of 17:00 UTC.")

    def test_the_four_restatements_collapse_to_one_key(self):
        keys = {ra.fact_key(t) for t in (self.BANK_1, self.BANK_2, self.BANK_3, self.BANK_4)}
        self.assertEqual(len(keys), 1, keys)
        self.assertEqual(next(iter(keys)), "checking northwind overdrawn x1234")

    def test_a_different_account_does_not_collapse(self):
        other = "🚨 Financial alert: Northwind checking x5678 is overdrawn by $9.00, as of 02:00 UTC."
        self.assertNotEqual(ra.fact_key(self.BANK_1), ra.fact_key(other))

    def test_a_wholly_different_alert_does_not_collapse(self):
        other = "🔴 Cloudflare Alert: old-shop.example expired 30+ days ago and will be deleted."
        self.assertNotEqual(ra.fact_key(self.BANK_1), ra.fact_key(other))

    def test_time_currency_and_relative_phrases_alone_carry_no_identity(self):
        self.assertEqual(ra.fact_key("at 01:15 UTC for $42.17 ago overdue since as of checked"), "")

    def test_non_string_and_empty_are_empty(self):
        self.assertEqual(ra.fact_key(None), "")
        self.assertEqual(ra.fact_key(""), "")
        self.assertEqual(ra.fact_key("   "), "")


class DuplicateWindow(GateFixture):
    """`reminders_acks.watch_duplicate_blocked` as a pure predicate over the ledger."""

    def _seed_row(self, ts, text, blocked=False):
        ra.log_watch_gate(self.dir, {"ts": ts, "surface": "telegram", "source": "watch",
                                     "blocked": blocked, "fact_key": ra.fact_key(text),
                                     "text": text[:400]})

    def test_a_repeat_inside_the_window_is_flagged_duplicate(self):
        self._seed_row("2026-09-08T06:34:00Z", FactKeyDedupe.BANK_1)
        now = datetime(2026, 9, 8, 10, 14, 0, tzinfo=timezone.utc)
        verdict = ra.watch_duplicate_blocked(self.dir, FactKeyDedupe.BANK_2, now=now)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["reason"], "duplicate-of:2026-09-08T06:34:00Z")
        self.assertEqual(verdict["duplicate_of"], "2026-09-08T06:34:00Z")
        self.assertEqual(verdict["fact_key"], "checking northwind overdrawn x1234")

    def test_a_25_hour_old_row_does_not_block(self):
        self._seed_row("2026-09-08T06:34:00Z", FactKeyDedupe.BANK_1)
        now = datetime(2026, 9, 9, 7, 34, 0, tzinfo=timezone.utc)  # exactly 25h later
        self.assertIsNone(ra.watch_duplicate_blocked(self.dir, FactKeyDedupe.BANK_2, now=now))

    def test_a_blocked_prior_row_is_not_a_prior_escalation(self):
        """A push that never reached the owner can't be the thing a later one repeats."""
        self._seed_row("2026-09-08T06:34:00Z", FactKeyDedupe.BANK_1, blocked=True)
        now = datetime(2026, 9, 8, 10, 14, 0, tzinfo=timezone.utc)
        self.assertIsNone(ra.watch_duplicate_blocked(self.dir, FactKeyDedupe.BANK_2, now=now))

    def test_a_different_fact_does_not_block(self):
        self._seed_row("2026-09-08T06:34:00Z", "🔴 Cloudflare Alert: old-shop.example expired.")
        now = datetime(2026, 9, 8, 10, 14, 0, tzinfo=timezone.utc)
        self.assertIsNone(ra.watch_duplicate_blocked(self.dir, FactKeyDedupe.BANK_2, now=now))

    def test_unidentifiable_text_never_blocks(self):
        self._seed_row("2026-09-08T06:34:00Z", FactKeyDedupe.BANK_1)
        now = datetime(2026, 9, 8, 10, 14, 0, tzinfo=timezone.utc)
        self.assertIsNone(ra.watch_duplicate_blocked(self.dir, "ok", now=now))

    def test_a_row_with_no_fact_key_field_is_derived_from_its_own_text(self):
        """A row written without the field (an older sender, or a caller that skipped it) still
        counts — retroactive against the whole ledger, not just future rows."""
        ra.log_watch_gate(self.dir, {"ts": "2026-09-08T06:34:00Z", "surface": "telegram",
                                     "source": "watch", "blocked": False,
                                     "text": FactKeyDedupe.BANK_1[:400]})
        now = datetime(2026, 9, 8, 10, 14, 0, tzinfo=timezone.utc)
        self.assertIsNotNone(ra.watch_duplicate_blocked(self.dir, FactKeyDedupe.BANK_2, now=now))

    def test_no_prior_rows_at_all_sends(self):
        now = datetime(2026, 9, 8, 10, 14, 0, tzinfo=timezone.utc)
        self.assertIsNone(ra.watch_duplicate_blocked(self.dir, FactKeyDedupe.BANK_1, now=now))


# --------------------------------------------------------------------------- through telegram_send

SOURCE_ENV = ts.SOURCE_ENV_VAR  # "SENESCHAL_SESSION_SOURCE"


class _SendMixin:
    """Drive telegram_send's real `main` with argv, capturing its one-line JSON result.

    **`now` is why these classes are not a date bomb.** A bare `ts.main()` would resolve "today" from
    the wall clock while the fixture seeded an ack dated `TODAY` — a constant frozen at the fixture's
    date. Passing `now` makes the seeded ack and the runtime lookup the *same* instant through the
    *same* `local_today`, so they cannot disagree — in any timezone, on any date. Every send here is
    `--dry-run`, so no network is reachable."""

    def _run_send(self, argv, now=None):
        real_argv, buf = sys.argv, io.StringIO()
        sys.argv = ["telegram_send.py"] + argv
        try:
            with redirect_stdout(buf):
                code = ts.main(now=now)
        finally:
            sys.argv = real_argv
        return code, json.loads(buf.getvalue().strip())


class WiringIsWhatBinds(_SendMixin, GateFixture):
    """The class that cannot pass while the feature is inert. Everything above tests a predicate nobody
    has to call; these test that a Watch surface's sends actually go through it. (The daemon stamping
    its peek child `SENESCHAL_SESSION_SOURCE=watch` is daemon wiring and is tested with the daemon.)"""

    def _args(self, **kw):
        ns = type("A", (), {})()
        ns.state_dir, ns.reminder_id = self.dir, None
        ns.ack_gate = ns.no_ack_gate = ns.dry_run = ns.check_auth = False
        ns.text, ns.text_file, ns.chat_id, ns.parse_mode = PEEK_PUSH, None, "1", None
        ns.env_file, ns.disable_preview = None, False
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def test_telegram_send_arms_on_the_watch_stamp_and_nothing_else(self):
        args = self._args()
        self.assertTrue(ts.ack_gate_enabled(args, {SOURCE_ENV: "watch"}))
        self.assertFalse(ts.ack_gate_enabled(args, {SOURCE_ENV: "daemon"}))
        self.assertFalse(ts.ack_gate_enabled(args, {}))
        self.assertTrue(ts.ack_gate_enabled(self._args(ack_gate=True), {}))
        # --no-ack-gate always wins, even on the watch surface.
        self.assertFalse(ts.ack_gate_enabled(self._args(ack_gate=True, no_ack_gate=True),
                                             {SOURCE_ENV: "watch"}))

    def test_the_peeks_send_is_refused_in_code(self):
        """End-to-end through `telegram_send.main`: the chase, on the watch surface, does not go."""
        self.seed_queue()
        self.seed_ledger()
        os.environ[SOURCE_ENV] = "watch"
        try:
            code, out = self._run_send(["--text", PEEK_PUSH, "--chat-id", "1",
                                        "--state-dir", self.dir, "--dry-run"], now=NOW)
        finally:
            os.environ.pop(SOURCE_ENV, None)
        self.assertEqual(code, 3)
        self.assertFalse(out["ok"])
        self.assertFalse(out["sent"])
        self.assertEqual(out["suppressed"], "reminder_acked_today")
        self.assertEqual(out["reminder"]["date"], TODAY)
        rows = self.gate_log()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["blocked"])
        self.assertEqual(rows[0]["source"], "watch")

    def test_the_verdict_never_reads_the_wall_clock(self):
        """Stub `local_today` so a NO-ARGUMENT call — the one wall-clock read in the whole gate —
        *raises*, and drive the same end-to-end refusal. Green means the verdict was computed entirely
        from the injected instant.

        The second half stops the first being vacuous: with `now=None` — the PRODUCTION shape — the
        very same stub *does* reach the gate, which fails open (its documented contract) and the push
        goes out at exit 0. So the stub is provably on the path."""
        self.seed_queue()
        self.seed_ledger()
        real = ra.local_today

        def no_wall_clock(now=None):
            if now is None:
                raise RuntimeError("the verdict path read the wall clock")
            return real(now)

        ra.local_today = no_wall_clock
        os.environ[SOURCE_ENV] = "watch"
        try:
            code, out = self._run_send(["--text", PEEK_PUSH, "--chat-id", "1",
                                        "--state-dir", self.dir, "--dry-run"], now=NOW)
            self.assertEqual(code, 3)
            self.assertEqual(out["suppressed"], "reminder_acked_today")
            self.assertEqual(out["reminder"]["date"], TODAY)
            uninjected, out2 = self._run_send(["--text", PEEK_PUSH, "--chat-id", "1",
                                               "--state-dir", self.dir, "--dry-run"], now=None)
            self.assertEqual(uninjected, 0)
            self.assertTrue(out2["dry_run"])
        finally:
            ra.local_today = real
            os.environ.pop(SOURCE_ENV, None)

    def test_the_clock_seam_is_off_by_default_in_production(self):
        """The seam must change nothing for the console path: `main` and `ack_gate_check` both default
        `now` to `None`, and the module's only entrypoint (`sys.exit(main())`) passes nothing."""
        import inspect
        self.assertIsNone(inspect.signature(ts.main).parameters["now"].default)
        self.assertIsNone(inspect.signature(ts.ack_gate_check).parameters["now"].default)
        self.assertIn("sys.exit(main())", inspect.getsource(ts))

    def test_an_unacked_watch_push_still_goes_out(self):
        self.seed_queue()  # title known, no ack recorded
        os.environ[SOURCE_ENV] = "watch"
        try:
            code, out = self._run_send(["--text", PEEK_PUSH, "--chat-id", "1",
                                        "--state-dir", self.dir, "--dry-run"], now=NOW)
        finally:
            os.environ.pop(SOURCE_ENV, None)
        self.assertEqual(code, 0)
        self.assertTrue(out["dry_run"])
        self.assertFalse(self.gate_log()[0]["blocked"])  # allowed sends are recorded too

    def test_ungated_surfaces_are_untouched(self):
        """The warm session and the reminder fire path share this script. An acked row, an unset
        source: no gate, no log line, nothing changed for them."""
        self.seed_queue()
        self.seed_ledger()
        code, out = self._run_send(["--text", PEEK_PUSH, "--chat-id", "1",
                                    "--state-dir", self.dir, "--dry-run"], now=NOW)
        self.assertEqual(code, 0)
        self.assertTrue(out["dry_run"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, ra.WATCH_GATE_LOG)))

    def test_without_the_gate_predicates_the_send_is_ungated(self):
        """The documented no-gate fallback: a `reminders_acks` without the Watch predicates means the
        gate answers "send" and writes nothing, exactly like `--no-ack-gate`."""
        self.seed_queue()
        self.seed_ledger()
        real = ra.watch_escalation_blocked
        del ra.watch_escalation_blocked
        os.environ[SOURCE_ENV] = "watch"
        try:
            code, out = self._run_send(["--text", PEEK_PUSH, "--chat-id", "1",
                                        "--state-dir", self.dir, "--dry-run"], now=NOW)
        finally:
            ra.watch_escalation_blocked = real
            os.environ.pop(SOURCE_ENV, None)
        self.assertEqual(code, 0)
        self.assertTrue(out["dry_run"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, ra.WATCH_GATE_LOG)))


class DedupeReportOnlyThenEnforce(_SendMixin, GateFixture):
    """The dedupe predicate wired into `telegram_send.ack_gate_check`, beside the suppression and ack
    halves — report-only by default, blocking only once `WATCH_DEDUPE_ENFORCE` is set."""

    def _seed_prior_send(self, ts_, text):
        ra.log_watch_gate(self.dir, {"ts": ts_, "surface": "telegram", "source": "watch",
                                     "blocked": False, "fact_key": ra.fact_key(text),
                                     "text": text[:400]})

    def test_dedupe_enforced_reads_the_env_flag(self):
        self.assertFalse(ts.dedupe_enforced({}))
        self.assertTrue(ts.dedupe_enforced({"WATCH_DEDUPE_ENFORCE": "1"}))
        self.assertTrue(ts.dedupe_enforced({"WATCH_DEDUPE_ENFORCE": "true"}))
        self.assertFalse(ts.dedupe_enforced({"WATCH_DEDUPE_ENFORCE": "0"}))
        self.assertFalse(ts.dedupe_enforced({"WATCH_DEDUPE_ENFORCE": ""}))

    def test_report_only_default_sends_and_logs_would_block(self):
        self._seed_prior_send("2026-09-08T06:34:00Z", FactKeyDedupe.BANK_1)
        now = datetime(2026, 9, 8, 10, 14, 0, tzinfo=timezone.utc)
        os.environ[SOURCE_ENV] = "watch"
        os.environ.pop("WATCH_DEDUPE_ENFORCE", None)
        try:
            code, out = self._run_send(["--text", FactKeyDedupe.BANK_2, "--chat-id", "1",
                                        "--state-dir", self.dir, "--dry-run"], now=now)
        finally:
            os.environ.pop(SOURCE_ENV, None)
        self.assertEqual(code, 0)
        self.assertTrue(out["dry_run"])
        rows = self.gate_log()
        # `rows[0]` is the seeded prior send; `rows[-1]` is THIS send's own row.
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[-1]["blocked"])
        self.assertTrue(rows[-1]["dedupe"]["would_block"])
        self.assertEqual(rows[-1]["dedupe"]["duplicate_of"], "2026-09-08T06:34:00Z")
        self.assertFalse(rows[-1]["dedupe"]["enforced"])
        self.assertEqual(rows[-1]["fact_key"], "checking northwind overdrawn x1234")

    def test_enforce_flag_blocks_the_duplicate(self):
        self._seed_prior_send("2026-09-08T06:34:00Z", FactKeyDedupe.BANK_1)
        now = datetime(2026, 9, 8, 10, 14, 0, tzinfo=timezone.utc)
        os.environ[SOURCE_ENV] = "watch"
        os.environ["WATCH_DEDUPE_ENFORCE"] = "1"
        try:
            code, out = self._run_send(["--text", FactKeyDedupe.BANK_2, "--chat-id", "1",
                                        "--state-dir", self.dir, "--dry-run"], now=now)
        finally:
            os.environ.pop(SOURCE_ENV, None)
            os.environ.pop("WATCH_DEDUPE_ENFORCE", None)
        self.assertEqual(code, 3)
        self.assertFalse(out["ok"])
        self.assertFalse(out["sent"])
        self.assertEqual(out["suppressed"], "duplicate-of:2026-09-08T06:34:00Z")
        self.assertEqual(out["duplicate"]["duplicate_of"], "2026-09-08T06:34:00Z")
        rows = self.gate_log()
        self.assertTrue(rows[-1]["blocked"])
        self.assertTrue(rows[-1]["dedupe"]["enforced"])

    def test_never_suppresses_a_new_and_different_critical_alert(self):
        """Dedupe must never bypass `watch_suppress.NEVER_SUPPRESS` by silencing a genuinely NEW 🚨 —
        even enforced, it only ever matches an IDENTICAL fact_key, never the marker."""
        self._seed_prior_send("2026-09-08T06:34:00Z", FactKeyDedupe.BANK_1)
        different = "🚨 Financial alert: Contoso savings x7788 balance is -$5.00 as of 09:00 UTC."
        now = datetime(2026, 9, 8, 10, 14, 0, tzinfo=timezone.utc)
        os.environ[SOURCE_ENV] = "watch"
        os.environ["WATCH_DEDUPE_ENFORCE"] = "1"
        try:
            code, out = self._run_send(["--text", different, "--chat-id", "1",
                                        "--state-dir", self.dir, "--dry-run"], now=now)
        finally:
            os.environ.pop(SOURCE_ENV, None)
            os.environ.pop("WATCH_DEDUPE_ENFORCE", None)
        self.assertEqual(code, 0)
        self.assertTrue(out["dry_run"])

    def test_no_prior_send_at_all_is_not_a_duplicate(self):
        now = datetime(2026, 9, 8, 10, 14, 0, tzinfo=timezone.utc)
        os.environ[SOURCE_ENV] = "watch"
        os.environ["WATCH_DEDUPE_ENFORCE"] = "1"
        try:
            code, out = self._run_send(["--text", FactKeyDedupe.BANK_1, "--chat-id", "1",
                                        "--state-dir", self.dir, "--dry-run"], now=now)
        finally:
            os.environ.pop(SOURCE_ENV, None)
            os.environ.pop("WATCH_DEDUPE_ENFORCE", None)
        self.assertEqual(code, 0)
        self.assertFalse(self.gate_log()[0]["blocked"])
        self.assertFalse(self.gate_log()[0]["dedupe"]["would_block"])


class RuntimeAckGate(_SendMixin, GateFixture):
    """`watch_ack.ack_blocks` wired into `telegram_send.ack_gate_check`, beside the suppression,
    reminder-ack and dedupe halves. Unlike dedupe, this ENFORCES with no env var."""

    BANK_TEXT = FactKeyDedupe.BANK_1
    BANK_KEY = "checking northwind overdrawn x1234"
    OTHER_ACCOUNT_TEXT = "🚨 Financial alert: Contoso savings x7788 balance is -$5.00 as of 09:00 UTC."

    def test_an_acked_fact_is_blocked_with_no_env_var(self):
        """Enforcement needs nothing set — the opposite of dedupe's report-only default."""
        wa.record_ack(self.dir, self.BANK_KEY, "I fixed that", now=NOW)
        os.environ[SOURCE_ENV] = "watch"
        os.environ.pop("WATCH_DEDUPE_ENFORCE", None)
        try:
            code, out = self._run_send(["--text", self.BANK_TEXT, "--chat-id", "1",
                                        "--state-dir", self.dir, "--dry-run"], now=NOW)
        finally:
            os.environ.pop(SOURCE_ENV, None)
        self.assertEqual(code, 3)
        self.assertFalse(out["ok"])
        self.assertFalse(out["sent"])
        self.assertTrue(out["suppressed"].startswith("watch-acked:"))
        self.assertEqual(out["watch_ack"]["fact_key"], self.BANK_KEY)
        rows = self.gate_log()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["blocked"])
        self.assertTrue(rows[0]["watch_ack"]["blocked"])
        self.assertEqual(rows[0]["reason"], out["suppressed"])

    def test_an_unacked_fact_still_sends(self):
        os.environ[SOURCE_ENV] = "watch"
        try:
            code, out = self._run_send(["--text", self.BANK_TEXT, "--chat-id", "1",
                                        "--state-dir", self.dir, "--dry-run"], now=NOW)
        finally:
            os.environ.pop(SOURCE_ENV, None)
        self.assertEqual(code, 0)
        self.assertFalse(self.gate_log()[0]["blocked"])
        self.assertFalse(self.gate_log()[0]["watch_ack"]["blocked"])

    def test_never_suppresses_a_new_and_different_critical_alert(self):
        """The owner acked the Northwind fact — a brand-new 🚨 about a DIFFERENT account is never
        touched."""
        wa.record_ack(self.dir, self.BANK_KEY, "I fixed that", now=NOW)
        os.environ[SOURCE_ENV] = "watch"
        try:
            code, out = self._run_send(["--text", self.OTHER_ACCOUNT_TEXT, "--chat-id", "1",
                                        "--state-dir", self.dir, "--dry-run"], now=NOW)
        finally:
            os.environ.pop(SOURCE_ENV, None)
        self.assertEqual(code, 0)
        self.assertTrue(out["dry_run"])

    def test_an_expired_ack_no_longer_blocks(self):
        wa.record_ack(self.dir, self.BANK_KEY, "I fixed that", for_spec="1d", now=NOW)
        later = NOW.replace(day=9)  # well past a 1-day expiry
        os.environ[SOURCE_ENV] = "watch"
        try:
            code, out = self._run_send(["--text", self.BANK_TEXT, "--chat-id", "1",
                                        "--state-dir", self.dir, "--dry-run"], now=later)
        finally:
            os.environ.pop(SOURCE_ENV, None)
        self.assertEqual(code, 0)

    def test_ungated_surfaces_are_untouched(self):
        """No watch stamp: the ack exists but the gate never arms, same as every other half."""
        wa.record_ack(self.dir, self.BANK_KEY, "I fixed that", now=NOW)
        code, out = self._run_send(["--text", self.BANK_TEXT, "--chat-id", "1",
                                    "--state-dir", self.dir, "--dry-run"], now=NOW)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ra.WATCH_GATE_LOG)))


class SourceKeyedFamilyEndToEnd(_SendMixin, GateFixture):
    """The identity comes from the email, through the real chokepoint: an ack on the family blocks a
    differently-worded send that carries the same `--source-*` fields, and the ledger row says which
    identity it was judged on."""

    SENDER = "Northwind Alerts <alerts@northwind.example>"
    SUBJECT = "Overdraft Notice - account ending in 1234"
    SOURCE_KEY = "source:alerts@northwind.example|account notice overdraft|x1234"
    FIRST = ("🚨 Watch Alert: Your Northwind account ending in x1234 went overdrawn on 09/14. Check "
             "your balance and bring it positive ASAP to avoid fees.")
    REWORDED = ("🚨 Watch Alert: Northwind overdraft notice for account ending x1234. Alert came in at "
                "01:03 UTC.")

    def _send(self, text, *, source=True, now=NOW):
        argv = ["--text", text, "--chat-id", "1", "--state-dir", self.dir, "--dry-run"]
        if source:
            argv += ["--source-sender", self.SENDER, "--source-subject", self.SUBJECT]
        os.environ[SOURCE_ENV] = "watch"
        try:
            return self._run_send(argv, now=now)
        finally:
            os.environ.pop(SOURCE_ENV, None)

    def test_the_row_is_keyed_on_the_source_and_names_it(self):
        code, out = self._send(self.FIRST)
        self.assertEqual(code, 0)
        row = self.gate_log()[0]
        self.assertEqual(row["fact_key"], self.SOURCE_KEY)
        self.assertEqual(row["fact_key_text"], ra.fact_key(self.FIRST))
        self.assertEqual(row["source_sender"], self.SENDER)
        self.assertEqual(row["source_subject"], self.SUBJECT)

    def test_a_source_less_row_is_keyed_on_the_prose_exactly_as_before(self):
        code, out = self._send(self.FIRST, source=False)
        self.assertEqual(code, 0)
        row = self.gate_log()[0]
        self.assertEqual(row["fact_key"], ra.fact_key(self.FIRST))
        self.assertEqual(row["fact_key_text"], row["fact_key"])
        self.assertIsNone(row["source_sender"])

    def test_an_ack_on_the_family_blocks_a_reworded_send(self):
        wa.record_ack(self.dir, self.SOURCE_KEY, "I know. Stop.", now=NOW)
        code, out = self._send(self.REWORDED)
        self.assertEqual(code, 3)
        self.assertTrue(out["suppressed"].startswith("watch-acked:"))
        self.assertEqual(out["watch_ack"]["fact_key"], self.SOURCE_KEY)
        row = self.gate_log()[0]
        self.assertTrue(row["blocked"])
        self.assertTrue(row["watch_ack"]["blocked"])
        # The prose key of THIS rewording was never acked — the family was. Both are on the row.
        self.assertNotEqual(row["fact_key_text"], self.SOURCE_KEY)

    def test_the_family_ack_does_not_reach_a_source_less_send(self):
        wa.record_ack(self.dir, self.SOURCE_KEY, "stop", now=NOW)
        code, out = self._send(self.REWORDED, source=False)
        self.assertEqual(code, 0)

    def test_dedupe_would_block_the_reworded_family_send(self):
        # Seeded with an explicit ts: `log_watch_gate` stamps the wall clock, and NOW is fixed.
        ra.log_watch_gate(self.dir, {"ts": "2026-08-07T18:30:00Z", "surface": "telegram",
                                     "source": "watch", "blocked": False,
                                     "fact_key": self.SOURCE_KEY,
                                     "fact_key_text": ra.fact_key(self.FIRST),
                                     "source_sender": self.SENDER, "source_subject": self.SUBJECT,
                                     "text": self.FIRST})
        code, out = self._send(self.REWORDED, now=NOW)
        self.assertEqual(code, 0)  # report-only, as ever
        rows = self.gate_log()
        self.assertTrue(rows[1]["dedupe"]["would_block"])
        self.assertEqual(rows[1]["dedupe"]["duplicate_of"], "2026-08-07T18:30:00Z")
        self.assertFalse(rows[1]["dedupe"]["enforced"])
        # The same rewording with NO source fields is a different prose key — not a duplicate.
        code, out = self._send(self.REWORDED, source=False, now=NOW)
        self.assertFalse(self.gate_log()[2]["dedupe"]["would_block"])


class ThreadReconciliation(_SendMixin, GateFixture):
    """`watch_reconcile.classify` wired into `telegram_send.ack_gate_check`. Report-only by default,
    the same shape as dedupe.

    `SEPT_NOW` is a second clock, deliberately: the file-level `NOW` is in August, but the thread
    fixture is in September — mixing them would put a September message "in the future" relative to
    an August `now` and silently drop it (`watch_reconcile.classify`'s `dt > instant` guard)."""

    SEPT_NOW = datetime(2026, 9, 8, 19, 0, 0, tzinfo=timezone.utc)
    SENDER = "alerts@northwind.example"
    RESOLUTION = [{"id": "m2", "from": "alerts@northwind.example",
                   "subject": "You are no longer in Low Cash Mode",
                   "text": "Good news — your Northwind checking account is no longer in Low Cash Mode.",
                   "date": "2026-09-08T18:16:00Z"}]

    def _stub_classify(self, verdict, **extra):
        def fake(candidate, **kw):
            return {"verdict": verdict, "reason": extra.get("reason", "superseded-by:m2"),
                    "fact_key": extra.get("fact_key", "x"), "matched": extra.get("matched"),
                    "door": extra.get("door", "gmail")}
        return fake

    def _armed(self, enforce):
        os.environ[SOURCE_ENV] = "watch"
        if enforce:
            os.environ["WATCH_RECONCILE_ENFORCE"] = "1"
        else:
            os.environ.pop("WATCH_RECONCILE_ENFORCE", None)

    def _disarm(self):
        os.environ.pop(SOURCE_ENV, None)
        os.environ.pop("WATCH_RECONCILE_ENFORCE", None)

    def _alert(self, text="🚨 alert", received="2026-09-08T11:28:00Z", source=True):
        argv = ["--text", text, "--chat-id", "1", "--state-dir", self.dir, "--dry-run"]
        if source:
            argv += ["--source-sender", self.SENDER, "--source-received-at", received]
        return self._run_send(argv, now=self.SEPT_NOW)

    def test_reconcile_enforced_reads_the_env_flag(self):
        self.assertFalse(ts.reconcile_enforced({}))
        self.assertTrue(ts.reconcile_enforced({"WATCH_RECONCILE_ENFORCE": "1"}))
        self.assertTrue(ts.reconcile_enforced({"WATCH_RECONCILE_ENFORCE": "true"}))
        self.assertFalse(ts.reconcile_enforced({"WATCH_RECONCILE_ENFORCE": "0"}))
        self.assertFalse(ts.reconcile_enforced({"WATCH_RECONCILE_ENFORCE": ""}))

    def test_report_only_default_sends_and_logs_would_block(self):
        real = wr.classify
        wr.classify = self._stub_classify("SUPERSEDED")
        self._armed(enforce=False)
        try:
            code, out = self._alert()
        finally:
            wr.classify = real
            self._disarm()
        self.assertEqual(code, 0)
        self.assertTrue(out["dry_run"])
        rows = self.gate_log()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["blocked"])
        self.assertEqual(rows[0]["reconcile"]["verdict"], "SUPERSEDED")
        self.assertTrue(rows[0]["reconcile"]["would_block"])
        self.assertFalse(rows[0]["reconcile"]["enforced"])

    def test_enforce_flag_blocks_the_superseded_send(self):
        real = wr.classify
        wr.classify = self._stub_classify("SUPERSEDED")
        self._armed(enforce=True)
        try:
            code, out = self._alert()
        finally:
            wr.classify = real
            self._disarm()
        self.assertEqual(code, 3)
        self.assertFalse(out["ok"])
        self.assertFalse(out["sent"])
        self.assertEqual(out["suppressed"], "superseded-by:m2")
        self.assertEqual(out["reconcile"]["verdict"], "SUPERSEDED")
        rows = self.gate_log()
        self.assertTrue(rows[-1]["blocked"])
        self.assertTrue(rows[-1]["reconcile"]["enforced"])

    def test_still_open_never_blocks_even_enforced(self):
        real = wr.classify
        wr.classify = self._stub_classify("STILL_OPEN", reason="no-resolution-signal")
        self._armed(enforce=True)
        try:
            code, out = self._alert(source=False)
        finally:
            wr.classify = real
            self._disarm()
        self.assertEqual(code, 0)
        self.assertFalse(self.gate_log()[0]["blocked"])

    def test_no_source_metadata_reads_unknown_and_never_blocks_even_enforced(self):
        """No `--source-sender`/`--source-received-at`: reconciliation reads UNKNOWN without touching
        a door. Uses the REAL `watch_reconcile.classify`, not a stub."""
        self._armed(enforce=True)
        try:
            code, out = self._alert(source=False)
        finally:
            self._disarm()
        self.assertEqual(code, 0)
        self.assertEqual(self.gate_log()[0]["reconcile"]["verdict"], "UNKNOWN")
        self.assertFalse(self.gate_log()[0]["reconcile"]["would_block"])

    def test_door_unavailable_is_unknown_and_never_blocks(self):
        """The real `classify`, with the default door forced unavailable — the door-down case must
        still send, exactly as UNKNOWN's fail-open contract promises."""
        real_fetch = wr.default_fetch_messages
        wr.default_fetch_messages = lambda sender, since, **kw: (None, None)
        self._armed(enforce=True)
        try:
            code, out = self._alert()
        finally:
            wr.default_fetch_messages = real_fetch
            self._disarm()
        self.assertEqual(code, 0)
        self.assertEqual(self.gate_log()[0]["reconcile"]["verdict"], "UNKNOWN")

    def test_never_suppresses_a_different_fact_even_enforced(self):
        """The real `classify` end to end, door stubbed: the Low-Cash-Mode fact IS blocked once its
        own resolution is on record, but a brand-new 🚨 about a DIFFERENT fact from the SAME sender is
        never touched."""
        real_fetch = wr.default_fetch_messages
        wr.default_fetch_messages = lambda sender, since, **kw: (self.RESOLUTION, "gmail")
        self._armed(enforce=True)
        try:
            code1, _ = self._alert("🚨 Financial alert: Northwind checking has entered Low Cash Mode.",
                                   received="2026-09-08T11:28:00Z")
            code2, _ = self._alert("🚨 Financial alert: Northwind savings account balance is -$5.00.",
                                   received="2026-09-08T11:29:00Z")
        finally:
            wr.default_fetch_messages = real_fetch
            self._disarm()
        self.assertEqual(code1, 3)
        self.assertEqual(code2, 0)
        rows = self.gate_log()
        self.assertEqual(rows[0]["reconcile"]["verdict"], "SUPERSEDED")
        self.assertEqual(rows[1]["reconcile"]["verdict"], "STILL_OPEN")

    def test_ungated_surfaces_are_untouched(self):
        code, out = self._alert()
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ra.WATCH_GATE_LOG)))


if __name__ == "__main__":
    unittest.main()
