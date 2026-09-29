#!/usr/bin/env python3
"""Tests for **Markdown → Telegram HTML at the send boundary** (`telegram_format.py`).

The assistant writes Markdown and a plain-text channel shows the owner literal `**` around every
emphasised phrase. The design is *convert at the send boundary, fall back to plain if the API
rejects it* rather than *just set MarkdownV2*, and the reason is what this file is mostly about: in
MarkdownV2 an unescaped character is a 400 and **the message is gone**, on the channel that carries
the owner's critical reminders.

**`MessageIsNeverLostTests` IS THE POINT OF THIS FILE AND IS DELIBERATELY FIRST.** Everything below
it is about output being prettier; that class is about output existing. If a change here makes it
red, the change is wrong however good the rendering got. Its three cases are the three ways this
feature could eat a message — the API rejecting the HTML, the converter itself raising, and a
message too long for one Telegram message — and each asserts the same thing: **the original,
unconverted string reaches the wire, exactly once.**

**`AmbiguousFailureIsNeverReSentTests` IS ITS OTHER HALF.** *Exactly once* has two edges. A
fallback that fires on **any** exception — including the one `telegram_http` specifically refuses
to retry, a failure after the request went out, where Telegram may already have delivered the
message — turns the layer built to never lose a message into the layer that duplicates one: the
same long reply twice, once HTML and once plain, and duplicate reminder nudges that nobody catches
because the log row looks like every healthy fallback. **A green `MessageIsNeverLost` and a red `AmbiguousFailureIsNeverReSent` is not a
trade-off, it is the same bug from the other side.** Both stay green.

The rest pins the constructs, and two rules that exist because of how the assistant writes:

  * **`telegram_send.py` must not become `telegram<i>send.</i>py`.** Identifiers with underscores
    turn up in messages constantly. An `_` only opens emphasis off a word boundary, and `__init__` is why
    `__bold__` is not supported at all.
  * **A cut must never land inside a tag.** The source is chunked *before* conversion, so a tag
    cannot straddle a boundary by construction — `ChunkingTests` asserts the property (every chunk
    parses closed and re-concatenates to the original) rather than a byte count that would need
    re-measuring every time the converter's output shifts by a character.

No network anywhere. Every send path is driven through its `api=` seam with a fake — `telegram.env`
may exist on this host, and a careless test here messages the owner for real.

Run:  python -m unittest seneschal.scripts.test_telegram_format   (or)   python test_telegram_format.py
"""
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import telegram_ask as ta  # noqa: E402
import telegram_format as tf  # noqa: E402
import telegram_http as th  # noqa: E402
import telegram_send as ts  # noqa: E402

MD = tf.FORMAT_MARKDOWN


def html(text: str) -> str:
    return tf.to_html(text)


class FakeAPI:
    """A stand-in for `telegram_send.api_call`. Records every call; raises for the `sendMessage`
    attempts whose index is in `fail_on`, which is how a 400 from Telegram is simulated without one.

    **It raises what the real `api_call` raises, and that is a TYPE, not just a string.** A 400 is a `TelegramAPIError` (Telegram answered and refused) and the fallback turns
    on recognising it; a stub that raised a bare `RuntimeError` would be modelling *"we don't know
    what happened"*, which now correctly does NOT fall back. `raises=` overrides the exception for
    the tests that need the other side of that line."""

    def __init__(self, fail_on=(), error="Telegram sendMessage failed: Bad Request: can't parse entities",
                 raises=None):
        self.calls = []
        self.fail_on = set(fail_on)
        self.error = error
        self.raises = raises
        self._sends = 0

    def __call__(self, c, method, params):
        self.calls.append({"method": method, "params": params})
        if method == "sendMessage":
            index, self._sends = self._sends, self._sends + 1
            if index in self.fail_on:
                raise self.raises or ts.TelegramAPIError(self.error, method=method)
        return {"ok": True, "result": {"message_id": 100 + len(self.calls)}}

    @property
    def sends(self):
        return [c for c in self.calls if c["method"] == "sendMessage"]


def ambiguous(message="The read operation timed out — failed after the request was sent"):
    """The exact exception `telegram_http` raises when a send dies with the request already on the
    wire — the shape behind a duplicated reply."""
    return th.TelegramHTTPError(message, phase=th.PHASE_AMBIGUOUS, attempts=1)


def cfg(fmt=MD, parse_mode=""):
    return {"token": "t", "chat_id": "42", "api_base": "https://example.invalid",
            "parse_mode": parse_mode, "format": tf.normalize_format(fmt)}


# ===========================================================================================
# The invariant that outranks everything else here.
# ===========================================================================================

