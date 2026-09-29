#!/usr/bin/env python3
"""The bot token may not reach a message, and may not stay in a log that already has it.

**What went wrong.** `telegram_http` built every error message from ``url.split('?')[0]``, which
strips the *query* — and the Bot API keeps its credential in the *path*
(``/bot<TOKEN>/sendMessage``). So every `TelegramHTTPError` embedded the live token, and two of its
callers persist those strings: `telegram_send.log_format_fallback` into
`state/telegram-format-fallback.jsonl`, and the daemon's logger into `state/presence.log`. Both are
gitignored, so nothing reached git; both are **read by agents**, which is a durable
credential-exposure surface rather than a line that scrolls past.

**Why the assertions are shaped the way they are.** *"The token is gone"* is only half a test — a
redactor that returns the empty string passes it. These pin **both edges**, everywhere:

* the token is gone, **and**
* the **method name survives** — ``sendMessage`` failing is a different incident from
  ``editMessageText`` failing, and an error nobody can diagnose is what makes a future edit widen
  the redaction back off again.

Every one of the **seven** formatting sites in `telegram_http.request` / `get_json` / `download` is
driven to its raise, because the defect was that one class of message leaked while six others did
too and nobody enumerated them. The constructor case is the belt-and-braces half: it asserts the
property holds for a message this module does not build, so a **future** site added by a route
nobody predicted is covered without anyone remembering to route it.

**Nothing here reaches the wire.** Everything drives `telegram_http._perform`, the module's own test
seam, with a scripted fake — the same double style as `test_telegram_http.py`.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import scrub_telegram_token as st  # noqa: E402
import telegram_http as th  # noqa: E402

#: Shaped like the real thing — ``<bot id>:<secret>`` — because the point is that a token-shaped
#: string in a token-shaped position never survives. The value is invented; nothing sends anything.
TOKEN = "8858706356:AAF_ThisIsNotTheRealTokenJustAShapeLikeIt"
SEND_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
EDIT_URL = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
POLL_URL = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset=42&timeout=25"
FILE_URL = f"https://api.telegram.org/file/bot{TOKEN}/photos/file_5.jpg"


class Clock:
    """Time that only moves when something sleeps, so a ceiling is exact rather than timed."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def steps(*outcomes):
    """A `_perform` double: an exception is raised, a tuple returned; the last outcome repeats."""
    queue = list(outcomes)

    def perform(method, url, data, headers, timeout, reader, policy):
        step = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(step, BaseException):
            raise step
        return step

    return perform


def amb(exc):
    return th._PhaseError(th.PHASE_AMBIGUOUS, exc)


def pre(exc):
    return th._PhaseError(th.PHASE_PRE_DELIVERY, exc)


class RedactionMixin:
    def assertScrubbed(self, text, *, method):
        """The two edges of the rule, always asserted together."""
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("bot" + TOKEN, text)
        self.assertIn("bot<redacted>", text)
        self.assertIn(method, text)


# --------------------------------------------------------------------------- the helper itself

