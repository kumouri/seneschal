#!/usr/bin/env python3
"""Send a Telegram message as the assistant's bot. Standard library only.

Telegram bots talk to a simple HTTPS API (https://api.telegram.org/bot<TOKEN>/sendMessage). This is
the assistant's free, always-with-the-owner push + reply channel: reminders, nudges, approval
prompts, and the assistant's side of the two-way chat all go out through here. The inbound side is
`telegram_poll.py`.

CREDENTIALS — never hard-coded. Provide via environment or an --env-file (KEY=VALUE lines):
  TELEGRAM_BOT_TOKEN     the bot token from @BotFather (required)
  TELEGRAM_CHAT_ID       default recipient chat id (the owner's DM with the bot); overridable with --chat-id
  TELEGRAM_API_BASE      default https://api.telegram.org  (override only for a proxy/test)
  TELEGRAM_PARSE_MODE    default ""  ("MarkdownV2" | "HTML" | "" for plain) — plain avoids escaping traps
  TELEGRAM_FORMAT        default "plain"  ("markdown" converts the assistant's Markdown → Telegram HTML)

Setup is documented in TELEGRAM_SETUP.md.

**MARKDOWN → HTML AT THE SEND BOUNDARY.** The assistant writes Markdown, and a plain-text channel
shows the owner literal `**` around every emphasised phrase. "Just set MarkdownV2" is rejected on
purpose: an unescaped character in MarkdownV2 is a 400 and the message is **gone**, which is
unacceptable on the channel that carries the owner's critical reminders.

`TELEGRAM_FORMAT=markdown` converts through `telegram_format.py` and sends `parse_mode=HTML`.
**THE INVARIANT: a message is never lost to a formatting failure.** Conversion cannot raise into this
path (`plan_send` degrades to plain), the source is chunked BEFORE conversion so no tag can straddle
a 4096-character boundary, and **a chunk Telegram ANSWERED AND REFUSED is retried once, immediately,
as plain text with the ORIGINAL unconverted string** — after which the rest of the message goes plain
too, so one message never arrives half-formatted. Every fallback is logged to
`state/telegram-format-fallback.jsonl` and reported in the result JSON. `TELEGRAM_FORMAT` unset (or
unrecognised) is byte-identical to plain-text behaviour.

**AND A FAILURE THAT MIGHT HAVE LANDED IS NOT A REFUSAL.** A fallback that fires *"for any reason at
all"* sends the owner the same message twice — once as HTML, once as plain. See
:func:`classify_send_failure` and :func:`send_text`.

**TRANSPORT RETRY, AND WHY THIS SIDE IS THE CONSERVATIVE ONE.** Every HTTP call in the Telegram path
goes through `telegram_http.py`. On the inbound side that means free retries; here it means the
opposite, deliberately. `sendMessage` has **no idempotency key**, so a reset that lands after
Telegram accepted the message and before the response came back is indistinguishable — from this
end — from one that landed before it was delivered. A blind retry there sends the owner the same
message twice, on the channel that carries their critical reminders. So a send retries **only**
failures that provably happened before the request was on the wire (DNS, the TCP handshake, the TLS
handshake, a connect-phase reset or refusal), plus HTTP 429, which is Telegram saying it refused the
request outright. An ambiguous failure is reported, not retried, and the error says which it was.
**Under-sending is recoverable; double-sending is not.**

**THE FORMATTING FALLBACK IS A DIFFERENT MECHANISM THAT ASKS THE SAME QUESTION.** It is a *semantic*
re-send — the same words again, unformatted, because Telegram refused the markup — not a transport
retry, and the two must not be folded together. But a `send_text` that caught **every** exception
and re-sent would retry exactly the one failure the transport layer specifically refuses to retry —
duplicate reminder nudges, a long reply twice, once HTML and once plain. **A re-send is only ever
safe on a failure that provably was not a delivery**, so the fallback classifies before it acts
(:func:`classify_send_failure`) and refuses the ambiguous case exactly as `telegram_http` does. Same
rule, one layer up: *under-sending is recoverable; double-sending is not.*

**`--photo`.** `sendPhoto`, stdlib multipart via `telegram_http.build_multipart` — deliberately
narrow: a one-shot evidence photo (a confirmation ask that should carry the picture rather than
describe it). No ack gate, no dedupe, no chunking; see `send_photo`'s own docstring for why none of
those apply here.

**`--document`.** `sendDocument`, mirroring `--photo`'s conventions exactly (same
`telegram_http.build_multipart` door, same `th.UNSAFE` transport policy, mutually exclusive with
`--text`/`--text-file`, no ack gate, no dedupe, no chunking) — for content too large or too
structured to inline into a message body (`telegram-send-document-spec.md`). Capped at
:data:`MAX_DOCUMENT_BYTES` (50 MB — the Bot API's own upload ceiling for `sendDocument`), checked
before the file is read into memory.

**THE WATCH GATES — OPTIONAL, AND ABSENT MEANS NO GATE.** This script is also the outbound path a
**Watch comms peek** uses, and a peek is a second sender that can chase something the owner already
handled. :func:`ack_gate_check` consults up to five independent questions before a Watch send leaves:
the suppression list (`watch_suppress`), a reminder already acked today
(`reminders_acks.watch_escalation_blocked`), the owner's runtime ack of a fact (`watch_ack`), a
duplicate escalation (`reminders_acks.watch_duplicate_blocked`, report-only unless
`WATCH_DEDUPE_ENFORCE`), and source-thread reconciliation (`watch_reconcile`, report-only unless
`WATCH_RECONCILE_ENFORCE`). **Every one of those is an optional module**: the Watch gate set is a
separate component, and when its modules (or the `reminders_acks` watch predicates) are not
installed, :func:`ack_gate_check` answers "send" — the documented no-gate fallback, identical to
running with `--no-ack-gate`. A gate that cannot load may never be the reason the owner isn't told
something. The gate binds on `SENESCHAL_SESSION_SOURCE` (the daemon stamps `watch` on the peek child)
or explicitly via `--ack-gate`; `--reminder-id` makes the reminder check exact instead of
title-matched, `--source-sender`/`--source-subject`/`--source-received-at` give an email-backed
escalation a stable identity and a thread to reconcile against, and `--no-ack-gate` is the escape
hatch. When the gate set is installed, every verdict, blocked or allowed, appends one row to
`state/watch-gate.jsonl`.

**`--message-thread-id` PUTS THE MESSAGE IN A PRIVATE-CHAT TOPIC.** Bot API 9.3 documents
`message_thread_id` for supergroups **and private chats**, and the chat id does not change, so this is
one optional parameter and nothing else: `TELEGRAM_CHAT_ID`, the poller allowlist and every existing
caller are untouched. It rides `params()`, the one builder every chunk and every fallback rung already
goes through, and **the key is absent rather than null when there is no topic**, so a send with topics
off is byte-identical to a pre-topics send.

**AND `--topic PURPOSE` RESOLVES ONE BY NAME, BECAUSE A SUBPROCESS CALLER CANNOT.** A caller that
reaches Telegram by running this file (the reminder fire path, for one) holds no token and no chat id
and can never call `telegram_topics.thread_id` itself. It is **advisory in every direction**: topics
off, an unreachable `getMe`, an unknown purpose, an unreadable state file or a failed creation all
resolve to `None`, the main chat, and the message still goes. An explicit `--message-thread-id`
**wins and skips resolution entirely** — a caller that already knows the thread is not asking a
question, and resolving anyway would spend a round trip and could CREATE a topic on the one path
that named none.

**A THREAD TELEGRAM REFUSES COSTS THE THREAD, NEVER THE MESSAGE** — but only for `--topic`, and only
on a single-chunk message. `SEND_REJECTED` means Telegram answered and refused, so nothing was
delivered and a re-send into the main chat cannot duplicate anything; the stale id is then forgotten
(`telegram_topics.forget`) so the next send creates a fresh topic. **`--message-thread-id` falls
back on nothing**, because a caller that named a raw id owns that decision. The multi-chunk bound is
the honest half: `send_text` re-raises without saying WHICH chunk failed, so this frame cannot prove
nothing landed, and a re-send there would be the duplicate every other rule in this file exists to
prevent.

USAGE:
  python telegram_send.py --text "Reminder: dentist at 3pm"
  python telegram_send.py --text-file body.txt --chat-id 12345678
  python telegram_send.py --dry-run --text "**hi**" --format markdown   # print the HTML, no network
  python telegram_send.py --check-auth              # getMe only (verifies the token), no send
  python telegram_send.py --ack-gate --reminder-id <⏰ row id> --text "…"   # refuse if acked today
  python telegram_send.py --text "…" --message-thread-id 11539          # into a private-chat topic
  python telegram_send.py --text "…" --topic reminders                  # resolve the topic by purpose
  python telegram_send.py --document spec.md --caption "the full spec"           # sendDocument

Prints a one-line JSON result. Exit 0 on success, non-zero on failure — 1 send/transport failure,
2 bad arguments, **3 suppressed by a Watch gate** (only possible when the optional gate set is
installed); the result's `suppressed` field says which (nothing was sent, and nothing is wrong).

**A FAILURE RESULT CARRIES `delivery`, AND `ambiguous: true` IS AN INSTRUCTION TO THE CALLER.** It
means the request went out and Telegram may have delivered it, so **a caller must not automatically
re-send this text** — a blind retry one layer up re-creates exactly the duplicate this module
refuses to create. The daemon's reply path honours it and `telegram_ask.py`'s `_send_text` mirrors
the same instruction (see :func:`classify_send_failure`).

**THIS IS THE ONE CHOKEPOINT EVERY PROSE SEND FUNNELS THROUGH.** The reminder fire path,
`seneschald-control.ps1`, the break-glass supervisor and archon tools all subprocess this file
directly rather than opening their own socket. `send_text` owns the format knob, the chunking and
the fallback; `telegram_ask.py` separately reuses `telegram_format.py`'s converter (not this module)
for its question BODIES, which carry the assistant's prose too, so the two send doors render
identically without sharing code paths that would make one editable without the other.

**THE STALE-THREAD FALLBACK GATES ON *PURPOSE*, NOT ON THREAD** — widening it to gate on the thread
id would let a caller-supplied `--message-thread-id` be silently re-routed to the main chat, a
decision that belongs to the caller alone.

**THE `now=` TEST SEAM ON :func:`ack_gate_check` / :func:`main`** exists because the gate's verdict
turns on *what day it is* in the owner's timezone, so a date-dependent test must inject its instant
rather than bake a calendar date that expires.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_API_BASE", "TELEGRAM_PARSE_MODE",
            "TELEGRAM_FORMAT")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling import also works when we're imported, not run

import telegram_format as tf  # noqa: E402 — one converter, one place
import telegram_http as th  # noqa: E402 — one transport, one place

#: Bot API methods that are safe to re-issue. **The membership test is "does running this twice do
#: anything twice?"** — a read does not, so it gets the free-retry policy; everything else is a
#: conversation with the owner and gets the conservative one. THE DEFAULT IS THE CONSERVATIVE SIDE:
#: a method nobody has thought about lands in `UNSAFE`, so forgetting to update this list can only
#: ever cost a retry, never send the owner the same message twice.
IDEMPOTENT_METHODS = frozenset({"getMe", "getFile", "getUpdates", "getCustomEmojiStickers", "getChat"})

#: Where a degraded send is recorded. Fail-open like every other `state/` writer here: a failed
#: append costs the row, never the message.
FORMAT_FALLBACK_LOG = "telegram-format-fallback.jsonl"

#: `SENESCHAL_SESSION_SOURCE` values whose sends are ack-gated. Only the headless Watch peek today
#: (the daemon stamps `watch` on the peek child) — the warm session (`daemon`) is a conversation, where suppressing a
#: line because it names an acked reminder would be a bug, and the reminder queue's own fires are
#: already gated upstream by the reminder fire path. THE ONE PLACE another headless surface opts in.
ACK_GATE_SOURCES = frozenset({"watch"})
SOURCE_ENV_VAR = "SENESCHAL_SESSION_SOURCE"

#: The `reminders_acks` functions the Watch gate needs. They belong to the optional Watch gate set;
#: when any is missing, :func:`ack_gate_check` takes the documented no-gate fallback (module
#: docstring) rather than half-running a gate it cannot log.
WATCH_GATE_API = ("watch_escalation_blocked", "watch_duplicate_blocked", "fact_key", "log_watch_gate")

#: The dedupe verdict ships REPORT-ONLY first — every armed send still computes and LOGS a duplicate
#: verdict (`reminders_acks.watch_duplicate_blocked`), but does not act on it until this env var is
#: truthy — instrument an automatic judgment before letting it gate. Flipping it is then a one-line, no-rebuild change made after reading a few
#: days of real `watch-gate.jsonl` `dedupe` rows — see `ack_gate_check`.
WATCH_DEDUPE_ENFORCE_ENV_VAR = "WATCH_DEDUPE_ENFORCE"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})

#: Thread reconciliation ships REPORT-ONLY first too — every armed send
#: still computes and LOGS `watch_reconcile.classify`'s verdict, but a SUPERSEDED verdict only actually
#: blocks the send once this env var is truthy. Same flip pattern as dedupe: read a few days of real
#: `watch-gate.jsonl` `reconcile` rows, then set it — see `ack_gate_check`.
WATCH_RECONCILE_ENFORCE_ENV_VAR = "WATCH_RECONCILE_ENFORCE"

#: The Bot API's own upload ceiling for `sendDocument` — checked before the file is read into
#: memory, so an oversized attachment is refused locally rather than mid-upload.
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

#: Telegram **answered and refused**: the HTTP exchange completed and the Bot API said `ok: false`.
#: Re-sending the same words is safe (nothing was delivered) and is the whole point of the
#: formatting fallback. Safe to re-send.
SEND_REJECTED = "rejected"
#: The request **provably never left this machine** — DNS, the TCP handshake or the TLS handshake
#: failed with not one byte of the request written (`telegram_http.PHASE_PRE_DELIVERY`). Safe to
#: re-send.
SEND_UNDELIVERED = "undelivered"
#: **The request went out and we do not know what happened to it.** Telegram may already have
#: delivered the message. NOT safe to re-send, and the default for anything unrecognised.
SEND_AMBIGUOUS = "ambiguous"


class TelegramAPIError(RuntimeError):
    """**Telegram answered and refused.** A 400 on malformed entities, an unknown method, a chat the
    bot cannot post to — the exchange completed and the Bot API returned `ok: false`.

    It exists so the formatting fallback can recognise a genuine rejection **by type** rather than by
    reading the error string, which is the only way to tell it apart from a transport failure that
    may already have delivered the message. It subclasses `RuntimeError` and its message is
    byte-identical to the bare `RuntimeError` `api_call` used to raise, so every existing caller,
    every `except Exception` and every log line is unchanged."""

    def __init__(self, message: str, *, method: str | None = None, description=None,
                 payload: dict | None = None):
        super().__init__(message)
        self.method = method
        self.description = description
        self.payload = payload


class AmbiguousSendError(RuntimeError):
    """A chunk failed **after its request was on the wire**, so Telegram may already have delivered
    it — and therefore nothing was re-sent and nothing further was attempted.

    Carries the accounting a reader needs to answer *"what did the owner actually get?"*: `chunk` (0-based,
    the one whose fate is unknown), `chunks` (how many the message was split into), `delivered` (how
    many Telegram confirmed before it, i.e. chunks `0 .. delivered-1` definitely landed) and
    `message_ids` for those. Chunks after `chunk` were **not attempted**; see :func:`send_text` for
    why that is the decision."""

    def __init__(self, message: str, *, chunk: int, chunks: int, delivered: int,
                 message_ids=None, cause=None):
        super().__init__(message)
        self.chunk = chunk
        self.chunks = chunks
        self.delivered = delivered
        self.message_ids = list(message_ids or [])
        self.cause = cause


def classify_send_failure(exc: BaseException) -> str:
    """*"May we send these words again?"* — :data:`SEND_REJECTED` / :data:`SEND_UNDELIVERED` (yes) or
    :data:`SEND_AMBIGUOUS` (no).

    **THE DEFAULT IS AMBIGUOUS AND THAT IS THE WHOLE MECHANISM.** This function answers "yes" only
    from **positive evidence** — a `TelegramAPIError` (Telegram answered and refused), a
    `TelegramHTTPError` tagged `PHASE_PRE_DELIVERY` (the phase seam proved not one byte of the
    request was written), or one tagged `PHASE_REFUSED` (a 429 — Telegram explicitly refused the
    request, so nothing was delivered). Everything else, including a bare exception from a caller's
    own `api` seam, an unparseable response body and any future failure nobody has thought about yet,
    reads as ambiguous and is not re-sent. Getting this backwards costs a duplicate on the channel
    that carries the owner's critical reminders; getting it *this* way costs a message that visibly failed
    and can be sent again by hand. **Under-sending is recoverable; double-sending is not.**

    **It reads `.phase`, never the error string.** `telegram_http` puts the classification on the
    exception precisely so nobody downstream has to grep a sentence that a later edit may reword.

    **One remaining deliberate under-call.** A 5xx means the request was *received*, and whether it
    was *processed* before the server failed is exactly the ambiguity the send policy refuses to
    gamble on — so it is not re-sent, and `telegram_http` raises it with `phase=None` on purpose. A
    429 does NOT fall into that bucket: "the server refused" is a stronger claim than "we don't
    know", and `telegram_http` tags it `PHASE_REFUSED` at the raise site, so it needs no
    pattern-matching of the message — the raise site already knows the status was 429."""
    if isinstance(exc, TelegramAPIError):
        return SEND_REJECTED
    if isinstance(exc, th.TelegramHTTPError) and exc.phase in (th.PHASE_PRE_DELIVERY, th.PHASE_REFUSED):
        return SEND_UNDELIVERED if exc.phase == th.PHASE_PRE_DELIVERY else SEND_REJECTED
    return SEND_AMBIGUOUS


def load_env(env_file: str | None) -> dict:
    """Start from a KEY=VALUE file (if given); environment variables take precedence."""
    values: dict = {}
    if env_file:
        with open(env_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    for k in ENV_KEYS:
        if os.environ.get(k):
            values[k] = os.environ[k]
    return values


def cfg(creds: dict) -> dict:
    """`parse_mode` stays exactly what it has always been — the literal value handed to the Bot API.
    `format` is the **companion** knob: `markdown` means "convert the assistant's Markdown to
    Telegram HTML first", and it is what decides the parse mode on that path. Keeping them separate is deliberate:
    a reader of an env file can still see the raw API value, and setting `TELEGRAM_FORMAT` back to
    `plain` is the whole rollback."""
    return {
        "token": creds.get("TELEGRAM_BOT_TOKEN"),
        "chat_id": creds.get("TELEGRAM_CHAT_ID"),
        "api_base": creds.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
        "parse_mode": creds.get("TELEGRAM_PARSE_MODE", ""),
        "format": tf.normalize_format(creds.get("TELEGRAM_FORMAT", "")),
    }


def api_call(c: dict, method: str, params: dict, timeout: int = 30) -> dict:
    """POST to the Bot API and return the parsed JSON. Raises on transport / API error.

    **THE ONE HTTP DOOR FOR EVERY OUTBOUND TELEGRAM CALL IN THIS TREE** — `send_text` here, and
    `telegram_ask.py`'s question sends, message edits and `answerCallbackQuery`, which import this
    function rather than opening their own socket. So the retry policy chosen on this line is the
    policy for all of them, which is why it is chosen by METHOD and not by caller.

    **SENDS RETRY ONLY ON PROVABLY-PRE-DELIVERY FAILURES, AND THAT IS NOT TIMIDITY.** `sendMessage`
    has no idempotency key: a connection reset can land after Telegram accepted the message and
    before the response came back, so a blind retry pushes the owner the same nudge twice — on the
    channel carrying their critical reminders. `telegram_http` distinguishes the two by *phase*
    rather than by exception type (a failure during `connect()` has written no bytes; anything after
    it might have), and `UNSAFE` retries the first and refuses the second. **Under-sending is
    recoverable; double-sending is not.** A later edit that "helpfully" widens this to retry
    ambiguous failures is the bug, not an improvement — see `telegram_http.py`'s docstring.

    Reads (`IDEMPOTENT_METHODS`) have no such hazard and retry the full transient set.

    A 429 retries on both policies and honours Telegram's own `parameters.retry_after`: it means the
    request was *refused*, so nothing was delivered and a retry cannot duplicate anything.

    **WHAT IT RAISES IS PART OF ITS CONTRACT.** An `ok: false` answer is a :class:`TelegramAPIError`
    — Telegram spoke and refused — while a transport failure arrives as `telegram_http`'s
    `TelegramHTTPError` carrying its `.phase`, and a response body we cannot parse at all stays a
    bare `RuntimeError`. :func:`classify_send_failure` reads exactly that distinction to decide
    whether the words may be sent again, so **a new failure mode here must pick its exception type
    deliberately**: bare `RuntimeError` means *"we don't know"* and will not be re-sent."""
    if not c["token"]:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN (set in env or --env-file)")
    url = f"{c['api_base']}/bot{c['token']}/{method}"
    policy = th.IDEMPOTENT if method in IDEMPOTENT_METHODS else th.UNSAFE
    res = th.post_form(url, {k: v for k, v in params.items() if v is not None},
                       timeout=timeout, policy=policy)
    try:
        # Telegram returns JSON error bodies even on 4xx, so the status is not the interesting part —
        # `ok`/`description` below is. A body we cannot parse at all is a proxy or edge page, never
        # the Bot API, and that is the one case where the status is all we have to report.
        payload = json.loads(res.body.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"HTTP {res.status} from Telegram {method}") from e
    if not payload.get("ok"):
        raise TelegramAPIError(f"Telegram {method} failed: {payload.get('description', payload)}",
                               method=method, description=payload.get("description"),
                               payload=payload)
    return payload


def send_photo(c: dict, photo_path: str, caption: str | None = None, api=None,
               message_thread_id: int | None = None, timeout: int = 60) -> dict:
    """Send a photo to the owner's chat — stdlib multipart POST to `sendPhoto` via
    `telegram_http.request` directly, since `api_call`/`post_form` are form-urlencoded and have no
    multipart door.

    For a one-shot evidence photo — a confirmation ask that should carry the picture rather than
    describe it. `telegram-send-document-spec.md` scoped `sendPhoto` as deliberately narrow, and it
    stays exactly as narrow as the spec's own `sendDocument`: no ack gate, no dedupe, no chunking,
    because a one-shot evidence photo has none of the duplicate-reminder hazards those exist for.
    Same UNSAFE (pre-delivery-only-retry) transport policy as every other send in this module —
    **under-sending is recoverable; double-sending is not.**"""
    if not c["token"]:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN (set in env or --env-file)")
    if caption and len(caption) > 1024:
        raise ValueError(f"caption is {len(caption)} chars, over Telegram's 1024-char cap")
    with open(photo_path, "rb") as fh:
        data = fh.read()
    fields = {"chat_id": c["chat_id"], "caption": caption, "message_thread_id": message_thread_id}
    body, content_type = th.build_multipart(fields, "photo", os.path.basename(photo_path), data)
    url = f"{c['api_base']}/bot{c['token']}/sendPhoto"
    request_fn = api or th.request
    res = request_fn(url, method="POST", data=body, headers={"Content-Type": content_type},
                     timeout=timeout, policy=th.UNSAFE)
    try:
        payload = json.loads(res.body.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"HTTP {res.status} from Telegram sendPhoto") from e
    if not payload.get("ok"):
        raise TelegramAPIError(f"Telegram sendPhoto failed: {payload.get('description', payload)}",
                               method="sendPhoto", description=payload.get("description"),
                               payload=payload)
    return payload


def send_document(c: dict, document_path: str, caption: str | None = None, api=None,
                  message_thread_id: int | None = None, timeout: int = 60) -> dict:
    """Send a document to the owner's chat — stdlib multipart POST to `sendDocument`, mirroring
    `send_photo` exactly: same `telegram_http.build_multipart` door (`api_call`/`post_form` are
    form-urlencoded and have no multipart door), same `th.UNSAFE` (pre-delivery-only-retry)
    transport policy, no ack gate, no dedupe, no chunking — a one-shot file has none of the
    duplicate-reminder hazards those exist for.

    The outbound-attachment door, `proton_send.py --attach`'s sibling: content too large or too
    structured to inline into a message body goes as a file instead. Size is checked via
    `os.path.getsize` **before** the file is read into memory, against :data:`MAX_DOCUMENT_BYTES` —
    the Bot API's own upload ceiling for this method — so an oversized file is refused locally
    rather than mid-upload."""
    if not c["token"]:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN (set in env or --env-file)")
    if caption and len(caption) > 1024:
        raise ValueError(f"caption is {len(caption)} chars, over Telegram's 1024-char cap")
    size = os.path.getsize(document_path)
    if size > MAX_DOCUMENT_BYTES:
        raise ValueError(f"document is {size} bytes, over the Bot API's {MAX_DOCUMENT_BYTES}-byte "
                         f"(50 MB) cap for sendDocument")
    with open(document_path, "rb") as fh:
        data = fh.read()
    fields = {"chat_id": c["chat_id"], "caption": caption, "message_thread_id": message_thread_id}
    body, content_type = th.build_multipart(fields, "document", os.path.basename(document_path), data)
    url = f"{c['api_base']}/bot{c['token']}/sendDocument"
    request_fn = api or th.request
    res = request_fn(url, method="POST", data=body, headers={"Content-Type": content_type},
                     timeout=timeout, policy=th.UNSAFE)
    try:
        payload = json.loads(res.body.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"HTTP {res.status} from Telegram sendDocument") from e
    if not payload.get("ok"):
        raise TelegramAPIError(f"Telegram sendDocument failed: {payload.get('description', payload)}",
                               method="sendDocument", description=payload.get("description"),
                               payload=payload)
    return payload


def ack_gate_enabled(args, env: dict | None = None) -> bool:
    """Does the ack gate bind on this invocation? `--no-ack-gate` always wins; `--ack-gate` forces it
    on; otherwise it's the surface (`SENESCHAL_SESSION_SOURCE` in :data:`ACK_GATE_SOURCES`) that decides —
    which is what makes this bind on the Watch peek *without* the peek's model having to cooperate."""
    if getattr(args, "no_ack_gate", False):
        return False
    if getattr(args, "ack_gate", False):
        return True
    env = os.environ if env is None else env
    return (env.get(SOURCE_ENV_VAR) or "").strip() in ACK_GATE_SOURCES