class MessageIsNeverLostTests(unittest.TestCase):
    """A message must never be lost to a formatting failure. Three ways it could be; none of them
    may be."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_api_400_resends_the_original_plain_exactly_once(self):
        """THE case the design turns on. Telegram rejects the HTML → the same message goes out
        again immediately, as the ORIGINAL unconverted string, with no parse mode, once."""
        source = "**Quarterly tax estimate** is due — day 5. Ack in `⏰` or tell me."
        api = FakeAPI(fail_on=[0])
        res = ts.send_text(cfg(), source, api=api, state_dir=self.dir)

        self.assertEqual(len(api.sends), 2, "one rejected HTML attempt, one plain retry — no more")
        self.assertEqual(api.sends[0]["params"]["parse_mode"], "HTML")
        self.assertEqual(api.sends[1]["params"]["text"], source,
                         "the retry carries the ORIGINAL string, not a half-converted one")
        self.assertIsNone(api.sends[1]["params"]["parse_mode"])
        self.assertTrue(res["degraded"])
        self.assertEqual([f["chunk"] for f in res["fallbacks"]], [0])

    def test_the_fallback_is_logged(self):
        """A degraded send that nobody can see is how "is this feature working?" becomes unanswerable."""
        ts.send_text(cfg(), "**hi**", api=FakeAPI(fail_on=[0]), state_dir=self.dir)
        rows = self._log_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("can't parse entities", rows[0]["error"])
        self.assertTrue(rows[0]["resent"], "a real fallback DID re-send; the log must say so")
        self.assertEqual(rows[0]["delivery"], ts.SEND_REJECTED)

    def _log_rows(self):
        path = os.path.join(self.dir, ts.FORMAT_FALLBACK_LOG)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_a_converter_that_raises_still_sends_the_original(self):
        """A bug in the converter may cost the formatting and nothing else."""
        original = tf.to_html
        tf.to_html = lambda *a, **k: 1 / 0
        try:
            api = FakeAPI()
            ts.send_text(cfg(), "**boom** _and_ `more`", api=api, state_dir=self.dir)
        finally:
            tf.to_html = original
        self.assertEqual(len(api.sends), 1)
        self.assertEqual(api.sends[0]["params"]["text"], "**boom** _and_ `more`")
        self.assertIsNone(api.sends[0]["params"]["parse_mode"])

    def test_an_over_long_message_is_split_rather_than_rejected(self):
        """4096 is Telegram's hard cap: a longer message is a 400 and, before this, a lost message."""
        source = "\n".join(f"Line {i} of a very long **bold** report." for i in range(400))
        api = FakeAPI()
        res = ts.send_text(cfg(), source, api=api, state_dir=self.dir)
        self.assertGreater(res["chunks"], 1)
        self.assertEqual(len(api.sends), res["chunks"])
        for send in api.sends:
            self.assertLessEqual(len(send["params"]["text"]), tf.TELEGRAM_MESSAGE_LIMIT)
        self.assertFalse(res["degraded"], "splitting is not a failure and must not degrade the rest")

    def test_a_mid_message_rejection_degrades_the_remainder_and_duplicates_nothing(self):
        """Chunk 2 of 3 is rejected. Chunk 1 already landed as HTML and must NOT be resent; chunks 2
        and 3 go plain, so one message never arrives half-formatted."""
        source = "\n".join(f"Paragraph {i} with **bold** in it." for i in range(400))
        plan = tf.plan_send(source, MD)
        self.assertGreaterEqual(len(plan), 3, "fixture must actually produce three chunks")
        api = FakeAPI(fail_on=[1])
        res = ts.send_text(cfg(), source, api=api, state_dir=self.dir)

        self.assertEqual(len(api.sends), len(plan) + 1, "exactly one extra send: the single retry")
        self.assertEqual(api.sends[0]["params"]["parse_mode"], "HTML")
        self.assertEqual(api.sends[2]["params"]["text"], plan[1]["source"])
        for send in api.sends[2:]:
            self.assertIsNone(send["params"]["parse_mode"], "everything after the fallback is plain")
        self.assertTrue(res["degraded"])

    def test_a_plain_retry_that_also_fails_reports_failure(self):
        """The floor is honest reporting, not silent success."""
        api = FakeAPI(fail_on=[0, 1])
        with self.assertRaises(RuntimeError):
            ts.send_text(cfg(), "**hi**", api=api, state_dir=self.dir)

    def test_conversion_never_raises_on_anything(self):
        """`to_html` is total. Every one of these is a real shape either side of a chat types."""
        for source in ("", "*", "**", "_", "`", "```", "~~", "[", "[a](", "[a](b)", "<", ">&<",
                       "|", "|---|", "#", "###", "> ", "---", "***", "___", "a`b", "**a", "_a",
                       "```py\nx = 1", "\n\n\n", "* ", "- ", "1. ", "\\", "&amp;", "<b>hi</b>"):
            with self.subTest(source=source):
                tf.to_html(source)          # must not raise
                tf.plan_send(source, MD)    # nor must planning a send of it


# ===========================================================================================
# The other half of the same invariant: a message is never DOUBLED.
# ===========================================================================================

