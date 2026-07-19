#!/usr/bin/env python3
"""Tests for Telegram reaction awareness (seneschal/docs/telegram-inbound-spec.md §3).
Stdlib ``unittest`` only, like the rest of the suite.

Phase A is observe-only: a reaction is NAMED and QUOTED for the warm session, which acts in context. The
things worth pinning down:
  * `message_reaction` is actually requested — without it Telegram never sends reactions at all;
  * the ADDED emoji is new-minus-old (Telegram sends whole arrays), and a REMOVAL is not an event;
  * the allowlist gates reactions like it gates messages;
  * the emoji→intent map is the owner's to edit, fails open to the defaults, and ignores variation selectors
    (❤ vs ❤️ — an invisible codepoint would be a miserable mismatch to debug);
  * the sent-message map gives a reaction something to point at, is bounded, and never breaks a send;
  * a Telegram Premium custom-emoji reaction (`ReactionTypeCustomEmoji`, an opaque `custom_emoji_id` with
    no plain `emoji` field) resolves to its base emoji via a mocked `getCustomEmojiStickers`, a cache hit
    skips the API entirely, several ids in one batch cost one call, a corrupt cache file is tolerated, and
    an API failure (or no token) fails open to the same `note` an unmapped plain emoji gets — never an
    exception, never a dropped message.

Run:  python -m unittest seneschal.scripts.test_telegram_reactions   (or)   python test_telegram_reactions.py
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402
import sentinel as sn  # noqa: E402
import telegram_poll as tp  # noqa: E402

# Telegram's allowed reaction set — the Bot API's ReactionTypeEmoji list, verbatim (checked 2026-07-16).
# You cannot react with anything outside it, which is why ⏰/🤚/❔ needed in-set aliases. Variation
# selectors dropped, to compare the way the code does.
TELEGRAM_ALLOWED_REACTIONS = {pr._norm_emoji(e) for e in (
    "❤", "👍", "👎", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏",
    "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨",
    "😐", "🍓", "🍾", "💋", "🖕", "😈", "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝",
    "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎",
    "👾", "🤷‍♂", "🤷", "🤷‍♀", "😡",
)}


def _reaction_update(new, old=(), chat_id=555, message_id=42, uid=900):
    return {"update_id": uid, "message_reaction": {
        "chat": {"id": chat_id, "type": "private"},
        "message_id": message_id,
        "user": {"username": "owner"},
        "date": 1,
        "old_reaction": [{"type": "emoji", "emoji": e} for e in old],
        "new_reaction": [{"type": "emoji", "emoji": e} for e in new],
    }}


class AllowedUpdates(unittest.TestCase):
    def test_message_reaction_is_requested(self):
        """Off by default in the Bot API — if we don't name it, reactions can't arrive even in principle."""
        seen = {}

        class FakeResp:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s):
                return b'{"ok":true,"result":[]}'

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url if hasattr(req, "full_url") else req
            return FakeResp()

        orig = tp.urllib.request.urlopen
        tp.urllib.request.urlopen = fake_urlopen
        try:
            tp.get_updates("tok", "https://api.telegram.org", None, 0, 100)
        finally:
            tp.urllib.request.urlopen = orig
        self.assertIn("message_reaction", seen["url"])
        self.assertIn("message", seen["url"])


class ExtractReaction(unittest.TestCase):
    def test_added_emoji_is_new_minus_old(self):
        r = tp.extract_reaction(_reaction_update(new=["👍", "❤"], old=["❤"]), set())
        self.assertEqual(r["emoji"], "👍")
        self.assertEqual(r["kind"], "reaction")
        self.assertEqual(r["message_id"], 42)
        self.assertEqual(r["from"], "owner")

    def test_removal_is_not_an_event(self):
        self.assertIsNone(tp.extract_reaction(_reaction_update(new=[], old=["👍"]), set()))

    def test_unchanged_is_not_an_event(self):
        self.assertIsNone(tp.extract_reaction(_reaction_update(new=["👍"], old=["👍"]), set()))

    def test_allowlist_gates_reactions(self):
        upd = _reaction_update(new=["👍"], chat_id=999)
        self.assertIsNone(tp.extract_reaction(upd, {"555"}))
        self.assertIsNotNone(tp.extract_reaction(upd, set()))  # no allowlist configured = no gate

    def test_custom_emoji_type_is_extracted_unresolved(self):
        """A Telegram Premium custom-emoji reaction is no longer dropped — it comes out with an empty
        `emoji` and the opaque id, ready for `resolve_reactions` to fill in later."""
        upd = {"update_id": 1, "message_reaction": {
            "chat": {"id": 555}, "message_id": 1, "user": {},
            "old_reaction": [], "new_reaction": [{"type": "custom_emoji", "custom_emoji_id": "x"}]}}
        r = tp.extract_reaction(upd, set())
        self.assertIsNotNone(r)
        self.assertEqual(r["kind"], "reaction")
        self.assertEqual(r["custom_emoji_id"], "x")
        self.assertEqual(r["emoji"], "")

    def test_switching_from_plain_to_custom_emoji_is_added(self):
        """Old had a plain emoji, new has a custom one — different namespaces, so it's a real change."""
        upd = {"update_id": 1, "message_reaction": {
            "chat": {"id": 555}, "message_id": 1, "user": {},
            "old_reaction": [{"type": "emoji", "emoji": "👍"}],
            "new_reaction": [{"type": "custom_emoji", "custom_emoji_id": "y"}]}}
        r = tp.extract_reaction(upd, set())
        self.assertIsNotNone(r)
        self.assertEqual(r["custom_emoji_id"], "y")

    def test_unchanged_custom_emoji_is_not_an_event(self):
        upd = {"update_id": 1, "message_reaction": {
            "chat": {"id": 555}, "message_id": 1, "user": {},
            "old_reaction": [{"type": "custom_emoji", "custom_emoji_id": "x"}],
            "new_reaction": [{"type": "custom_emoji", "custom_emoji_id": "x"}]}}
        self.assertIsNone(tp.extract_reaction(upd, set()))

    def test_not_a_reaction_update(self):
        self.assertIsNone(tp.extract_reaction({"update_id": 1, "message": {"text": "hi"}}, set()))
        self.assertIsNone(tp.extract_reaction({"update_id": 1, "message_reaction": "junk"}, set()))


