#!/usr/bin/env python3
"""Tests for the shared Telegram HTTP transport (`telegram_http.py`) and its two callers.

The defect: an inbound photo's download dies on one `[WinError 10054]` reset and the message
reaches the warm session with a "download failed" placeholder. With every HTTP call in the Telegram
path a bare `urlopen`, nothing retries and nothing records that the file is still fetchable.

**What is actually being defended here is the ASYMMETRY, not the retry.** A read may be re-run
freely; a send may not, because `sendMessage` has no idempotency key and a reset can land after
Telegram accepted the message. So the load-bearing test in this file is
`SendsAreNotFreelyRetryable.test_ambiguous_send_failure_is_not_retried` — if a later edit
"helpfully" makes sends retry everything, that is the test that has to be deleted to do it.

Covered:
  * a transient failure retried and then succeeding, and the backoff/jitter/ceiling arithmetic;
  * a NON-transient failure raised on the first attempt (jobs.py's "unrecognised output is terminal");
  * both ceilings — the attempt cap and the total wall clock — actually stopping the loop;
  * HTTP 429 honouring Telegram's own `parameters.retry_after` instead of our schedule, on BOTH
    policies, and refusing an absurd one;
  * every way `request` gives up on a 429 (ceiling exceeded, attempts exhausted) tagging the raised
    error `PHASE_REFUSED` rather than leaving `phase=None` — a 429 means refused, not unknown — while
    a 5xx it gives up on stays untagged, since that ambiguity is deliberate;
  * an AMBIGUOUS send failure not retried, while a PRE-DELIVERY one is;
  * `_perform_direct`'s phase tagging against real localhost sockets — the only part that cannot be
    tested by assertion alone, since the whole claim is about where in the exchange we are;
  * a download retried mid-stream, leaving no partial file under the real name;
  * a failed download keeping its `file_id` so it stays re-fetchable, and the placeholder saying so;
  * `api_call` picking the policy by METHOD, defaulting to the conservative one;
  * the getUpdates offset never advancing on a read that failed;
  * the Markdown→HTML formatting fallback still being a separate mechanism, untouched.

No network: `telegram_http._perform` is the seam, except for the two socket tests which bind
127.0.0.1 on an ephemeral port. Run:
  python -m unittest test_telegram_http   (or)   python test_telegram_http.py
"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import presence as pr  # noqa: E402
import telegram_http as th  # noqa: E402
import telegram_poll as tp  # noqa: E402
import telegram_send as ts  # noqa: E402

URL = "https://api.telegram.org/bottok/sendMessage"


# --------------------------------------------------------------------------- doubles

def pre(exc):
    """A step that fails in the connect phase — provably before delivery."""
    return th._PhaseError(th.PHASE_PRE_DELIVERY, exc)


def amb(exc):
    """A step that fails once the request is on the wire — Telegram may already have acted."""
    return th._PhaseError(th.PHASE_AMBIGUOUS, exc)


def ok(payload=None, status=200, headers=None):
    return (status, headers or {}, json.dumps(payload if payload is not None
                                              else {"ok": True, "result": {}}).encode("utf-8"))


class Clock:
    """A fake wall clock that only moves when something sleeps (or a step says it did), so the
    ceiling arithmetic is exact rather than timing-dependent."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Scripted:
    """A queue of outcomes for `th._perform`. An exception instance is raised, a tuple is returned,
    a callable is handed the `reader` so a body-read failure can be simulated faithfully. The last
    step repeats, so a test only has to script as far as the behaviour it is asserting."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, method, url, data, headers, timeout, reader, policy):
        self.calls.append({"method": method, "url": url, "policy": policy, "timeout": timeout})
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step(reader)
        return step


def run(scripted, *, policy=th.IDEMPOTENT, clock=None, **kw):
    clock = clock or Clock()
    with mock.patch.object(th, "_perform", scripted):
        return th.request(URL, policy=policy, sleep=clock.sleep, monotonic=clock.monotonic,
                          jitter=lambda: 0.0, **kw), clock


# --------------------------------------------------------------------------- the retry loop

class TransientIsRetried(unittest.TestCase):
    def test_reset_then_success(self):
        """The motivating failure, on the read side: one reset, then the call works."""
        s = Scripted(amb(ConnectionResetError(10054, "forcibly closed")), ok({"ok": True}))
        res, clock = run(s)
        self.assertEqual(res.attempts, 2)
        self.assertEqual(json.loads(res.body)["ok"], True)
        self.assertEqual(len(s.calls), 2)

    def test_backoff_is_exponential_and_capped(self):
        s = Scripted(*([amb(ConnectionResetError("x"))] * 3 + [ok()]))
        res, clock = run(s)
        self.assertEqual(res.attempts, 4)
        self.assertEqual(clock.sleeps, [0.5, 1.0, 2.0])  # base 0.5, doubling, jitter pinned to 0

    def test_jitter_only_ever_adds(self):
        """Jitter exists so a fleet coming off one outage does not resynchronise — it must never
        shorten a wait below the schedule."""
        s = Scripted(amb(ConnectionResetError("x")), ok())
        clock = Clock()
        with mock.patch.object(th, "_perform", s):
            th.request(URL, sleep=clock.sleep, monotonic=clock.monotonic, jitter=lambda: 1.0)
        self.assertEqual(clock.sleeps, [0.625])  # 0.5 + 25%

    def test_dns_failure_is_transient(self):
        s = Scripted(pre(socket.gaierror("no such host")), ok())
        res, _ = run(s)
        self.assertEqual(res.attempts, 2)

    def test_timeout_is_transient(self):
        s = Scripted(amb(TimeoutError("timed out")), ok())
        res, _ = run(s)
        self.assertEqual(res.attempts, 2)

    def test_server_error_retries_on_a_read(self):
        s = Scripted((500, {}, b"nope"), ok())
        res, _ = run(s)
        self.assertEqual(res.attempts, 2)
        self.assertEqual(res.status, 200)


class NonTransientIsNotRetried(unittest.TestCase):
    """jobs.py's rule in exception form: unrecognised failure is TERMINAL, first time. A too-eager
    retry re-runs whatever the failure actually was."""

    def test_programming_error_raised_on_the_first_attempt(self):
        s = Scripted(pre(ValueError("that is not a url")))
        with self.assertRaises(th.TelegramHTTPError) as cm:
            run(s)
        self.assertEqual(len(s.calls), 1)
        self.assertEqual(cm.exception.attempts, 1)

    def test_certificate_verification_is_not_transient(self):
        """An `SSLError`, but a deterministic one — it fails identically four times in a row."""
        import ssl
        s = Scripted(pre(ssl.SSLCertVerificationError("bad chain")))
        with self.assertRaises(th.TelegramHTTPError):
            run(s)
        self.assertEqual(len(s.calls), 1)

    def test_a_4xx_is_a_result_not_a_retry(self):
        """Telegram answers its API errors with a JSON body on 4xx; the call site judges it."""
        s = Scripted((400, {}, b'{"ok":false,"description":"can\'t parse entities"}'))
        res, _ = run(s)
        self.assertEqual(res.status, 400)
        self.assertEqual(len(s.calls), 1)


class CeilingsStopTheLoop(unittest.TestCase):
    def test_attempt_cap(self):
        s = Scripted(amb(ConnectionResetError("x")))
        with self.assertRaises(th.TelegramHTTPError) as cm:
            run(s)
        self.assertEqual(len(s.calls), th.IDEMPOTENT.attempts)
        self.assertEqual(cm.exception.attempts, th.IDEMPOTENT.attempts)
        self.assertIn("gave up after 4 attempts", str(cm.exception))

    def test_total_wall_clock_ceiling(self):
        """A long-poll-shaped failure: each attempt itself burns time, so the attempt cap is never
        reached and the wall clock is what has to stop it."""
        clock = Clock()

        def slow_failure(method, url, data, headers, timeout, reader, policy):
            clock.now += 25.0  # a 25 s long poll that dies at the end
            raise amb(ConnectionResetError("x"))

        slow_failure.calls = []
        with mock.patch.object(th, "_perform", slow_failure):
            with self.assertRaises(th.TelegramHTTPError) as cm:
                th.request(URL, policy=th.IDEMPOTENT, sleep=clock.sleep,
                           monotonic=clock.monotonic, jitter=lambda: 0.0)
        self.assertLess(cm.exception.attempts, th.IDEMPOTENT.attempts)  # the clock won, not the cap
        self.assertIn("ceiling", str(cm.exception))
        self.assertLessEqual(clock.now, th.IDEMPOTENT.total_seconds + 25.0)

    def test_the_ceiling_is_checked_before_sleeping_not_after(self):
        """It is a promise about when `request` returns, so the last wait must not be taken and
        then regretted."""
        clock = Clock()

        def near_ceiling(method, url, data, headers, timeout, reader, policy):
            clock.now += 19.8
            raise amb(ConnectionResetError("x"))

        with mock.patch.object(th, "_perform", near_ceiling):
            with self.assertRaises(th.TelegramHTTPError) as cm:
                th.request(URL, policy=th.UNSAFE._replace(retry_ambiguous=True),
                           sleep=clock.sleep, monotonic=clock.monotonic, jitter=lambda: 0.0)
        # 19.8 + the 0.5 s first backoff would pass the 20 s ceiling, so it is never taken — the
        # loop gives up while it still has attempts left rather than overshooting and regretting it.
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(cm.exception.attempts, 1)
        self.assertIn("ceiling", str(cm.exception))


class RateLimitHonoursTelegram(unittest.TestCase):
    """A 429 means the request was REFUSED, so nothing was delivered and a retry cannot duplicate
    anything — it is the one status that retries on both policies."""

    def test_retry_after_from_the_json_body_beats_our_backoff(self):
        s = Scripted((429, {}, b'{"ok":false,"error_code":429,"parameters":{"retry_after":7}}'), ok())
        res, clock = run(s)
        self.assertEqual(clock.sleeps, [7.0])  # not 0.5
        self.assertEqual(res.attempts, 2)

    def test_retry_after_header_is_the_fallback(self):
        s = Scripted((429, {"Retry-After": "3"}, b"too many requests"), ok())
        res, clock = run(s)
        self.assertEqual(clock.sleeps, [3.0])

    def test_a_send_also_retries_a_429(self):
        s = Scripted((429, {}, b'{"parameters":{"retry_after":2}}'), ok())
        res, clock = run(s, policy=th.UNSAFE)
        self.assertEqual(clock.sleeps, [2.0])
        self.assertEqual(res.attempts, 2)

    def test_an_absurd_retry_after_is_refused_rather_than_waited_out(self):
        s = Scripted((429, {}, b'{"parameters":{"retry_after":3600}}'))
        with self.assertRaises(th.TelegramHTTPError) as cm:
            run(s)
        self.assertIn("ceiling", str(cm.exception))
        self.assertEqual(len(s.calls), 1)
        # A 429 is a REFUSAL, not an unknown outcome — `phase=None` here reads as PHASE_AMBIGUOUS at
        # the caller (telegram_send.classify_send_failure) and a flood-waited send is silently
        # dropped instead of being safely re-sendable.
        self.assertEqual(cm.exception.phase, th.PHASE_REFUSED)

    def test_a_429_with_no_number_falls_back_to_our_backoff(self):
        s = Scripted((429, {}, b"<html>rate limited</html>"), ok())
        res, clock = run(s)
        self.assertEqual(clock.sleeps, [0.5])

    def test_repeated_429s_exhausting_the_attempt_cap_are_also_tagged_refused(self):
        s = Scripted(*([(429, {}, b'{"parameters":{"retry_after":0.1}}')] * th.IDEMPOTENT.attempts))
        with self.assertRaises(th.TelegramHTTPError) as cm:
            run(s)
        self.assertEqual(len(s.calls), th.IDEMPOTENT.attempts)
        self.assertEqual(cm.exception.phase, th.PHASE_REFUSED)

    def test_a_5xx_gave_up_on_stays_untagged(self):
        """The deliberate remaining under-call: a 5xx means the server answered, but whether it
        PROCESSED the request before failing is exactly the ambiguity the send policy refuses to
        gamble on — this must NOT be tagged PHASE_REFUSED the way a 429 now is."""
        s = Scripted(*([(500, {}, b"nope")] * th.IDEMPOTENT.attempts))
        with self.assertRaises(th.TelegramHTTPError) as cm:
            run(s)
        self.assertIsNone(cm.exception.phase)


# --------------------------------------------------------------------------- THE ONE THAT MATTERS

class SendsAreNotFreelyRetryable(unittest.TestCase):
    """`sendMessage` has no idempotency key. A reset can land AFTER Telegram accepted the message
    and BEFORE the response came back, so a blind retry pushes the owner the same nudge twice — on
    the channel carrying their critical reminders. **Under-sending is recoverable; double-sending is
    not.**

    Deleting or loosening these is how the bug comes back."""

    def test_ambiguous_send_failure_is_not_retried(self):
        s = Scripted(amb(ConnectionResetError(10054, "forcibly closed")), ok())
        with self.assertRaises(th.TelegramHTTPError) as cm:
            run(s, policy=th.UNSAFE)
        self.assertEqual(len(s.calls), 1, "a send that MIGHT have landed was retried")
        self.assertEqual(cm.exception.phase, th.PHASE_AMBIGUOUS)

    def test_the_refusal_says_which_kind_of_failure_it_was(self):
        """A send that failed after the bytes went out must never READ like a send that simply did
        not happen — the whole point is that the caller cannot safely assume either."""
        s = Scripted(amb(ConnectionResetError("x")))
        with self.assertRaises(th.TelegramHTTPError) as cm:
            run(s, policy=th.UNSAFE)
        message = str(cm.exception)
        self.assertIn("after the request was sent", message)
        self.assertIn("NOT retried", message)

    def test_ambiguous_timeout_is_not_retried_either(self):
        """A read timeout is the same hazard wearing a different exception: the request went out."""
        s = Scripted(amb(TimeoutError("timed out")), ok())
        with self.assertRaises(th.TelegramHTTPError):
            run(s, policy=th.UNSAFE)
        self.assertEqual(len(s.calls), 1)

    def test_a_5xx_is_not_retried_on_a_send(self):
        """The server answered, so it RECEIVED the request; whether it processed the message before
        failing is exactly the ambiguity this policy refuses to gamble on."""
        s = Scripted((502, {}, b"bad gateway"), ok())
        res, _ = run(s, policy=th.UNSAFE)
        self.assertEqual(res.status, 502)
        self.assertEqual(len(s.calls), 1)

    def test_pre_delivery_send_failure_IS_retried(self):
        """The other half of the design: refusing everything would be a different bug. Nothing was
        written to the socket, so this cannot possibly have duplicated anything."""
        s = Scripted(pre(ConnectionRefusedError("nothing listening")), ok())
        res, _ = run(s, policy=th.UNSAFE)
        self.assertEqual(res.attempts, 2)

    def test_dns_and_tls_failures_are_pre_delivery_and_retry_on_a_send(self):
        import ssl
        for exc in (socket.gaierror("dns"), ssl.SSLError("handshake failure")):
            with self.subTest(exc=type(exc).__name__):
                s = Scripted(pre(exc), ok())
                res, _ = run(s, policy=th.UNSAFE)
                self.assertEqual(res.attempts, 2)

    def test_the_unsafe_policy_refuses_ambiguity_by_construction(self):
        self.assertFalse(th.UNSAFE.retry_ambiguous)
        self.assertFalse(th.UNSAFE.retry_server_error)
        self.assertTrue(th.IDEMPOTENT.retry_ambiguous)


class PhaseTaggingIsPositional(unittest.TestCase):
    """The phase split is the claim everything above rests on, and it is the one part that cannot be
    established by assertion alone — it is about WHERE in the exchange we are. So these two drive
    `_perform_direct` against real sockets on 127.0.0.1.

    `conn.connect()` completes DNS, TCP and TLS without writing a byte of the request; everything
    after it is on the wire. Collapsing the two try blocks in `_perform_direct` breaks both."""

    def _perform(self, url):
        return th._perform_direct("POST", url, b"a=1",
                                  {"Content-Type": "application/x-www-form-urlencoded"},
                                  2.0, lambda r: r.read())

    def test_connection_refused_is_pre_delivery(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing is listening on `port` now
        with self.assertRaises(th._PhaseError) as cm:
            self._perform(f"http://127.0.0.1:{port}/x")
        self.assertEqual(cm.exception.phase, th.PHASE_PRE_DELIVERY)
        self.assertTrue(th.is_transient(cm.exception.cause))

    def test_a_server_that_hangs_up_after_accepting_is_ambiguous(self):
        """Accept the connection, then close without answering. The handshake succeeded, so the
        request is on the wire and we must NOT claim it never arrived."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def serve():
            try:
                conn, _ = srv.accept()
                conn.close()
            except OSError:
                pass

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            with self.assertRaises(th._PhaseError) as cm:
                self._perform(f"http://127.0.0.1:{port}/x")
            self.assertEqual(cm.exception.phase, th.PHASE_AMBIGUOUS)
        finally:
            srv.close()
            t.join(timeout=2)

    def test_the_phased_transport_is_used_only_where_the_answer_changes_something(self):
        """A seam nobody consults is a second code path that can rot. Reads take `urlopen` (which
        also honours the proxy environment); only a policy that would REFUSE an ambiguous failure
        pays for the split."""
        seen = []

        def spy(name):
            def f(*a):
                seen.append(name)
                return ok()
            return f

        with mock.patch.object(th, "_perform_direct", spy("direct")), \
                mock.patch.object(th, "_perform_urlopen", spy("urlopen")), \
                mock.patch.object(th, "_proxied", lambda url: False):
            th.request(URL, policy=th.IDEMPOTENT)
            th.request(URL, policy=th.UNSAFE)
        self.assertEqual(seen, ["urlopen", "direct"])

    def test_a_proxied_send_falls_back_and_therefore_retries_nothing(self):
        """A direct `http.client` connection would bypass the proxy entirely, so the fallback is not
        optional — and it costs the phase seam, which means a proxied send behaves exactly as it did
        before this module existed. Losing the retry is the right way to lose."""
        seen = []
        with mock.patch.object(th, "_perform_direct", lambda *a: seen.append("direct") or ok()), \
                mock.patch.object(th, "_perform_urlopen",
                                  lambda *a: seen.append("urlopen") or ok()), \
                mock.patch.object(th, "_proxied", lambda url: True):
            th.request(URL, policy=th.UNSAFE)
        self.assertEqual(seen, ["urlopen"])

    def test_proxy_detection_fails_towards_the_fallback(self):
        """Anything we cannot determine is treated as proxied: a send out of the wrong socket is
        worse than a send that retries nothing."""
        with mock.patch.object(th.urllib.request, "getproxies", side_effect=OSError("registry")):
            self.assertTrue(th._proxied("https://api.telegram.org/x"))