def dedupe_enforced(env: dict | None = None) -> bool:
    """Does the dedupe verdict actually BLOCK a send, or only get logged?

    Default OFF (report-only): `ack_gate_check` always computes and logs
    `reminders_acks.watch_duplicate_blocked`'s verdict, but a truthy `WATCH_DEDUPE_ENFORCE` is what
    turns `would_block` into an actual refusal. No CLI flag — this is a host/deploy-time setting, not a
    per-call one, so `os.environ` (or an injected mapping, for tests) is the only door."""
    env = os.environ if env is None else env
    return (env.get(WATCH_DEDUPE_ENFORCE_ENV_VAR) or "").strip().lower() in _TRUE_ENV_VALUES


def reconcile_enforced(env: dict | None = None) -> bool:
    """Does the thread-reconciliation verdict actually BLOCK a send, or only get logged?

    Default OFF (report-only), same shape as :func:`dedupe_enforced`: `ack_gate_check` always
    computes and logs `watch_reconcile.classify`'s verdict, but a truthy `WATCH_RECONCILE_ENFORCE` is
    what turns a SUPERSEDED verdict into an actual refusal. No CLI flag — host/deploy-time setting."""
    env = os.environ if env is None else env
    return (env.get(WATCH_RECONCILE_ENFORCE_ENV_VAR) or "").strip().lower() in _TRUE_ENV_VALUES