class AmbiguousFailureIsNeverReSentTests(unittest.TestCase):
    """**A message may never be sent TWICE by a formatting fallback.**

    `telegram_http` classifies a failed send by phase and refuses to retry an AMBIGUOUS one — the
    request went out, so Telegram may already have delivered it. A `send_text` that caught every
    exception and re-sent the original as plain text would defeat that guarantee one level up: the
    HTML had landed, and the plain re-send is the second copy — and its log row is the same shape as
    every healthy fallback, so nobody notices. The class above proves a message is never LOST; this
    one proves it is never DOUBLED. Both must stay green."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _rows(self):
        path = os.path.join(self.dir, ts.FORMAT_FALLBACK_LOG)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_an_ambiguous_failure_produces_exactly_one_send_attempt(self):
        """THE regression. One attempt out, one attempt total — no plain re-send, ever."""
        api = FakeAPI(fail_on=[0], raises=ambiguous())
        with self.assertRaises(ts.AmbiguousSendError):
            ts.send_text(cfg(), "**Quarterly tax estimate** is due — day 5.", api=api,
                         state_dir=self.dir)
        self.assertEqual(len(api.sends), 1,
                         "the request went out once; a second send is the duplicate")
        self.assertEqual(api.sends[0]["params"]["parse_mode"], "HTML")

    def test_the_real_transport_error_is_the_one_that_stops_it(self):
        """Not a synthetic exception: the verbatim `TelegramHTTPError` shape from the 01:12 row,
        raised the way `telegram_http.request` raises it."""
        err = th.TelegramHTTPError(
            "https://api.telegram.org/botX/sendMessage: The read operation timed out — failed "
            "after the request was sent, so Telegram may already have acted on it; NOT retried "
            "(a blind retry here duplicates the message)",
            phase=th.PHASE_AMBIGUOUS, attempts=1)
        api = FakeAPI(fail_on=[0], raises=err)
        with self.assertRaises(ts.AmbiguousSendError):
            ts.send_text(cfg(), "**hi**", api=api, state_dir=self.dir)
        self.assertEqual(len(api.sends), 1)

    def test_a_400_style_rejection_still_falls_back_to_plain(self):
        """The other direction, which must keep working exactly as it does today: Telegram ANSWERED
        and refused the entities, so the same words go out once more, plain and unconverted."""
        source = "**Quarterly tax estimate** is due — day 5. Ack in `⏰` or tell me."
        api = FakeAPI(fail_on=[0])
        res = ts.send_text(cfg(), source, api=api, state_dir=self.dir)
        self.assertEqual(len(api.sends), 2)
        self.assertEqual(api.sends[1]["params"]["text"], source)
        self.assertIsNone(api.sends[1]["params"]["parse_mode"])
        self.assertTrue(res["degraded"])

    def test_a_pre_delivery_failure_still_falls_back(self):
        """`PHASE_PRE_DELIVERY` means the phase seam proved not one byte was written — nothing was
        delivered, so re-sending cannot duplicate. That case happens in practice and the fix must
        not cost it."""
        api = FakeAPI(fail_on=[0], raises=th.TelegramHTTPError(
            "[WinError 10054] connection forcibly closed", phase=th.PHASE_PRE_DELIVERY, attempts=1))
        res = ts.send_text(cfg(), "**hi**", api=api, state_dir=self.dir)
        self.assertEqual(len(api.sends), 2)
        self.assertTrue(res["degraded"])
        self.assertEqual(self._rows()[0]["delivery"], ts.SEND_UNDELIVERED)

    def test_an_unknown_exception_defaults_to_not_re_sending(self):
        """A bare exception carries no phase, so nobody can prove the words did not land. **Unknown
        reads as ambiguous.** Under-sending is recoverable; double-sending is not — and a future
        failure mode nobody has thought of gets the safe answer by default rather than by luck."""
        for exc in (RuntimeError("HTTP 502 from Telegram sendMessage"),
                    th.TelegramHTTPError("HTTP 429 (gave up after 3 attempts)", attempts=3),
                    ValueError("something nobody has thought about yet")):
            with self.subTest(exc=type(exc).__name__):
                api = FakeAPI(fail_on=[0], raises=exc)
                with self.assertRaises(Exception):
                    ts.send_text(cfg(), "**hi**", api=api, state_dir=tempfile.mkdtemp())
                self.assertEqual(len(api.sends), 1)

    def test_the_classifier_reads_the_phase_not_the_string(self):
        """The message text is a sentence a later edit may reword; `.phase` is the contract."""
        self.assertEqual(ts.classify_send_failure(ambiguous()), ts.SEND_AMBIGUOUS)
        self.assertEqual(ts.classify_send_failure(
            th.TelegramHTTPError("x", phase=th.PHASE_PRE_DELIVERY)), ts.SEND_UNDELIVERED)
        self.assertEqual(ts.classify_send_failure(
            ts.TelegramAPIError("Telegram sendMessage failed: Bad Request")), ts.SEND_REJECTED)
        # A 429 telegram_http gave up on (ceiling exceeded, or attempts/wall-clock spent) is tagged
        # PHASE_REFUSED at the raise site — the server refused, so nothing was delivered, and this
        # must NOT fall through to the ambiguous default the way a bare `phase=None` 429 used to.
        self.assertEqual(ts.classify_send_failure(
            th.TelegramHTTPError("HTTP 429, retry_after 3600s exceeds the 60s ceiling",
                                 phase=th.PHASE_REFUSED)), ts.SEND_REJECTED)
        # A bare RuntimeError whose text *mentions* a rejection is still unknown. Reading the
        # sentence instead of the type is the mistake this whole change exists to stop.
        self.assertEqual(ts.classify_send_failure(
            RuntimeError("Bad Request: can't parse entities")), ts.SEND_AMBIGUOUS)

    def test_the_log_row_says_nothing_was_re_sent(self):
        """Requirement 4: an ambiguous non-fallback must be DISTINGUISHABLE from a real fallback in
        `telegram-format-fallback.jsonl`. Without that, duplicate reminders sit in the log looking
        like every other row."""
        api = FakeAPI(fail_on=[0], raises=ambiguous())
        with self.assertRaises(ts.AmbiguousSendError):
            ts.send_text(cfg(), "⏰ Reminder: water the plants.", api=api,
                         state_dir=self.dir)
        rows = self._rows()
        self.assertEqual(len(rows), 1, "the refusal is still recorded — silence is how this hid")
        self.assertFalse(rows[0]["resent"])
        self.assertEqual(rows[0]["delivery"], ts.SEND_AMBIGUOUS)
        self.assertEqual(rows[0]["delivered_chunks"], 0)

    def test_the_remaining_chunks_are_not_sent_and_the_accounting_is_exact(self):
        """The decision: chunk N's fate is unknown and N+1… are provably unsent, so the send
        STOPS. A message with an invisible hole in the middle reads as the assistant being wrong.
        What landed has to be answerable, so the error names it."""
        source = "\n".join(f"Paragraph {i} with **bold** in it." for i in range(400))
        plan = tf.plan_send(source, MD)
        self.assertGreaterEqual(len(plan), 3, "fixture must actually produce three chunks")
        api = FakeAPI(fail_on=[1], raises=ambiguous())
        with self.assertRaises(ts.AmbiguousSendError) as caught:
            ts.send_text(cfg(), source, api=api, state_dir=self.dir)

        self.assertEqual(len(api.sends), 2, "chunk 1 landed, chunk 2 was attempted, then we stopped")
        err = caught.exception
        self.assertEqual(err.chunk, 1)
        self.assertEqual(err.chunks, len(plan))
        self.assertEqual(err.delivered, 1)
        self.assertEqual(len(err.message_ids), 1)
        self.assertEqual(self._rows()[0]["delivered_chunks"], 1)

    def test_a_plain_mode_send_reports_the_ambiguity_without_logging_a_fallback(self):
        """No HTML was attempted, so there was no fallback decision to record — but the caller must
        still be told it may not try again."""
        api = FakeAPI(fail_on=[0], raises=ambiguous())
        with self.assertRaises(th.TelegramHTTPError):
            ts.send_text(cfg(fmt=tf.FORMAT_PLAIN), "plain nudge", api=api, state_dir=self.dir)
        self.assertEqual(len(api.sends), 1)
        self.assertEqual(self._rows(), [])


class TheCliTellsTheCallerNotToRetryTests(unittest.TestCase):
    """`ambiguous: true` in the result JSON is the whole reason the fix survives its own callers:
    `presence.deliver_reply` retries a failed send once, which would simply move the duplicate one
    layer up. The flag travels with the failure so nobody has to read the error text."""

    def _run(self, raises):
        env = tempfile.mkdtemp()
        env_file = os.path.join(env, "telegram.env")
        with open(env_file, "w", encoding="utf-8") as fh:
            fh.write("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=42\nTELEGRAM_FORMAT=markdown\n")
        argv = ["telegram_send.py", "--text", "**hi**", "--env-file", env_file,
                "--state-dir", env, "--no-ack-gate"]
        real_argv, real_stdout = sys.argv, sys.stdout
        sys.argv = argv
        sys.stdout = io.StringIO()
        try:
            def api(c, method, params, timeout=30):
                raise raises
            with mock.patch.object(ts, "api_call", api):
                rc = ts.main()
            return rc, json.loads(sys.stdout.getvalue().strip().splitlines()[-1])
        finally:
            sys.argv, sys.stdout = real_argv, real_stdout

    def test_an_ambiguous_failure_is_flagged(self):
        rc, out = self._run(ambiguous())
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertTrue(out["ambiguous"], "the caller MUST NOT re-send this text")
        self.assertEqual(out["delivery"], ts.SEND_AMBIGUOUS)
        self.assertEqual(out["chunks"], 1)
        self.assertEqual(out["delivered_chunks"], 0)
        self.assertEqual(out["unsent_chunks"], 0)

    def test_a_rejection_that_also_fails_plain_is_not_flagged_ambiguous(self):
        """Both attempts refused by Telegram: it failed, but nothing was delivered, so a caller is
        free to try again. Flagging this would turn a recoverable failure into a silent drop."""
        rc, out = self._run(ts.TelegramAPIError("Telegram sendMessage failed: Bad Request"))
        self.assertEqual(rc, 1)
        self.assertNotIn("ambiguous", out)
        self.assertEqual(out["delivery"], ts.SEND_REJECTED)


class TheQuestionPickerRefusesToDoubleAskTests(unittest.TestCase):
    """`telegram_ask._send_text` carried the identical `except Exception` → re-send shape, where the
    duplicate is a second live picker for the same question — exactly the buzz a merge-ask dedupe
    log exists to prevent."""

    def test_an_ambiguous_question_send_is_not_re_sent(self):
        calls = []

        def api(c, method, params):
            calls.append(params)
            raise ambiguous()

        with self.assertRaises(th.TelegramHTTPError):
            ta._send_text(api, cfg(), "sendMessage", {"chat_id": "42"}, "**Merge pull request 417?**")
        self.assertEqual(len(calls), 1)

    def test_a_rejected_question_still_falls_back_to_plain(self):
        calls = []

        def api(c, method, params):
            calls.append(params)
            if params.get("parse_mode") == "HTML":
                raise ts.TelegramAPIError("Telegram sendMessage failed: can't parse entities")
            return {"ok": True, "result": {"message_id": 9}}

        ta._send_text(api, cfg(), "sendMessage", {"chat_id": "42"}, "**Merge pull request 417?**")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["text"], "**Merge pull request 417?**")
        self.assertIsNone(calls[1]["parse_mode"])


# ===========================================================================================
# Escaping — first, because everything else emits tags on top of it.
# ===========================================================================================

class EscapingTests(unittest.TestCase):

    def test_a_message_that_is_only_a_less_than(self):
        """It sends fine today and must keep doing so."""
        self.assertEqual(html("<"), "&lt;")

    def test_the_three_characters(self):
        self.assertEqual(html("a & b < c > d"), "a &amp; b &lt; c &gt; d")

    def test_ampersand_is_escaped_first(self):
        """`&` before `<`/`>`: the other order double-escapes every entity it just introduced."""
        self.assertEqual(html("&lt;"), "&amp;lt;")
        self.assertEqual(html("5 < 6 && 7 > 6"), "5 &lt; 6 &amp;&amp; 7 &gt; 6")

    def test_html_in_the_source_is_inert(self):
        """The assistant quoting a tag must not emit one — the injection-shaped bug."""
        self.assertEqual(html("<b>not bold</b>"), "&lt;b&gt;not bold&lt;/b&gt;")
        self.assertEqual(html("<script>x</script>"), "&lt;script&gt;x&lt;/script&gt;")

    def test_escaping_inside_every_construct(self):
        self.assertEqual(html("**a < b**"), "<b>a &lt; b</b>")
        self.assertEqual(html("`a < b`"), "<code>a &lt; b</code>")
        self.assertEqual(html("# a & b"), "<b>a &amp; b</b>")
        self.assertEqual(html("> a > b"), "<blockquote>a &gt; b</blockquote>")

    def test_a_url_is_attribute_escaped(self):
        """A query string is mostly `&`, and an href is a quoted attribute."""
        self.assertEqual(html("[q](https://example.com/a?x=1&y=2)"),
                          '<a href="https://example.com/a?x=1&amp;y=2">q</a>')
        self.assertEqual(html('[q](https://example.com/")'),
                          '<a href="https://example.com/&quot;">q</a>',
                          "a quote in the URL may not close the attribute it sits in")


# ===========================================================================================
# Inline constructs.
# ===========================================================================================

class InlineConstructTests(unittest.TestCase):

    def test_each_construct(self):
        self.assertEqual(html("**bold**"), "<b>bold</b>")
        self.assertEqual(html("*italic*"), "<i>italic</i>")
        self.assertEqual(html("_italic_"), "<i>italic</i>")
        self.assertEqual(html("~~gone~~"), "<s>gone</s>")
        self.assertEqual(html("`code`"), "<code>code</code>")
        self.assertEqual(html("[text](https://x.dev)"), '<a href="https://x.dev">text</a>')

    def test_nested_emphasis(self):
        self.assertEqual(html("**bold with _italic_**"), "<b>bold with <i>italic</i></b>")
        self.assertEqual(html("*italic with **bold***"), "<i>italic with <b>bold</b></i>")
        self.assertEqual(html("~~**both**~~"), "<s><b>both</b></s>")
        self.assertEqual(html("**a `c` b**"), "<b>a <code>c</code> b</b>")
        self.assertEqual(html("[**b**](https://x.dev)"), '<a href="https://x.dev"><b>b</b></a>')

    def test_a_code_span_is_literal_inside(self):
        """Markers inside backticks are text. This is what makes `**kwargs` and `a_b_c` safe."""
        self.assertEqual(html("`**not bold**`"), "<code>**not bold**</code>")
        self.assertEqual(html("`a_b_c`"), "<code>a_b_c</code>")
        self.assertEqual(html("``a ` b``"), "<code>a ` b</code>")

    def test_multiple_constructs_on_one_line(self):
        self.assertEqual(html("**a** and *b* and `c`"),
                         "<b>a</b> and <i>b</i> and <code>c</code>")

    def test_only_schemes_telegram_accepts_become_links(self):
        self.assertEqual(html("[t](https://x.dev)"), '<a href="https://x.dev">t</a>')
        self.assertEqual(html("[t](mailto:owner@example.com)"),
                          '<a href="mailto:owner@example.com">t</a>')
        self.assertEqual(html("[t](javascript:alert(1))"), "[t](javascript:alert(1))")
        self.assertEqual(html("[t](/local/path)"), "[t](/local/path)")

    def test_a_bare_url_is_left_for_telegram_to_autolink(self):
        self.assertEqual(html("see https://x.dev/a_b"), "see https://x.dev/a_b")


class UnderscoreRuleTests(unittest.TestCase):
    """The rule that prevents a large class of mangling, in the vocabulary messages actually use."""

    def test_identifiers_round_trip_literally(self):
        for identifier in ("reminders_seed.py", "state_dir", "snake_case_name", "__init__.py",
                           "__main__", "reminders_acks.watch_escalation_blocked",
                           "TELEGRAM_PARSE_MODE", "a_b_c_d"):
            with self.subTest(identifier=identifier):
                self.assertEqual(html(identifier), identifier)

    def test_an_identifier_mid_sentence(self):
        self.assertEqual(html("I patched reminders_seed.py and telegram_send.py tonight."),
                          "I patched reminders_seed.py and telegram_send.py tonight.")

    def test_real_underscore_emphasis_still_works(self):
        self.assertEqual(html("this is _important_ now"), "this is <i>important</i> now")
        self.assertEqual(html("(_yes_)"), "(<i>yes</i>)")

    def test_double_underscore_is_not_bold(self):
        """`__bold__` is unsupported on purpose: it would make `__init__.py` a bold *init*."""
        self.assertEqual(html("__init__"), "__init__")
        self.assertEqual(html("__bold__"), "__bold__")


class StrayMarkerTests(unittest.TestCase):
    """Unmatched or stray markers must not break the message — the brief's hard requirement."""

    def test_a_lone_star(self):
        self.assertEqual(html("2 * 3 = 6"), "2 * 3 = 6")
        self.assertEqual(html("a lone * here"), "a lone * here")
        self.assertEqual(html("trailing *"), "trailing *")

    def test_an_unclosed_bold(self):
        self.assertEqual(html("**never closed"), "**never closed")
        self.assertEqual(html("~~never closed"), "~~never closed")

    def test_an_unclosed_backtick(self):
        self.assertEqual(html("run `python x.py and see"), "run `python x.py and see")

    def test_an_unclosed_fence_still_renders(self):
        """It runs to the end of the message rather than swallowing it."""
        self.assertEqual(html("```\nx = 1"), "<pre>x = 1</pre>")

    def test_bullets_are_not_emphasis(self):
        self.assertEqual(html("* first\n* second"), "* first\n* second")
        self.assertEqual(html("- first\n- second"), "- first\n- second")
        self.assertEqual(html("1. first\n2. second"), "1. first\n2. second")

    def test_bullets_still_carry_inline_emphasis(self):
        self.assertEqual(html("- a **bold** item"), "- a <b>bold</b> item")

    def test_emphasis_needs_non_whitespace_content(self):
        self.assertEqual(html("a ** b ** c"), "a ** b ** c")

    def test_no_empty_entities_are_emitted(self):
        """Telegram rejects an entity with no content, so none may be produced."""
        for source in ("****", "``", "##", "> ", "~~~~", "[]()"):
            with self.subTest(source=source):
                self.assertNotIn("<b></b>", html(source))
                self.assertNotIn("<i></i>", html(source))
                self.assertNotIn("<code></code>", html(source))
                self.assertNotIn("<blockquote></blockquote>", html(source))


