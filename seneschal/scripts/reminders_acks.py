#!/usr/bin/env python3
"""Durable local **ack ledger** — the fire-time gate that stops the daemon re-nudging acked reminders.

**Why this exists.** The presence daemon (``sentinel.check_reminders``) delivers queued nudges purely by
``due_at`` and **can't read the store**: on the Notion backend its only path is the hosted OAuth MCP,
reachable from the ``claude`` CLI, not from this stdlib daemon. So a nudge staggered earlier in the day
still fires after the owner has acked the underlying item, because the fire path had no way to see the
ack. ``reminders_dequeue.py`` pulls an obsolete *single* nudge by id, but it can't reach inside a
multi-item digest (one entry, one text), and it only helps when the chat/slot path remembers to call it.

This closes the gap with a **durable local ledger** (``state/acks.json``): every ack records the ⏰ row's
key + the local date it landed, and ``check_reminders`` consults it at **fire time**. It's durable
(survives the warm session winding down / a reboot — it's a file, not the volatile session), store-
independent, and **fail-open**: any read error means *fire the nudge* (a redundant buzz beats a missed
Critical one). This is the "gate delivery on durable state, not session memory" fix.

``acks.json`` maps ``{ "<normalized reminder_id>": "YYYY-MM-DD" }`` — the **local** (owner-timezone,
via ``tz_common``) date of the most recent ack for that ⏰ row. Only *today's* acks gate; yesterday's don't
(so tomorrow's re-fire is unaffected), which mirrors the daily reset. Matching is dash-insensitive so a
page id matches with or without dashes, exactly like ``reminders_dequeue``.

A queue entry opts **out** of the gate with ``"ack_gate": false`` — set by same-day-recurring **rolls**
(``reminders_roll.py``), where one "checked messages" ack must not cancel the rest of the day's pings. A
combined digest carries ``"member_reminder_ids": [...]``; it's suppressed only once **every** member is
acked today. Stdlib only.

This module also owns ``queue_lock``, the SAME lock every ``reminders.json`` writer shares
(enqueue/dequeue/roll/reconcile/sentinel) — never a second one — and the catch-up stagger's state file
(``nudge-stagger.json``), which the ack path stamps with ``last_ack_at`` so an ack can advance the drip.

--------------------------------------------------------------------------------------------------
**THE SECOND SENDER — the Watch-peek gate below the fold.**

Everything above gates *the reminder queue*. The queue is not the only thing that can chase a ⏰ row:
the headless **Watch comms peek** (a cheap ``claude -p`` the daemon runs on cadence) can wander outside
its email/Slack/calendar lane into the Reminders data, see a high-importance nag row, and push "X is due
2 hours ago" **without ever reading an ack** — after the ack had landed in every durable place and the
queue had correctly fired nothing.

So there are **two senders and one gate**. Prose can't fix that — the peek is a small-model one-shot,
and this bug class *is* prose rules failing to bind. The functions below are the second gate, run by
the outbound Telegram path a Watch escalation actually uses, regardless of what the peek's model decided
to say:

* :func:`reminder_acked_today` — the shared "is this row acked **right now**?" predicate, over **two**
  independent local readings: this ledger, and (Notion backend only) what the write-behind outbox has
  actually **landed** in the store (``outbox_common.latest_landed_ack`` — the row's `Last Acknowledged`,
  as close to reading the row as a stdlib process gets).
* :func:`watch_escalation_blocked` — the gate itself. A Watch push carries no reminder id, so a row is
  identified from its **title** and the push is blocked only when BOTH the title matches AND the
  message reads as a *chase* (:data:`NAG_CUES`) — a peek that merely mentions the topic ("your
  prescription refill is ready") is not this bug and still goes out.

**The title source matters more than the threshold.** ``reminders.json`` stores no title: it stores the
sentence the assistant sends (*"<row name> — it's due today (day 5). — ack in the store or tell me."*),
and tokenizing all of it lets template boilerplate (``day``, ``ack``, ``notion``) swamp the denominator
until a real chase of an acked row scores 0.33 against a 0.6 knob. So the title comes from the row's
real name in ``state/reminders-id-cache.md`` (:func:`id_cache_titles`) with the template stripped off
the queue text as a fallback (:func:`strip_nudge_template`). The knob did not need to move.

**Fail-open on unknown, fail-closed on acked** (the mirror of the ledger doctrine above): only a
*positive* ack-today reading suppresses. Unreadable ledger, absent outbox, missing titles, any exception
at all → the escalation **goes out**. A genuinely missed Super-Critical item costs incomparably more
than a duplicate nudge. Where the two readings disagree, either one saying "acked today" is enough —
both are written only by a real ack event, neither ever infers one.

Every verdict — blocked *and* allowed — is appended to ``state/watch-gate.jsonl``, because the other
half of the defect was that **nothing anywhere recorded that the peek had sent at all**.

--------------------------------------------------------------------------------------------------
**DEDUPE — the third question on the same door.**

A per-instance **suppression list is never the fix** for a Watch peek that keeps saying the same thing.
The shape it answers: one resolved bank alert re-escalated four times in sixteen hours, each re-worded
with a fresher timestamp and a bigger fee, and the owner's "I just fixed that" changed nothing because
it landed in carry-over prose the Watch path never reads.

:func:`fact_key` and :func:`watch_duplicate_blocked` ask *"has this peek already said this exact
thing?"* over the **same ledger** :func:`watch_escalation_blocked` already writes,
``state/watch-gate.jsonl``, rather than a new store. They live here because this module already owns
``WATCH_GATE_LOG`` and its only writer (:func:`log_watch_gate`); a second module would either re-derive
the log's row shape or import it back from here. A **runtime ack** for arbitrary Watch topics is a
different shape of problem (a new write surface, not a predicate over an existing ledger) and lives in
``watch_ack.py``.

**This is a narrower question than the others the outbound gate asks.** The standing suppression list
(``watch_suppress.py``) asks *"has the owner told me they never want this topic?"*;
:func:`watch_escalation_blocked` asks *"has the owner already acked the ⏰ row this chases?"*;
:func:`watch_duplicate_blocked` asks *"has THIS peek already escalated this exact fact, recently?"* — no
reminder row, no standing instruction. **It must never be read as a second suppression list**: the key
is derived from the message text itself, every time, never hand-maintained, and it only ever matches an
**identical** fact — a genuinely new 🚨/🛑/"Call Me" about a DIFFERENT fact is untouched, because this
predicate never looks at the marker at all, only at what changed underneath it.

**Ships REPORT-ONLY first (instrument before gate).** The outbound path's enforce flag
(``WATCH_DEDUPE_ENFORCE``) flips this from *logged* to *blocking*; until then every verdict — duplicate
or not — is still written to ``watch-gate.jsonl`` so the flip is a one-line env change made after
reading a few days of real log, never a rebuild.

--------------------------------------------------------------------------------------------------
**THE RELAY PREDICATE** — :func:`reaction_ack_fully_landed` — answers a different question from
everything above: *has every store write a 👍 reaction on this Reminders row owes ALREADY LANDED?* IT IS
NOT :func:`reminder_acked_today`, AND READING IT AS THAT REBUILDS THE SECOND-SENDER BUG — that
function's ledger arm is stamped by the daemon's own dequeue and goes positive MILLISECONDS after the
reaction, so gating on bare "acked" would suppress THE FIRST RELAY, the one turn that actually has to
run. ONLY THE notion-outbox ARM MAY LICENSE SILENCE, because only it counts rows that have actually
WRITTEN. Anything short of all-landed relays; the predicate is read-only and never creates the outbox
entries it reads. Notion backend only in effect: on a filesystem backend there is no outbox, so it
always answers "not landed" and the turn relays (today's behaviour).

In naming terms: :func:`entry_acked` is the fire-time gate consumed by ``sentinel.check_reminders``;
:func:`reminder_acked_today` + :func:`watch_escalation_blocked` are the second-sender gate;
:func:`watch_duplicate_blocked` is the dedupe; :func:`reaction_ack_fully_landed` is the relay predicate.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import reminders_cadence as rc  # the cadence grammar — sibling, stdlib-only, no back-import
import tz_common

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
ACKS_FILE = "acks.json"
STAGGER_STATE_FILE = "nudge-stagger.json"  # sentinel's catch-up stagger clock; the ack path stamps last_ack_at
QUEUE_LOCK_FILE = "reminders.json.lock"
REMINDERS_FILE = "reminders.json"
MESSAGE_MAP_FILE = "telegram-message-map.json"  # sentinel.record_sent_message — nudge text by message id
ID_CACHE_FILE = "reminders-id-cache.md"         # the ⏰ row-id cache — page id ← the row's REAL title
WATCH_GATE_LOG = "watch-gate.jsonl"             # every Watch-send verdict, blocked or allowed


@contextlib.contextmanager
def queue_lock(state_dir: str, timeout: float = 90.0, stale_sec: float = 120.0):
    """Cross-process advisory mutex for ``reminders.json`` load-modify-save sections.

    Every writer rewrites the whole file (load → mutate → atomic replace), and since the asyncio daemon
    they can genuinely overlap: the scheduler's ``check_reminders`` runs in a worker thread WHILE a chat
    turn's ``reminders_dequeue.py`` (or a slot's ``reminders_enqueue.py``) runs as a separate process.
    Last-writer-wins there can erase a fresh ``fired_at`` (→ double buzz, violating the one-nudge-per-push
    policy) or resurrect a dequeued entry. This serializes the writers (sentinel.check_reminders,
    reminders_dequeue, reminders_enqueue, reminders_roll.refill_rolls, reminders_reconcile); lives here
    because this module is the leaf they all already reach.

    Semantics, chosen deliberately:
      * plain lockfile (O_CREAT|O_EXCL) — the only cross-process primitive that's stdlib on Windows;
      * a holder that crashed is STOLEN after ``stale_sec`` (mtime age) so a dead process can't wedge
        the queue (120 s > the longest legitimate hold: a burst of sends at 60 s subprocess timeout);
      * **fail-open** past ``timeout``: proceed unlocked. A stuck lock degrades to the pre-lock race —
        a possible redundant buzz — which beats reminders falling silent. Same doctrine as the ledger.
    """
    path = os.path.join(state_dir, QUEUE_LOCK_FILE)
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            os.makedirs(state_dir, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - os.path.getmtime(path) > stale_sec:
                    os.remove(path)  # crashed holder — steal and retry immediately
                    continue
            if time.monotonic() >= deadline:
                break  # fail-open (see docstring)
            time.sleep(0.05)
        except OSError:
            break  # can't even create the lock (odd perms/fs) — fail-open, never silence nudges
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                os.remove(path)


def norm_key(key) -> str:
    """Normalize a reminder key for comparison: lowercase, drop dashes/whitespace — so a page id
    matches whether or not it carries dashes. Non-strings normalize to '' (never match). Mirrors
    ``reminders_dequeue._norm`` so the ledger and the dequeue path agree on identity."""
    if not isinstance(key, str):
        return ""
    return "".join(key.split()).replace("-", "").lower()


def local_today(now: datetime | None = None) -> str:
    """The current **owner-local** date as ``YYYY-MM-DD``, via ``tz_common``.

    Accepts an aware UTC instant (as ``check_reminders`` holds) — naive is taken as UTC — and converts
    to the owner's timezone (the configured identity zone when resolvable, else machine-local). With no
    argument, reads the clock now. Gating on the *local* date (not UTC) matches the daily reset.

    **A date-dependent test must inject its instant** — pass ``now=`` here and to every predicate under
    test, deriving fixture dates from *that same instant through this same function* — never seed a
    hard-coded date against a runtime no-argument call. Those agree on exactly one calendar day, in
    exactly one timezone, and a CI runner in UTC disagrees for hours of every evening."""
    if now is None:
        return tz_common.local_today()
    return tz_common.to_local(now).strftime("%Y-%m-%d")


def acks_path(state_dir: str) -> str:
    return os.path.join(state_dir, ACKS_FILE)


def load_acks(state_dir: str) -> dict:
    """Load the ack ledger (``{normalized_reminder_id: 'YYYY-MM-DD'}``). A missing or malformed file
    reads as an empty ledger — fail-open, so a broken ledger never *suppresses* a genuine nudge."""
    data = load_json(acks_path(state_dir), {})
    return data if isinstance(data, dict) else {}


def save_acks(state_dir: str, acks: dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    tmp = acks_path(state_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(acks, fh, indent=2, sort_keys=True)
    os.replace(tmp, acks_path(state_dir))


def record_ack(state_dir: str, reminder_id: str, date_str: str | None = None) -> dict | None:
    """Stamp ``reminder_id`` as acked on ``date_str`` (default: local today). Returns the written
    ``{key, date}`` record, or ``None`` if the id normalizes to empty (nothing to record). Overwrites
    any prior date for that key — we only ever care about the *most recent* ack."""
    key = norm_key(reminder_id)
    if not key:
        return None
    date_str = date_str or local_today()
    acks = load_acks(state_dir)
    acks[key] = date_str
    save_acks(state_dir, acks)
    return {"key": key, "date": date_str}


def prune_acks(state_dir: str, keep_date: str) -> int:
    """Drop ledger rows older than ``keep_date`` (string compare works for ISO dates). Returns how many
    were removed. Optional housekeeping for Dream — the gate ignores stale rows anyway, this just keeps
    the file small."""
    acks = load_acks(state_dir)
    kept = {k: d for k, d in acks.items() if isinstance(d, str) and d >= keep_date}
    removed = len(acks) - len(kept)
    if removed:
        save_acks(state_dir, kept)
    return removed


def stagger_path(state_dir: str) -> str:
    return os.path.join(state_dir, STAGGER_STATE_FILE)


def load_stagger_state(state_dir: str) -> dict:
    """``nudge-stagger.json`` as a dict — ``{last_nonpiercing_fire, last_ack_at}``, both ISO-UTC and
    both optional. A missing or non-object file reads as empty (fail-open on the fire side, fail-SAFE
    on the ack-advance side: no ``last_ack_at`` means no advance)."""
    data = load_json(stagger_path(state_dir), {})
    return data if isinstance(data, dict) else {}


def save_stagger_state(state_dir: str, **fields: str) -> dict:
    """Merge ``fields`` into ``nudge-stagger.json`` atomically (tmp + ``os.replace``, the same shape as
    ``save_acks``) and return the written dict. MERGE, not overwrite: the fire path owns
    ``last_nonpiercing_fire`` and the ack path owns ``last_ack_at``, and each must leave the other's
    field standing or the stagger clock and the ack-advance would erase each other's evidence."""
    os.makedirs(state_dir, exist_ok=True)
    data = load_stagger_state(state_dir)
    data.update(fields)
    tmp = stagger_path(state_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    # Same Windows retry `sentinel.save_json` carries: `os.replace` onto a file another process holds
    # open raises PermissionError there where POSIX renames straight through, and this file has TWO
    # writers (the daemon's fire path, the dequeue subprocess) plus its readers.
    for attempt in range(3):
        try:
            os.replace(tmp, stagger_path(state_dir))
            break
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.1 * (attempt + 1))
    return data


