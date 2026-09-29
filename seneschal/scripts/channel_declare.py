#!/usr/bin/env python3
"""**A channel declaration, forced onto every Telegram reply.**
`../docs/message-routing-spec.md` is the contract; this module is its §1-§4 machinery plus the
outcome log (§7). Read the spec before touching this file — the regex, the retry bound and the
fuzzy-match cutoff are each a specific decision, not a convenience.

## Why it exists

A reply that belongs in a named Telegram topic (a status brief for a project, say) otherwise lands
in the main chat: the mechanism to route it exists, but nothing on the reply path ever asks the
assistant to reach for it. This is that ask, made structural: **presence, never validity** — a reply
may not leave the daemon without having named a destination, but naming the WRONG one costs nothing
beyond a fuzzy match or a fall back to `main`, which is where an undeclared reply already goes.

## What this module does, and what the daemon does with it

This module parses, strips, retries-prompts and resolves; it also logs every outcome to
`state/channel-declare-log.jsonl`. The routing decision itself lives in the daemon's drainer, which
passes the resolved purpose to its delivery call so it WINS over the inbound thread on a Telegram
turn. The log is what justified acting on the declaration rather than only observing it: a
declaration that is reliably present (and reliably rescued by the one retry when missing) is safe to
route on.

## The one interception point

`extract_channel_declaration` is called exactly once, in the drainer, immediately after a reply is
known to be real, NON-EMPTY text (model output or a synthesized apology/timeout string — the latter
never match, which is correct: those are the daemon's own words, not something the owner is owed a
channel choice about). **An empty or whitespace-only reply never reaches this function at all** —
the drainer intercepts it first (`reply-marker-forcing-function-spec.md` §9) and sends nothing,
because `extract_channel_declaration("")` / `has_reply_marker("")` both read an empty string as
missing BOTH required lines, and spending the bounded retry recovering two control lines for a reply
with no substance behind them would deliver a bare marker and nothing else. The empty-reply turn
writes NO row to this module's log either: every value `record_outcome` could log for it would
misstate a routing question that turn never asked.

The **stripped** text it returns is what flows into every consumer — delivery, the warm thread, the
turn capture, the Mouth's assertions log and the cockpit tee's reply preview. Stripping is
**unconditional on every channel**, so a stray `[[channel:…]]`-shaped string can never leak (§1);
the bounded retry escalation is Telegram-only (§3, §7 scope), because there is no routing decision to
retry for on Discord or the cockpit.

## The regex diverges from the spec's literal text, on purpose, in one place

§1 gives `^\\[\\[channel:\\s*([a-z0-9-]+)\\s*\\]\\]\\s*\\n` — a capture group restricted to lowercase.
Taken literally, a model that echoes the display title it saw (`Projects`, capitalized — the exact
case §4's own hand-check flags as a real trap) would not match this regex AT ALL: the whole line
would fail to parse as a declaration, `reply` would come back UNCHANGED, and the raw
`[[channel:Projects]]` line would reach the owner — the one failure this feature is most paranoid
about. §1 also says extraction "returns the **lower-cased** purpose group", which only does real work
if the group can be non-lowercase going in. So the capture group here is widened to `[A-Za-z0-9-]+`
and lower-cased on the way out, which makes §4's `.strip().lower()` defence in `resolve_purpose` a
second line of the same belt rather than the only one. Nothing else about the regex — the anchor, the
literal `[[channel:`, position-0-only, the exact-one-trailing-newline strip — differs from the spec.

## The reply-marker line — a second required line, generalizing this same machine

`../docs/reply-marker-forcing-function-spec.md` is its own contract; this module hosts its mechanism
too, reusing the SAME bounded retry (`CHANNEL_DECLARE_MAX_RETRIES`) rather than a second, independent
one — a reply missing BOTH required opening lines gets ONE bounce naming both, never two round trips.
Two differences from the channel line, both load-bearing. **THE ANSWERING LINE IS NOT STRIPPED** —
`[[channel:PURPOSE]]` is parsed and removed before delivery because it is plumbing; `*(answering
...)*` is left in the delivered text because it is written FOR the owner, never for the daemon. **A
corrective retry's own TEXT can therefore reach delivery** — the one case where that happens anywhere
in this mechanism — prepended onto the already-obtained substantive reply when the original omitted
it; the channel line never needed this because its corrective was always fully discardable (only the
parsed purpose ever mattered). `record_outcome` logs `reply_marker_required`/`reply_marker_present`/
`reply_marker_retries_used`, the last counted SEPARATELY from the channel's own `retries_used` — a
retry spent solely on the marker must never be misread as a channel-declaration rescue.

The owner-facing prompt text (the grounding paragraph and the retry prompts) names the owner from
`persona/identity.json` via `identity_common.owner_name` — "the owner" when unconfigured — and the
topic list is read fresh from the live routing table on every call, never pinned in a string.

## The one other independent stripper

`cockpit_pipe.build_chat_event_from_stream` strips the SAME `[[channel:...]]` marker a SECOND time,
independently of this module's own extraction — not a redundant safeguard but a structural necessity:
the trace panel's `reply_preview` is built from the raw stream `result` event, which lands BEFORE the
drainer's own `extract_channel_declaration` call ever runs on that same reply. Two strippers, one
regex, because the two consumers see the text at two different points in the pipeline.

## The retry loop (in the daemon, not here)

The bounded retry loop itself lives in the drainer and is gated on a Telegram turn with a live warm
session — a session already dead on the substantive turn gets no corrective question and falls
straight through to `declared is None`, which `resolve_purpose` turns into `main`. A dead corrective
ask never costs the substantive reply: the reply is already in hand before the retry loop runs.

## Duplicate markers on a corrective retry

The retry loop PREPENDS a recovered marker line onto the reply, which is right in general — the
substantive answer must never be re-generated — but the original reply's own malformed attempt at the
two lines may be buried mid-message: narration before them is exactly what makes the position-0
checks call them "missing" in the first place. A naive prepend would stack a SECOND marker beside the
orphaned original, and the original's stray `[[channel:...]]` line — never stripped, because it was
never at position 0 — would ride along unstripped too.

`strip_stray_control_lines`, called by the retry path immediately before that one prepend (never
elsewhere — the position-0-only design stays exactly as deliberate as the section above says for
every other path), removes a standalone `[[channel:...]]` or `*(answering ...)*` line found OUTSIDE a
fenced code block, anywhere in the text. Fence-aware on purpose: the assistant quoting the syntax
while explaining the feature survives untouched, because that quoting happens inside a code fence in
practice. A line is dropped only when it is EXACTLY a control line with nothing else on it.

Tests: `test_channel_declare.py` exercises this module in isolation.
"""
from __future__ import annotations