# ===========================================================================================
# Block constructs.
# ===========================================================================================

class BlockConstructTests(unittest.TestCase):

    def test_headings_become_bold_lines(self):
        """Nothing in Telegram renders a heading."""
        self.assertEqual(html("# Top"), "<b>Top</b>")
        self.assertEqual(html("## Second"), "<b>Second</b>")
        self.assertEqual(html("### Third"), "<b>Third</b>")
        self.assertEqual(html("#### Fourth"), "<b>Fourth</b>")

    def test_a_hash_without_a_space_is_not_a_heading(self):
        self.assertEqual(html("#hashtag"), "#hashtag")
        self.assertEqual(html("Issue #387 merged"), "Issue #387 merged")

    def test_a_fenced_block(self):
        self.assertEqual(html("```\na = 1\nb = 2\n```"), "<pre>a = 1\nb = 2</pre>")

    def test_a_fenced_block_with_a_language(self):
        self.assertEqual(html("```python\nx = 1\n```"),
                          '<pre><code class="language-python">x = 1</code></pre>')

    def test_a_fence_body_is_literal(self):
        self.assertEqual(html("```\n**not bold** & <tag>\n```"),
                          "<pre>**not bold** &amp; &lt;tag&gt;</pre>")

    def test_a_blockquote_run_is_one_quote(self):
        self.assertEqual(html("> one\n> two"), "<blockquote>one\ntwo</blockquote>")
        self.assertEqual(html("> **hi**"), "<blockquote><b>hi</b></blockquote>")

    def test_a_horizontal_rule(self):
        self.assertEqual(html("---"), "——————")
        self.assertEqual(html("***"), "——————")

    def test_a_dash_list_is_not_a_rule(self):
        self.assertEqual(html("- one"), "- one")

    def test_blank_lines_and_layout_survive(self):
        self.assertEqual(html("a\n\nb"), "a\n\nb")