class CustomEmojiCache(unittest.TestCase):
    """`custom-emoji-cache.json` follows the same tolerant load/never-crash pattern as the rest of
    state/ — a corrupt or missing file just means the next lookup pays for a network round-trip."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "custom-emoji-cache.json")

    def test_missing_file_reads_empty(self):
        self.assertEqual(tp.load_custom_emoji_cache(self.path), {})

    def test_corrupt_file_reads_empty(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        self.assertEqual(tp.load_custom_emoji_cache(self.path), {})

    def test_wrong_shape_is_tolerated(self):
        for junk in ("[]", '{"x": 5}', '{"x": null}', "null", '"just a string"'):
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(junk)
            self.assertEqual(tp.load_custom_emoji_cache(self.path), {})

    def test_round_trips(self):
        tp.save_custom_emoji_cache(self.path, {"123": "🔥", "456": "🎉"})
        self.assertEqual(tp.load_custom_emoji_cache(self.path), {"123": "🔥", "456": "🎉"})

    def test_save_never_raises_on_a_bad_path(self):
        tp.save_custom_emoji_cache(os.path.join(self.dir, "no", "such", "\0bad"), {"1": "🔥"})


class ResolveCustomEmojis(unittest.TestCase):
    """`resolve_custom_emojis` / `resolve_reactions` — the network-touching half. No real network: the
    Bot API call is always mocked."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.dir, "custom-emoji-cache.json")

    def _mock_api(self, fn):
        return unittest.mock.patch.object(tp, "get_custom_emoji_stickers", fn)

    def test_resolves_via_mocked_api_and_caches_it(self):
        calls = []

        def fake(token, api_base, ids, timeout=15):
            calls.append(list(ids))
            return {"x": "🔥"}

        with self._mock_api(fake):
            out = tp.resolve_custom_emojis(["x"], "tok", "https://api.telegram.org", self.cache_path)
        self.assertEqual(out, {"x": "🔥"})
        self.assertEqual(calls, [["x"]])
        # Persisted, so a second call doesn't need the API at all.
        self.assertEqual(tp.load_custom_emoji_cache(self.cache_path), {"x": "🔥"})

    def test_cache_hit_skips_the_api(self):
        tp.save_custom_emoji_cache(self.cache_path, {"x": "🔥"})

        def fail(*a, **kw):
            raise AssertionError("should not call the API for a cached id")

        with self._mock_api(fail):
            out = tp.resolve_custom_emojis(["x"], "tok", "https://api.telegram.org", self.cache_path)
        self.assertEqual(out, {"x": "🔥"})

    def test_api_failure_fails_open(self):
        """A raised exception leaves the id unresolved — not an exception bubbling up."""
        def boom(*a, **kw):
            raise RuntimeError("network is down")

        with self._mock_api(boom):
            out = tp.resolve_custom_emojis(["x"], "tok", "https://api.telegram.org", self.cache_path)
        self.assertEqual(out, {})
        self.assertEqual(tp.load_custom_emoji_cache(self.cache_path), {})  # nothing spuriously cached

    def test_no_token_never_calls_the_api(self):
        def fail(*a, **kw):
            raise AssertionError("should not call the API with no token")

        with self._mock_api(fail):
            out = tp.resolve_custom_emojis(["x"], "", "https://api.telegram.org", self.cache_path)
        self.assertEqual(out, {})

    def test_unresolvable_id_is_simply_missing(self):
        """The sticker had no `emoji` field (or the id was unknown) — get_custom_emoji_stickers already
        drops those, so the id just doesn't come back resolved."""
        with self._mock_api(lambda token, api_base, ids, timeout=15: {}):
            out = tp.resolve_custom_emojis(["x"], "tok", "https://api.telegram.org", self.cache_path)
        self.assertEqual(out, {})

    def test_batch_resolution_across_many_ids(self):
        """More ids than CUSTOM_EMOJI_BATCH split into multiple calls; a modest batch is one call."""
        many = [str(i) for i in range(tp.CUSTOM_EMOJI_BATCH + 5)]
        calls = []

        def fake(token, api_base, ids, timeout=15):
            calls.append(list(ids))
            return {i: "🔥" for i in ids}

        with self._mock_api(fake):
            out = tp.resolve_custom_emojis(many, "tok", "https://api.telegram.org", self.cache_path)
        self.assertEqual(len(out), len(many))
        self.assertEqual(len(calls), 2)  # 200 + 5, batched

    def test_resolve_reactions_fills_in_emoji_in_place(self):
        messages = [
            {"kind": "reaction", "custom_emoji_id": "x", "emoji": ""},
            {"kind": "reaction", "custom_emoji_id": "y", "emoji": ""},
            {"kind": "message", "text": "hi"},  # untouched — not a reaction at all
            {"kind": "reaction", "emoji": "👍"},  # plain emoji — already resolved, untouched
        ]

        def fake(token, api_base, ids, timeout=15):
            return {"x": "🔥", "y": "🎉"}

        with self._mock_api(fake):
            tp.resolve_reactions(messages, "tok", "https://api.telegram.org", self.cache_path)
        self.assertEqual(messages[0]["emoji"], "🔥")
        self.assertEqual(messages[1]["emoji"], "🎉")
        self.assertEqual(messages[2], {"kind": "message", "text": "hi"})
        self.assertEqual(messages[3]["emoji"], "👍")

    def test_resolve_reactions_one_call_for_several_distinct_ids(self):
        """The whole poll batch resolves in one API call, not one per reaction."""
        messages = [{"kind": "reaction", "custom_emoji_id": str(i), "emoji": ""} for i in range(5)]
        calls = []

        def fake(token, api_base, ids, timeout=15):
            calls.append(sorted(ids))
            return {i: "🔥" for i in ids}

        with self._mock_api(fake):
            tp.resolve_reactions(messages, "tok", "https://api.telegram.org", self.cache_path)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], sorted(str(i) for i in range(5)))
        self.assertTrue(all(m["emoji"] == "🔥" for m in messages))

    def test_resolve_reactions_unresolved_id_leaves_emoji_blank(self):
        """Fails open all the way through: an id the API can't resolve leaves `emoji` empty, which
        presence.reaction_intent already maps to the unmapped 'note' intent — never a dropped message."""
        messages = [{"kind": "reaction", "custom_emoji_id": "x", "emoji": "", "message_id": 1}]
        with self._mock_api(lambda token, api_base, ids, timeout=15: {}):
            tp.resolve_reactions(messages, "tok", "https://api.telegram.org", self.cache_path)
        self.assertEqual(messages[0]["emoji"], "")
        ctx = {"intents": pr.load_reaction_intents(tempfile.mkdtemp()), "sent": {}}
        self.assertEqual(pr.reaction_intent(messages[0], ctx), "note")

    def test_resolve_reactions_no_reactions_never_calls_the_api(self):
        def fail(*a, **kw):
            raise AssertionError("should not call the API when nothing needs resolving")

        with self._mock_api(fail):
            tp.resolve_reactions([{"kind": "message", "text": "hi"}], "tok",
                                "https://api.telegram.org", self.cache_path)


