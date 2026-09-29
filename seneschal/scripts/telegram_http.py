#!/usr/bin/env python3
"""The one HTTP transport every Telegram call goes through. Standard library only.

**Why this module exists.** An inbound photo's attachment download can die with
``<urlopen error [WinError 10054] An existing connection was forcibly closed by the remote host>`` —
one transient TCP reset — and without a retry the message reaches the warm session with the image
replaced by a *"download failed"* placeholder: nothing retries it, nothing queues it, nothing records
that it is still fetchable, and the owner has to notice the reply ignored the picture and re-send it
by hand. With every HTTP call in the Telegram path a bare `urllib.request.urlopen(...)`, **one reset
silently and permanently degrades an inbound message.**

**THE DESIGN IS THE SPLIT, NOT THE RETRY. A SEND IS NOT FREELY RETRYABLE.** Telegram's
``sendMessage`` has no idempotency key, and a reset can land *after* Telegram accepted the message
and *before* the response came back. A blind retry there sends the owner the same message twice —
and on the channel that carries their critical reminders, a duplicate nudge is a real harm, not a
cosmetic one. **Under-sending is recoverable; double-sending is not.** So:

* **Reads and downloads** (``getUpdates`` / ``getFile`` / the file fetch / ``getMe`` /
  ``getCustomEmojiStickers``) are idempotent — :data:`IDEMPOTENT` retries **every** recognised
  transient signature, in either phase.
* **Sends, edits and ``answerCallbackQuery``** get :data:`UNSAFE`, which retries **only failures
  that provably happened before the request was delivered.**

**How "provably before" is established, since that is the whole load-bearing claim.** `urlopen`
cannot answer it: it collapses connect, write and read into one call, so a `URLError` wrapping a
reset is indistinguishable between *"the TCP handshake was refused"* and *"Telegram took the
message and then the socket died."* So for the calls where the answer changes what we do,
:func:`_perform_direct` drives `http.client` itself and splits the exchange in two at the one place
stdlib gives you a seam:

1. ``conn.connect()`` — DNS resolution, the TCP handshake, the TLS handshake. **Not one byte of the
   request has been written.** Anything that fails here is :data:`PHASE_PRE_DELIVERY` and is safe to
   retry on any policy.
2. ``conn.request()`` / ``getresponse()`` / the body read — the request is on the wire.
   :data:`PHASE_AMBIGUOUS`, always, even for a failure inside `request()` itself: a reset while the
   body is being written is *also* what a server that read the whole body and then hung up looks
   like. **The classification is positional, not a guess at the exception's meaning**, which is why
   it can be trusted; widening it into pattern-matching on exception types is the regression.

**Reads deliberately do NOT take that path, and that is not an oversight.** :data:`IDEMPOTENT`
retries both phases, so the seam would answer a question whose answer changes nothing — and
`urlopen` brings two things worth keeping: it honours ``HTTPS_PROXY``/``HTTP_PROXY``, and it is the
layer the existing poller tests already stub. :func:`_perform` therefore picks the phased transport
**only when the policy would actually refuse an ambiguous failure**. A phase seam nobody consults is
a second code path that can rot; this way there is exactly one caller of the expensive one.

**The proxy escape hatch, and why it fails to the conservative side.** A direct `http.client`
connection ignores the proxy environment, so on a proxied host the send path would break outright.
:func:`_proxied` detects that and sends it back through `urlopen`, where the seam does not exist and
every failure is therefore classified AMBIGUOUS — so a proxied send retries nothing at all and
behaves exactly as it did before this module existed. Losing the retry is the right way to lose.

**Two more rules that are not obvious from the code alone:**

* **HTTP 429 retries on both policies, and honours Telegram's own number.** A 429 is the server
  saying it *refused* the request, so nothing was delivered and a retry cannot duplicate. Telegram
  puts the wait in ``parameters.retry_after`` in the JSON error body (with a ``Retry-After`` header
  as the fallback); that value wins over our own backoff schedule, because guessing shorter just
  earns another 429. **When this module itself gives up on a 429** (its ``retry_after`` exceeds
  :data:`MAX_RETRY_AFTER`, or the attempt/wall-clock budget runs out), the raised
  :class:`TelegramHTTPError` is tagged :data:`PHASE_REFUSED`, not left at ``phase=None`` — a bare
  ``None`` reads as :data:`PHASE_AMBIGUOUS` at the caller, which is backwards for a status that means
  the server refused the request rather than one where we lost track of it.
* **HTTP 5xx retries on reads and NOT on sends.** The server answered, which means it received the
  request — whether it *processed* it before failing is exactly the ambiguity the send policy
  refuses to gamble on.

Everything is bounded three ways at once so nothing can flail: an attempt cap, exponential backoff
capped at :attr:`RetryPolicy.max_delay`, and a **total wall-clock ceiling** checked before each new
attempt. Jitter is added so several callers waking from the same outage do not resynchronise.

This module never logs and never prints — it raises :class:`TelegramHTTPError` with the phase, the
attempt count and the underlying cause in the message, and its callers own what that means.

**AND NO MESSAGE IT RAISES CARRIES THE BOT TOKEN.** Its callers *persist* these strings
— `telegram_send.log_format_fallback` into `state/telegram-format-fallback.jsonl`, the daemon into
`state/presence.log` — and both files are read by agents, so an error message here is a durable,
re-read artefact rather than a line that scrolls past. Every message used to be built from
``url.split('?')[0]`` would strip only the **query** while the Bot API keeps its credential in
the **path** (``/bot<TOKEN>/sendMessage``), writing the live token verbatim into both. Two things
now prevent that and they are deliberately redundant: :func:`redact_url` at each formatting site,
and :func:`redact` inside :class:`TelegramHTTPError`'s constructor, so a message composed by some
future route nobody enumerated is scrubbed anyway. **What survives is the method name** —
``sendMessage`` versus ``editMessageText`` is how these failures get diagnosed, so redaction is
scoped to the one path segment and never to the whole URL. Anything that formats a Bot API URL for a
human or a log goes through that helper; there is no second door. Log rows written by an older,
unredacted build are cleaned by `scrub_telegram_token.py`.

`sleep`, `monotonic` and `jitter` are test seams on every entry point, so the suite exercises the
ceilings without spending a single real second.
"""
from __future__ import annotations