class TableTests(unittest.TestCase):
    """Markdown tables have no Telegram equivalent, so they become a padded `<pre>`: the columns
    lining up is the reason anyone writes a table at all, and `<pre>` is monospace with horizontal
    scrolling on mobile. The documented cost is that a `<pre>` may not contain other tags."""

    TABLE = ("| Row | Due |\n"
             "|-----|-----|\n"
             "| Pay rent | 12:30 |\n"
             "| Shower | 23:30 |")

    def test_a_table_becomes_a_pre_block(self):
        out = html(self.TABLE)
        self.assertTrue(out.startswith("<pre>") and out.endswith("</pre>"))

    def test_the_columns_line_up(self):
        body = html(self.TABLE)[len("<pre>"):-len("</pre>")]
        rows = [line for line in body.split("\n") if "|" in line]
        self.assertEqual(len({line.index("|") for line in rows}), 1,
                         "every row's first separator sits at the same column")

    def test_the_separator_row_is_not_content(self):
        self.assertNotIn("|---", html(self.TABLE))

    def test_cell_emphasis_is_flattened_not_left_literal(self):
        """`<pre>` cannot carry a tag — but leaving the `**` visible is the bug being fixed."""
        out = html("| A | B |\n|---|---|\n| **bold** | `code` |")
        self.assertIn("bold", out)
        self.assertNotIn("**", out)
        self.assertNotIn("<b>", out)

    def test_a_link_in_a_cell_keeps_its_url(self):
        """Not tappable inside a `<pre>`; visible and copyable beats invisible."""
        out = html("| PR | Where |\n|---|---|\n| #387 | [here](https://x.dev/p) |")
        self.assertIn("https://x.dev/p", out)

    def test_a_pipe_in_prose_is_not_a_table(self):
        self.assertEqual(html("run a | b to pipe it"), "run a | b to pipe it")

    def test_a_ragged_table_does_not_break(self):
        html("| A | B | C |\n|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |")   # must not raise