import difflib
import os
import re
from datetime import datetime, timezone

import stateio
import telegram_topics

CHANNEL_DECLARE_LOG_FILENAME = "channel-declare-log.jsonl"

#: Position-0-only, matched with `re.match` (which always starts at index 0) plus a leading `^` as a
#: belt-and-braces second anchor — a `[[channel:…]]`-shaped string mid-message, in a code fence, or in
#: a quoted example (the assistant explaining this very feature) is ordinary text and is never touched. See the
#: module docstring for why the capture group is `[A-Za-z0-9-]+` rather than the spec's literal
#: lowercase-only class.
CHANNEL_DECLARATION_RE = re.compile(r"^\[\[channel:\s*([A-Za-z0-9-]+)\s*\]\]\s*\n")

#: At least 80% similarity (difflib ratio) before a misspelled declaration resolves to a real topic;
#: anything below falls to `main`, which is where an undeclared reply lands anyway.
CHANNEL_FUZZY_CUTOFF = 0.8

#: Overturnable (message-routing-spec.md §8 fork 1). One extra round trip is cheap and converges
#: fast; the outcome log is exactly the data that would justify a different number.
CHANNEL_DECLARE_MAX_RETRIES = 1

#: §2. `{choices}` is filled FRESH from the live routing table on every call (:func:`retry_prompt`),
#: never pinned in this string, so a topic added to the owner's `telegram-topics.json` (or minted at
#: runtime) is named in the very next corrective ask. The names are a hint for the model, not
#: authoritative — resolution still goes through `resolve_purpose`.
CHANNEL_RETRY_PROMPT_TEMPLATE = (
    "[channel-check] Your last reply didn't open with a channel declaration. Every reply must start "
    "with exactly one line, `[[channel:PURPOSE]]`, naming where this belongs — one of: {choices} "
    "(or `main` if none of the others fit). Reply with ONLY that one line, nothing else."
)