def record_ack_instant(state_dir: str, now: datetime | None = None) -> str:
    """Stamp ``last_ack_at`` (UTC ISO, trailing ``Z``) into ``nudge-stagger.json`` — the ack-advance
    signal. ``sentinel``'s stagger gate releases its 15-min hold once an ack has landed AFTER the last
    non-piercing fire and ``ACK_ADVANCE_DEBOUNCE_SEC`` has passed since that ack, so the next
    still-pending nudge follows an ack promptly instead of waiting out the window. Every ack — chat
    write-through, a Telegram 👍, a slot's ``Ack`` tick — reaches ``reminders_dequeue.main``, which is
    why the stamp lives on that path and nowhere else. Overwrites any prior instant: a burst of acks is
    a burst of overwrites, and the debounce measures from the LAST one, which is the point."""
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    save_stagger_state(state_dir, last_ack_at=stamp)
    return stamp


def entry_acked(entry: dict, acks: dict, today_str: str) -> bool:
    """Pure fire-time gate predicate: should this queued entry be **suppressed as already-acked today**?

    - ``ack_gate: false`` (multi-fire rolls) → never gated.
    - Collect the entry's reminder keys: ``reminder_id`` plus any ``member_reminder_ids`` (a digest).
    - No keys → not gated (an un-keyed nudge fires as before; we never make delivery *worse*).
    - Gated iff **every** collected key is acked on ``today_str`` in ``acks`` — so a plain single-row
      nudge drops once its row is acked, and a digest drops only once *all* its members are.

    Pure and total (never raises) so ``check_reminders`` can call it in a tight loop and callers stay
    fail-open."""
    if not isinstance(entry, dict) or entry.get("ack_gate") is False:
        return False
    keys = []
    rid = entry.get("reminder_id")
    if rid:
        keys.append(rid)
    members = entry.get("member_reminder_ids")
    if isinstance(members, list):
        keys.extend(m for m in members if m)
    norm = [norm_key(k) for k in keys]
    norm = [k for k in norm if k]
    if not norm:
        return False
    return all(acks.get(k) == today_str for k in norm)