# ===========================================================================================
# Chunking — the trap.
# ===========================================================================================

TAG = re.compile(r"</?([a-z-]+)(?:\s[^>]*)?>")


def tags_are_balanced(fragment: str) -> bool:
    """Every tag in `fragment` opens and closes inside it. A chunk boundary landing inside a tag —
    or between an open and its close — is exactly what produces a 400."""
    stack = []
    for m in TAG.finditer(fragment):
        name = m.group(1)
        if m.group(0).startswith("</"):
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


class ChunkingTests(unittest.TestCase):
    """The source is chunked BEFORE conversion, so a tag cannot straddle a boundary by
    construction. These assert the property, not a byte count."""

    def test_a_short_message_is_one_chunk_and_untouched(self):
        plan = tf.plan_send("**hi**", MD)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["source"], "**hi**")
        self.assertEqual(plan[0]["html"], "<b>hi</b>")

    def test_every_chunk_fits_and_every_chunk_is_closed(self):
        source = "\n".join(f"**Item {i}** — a line of a long report about `thing_{i}`."
                           for i in range(300))
        plan = tf.plan_send(source, MD)
        self.assertGreater(len(plan), 1)
        for piece in plan:
            self.assertLessEqual(len(piece["source"]), tf.TELEGRAM_MESSAGE_LIMIT)
            self.assertLessEqual(len(piece["html"] or ""), tf.TELEGRAM_MESSAGE_LIMIT)
            self.assertTrue(tags_are_balanced(piece["html"] or ""))

    def test_nothing_is_dropped_across_the_boundaries(self):
        source = "\n".join(f"Line {i} about **stuff**." for i in range(300))
        rejoined = "\n".join(p["source"] for p in tf.plan_send(source, MD))
        self.assertEqual(rejoined, source)

    def test_a_split_mid_emphasis_closes_and_reopens(self):
        """One bolded run far longer than a whole message: the cut lands inside the emphasis, and
        the seam must be closed on one side and re-opened on the other rather than leaking a tag."""
        source = "**" + " ".join(f"word{i}" for i in range(2000)) + "**"
        plan = tf.plan_send(source, MD)
        self.assertGreater(len(plan), 1)
        for piece in plan:
            self.assertTrue(tags_are_balanced(piece["html"]),
                            f"unbalanced at the seam: {piece['html'][-60:]!r}")
        self.assertIn("<b>", plan[0]["html"])
        self.assertIn("<b>", plan[1]["html"], "the emphasis is re-opened, not lost")

    def test_a_split_mid_word_still_fits(self):
        """A single token longer than the limit — a pasted URL or a base64 blob."""
        plan = tf.plan_send("x" * 12000, MD)
        self.assertGreater(len(plan), 1)
        for piece in plan:
            self.assertLessEqual(len(piece["html"] or piece["source"]), tf.TELEGRAM_MESSAGE_LIMIT)

    def test_a_long_code_block_is_reopened_as_a_code_block(self):
        """A fence split in half would leave the tail rendering as prose."""
        source = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(600)) + "\n```"
        plan = tf.plan_send(source, MD)
        self.assertGreater(len(plan), 1)
        for piece in plan:
            self.assertTrue(piece["html"].startswith("<pre>")
                            or piece["html"].startswith("<pre><code"))
            self.assertTrue(tags_are_balanced(piece["html"]))

    def test_escaping_expansion_is_measured_not_estimated(self):
        """`&` becomes five characters. A source chunk sized to 4096 would render over it."""
        plan = tf.plan_send("& " * 3000, MD)
        for piece in plan:
            self.assertLessEqual(len(piece["html"] or piece["source"]), tf.TELEGRAM_MESSAGE_LIMIT)

    def test_plain_mode_chunks_too(self):
        """The 4096 cap is Telegram's, not the converter's: a long plain message was a 400 — i.e. a
        lost message — before this, and must not still be."""
        plan = tf.plan_send("y" * 10000, tf.FORMAT_PLAIN)
        self.assertGreater(len(plan), 1)
        for piece in plan:
            self.assertIsNone(piece["html"])
            self.assertLessEqual(len(piece["source"]), tf.TELEGRAM_MESSAGE_LIMIT)