import collections
import http.client
import json
import os
import random
import re
import shutil
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

#: The request never reached Telegram — DNS, the TCP handshake or the TLS handshake failed. Safe to
#: retry under any policy, including a send.
PHASE_PRE_DELIVERY = "pre-delivery"
#: The request was on the wire when it failed. Telegram may or may not have acted on it. Retryable
#: only when the call is idempotent.
PHASE_AMBIGUOUS = "ambiguous"
#: Telegram answered with HTTP 429 and this module chose not to retry it (its own `retry_after`
#: exceeded :data:`MAX_RETRY_AFTER`, or the retry budget/wall-clock ceiling ran out while still
#: getting 429s). **A 429 means refused, not accepted** — nothing was delivered, so this is safe to
#: retry/resend under any policy, unlike :data:`PHASE_AMBIGUOUS`. Distinct from that phase precisely
#: so a caller (`telegram_send.classify_send_failure`) does not have to pattern-match the message to
#: tell "refused" apart from "we don't know" — without it, a flood-waited send exceeding the
#: ceiling raises with `phase=None`, reads as ambiguous, is never re-sent, and the reply/reminder it
#: carried is silently dropped.
PHASE_REFUSED = "refused"

RetryPolicy = collections.namedtuple(
    "RetryPolicy",
    "name attempts base_delay max_delay total_seconds retry_ambiguous retry_server_error")

#: Reads and downloads: `getUpdates`, `getFile`, the file fetch, `getMe`, `getCustomEmojiStickers`.
#: Re-running one of these costs a round trip and nothing else, so both phases retry.
IDEMPOTENT = RetryPolicy("idempotent", attempts=4, base_delay=0.5, max_delay=8.0,
                         total_seconds=45.0, retry_ambiguous=True, retry_server_error=True)

#: Sends, edits, `answerCallbackQuery`. **Pre-delivery failures only** — see the module docstring.
#: Deliberately shorter than :data:`IDEMPOTENT` as well as narrower: a send is on a path the owner is
#: waiting on, and the fallbacks above it (the plain-text re-send, the daemon's own queues) all
#: assume this returns rather than grinding.
UNSAFE = RetryPolicy("unsafe", attempts=3, base_delay=0.5, max_delay=4.0,
                     total_seconds=20.0, retry_ambiguous=False, retry_server_error=False)