def entry_acked_today(state_dir: str, entry: dict, now: datetime | None = None) -> bool:
    """Convenience wrapper: load the ledger and evaluate :func:`entry_acked` for ``now`` (local today).
    ``check_reminders`` uses the pure form directly (loading the ledger once per pass); this is for
    one-off callers/tests."""
    try:
        return entry_acked(entry, load_acks(state_dir), local_today(now))
    except Exception:  # noqa: BLE001 — fail-open: a ledger error must never suppress a real nudge
        return False


# Local json loader (kept self-contained so this module has no import cycle with sentinel).
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# =========================================================================== the Watch-peek ack gate
# (See "THE SECOND SENDER" in the module docstring for why this exists at all.)

#: Title tokens shorter than this never carry identity ("ml", "am", "pm", "0").
MIN_TOKEN_LEN = 3

#: Words that appear in so many reminder titles that matching on them would identify nothing. This is
#: deliberately a *stop* list rather than a whitelist: an unrecognised word counts as distinctive, so a
#: new reminder is matchable the day it exists. Widen it only when a word demonstrably over-matches.
#:
#: ``ack`` / ``acked`` / ``notion`` / ``tick`` / ``day`` / ``days`` are here on exactly that evidence:
#: every entry in a live queue ends in the template's ack hint, so those words carried zero identity
#: while inflating every title's denominator. :func:`strip_nudge_template` removes the clause itself;
#: these entries are the second line of defence, for a source that isn't shaped like the template (a
#: message-map entry, a reworded hint) and for the scheduling filler ``day``/``days``.
STOPWORDS = frozenset("""
    a ack acked an and any are ask ask's at be been before but by can check day days did do does
    doing done due
    each every for from get gets go going had has have her hers him his how i if in into is it its
    just make made me my need needs new not notion now of off on once one only or other our out over
    please put ready remind reminder reminders run set she should so some soon still store take taken
    taking tell that the their them then there these they this those tick time today todo too two up
    us use was way we were what when where which while who why will with would yet you your
""".split())