class TheHelperRedactsAndKeepsTheMethod(RedactionMixin, unittest.TestCase):
    def test_a_send_url_keeps_its_method_and_loses_its_token(self):
        self.assertEqual(th.redact_url(SEND_URL),
                         "https://api.telegram.org/bot<redacted>/sendMessage")

    def test_the_method_name_is_what_makes_the_error_diagnosable(self):
        """`sendMessage` failing and `editMessageText` failing are different incidents. If a
        redaction ever costs that distinction, someone widens it back off."""
        for url, method in ((SEND_URL, "sendMessage"), (EDIT_URL, "editMessageText")):
            self.assertScrubbed(th.redact_url(url), method=method)

    def test_a_download_url_keeps_its_file_path(self):
        """The file endpoint carries the token too — ``/file/bot<TOKEN>/<path>`` — and there the
        useful survivor is the remote path, not a method name."""
        out = th.redact_url(FILE_URL)
        self.assertEqual(out, "https://api.telegram.org/file/bot<redacted>/photos/file_5.jpg")

    def test_the_query_string_is_still_stripped(self):
        self.assertEqual(th.redact_url(POLL_URL),
                         "https://api.telegram.org/bot<redacted>/getUpdates")

    def test_a_url_with_no_token_is_unchanged(self):
        """The one direction over-redaction would show up in: a URL that never had a credential
        must come back byte-identical, or every unrelated error message starts lying."""
        for url in ("https://api.telegram.org/health",
                    "https://example.com/a/b/c",
                    "https://127.0.0.1:8760/api/health",
                    "https://api.telegram.org/"):
            self.assertEqual(th.redact_url(url), url)

    def test_text_that_is_not_a_url_at_all_is_unchanged(self):
        for text in ("", "the read operation timed out", "HTTP 429", "bot8858706356:nope"):
            self.assertEqual(th.redact(text), text)

    def test_the_bare_token_without_a_leading_slash_is_not_matched(self):
        """Stated so the limit is deliberate rather than discovered: the pattern is anchored to the
        `/bot` PATH POSITION. A token pasted into prose is a different problem — the writers here
        format URLs, and widening this to bare digit-colon-string would redact chat text."""
        self.assertEqual(th.redact(f"token={TOKEN}"), f"token={TOKEN}")

    def test_redaction_is_idempotent(self):
        once = th.redact_url(SEND_URL)
        self.assertEqual(th.redact(once), once)


# ------------------------------------------------------------------- every raise in the module