def suppression_check(text: str) -> dict | None:
    """``None`` = nothing on the suppression list names this message. A dict = it does.

    The whole judgment is the optional `watch_suppress` module's `match` (see that module for the
    list's shape, the literal-substring rule and why 🚨/🛑/`Call Me` are unreachable from it). Lazy
    import, and an import that fails answers ``None`` — a list that cannot load may never be the
    reason the owner isn't told something.

    **This does NOT touch the reminder path.** Reminders fire from the sentinel's own door, which
    never calls this function; nothing here is to be "extended" into it."""
    try:
        if SCRIPT_DIR not in sys.path:
            sys.path.insert(0, SCRIPT_DIR)  # sibling import also works when we're imported, not run
        import watch_suppress as ws
    except Exception:  # noqa: BLE001 — fail-open
        return None
    return ws.match(text)


def ack_gate_check(text: str, args, now=None) -> dict | None:
    """``None`` = send. A dict = refuse — **for one of five independent reasons**, and the returned
    verdict's `reason` says which:

      * `watch_suppression_list` — the message names a topic the owner has asked never to be raised
        (:func:`suppression_check`). A standing instruction. Checked first, because it reads no state
        and cannot fail slowly.
      * `reminder_acked_today` — the push chases a ⏰ row already acked today
        (`reminders_acks.watch_escalation_blocked`, the same module the fire path gates on).
      * `watch-acked:<ts>` — the owner told the assistant this exact fact is handled
        (`watch_ack.ack_blocks`, keyed on `reminders_acks.fact_key`). **ENFORCES UNCONDITIONALLY** —
        no report-only flag, unlike dedupe below: an ack is the owner's explicit instruction.
      * `duplicate-of:<ts>` — this exact fact was already escalated, un-blocked, inside the dedupe
        window (`reminders_acks.watch_duplicate_blocked`). **Ships REPORT-ONLY**: the verdict is
        always computed and logged, but only actually blocks the send when :func:`dedupe_enforced`
        is true — see `WATCH_DEDUPE_ENFORCE_ENV_VAR`.
      * `superseded-by:<message id/ts>` — the source thread later resolved this exact fact
        (`watch_reconcile.classify`, given `--source-sender`/`--source-subject`/
        `--source-received-at`). **Ships REPORT-ONLY**, the same as dedupe: only a SUPERSEDED
        verdict actually blocks, once :func:`reconcile_enforced` is true.

    **All five are consulted and none replaces another**; the name is historical, the door is one.
    Priority when more than one fires: suppression, then the reminder ack gate, then the runtime ack,
    then dedupe, then thread reconciliation — the first three are unconditional blocking mechanisms,
    dedupe and reconciliation are still building trust report-only. The verdict — whichever it is,
    and `blocked: false` when there is none — is logged **exactly once** per send, alongside a
    `dedupe` sub-object (`fact_key`, `would_block`, `duplicate_of`, `enforced`), a `watch_ack`
    sub-object and a `reconcile` sub-object (`verdict`, `would_block`, `matched`, `enforced`), so the
    log stays one row per Watch send while naming everything that was checked, not only what stopped
    it.

    **THE NO-GATE FALLBACK.** Every module this consults belongs to the optional Watch gate set. If
    `reminders_acks` is unimportable or lacks any of :data:`WATCH_GATE_API`, there is nothing to log
    *with* and no reminder/dedupe predicate to call, so this returns the suppression verdict alone
    (``None`` when `watch_suppress` is absent too) — a suppression found on that path still blocks
    but goes unrecorded. `watch_ack` and `watch_reconcile` are each skipped on their own when absent.
    Every failure mode returns ``None`` for its own gate: an import that can't load or a state dir
    that can't be read must never be the reason the owner isn't told something.

    ``now`` is a **test seam and nothing else**: the instant "today" (and the dedupe/ack windows) is
    computed from. Production passes nothing and the owner's local wall clock is read — there is no
    CLI flag, no env var, and no caller in the tree that sets it. It exists because the gate's verdict
    turns on *what day it is*, so an end-to-end test either injects the instant or bakes a calendar
    date that expires."""
    if not ack_gate_enabled(args):
        return None
    suppressed = suppression_check(text)
    try:
        if SCRIPT_DIR not in sys.path:
            sys.path.insert(0, SCRIPT_DIR)  # sibling import also works when we're imported, not run
        import reminders_acks as ra
    except Exception:  # noqa: BLE001 — fail-open on the ack half; see the docstring on the log
        return suppressed
    if not all(callable(getattr(ra, name, None)) for name in WATCH_GATE_API):
        return suppressed  # the Watch gate set is not installed: the documented no-gate fallback
    state_dir = getattr(args, "state_dir", None) or DEFAULT_STATE_DIR
    reminder_id = getattr(args, "reminder_id", None)
    # The source fields, if the peek passed them (an email-backed escalation) — the identity every
    # gate below keys on comes from these first, the prose second (see the module docstring).
    source_sender = (getattr(args, "source_sender", None) or "").strip() or None
    source_subject = (getattr(args, "source_subject", None) or "").strip() or None
    ack_blocked = ra.watch_escalation_blocked(state_dir, text, reminder_id=reminder_id, now=now)
    try:
        import watch_ack as wa
        watch_ack_blocked = wa.ack_blocks(state_dir, text, now=now,
                                          sender=source_sender, subject=source_subject)
    except Exception:  # noqa: BLE001 — absent or broken, the runtime ack is never why a check fails
        watch_ack_blocked = None
    try:
        dedupe_verdict = ra.watch_duplicate_blocked(state_dir, text, now=now,
                                                    sender=source_sender, subject=source_subject)
    except Exception:  # noqa: BLE001 — dedupe is never the reason a check elsewhere fails
        dedupe_verdict = None
    try:
        gated_key = ra.escalation_fact_key(text, source_sender, source_subject)
    except Exception:  # noqa: BLE001 — identity is never the reason a check elsewhere fails
        gated_key = ra.fact_key(text)
    try:
        import watch_reconcile as wr
        candidate = {"sender": source_sender,
                     "subject": source_subject,
                     "text": text,
                     "received_at": getattr(args, "source_received_at", None)}
        reconcile_verdict = wr.classify(candidate, state_dir=state_dir, now=now)
    except Exception:  # noqa: BLE001 — absent or broken, reconciliation is never why a check fails
        reconcile_verdict = None
    enforce = dedupe_enforced()
    r_enforce = reconcile_enforced()
    reconcile_superseded = bool(reconcile_verdict) and reconcile_verdict.get("verdict") == "SUPERSEDED"
    reconcile_block = reconcile_verdict if (reconcile_superseded and r_enforce) else None
    blocked = (suppressed or ack_blocked or watch_ack_blocked
               or (dedupe_verdict if enforce else None) or reconcile_block)
    ra.log_watch_gate(state_dir, {
        "surface": "telegram",
        "source": (os.environ.get(SOURCE_ENV_VAR) or "").strip() or None,
        "blocked": bool(blocked),
        # WHICH gate, on every row — `null` when nothing blocked. A reader of this log answering
        # "why wasn't the owner told?" may not have to infer it from the shape of `verdict`.
        "reason": blocked.get("reason") if blocked else None,
        "reminder_id": reminder_id,
        "chars": len(text or ""),
        "text": (text or "")[:400],
        # Always recorded regardless of `enforce` — the report the enforce decision reads.
        # `fact_key` is the identity the gates were judged on — a SOURCE key when the peek passed
        # `--source-sender`, else the prose key — and `fact_key_text` is always the prose key, so a
        # reader can tell the two apart and dedupe stays retroactive against either.
        "fact_key": gated_key,
        "fact_key_text": ra.fact_key(text),
        "source_sender": source_sender,
        "source_subject": (source_subject or "")[:200] or None,
        "dedupe": {
            "would_block": bool(dedupe_verdict),
            "duplicate_of": dedupe_verdict.get("duplicate_of") if dedupe_verdict else None,
            "enforced": enforce,
        },
        # Always recorded, even though (unlike dedupe) it has no report-only phase; measurable is
        # not the same thing as optional.
        "watch_ack": {"blocked": bool(watch_ack_blocked),
                      "acked_at": watch_ack_blocked.get("acked_at") if watch_ack_blocked else None},
        # Always recorded regardless of `r_enforce` — same report-only shape as dedupe.
        "reconcile": {
            "verdict": reconcile_verdict.get("verdict") if reconcile_verdict else None,
            "would_block": reconcile_superseded,
            "matched": reconcile_verdict.get("matched") if reconcile_verdict else None,
            "enforced": r_enforce,
        },
        **({"verdict": blocked} if blocked else {}),
    })
    return blocked


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log_format_fallback(state_dir: str, entry: dict) -> None:
    """Record one HTML attempt that did not survive. Fail-open in both directions — a log we cannot
    write must not cost the message, and a message that had to go plain must not be silent, because
    "how often does the converter get rejected?" is the only question that decides whether this
    feature stays on.

    **EVERY ROW SAYS WHETHER ANYTHING WAS RE-SENT: `delivery` + `resent`.** Without it, a duplicated
    reminder sits in this file looking exactly like every healthy fallback. A row with `"resent": false` is a send that STOPPED; a row with
    `"resent": true` is the fallback working as designed. `grep '"resent": false'` is the query."""
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, FORMAT_FALLBACK_LOG), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def send_text(c: dict, text: str, disable_preview: bool = False, api=None,
              state_dir: str | None = None, message_thread_id=None) -> dict:
    """Send one message, chunked, converting Markdown → Telegram HTML when `c["format"]` says so.

    **`message_thread_id` is the private-chat TOPIC seam and it defaults to OFF**
    (`../docs/telegram-capability-map.md` §2.1). It rides the SAME `params()` builder every chunk,
    every parse mode and every fallback rung already goes through — deliberately, because a second
    parameter path is a second place the degradation ladder can forget it, and a reply that lost its
    thread halfway through a three-chunk message would arrive split across two conversations. With
    no topic the key is **absent, not null**, so the payload Telegram receives is byte-identical to
    the one it received before this parameter existed.

    **The fallback, exactly.** A chunk is attempted as HTML only if the plan produced HTML for it.
    If that attempt fails **and :func:`classify_send_failure` can prove the words were not
    delivered** — Telegram answered and refused (a 400 on malformed entities), or the request never
    left the machine — the same chunk is immediately retried **once**, as plain text, with the
    ORIGINAL unconverted source and no parse mode. If that retry also fails, the send has failed and
    says so, exactly as it would have before any of this existed.

    Once a chunk has degraded, every later chunk of the same message goes plain too. A message that
    arrived bold for two screens and literal-asterisks for the third would read as a bug in the
    assistant rather than as a formatting fallback.

    **AN AMBIGUOUS FAILURE STOPS THE SEND. IT DOES NOT FALL BACK, AND IT DOES NOT CONTINUE.** Both
    halves are deliberate:

    * **No fallback**, because the message may already be on the owner's phone. A fallback *"for any
      reason at all"* turns the one failure `telegram_http` specifically refuses to retry into a
      guaranteed duplicate — the HTML landed, and the plain re-send is the second copy.
    * **No remaining chunks either**, which is the decision worth arguing. Chunk *N*'s fate is
      unknown; chunks *N+1…* are provably unsent. Sending them anyway produces the one state worse
      than either honest option: if *N* did not land, the owner reads continuous prose with a
      paragraph silently missing and **nothing marks the hole** — which reads as the assistant being
      wrong, not as the assistant being cut off. A message that visibly stops is legible; a message with an invisible gap
      is not. Composing some "…part 2 of 3 may be missing…" marker on the failure path would be
      inventing a half-sent state on the one code path least able to verify it. And the failure that
      got us here is a *live* transport failure, so the remaining chunks would most likely each fail
      and each add their own ambiguity. So: stop, and make the accounting exact instead.

    **What was delivered is answerable from the logs, which is the price of stopping.** The raised
    :class:`AmbiguousSendError` names chunk *N* of *M* and how many landed before it, `main` puts
    `delivery`/`ambiguous`/`delivered_chunks`/`unsent_chunks` in the result JSON, and one row lands
    in `state/telegram-format-fallback.jsonl` with `"resent": false` — the field that tells it apart
    from a healthy fallback at a glance. In practice this is nearly always M = 1: almost every
    message is a single chunk.

    `api` is the test seam — the real :func:`api_call` by default, so no test reaches the wire
    without saying so."""
    api = api or api_call
    plan = tf.plan_send(text, c.get("format"))
    ids, fallbacks = [], []
    degraded = False

    def params(body: str, mode: str | None) -> dict:
        p = {"chat_id": c["chat_id"], "text": body, "parse_mode": mode or None,
             "disable_web_page_preview": "true" if disable_preview else None}
        # ABSENT, not null. `api_call` drops a None anyway, but the seam a test can observe is this
        # dict — so omitting the key is what makes "topics off is byte-identical" an assertion about
        # the payload rather than a claim about a filter two functions away. Same shape, same
        # reasoning, as `telegram_ask.ask`'s `send_params`.
        if message_thread_id is not None:
            p["message_thread_id"] = message_thread_id
        return p

    def note(index: int, piece: dict, error: str, delivery: str, resent: bool) -> None:
        if not state_dir:
            return
        log_format_fallback(state_dir, {
            "at": _stamp(), "chunk": index, "chunks": len(plan),
            # `delivery` is what we could prove about the failure; `resent` is what we did about it.
            # Two facts, kept separate, because a future policy change moves the second and not the
            # first — and because the interesting query is on `resent`.
            "delivery": delivery, "resent": resent, "delivered_chunks": len(ids),
            "error": error, "chars": len(piece["source"]),
            "text": piece["source"][:400],
        })

    for index, piece in enumerate(plan):
        if piece["html"] is not None and not degraded:
            try:
                res = api(c, "sendMessage", params(piece["html"], "HTML"))
                ids.append((res.get("result") or {}).get("message_id"))
                continue
            except Exception as e:  # noqa: BLE001 — classified, then acted on; never blindly re-sent
                delivery = classify_send_failure(e)
                if delivery == SEND_AMBIGUOUS:
                    unsent = len(plan) - index - 1
                    note(index, piece, str(e), delivery, resent=False)
                    print(f"! telegram chunk {index + 1}/{len(plan)} failed AFTER the request went "
                          f"out; NOT re-sending (a blind retry here duplicates the message): {e}",
                          file=sys.stderr)
                    raise AmbiguousSendError(
                        f"chunk {index + 1}/{len(plan)} failed after the request was sent, so "
                        f"Telegram may already have delivered it; NOT re-sent and the remaining "
                        f"{unsent} chunk(s) were NOT attempted. {len(ids)} chunk(s) confirmed "
                        f"delivered. Cause: {e}",
                        chunk=index, chunks=len(plan), delivered=len(ids),
                        message_ids=[m for m in ids if m is not None], cause=e) from e
                degraded = True
                fallbacks.append({"chunk": index, "delivery": delivery, "error": str(e)})
                print(f"! telegram HTML rejected on chunk {index + 1}/{len(plan)} ({delivery}); "
                      f"re-sending as plain text: {e}", file=sys.stderr)
                note(index, piece, str(e), delivery, resent=True)
        # Plain: the default path, and the fallback path. Either way this is the ORIGINAL source
        # text — never a partially-converted string, which is the one thing worse than both.
        res = api(c, "sendMessage", params(piece["source"], None if degraded else c["parse_mode"]))
        ids.append((res.get("result") or {}).get("message_id"))

    return {"message_ids": ids, "chunks": len(plan), "degraded": degraded, "fallbacks": fallbacks,
            "format": tf.normalize_format(c.get("format"))}


