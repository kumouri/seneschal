#!/usr/bin/env python3
"""Tests for `watch_ack.py` — the Watch runtime ack.

Covers: the store round trip + expiry, phrase resolution against recent escalations (match /
ambiguous refusal / unmatched refusal / the `--key` bypass), the gate blocking an acked fact while
leaving a new one untouched (including the 🚨-marker interplay both ways), source-keyed families, and
the `--replied-to` door. Wiring into the Telegram send path's Watch gate is tested beside that sender,
since that is the one chokepoint every one of these gates shares.

Every fixture is synthetic (a fictional "Northwind" bank, `*.example` addresses); every instant is
injected — nothing here reads the wall clock.

Run:  python -m unittest test_watch_ack   (from seneschal/scripts)
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import reminders_acks as ra  # noqa: E402
import watch_ack as wa  # noqa: E402

# One instant, everything else derived from it.
NOW = datetime(2026, 9, 13, 22, 0, 0, tzinfo=timezone.utc)

BANK_TEXT = "🚨 Financial alert: Northwind checking x1234 is overdrawn by $42.17, as of 01:15 UTC."
BANK_KEY = "checking northwind overdrawn x1234"  # ra.fact_key(BANK_TEXT), pinned
OTHER_ACCOUNT_TEXT = "Northwind checking x5678 is overdrawn by $9.00, as of 02:00 UTC."
OTHER_ACCOUNT_KEY = "checking northwind overdrawn x5678"
DOMAIN_TEXT = "🔴 Cloudflare Alert: old-shop.example expired 30+ days ago and will be deleted."


# One alert email, re-worded three ways by successive peeks, plus the source fields the sender
# receives for it.
BANK_SENDER = "Northwind Alerts <alerts@northwind.example>"
BANK_SUBJECT = "Overdraft Notice - account ending in 1234"
BANK_SOURCE_KEY = "source:alerts@northwind.example|account notice overdraft|x1234"
BANK_REWORDINGS = (
    "🚨 Watch Alert: Your Northwind account ending in x1234 went overdrawn on 09/14. Check your "
    "balance and bring it positive ASAP to avoid fees.",
    "🚨 Watch Alert: Northwind overdraft notice for account ending x1234. Alert came in at 01:03 UTC.",
    "🚨 Watch Alert: Your Northwind account ending in x1234 went overdrawn on 09/14. Streaming (4.97) "
    "and Music (8.99) triggered it. Check your balance and add funds if needed.",
)
BANK_FOURTH = ("⚠️ **Account alert from Northwind:** Your account ending in x1234 was overdrawn on "
               "09/14, now out of low-balance mode as of 09/15 01:23am UTC.")
#: An exasperated ack that names no fact at all — the refusal it earns is correct.
OWNER_VERBATIM = "YES I KNOW, IT'S BACK UP TO $3k NOW. STOP IT!"


class WatchAckFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def seed_escalation(self, ts, text, blocked=False, with_fact_key=True, sender=None, subject=None):
        row = {"ts": ts, "surface": "telegram", "source": "watch", "blocked": blocked,
               "text": text[:400]}
        if with_fact_key:
            # The same two fields the Telegram send path's Watch gate logs.
            row["fact_key"] = ra.escalation_fact_key(text, sender, subject)
            row["fact_key_text"] = ra.fact_key(text)
        if sender:
            row["source_sender"] = sender
            row["source_subject"] = subject
        ra.log_watch_gate(self.dir, row)

    def gate_log(self):
        path = os.path.join(self.dir, ra.WATCH_GATE_LOG)
        with open(path, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]


class PinnedFactKey(WatchAckFixture):
    def test_the_pinned_key_is_correct(self):
        self.assertEqual(ra.fact_key(BANK_TEXT), BANK_KEY)
        self.assertEqual(ra.fact_key(OTHER_ACCOUNT_TEXT), OTHER_ACCOUNT_KEY)


class StoreRoundTripAndExpiry(WatchAckFixture):
    def test_record_then_get(self):
        row = wa.record_ack(self.dir, BANK_KEY, "I fixed that", now=NOW)
        self.assertEqual(row["fact_key"], BANK_KEY)
        self.assertEqual(row["source"], "chat")
        got = wa.get_ack(self.dir, BANK_KEY)
        self.assertEqual(got["text_as_said"], "I fixed that")
        self.assertEqual(got["acked_at"], "2026-09-13T22:00:00Z")

    def test_the_store_carries_its_schema(self):
        wa.record_ack(self.dir, BANK_KEY, "fixed", now=NOW)
        with open(wa.acks_path(self.dir), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["schema"], "seneschal.watch-acks/1")

    def test_default_expiry_is_seven_days(self):
        row = wa.record_ack(self.dir, BANK_KEY, "fixed", now=NOW)
        self.assertEqual(row["expires_at"], "2026-09-20T22:00:00Z")

    def test_for_spec_overrides_expiry(self):
        row = wa.record_ack(self.dir, BANK_KEY, "fixed", for_spec="3d", now=NOW)
        self.assertEqual(row["expires_at"], "2026-09-16T22:00:00Z")
        row2 = wa.record_ack(self.dir, BANK_KEY, "fixed", for_spec="2w", now=NOW)
        self.assertEqual(row2["expires_at"], "2026-09-27T22:00:00Z")

    def test_parse_duration_variants(self):
        self.assertEqual(wa.parse_duration("7d").days, 7)
        self.assertEqual(wa.parse_duration("2w").days, 14)
        self.assertEqual(wa.parse_duration("12h").total_seconds(), 12 * 3600)
        self.assertEqual(wa.parse_duration("5").days, 5)  # bare number defaults to days

    def test_parse_duration_refuses_garbage(self):
        with self.assertRaises(wa.DurationError):
            wa.parse_duration("soon")
        with self.assertRaises(wa.DurationError):
            wa.parse_duration("3x")

    def test_active_ack_before_expiry(self):
        wa.record_ack(self.dir, BANK_KEY, "fixed", now=NOW)
        just_before = NOW.replace(day=20, hour=21, minute=59, second=59)
        self.assertIsNotNone(wa.active_ack(self.dir, BANK_KEY, now=just_before))

    def test_active_ack_after_expiry_is_none(self):
        wa.record_ack(self.dir, BANK_KEY, "fixed", now=NOW)
        after = NOW.replace(day=20, hour=22, minute=0, second=1)
        self.assertIsNone(wa.active_ack(self.dir, BANK_KEY, now=after))

    def test_a_second_ack_overwrites_the_first(self):
        wa.record_ack(self.dir, BANK_KEY, "first words", now=NOW)
        later = NOW.replace(hour=23)
        wa.record_ack(self.dir, BANK_KEY, "second words", for_spec="1d", now=later)
        row = wa.get_ack(self.dir, BANK_KEY)
        self.assertEqual(row["text_as_said"], "second words")
        self.assertEqual(row["expires_at"], "2026-09-14T23:00:00Z")

    def test_corrupt_expires_at_reads_as_expired(self):
        data = wa.load_store(self.dir)
        data["acks"][BANK_KEY] = {"text_as_said": "x", "acked_at": "not-a-date",
                                  "expires_at": "also-not-a-date", "source": "chat"}
        wa.save_store(self.dir, data)
        self.assertIsNone(wa.active_ack(self.dir, BANK_KEY, now=NOW))

    def test_unreadable_store_fails_open_on_active_ack(self):
        path = wa.acks_path(self.dir)
        os.makedirs(self.dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not json{")
        self.assertIsNone(wa.active_ack(self.dir, BANK_KEY, now=NOW))
        self.assertEqual(wa.load_store(self.dir), {"schema": wa.SCHEMA, "acks": {}})

    def test_get_ack_absent_key_is_none(self):
        self.assertIsNone(wa.get_ack(self.dir, BANK_KEY))
        self.assertIsNone(wa.active_ack(self.dir, BANK_KEY, now=NOW))

    def test_remove_ack(self):
        wa.record_ack(self.dir, BANK_KEY, "fixed", now=NOW)
        self.assertTrue(wa.remove_ack(self.dir, BANK_KEY))
        self.assertIsNone(wa.get_ack(self.dir, BANK_KEY))

    def test_remove_ack_absent_key_returns_false(self):
        self.assertFalse(wa.remove_ack(self.dir, BANK_KEY))

    def test_record_ack_refuses_an_empty_key(self):
        with self.assertRaises(ValueError):
            wa.record_ack(self.dir, "   ", "fixed", now=NOW)

    def test_list_acks_active_vs_all(self):
        wa.record_ack(self.dir, BANK_KEY, "fixed", now=NOW)
        wa.record_ack(self.dir, OTHER_ACCOUNT_KEY, "also fixed", for_spec="1d",
                      now=NOW.replace(day=1))  # long expired relative to NOW
        active = wa.list_acks(self.dir, now=NOW)
        self.assertEqual([r["fact_key"] for r in active], [BANK_KEY])
        everything = wa.list_acks(self.dir, now=NOW, include_expired=True)
        self.assertEqual(len(everything), 2)
        self.assertTrue(any(r["expired"] for r in everything))
        self.assertTrue(any(not r["expired"] for r in everything))


class Resolution(WatchAckFixture):
    """NEVER GUESS: matched only against facts the peek actually escalated recently."""

    def test_no_candidates_at_all_refuses(self):
        result = wa.resolve_fact_key("I fixed that", self.dir, now=NOW)
        self.assertFalse(result["ok"])
        self.assertIn("no Watch escalation", result["error"])

    def test_a_clear_match_resolves(self):
        self.seed_escalation("2026-09-13T06:15:00Z", BANK_TEXT)
        result = wa.resolve_fact_key(
            "I fixed the Northwind checking x1234 overdraft, all good now.", self.dir, now=NOW)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["fact_key"], BANK_KEY)
        self.assertEqual(result["matched_text"], BANK_TEXT[:400])
        self.assertGreaterEqual(result["coverage"], wa.FACT_MATCH_MIN_COVERAGE)

    def test_unmatched_phrase_refuses_and_names_candidates(self):
        self.seed_escalation("2026-09-13T06:15:00Z", BANK_TEXT)
        result = wa.resolve_fact_key("stop yelling at me", self.dir, now=NOW)
        self.assertFalse(result["ok"])
        self.assertIn(BANK_KEY, result["error"])

    def test_ambiguous_tie_refuses_naming_both(self):
        self.seed_escalation("2026-09-13T06:00:00Z", BANK_TEXT)
        self.seed_escalation("2026-09-13T07:00:00Z", OTHER_ACCOUNT_TEXT)
        result = wa.resolve_fact_key(
            "the northwind checking overdrawn thing is fixed now", self.dir, now=NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(sorted(result["candidates"]), sorted([BANK_KEY, OTHER_ACCOUNT_KEY]))

    def test_a_blocked_prior_escalation_is_not_a_candidate(self):
        """A push that never reached the owner can't be the fact they're acking."""
        self.seed_escalation("2026-09-13T06:15:00Z", BANK_TEXT, blocked=True)
        result = wa.resolve_fact_key(
            "I fixed the Northwind checking x1234 overdraft, all good now.", self.dir, now=NOW)
        self.assertFalse(result["ok"])

    def test_an_escalation_outside_the_window_is_not_a_candidate(self):
        self.seed_escalation("2026-09-01T06:15:00Z", BANK_TEXT)  # 12+ days before NOW
        result = wa.resolve_fact_key(
            "I fixed the Northwind checking x1234 overdraft, all good now.", self.dir, now=NOW)
        self.assertFalse(result["ok"])

    def test_a_row_with_no_fact_key_field_is_derived_from_its_text(self):
        self.seed_escalation("2026-09-13T06:15:00Z", BANK_TEXT, with_fact_key=False)
        result = wa.resolve_fact_key(
            "I fixed the Northwind checking x1234 overdraft, all good now.", self.dir, now=NOW)
        self.assertTrue(result["ok"])
        self.assertEqual(result["fact_key"], BANK_KEY)

    def test_a_wholly_unrelated_recent_escalation_does_not_match(self):
        self.seed_escalation("2026-09-13T06:15:00Z", DOMAIN_TEXT)
        result = wa.resolve_fact_key(
            "I fixed the Northwind checking x1234 overdraft, all good now.", self.dir, now=NOW)
        self.assertFalse(result["ok"])

    def test_empty_phrase_refuses(self):
        result = wa.resolve_fact_key("", self.dir, now=NOW)
        self.assertFalse(result["ok"])
        result2 = wa.resolve_fact_key("   ", self.dir, now=NOW)
        self.assertFalse(result2["ok"])


class KeyBypass(WatchAckFixture):
    """`--key` — the id-first door for a caller (the warm session) that already resolved the fact
    from conversational context, e.g. a purely deictic "I fixed that"."""

    def test_key_bypass_needs_no_candidates(self):
        # No escalation in the ledger at all — free-text resolution would refuse every time.
        code = wa.main(["--state-dir", self.dir, "ack", "I fixed that", "--key", BANK_KEY], now=NOW)
        self.assertEqual(code, 0)
        row = wa.get_ack(self.dir, BANK_KEY)
        self.assertEqual(row["text_as_said"], "I fixed that")
        self.assertEqual(row["source"], "chat")

    def test_key_bypass_honours_for_and_source(self):
        code = wa.main(["--state-dir", self.dir, "ack", "handled it", "--key", BANK_KEY, "--for", "2w",
                        "--source", "reaction"], now=NOW)
        self.assertEqual(code, 0)
        row = wa.get_ack(self.dir, BANK_KEY)
        self.assertEqual(row["source"], "reaction")
        self.assertEqual(row["expires_at"], "2026-09-27T22:00:00Z")

    def test_a_bad_for_span_refuses_before_writing(self):
        code = wa.main(["--state-dir", self.dir, "ack", "handled", "--key", BANK_KEY, "--for", "soon"],
                       now=NOW)
        self.assertEqual(code, 2)
        self.assertIsNone(wa.get_ack(self.dir, BANK_KEY))


class GateBlocksAckedFacts(WatchAckFixture):
    """`ack_blocks` — enforces unconditionally, never inspects the marker."""

    def test_no_ack_sends(self):
        self.assertIsNone(wa.ack_blocks(self.dir, BANK_TEXT, now=NOW))

    def test_an_acked_fact_blocks_a_repeat(self):
        wa.record_ack(self.dir, BANK_KEY, "I fixed that", now=NOW)
        blocked = wa.ack_blocks(self.dir, BANK_TEXT, now=NOW)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["reason"], "watch-acked:2026-09-13T22:00:00Z")
        self.assertEqual(blocked["fact_key"], BANK_KEY)
        self.assertEqual(blocked["text_as_said"], "I fixed that")

    def test_an_expired_ack_no_longer_blocks(self):
        wa.record_ack(self.dir, BANK_KEY, "I fixed that", for_spec="1d", now=NOW)
        later = NOW.replace(day=15)
        self.assertIsNone(wa.ack_blocks(self.dir, BANK_TEXT, now=later))

    def test_marker_interplay_same_fact_is_still_blocked(self):
        """The owner acked the fact — a 🚨 restating THAT SAME fact is suppressed too, marker and all."""
        wa.record_ack(self.dir, BANK_KEY, "I fixed that", now=NOW)
        still_marked = "🚨 Financial alert: Northwind checking x1234 remains overdrawn by $58.32."
        self.assertEqual(ra.fact_key(still_marked), BANK_KEY)
        self.assertIsNotNone(wa.ack_blocks(self.dir, still_marked, now=NOW))

    def test_marker_interplay_new_fact_is_never_touched(self):
        """A genuinely NEW 🚨 about a DIFFERENT fact is structurally unreachable here."""
        wa.record_ack(self.dir, BANK_KEY, "I fixed that", now=NOW)
        self.assertIsNone(wa.ack_blocks(self.dir, OTHER_ACCOUNT_TEXT, now=NOW))
        domain_critical = "🚨 Cloudflare Alert: old-shop.example expired."
        self.assertIsNone(wa.ack_blocks(self.dir, domain_critical, now=NOW))

    def test_unidentifiable_text_never_blocks(self):
        wa.record_ack(self.dir, BANK_KEY, "I fixed that", now=NOW)
        self.assertIsNone(wa.ack_blocks(self.dir, "ok", now=NOW))

    def test_corrupt_store_fails_open(self):
        path = wa.acks_path(self.dir)
        os.makedirs(self.dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{{{not json")
        self.assertIsNone(wa.ack_blocks(self.dir, BANK_TEXT, now=NOW))


class SourceIdentity(WatchAckFixture):
    """A source-backed escalation is keyed by the EMAIL it came from, not its prose — so the same alert
    email re-worded three ways is one fact, not three."""

    def test_three_rewordings_of_one_email_share_one_source_key(self):
        keys = {ra.escalation_fact_key(t, BANK_SENDER, BANK_SUBJECT) for t in BANK_REWORDINGS}
        self.assertEqual(keys, {BANK_SOURCE_KEY})
        # …while their prose keys are three DIFFERENT strings — the defect this closes.
        self.assertEqual(len({ra.fact_key(t) for t in BANK_REWORDINGS}), 3)

    def test_a_fourth_rewording_resolves_to_the_same_family(self):
        self.assertEqual(ra.escalation_fact_key(BANK_FOURTH, BANK_SENDER, BANK_SUBJECT),
                         BANK_SOURCE_KEY)

    def test_sender_forms_and_subject_reply_chains_normalize(self):
        bare = ra.source_fact_key("alerts@northwind.example",
                                  "Re: FW: Overdraft Notice - account ending in 1234",
                                  BANK_REWORDINGS[0])
        self.assertEqual(bare, BANK_SOURCE_KEY)
        self.assertEqual(ra.source_fact_key("ALERTS@NORTHWIND.EXAMPLE", BANK_SUBJECT,
                                            BANK_REWORDINGS[1]),
                         BANK_SOURCE_KEY)

    def test_account_token_is_taken_from_the_prose_when_the_subject_lacks_it(self):
        key = ra.source_fact_key(BANK_SENDER, "Overdraft Notice", BANK_REWORDINGS[0])
        self.assertTrue(key.endswith("|x1234"), key)
        # A different account under the same sender+subject is a DIFFERENT family.
        other = ra.source_fact_key(BANK_SENDER, "Overdraft Notice",
                                   "Northwind account ending in x5678 went overdrawn on 09/14.")
        self.assertNotEqual(key, other)

    def test_account_tokens_need_a_lead_in(self):
        self.assertEqual(ra.account_tokens("ending in 1234, ****5678, x7788, acct #4444, in 2026"),
                         {"x1234", "x5678", "x7788", "x4444"})
        self.assertEqual(ra.account_tokens("paid $1234 on 09/14"), set())  # a figure, not an account

    def test_no_sender_means_the_prose_key_exactly_as_before(self):
        for t in (BANK_TEXT, OTHER_ACCOUNT_TEXT, DOMAIN_TEXT):
            self.assertEqual(ra.escalation_fact_key(t), ra.fact_key(t))
            self.assertEqual(ra.escalation_fact_key(t, "", ""), ra.fact_key(t))
            self.assertEqual(ra.escalation_fact_key(t, None, "a subject alone"), ra.fact_key(t))
        self.assertFalse(ra.is_source_fact_key(ra.escalation_fact_key(BANK_TEXT)))
        self.assertTrue(ra.is_source_fact_key(BANK_SOURCE_KEY))

    def test_match_title_renders_a_source_key_sayable(self):
        self.assertEqual(ra.fact_key_match_title(BANK_SOURCE_KEY),
                         "northwind account notice overdraft x1234")
        self.assertEqual(ra.fact_key_match_title(BANK_KEY), BANK_KEY)  # a prose key is its own title

    def test_totality(self):
        self.assertEqual(ra.source_fact_key(None, None, None), "")
        self.assertEqual(ra.escalation_fact_key(None), "")
        self.assertEqual(ra.fact_key_match_title(None), "")
        self.assertEqual(ra.account_tokens(None, 42), set())


class AckCoversTheFamily(WatchAckFixture):
    """An ack recorded against the source key suppresses EVERY later escalation whose source resolves
    to it, for the ack's span — however the peek re-words the alert."""

    def test_ack_on_the_family_suppresses_a_fourth_rewording(self):
        wa.record_ack(self.dir, BANK_SOURCE_KEY, OWNER_VERBATIM, now=NOW)
        for text in (*BANK_REWORDINGS, BANK_FOURTH):
            verdict = wa.ack_blocks(self.dir, text, now=NOW, sender=BANK_SENDER, subject=BANK_SUBJECT)
            self.assertIsNotNone(verdict, text)
            self.assertEqual(verdict["fact_key"], BANK_SOURCE_KEY)
            self.assertTrue(verdict["reason"].startswith("watch-acked:"))

    def test_the_same_prose_with_no_source_is_not_covered(self):
        """The family is the EMAIL. A source-less escalation has no family to belong to."""
        wa.record_ack(self.dir, BANK_SOURCE_KEY, OWNER_VERBATIM, now=NOW)
        self.assertIsNone(wa.ack_blocks(self.dir, BANK_FOURTH, now=NOW))

    def test_a_different_account_under_the_same_sender_is_never_touched(self):
        wa.record_ack(self.dir, BANK_SOURCE_KEY, OWNER_VERBATIM, now=NOW)
        other = "🚨 Watch Alert: Northwind account ending in x5678 went overdrawn on 09/15."
        self.assertIsNone(wa.ack_blocks(self.dir, other, now=NOW, sender=BANK_SENDER,
                                        subject=BANK_SUBJECT))

    def test_a_pre_existing_prose_ack_still_blocks_what_it_blocked(self):
        """Migration safety: an ack recorded on a prose key keeps blocking that exact prose, source
        fields or not."""
        wa.record_ack(self.dir, BANK_KEY, "I fixed that", now=NOW)
        self.assertIsNotNone(wa.ack_blocks(self.dir, BANK_TEXT, now=NOW))
        verdict = wa.ack_blocks(self.dir, BANK_TEXT, now=NOW, sender="x@bank.example", subject="Hi")
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["fact_key"], BANK_KEY)

    def test_an_expired_family_ack_no_longer_covers(self):
        wa.record_ack(self.dir, BANK_SOURCE_KEY, OWNER_VERBATIM, for_spec="1d", now=NOW)
        later = NOW.replace(day=16)
        self.assertIsNone(wa.ack_blocks(self.dir, BANK_FOURTH, now=later, sender=BANK_SENDER,
                                        subject=BANK_SUBJECT))

    def test_dedupe_sees_the_family_too(self):
        """Dedupe keys on the same identity: a re-worded source-backed escalation inside the window is
        a duplicate of the family's last send."""
        self.seed_escalation("2026-09-13T20:00:00Z", BANK_REWORDINGS[0], sender=BANK_SENDER,
                             subject=BANK_SUBJECT)
        verdict = ra.watch_duplicate_blocked(self.dir, BANK_REWORDINGS[1], now=NOW,
                                             sender=BANK_SENDER, subject=BANK_SUBJECT)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["fact_key"], BANK_SOURCE_KEY)
        # Without the source fields the two rewordings are still two different prose keys.
        self.assertIsNone(ra.watch_duplicate_blocked(self.dir, BANK_REWORDINGS[1], now=NOW))