#: What a `retry_after` may never turn into: a caller blocked past its own patience. Telegram's
#: floods are seconds; anything larger is answered by giving up and letting the caller decide.
MAX_RETRY_AFTER = 60.0

Response = collections.namedtuple("Response", "status headers body attempts")


#: What a Bot API URL puts where a path segment goes: ``/bot<TOKEN>/<method>``. **The token is the
#: whole credential** — anyone holding it can read every message the owner has ever sent this bot
#: and send as the assistant — and `url.split('?')[0]` strips only the QUERY while the token is in
#: the PATH. Error messages are persisted (`state/telegram-format-fallback.jsonl`, the daemon's
#: `presence.log`) and read by agents, so a leak there is durable and on a surface built for other
#: readers.
#:
#: **This matches the segment, not the token's shape, and that is deliberate.** A pattern like
#: ``\d+:[\w-]{30,}`` reads as more precise and fails open on everything it did not predict — a
#: test double, a proxy that rewrites the segment, a future Bot API id format. Whatever sits between
#: ``/bot`` and the next separator is a credential by position, so it goes. Over-redacting costs a
#: log line some noise; under-redacting costs the token.
_BOT_SEGMENT = re.compile(r"(?<=/)bot[^/?#\s\"'<>]+")

#: What replaces it. Keeps the ``bot`` prefix so the URL still reads as a Bot API URL.
REDACTED = "bot<redacted>"


def redact(text: str) -> str:
    """Every ``/bot<TOKEN>`` path segment in `text`, replaced by :data:`REDACTED`.

    Takes arbitrary text, not just a URL, because the whole point is that there is **one** door: a
    cause string, a proxy's echo of the request line, anything that reached a message by a route
    nobody enumerated, is scrubbed by the same call. Cheap enough to be unconditional."""
    return _BOT_SEGMENT.sub(REDACTED, text) if text else text


def redact_url(url: str) -> str:
    """A Bot API URL as it may be written into something a human or a log will read.

    Two things happen and both are load-bearing. The query goes (`file_id`, `offset`, the message
    body on a GET — noise at best), **and the token goes**. What survives is the part that makes the
    error diagnosable: the host and the **method name**. ``sendMessage`` failing is a different
    incident from ``editMessageText`` failing, and an error that cannot tell you which is worth
    little — so the redaction is scoped to the one segment rather than to the path."""
    return redact(str(url).split("?", 1)[0])


class TelegramHTTPError(RuntimeError):
    """A Telegram HTTP call that the retry policy could not rescue.

    Carries `phase` (:data:`PHASE_PRE_DELIVERY` / :data:`PHASE_AMBIGUOUS` / :data:`PHASE_REFUSED` /
    ``None`` for any other HTTP status), `attempts` and the underlying `cause`, because *"was this
    safe to retry?"* is the first question anyone reading the failure will ask."""

    def __init__(self, message: str, *, phase: str | None = None, attempts: int = 0, cause=None):
        # **Redacted here, not only at the call sites below, and that is the point.** Every message
        # in this module is built by hand from a URL and a cause; a future one built by a route
        # nobody thought of would leak again if this only lived in `redact_url`. Applying it at
        # construction makes "no TelegramHTTPError carries the token" a property of the TYPE.
        super().__init__(redact(message))
        self.phase = phase
        self.attempts = attempts
        self.cause = cause


class _PhaseError(Exception):
    """Internal: an exception tagged with the phase it happened in. Never escapes this module."""

    def __init__(self, phase: str, cause: BaseException):
        super().__init__(str(cause) or cause.__class__.__name__)
        self.phase = phase
        self.cause = cause


# The recognised transient signatures. Everything outside this set is TERMINAL and is raised on the
# first attempt — jobs.py's `TRANSIENT_SIGNATURES` rule, in exception form: *unrecognised failure is
# terminal, first time*, because a too-eager retry re-runs whatever the failure actually was.
_TRANSIENT_TYPES = (
    ConnectionResetError,      # WinError 10054 — the mid-download reset this module exists for
    ConnectionRefusedError,    # WinError 10061 — nothing listening
    ConnectionAbortedError,    # WinError 10053
    BrokenPipeError,
    TimeoutError,              # `socket.timeout` is an alias of this since 3.10
    socket.gaierror,           # DNS
    socket.herror,
    ssl.SSLError,              # TLS handshake / session failures
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    http.client.IncompleteRead,
)