#: `reply-marker-forcing-function-spec.md` §1. Position-0-only like the channel line, but checked on
#: the reply AFTER channel extraction — i.e. right where the channel line used to be, or literally
#: first if the channel line is itself missing (§3). Non-greedy `.*?` up to the first `)*` so a
#: parenthetical inside the content ("...the second one))*") still closes on the model's own closing
#: `*` rather than an earlier bare `)`. `(?:\n|$)` rather than a mandatory `\n`: a corrective retry
#: (§3) may hand back the bare line with no trailing newline at all, and it must still match.
REPLY_MARKER_RE = re.compile(r"^\*\(answering\b.*?\)\*[ \t]*(?:\n|$)", re.IGNORECASE)

#: Illustrative, not authoritative — it costs nothing if the model phrases its own answer
#: differently as long as the shape matches `REPLY_MARKER_RE`. Written in the second person because
#: the line is addressed to the owner, who reads it.
REPLY_MARKER_EXAMPLE = "*(answering your question about the build)*"

#: `{owner}` is `identity_common.owner_name` ("the owner" when unconfigured).
REPLY_MARKER_RETRY_PROMPT_TEMPLATE = (
    "[channel-check] Your last reply was missing the required reply-marker line. Every reply must "
    "open with (right after the channel line) exactly one line, `*(answering <what you're replying "
    "to>)*`, naming what this reply answers — e.g. `" + REPLY_MARKER_EXAMPLE + "`. Unlike the channel "
    "line, this one is NOT stripped — it reaches {owner}, so word it for {owner} to read. Reply with "
    "ONLY that one line, nothing else."
)

CHANNEL_AND_MARKER_RETRY_PROMPT_TEMPLATE = (
    "[channel-check] Your last reply was missing both required opening lines. It must open with "
    "exactly two lines, in this order: `[[channel:PURPOSE]]` (naming where this belongs — one of: "
    "{choices}, or `main` if none of the others fit — this line IS stripped, {owner} never sees it), "
    "then `*(answering <what you're replying to>)*` (naming what this reply answers — e.g. `"
    + REPLY_MARKER_EXAMPLE + "` — this line is NOT stripped, it reaches {owner}). Reply with ONLY "
    "those two lines, nothing else."
)


def _owner() -> str:
    """The owner's configured name for owner-facing prompt prose, else "the owner". Never raises: a
    prompt line is never worth a failed turn."""
    try:
        import identity_common

        return identity_common.owner_name(identity_common.load_identity())
    except Exception:  # noqa: BLE001
        return "the owner"


def _topic_choices() -> str:
    """`main` plus every currently named topic, read FRESH (the `resolve_purpose` convention). Falls
    back to `main` alone on any failure — a hint list is never worth a raise."""
    try:
        names = sorted(telegram_topics._load_topic_names())  # noqa: SLF001
    except Exception:  # noqa: BLE001
        names = []
    return ", ".join([telegram_topics.TOPIC_MAIN_CHAT] + names)


def has_reply_marker(reply: str) -> bool:
    """True iff `reply` opens (position 0, exactly like the channel line's own anchor) with a
    `*(answering ...)*` line. Read-only — never strips, never mutates `reply` — because the module
    docstring's "not stripped" rule means this line must reach the owner exactly as written."""
    if not reply:
        return False
    return REPLY_MARKER_RE.match(reply) is not None