class NoRaisedMessageCarriesTheToken(RedactionMixin, unittest.TestCase):
    """All seven formatting sites, driven to their raise.

    The defect was never *one* message: it was that seven were written the same wrong way and
    nobody enumerated them. So the enumeration is the test."""

    def _request(self, perform, *, url=SEND_URL, policy=th.IDEMPOTENT, **kw):
        clock = Clock()
        with mock.patch.object(th, "_perform", perform):
            with self.assertRaises(th.TelegramHTTPError) as cm:
                th.request(url, policy=policy, sleep=clock.sleep, monotonic=clock.monotonic,
                           jitter=lambda: 0.0, **kw)
        return str(cm.exception)

    def test_a_terminal_failure(self):
        """Site 1 — an unrecognised exception, raised on the first attempt."""
        msg = self._request(steps(amb(ValueError("nope"))))
        self.assertScrubbed(msg, method="sendMessage")

    def test_the_ambiguous_send_refusal(self):
        """Site 2 — the one refusal this module exists for. Its wording must survive intact."""
        msg = self._request(steps(amb(TimeoutError("The read operation timed out"))),
                            policy=th.UNSAFE)
        self.assertScrubbed(msg, method="sendMessage")
        self.assertIn("NOT retried", msg)
        self.assertIn("after the request was sent", msg)

    def test_a_429_whose_retry_after_exceeds_the_ceiling(self):
        """Site 3."""
        body = json.dumps({"ok": False, "parameters": {"retry_after": 900}}).encode("utf-8")
        msg = self._request(steps((429, {}, body)))
        self.assertScrubbed(msg, method="sendMessage")
        self.assertIn("429", msg)

    def test_the_attempt_cap(self):
        """Site 4 — gave up after N attempts."""
        msg = self._request(steps(amb(ConnectionResetError(10054, "forcibly closed"))))
        self.assertScrubbed(msg, method="sendMessage")
        self.assertIn("gave up after", msg)

    def test_the_wall_clock_ceiling(self):
        """Site 5 — the next wait would pass the total-seconds bound."""
        policy = th.IDEMPOTENT._replace(attempts=99, total_seconds=1.0)
        msg = self._request(steps(pre(ConnectionResetError("x"))), policy=policy)
        self.assertScrubbed(msg, method="sendMessage")
        self.assertIn("ceiling", msg)

    def test_get_json_on_an_unreadable_body(self):
        """Site 6 — `get_json`'s own raise, which the request loop never reaches."""
        with mock.patch.object(th, "_perform", steps((200, {}, b"<html>edge page</html>"))):
            with self.assertRaises(th.TelegramHTTPError) as cm:
                th.get_json(POLL_URL)
        self.assertScrubbed(str(cm.exception), method="getUpdates")

    def test_download_on_a_bad_status(self):
        """Site 7 — the file endpoint, where the token also rides in the path."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "f.jpg")
            with mock.patch.object(th, "_perform", steps((404, {}, b"not found"))):
                with self.assertRaises(th.TelegramHTTPError) as cm:
                    th.download(FILE_URL, dest)
        msg = str(cm.exception)
        self.assertNotIn(TOKEN, msg)
        self.assertIn("bot<redacted>", msg)
        self.assertIn("photos/file_5.jpg", msg)

    def test_the_constructor_scrubs_a_message_this_module_did_not_build(self):
        """**The backstop, and the reason this is not just seven call-site tests.** A future raise
        written by someone who has not read `redact_url` is still covered, because the property
        belongs to the type rather than to the seven places that happen to exist today."""
        exc = th.TelegramHTTPError(f"something new said {SEND_URL} and then failed")
        self.assertScrubbed(str(exc), method="sendMessage")


# ------------------------------------------------------------------------------- the scrubber

FALLBACK_ROW = (
    '{"at": "2026-08-24T20:00:31.934542Z", "chunk": 0, "chunks": 1, "error": '
    '"https://api.telegram.org/bot%s/sendMessage: The read operation timed out — failed after '
    'the request was sent, so Telegram may already have acted on it; NOT retried (a blind retry '
    'here duplicates the message)", "chars": 72, "text": "⏰ Reminder: water the plants — '
    'tray 1 of 3. tick Ack in Notion or tell me."}' % TOKEN
)
CLEAN_ROW = '{"at": "2026-08-17T00:00:00Z", "chunk": 0, "chunks": 1, "error": "HTTP 400", "chars": 3}'
PRESENCE_LINE = ("[2026-08-22T00:17:23-05:00] ! telegram poll: https://api.telegram.org/bot%s"
                 "/getUpdates: The read operation timed out (gave up after 1 attempts)" % TOKEN)


class TheScrubberCleansWhatIsAlreadyOnDisk(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "telegram-format-fallback.jsonl")

    def _write(self, *lines, ending="\n"):
        raw = ending.join(lines).encode("utf-8") + ending.encode("utf-8")
        with open(self.path, "wb") as fh:
            fh.write(raw)
        return raw

    def _read(self):
        with open(self.path, "rb") as fh:
            return fh.read()

    def test_it_reports_the_rows_it_changed_and_only_those(self):
        self._write(CLEAN_ROW, FALLBACK_ROW, CLEAN_ROW, FALLBACK_ROW)
        report = st.scrub_file(self.path)
        self.assertEqual(report["status"], "scrubbed")
        self.assertEqual(report["changed"], 2)

    def test_the_token_is_gone_and_the_method_survives(self):
        self._write(FALLBACK_ROW)
        st.scrub_file(self.path)
        out = self._read().decode("utf-8")
        self.assertNotIn(TOKEN, out)
        self.assertIn("bot<redacted>/sendMessage", out)

    def test_every_other_byte_of_the_row_is_preserved(self):
        """The row is not re-serialised: it is a substring replacement inside the original bytes,
        so key order, spacing, escaping and the em dashes all come back exactly."""
        self._write(FALLBACK_ROW)
        st.scrub_file(self.path)
        row = json.loads(self._read().decode("utf-8").splitlines()[0])
        self.assertEqual(row["at"], "2026-08-24T20:00:31.934542Z")
        self.assertEqual(row["chars"], 72)
        self.assertEqual(row["text"], "⏰ Reminder: water the plants — tray 1 of 3. tick Ack in "
                                      "Notion or tell me.")
        self.assertIn("NOT retried (a blind retry here duplicates the message)", row["error"])
        self.assertEqual(list(row), ["at", "chunk", "chunks", "error", "chars", "text"])

    def test_a_clean_line_is_returned_as_its_original_bytes(self):
        raw = self._write(CLEAN_ROW, FALLBACK_ROW)
        st.scrub_file(self.path)
        after = self._read()
        self.assertEqual(after.split(b"\n")[0], raw.split(b"\n")[0])

    def test_rows_are_neither_reordered_nor_dropped(self):
        self._write(CLEAN_ROW, FALLBACK_ROW, PRESENCE_LINE)
        before = self._read().count(b"\n")
        st.scrub_file(self.path)
        after = self._read()
        self.assertEqual(after.count(b"\n"), before)
        self.assertTrue(after.decode("utf-8").splitlines()[0].startswith('{"at": "2026-08-17'))
        self.assertTrue(after.decode("utf-8").splitlines()[2].startswith("[2026-08-22T00:17:23"))

    def test_running_it_twice_changes_nothing_the_second_time(self):
        """Idempotence, asserted on the BYTES as well as on the count — a second run that reports
        zero while silently rewriting the file would pass the weaker version of this."""
        self._write(CLEAN_ROW, FALLBACK_ROW, FALLBACK_ROW)
        first = st.scrub_file(self.path)
        self.assertEqual(first["changed"], 2)
        after_first = self._read()
        second = st.scrub_file(self.path)
        self.assertEqual(second, {"path": self.path, "status": "clean", "mode": "atomic",
                                  "changed": 0})
        self.assertEqual(self._read(), after_first)

    def test_a_file_with_no_token_is_left_completely_alone(self):
        raw = self._write(CLEAN_ROW, CLEAN_ROW)
        report = st.scrub_file(self.path)
        self.assertEqual(report["status"], "clean")
        self.assertEqual(self._read(), raw)

    def test_crlf_and_a_missing_final_newline_survive(self):
        """`presence.log` is written on Windows. A scrubber that normalises line endings rewrites
        every byte of a 900 kB file to fix four lines."""
        raw = (CLEAN_ROW + "\r\n" + FALLBACK_ROW).encode("utf-8")
        with open(self.path, "wb") as fh:
            fh.write(raw)
        st.scrub_file(self.path)
        after = self._read()
        self.assertTrue(after.startswith(CLEAN_ROW.encode("utf-8") + b"\r\n"))
        self.assertFalse(after.endswith(b"\n"))
        self.assertNotIn(TOKEN.encode("utf-8"), after)

    def test_bytes_that_are_not_valid_utf8_round_trip(self):
        """The surrogateescape round trip, pinned: a log with one mojibake byte in it is still a
        log this has to be safe to run over."""
        raw = b"\xff\xfe not utf-8 at all\n" + FALLBACK_ROW.encode("utf-8") + b"\n"
        with open(self.path, "wb") as fh:
            fh.write(raw)
        report = st.scrub_file(self.path)
        self.assertEqual(report["changed"], 1)
        self.assertTrue(self._read().startswith(b"\xff\xfe not utf-8 at all\n"))

    def test_a_missing_file_is_reported_and_never_created(self):
        gone = os.path.join(self.dir, "nope.jsonl")
        self.assertEqual(st.scrub_file(gone)["status"], "missing")
        self.assertFalse(os.path.exists(gone))

    def test_dry_run_writes_nothing(self):
        raw = self._write(FALLBACK_ROW)
        report = st.scrub_file(self.path, dry_run=True)
        self.assertEqual(report, {"path": self.path, "status": "would-scrub", "mode": "atomic",
                                  "changed": 1})
        self.assertEqual(self._read(), raw)

    def test_no_temp_file_is_left_behind(self):
        self._write(FALLBACK_ROW)
        st.scrub_file(self.path)
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".scrub-")], [])

    def test_it_shares_one_definition_with_the_redactor(self):
        """The property that keeps the writer and the cleaner from drifting apart: there is no
        second pattern here, only `telegram_http.redact`."""
        self.assertEqual(st.scrub_text(SEND_URL), th.redact(SEND_URL))
        self.assertEqual(st._BOT_SEGMENT_BYTES.pattern.decode("utf-8"), th._BOT_SEGMENT.pattern)


class TheScanAnswersWhichFilesCarryIt(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write(self, name, raw):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(raw)
        return path

    def test_it_names_the_files_that_carry_it_and_counts_the_hits(self):
        self._write("presence.log", (PRESENCE_LINE + "\n").encode("utf-8") * 3)
        self._write("clean.jsonl", (CLEAN_ROW + "\n").encode("utf-8"))
        found = st.scan_dir(self.dir)
        self.assertEqual(found, [{"path": "presence.log", "hits": 3}])

    def test_an_empty_result_is_a_real_answer(self):
        self._write("clean.jsonl", (CLEAN_ROW + "\n").encode("utf-8"))
        self.assertEqual(st.scan_dir(self.dir), [])

    def test_a_token_straddling_a_chunk_boundary_is_still_counted_exactly_once(self):
        """The overlap, which is the only interesting thing about a chunked scanner: without it a
        split token is missed by both halves, and a scanner that under-reports converts *"I did
        not look properly"* into *"there is nothing there."*"""
        line = (PRESENCE_LINE + "\n").encode("utf-8")
        for pad in range(len(line) + 2):
            path = self._write("straddle.log", b"x" * pad + line)
            with mock.patch.object(st, "SCAN_CHUNK", 64):
                self.assertEqual(st.count_hits(path), 1, f"pad={pad}")

    def test_a_file_it_cannot_open_is_skipped_not_raised_on(self):
        path = self._write("locked.log", (PRESENCE_LINE + "\n").encode("utf-8"))
        with mock.patch("builtins.open", side_effect=OSError("in use")):
            self.assertEqual(st.count_hits(path), 0)

    def test_skipped_directories_are_not_walked(self):
        self._write(os.path.join("archives", "old.log"), (PRESENCE_LINE + "\n").encode("utf-8"))
        self.assertEqual(st.scan_dir(self.dir), [])