class RepliedTo(WatchAckFixture):
    """`--replied-to`: the owner swipe-replied to the alert — the quote IS the alert, resolved against
    the ledger row that sent it, source key first."""

    def test_a_quoted_alert_resolves_to_the_ledger_rows_source_key(self):
        self.seed_escalation("2026-09-13T20:00:00Z", BANK_REWORDINGS[2], sender=BANK_SENDER,
                             subject=BANK_SUBJECT)
        # Telegram's quote: Markdown rendered away, cut short with an ellipsis.
        quoted = BANK_REWORDINGS[2].replace("**", "")[:80].rstrip() + "…"
        result = wa.resolve_replied_to(quoted, self.dir, now=NOW)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["matched_by"], "ledger")
        self.assertEqual(result["fact_key"], BANK_SOURCE_KEY)

    def test_a_quote_with_no_ledger_row_falls_back_to_its_own_prose_key(self):
        result = wa.resolve_replied_to(BANK_TEXT, self.dir, now=NOW)
        self.assertTrue(result["ok"])
        self.assertEqual(result["matched_by"], "quote")
        self.assertEqual(result["fact_key"], BANK_KEY)

    def test_an_empty_quote_refuses(self):
        self.assertFalse(wa.resolve_replied_to("", self.dir, now=NOW)["ok"])
        self.assertFalse(wa.resolve_replied_to("🚨", self.dir, now=NOW)["ok"])

    def test_cli_replied_to_records_the_family_and_covers_the_fourth(self):
        for i, text in enumerate(BANK_REWORDINGS):
            self.seed_escalation(f"2026-09-13T{10 + i:02d}:00:00Z", text, sender=BANK_SENDER,
                                 subject=BANK_SUBJECT)
        code = wa.main(["--state-dir", self.dir, "ack", OWNER_VERBATIM,
                        "--replied-to", BANK_REWORDINGS[1][:120]], now=NOW)
        self.assertEqual(code, 0)
        self.assertIsNotNone(wa.get_ack(self.dir, BANK_SOURCE_KEY))
        self.assertIsNotNone(wa.ack_blocks(self.dir, BANK_FOURTH, now=NOW, sender=BANK_SENDER,
                                           subject=BANK_SUBJECT))