def extract_reply_marker_line(text: str) -> str | None:
    """The matched marker line with its trailing newline removed, or `None` if `text` does not open
    with one. Used ONLY on a corrective retry's response, to pull the line back out so the drainer can
    PREPEND it onto the substantive reply that was missing one — the module docstring's "one case a
    corrective's text reaches delivery." Anything the model adds after the line despite being told
    "ONLY that one line" is discarded here, exactly as `extract_channel_declaration`'s corrective
    prose is discarded: what reaches the owner from a corrective is bounded to the one line they
    were owed, never a second free-form message riding in on the side door."""
    if not text:
        return None
    m = REPLY_MARKER_RE.match(text)
    return m.group(0).rstrip("\n") if m else None


#: A fenced code block delimiter, at the start of a line (indentation and a trailing language tag are
#: both tolerated). Used only by :func:`strip_stray_control_lines` — never by
#: :func:`extract_channel_declaration`/:func:`has_reply_marker`, whose position-0-only contracts are
#: unchanged — to keep the one deliberate escape hatch this module's docstring describes: the
#: assistant quoting `[[channel:...]]` inside a code fence while explaining the feature must survive
#: untouched.
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")

#: A standalone channel-declaration line, matched ANYWHERE a line boundary puts it — unlike
#: :data:`CHANNEL_DECLARATION_RE`, which is deliberately position-0-only for every other caller. Only
#: :func:`strip_stray_control_lines` uses this.
_STRAY_CHANNEL_LINE_RE = re.compile(r"^\[\[channel:\s*[A-Za-z0-9-]+\s*\]\]\s*$")


def strip_stray_control_lines(text: str) -> str:
    """Remove a standalone `[[channel:...]]` or `*(answering ...)*` line found OUTSIDE a fenced code
    block, anywhere in `text` — not just at position 0.

    **Called ONLY from the retry path, never on an ordinary reply.** Every other caller in this
    module keeps its position-0-only contract exactly as deliberate as the module docstring says: it
    is what lets the assistant quote this very syntax in a code fence while explaining the
    feature, without the quote being eaten or acted on. This function exists for the one case that
    design doesn't cover — the retry loop is about to PREPEND a freshly recovered marker line onto a
    reply whose own malformed attempt at the control lines is buried mid-message (narration before
    them is exactly why the position-0 checks called them "missing" in the first place) — and cleans
    that stray, orphaned attempt out first so the prepend replaces it instead of stacking a second
    marker and an unstripped `[[channel:...]]` token beside it.

    A line is dropped only when it is EXACTLY a control line with nothing else on it, matching how
    `has_reply_marker`/`extract_channel_declaration` already read only a clean, isolated line as the
    real thing — narration that merely mentions the syntax mid-sentence is untouched, and a line
    inside a ``` fence is untouched regardless of shape."""
    if not text:
        return text
    out = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and (_STRAY_CHANNEL_LINE_RE.match(line.strip())
                              or REPLY_MARKER_RE.match(line + "\n")):
            continue
        out.append(line)
    return "\n".join(out)


def retry_prompt(missing_channel: bool, missing_marker: bool) -> str:
    """ONE prompt naming whichever required opening line(s) are missing this iteration — never two
    round trips for a reply missing both (`reply-marker-forcing-function-spec.md` §3: this reshapes
    the SAME retry rather than stacking a second, independent one alongside it). Rendered fresh on
    every call: the topic list from the live routing table, the owner's name from identity."""
    if missing_channel and missing_marker:
        return CHANNEL_AND_MARKER_RETRY_PROMPT_TEMPLATE.format(choices=_topic_choices(),
                                                               owner=_owner())
    if missing_channel:
        return CHANNEL_RETRY_PROMPT_TEMPLATE.format(choices=_topic_choices())
    return REPLY_MARKER_RETRY_PROMPT_TEMPLATE.format(owner=_owner())