def main(now=None) -> int:
    """The CLI entrypoint. ``now`` is the :func:`ack_gate_check` test seam threaded through — the
    console path (``sys.exit(main())``) never passes it, so argv, behaviour and exit codes are
    byte-identical to before it existed. It is here only so a test can drive the *real* `main` end to
    end — argv parsing, `SENESCHAL_SESSION_SOURCE` arming, the gate, the log line, the exit code — without
    its verdict depending on what day the suite happens to run."""
    p = argparse.ArgumentParser(description="Send a Telegram message as the assistant's bot.")
    p.add_argument("--text", help="message text")
    p.add_argument("--text-file", help="read message text from a file")
    p.add_argument("--photo", default=None,
                   help="send a photo (sendPhoto) instead of text; mutually exclusive with "
                        "--text/--text-file, no ack gate/dedupe/chunking (see send_photo docstring)")
    p.add_argument("--document", default=None,
                   help="send a document (sendDocument) instead of text; mutually exclusive with "
                        "--text/--text-file, no ack gate/dedupe/chunking (see send_document "
                        "docstring). Max 50 MB — the Bot API's own upload cap for sendDocument.")
    p.add_argument("--caption", default=None,
                   help="optional caption for --photo/--document (max 1024 chars)")
    p.add_argument("--chat-id", help="recipient chat id (default TELEGRAM_CHAT_ID)")
    p.add_argument("--parse-mode", help='"MarkdownV2" | "HTML" | "" (default TELEGRAM_PARSE_MODE)')
    p.add_argument("--format", dest="fmt", choices=[tf.FORMAT_PLAIN, tf.FORMAT_MARKDOWN],
                   default=None,
                   help='"markdown" converts the assistant\'s Markdown to Telegram HTML at the send '
                        'boundary and falls back to plain text if Telegram rejects it; "plain" is '
                        "today's behaviour (default TELEGRAM_FORMAT, itself defaulting to plain)")
    p.add_argument("--disable-preview", action="store_true", help="disable link previews")
    p.add_argument("--message-thread-id", type=int, default=None, metavar="N",
                   help="send into private-chat TOPIC N (Bot API 9.3) instead of the main chat. "
                        "Omit it and the payload is exactly what it was before topics existed. "
                        "A topic Telegram refuses is the CALLER's to fall back from — this flag "
                        "does one thing and reports what happened.")
    p.add_argument("--topic", default=None, metavar="PURPOSE",
                   help="resolve a private-chat TOPIC by PURPOSE (telegram_topics.py) and send into "
                        "it — `reminders`, `decisions`, `pull-requests`. ADVISORY IN EVERY "
                        "DIRECTION: topics off, an unreachable getMe, an unknown purpose, a corrupt "
                        "state file or a failed creation all mean the main chat, and the message "
                        "still goes. An explicit --message-thread-id WINS and skips resolution "
                        "entirely. Deliberately no argparse `choices`: a bad purpose must cost the "
                        "thread, never the message.")
    p.add_argument("--env-file", help="KEY=VALUE file with TELEGRAM_* settings (kept untracked)")
    p.add_argument("--dry-run", action="store_true", help="build the request and print a summary; no network")
    p.add_argument("--check-auth", action="store_true", help="call getMe only (verify token); do not send")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="seneschal/state (ack gate + gate log)")
    p.add_argument("--reminder-id", default=None, metavar="KEY",
                   help="the ⏰ row this message is about — makes the ack gate exact instead of "
                        "title-matched. Dash-insensitive.")
    p.add_argument("--ack-gate", action="store_true",
                   help="force the ack gate on (default: on for SENESCHAL_SESSION_SOURCE=watch). A message "
                        "chasing a reminder acked today is refused with exit 3 instead of sent.")
    p.add_argument("--no-ack-gate", action="store_true", help="force the ack gate off; always wins")
    p.add_argument("--source-sender", default=None, metavar="ADDR",
                   help="thread reconciliation + source identity: the escalation's source sender "
                        "(email address). Omit it and reconciliation reads UNKNOWN without touching "
                        "a door — the unchanged default.")
    p.add_argument("--source-subject", default=None, metavar="TEXT",
                   help="the source message's subject line, if the peek has one.")
    p.add_argument("--source-received-at", default=None, metavar="ISO8601",
                   help="when the source message was received (ISO 8601). Required "
                        "alongside --source-sender for reconciliation to run at all.")
    args = p.parse_args()

    creds = load_env(args.env_file)
    c = cfg(creds)
    if args.chat_id:
        c["chat_id"] = args.chat_id
    if args.parse_mode is not None:
        c["parse_mode"] = args.parse_mode
    if args.fmt is not None:
        c["format"] = tf.normalize_format(args.fmt)

    if args.check_auth:
        try:
            me = api_call(c, "getMe", {})
            u = me.get("result", {})
            print(json.dumps({"ok": True, "check_auth": "token OK", "bot": u.get("username"), "id": u.get("id")}))
            return 0
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1

    if args.photo and args.document:
        print(json.dumps({"ok": False, "error": "--photo and --document are mutually exclusive"}))
        return 2

    if args.photo:
        if args.text or args.text_file:
            print(json.dumps({"ok": False, "error": "--photo is mutually exclusive with --text/--text-file"}))
            return 2
        if not c["chat_id"]:
            print(json.dumps({"ok": False, "error": "no chat id (set TELEGRAM_CHAT_ID or pass --chat-id)"}))
            return 2
        if args.caption and len(args.caption) > 1024:
            print(json.dumps({"ok": False, "error": f"caption is {len(args.caption)} chars, over "
                              f"Telegram's 1024-char cap"}))
            return 2
        if args.dry_run:
            print(json.dumps({"ok": True, "dry_run": True, "chat_id": c["chat_id"],
                              "photo": args.photo, "caption": args.caption,
                              "message_thread_id": args.message_thread_id}))
            return 0
        try:
            result = send_photo(c, args.photo, caption=args.caption,
                                message_thread_id=args.message_thread_id)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
        print(json.dumps({"ok": True, "sent": True,
                          "message_id": (result.get("result") or {}).get("message_id")}))
        return 0

    if args.document:
        if args.text or args.text_file:
            print(json.dumps({"ok": False, "error": "--document is mutually exclusive with --text/--text-file"}))
            return 2
        if not c["chat_id"]:
            print(json.dumps({"ok": False, "error": "no chat id (set TELEGRAM_CHAT_ID or pass --chat-id)"}))
            return 2
        if args.caption and len(args.caption) > 1024:
            print(json.dumps({"ok": False, "error": f"caption is {len(args.caption)} chars, over "
                              f"Telegram's 1024-char cap"}))
            return 2
        if args.dry_run:
            print(json.dumps({"ok": True, "dry_run": True, "chat_id": c["chat_id"],
                              "document": args.document, "caption": args.caption,
                              "message_thread_id": args.message_thread_id}))
            return 0
        try:
            result = send_document(c, args.document, caption=args.caption,
                                   message_thread_id=args.message_thread_id)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(e)}))
            return 1
        print(json.dumps({"ok": True, "sent": True,
                          "message_id": (result.get("result") or {}).get("message_id")}))
        return 0

    text = args.text
    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as fh:
            text = fh.read()
    if not text:
        print(json.dumps({"ok": False, "error": "no --text or --text-file provided"}))
        return 2
    if not c["chat_id"]:
        print(json.dumps({"ok": False, "error": "no chat id (set TELEGRAM_CHAT_ID or pass --chat-id)"}))
        return 2

    # The ack gate, BEFORE the dry-run branch: a dry run should report the same verdict a real send
    # would get, and a blocked message is blocked whether or not we were going to touch the network.
    blocked = ack_gate_check(text, args, now=now)
    if blocked:
        # Five gates, five shapes of result, one exit code. The suppression verdict carries a
        # `pattern`; the dedupe verdict's `reason` is the only one shaped `duplicate-of:<ts>`; the
        # runtime-ack verdict is the only one shaped `watch-acked:<ts>`; the reconciliation verdict is
        # the only one shaped `superseded-by:<...>`; the reminder-ack verdict is whatever is left —
        # which is what tells the five apart without importing any of their modules' constants here.
        reason = blocked.get("reason") or ""
        if blocked.get("pattern"):
            out = {
                "ok": False, "sent": False, "suppressed": blocked.get("reason"),
                "suppression": {k: blocked.get(k) for k in ("pattern", "why", "added")},
                "note": "a topic the owner asked never to be raised — not sent "
                        "(the Watch suppression list, via watch_suppress.py)",
            }
        elif reason.startswith("duplicate-of:"):
            out = {
                "ok": False, "sent": False, "suppressed": blocked.get("reason"),
                "duplicate": {k: blocked.get(k) for k in ("fact_key", "duplicate_of", "window_hours")},
                "note": "already escalated this fact recently — not sent "
                        "(seneschal/scripts/reminders_acks.py, the Watch dedupe gate; "
                        "set WATCH_DEDUPE_ENFORCE=1 to enable — report-only by default)",
            }
        elif reason.startswith("watch-acked:"):
            out = {
                "ok": False, "sent": False, "suppressed": blocked.get("reason"),
                "watch_ack": {k: blocked.get(k) for k in
                             ("fact_key", "acked_at", "expires_at", "source", "text_as_said")},
                "note": "the owner already said this is handled — not sent "
                        "(watch_ack.py, the Watch runtime ack)",
            }
        elif reason.startswith("superseded-by:"):
            out = {
                "ok": False, "sent": False, "suppressed": blocked.get("reason"),
                "reconcile": {k: blocked.get(k) for k in ("verdict", "fact_key", "matched", "door")},
                "note": "the source thread later resolved this fact — not sent "
                        "(watch_reconcile.py, thread reconciliation; "
                        "set WATCH_RECONCILE_ENFORCE=1 to enable — report-only by default)",
            }
        else:
            out = {
                "ok": False, "sent": False, "suppressed": blocked.get("reason"),
                "reminder": {k: blocked.get(k) for k in
                             ("key", "date", "sources", "matched_by", "matched_title", "coverage")},
                "note": "already acked today — not sent (seneschal/scripts/reminders_acks.py, the Watch ack gate)",
            }
        print(json.dumps(out, ensure_ascii=False))
        return 3

    if args.dry_run:
        plan = tf.plan_send(text, c.get("format"))
        out = {
            "ok": True, "dry_run": True, "chat_id": c["chat_id"],
            "parse_mode": c["parse_mode"], "format": tf.normalize_format(c.get("format")),
            "chars": len(text), "chunks": len(plan),
        }
        if args.message_thread_id is not None:
            out["message_thread_id"] = args.message_thread_id
        if args.topic:
            # NAMED, never RESOLVED. Resolving a purpose can CREATE a topic, which is exactly what
            # `--dry-run` promises not to do — `telegram_ask._cmd_ask` draws the same line.
            out["topic"] = args.topic
        if any(p["html"] is not None for p in plan):
            # The rendered HTML is the whole reason to dry-run this: it is the only way to see what
            # Telegram will be handed without messaging the owner to find out.
            out["html"] = [p["html"] for p in plan]
        print(json.dumps(out, ensure_ascii=False))
        return 0

    # WHICH THREAD, resolved before the send and never allowed to prevent one — the same guard
    # `telegram_ask._cmd_ask` applies, for the same reason and with the same contract:
    # `thread_id` never raises and answers `None`, the main chat, for every failure there is.
    #
    # **AN EXPLICIT `--message-thread-id` WINS AND SKIPS RESOLUTION ENTIRELY.** A caller that
    # already knows the thread is not asking a question, and resolving anyway would spend a `getMe`
    # and could CREATE a topic on the one path that named none.
    #
    # `telegram_topics` is imported HERE rather than at module scope, exactly as `merge_guard`
    # imports it: an import that fails must cost the topic and not the message, and this file is
    # the door every reminder in the tree goes out through.
    thread, purpose = args.message_thread_id, None
    if thread is None and args.topic:
        try:
            import telegram_topics as tt  # noqa: PLC0415 — lazy on purpose; see above
            purpose = args.topic
            thread = tt.thread_id(c, purpose, args.state_dir,
                                  log=lambda m: print(m, file=sys.stderr))
        except Exception as e:  # noqa: BLE001 — the main chat is a complete, correct answer
            print(f"! telegram_topics unavailable ({e}); this message goes to the main chat",
                  file=sys.stderr)
            thread, purpose = None, None

    thread_fallback = False
    try:
        try:
            res = send_text(c, text, disable_preview=args.disable_preview, state_dir=args.state_dir,
                            message_thread_id=thread)
        except Exception as e:  # noqa: BLE001 — classified, then acted on; never blindly re-sent
            # **A THREAD THAT NO LONGER EXISTS MUST NOT COST THE MESSAGE.** The owner deleted the
            # topic, or the bot was re-pointed: Telegram *refuses* the send, so nothing was delivered and
            # re-sending into the main chat cannot duplicate anything. `telegram_ask.ask`'s rule,
            # and only `SEND_REJECTED` qualifies — AMBIGUOUS means the request went out and the
            # words may already be on the owner's phone, UNDELIVERED is a transport failure the thread had
            # nothing to do with, and dressing that up as a stale topic would hide an outage.
            #
            # **AND ONLY WHEN THE WHOLE MESSAGE IS ONE CHUNK.** `send_text` re-raises the original
            # exception on a rejected chunk, which does not say WHICH chunk, so on a multi-chunk
            # send this frame cannot prove nothing landed — and re-sending a message whose first
            # chunk arrived is the duplicate every other rule in this file exists to prevent. A
            # thread-caused rejection fails on chunk 1 in practice (a thread good enough for chunk 1
            # is good enough for chunk 2), so the bound costs nothing real and is provable rather
            # than argued. `plan_send` is pure and touches no network.
            # **`purpose`, NOT `thread`.** An explicit `--message-thread-id` keeps its own
            # contract — the caller named a raw id and owns whether the words are worth re-sending,
            # because only that caller knows what they are. This frame may only speak for a thread
            # IT resolved, which is exactly the case where it also knows the purpose to forget.
            if purpose is None or classify_send_failure(e) != SEND_REJECTED:
                raise
            if len(tf.plan_send(text, c.get("format"))) != 1:
                print(f"! telegram refused thread {thread} on a multi-chunk message ({e}); NOT "
                      f"re-sending to the main chat — some chunks may already have landed",
                      file=sys.stderr)
                raise
            print(f"! telegram refused thread {thread} ({e}); re-sending to the main chat. The "
                  f"stored topic id is stale.", file=sys.stderr)
            thread_fallback, thread = True, None
            res = send_text(c, text, disable_preview=args.disable_preview,
                            state_dir=args.state_dir, message_thread_id=None)
        if thread_fallback and purpose:
            # The message LANDED, in the main chat, and the stored id is stale. Forget it so the
            # next send creates a fresh topic instead of paying a rejected call every time.
            # Best-effort: a failed forget costs one refused call per message, never a message.
            try:
                import telegram_topics as tt  # noqa: PLC0415 — already imported above; cheap
                tt.forget(args.state_dir, purpose)
            except Exception as e:  # noqa: BLE001
                print(f"! could not forget the stale topic {purpose!r} ({e})", file=sys.stderr)
        # `message_id` stays the single-value field every existing caller reads (the reaction map
        # keys on it). With one chunk — every reminder, every question, nearly every reply — it is
        # exactly what it was before. With several, it is the LAST, which is the message sitting at
        # the bottom of the owner's screen and therefore the one a 👍 lands on.
        ids = [m for m in res["message_ids"] if m is not None]
        print(json.dumps({"ok": True, "sent": True, "chat_id": c["chat_id"],
                          "message_id": ids[-1] if ids else None,
                          # Additive and only when a topic was actually in play, so a send with
                          # topics off returns exactly the object it returned before. It reports
                          # the thread the message ACTUALLY went to — so a fallback reports no
                          # thread, which is the truth, and `thread_fallback` is what says a topic
                          # had been asked for.
                          **({"message_thread_id": thread} if thread is not None else {}),
                          **({"thread_fallback": True} if thread_fallback else {}),
                          **({"message_ids": ids} if len(ids) > 1 else {}),
                          **({"chunks": res["chunks"]} if res["chunks"] > 1 else {}),
                          **({"degraded": True, "fallbacks": res["fallbacks"]}
                             if res["degraded"] else {})}, ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        # **The failure result now says whether the caller may try again.** Every subprocess caller
        # in the tree reads this JSON and some of them retry on `ok: false` — which is the same
        # duplicate one layer up, so the answer has to travel with the failure rather than be
        # guessed from the error text.
        delivery = classify_send_failure(e)
        out = {"ok": False, "error": str(e), "chat_id": c["chat_id"], "delivery": delivery}
        if isinstance(e, AmbiguousSendError):
            out.update({"chunks": e.chunks, "delivered_chunks": e.delivered,
                        "unsent_chunks": e.chunks - e.delivered - 1,
                        **({"message_ids": e.message_ids} if e.message_ids else {})})
        if delivery == SEND_AMBIGUOUS:
            out["ambiguous"] = True
            out["note"] = ("the request went out and Telegram may already have delivered it — do "
                           "NOT automatically re-send this text; a blind retry is the duplicate")
        print(json.dumps(out, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