# ===========================================================================================
# The knob.
# ===========================================================================================

class ConfigTests(unittest.TestCase):

    def test_the_default_is_todays_behaviour(self):
        """Unset ⇒ plain ⇒ byte-identical to before this existed."""
        self.assertEqual(tf.normalize_format(""), tf.FORMAT_PLAIN)
        self.assertEqual(tf.normalize_format(None), tf.FORMAT_PLAIN)
        self.assertEqual(ts.cfg({})["format"], tf.FORMAT_PLAIN)

    def test_a_typo_reads_as_plain_rather_than_breaking_the_channel(self):
        self.assertEqual(tf.normalize_format("markdwon"), tf.FORMAT_PLAIN)
        self.assertEqual(tf.normalize_format("MarkdownV2"), tf.FORMAT_PLAIN)

    def test_the_switch(self):
        for value in ("markdown", "Markdown", " MARKDOWN ", "md"):
            with self.subTest(value=value):
                self.assertEqual(tf.normalize_format(value), tf.FORMAT_MARKDOWN)
        self.assertEqual(ts.cfg({"TELEGRAM_FORMAT": "markdown"})["format"], tf.FORMAT_MARKDOWN)

    def test_parse_mode_still_means_what_it_always_meant(self):
        c = ts.cfg({"TELEGRAM_PARSE_MODE": "MarkdownV2"})
        self.assertEqual(c["parse_mode"], "MarkdownV2")
        self.assertEqual(c["format"], tf.FORMAT_PLAIN)

    def test_plain_mode_sends_exactly_what_it_used_to(self):
        api = FakeAPI()
        source = "**bold** _and_ `code` < > &"
        ts.send_text(cfg(fmt=tf.FORMAT_PLAIN), source, api=api, state_dir=tempfile.mkdtemp())
        self.assertEqual(len(api.sends), 1)
        self.assertEqual(api.sends[0]["params"]["text"], source)
        self.assertIsNone(api.sends[0]["params"]["parse_mode"])

    def test_plain_mode_still_honours_an_explicit_parse_mode(self):
        api = FakeAPI()
        ts.send_text(cfg(fmt=tf.FORMAT_PLAIN, parse_mode="HTML"), "<b>x</b>", api=api,
                     state_dir=tempfile.mkdtemp())
        self.assertEqual(api.sends[0]["params"]["parse_mode"], "HTML")

    def test_markdown_mode_sends_html(self):
        api = FakeAPI()
        ts.send_text(cfg(), "**bold**", api=api, state_dir=tempfile.mkdtemp())
        self.assertEqual(api.sends[0]["params"]["parse_mode"], "HTML")
        self.assertEqual(api.sends[0]["params"]["text"], "<b>bold</b>")