class GetCustomEmojiStickers(unittest.TestCase):
    """The Bot API call itself — mocked at the urllib layer, one level lower than the tests above."""

    def _fake_urlopen(self, body: bytes):
        class FakeResp:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s):
                return body
        return lambda req, timeout=None: FakeResp()

    def test_parses_sticker_emoji_fields(self):
        body = json.dumps({"ok": True, "result": [
            {"custom_emoji_id": "x", "emoji": "🔥"},
            {"custom_emoji_id": "y", "emoji": "🎉"},
            {"custom_emoji_id": "z"},  # no emoji field at all — dropped, not crashed on
        ]}).encode("utf-8")
        orig = tp.urllib.request.urlopen
        tp.urllib.request.urlopen = self._fake_urlopen(body)
        try:
            out = tp.get_custom_emoji_stickers("tok", "https://api.telegram.org", ["x", "y", "z"])
        finally:
            tp.urllib.request.urlopen = orig
        self.assertEqual(out, {"x": "🔥", "y": "🎉"})

    def test_api_error_raises(self):
        body = json.dumps({"ok": False, "description": "bad request"}).encode("utf-8")
        orig = tp.urllib.request.urlopen
        tp.urllib.request.urlopen = self._fake_urlopen(body)
        try:
            with self.assertRaises(RuntimeError):
                tp.get_custom_emoji_stickers("tok", "https://api.telegram.org", ["x"])
        finally:
            tp.urllib.request.urlopen = orig

    def test_empty_ids_short_circuits_with_no_network(self):
        def fail(req, timeout=None):
            raise AssertionError("should not touch the network for an empty id list")

        orig = tp.urllib.request.urlopen
        tp.urllib.request.urlopen = fail
        try:
            self.assertEqual(tp.get_custom_emoji_stickers("tok", "https://api.telegram.org", []), {})
        finally:
            tp.urllib.request.urlopen = orig