class CliVerbs(WatchAckFixture):
    def test_words_naming_no_fact_still_refuse_against_a_source_keyed_family(self):
        """The refusal is CORRECT and must stay: exasperated words name no fact. The fix is
        `--replied-to`/`--key`, never a looser matcher."""
        for i, text in enumerate(BANK_REWORDINGS):
            self.seed_escalation(f"2026-09-13T{10 + i:02d}:00:00Z", text, sender=BANK_SENDER,
                                 subject=BANK_SUBJECT)
        code = wa.main(["--state-dir", self.dir, "ack", OWNER_VERBATIM], now=NOW)
        self.assertEqual(code, 3)
        self.assertIsNone(wa.get_ack(self.dir, BANK_SOURCE_KEY))

    def test_words_naming_the_family_resolve_to_its_source_key(self):
        self.seed_escalation("2026-09-13T20:00:00Z", BANK_REWORDINGS[0], sender=BANK_SENDER,
                             subject=BANK_SUBJECT)
        result = wa.resolve_fact_key("the Northwind overdraft notice on x1234 is paid", self.dir,
                                     now=NOW)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["fact_key"], BANK_SOURCE_KEY)

    def test_key_verb_prints_the_gated_identity(self):
        self.assertEqual(wa.main(["key", "--text", BANK_REWORDINGS[0], "--source-sender", BANK_SENDER,
                                  "--source-subject", BANK_SUBJECT], now=NOW), 0)
        self.assertEqual(wa.main(["key", "--text", BANK_TEXT], now=NOW), 0)
        self.assertEqual(wa.main(["key", "--text", "🚨"], now=NOW), 3)

    def test_ack_via_text_resolution_end_to_end(self):
        self.seed_escalation("2026-09-13T06:15:00Z", BANK_TEXT)
        code = wa.main(["--state-dir", self.dir, "ack",
                        "I fixed the Northwind checking x1234 overdraft, all good now."], now=NOW)
        self.assertEqual(code, 0)
        self.assertIsNotNone(wa.get_ack(self.dir, BANK_KEY))

    def test_ack_refuses_ambiguous_with_exit_3(self):
        self.seed_escalation("2026-09-13T06:00:00Z", BANK_TEXT)
        self.seed_escalation("2026-09-13T07:00:00Z", OTHER_ACCOUNT_TEXT)
        code = wa.main(["--state-dir", self.dir, "ack",
                        "the northwind checking overdrawn thing is fixed now"], now=NOW)
        self.assertEqual(code, 3)
        self.assertIsNone(wa.get_ack(self.dir, BANK_KEY))
        self.assertIsNone(wa.get_ack(self.dir, OTHER_ACCOUNT_KEY))

    def test_ack_refuses_unmatched_with_exit_3(self):
        code = wa.main(["--state-dir", self.dir, "ack", "stop yelling at me"], now=NOW)
        self.assertEqual(code, 3)

    def test_list_and_status(self):
        wa.record_ack(self.dir, BANK_KEY, "fixed", now=NOW)
        self.assertEqual(wa.main(["--state-dir", self.dir, "list"], now=NOW), 0)
        self.assertEqual(wa.main(["--state-dir", self.dir, "status"], now=NOW), 0)

    def test_unack_removes_and_reports_missing(self):
        wa.record_ack(self.dir, BANK_KEY, "fixed", now=NOW)
        self.assertEqual(wa.main(["--state-dir", self.dir, "unack", BANK_KEY], now=NOW), 0)
        self.assertIsNone(wa.get_ack(self.dir, BANK_KEY))
        self.assertEqual(wa.main(["--state-dir", self.dir, "unack", BANK_KEY], now=NOW), 3)


class StaysOptionallyImportable(unittest.TestCase):
    """The Telegram send path imports this lazily (`try: import watch_ack`). That only works if the
    module drags in nothing heavier than its stdlib siblings."""

    def test_no_telegram_or_presence_import(self):
        with open(os.path.join(SCRIPT_DIR, "watch_ack.py"), encoding="utf-8") as fh:
            src = fh.read()
        for name in ("import telegram_", "import presence", "from presence", "from telegram_"):
            self.assertNotIn(name, src, name)


if __name__ == "__main__":
    unittest.main()