#: `{topic_names}` is the live routing table, `{owner}` is `identity_common.owner_name` ("the owner"
#: when unconfigured) — both filled by :func:`grounding_instruction`, fresh on every call.
GROUNDING_INSTRUCTION_TEMPLATE = (
    "Every Telegram reply must open with exactly TWO control lines, in this order, before any other "
    "text.\n"
    "FIRST, `[[channel:PURPOSE]]`, PURPOSE being one of the currently named topics ({topic_names}) or "
    "`main` for the main chat. This line never reaches {owner} — it is parsed and stripped before "
    "delivery, so use it on every single Telegram reply without exception. `main` is a normal, "
    "expected choice for most replies, not a fallback to avoid: use it whenever nothing more specific "
    "fits rather than guessing at a topic that doesn't exist. This applies whether or not the message "
    "you're answering arrived inside a named topic already — reply from inside one topic about "
    "something unrelated to it, and the declaration should say so, never default to wherever the "
    "question happened to land. A close misspelling still resolves to the right topic; anything else "
    "falls back to `main` — either way, always try to spell it right.\n"
    "SECOND, `*(answering <what you're replying to>)*` — a short clause naming which message from "
    "{owner} this reply is for. It is common to have several threads open with {owner} at once: one "
    "comment can be a steer, one needs no response, and one is an entirely separate line of thought, "
    "and it is easy to lose track of which is which. Unlike the channel line, THIS ONE IS NOT "
    "STRIPPED — it reaches {owner} verbatim, so word it for {owner} to read, e.g. "
    "`" + REPLY_MARKER_EXAMPLE + "`. "
    "Write it on EVERY reply, even when the answer feels obvious to you: there is no exception for an "
    "unambiguous case, because judging ambiguity is exactly the step that is easy to skip.\n"
    "Discord and cockpit replies need no such lines at all.\n"
)


def extract_channel_declaration(reply: str) -> tuple[str | None, str]:
    """`(purpose | None, reply)` — the ONE interception point (module docstring). On a match: the
    lower-cased purpose group, and `reply` with the matched line and its one trailing newline removed,
    so the substantive text starts cleanly with no leading blank line. On no match: `(None, reply)`,
    byte-for-byte unchanged — a synthesized apology/timeout string, a Discord/cockpit reply, or an
    ordinary reply that simply omitted the declaration all take this branch identically."""
    if not reply:
        return None, reply
    m = CHANNEL_DECLARATION_RE.match(reply)
    if not m:
        return None, reply
    return m.group(1).lower(), reply[m.end():]


def resolve_purpose(declared: str | None) -> str:
    """§4, verbatim logic. **Always returns a real purpose string, never `None`** — worst case
    `telegram_topics.TOPIC_MAIN_CHAT` ("main"). `declared.strip().lower()` before matching is
    load-bearing, not decoration: see the module docstring's regex note — a topic's capitalized
    DISPLAY title can score below the 0.8 cutoff against its lower-case key on case alone.

    Reads the topic table FRESH on every call (`telegram_topics._load_topic_names`, that module's own
    convention), never the cached module-level snapshot, so a topic added to the tracked references
    file resolves here with no reimport."""
    if declared is None:
        return telegram_topics.TOPIC_MAIN_CHAT
    declared = declared.strip().lower()
    names = telegram_topics._load_topic_names()  # noqa: SLF001 — the module's own re-read convention
    if declared in names or declared == telegram_topics.TOPIC_MAIN_CHAT:
        return declared
    match = difflib.get_close_matches(declared, list(names), n=1, cutoff=CHANNEL_FUZZY_CUTOFF)
    return match[0] if match else telegram_topics.TOPIC_MAIN_CHAT


def classify_outcome(declared: str | None, resolved: str, retries_used: int) -> str:
    """§7's four log shapes, as one function so the log row and any reader of it agree on what each
    string means: `defaulted-main` (never declared, on any attempt), `fuzzy-matched(from->to)`
    (declared, but not verbatim-equal to what it resolved to), or `declared-first-try` /
    `declared-after-retry(n)` (declared, and it resolved to exactly itself)."""
    if declared is None:
        return "defaulted-main"
    if resolved != declared:
        return f"fuzzy-matched({declared}->{resolved})"
    return "declared-first-try" if retries_used == 0 else f"declared-after-retry({retries_used})"