def is_transient(exc: BaseException) -> bool:
    """Is this failure worth trying again at all (before any policy question)?

    **`SSLCertVerificationError` is deliberately excluded.** It is an `SSLError`, but it is
    deterministic — a bad chain, a wrong hostname or a clock problem fails identically four times in
    a row, and burning the ceiling on it only delays the real answer."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False
    if isinstance(exc, _TRANSIENT_TYPES):
        return True
    # urlopen wraps transport failures; the reason carries the real one (the proxied path only).
    reason = getattr(exc, "reason", None)
    if isinstance(exc, urllib.error.URLError) and isinstance(reason, BaseException):
        return is_transient(reason)
    return False


def _proxied(url: str) -> bool:
    """Does a proxy apply to this URL? If so the direct `http.client` path would bypass it, so we
    fall back to `urlopen` — and give up the phase seam, conservatively (see the module docstring).

    Fails **towards** the fallback: anything we cannot determine is treated as proxied, because a
    send that goes out through the wrong socket is worse than a send that retries nothing."""
    try:
        parts = urllib.parse.urlsplit(url)
        proxies = urllib.request.getproxies()
        if parts.scheme not in proxies:
            return False
        return not urllib.request.proxy_bypass(parts.netloc)
    except Exception:  # noqa: BLE001
        return True


def _target(url: str):
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return parts.scheme, parts.hostname, parts.port, path


def _perform_direct(method: str, url: str, data, headers: dict, timeout: float, reader):
    """One exchange over a connection we open ourselves, so the two phases stay distinguishable.

    **Do not collapse the two try blocks.** The boundary between them *is* the pre-delivery /
    ambiguous distinction the send policy rests on: `connect()` completes DNS, TCP and TLS without
    writing a byte of the request, and everything after it is on the wire."""
    scheme, host, port, path = _target(url)
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = cls(host, port, timeout=timeout)
    try:
        try:
            conn.connect()
        except Exception as e:  # noqa: BLE001
            raise _PhaseError(PHASE_PRE_DELIVERY, e) from e
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), reader(resp)
        except Exception as e:  # noqa: BLE001
            raise _PhaseError(PHASE_AMBIGUOUS, e) from e
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _perform_urlopen(method: str, url: str, data, headers: dict, timeout: float, reader):
    """The `urlopen` transport: every idempotent call, plus any call at all on a proxied host.

    It honours the proxy environment and it erases the phase seam, so **everything that fails here
    is AMBIGUOUS.** For a read that costs nothing (ambiguous is retryable under
    :data:`IDEMPOTENT`); for a send under a proxy it costs the retry, which is the direction to lose
    in."""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _status_of(resp), _headers_of(resp), reader(resp)
    except urllib.error.HTTPError as e:  # a status, not a transport failure
        with e:
            return e.code, _headers_of(e), reader(e)
    except Exception as e:  # noqa: BLE001
        raise _PhaseError(PHASE_AMBIGUOUS, e) from e


def _status_of(resp) -> int:
    """`HTTPResponse.status`, tolerating the older `.getcode()` spelling. Only 429 and 5xx change
    what :func:`request` does, so an unreadable status resolves to 200 — *"the transport gave us a
    response body"* — and the ``ok``/``description`` check at the call site remains the judge, which
    is what it has always been for the Bot API anyway."""
    for attr in ("status", "code"):
        value = getattr(resp, attr, None)
        if isinstance(value, int):
            return value
    return 200


def _headers_of(resp) -> dict:
    """Response headers as a plain dict. Used for exactly one thing — the `Retry-After` fallback on
    a 429 — so a response that cannot produce them degrades to Telegram's JSON `retry_after` and
    then to our own backoff, never to an exception."""
    try:
        if hasattr(resp, "getheaders"):
            return dict(resp.getheaders())
        return dict(getattr(resp, "headers", {}).items())
    except Exception:  # noqa: BLE001
        return {}


def _perform(method: str, url: str, data, headers: dict, timeout: float, reader,
             policy: RetryPolicy):
    """Pick the transport and run one exchange. Returns ``(status, headers, body)``; raises
    :class:`_PhaseError`.

    **The phased transport is used only where its answer changes the outcome** — i.e. where the
    policy refuses ambiguous failures — and never when a proxy applies. Everything else takes
    `urlopen`. This is also the module's single test seam: replacing this one function replaces the
    network for `request`, `get_json`, `post_form` and `download` alike."""
    if policy.retry_ambiguous or _proxied(url):
        return _perform_urlopen(method, url, data, headers, timeout, reader)
    return _perform_direct(method, url, data, headers, timeout, reader)


def _retry_after(headers: dict, body: bytes) -> float | None:
    """Telegram's own answer to *"how long?"* on a 429, from `parameters.retry_after` in the JSON
    error body, else the `Retry-After` header. ``None`` when it said nothing and we fall back to our
    own backoff."""
    try:
        payload = json.loads((body or b"").decode("utf-8"))
        value = ((payload or {}).get("parameters") or {}).get("retry_after")
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    except Exception:  # noqa: BLE001
        pass
    for key, value in (headers or {}).items():
        if key.lower() == "retry-after":
            try:
                return float(str(value).strip())
            except (TypeError, ValueError):
                return None
    return None


def _backoff(attempt: int, policy: RetryPolicy, jitter) -> float:
    """Exponential, capped, with up to 25% added jitter so a fleet of callers coming off one outage
    does not re-converge on the same instant."""
    delay = min(policy.base_delay * (2 ** (attempt - 1)), policy.max_delay)
    return delay + delay * 0.25 * jitter()


def request(url: str, *, method: str = "GET", data=None, headers: dict | None = None,
            timeout: float = 30, policy: RetryPolicy = IDEMPOTENT, reader=None,
            sleep=time.sleep, monotonic=time.monotonic, jitter=random.random) -> Response:
    """One Telegram HTTP call, retried according to `policy`. Returns the :class:`Response` for any
    status the policy does not retry; raises :class:`TelegramHTTPError` when the policy is spent.

    The three bounds are all live at once and the wall-clock one is checked **before** sleeping into
    the next attempt, so the ceiling is a promise about when this returns rather than a hope.

    **An unrecognised failure is TERMINAL on the first attempt, never retried** — `is_transient`
    returning `False` raises immediately, with no attempt spent probing whether it might clear up.
    This is `jobs.py`'s rule in exception form: an unknown failure mode gets no benefit of the doubt
    and no budget burned guessing at it.

    **This function (via `_perform`) is THE test seam for the whole transport, and it is deliberately
    NOT folded together with `telegram_format.py`'s formatting fallback below it in the send path** —
    two different mechanisms answering the SAME underlying question ("was this provably not
    delivered?"). A past version of this sentence described only the docstring and not the code: `telegram_send.py`'s formatting fallback caught
    every exception and re-sent regardless of what this module's own phase classification said,
    which is exactly the duplicate-message bug the split above exists to prevent."""
    reader = reader or (lambda resp: resp.read())
    headers = dict(headers or {})
    started = monotonic()
    attempt = 0
    while True:
        attempt += 1
        phase = None
        cause = None
        try:
            status, resp_headers, body = _perform(method, url, data, headers, timeout, reader,
                                                  policy)
        except _PhaseError as e:
            phase, cause, wait = e.phase, e.cause, None
            if not is_transient(cause):
                raise TelegramHTTPError(f"{redact_url(url)}: {cause}", phase=phase,
                                        attempts=attempt, cause=cause) from cause
            if phase == PHASE_AMBIGUOUS and not policy.retry_ambiguous:
                # The one refusal this module exists for. Say so in the message: a send that failed
                # after the bytes went out must never LOOK like a send that simply didn't happen.
                raise TelegramHTTPError(
                    f"{redact_url(url)}: {cause} — failed after the request was sent, so Telegram "
                    f"may already have acted on it; NOT retried (a blind retry here duplicates the "
                    f"message)", phase=phase, attempts=attempt, cause=cause) from cause
            reason = f"{cause}"
        else:
            wait = None
            if status == 429:
                # Refused, therefore not delivered, therefore safe to retry on ANY policy. Tagged
                # PHASE_REFUSED so that holds true even when THIS module gives up on the
                # 429 itself (ceiling exceeded below, or the retry budget below runs out while still
                # getting 429s) — a bare `phase=None` read as PHASE_AMBIGUOUS at the caller, which is
                # backwards for a status that means "refused", not "we don't know".
                phase = PHASE_REFUSED
                wait = _retry_after(resp_headers, body)
                if wait is not None and wait > MAX_RETRY_AFTER:
                    raise TelegramHTTPError(
                        f"{redact_url(url)}: HTTP 429, retry_after {wait:g}s exceeds the "
                        f"{MAX_RETRY_AFTER:g}s ceiling", phase=phase, attempts=attempt)
                reason = "HTTP 429"
            elif 500 <= status < 600 and policy.retry_server_error:
                reason = f"HTTP {status}"
            else:
                return Response(status, resp_headers, body, attempt)

        if attempt >= policy.attempts:
            raise TelegramHTTPError(f"{redact_url(url)}: {reason} (gave up after {attempt} "
                                    f"attempts)", phase=phase, attempts=attempt, cause=cause)
        if wait is None:
            wait = _backoff(attempt, policy, jitter)
        if monotonic() - started + wait > policy.total_seconds:
            raise TelegramHTTPError(
                f"{redact_url(url)}: {reason} (gave up after {attempt} attempts; the next wait "
                f"would pass the {policy.total_seconds:g}s ceiling)",
                phase=phase, attempts=attempt, cause=cause)
        sleep(wait)


def build_multipart(fields: dict, file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    """A `multipart/form-data` body for a Bot API upload (`sendPhoto`, `sendDocument`) — stdlib only,
    no `requests`. `fields` are plain form fields (a `None` value is dropped, matching `post_form`'s
    own filtering); `file_field` is the file part's field name. The one multipart builder every
    Telegram media send goes through, the same way `request` is the one transport — `telegram_send
    .send_photo` and `telegram_send.send_document` are today's callers."""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            .encode("utf-8")
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
    )
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def get_json(url: str, *, timeout: float = 30, policy: RetryPolicy = IDEMPOTENT, **kw) -> dict:
    """A retried GET whose body is parsed as JSON. Telegram answers its API errors with a JSON body
    on 4xx too, so a non-200 with a readable body is handed back to the caller to interpret — the
    ``ok``/``description`` check belongs to the call site, which knows what it asked for."""
    res = request(url, method="GET", timeout=timeout, policy=policy, **kw)
    try:
        return json.loads(res.body.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise TelegramHTTPError(f"{redact_url(url)}: HTTP {res.status} with an unreadable body",
                                attempts=res.attempts, cause=e) from e


def post_form(url: str, params: dict, *, timeout: float = 30, policy: RetryPolicy = UNSAFE,
              **kw) -> Response:
    """A form-encoded POST. **Defaults to the conservative policy** — a caller that knows its method
    is idempotent has to say so, which is the safe direction for the default to lean.

    `Content-Type` is set explicitly because `http.client` (unlike `urlopen`) does not add it."""
    data = urllib.parse.urlencode(params).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               **(kw.pop("headers", None) or {})}
    return request(url, method="POST", data=data, headers=headers, timeout=timeout, policy=policy,
                   **kw)


def download(url: str, dest: str, *, timeout: float = 60, policy: RetryPolicy = IDEMPOTENT,
             **kw) -> str:
    """Fetch `url` to `dest`, retried. Returns `dest`.

    **The retry wraps the body copy, not just the connect** — a connection reset is exactly the
    kind of failure that lands mid-stream, and a retry that only covered the handshake would not have saved it.
    That means a failed attempt can leave a half-written file, so the copy goes to a `.part`
    sidecar and only reaches `dest` via `os.replace` once the whole body is down: **a partial
    download is never visible under the real name**, and a caller that finds the file finds all of
    it."""
    tmp = dest + ".part"

    def reader(resp):
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        return b""

    try:
        res = request(url, method="GET", timeout=timeout, policy=policy, reader=reader, **kw)
        if res.status != 200:
            raise TelegramHTTPError(f"{redact_url(url)}: HTTP {res.status} downloading the file",
                                    attempts=res.attempts)
        os.replace(tmp, dest)
        return dest
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