class TheInPlaceModeDoesNotStrandTheDaemonsHandle(unittest.TestCase):
    """`presence.log` is opened once at daemon startup and held for the process's whole life.

    An `os.replace` onto that path either fails outright on Windows or — worse — succeeds and
    orphans the handle, after which **every later log line goes to a file nobody can see**. That is
    the observability-blind failure this repo has already paid 39.2 h for once, and it would be
    bought back by a scrub. So the redaction is written OVER the token, same offsets, same width."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "presence.log")

    def _write(self, raw):
        with open(self.path, "wb") as fh:
            fh.write(raw)
        return raw

    def _read(self):
        with open(self.path, "rb") as fh:
            return fh.read()

    def test_the_file_size_never_changes(self):
        """The load-bearing property. If one byte moves, every offset after it moves, and the
        thing appending to this file is doing so at an offset of its own."""
        raw = self._write((PRESENCE_LINE + "\n").encode("utf-8") * 3)
        report = st.scrub_in_place(self.path)
        self.assertEqual(report["status"], "scrubbed")
        self.assertEqual(len(self._read()), len(raw))

    def test_the_token_is_gone_and_the_method_survives(self):
        self._write((PRESENCE_LINE + "\n").encode("utf-8"))
        st.scrub_in_place(self.path)
        out = self._read().decode("utf-8")
        self.assertNotIn(TOKEN, out)
        self.assertIn("bot<redacted", out)
        self.assertIn("/getUpdates", out)
        self.assertIn("gave up after 1 attempts", out)

    def test_everything_outside_the_token_is_byte_identical(self):
        raw = self._write((PRESENCE_LINE + "\n").encode("utf-8"))
        start = raw.index(b"/bot") + 1
        end = raw.index(b"/getUpdates")
        st.scrub_in_place(self.path)
        after = self._read()
        self.assertEqual(after[:start], raw[:start])
        self.assertEqual(after[end:], raw[end:])

    def test_a_line_the_daemon_appends_mid_scrub_survives(self):
        """The concurrency case, played out in the order it really happens: the scrubber reads,
        the daemon appends, the scrubber writes. Every offset it touches is below the EOF it read,
        so the appended line is untouched — and still there."""
        self._write((PRESENCE_LINE + "\n").encode("utf-8"))
        appended = b"[2026-08-26T01:20:00-05:00] tick: nothing due\n"

        real_open = open

        def open_and_append(path, mode="r", *a, **kw):
            fh = real_open(path, mode, *a, **kw)
            if mode == "r+b":            # the write handle: the daemon gets its line in first
                with real_open(self.path, "ab") as other:
                    other.write(appended)
            return fh

        with mock.patch("builtins.open", side_effect=open_and_append):
            st.scrub_in_place(self.path)

        after = self._read()
        self.assertTrue(after.endswith(appended))
        self.assertNotIn(TOKEN.encode("utf-8"), after)
        self.assertIn(b"bot<redacted", after)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self._write((PRESENCE_LINE + "\n").encode("utf-8") * 2)
        first = st.scrub_in_place(self.path)
        self.assertEqual(first["changed"], 2)
        after_first = self._read()
        second = st.scrub_in_place(self.path)
        self.assertEqual(second["changed"], 0)
        self.assertEqual(second["status"], "clean")
        self.assertEqual(self._read(), after_first)

    def test_two_tokens_on_one_line_count_as_one_row_and_both_go(self):
        """`telegram_get_file` logs a getFile URL and its download URL; a row count that said 2
        would over-report, and a scrub that took only the first would under-deliver."""
        line = ("[t] getFile https://api.telegram.org/bot%s/getFile then "
                "https://api.telegram.org/file/bot%s/photos/f.jpg\n" % (TOKEN, TOKEN))
        self._write(line.encode("utf-8"))
        report = st.scrub_in_place(self.path)
        self.assertEqual(report["changed"], 1)
        after = self._read().decode("utf-8")
        self.assertNotIn(TOKEN, after)
        self.assertEqual(after.count("bot<redacted"), 2)
        self.assertIn("/getFile", after)
        self.assertIn("photos/f.jpg", after)

    def test_a_clean_file_is_not_opened_for_writing_at_all(self):
        raw = self._write(b"[t] nothing interesting here\n")
        mtime = os.stat(self.path).st_mtime_ns
        report = st.scrub_in_place(self.path)
        self.assertEqual(report["status"], "clean")
        self.assertEqual(self._read(), raw)
        self.assertEqual(os.stat(self.path).st_mtime_ns, mtime)

    def test_dry_run_writes_nothing(self):
        raw = self._write((PRESENCE_LINE + "\n").encode("utf-8"))
        report = st.scrub_in_place(self.path, dry_run=True)
        self.assertEqual(report["status"], "would-scrub")
        self.assertEqual(report["changed"], 1)
        self.assertEqual(self._read(), raw)

    def test_a_missing_file_is_reported_and_never_created(self):
        gone = os.path.join(self.dir, "absent.log")
        self.assertEqual(st.scrub_in_place(gone)["status"], "missing")
        self.assertFalse(os.path.exists(gone))

    def test_a_file_over_the_cap_is_refused_rather_than_half_scrubbed(self):
        self._write((PRESENCE_LINE + "\n").encode("utf-8"))
        with mock.patch.object(st, "MAX_IN_PLACE_BYTES", 8):
            report = st.scrub_in_place(self.path)
        self.assertEqual(report["status"], "too-large")
        self.assertEqual(report["changed"], 0)
        self.assertIn(TOKEN, self._read().decode("utf-8"))  # untouched, not partly done

    def test_the_padding_is_exactly_as_wide_as_what_it_replaces(self):
        for secret in ("a", "ab", "1234567890", TOKEN, TOKEN * 2):
            raw = ("x /bot%s/sendMessage\n" % secret).encode("utf-8")
            self._write(raw)
            st.scrub_in_place(self.path)
            self.assertEqual(len(self._read()), len(raw), secret[:12])

    def test_the_replacement_never_matches_the_pattern_again(self):
        """Why the second run is a no-op rather than a slow corruption of the same line."""
        for width in range(4, 60):
            raw = ("/bot" + "z" * width + "/sendMessage").encode("utf-8")
            self.assertEqual(len(st._BOT_SEGMENT_BYTES.findall(raw)), 1)
            match = st._BOT_SEGMENT_BYTES.search(raw)
            padded = st._padded(match)
            self.assertEqual(len(padded), match.end() - match.start())
            self.assertEqual(st._BOT_SEGMENT_BYTES.findall(b"/" + padded), [])


class TheModeDispatchIsExplicit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write(self, name, raw):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(raw)
        return path

    def test_each_default_target_carries_the_mode_that_file_needs(self):
        """Not a preference: `presence.log` is held open by the running daemon, so the common
        invocation must not be able to pick `atomic` for it by omission."""
        self.assertEqual(dict(st.DEFAULT_TARGETS)["presence.log"], "in-place")
        self.assertEqual(dict(st.DEFAULT_TARGETS)["telegram-format-fallback.jsonl"], "atomic")

    def test_scrub_dispatches_on_the_mode_it_is_given(self):
        raw = (PRESENCE_LINE + "\n").encode("utf-8")
        atomic = self._write("a.log", raw)
        inplace = self._write("b.log", raw)
        self.assertEqual(st.scrub(atomic, "atomic")["mode"], "atomic")
        self.assertEqual(st.scrub(inplace, "in-place")["mode"], "in-place")
        with open(atomic, "rb") as fh:
            shrunk = fh.read()
        with open(inplace, "rb") as fh:
            same_width = fh.read()
        self.assertLess(len(shrunk), len(raw))        # atomic may shorten the line
        self.assertEqual(len(same_width), len(raw))   # in-place may NOT

    def test_there_is_no_auto_detection(self):
        """*"Is something holding this open?"* is not a question a process can answer portably, so
        the mode is stated rather than guessed — guessing it wrong is the failure it prevents."""
        path = self._write("c.log", (PRESENCE_LINE + "\n").encode("utf-8"))
        self.assertEqual(st.scrub(path, "anything-else")["mode"], "atomic")


class TheCliReportsWhatItDid(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _run(self, argv):
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            code = st.main(argv)
        return code, json.loads(buf.getvalue())

    def test_it_defaults_to_the_two_known_targets_and_creates_neither(self):
        code, out = self._run(["--state-dir", self.dir])
        self.assertEqual(code, 0)
        self.assertEqual([r["status"] for r in out["files"]], ["missing", "missing"])
        self.assertEqual(out["changed"], 0)
        self.assertEqual(os.listdir(self.dir), [])

    def test_it_totals_the_rows_it_changed(self):
        for name, _mode in st.DEFAULT_TARGETS:
            with open(os.path.join(self.dir, name), "wb") as fh:
                fh.write((FALLBACK_ROW + "\n").encode("utf-8"))
        code, out = self._run(["--state-dir", self.dir])
        self.assertEqual(code, 0)
        self.assertEqual(out["changed"], 2)

    def test_scan_mode_writes_nothing(self):
        path = os.path.join(self.dir, "presence.log")
        with open(path, "wb") as fh:
            fh.write((PRESENCE_LINE + "\n").encode("utf-8"))
        with open(path, "rb") as fh:
            before = fh.read()
        code, out = self._run(["--scan", "--state-dir", self.dir])
        self.assertEqual(code, 0)
        self.assertEqual(out["found"], [{"path": "presence.log", "hits": 1}])
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)


if __name__ == "__main__":
    unittest.main()