def current_purpose(state_dir: str, topic) -> str | None:
    """The inbound thread's own resolved purpose — `telegram_topics.purpose_for_thread`, wrapped so
    every caller here gets the same fail-open belt: this feeds a log row and a prompt line, never the
    reply, so nothing it does may raise. `None` on anything unreadable, unmapped, or on the main chat."""
    try:
        return telegram_topics.purpose_for_thread(state_dir, topic)
    except Exception:  # noqa: BLE001 — feeds a log row and a prompt line, never the reply
        return None


def current_topic_line(state_dir: str, topic) -> str:
    """One line naming which Telegram topic (if any) THIS TURN's inbound thread already resolves to —
    refreshed every turn, the same treatment the existing clock line gets, never told once at grounding
    and trusted afterward (message-routing-spec.md §8 fork 4: a stale wrong topic looks exactly like a
    correct one and nothing would flag it). Always returns a
    non-empty line — "no named topic" is itself the informative, common-case answer, not a thing to
    omit."""
    purpose = current_purpose(state_dir, topic)
    if purpose:
        return (f"(This message arrived in Telegram's '{purpose}' topic — that is only where the "
                 f"question came FROM; your own [[channel:...]] declaration decides where the answer "
                 f"belongs, and the two don't have to match.)")
    return "(This message arrived in Telegram's main chat — no named topic.)"


def grounding_instruction() -> str:
    """The paragraph `presence.py`'s `GROUNDING` folds in, telling the model to declare a channel on
    every Telegram reply. **Not optional bookkeeping** (§7's own warning): without it, every reply
    misses the declaration and triggers a corrective round-trip — real latency the owner feels on
    every message, and the outcome log would measure an instruction that was never given. Topic names
    are read FRESH rather than pinned in this string, matching `resolve_purpose`'s convention; the
    owner's name comes from identity (:func:`_owner`)."""
    names = ", ".join(sorted(telegram_topics._load_topic_names()))  # noqa: SLF001
    return GROUNDING_INSTRUCTION_TEMPLATE.format(topic_names=names or "none configured yet",
                                                 owner=_owner())


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_outcome(state_dir: str, *, turn_id: str | None, channel: str, inbound_purpose: str | None,
                    declared_purpose: str | None, resolved_purpose: str, retries_used: int,
                    outcome: str, reply_marker_required: bool, reply_marker_present: bool,
                    reply_marker_retries_used: int, now: datetime | None = None) -> None:
    """One row per Telegram reply to `state/channel-declare-log.jsonl` (§7 + `reply-marker-forcing-
    function-spec.md` §6). **NEVER RAISES** — a logging failure can never cost the reply it is
    describing, the same contract `mouth.record_assertion` and `failures.record` already hold in this
    directory. `declared_purpose` is the raw declaration text (or `None`); `inbound_purpose` is the
    inbound thread's OWN resolved purpose, logged for comparison per the spec's "inbound-thread
    interaction" section — a disagreement between the two is a signal worth having, never a thing this
    phase blocks on.

    `reply_marker_retries_used` is COUNTED SEPARATELY from `retries_used`, deliberately, though both
    come off the same single bounded retry loop: `retries_used` feeds `classify_outcome` and, through
    it, message-routing-spec.md §7's rescue-rate measurement, which is about the CHANNEL retry
    specifically. A retry spent solely recovering the marker must never inflate
    that count, or a reply that declared correctly on the first try but was missing only the marker
    would misread as a channel-declaration rescue it never needed."""
    try:
        path = os.path.join(state_dir, CHANNEL_DECLARE_LOG_FILENAME)
        row = {
            "at": _stamp(now),
            "schema": "seneschal.channel-declare-log/2",
            "turn_id": turn_id,
            "channel": channel,
            "inbound_purpose": inbound_purpose,
            "declared_purpose": declared_purpose,
            "resolved_purpose": resolved_purpose,
            "retries_used": retries_used,
            "outcome": outcome,
            "reply_marker_required": reply_marker_required,
            "reply_marker_present": reply_marker_present,
            "reply_marker_retries_used": reply_marker_retries_used,
        }
        stateio.append_jsonl(path, row)
    except Exception:  # noqa: BLE001 — logging may never cost the reply
        pass