# ===========================================================================================
# The question picker's own bodies.
# ===========================================================================================

class AskPathTests(unittest.TestCase):
    """`telegram_ask.py` renders the assistant's prose too — the question and every option's description —
    so it was arriving with literal `**` in it for the same reason."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.options = [{"label": "Convert", "description": "**Recommended** — see `telegram_send.py`."},
                        {"label": "Leave it", "description": "Keep reading literal asterisks."}]

    def test_a_question_body_is_converted(self):
        api = FakeAPI()
        ta.ask(cfg(), self.dir, "Which **one**?", self.options, api=api)
        params = api.sends[0]["params"]
        self.assertEqual(params["parse_mode"], "HTML")
        self.assertIn("<b>one</b>", params["text"])
        self.assertIn("<code>telegram_send.py</code>", params["text"])
        self.assertIn("reply_markup", params, "the keyboard still rides along")

    def test_a_rejected_question_body_is_resent_plain(self):
        api = FakeAPI(fail_on=[0])
        ta.ask(cfg(), self.dir, "Which **one**?", self.options, api=api)
        self.assertEqual(len(api.sends), 2)
        self.assertIn("**one**", api.sends[1]["params"]["text"], "the ORIGINAL body, unconverted")
        self.assertIsNone(api.sends[1]["params"]["parse_mode"])
        self.assertIn("reply_markup", api.sends[1]["params"], "the picker is still a picker")

    def test_plain_mode_leaves_the_ask_path_alone(self):
        api = FakeAPI()
        ta.ask(cfg(fmt=tf.FORMAT_PLAIN), self.dir, "Which **one**?", self.options, api=api)
        self.assertEqual(len(api.sends), 1)
        self.assertIn("**one**", api.sends[0]["params"]["text"])
        self.assertIsNone(api.sends[0]["params"]["parse_mode"])


# ===========================================================================================
# The golden test — one real message, whole.
# ===========================================================================================

GOLDEN_SOURCE = """**Evening check — Tuesday**

Two things need you, then I'll leave you to it.

| Row | Due | Status |
|---|---|---|
| Quarterly tax estimate payment | 12:30 | **overdue** |
| Eat lunch | 15:30 | done |

The `reminders_seed.py` ack landed — I ran `ack.py "lunch"` right after, so ⏰ won't
re-nudge. Details in [change #387](https://github.com/example/repo/pull/387).

> You said you'd rather be told twice than not at all.

## What I need
- Your call on the LW/SW encoding (the build is blocked on it)
- Nothing else — _genuinely_ nothing else
"""

GOLDEN_HTML = (
    "<b>Evening check — Tuesday</b>\n"
    "\n"
    "Two things need you, then I'll leave you to it.\n"
    "\n"
    "<pre>Row                            | Due   | Status\n"
    "-------------------------------+-------+--------\n"
    "Quarterly tax estimate payment | 12:30 | overdue\n"
    "Eat lunch                      | 15:30 | done</pre>\n"
    "\n"
    "The <code>reminders_seed.py</code> ack landed — I ran <code>ack.py \"lunch\"</code> right after, "
    "so ⏰ won't\n"
    "re-nudge. Details in "
    '<a href="https://github.com/example/repo/pull/387">change #387</a>.\n'
    "\n"
    "<blockquote>You said you'd rather be told twice than not at all.</blockquote>\n"
    "\n"
    "<b>What I need</b>\n"
    "- Your call on the LW/SW encoding (the build is blocked on it)\n"
    "- Nothing else — <i>genuinely</i> nothing else\n"
)


class GoldenMessageTests(unittest.TestCase):
    """One message shaped like a real evening check-in — bold, a table, code spans, a link, a
    quote, a heading, a list, and `reminders_seed.py` sitting in the middle of it — rendered whole.

    A golden test earns its maintenance cost by catching the interactions the per-construct tests
    cannot: a table immediately after a blank line, a link whose label contains a `#`, an em dash
    beside an entity. If this goes red, read the diff before re-baselining it."""

    def test_the_whole_rendering(self):
        self.assertEqual(html(GOLDEN_SOURCE), GOLDEN_HTML)

    def test_it_is_one_chunk_and_valid(self):
        plan = tf.plan_send(GOLDEN_SOURCE, MD)
        self.assertEqual(len(plan), 1)
        self.assertTrue(tags_are_balanced(plan[0]["html"]))
        self.assertLessEqual(len(plan[0]["html"]), tf.TELEGRAM_MESSAGE_LIMIT)

    def test_no_literal_emphasis_markers_survive(self):
        """The whole reason this feature exists."""
        rendered = html(GOLDEN_SOURCE)
        self.assertNotIn("**", rendered)
        self.assertNotIn("##", rendered)
        self.assertIn("reminders_seed.py", rendered, "…while the filename keeps its underscore")


if __name__ == "__main__":
    unittest.main()