class ReactionIntents(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_defaults_when_no_config(self):
        intents = pr.load_reaction_intents(self.dir)
        self.assertEqual(intents["👍"], "ack")
        self.assertEqual(intents["👎"], "reject")

    def test_every_intent_has_an_emoji_telegram_will_actually_offer(self):
        """The constraint that shaped this map: you can only react with emoji from Telegram's own
        allowed set (Bot API ReactionTypeEmoji), and ⏰/🤚/❔ are NOT in it — so each of those intents
        must also be reachable by an in-set alias, or it's dead config."""
        intents = pr.load_reaction_intents(self.dir)
        reachable = {intent for emoji, intent in intents.items()
                     if pr._norm_emoji(emoji) in TELEGRAM_ALLOWED_REACTIONS}
        self.assertEqual(reachable, set(intents.values()),
                         "an intent is only reachable via emoji Telegram won't let the owner send")

    def test_the_unreactable_originals_are_still_mapped(self):
        """Kept deliberately: they record the intended meaning, and cost nothing."""
        intents = pr.load_reaction_intents(self.dir)
        self.assertEqual(intents["⏰"], "snooze")
        self.assertEqual(intents["🤚"], "hold")
        self.assertEqual(intents["❔"], "elaborate")

    def test_in_set_aliases_carry_the_same_intents(self):
        intents = pr.load_reaction_intents(self.dir)
        for emoji, intent in (("😴", "snooze"), ("🥱", "snooze"), ("🤝", "hold"),
                              ("🙏", "hold"), ("✍", "elaborate"), ("🤔", "elaborate")):
            self.assertEqual(intents[pr._norm_emoji(emoji)], intent, emoji)

    def test_the_shipped_example_matches_the_code_defaults(self):
        """The seed and the fallback must not drift — the owner may copy either into place."""
        seed = os.path.join(SCRIPT_DIR, "..", "state", "telegram-reactions.example.json")
        with open(seed, encoding="utf-8") as fh:
            table = json.load(fh)["reactions"]
        self.assertEqual({pr._norm_emoji(k): v for k, v in table.items()},
                         {pr._norm_emoji(k): v for k, v in pr.DEFAULT_REACTION_INTENTS.items()})

    def test_variation_selector_is_ignored(self):
        """Telegram is inconsistent about the VS16 on ❤ — both forms must resolve."""
        intents = pr.load_reaction_intents(self.dir)
        self.assertEqual(intents[pr._norm_emoji("❤️")], "liked")
        self.assertEqual(intents[pr._norm_emoji("❤")], "liked")

    def test_owner_config_wins(self):
        with open(os.path.join(self.dir, pr.REACTIONS_CONFIG), "w", encoding="utf-8") as fh:
            json.dump({"reactions": {"🔥": "ack", "👍": "elaborate"}}, fh)
        intents = pr.load_reaction_intents(self.dir)
        self.assertEqual(intents["🔥"], "ack")
        self.assertEqual(intents["👍"], "elaborate")  # the owner's file overrides the default outright

    def test_broken_config_falls_open_to_defaults(self):
        for junk in ('{"reactions": []}', "not json at all", "{}"):
            with open(os.path.join(self.dir, pr.REACTIONS_CONFIG), "w", encoding="utf-8") as fh:
                fh.write(junk)
            self.assertEqual(pr.load_reaction_intents(self.dir)["👍"], "ack")


class ReactionLine(unittest.TestCase):
    """What the warm session actually reads."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ctx = {"intents": pr.load_reaction_intents(self.dir),
                    "sent": {"42": {"kind": "reply", "text": "Want me to send it?"}}}

    def test_names_intent_and_quotes_the_target(self):
        line = pr.telegram_inbound_text({"kind": "reaction", "emoji": "👍", "message_id": 42}, self.ctx)
        self.assertEqual(line, '[the owner reacted 👍 (= ack) to: "Want me to send it?"]')

    def test_unknown_message_still_produces_a_line(self):
        line = pr.telegram_inbound_text({"kind": "reaction", "emoji": "👍", "message_id": 999}, self.ctx)
        self.assertIn("an earlier message", line)
        self.assertTrue(line.strip())  # never empty — an empty line gets dropped by telegram_task

    def test_unmapped_emoji_is_a_note(self):
        line = pr.telegram_inbound_text({"kind": "reaction", "emoji": "🦄", "message_id": 42}, self.ctx)
        self.assertIn("(= note)", line)

    def test_works_with_no_ctx_at_all(self):
        line = pr.telegram_inbound_text({"kind": "reaction", "emoji": "👍", "message_id": 42})
        self.assertIn("(= ack)", line)

    def test_long_quote_truncated(self):
        ctx = {"intents": self.ctx["intents"], "sent": {"42": {"text": "x" * 500}}}
        line = pr.telegram_inbound_text({"kind": "reaction", "emoji": "👍", "message_id": 42}, ctx)
        self.assertIn("…", line)
        self.assertLess(len(line), 400)

    def test_a_plain_message_is_untouched_by_the_reaction_path(self):
        self.assertEqual(pr.telegram_inbound_text({"kind": "message", "text": "hi"}, self.ctx), "hi")


class AckableNudge(unittest.TestCase):
    """Phase B's safety predicate (§3.4B). The asymmetry that drives every case here: failing to
    auto-ack costs the owner a few taps; auto-acking wrongly marks meds Done that they never took."""

    def setUp(self):
        self.now = datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc)
        self.today = self.now.isoformat().replace("+00:00", "Z")
        self.intents = {pr._norm_emoji(k): v for k, v in pr.DEFAULT_REACTION_INTENTS.items()}

    def _ctx(self, **entry):
        base = {"kind": "nudge", "text": "⏰ Reminder: meds", "reminder_id": "row-meds",
                "sent_at": self.today}
        base.update(entry)
        return {"intents": self.intents, "sent": {"42": base}}

    def _react(self, emoji="👍", message_id=42):
        return {"kind": "reaction", "emoji": emoji, "message_id": message_id}

    def test_thumbs_up_on_todays_nudge_acks(self):
        self.assertEqual(pr.ackable_nudge(self._react(), self._ctx(), self.now), "row-meds")

    def test_yesterdays_nudge_does_not_ack(self):
        """The one that would mark today's meds Done off a stale 👍. Observe-only instead."""
        yesterday = (self.now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        self.assertIsNone(pr.ackable_nudge(self._react(), self._ctx(sent_at=yesterday), self.now))

    def test_non_ack_emoji_does_not_ack(self):
        for emoji in ("❤", "👎", "⏰", "🤚", "❔", "🦄"):
            self.assertIsNone(pr.ackable_nudge(self._react(emoji), self._ctx(), self.now), emoji)

    def test_thumbs_up_on_a_chat_reply_does_not_auto_ack(self):
        """"👍 a question = yes" is a judgment call — it belongs to the LLM tier, not the daemon."""
        self.assertIsNone(pr.ackable_nudge(
            self._react(), self._ctx(kind="reply", reminder_id=None), self.now))

    def test_nudge_without_a_row_id_does_not_ack(self):
        self.assertIsNone(pr.ackable_nudge(self._react(), self._ctx(reminder_id=None), self.now))

    def test_untracked_message_does_not_ack(self):
        self.assertIsNone(pr.ackable_nudge(self._react(message_id=999), self._ctx(), self.now))

    def test_garbled_or_missing_timestamp_does_not_ack(self):
        for bad in ("not-a-date", "", None):
            self.assertIsNone(pr.ackable_nudge(self._react(), self._ctx(sent_at=bad), self.now), bad)
        ctx = self._ctx()
        del ctx["sent"]["42"]["sent_at"]
        self.assertIsNone(pr.ackable_nudge(self._react(), ctx, self.now))

    def test_no_ctx_never_acks(self):
        self.assertIsNone(pr.ackable_nudge(self._react(), None, self.now))

    def test_owner_config_can_move_ack_to_another_emoji(self):
        ctx = self._ctx()
        ctx["intents"] = {"🔥": "ack"}
        self.assertEqual(pr.ackable_nudge(self._react("🔥"), ctx, self.now), "row-meds")
        self.assertIsNone(pr.ackable_nudge(self._react("👍"), ctx, self.now))


class AckedLineTellsAssistant(unittest.TestCase):
    def test_acked_line_says_not_to_repeat_it(self):
        ctx = {"intents": {"👍": "ack"}, "sent": {"42": {"kind": "nudge", "text": "⏰ Reminder: meds"}}}
        line = pr.telegram_inbound_text({"kind": "reaction", "emoji": "👍", "message_id": 42},
                                        ctx, acked=True)
        self.assertIn("already run the ack", line)
        self.assertIn("no need to repeat it", line)
        self.assertTrue(line.endswith("]"))

    def test_unacked_line_makes_no_such_claim(self):
        ctx = {"intents": {"👍": "ack"}, "sent": {"42": {"kind": "nudge", "text": "⏰ Reminder: meds"}}}
        line = pr.telegram_inbound_text({"kind": "reaction", "emoji": "👍", "message_id": 42}, ctx)
        self.assertNotIn("already run the ack", line)


class AckByReaction(unittest.TestCase):
    """The action itself — the same calls the chat ack path makes, so they can't drift apart. The
    outbox leg is Notion-backend-only (seneschal's store seam — see test_presence_state's
    ReactionAckOutboxGate for the gate itself), so these pin the notion-backend shape."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.calls = []
        # Pin the backend to notion so the outbox leg is in play regardless of the host's store config.
        self._backend = unittest.mock.patch.object(pr, "store_backend_active", return_value="notion")
        self._backend.start()
        self.addCleanup(self._backend.stop)

    def _run(self, rc=0, stderr=""):
        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            return type("P", (), {"returncode": rc, "stderr": stderr, "stdout": "{}"})()
        return fake_run

    def test_runs_dequeue_and_outbox_ack(self):
        with unittest.mock.patch.object(pr.subprocess, "run", self._run()):
            ok = pr.ack_reminder_by_reaction(self.dir, "row-1", lambda *_: None)
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(any("reminders_dequeue.py" in c for c in self.calls[0]))
        self.assertIn("row-1", self.calls[0])
        self.assertTrue(any("outbox.py" in c for c in self.calls[1]))
        self.assertIn("ack", self.calls[1])

    def test_failure_is_reported_not_raised(self):
        logged = []
        with unittest.mock.patch.object(pr.subprocess, "run", self._run(rc=1, stderr="nope")):
            ok = pr.ack_reminder_by_reaction(self.dir, "row-1", logged.append)
        self.assertFalse(ok)
        self.assertEqual(len(logged), 2)

    def test_exception_is_swallowed(self):
        with unittest.mock.patch.object(pr.subprocess, "run", side_effect=OSError("boom")):
            self.assertFalse(pr.ack_reminder_by_reaction(self.dir, "row-1", lambda *_: None))


class SentMessageMap(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_records_and_reads_back(self):
        sn.record_sent_message(self.dir, {"ok": True, "message_id": 7}, "nudge", "⏰ Reminder: meds",
                               reminder_id="row-1")
        entry = sn.load_message_map(self.dir)["7"]
        self.assertEqual(entry["kind"], "nudge")
        self.assertEqual(entry["reminder_id"], "row-1")
        self.assertIn("meds", entry["text"])

    def test_failed_send_records_nothing(self):
        sn.record_sent_message(self.dir, {"ok": False, "error": "boom"}, "reply", "hi")
        sn.record_sent_message(self.dir, {"ok": True}, "reply", "no message_id")
        sn.record_sent_message(self.dir, {}, "reply", "hi")
        self.assertEqual(sn.load_message_map(self.dir), {})

    def test_missing_and_broken_map_read_empty(self):
        self.assertEqual(sn.load_message_map(self.dir), {})
        with open(os.path.join(self.dir, sn.TELEGRAM_MESSAGE_MAP), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(sn.load_message_map(self.dir), {})

    def test_bounded_to_the_newest(self):
        base = datetime(2026, 7, 16, tzinfo=timezone.utc)
        for i in range(sn.MESSAGE_MAP_CAP + 25):
            sn.record_sent_message(self.dir, {"ok": True, "message_id": i}, "reply", f"m{i}",
                                   now=base + timedelta(seconds=i))
        sent = sn.load_message_map(self.dir)
        self.assertEqual(len(sent), sn.MESSAGE_MAP_CAP)
        self.assertNotIn("0", sent)                                   # oldest evicted
        self.assertIn(str(sn.MESSAGE_MAP_CAP + 24), sent)             # newest kept

    def test_never_raises_into_a_send_path(self):
        """The map is context, not truth — a nudge must go out even if this can't be written."""
        sn.record_sent_message(os.path.join(self.dir, "no", "such", "\0bad"),
                               {"ok": True, "message_id": 1}, "nudge", "x")


if __name__ == "__main__":
    unittest.main()