#: A Watch push is blocked only if it reads as a **chase** — "you still owe me this" — rather than a
#: mention. THE ONE LIST TO EDIT when a new chase wording slips through; each entry is matched as a
#: plain lowercase substring. Keeping this conjunction is what stops the gate eating a legitimate
#: escalation that happens to name an acked reminder's topic.
NAG_CUES = (
    "due", "overdue", "past due", "still", "haven't", "havent", "have not", "hasn't", "hasnt",
    "did you", "have you", "are you able", "did we", "time to", "don't forget", "dont forget",
    "forgot", "missed", "missing", "not done", "undone", "outstanding", "unacked", "no ack",
    "ago", "yet", "reminder", "nag", "chase", "follow up", "follow-up", "waiting on you",
)

#: Fraction of a title's distinctive tokens that must appear in the message for it to be *that* row.
#: THE ONE KNOB. Lower = more suppression (and more risk of eating a real escalation); higher = a chase
#: that names two of a three-token row name (0.67) stops being caught.
#:
#: **When the gate misses, suspect the DENOMINATOR before this knob.** Scoring a chase against the
#: queue's whole nudge sentence (template boilerplate included) instead of the row's name is how a real
#: repeat once scored 0.333 and went out; the fix was :func:`strip_nudge_template` +
#: :func:`id_cache_titles`, and this number never moved. Any change here wants a test that pins real
#: messages against real queue text.
TITLE_MATCH_MIN_COVERAGE = 0.6

_WORD_RE = re.compile(r"[^0-9a-z]+")
_NUDGE_PREFIX_RE = re.compile(r"^\s*(?:⏰\s*)?reminder:\s*", re.IGNORECASE)

#: The ack hint the reminder template ends EVERY nudge with — *"— ack in Notion or tell me."*
#: (``subagents/reminders/SKILL.md`` words it as a tick-or-tell hint). Matched as a trailing
#: dash-clause containing both `ack` and a store word (`notion` / `store`), so a reworded hint still
#: goes; the leading `[^—–]*` cannot cross a dash, which is what keeps this anchored to the LAST
#: clause rather than eating the row's own name.
_ACK_HINT_RE = re.compile(r"\s*[—–-]+\s*[^—–]*\back\b[^—–]*\b(?:notion|store)\b.*$", re.IGNORECASE)

#: The template's scheduling clause — *"— it's due today (day 5)."*, *"— due today"*, *"— it's
#: overdue"*. Same reasoning as the ack hint: the ⏰ row's own name never carries when it is due.
_DUE_CLAUSE_RE = re.compile(r"\s*[—–-]+\s*(?:it['’]?s\s+)?(?:over)?due\b[^—–]*$", re.IGNORECASE)

#: A ⏰ page id, normalized (32 hex, dashes dropped). Used to tell a real id-cache row from the table's
#: header and ``|---|`` separator without having to know where in the file the table starts.
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")

#: The id cache's ``Cadence`` predicate for a row that fires many times a day — its mirror of the
#: queue's ``ack_gate: false`` (see :func:`id_cache_titles`). **Delegated to `reminders_cadence` so
#: this module holds no cadence vocabulary of its own**; that function is contractually never narrower
#: than the substring test it replaced, which is what makes the swap unable to newly gate a roll.
_MULTI_FIRE_CADENCE = rc.is_multi_fire


def significant_tokens(text) -> set:
    """The distinctive lowercase word-tokens of ``text``: emoji and punctuation dissolve, stopwords and
    sub-:data:`MIN_TOKEN_LEN` fragments drop out. Total — a non-string is an empty set."""
    if not isinstance(text, str):
        return set()
    words = _WORD_RE.sub(" ", text.lower()).split()
    return {w for w in words if len(w) >= MIN_TOKEN_LEN and w not in STOPWORDS}


def title_coverage(title, message) -> float:
    """What fraction of ``title``'s distinctive tokens the ``message`` contains (0.0 when the title has
    no distinctive tokens at all — an unidentifiable title must never match, or "Do it now" would gate
    every message). Direction matters: we ask whether the message names the ROW, not vice versa."""
    want = significant_tokens(title)
    if not want:
        return 0.0
    return len(want & significant_tokens(message)) / len(want)


def message_chases(message) -> bool:
    """Does ``message`` read as chasing an outstanding item (:data:`NAG_CUES`) rather than merely
    mentioning it? The second half of the match conjunction — see :data:`NAG_CUES`."""
    if not isinstance(message, str):
        return False
    low = message.lower()
    return any(cue in low for cue in NAG_CUES)


def strip_nudge_prefix(text) -> str:
    """Drop the ``⏰ Reminder: `` the delivery path adds, so a message-map entry yields the row's own
    wording rather than a token every nudge shares."""
    return _NUDGE_PREFIX_RE.sub("", text) if isinstance(text, str) else ""


def strip_nudge_template(text) -> str:
    """A queued nudge's **text** minus the reminder template, leaving the row's distinctive name.

    ``reminders.json`` does not store a title — it stores the sentence the assistant sends, and that
    sentence is mostly template::

        "Water the fern — it's due today (day 5). — ack in Notion or tell me."
         └─ the row ──┘  └─ scheduling ────────┘  └─ on every single nudge ──┘

    Tokenized whole, the template words (`day`, `ack`, `notion`) appear in **every** entry in the queue
    and dilute every title's denominator. Removes, in order: the ``⏰ Reminder:`` prefix, the trailing
    ack hint (:data:`_ACK_HINT_RE`), and the trailing due-clause (:data:`_DUE_CLAUSE_RE`).

    Total — a non-string is ``""``, and a text that is *only* template strips to empty, which
    :func:`title_coverage` already reads as "unidentifiable, never match"."""
    if not isinstance(text, str):
        return ""
    out = _ACK_HINT_RE.sub("", strip_nudge_prefix(text))
    return _DUE_CLAUSE_RE.sub("", out).strip()