# --------------------------------------------------------------------------- downloads

class DownloadsRetryAndLeaveNoPartial(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.dest = os.path.join(self.dir, "photo.jpg")

    def _body(self, blob, fail_after=None):
        """A step whose body read can die mid-stream, tagged AMBIGUOUS exactly as the real transport
        would tag it."""
        def step(reader):
            stream = io.BytesIO(blob) if fail_after is None else _Dying(blob, fail_after)
            try:
                return 200, {}, reader(stream)
            except Exception as e:
                raise th._PhaseError(th.PHASE_AMBIGUOUS, e) from e
        return step

    def test_a_reset_mid_stream_is_retried_and_the_file_lands_whole(self):
        """The motivating shape. A retry that only covered the handshake would have sailed straight
        past it."""
        s = Scripted(self._body(b"\xff\xd8" + b"x" * 500, fail_after=64),
                     self._body(b"\xff\xd8" + b"x" * 500))
        clock = Clock()
        with mock.patch.object(th, "_perform", s):
            th.download("https://api.telegram.org/file/bottok/photos/f.jpg", self.dest,
                        sleep=clock.sleep, monotonic=clock.monotonic, jitter=lambda: 0.0)
        with open(self.dest, "rb") as fh:
            self.assertEqual(fh.read(), b"\xff\xd8" + b"x" * 500)
        self.assertEqual(len(s.calls), 2)

    def test_a_partial_download_is_never_visible_under_the_real_name(self):
        s = Scripted(self._body(b"x" * 500, fail_after=64))
        clock = Clock()
        with mock.patch.object(th, "_perform", s):
            with self.assertRaises(th.TelegramHTTPError):
                th.download("https://api.telegram.org/file/bottok/photos/f.jpg", self.dest,
                            sleep=clock.sleep, monotonic=clock.monotonic, jitter=lambda: 0.0)
        self.assertFalse(os.path.exists(self.dest))
        self.assertFalse(os.path.exists(self.dest + ".part"), "the .part sidecar was left behind")

    def test_a_non_200_never_becomes_a_file(self):
        s = Scripted(self._body(b"<html>404</html>"))
        s.steps = [(404, {}, b"")]
        with mock.patch.object(th, "_perform", s):
            with self.assertRaises(th.TelegramHTTPError):
                th.download("https://api.telegram.org/file/bottok/photos/f.jpg", self.dest)
        self.assertFalse(os.path.exists(self.dest))


class _Dying(io.RawIOBase):
    """A stream that gives up `limit` bytes and then resets, like a real mid-body hangup."""

    def __init__(self, blob, limit):
        self._blob = blob
        self._limit = limit
        self._read = 0

    def readable(self):
        return True

    def readinto(self, buf):
        if self._read >= self._limit:
            raise ConnectionResetError(10054, "An existing connection was forcibly closed")
        chunk = self._blob[self._read:self._read + min(len(buf), self._limit - self._read)]
        buf[:len(chunk)] = chunk
        self._read += len(chunk)
        return len(chunk)


# --------------------------------------------------------------------------- the callers

class FailedDownloadStaysRefetchable(unittest.TestCase):
    """A `file_id` outlives the fetch that failed on it, so the id IS the recovery handle. Without
    it the id dies with the exception and the only recovery is the owner noticing that the reply
    ignored their photo.

    The daemon-side placeholder that names the `--refetch` recovery (`presence.telegram_inbound_text`
    saying "still on Telegram") is daemon wiring and is tested with it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _media(self):
        return {"kind": "photo", "file_id": "AgACAgEAAx", "file_unique_id": "u1",
                "file_name": None, "mime_type": "image/jpeg", "file_size": 120_000}

    def test_the_record_keeps_the_file_id(self):
        with mock.patch.object(tp, "telegram_get_file",
                               side_effect=RuntimeError("[WinError 10054] forcibly closed")):
            att = tp.fetch_attachment(self._media(), "tok", "https://api.telegram.org", self.dir)
        self.assertEqual(att["file_id"], "AgACAgEAAx")
        self.assertTrue(att["refetchable"])
        self.assertIn("10054", att["error"])
        self.assertNotIn("local_path", att)  # still fail-open: the message survives without the file

    def test_a_successful_download_carries_no_recovery_handle(self):
        with mock.patch.object(tp, "telegram_get_file", return_value="/tmp/x.jpg"):
            att = tp.fetch_attachment(self._media(), "tok", "https://api.telegram.org", self.dir)
        self.assertNotIn("file_id", att)
        self.assertNotIn("refetchable", att)

    def test_a_failure_with_no_file_id_still_reads_as_it_always_did(self):
        line = pr.telegram_inbound_text({"attachment": {"kind": "photo", "error": "boom"}})
        self.assertIn("download failed (boom)", line)
        self.assertNotIn("refetch", line)

    def test_the_refetch_door_exists_and_reuses_the_one_download_path(self):
        """`refetchable: true` is a claim, and this is what makes it true."""
        with mock.patch.object(tp, "telegram_get_file", return_value="/inbox/x.jpg") as get:
            with mock.patch.object(sys, "argv",
                                   ["telegram_poll.py", "--refetch", "AgACAgEAAx",
                                    "--download-dir", self.dir]):
                with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
                    with mock.patch("sys.stdout", io.StringIO()) as out:
                        rc = tp.main()
        self.assertEqual(rc, 0)
        self.assertEqual(get.call_args[0][2], "AgACAgEAAx")
        self.assertEqual(json.loads(out.getvalue())["local_path"], "/inbox/x.jpg")


class OffsetOnlyAdvancesOnASuccessfulRead(unittest.TestCase):
    """Telegram never re-sends an acknowledged update, so an offset advanced past a read that failed
    is messages destroyed. This was already correct; the tests are here so it stays that way."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.offset_file = os.path.join(self.dir, "telegram-offset")
        with open(self.offset_file, "w", encoding="utf-8") as fh:
            fh.write("100")

    def _main(self, argv_extra):
        with mock.patch.object(sys, "argv", ["telegram_poll.py", "--offset-file", self.offset_file,
                                             "--commit"] + argv_extra):
            with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok"}):
                with mock.patch("sys.stdout", io.StringIO()) as out:
                    return tp.main(), out.getvalue()

    def test_a_failed_read_leaves_the_offset_alone(self):
        with mock.patch.object(tp, "get_updates",
                               side_effect=th.TelegramHTTPError("reset (gave up after 4 attempts)")):
            rc, _ = self._main([])
        self.assertEqual(rc, 1)
        self.assertEqual(tp.read_offset(self.offset_file), 100)

    def test_a_successful_read_advances_past_the_batch(self):
        updates = [{"update_id": 101, "message": {"message_id": 5, "chat": {"id": "1"},
                                                  "from": {"username": "c"}, "text": "hi"}}]
        with mock.patch.object(tp, "get_updates", return_value=updates):
            rc, _ = self._main([])
        self.assertEqual(rc, 0)
        self.assertEqual(tp.read_offset(self.offset_file), 102)

    def test_a_failed_ATTACHMENT_download_does_not_hold_the_batch_back(self):
        """Deliberately: the message itself was read fine, and re-delivering the whole batch to
        chase one file is the wrong trade. The recorded `file_id` is what makes it safe."""
        updates = [{"update_id": 101, "message": {
            "message_id": 5, "chat": {"id": "1"}, "from": {"username": "c"},
            "photo": [{"file_id": "fid", "file_unique_id": "u", "file_size": 10}]}}]
        with mock.patch.object(tp, "get_updates", return_value=updates):
            with mock.patch.object(tp, "telegram_get_file", side_effect=RuntimeError("reset")):
                rc, out = self._main(["--download-dir", self.dir])
        self.assertEqual(rc, 0)
        self.assertEqual(tp.read_offset(self.offset_file), 102)
        att = json.loads(out)["messages"][0]["attachment"]
        self.assertEqual(att["file_id"], "fid")
        self.assertTrue(att["refetchable"])


class ApiCallPicksThePolicyByMethod(unittest.TestCase):
    """`telegram_ask.py` imports `api_call` too, so this one line is the policy for every send, edit
    and `answerCallbackQuery` in the tree."""

    def _policy_for(self, method):
        seen = {}

        def fake_post(url, params, timeout=30, policy=None, **kw):
            seen["policy"] = policy
            return th.Response(200, {}, b'{"ok":true,"result":{"message_id":1}}', 1)

        with mock.patch.object(ts.th, "post_form", fake_post):
            ts.api_call({"token": "t", "api_base": "https://api.telegram.org"}, method, {})
        return seen["policy"]

    def test_sends_edits_and_callbacks_get_the_conservative_policy(self):
        for method in ("sendMessage", "editMessageText", "editMessageReplyMarkup",
                       "answerCallbackQuery"):
            with self.subTest(method=method):
                self.assertIs(self._policy_for(method), th.UNSAFE)

    def test_reads_get_the_free_retry_policy(self):
        for method in ("getMe", "getUpdates", "getFile", "getCustomEmojiStickers"):
            with self.subTest(method=method):
                self.assertIs(self._policy_for(method), th.IDEMPOTENT)

    def test_an_unknown_method_defaults_to_conservative(self):
        """Forgetting to update the list can only ever cost a retry — never send the owner the same
        message twice. The default leaning this way is the whole safety of the mechanism."""
        self.assertIs(self._policy_for("sendPhoto"), th.UNSAFE)
        self.assertIs(self._policy_for("somethingTelegramAddsIn2027"), th.UNSAFE)

    def test_the_idempotent_list_holds_only_reads(self):
        for method in ts.IDEMPOTENT_METHODS:
            self.assertTrue(method.startswith("get"),
                            f"{method} is in IDEMPOTENT_METHODS but is not a read")


class FormattingFallbackHonoursThePhase(unittest.TestCase):
    """The HTML→plain re-send is a SEMANTIC fallback (Telegram rejected the entities), not a
    transport retry, and the two must not be folded together — but **they answer the same question
    and the fallback used not to ask it.**

    This class was `FormattingFallbackIsUntouched`, and its second case asserted that a
    `PHASE_AMBIGUOUS` failure *still* got the plain re-send, on the reasoning that *"for any reason
    at all"* was a load-bearing clause predating the transport split. **That test was wrong and it
    pinned the bug in place.** A failure after the request went out means Telegram may already have
    delivered the message; re-sending it is the duplicate the whole `UNSAFE` policy exists to
    prevent — the same reply twice, once HTML, once plain. The case is inverted below. The `telegram_send` side of the same rule, in full:
    `test_telegram_format.AmbiguousFailureIsNeverReSentTests`."""

    def _cfg(self):
        return {"token": "t", "chat_id": "1", "api_base": "https://api.telegram.org",
                "parse_mode": "", "format": "markdown"}

    def test_a_rejected_html_chunk_is_re_sent_plain_exactly_once(self):
        """Telegram ANSWERED and refused, so nothing was delivered and the fallback is exactly
        right. This half is unchanged and must stay that way."""
        calls = []

        def api(c, method, params, timeout=30):
            calls.append(params)
            if params.get("parse_mode") == "HTML":
                raise ts.TelegramAPIError("Telegram sendMessage failed: can't parse entities")
            return {"ok": True, "result": {"message_id": 7}}

        res = ts.send_text(self._cfg(), "**bold**", api=api)
        self.assertTrue(res["degraded"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["text"], "**bold**")  # the ORIGINAL, never a half-converted string
        self.assertIsNone(calls[1]["parse_mode"])

    def test_an_ambiguous_transport_failure_gets_NO_plain_re_send(self):
        """The inversion. `UNSAFE` refused to retry it precisely because it may have been delivered;
        the formatting layer must not undo that one frame up."""
        calls = []

        def api(c, method, params, timeout=30):
            calls.append(params)
            raise th.TelegramHTTPError("reset", phase=th.PHASE_AMBIGUOUS, attempts=1)

        with self.assertRaises(ts.AmbiguousSendError):
            ts.send_text(self._cfg(), "**bold**", api=api)
        self.assertEqual(len(calls), 1, "one attempt out; a second is the duplicate")

    def test_a_pre_delivery_transport_failure_still_gets_the_plain_re_send(self):
        """The phase seam proved not one byte was written, so re-sending cannot duplicate — and
        *"a formatting failure never costs a message"* keeps its teeth wherever it safely can."""
        calls = []

        def api(c, method, params, timeout=30):
            calls.append(params)
            if params.get("parse_mode") == "HTML":
                raise th.TelegramHTTPError("refused", phase=th.PHASE_PRE_DELIVERY, attempts=3)
            return {"ok": True, "result": {"message_id": 7}}

        res = ts.send_text(self._cfg(), "**bold**", api=api)
        self.assertTrue(res["degraded"])
        self.assertEqual(len(calls), 2)


class BuildMultipartTest(unittest.TestCase):
    """`telegram_http.build_multipart` — the shared multipart body builder `send_photo` uses, since
    `api_call`/`post_form` are form-urlencoded and have no file-upload door."""

    def test_body_carries_fields_and_file_bytes(self):
        body, content_type = th.build_multipart(
            {"chat_id": "999", "caption": "hi"}, "photo", "snap.jpg", b"\xff\xd8fakejpeg"
        )
        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(b"999", body)
        self.assertIn(b'name="caption"', body)
        self.assertIn(b'name="photo"; filename="snap.jpg"', body)
        self.assertIn(b"\xff\xd8fakejpeg", body)

    def test_none_valued_fields_are_dropped(self):
        body, _ = th.build_multipart(
            {"chat_id": "999", "message_thread_id": None}, "photo", "snap.jpg", b"x"
        )
        self.assertNotIn(b'name="message_thread_id"', body)

    def test_boundary_is_unique_per_call(self):
        _, ct1 = th.build_multipart({"a": "1"}, "file", "f", b"x")
        _, ct2 = th.build_multipart({"a": "1"}, "file", "f", b"x")
        self.assertNotEqual(ct1, ct2)


class SendPhotoTest(unittest.TestCase):
    """`telegram_send.send_photo` — the capability `telegram-send-document-spec.md` §2 filed
    `sendPhoto` under. Offline: `api`
    is the transport seam, exactly as `send_text` uses it."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        self.tmp.write(b"\xff\xd8fakejpeg")
        self.tmp.close()
        self.addCleanup(os.remove, self.tmp.name)

    def _cfg(self):
        return {"token": "123:abc", "chat_id": "999", "api_base": "https://api.telegram.org"}

    def _fake_response(self, payload):
        return type("R", (), {"body": json.dumps(payload).encode("utf-8"), "status": 200})()

    def test_happy_path_posts_multipart_with_unsafe_policy(self):
        calls = []

        def api(url, **kw):
            calls.append(kw)
            return self._fake_response({"ok": True, "result": {"message_id": 42}})

        res = ts.send_photo(self._cfg(), self.tmp.name, caption="plate clear?", api=api)
        self.assertEqual(res["result"]["message_id"], 42)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertIs(calls[0]["policy"], th.UNSAFE)
        self.assertIn("multipart/form-data; boundary=", calls[0]["headers"]["Content-Type"])

    def test_api_rejection_raises_telegram_api_error(self):
        def api(url, **kw):
            return self._fake_response({"ok": False, "description": "chat not found"})

        with self.assertRaises(ts.TelegramAPIError):
            ts.send_photo(self._cfg(), self.tmp.name, api=api)

    def test_caption_over_1024_chars_refuses_before_any_network_call(self):
        calls = []

        def api(url, **kw):
            calls.append(kw)
            return self._fake_response({"ok": True, "result": {}})

        with self.assertRaises(ValueError):
            ts.send_photo(self._cfg(), self.tmp.name, caption="x" * 1025, api=api)
        self.assertEqual(calls, [], "a local refusal must never touch the network")

    def test_missing_token_refuses_before_any_network_call(self):
        calls = []

        def api(url, **kw):
            calls.append(kw)
            return self._fake_response({"ok": True, "result": {}})

        c = {"token": None, "chat_id": "999", "api_base": "https://api.telegram.org"}
        with self.assertRaises(RuntimeError):
            ts.send_photo(c, self.tmp.name, api=api)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