def id_cache_titles(state_dir: str) -> dict:
    """``{normalized page id: {"title": str, "multi_fire": bool}}`` from ``state/reminders-id-cache.md``
    — **the row's real store name**, which is the best title source on this host.

    That file is the live ⏰ row-id cache (page id ← Reminder, written when a reminder is created and
    reconciled nightly by Dream). It is Markdown, it is local, and a stdlib process can read it — so the
    row's real name is available without the store MCP the daemon doesn't have. Rows are parsed out of
    the table by shape: a first cell that is the title, a second cell whose contents normalize to a
    32-hex page id. That last requirement is what makes the header and the ``|-----|`` separator rows
    fall out for free.

    ``multi_fire`` comes from the ``Cadence`` column, via ``reminders_cadence.is_multi_fire`` rather
    than a value this module knows: a **multiple/day** cadence is the cache's own mirror of a queue
    entry's ``ack_gate: false``, and the caller maps it to the same exemption — otherwise adding this
    source would newly gate rows (a "check messages every 2h" roll) that the fire-time gate has always
    exempted, on a day when they happen not to be in the queue.

    Total and fail-open: a missing/unreadable/reshaped file contributes nothing rather than raising."""
    out: dict = {}
    try:
        with open(os.path.join(state_dir, ID_CACHE_FILE), "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except Exception:  # noqa: BLE001 — an absent cache is the normal case on a fresh checkout
        return out
    for line in lines:
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        key = norm_key(cells[1].strip("`"))
        if not _HEX32_RE.match(key):
            continue  # the header, the |---| separator, and any prose row: not a page id, not a row
        title = cells[0].strip("`* ")
        if not title:
            continue
        cadence = cells[4] if len(cells) > 4 else ""
        rec = out.setdefault(key, {"title": title, "multi_fire": False})
        if _MULTI_FIRE_CADENCE(cadence):
            rec["multi_fire"] = True
    return out


def known_reminder_titles(state_dir: str) -> dict:
    """``{normalized reminder key: {"titles": [...], "ack_gate": bool}}`` — every ⏰ row this host can
    put a *name* to, from three local sources that already exist and that no ack path has to start
    writing. Each contributes a candidate; :func:`watch_escalation_blocked` scores them all and keeps
    the **best** coverage, so a weak source can only ever fail to match, never mask a good one:

      * ``reminders-id-cache.md`` — **the row's real store title**, and therefore the one to prefer.
        See :func:`id_cache_titles`;
      * ``reminders.json`` — the queue's own `text` per `reminder_id`, including **fired** entries (the
        dequeue only ever removes un-fired ones), which is exactly what survives an ack — **minus the
        nudge template** (:func:`strip_nudge_template`), because that text is a whole sentence, not a
        title; and
      * ``telegram-message-map.json`` — what was actually sent for that row, same stripping.

    ``ack_gate`` mirrors the queue-entry flag of the same name: ``False`` marks a **multi-fire roll**,
    where one ack must not silence the rest of the day — such a row is never blocked here either, so the
    two gates agree by construction. A ``Cadence = Multiple/day`` row in the id cache sets it the same
    way, so widening the title sources cannot widen *what gets gated*. Digest members are deliberately
    NOT mapped: a digest's text covers several rows and would over-match every one of them.

    Total and fail-open — any unreadable/odd file contributes nothing rather than raising."""
    out: dict = {}

    def _add(key: str, title: str, ack_gate=None):
        if not key or not isinstance(title, str) or not title.strip():
            return
        rec = out.setdefault(key, {"titles": [], "ack_gate": True})
        if title not in rec["titles"]:
            rec["titles"].append(title)
        if ack_gate is False:
            rec["ack_gate"] = False

    try:
        for key, rec in id_cache_titles(state_dir).items():
            _add(key, rec.get("title"), False if rec.get("multi_fire") else None)
    except Exception:  # noqa: BLE001 — a title source is a nicety; never let one break the send path
        pass
    try:
        queue = load_json(os.path.join(state_dir, REMINDERS_FILE), [])
        for entry in queue if isinstance(queue, list) else []:
            if isinstance(entry, dict):
                _add(norm_key(entry.get("reminder_id")), strip_nudge_template(entry.get("text")),
                     entry.get("ack_gate"))
    except Exception:  # noqa: BLE001
        pass
    try:
        data = load_json(os.path.join(state_dir, MESSAGE_MAP_FILE), {})
        messages = data.get("messages") if isinstance(data, dict) else None
        for entry in (messages or {}).values():
            if isinstance(entry, dict) and entry.get("reminder_id"):
                _add(norm_key(entry.get("reminder_id")), strip_nudge_template(entry.get("text")))
    except Exception:  # noqa: BLE001
        pass
    return out


def _ledger_ack_date(state_dir: str, key: str):
    """(ack date for ``key`` or None, readable?) from ``acks.json``. An ABSENT file is readable-and-
    silent; a present-but-corrupt one is *unreadable*, which the caller reports but still fails open
    on — the distinction only ever changes what the log line says."""
    path = acks_path(state_dir)
    if not os.path.exists(path):
        return None, True
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:  # noqa: BLE001
        return None, False
    if not isinstance(raw, dict):
        return None, False
    value = raw.get(key)
    return (value if isinstance(value, str) else None), True


def _notion_ack_date(state_dir: str, key: str):
    """(the newest `Last Acknowledged` **landed in the store** for ``key`` or None, readable?).

    This is the store arm of the predicate, and it exists only on the **Notion backend** (the only one
    with a write-behind outbox; filesystem backends write directly and have no outbox database, which
    reads here as readable-and-silent). The daemon cannot read Notion, so the honest local stand-in is
    the outbox's record of what it has actually **written**: ``outbox_common.latest_landed_ack`` counts
    only `done` entries that really wrote. Read-only — it never creates the store, never enqueues,
    never drains.

    An ``outbox_common`` that does not (yet) expose ``latest_landed_ack`` gives this arm no opinion
    (readable-and-silent), so the ledger arm alone decides — the behaviour before this arm existed."""
    try:
        import outbox_common as ob
    except Exception:  # noqa: BLE001 — outbox absent/broken: this arm simply has no opinion
        return None, False
    try:
        if not os.path.exists(ob.db_path(state_dir)):
            return None, True
        landed = getattr(ob, "latest_landed_ack", None)
        if landed is None:
            return None, True
        conn = ob.connect(state_dir)
        try:
            return landed(conn, key), True
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None, False


def reminder_acked_today(state_dir: str, reminder_id, now: datetime | None = None) -> dict:
    """**The shared predicate: is this ⏰ row acked right now?**

    Consults both local readings — the durable ack ledger and the outbox's landed store writes — and
    reports each separately, so a caller (and the gate log) can say *which* one knew. Returns::

        {"key", "date", "acked": bool, "sources": [...], "unreadable": [...]}

    ``acked`` is true iff **some** source reads today's owner-local date. Disagreement resolves to
    acked: both sources are written only by a real ack, so a positive is evidence and a silence is not.
    Never raises; an id that normalizes empty is simply not acked."""
    key = norm_key(reminder_id)
    today = local_today(now)
    result = {"key": key, "date": None, "acked": False, "sources": [], "unreadable": []}
    if not key:
        return result
    for name, reader in (("ledger", _ledger_ack_date), ("notion-outbox", _notion_ack_date)):
        try:
            date, readable = reader(state_dir, key)
        except Exception:  # noqa: BLE001 — belt and braces: a reader must never raise into a send path
            date, readable = None, False
        if not readable:
            result["unreadable"].append(name)
        if date == today:
            result["acked"] = True
            result["date"] = today
            result["sources"].append(name)
    return result


def reaction_ack_fully_landed(state_dir: str, reminder_id, now: datetime | None = None) -> dict:
    """**Has every store write a 👍 on this ⏰ row owes already landed?**

    Returns ``{"key", "date", "landed": bool, "why", "ack_sources"}``. ``landed`` is the only field a
    gate should branch on; the rest is *why*, for the log and for a human counting suppressions
    afterwards.

    **This is NOT :func:`reminder_acked_today`, and reading it as that would rebuild the second-sender
    bug.** That predicate answers *"has the owner acked?"* over two arms, and its ``ledger`` arm is
    stamped by the daemon's own dequeue — so it is positive **milliseconds** after the 👍 arrives, and a
    gate built on it would suppress the one relay that has to run. This answers a different question:
    *"has the write the owner is owed HAPPENED?"* Only the ``notion-outbox`` arm can answer that,
    because only it counts rows that actually **wrote**.

    **Fail-open in exactly one direction, absolutely.** Anything short of *all landed* returns
    ``landed: False``, which relays the turn — today's behaviour. An unreadable store, a missing
    module, a filesystem backend with no outbox, an id that normalizes empty, a raise anywhere: all
    relay. The only harm a suppression mechanism can do is withhold a message the owner needed, and it
    may never do that because a file would not open. Reads, writes nothing, creates nothing."""
    key = norm_key(reminder_id)
    today = local_today(now)
    result = {"key": key, "date": today, "landed": False, "why": "unknown", "ack_sources": []}
    if not key:
        result["why"] = "unnamed-row"
        return result
    try:
        verdict = reminder_acked_today(state_dir, key, now)
        result["ack_sources"] = list(verdict.get("sources") or [])
        if "notion-outbox" not in result["ack_sources"]:
            # Covers the case where the LEDGER arm is positive and the outbox arm is not — which is
            # exactly the first relay after a reaction, before its own outbox entry lands, and exactly
            # the turn that must not be suppressed.
            result["why"] = "ack-not-landed"
            return result
        result["landed"] = True
        result["why"] = "all-landed"
        return result
    except Exception:  # noqa: BLE001 — see the docstring: a raise relays, it never suppresses
        result["landed"] = False
        result["why"] = "raised"
        return result


def watch_escalation_blocked(state_dir: str, text: str, reminder_id=None,
                             now: datetime | None = None) -> dict | None:
    """**The gate.** ``None`` = send it. A dict = this push is chasing a row that is already acked
    today, and must not go out; the dict says which row, which reading knew, and why it matched.

    Two ways in:
      * ``reminder_id`` given — the row is named, so ask the predicate directly. No heuristics.
      * text only (the real Watch case) — require BOTH that the message reads as a chase
        (:func:`message_chases`) and that it names a known row's title to
        :data:`TITLE_MATCH_MIN_COVERAGE`. Rows flagged ``ack_gate: false`` (multi-fire rolls) are never
        blocked, matching the fire-time gate.

    Fail-open on absolutely everything: no title source, an unreadable ledger, a raise anywhere → the
    escalation goes out. Only a positive ack-today reading suppresses."""
    try:
        if reminder_id:
            verdict = reminder_acked_today(state_dir, reminder_id, now)
            if verdict["acked"]:
                return {"reason": "reminder_acked_today", "key": verdict["key"], "date": verdict["date"],
                        "sources": verdict["sources"], "matched_by": "reminder_id",
                        "matched_title": None, "coverage": None}
            return None
        if not message_chases(text):
            return None  # a mention, not a chase — not this bug
        best = None
        for key, info in known_reminder_titles(state_dir).items():
            if info.get("ack_gate") is False:
                continue  # multi-fire roll: one ack never silences the day (mirrors `entry_acked`)
            for title in info.get("titles", []):
                cov = title_coverage(title, text)
                if cov >= TITLE_MATCH_MIN_COVERAGE and (best is None or cov > best[2]):
                    best = (key, title, cov)
        if best is None:
            return None
        key, title, cov = best
        verdict = reminder_acked_today(state_dir, key, now)
        if not verdict["acked"]:
            return None
        return {"reason": "reminder_acked_today", "key": key, "date": verdict["date"],
                "sources": verdict["sources"], "matched_by": "title",
                "matched_title": title, "coverage": round(cov, 3)}
    except Exception:  # noqa: BLE001 — fail-open: the gate may never be the reason the owner wasn't told
        return None


def log_watch_gate(state_dir: str, record: dict) -> None:
    """Append one verdict to ``state/watch-gate.jsonl`` — **blocked and allowed alike**, because the
    other half of the second-sender defect was that a Watch push left no trace anywhere. This NEVER
    raises, so no caller wraps it in a try/except; a failed append costs the row, never the message."""
    try:
        os.makedirs(state_dir, exist_ok=True)
        line = dict(record)
        line.setdefault("ts", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        line.setdefault("schema", "seneschal.watch-gate/1")
        with open(os.path.join(state_dir, WATCH_GATE_LOG), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ============================================================================= dedupe (fact identity)
# (See "DEDUPE" in the module docstring for why this lives here rather than a new module.)

#: Default lookback for :func:`watch_duplicate_blocked` — long enough to catch a fact re-escalated
#: several times inside a day, short enough that a fact which legitimately recurs day over day (the
#: SAME account trips again a week later) is not gated by something that happened last month.
WATCH_DEDUPE_WINDOW_HOURS = 24.0

#: :func:`fact_key`'s extra stopwords — hedge/filler words an escalation restates every time it
#: re-describes the SAME fact, on top of the title matcher's own :data:`STOPWORDS` above. Kept as a
#: SEPARATE set deliberately: :data:`STOPWORDS` is tuned for ⏰ row *titles* (short noun phrases);
#: this is tuned for freeform alert prose, where a peek narrates elapsed time and magnitude in whatever
#: words it picks that pass ("remains" vs. "is still", "down" vs. "at"). Widen it only on a demonstrated
#: false-negative (two real repeats of one fact that failed to collapse to one key).
FACT_STOPWORDS = frozenset("""
    remains remaining down about roughly approximately around nearly almost update updated status
    current currently confirm confirmed
""".split())

#: A time-of-day mention — ``12:30``, ``12:30 pm``, ``12:30 EST``, ``12:30 UTC`` — the part of an
#: escalation that is GUARANTEED to differ across repeats of one fact (each push names when IT last
#: checked, not when the fact began). The trailing zone is any common abbreviation (``[ECMPA][SD]?T``
#: covers the North-American families, plus UTC/GMT and a few frequent others) — never one owner's
#: zone, because the peek writes whatever the owner's zone is called.
_TIME_OF_DAY_RE = re.compile(
    r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\s*(?:[ecmpa][sd]?t|utc|gmt|bst|cet|cest|ist|jst|aest|aedt)?\b",
    re.IGNORECASE)

#: An elapsed-time mention — ``2 hours``, ``3.5 hours``, ``16 hours``, ``20 minutes`` — same reasoning:
#: this is the AGE of the fact at push time, which grows every repeat by construction.
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?|days?)\b", re.IGNORECASE)

#: A dollar amount — ``$42.17``, ``-$58.32``. Stripped rather than kept: an accruing fee changes the
#: figure on every repeat of the SAME fact, and the figure is not what identifies the row (the account
#: is). If a future case needs the amount as identity, that is a widening to argue for with a measured
#: false-negative, the same as :data:`FACT_STOPWORDS`.
_CURRENCY_RE = re.compile(r"-?\$\s?\d[\d,]*(?:\.\d+)?")

#: Multi-word (or otherwise un-tokenizable) phrases that mark WHEN/HOW-RECENTLY rather than WHAT —
#: stripped before tokenizing. ``\bsince\b`` and ``\bas of\b`` in particular are load-bearing: without
#: them, every repeat of a fact contributes its own "as of <time>" clause and the residual "as"/"of"
#: pair (or the word "since") would vary the token set for no identity reason.
_RELATIVE_PHRASE_RE = re.compile(
    r"\b(?:ago|overdue|since|as of|checked|outstanding|early this morning|this morning|"
    r"right now|just now|earlier today|recently)\b", re.IGNORECASE)

#: A leading emoji/symbol run — ``🚨``, ``🛑``, ``🔴`` — never identity; the suppression list's own
#: critical-marker carve-out already proves the marker itself must never be what a gate keys on.
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF\U00002B00-\U00002BFF]+")

#: A leading label the peek (or a source alert) prefixes onto the line — ``Financial alert:``,
#: ``Watch Alert:`` — never part of the fact itself.
_LABEL_PREFIX_RE = re.compile(
    r"^\s*(?:financial alert|watch alert|alert|reminder|heads up|fyi)\s*[:\-–—]\s*",
    re.IGNORECASE)


def fact_key(text) -> str:
    """The normalized identity of an escalation's underlying fact — total, never raises; a non-string
    or an unidentifiable text is ``""`` (which :func:`watch_duplicate_blocked` reads as "never match",
    the same convention :func:`title_coverage` uses for an empty title).

    Strips, in order: a leading emoji/symbol run, a leading label prefix (``"Financial alert: "``),
    every time-of-day and elapsed-duration mention, every dollar amount, and the relative-time phrases
    that frame them (``"as of"``, ``"since"``, ``"ago"``, ``"overdue"``, …) — the parts of an
    escalation that are GUARANTEED to change on every repeat of the SAME fact. What survives is
    tokenized exactly like a ⏰ row title (:func:`significant_tokens`) minus :data:`FACT_STOPWORDS`,
    then joined sorted — so token ORDER in the source text never matters.

    **This is intentionally narrower than a general paraphrase-matcher.** It recognises that the SAME
    templated/near-templated escalation, re-generated with a fresher timestamp and a bigger fee each
    time, is one fact wearing four timestamps — not that two wholly different sentences describe the
    same event (that is :func:`source_fact_key`'s job, for a source-backed escalation)."""
    if not isinstance(text, str) or not text.strip():
        return ""
    s = _EMOJI_RE.sub(" ", text)
    s = _LABEL_PREFIX_RE.sub("", s)
    s = _TIME_OF_DAY_RE.sub(" ", s)
    s = _DURATION_RE.sub(" ", s)
    s = _CURRENCY_RE.sub(" ", s)
    s = _RELATIVE_PHRASE_RE.sub(" ", s)
    tokens = {t for t in significant_tokens(s) if t not in FACT_STOPWORDS}
    if not tokens:
        return ""
    return " ".join(sorted(tokens))


# ------------------------------------------------------------------------ source-derived identity

#: A source-derived key's prefix, so a reader of `watch-gate.jsonl`/`watch-acks.json` can tell the two
#: kinds of key apart at a glance and :func:`is_source_fact_key` needs no second signal.
SOURCE_FACT_KEY_PREFIX = "source:"

#: An account token — ``x1234``, ``ending in x1234``, ``ending 1234``, ``****1234`` — the one part of a
#: bank alert that identifies WHICH account the family is about. Captured from the subject and the
#: escalation prose alike (a peek names the account whichever way it rewords the rest). The bare
#: 4-digit form without any lead-in is deliberately NOT matched: a year or a dollar figure would be.
_ACCOUNT_TOKEN_RE = re.compile(
    r"(?:\bx(?=\d{4}\b)|\bending\s+(?:in\s+)?x?|\*{2,}\s*|\bacct\.?\s*#?\s*|\baccount\s*#\s*)(\d{4})\b",
    re.IGNORECASE)

#: ``Re:`` / ``Fwd:`` / ``FW:`` chains on a subject line — a reply's subject is the same family as the
#: original's.
_SUBJECT_REPLY_RE = re.compile(r"^\s*(?:re|fwd?|fw)\s*:\s*", re.IGNORECASE)

#: A mailbox address inside a display-name form — ``Bank Alerts <alerts@bank.example>`` →
#: ``alerts@bank.example``.
_ADDR_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def account_tokens(*texts) -> set:
    """Every account token named in any of `texts`, normalized to ``x<last4>`` (``{"x1234"}``).
    Total — a non-string contributes nothing."""
    out = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        for m in _ACCOUNT_TOKEN_RE.finditer(text):
            out.add("x" + m.group(1))
    return out


def sender_identity(sender) -> str:
    """The stable identity of a source sender: the lowercase mailbox address if one is present
    (``"Bank Alerts <alerts@bank.example>"`` → ``"alerts@bank.example"``), else the lowercase stripped
    name. ``""`` for anything unusable."""
    if not isinstance(sender, str) or not sender.strip():
        return ""
    m = _ADDR_RE.search(sender)
    return (m.group(0) if m else sender.strip()).lower()


def normalized_subject(subject) -> str:
    """A subject line reduced to its identifying tokens — ``Re:``/``Fwd:`` chains dropped, then the
    SAME stripping :func:`fact_key` applies to prose (emoji, label prefix, times, durations, currency,
    relative-time phrasing), with the account token removed too (it has its own slot in the key).
    Sorted, so word order never matters. ``""`` when nothing distinctive survives."""
    if not isinstance(subject, str):
        return ""
    s = subject
    for _ in range(4):  # "Re: Fwd: Re: …" — bounded, never a while-loop on user text
        stripped = _SUBJECT_REPLY_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    s = _ACCOUNT_TOKEN_RE.sub(" ", s)
    return fact_key(s)


def source_fact_key(sender, subject=None, text=None) -> str:
    """**The stable identity of a SOURCE-BACKED escalation** — derived from the email it came from
    (sender + normalized subject + the account token, if any, named in the subject OR the escalation
    prose), never from the escalation's own wording::

        source:alerts@bank.example|notice overdraft|x1234

    Why this exists: the peek re-describes one alert email in fresh prose on every pass, and
    :func:`fact_key`, being a bag of the prose's own words, can mint dozens of distinct keys for ONE
    alert family in a week — so an ack on any one of them covered none of the others. The prose is what
    varies; the email it came from is what doesn't.

    ``""`` when there is no usable sender — the caller falls back to :func:`fact_key`
    (:func:`escalation_fact_key` does exactly that). Total, never raises."""
    addr = sender_identity(sender)
    if not addr:
        return ""
    subj = normalized_subject(subject)
    accounts = "+".join(sorted(account_tokens(subject, text)))
    return f"{SOURCE_FACT_KEY_PREFIX}{addr}|{subj}|{accounts}"


def is_source_fact_key(key) -> bool:
    return isinstance(key, str) and key.startswith(SOURCE_FACT_KEY_PREFIX)


def escalation_fact_key(text, sender=None, subject=None) -> str:
    """**The one identity every Watch gate keys on.** A source-derived key
    (:func:`source_fact_key`) when the escalation carries a sender; otherwise the prose bag-of-words
    :func:`fact_key` — a Slack or calendar finding has no email to derive from and is unchanged by
    this. ``""`` when neither identifies anything."""
    return source_fact_key(sender, subject, text) or fact_key(text)


def fact_key_match_title(key) -> str:
    """What a key reads as when the owner's ack WORDS are scored against it (`title_coverage`, key as
    the "title"). A prose key already is its own distinctive-token string. A source key is not — its
    address (``alerts@bank.example``) would tokenize into ``alerts``/``bank``/``example`` noise nobody
    would say — so it becomes the sender's organisation label, the subject tokens and the account token:
    ``source:alerts@bank.example|notice overdraft|x1234`` → ``"bank notice overdraft x1234"``. Total."""
    if not is_source_fact_key(key):
        return key if isinstance(key, str) else ""
    body = key[len(SOURCE_FACT_KEY_PREFIX):]
    addr, _, rest = body.partition("|")
    subj, _, accounts = rest.partition("|")
    parts = []
    domain = addr.rsplit("@", 1)[1] if "@" in addr else addr
    labels = [p for p in domain.split(".") if p]
    if len(labels) >= 2:
        parts.append(labels[-2])  # the organisation label — "bank" of "alerts.bank.example"
    elif labels:
        parts.append(labels[0])
    parts.extend(subj.split())
    parts.extend(a for a in accounts.split("+") if a)
    return " ".join(parts)


def _read_watch_gate_rows(state_dir: str) -> list:
    """Every parseable row of ``state/watch-gate.jsonl``, oldest-line-first. Total and fail-open — an
    absent file, an unreadable one, or an individual line that isn't valid JSON each contribute nothing
    (a bad line is skipped, never fatal to the rest, matching this ledger's own append-only, single-
    writer-per-line shape)."""
    path = os.path.join(state_dir, WATCH_GATE_LOG)
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001 — one bad line costs itself, never the rest
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:  # noqa: BLE001 — absent/unreadable file: no prior rows, not an error
        return []
    return rows


def watch_duplicate_blocked(state_dir: str, text: str, now: datetime | None = None,
                            window_hours: float = WATCH_DEDUPE_WINDOW_HOURS, *,
                            sender=None, subject=None) -> dict | None:
    """**The dedupe gate.** ``None`` = not a duplicate (or the fact can't be identified at all — same
    fail-open convention as :func:`watch_escalation_blocked`). A dict = this exact fact was already
    escalated, un-blocked, inside ``window_hours``::

        {"reason": "duplicate-of:<ts>", "fact_key": str, "duplicate_of": <ts>, "window_hours": float}

    Reads ``state/watch-gate.jsonl`` — the same ledger :func:`log_watch_gate` already writes, no new
    store (a generated read over an existing record beats a hand-synced second copy). A prior row counts
    as "already escalated" only when it was **NOT itself blocked** — a push that never reached the owner
    can't be the thing this one repeats — and only within the window, keeping the MOST RECENT such row
    (`duplicate_of` names it). A row carrying no `fact_key` field has its key derived on the fly from its
    own truncated `text`, so this is retroactive against every row already in the ledger.

    Fail-open on everything: an unidentifiable fact, an unreadable ledger, a raise anywhere → ``None``.
    **This never inspects 🚨/🛑/"Call Me" at all** — it keys on the fact underneath the marker, so a
    genuinely NEW 🚨 about a different fact is structurally unreachable here.

    A source-backed escalation (`sender` given) keys on its SOURCE (:func:`escalation_fact_key`), so a
    peek that re-words one alert email is still a repeat. A prior row matches on EITHER the source key
    or the prose key — a row written before source keys existed carries only the prose one."""
    try:
        key = escalation_fact_key(text, sender, subject)
        if not key:
            return None
        keys = {key, fact_key(text)} - {""}
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        cutoff = instant - timedelta(hours=window_hours)
        best_ts = None
        for row in _read_watch_gate_rows(state_dir):
            if row.get("blocked"):
                continue  # never reached the owner — not a prior escalation of anything
            row_keys = {row.get("fact_key"), row.get("fact_key_text")} - {None, ""}
            if not row_keys:
                row_keys = {fact_key(row.get("text") or "")} - {""}
            if not (row_keys & keys):
                continue
            ts_str = row.get("ts")
            if not isinstance(ts_str, str):
                continue
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001 — an unparseable stamp contributes nothing
                continue
            if not (cutoff <= ts < instant):
                continue
            if best_ts is None or ts > best_ts:
                best_ts = ts
        if best_ts is None:
            return None
        stamp = best_ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"reason": f"duplicate-of:{stamp}", "fact_key": key, "duplicate_of": stamp,
                "window_hours": window_hours}
    except Exception:  # noqa: BLE001 — fail-open: this gate may never be the reason the owner wasn't told
        return None
