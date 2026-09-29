#!/usr/bin/env python3
"""**Which Telegram thread a named kind of message belongs in.** Stdlib only, like every other
daemon module.

## The problem

A day's conversation with the assistant buries its pickers: a merge approval or an open decision
asked at 10:00 is a long scroll away by 16:00, and a picker the owner cannot find is a decision
that cannot be made. **Topics in private chats** answer it exactly: many threads, one chat.
`../docs/telegram-capability-map.md` §2.1 is the survey (Bot API 9.3 private-chat topics; 9.4
bot-created topics). **The chat id does not change**, which is why this is a small module and not
a migration: `TELEGRAM_CHAT_ID`, the poller allowlist, the offset file and every existing record
keep working untouched.

## THE THREE CONSTRAINTS THAT SHAPE EVERYTHING BELOW

1. **The toggle is host-side and no code can flip it.** Private-chat topics are gated on a switch in
   the **@BotFather Mini App** — not the classic `/mybots → Bot Settings` menu (capability map §1).
   So this module cannot assume the feature is on, cannot turn it on, and must be **invisible when
   it is off**: same chat, same payload, no extra send. `topics_enabled` is the runtime detect
   (`getMe.has_topics_enabled`), cached, because asking before every send would spend a round trip
   per picker to learn something that changes about once ever.
2. **THE BOT API HAS NO `getForumTopics`.** A bot cannot enumerate its own topics. So the id this
   module creates exists in exactly one place — `state/telegram-topics.json` — and losing that file
   loses the thread permanently: the next picker creates a *second* "Pull requests" topic and the
   first is orphaned. There is no recovery call to write, which is why the write is atomic and the
   file is never pruned.
3. **FAIL OPEN, ON EVERY PATH, ALWAYS.** Every function here answers *"which thread?"* and the
   honest answer to any failure is `None`, meaning **the main chat**. A picker that does not arrive
   is a decision the owner never gets to make (`telegram_ask.ask`'s own rule), and a picker lost to
   a thread being unavailable would be strictly worse than the scrolling this module exists to end.
   Nothing here raises; nothing here is on the critical path of a send.

## THE DEFAULT IS A TOPIC

:data:`DEFAULT_TOPIC` is `decisions`, and a picker whose caller names no purpose goes there rather
than to the main chat, so **an unanswered decision is findable by scrolling one place**. The default
IS the mechanism: a routing rule that only applies when a caller remembers to opt in routes only the
pickers written by whoever read this file.

Two escapes, and they are not the same thing. An explicit `--topic <purpose>` still wins — that is
how a merge-approval picker lands in `pull-requests`. And :data:`TOPIC_MAIN_CHAT` (`main`) is the
spelling for something that genuinely is not a picker-in-a-channel. **`main` is deliberately NOT a
row in the table**: it resolves through constraint 3's unknown-purpose branch, so *"the main chat, on
purpose"* and *"a typo in a purpose name"* land in exactly the same place — which is the only place
either of them may land, and which is why `--topic` takes no argparse `choices`.

**Constraint 3 covers every picker.** With topics off, with a `getMe` that could not be reached, with
a deleted thread, with a state file that will not parse, with a creation that failed — every picker
goes to the main chat exactly as it would with no topics at all. A default that could cost a picker
would be strictly worse than no topic.

## The rows, and the lines they are drawn on

The shipped table (`../references/telegram-topics.example.json`) has three purposes, drawn on two
different lines:

* `pull-requests` and `decisions` are drawn on **does it wait for an answer** — the merge-approval
  pickers, and every other picker.
* `reminders` is drawn on **does it interrupt**. A nudge answers nothing and waits for nothing; it
  arrives on a schedule the owner did not pick the moment of, in the middle of whatever they were
  saying. It reaches the table only through :func:`reminder_topic`, never through the default.

A purpose reached ONLY by an explicit `--topic <purpose>` at a call site is a third kind — neither a
picker nor an interrupt, just somewhere a class of material is findable — and costs nothing to any
existing caller.

## THE TABLE IS DATA, AND A NEW PURPOSE NEEDS NO CODE CHANGE AT ALL

The purpose → title table is **read from a file, never a literal**: the owner's
`../references/telegram-topics.json` (gitignored, per-install) when it exists, otherwise the shipped
`telegram-topics.example.json`. The well-known purposes keep Python constants because other modules
need to NAME them in code (the merge picker routes to `pull-requests`, every nudge routes through
:func:`reminder_topic`), but nothing requires a purpose to have a constant.

**And a purpose can be minted at runtime with no edit to either table file.** `_load_topic_names`
reads base ∪ overlay: `_load_base_topic_names` (the reference table above) merged with
`_load_overlay_topic_names`, which reads the SAME `STATE_FILE` `create_topic` already writes
`message_thread_id`/`created_at` into — any row there carrying a `name` is a known purpose.
`python telegram_topics.py add <purpose> "<Title>"` mints one and creates its Telegram topic in one
command. `retire` marks a runtime-added purpose so it stops resolving, without deleting its row or
its Telegram-side topic (the no-delete posture below). A purpose in the reference table can only be
retired or renamed by editing that table; the daemon never writes it. Both tables are re-read on
every `thread_id`/`create_topic` call, so a new row resolves with no reimport, and **both share the
fail-open ladder**: a missing, empty, unreadable or malformed file reads as an empty table — the
main chat, never a crash. `_RESERVED_MAIN_CHAT` is filtered out of whatever either file says,
because that invariant is structural and may not depend on a data file being edited correctly.
`../docs/dynamic-topics-spec.md` designs a fuller version (a separate overlay file, promotion back to
the reviewed table); its open questions remain open.

## What it deliberately does NOT do

**No delete and no archive path.** `closeForumTopic`/`reopenForumTopic` do *not* work in private
chats (capability map §2.1), so the only verb short of leaving a topic alone is `deleteForumTopic`,
which is destructive and would be ask-high. **No `editForumTopic`** either:
`ForumTopic.is_name_implicit` exists for threads the *owner* starts and a bot renames; here the
assistant creates the topic, so it is named explicitly at creation and never renamed.

**Telegram only.** Discord is deliberately out of scope — without a flag or a stub waiting for it,
because a half-built second channel is a place the fail-open ladder can rot unobserved.

**Piercing reminders go to the topic too** (:data:`REMINDERS_PIERCING_TO_MAIN_CHAT`, the one line
that holds that decision, and its comment carries the premise to re-check).

**It does not split the conversation cache.** A per-thread continuity cache (keyed off the inbound
`message_thread_id`) is the daemon's concern, and **the two mechanisms deliberately do not meet in
code**: this module answers *which thread does a named PURPOSE belong in* off a state file; the
daemon answers *which thread did THIS MESSAGE arrive in* off the payload. They touch only at the Bot
API parameter they both set, which is what lets a nudge sent into the topic and the `done` typed
under it land in the same per-thread cache with nothing in this module aware of either.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling import also works when we're imported, not run

import mouth  # noqa: E402 — the one-time nudge rides the outbound queue like every other push

DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
STATE_FILE = "telegram-topics.json"
SCHEMA = "seneschal.telegram.topics/1"

#: **Where the routing table itself lives — references/, not `state/`.** The owner's copy is
#: `telegram-topics.json` (gitignored, per-install config); the shipped default is
#: `telegram-topics.example.json`, read only when the owner's copy is absent. Durable vocabulary
#: belongs in a file a human edits, never in a branch in `create_topic`/`thread_id`. Runtime thread
#: ids stay in the gitignored `STATE_FILE` above — the Bot API's missing `getForumTopics` doesn't
#: care where the NAME came from.
REFERENCES_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "references"))
REFERENCES_FILE = "telegram-topics.json"
REFERENCES_EXAMPLE_FILE = "telegram-topics.example.json"

#: The one purpose that must never be a row in the table this file loads — see `_load_topic_names`.
_RESERVED_MAIN_CHAT = "main"


def _load_base_topic_names(references_dir: str = REFERENCES_DIR) -> dict:
    """The REFERENCE table alone, read fresh from `REFERENCES_FILE` (or the example) on every call — never
    cached beyond the process-wide :data:`TOPIC_NAMES` snapshot below, so a caller that passes its
    own `references_dir` (a test, or a future per-request override) sees a row added to THAT file
    with no code change and no reimport. **Tolerant on every path, exactly like `load_state`**: a
    missing file, unparseable JSON, a non-dict body, a `topics` value that isn't a dict, and any
    non-string key or value are each dropped rather than raising. A broken or missing file therefore
    reads as an EMPTY table, which resolves every purpose through the unknown-purpose branch —
    i.e. the main chat — never a crash and never a lost message (module docstring constraint 3).

    **`_RESERVED_MAIN_CHAT` is filtered out even if present in the file** — :data:`TOPIC_MAIN_CHAT`
    must stay out of the registry (module docstring), and a row here would let a future edit to DATA
    alone quietly undo an invariant that is otherwise enforced only by review.

    **Which file:** the owner's `REFERENCES_FILE` when it exists, else the shipped
    `REFERENCES_EXAMPLE_FILE`. Existence decides, not validity — an owner file that exists but will
    not parse reads as an empty table rather than silently resurrecting the example's rows, because
    the owner may have removed a row on purpose.

    This is the edited-by-hand half of :func:`_load_topic_names` — see that function and the module
    docstring for the runtime-minted other half."""
    path = os.path.join(references_dir, REFERENCES_FILE)
    if not os.path.exists(path):
        path = os.path.join(references_dir, REFERENCES_EXAMPLE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    rows = data.get("topics")
    if not isinstance(rows, dict):
        return {}
    return {k: v for k, v in rows.items()
            if isinstance(k, str) and isinstance(v, str) and k != _RESERVED_MAIN_CHAT}


def _load_overlay_topic_names(state_dir: str = DEFAULT_STATE_DIR) -> dict:
    """The purpose -> display-name rows minted at runtime with `add` — no file edit, no reload
    (module docstring). Reads `STATE_FILE`'s own `topics` map, the SAME file
    :func:`create_topic` already writes `message_thread_id`/`name`/`created_at` into for EVERY
    purpose it ever creates, reference or runtime-added — there is no second file, because that one
    already had everywhere a runtime name needed to live. Any row there carrying a non-empty string
    `name` is a known purpose; a row marked `retired: true` (:func:`retire_overlay_purpose`) is
    skipped, which is what makes retiring a purpose stop it from resolving without ever deleting its
    record or the Telegram-side topic it may already have (module docstring: no delete, no archive).

    **Tolerant on every path, exactly like `load_state`**: a missing/unparseable file, a non-dict
    body, a `topics` value that isn't a dict, or a row that isn't itself a dict all read as "nothing
    minted," never a crash. `_RESERVED_MAIN_CHAT` is filtered here too, belt-and-braces with the
    filter in :func:`_load_topic_names` — a `main` row can never reach a caller from either door."""
    try:
        topics = load_state(state_dir).get("topics")
    except Exception:  # noqa: BLE001 — belt and braces; load_state already fails open on its own
        return {}
    if not isinstance(topics, dict):
        return {}
    out = {}
    for purpose, row in topics.items():
        if not isinstance(purpose, str) or purpose == _RESERVED_MAIN_CHAT:
            continue
        if not isinstance(row, dict) or row.get("retired") is True:
            continue
        name = row.get("name")
        if isinstance(name, str) and name:
            out[purpose] = name
    return out


def _load_topic_names(references_dir: str = REFERENCES_DIR,
                       state_dir: str = DEFAULT_STATE_DIR) -> dict:
    """The purpose -> display-name routing table actually resolved against: the reference
    table (:func:`_load_base_topic_names`) merged with the RUNTIME-minted overlay
    (:func:`_load_overlay_topic_names`), **base winning on a shared key** — once a purpose is
    promoted into the reference table, that name must not lose to a stale runtime one that may
    predate it. Both halves share the same tolerant-on-every-path contract, so this function's own
    failure surface is exactly their union: nothing here raises, and a broken/missing file on either
    side costs only that side's rows, never the other's.

    Re-read fresh on every `thread_id`/`create_topic` call rather than trusting the module-level
    :data:`TOPIC_NAMES` snapshot, so a row added to EITHER file resolves with no reimport — see the
    module docstring for why each half exists."""
    merged = {**_load_overlay_topic_names(state_dir), **_load_base_topic_names(references_dir)}
    return {k: v for k, v in merged.items() if k != _RESERVED_MAIN_CHAT}


#: **The well-known purposes, and each row is one purpose.** Key = the purpose a caller names
#: (`telegram_ask.py --topic <purpose>`); value = the topic's title, read from the reference table.
#: The assistant creates these topics, so the names are explicit — `is_name_implicit` is for threads
#: the owner starts, which this module does not handle.
TOPIC_PULL_REQUESTS = "pull-requests"
#: Every picker that is NOT a merge approval: the decisions the assistant is waiting on an answer
#: for. **`Decisions` rather than `Questions`** — the PR topic holds questions too, so a name that
#: described the *shape* would describe both; what actually separates them is that one of them is a
#: merge and the rest are everything else. The strip in a Telegram client shows a short title.
TOPIC_DECISIONS = "decisions"
#: **The reminder nudges** — the first purpose that is NOT a picker. Everything above is routed on
#: *does it wait for an answer*; a nudge does not, and it moves for the other reason — **it
#: interrupts.** A ⏰ nudge landing mid-conversation is the thing being fixed.
#:
#: **This row does not make reminders a picker.** `DEFAULT_TOPIC` stays `decisions`: a caller that
#: names no purpose still gets `decisions`, because a nudge reaches this table through
#: :func:`reminder_topic` and never through the default.
TOPIC_REMINDERS = "reminders"

#: **THE TABLE ITSELF IS DATA, NOT A LITERAL** — read via :func:`_load_topic_names`, so a NEW purpose
#: costs a row in a file, never a code change here. The constants above exist because other modules
#: need to NAME a specific well-known purpose in code; nothing requires a purpose to have a constant —
#: `--topic` has never taken an argparse `choices`, and any string in the table resolves. This
#: snapshot is taken once at import for module-level readers (`telegram_ask.py`'s `--help` text, the
#: tests); `create_topic`/`thread_id` re-read on every call instead of trusting this cached copy, so a
#: caller pointed at a different `references_dir` sees its own file's rows with no reimport.
TOPIC_NAMES = _load_topic_names()

#: **Where a picker goes when its caller names no topic** — the reason the docstring argues it is a
#: mechanism rather than a convenience. An explicit `--topic` wins.
DEFAULT_TOPIC = TOPIC_DECISIONS

#: The reserved purpose meaning *the main chat, deliberately* — for something that is genuinely not
#: a picker-in-a-channel. **It is NOT a row in TOPIC_NAMES and must never become one**: it resolves
#: through :func:`thread_id`'s unknown-purpose branch, which is what makes the explicit spelling and
#: a typo behave identically, and keeps `--topic` free of an argparse `choices` that would turn a
#: mistyped purpose into a picker that never arrives.
TOPIC_MAIN_CHAT = "main"

#: **DOES A PIERCING REMINDER STAY IN THE MAIN CHAT? — THE ONE LINE THAT DECIDES. `False`: THEY GO
#: TO THE TOPIC.**
#:
#: `False` (what ships): **every nudge routes to :data:`TOPIC_REMINDERS`** — Critical,
#: Super-Critical and `Call Me` included. `True`: the piercing ones stay in the main chat while
#: everything else goes to the topic.
#:
#: **The argument for `True` is real and is why this line exists at all.** The piercing set is
#: defined by what it defeats — quiet hours, the night curfew, a day-off hold, a live-session defer —
#: and every one of those gates exists to keep noise out of a place the owner is not looking; a topic
#: could be *another* such place. **The argument that decided it `False`** is a fact about the
#: Telegram client: a message in a private-chat topic is visible in the topic AND in the main
#: conversation view, while a main-chat message appears in the main chat only. The topic is
#: therefore the strictly WIDER audience, so routing a pierce there widens who sees it rather than
#: narrowing it.
#:
#: **THAT PREMISE IS AN OBSERVATION OF THE CLIENT, NOT A BOT API GUARANTEE, AND IT IS WRITTEN DOWN
#: HERE SO IT CAN BE RE-CHECKED.** There is no `getForumTopics` to ask
#: (../docs/telegram-capability-map.md §2.1). The cost of getting the visibility model wrong is a
#: missed critical reminder — so if a Telegram change ever breaks the both-places property, **this is
#: the one line to reconsider**, and this paragraph is the premise to re-check before touching it.
REMINDERS_PIERCING_TO_MAIN_CHAT = False

#: How long a `getMe` answer about `has_topics_enabled` is trusted. Six hours: the toggle is a thing
#: the owner flips by hand at most once, so the only cost this bounds is how long the *first* picker
#: after they flip it keeps going to the main chat. Shorter would spend a round trip per picker for
#: no gain; longer would make the flip feel broken.
DETECT_TTL_SEC = 6 * 3600

#: What the one-time nudge says, and it names WHERE the switch is because that is the part nobody
#: could find: the classic `/mybots → Bot Settings` menu has no topics entry at all (capability map
#: §1), so a nudge that said only "enable topics" would send the owner looking in the wrong place.
TOGGLE_HINT = (
    "Every picker I send you would be much easier to find in its own Telegram topic — merge "
    "approvals in one, every other open decision in another — but topics are off for this bot. "
    "To turn them on: open @BotFather, tap the blue **Bot Settings** / menu "
    "button to launch its **Mini App** (not the old /mybots text menu — the switch is not in there), "
    "pick this bot, and enable topics in private chats. Nothing changes until you do, and nothing "
    "breaks if you never do: every picker keeps arriving in this chat exactly as it does now."
)

#: **Once per boot, structurally.** A module global rather than a persisted counter: the daemon is one
#: long-lived process, so "this process" IS "this boot", and there is no stamp to get wrong. Only
#: the daemon calls :func:`nudge_if_disabled`; the short-lived `telegram_ask.py` subprocesses never
#: do, so a picker can never turn this into a ladder. Tests reset it.
_NUDGED_THIS_BOOT = False


def reminder_topic(pierces: bool) -> str:
    """**Which purpose one reminder nudge belongs to** — :data:`TOPIC_REMINDERS`, for every nudge,
    piercing or not. :data:`REMINDERS_PIERCING_TO_MAIN_CHAT` is
    `False`; were it `True` a piercing nudge would resolve to :data:`TOPIC_MAIN_CHAT` instead, and
    that is the whole of the other answer.

    A pure function of one boolean and one constant: no state, no network, no failure mode. It is a
    separate function rather than an expression at the call site so the split has ONE spelling and
    the constant above has exactly one reader — which is what makes *"flip the line and every nudge
    moves"* a true statement rather than a hopeful one, and a test counts the readers off the
    bytecode to keep it true.

    `pierces` is `sentinel.entry_pierces_quiet`'s answer for the entry, passed down rather than
    recomputed: the fire path already knows, and a second definition of *piercing* is a second thing
    that can drift from the gate it is named after.

    **`TOPIC_MAIN_CHAT` is not a row in :data:`TOPIC_NAMES`**, so a caller that resolves it through
    :func:`thread_id` gets `None` — the main chat — with no `getMe` and no creation. `sentinel` goes
    one better and omits the flag entirely, so that answer's argv is byte-identical to the one it
    built before this existed. That branch is reached only by flipping the constant — it is kept,
    and tested, because it is the other half of a genuinely open decision, not dead code."""
    return TOPIC_MAIN_CHAT if (pierces and REMINDERS_PIERCING_TO_MAIN_CHAT) else TOPIC_REMINDERS


def state_path(state_dir: str) -> str:
    return os.path.join(state_dir, STATE_FILE)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(raw):
    try:
        return datetime.strptime(str(raw), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def load_state(state_dir: str) -> dict:
    """`{"schema", "detect": {...}, "topics": {purpose: {...}}}`. **Tolerant on every path** — a
    missing file, unparseable JSON, or a shape a later version invents all read as empty, which
    means *no topic known* and therefore the main chat. A broken file may cost the thread; it may
    never cost the picker."""
    try:
        with open(state_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        data = None
    if not isinstance(data, dict):
        data = {}
    detect = data.get("detect")
    topics = data.get("topics")
    return {"schema": SCHEMA,
            "detect": detect if isinstance(detect, dict) else {},
            "topics": topics if isinstance(topics, dict) else {}}


def save_state(state_dir: str, state: dict) -> bool:
    """Atomic write; returns whether it landed. **The caller is told**, because of constraint 2: a
    created topic whose id was not written down is a topic that will be created again."""
    try:
        os.makedirs(state_dir, exist_ok=True)
        path = state_path(state_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"schema": SCHEMA, "detect": state.get("detect") or {},
                       "topics": state.get("topics") or {}}, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def topics_enabled(c: dict, state_dir: str, api=None, now=None) -> bool | None:
    """Are private-chat topics switched on for this bot? `True`/`False`, or **`None` for "could not
    tell"** — and the caller treats `None` exactly like `False`, because an unanswerable question
    about a feature is not evidence the feature is there.

    Cached in the state file for :data:`DETECT_TTL_SEC`. `getMe` is in
    `telegram_send.IDEMPOTENT_METHODS`, so the one call this makes retries safely on a flaky
    network and can never duplicate anything.

    **A failed `getMe` is not written to the cache.** Caching *"could not tell"* would turn one
    network blip into six hours of main-chat pickers; leaving the cache alone means the next call
    tries again and an existing good answer keeps being used."""
    state = load_state(state_dir)
    cached = state.get("detect") or {}
    at = _parse_stamp(cached.get("checked_at"))
    if isinstance(cached.get("enabled"), bool) and at is not None:
        age = ((now or datetime.now(timezone.utc)) - at).total_seconds()
        if 0 <= age < DETECT_TTL_SEC:
            return cached["enabled"]
    if api is None:
        from telegram_send import api_call  # lazy: the import graph, not the behaviour
        api = api_call
    try:
        payload = api(c, "getMe", {})
    except Exception:  # noqa: BLE001 — cannot ask ⇒ cannot claim; the caller falls back
        return None
    result = (payload or {}).get("result")
    if not isinstance(result, dict) or not isinstance(result.get("has_topics_enabled"), bool):
        # A bot API older than 9.3, or a proxy answering with a shape we don't recognise. Absent is
        # not False: it is unknown, and unknown falls back the same way without poisoning the cache.
        return None
    enabled = result["has_topics_enabled"]
    state["detect"] = {"enabled": enabled, "checked_at": _stamp(now)}
    save_state(state_dir, state)
    return enabled


def stored_thread_id(state_dir: str, purpose: str):
    """The remembered `message_thread_id` for `purpose`, or `None`. Reads only."""
    row = (load_state(state_dir).get("topics") or {}).get(str(purpose))
    if not isinstance(row, dict):
        return None
    tid = row.get("message_thread_id")
    return tid if isinstance(tid, int) and not isinstance(tid, bool) else None


def purpose_for_thread(state_dir: str, message_thread_id) -> str | None:
    """The REVERSE of `thread_id`: which purpose (if any) is this Telegram thread the topic FOR?

    Built for `channel_declare.py`'s per-turn grounding line (message-routing-spec.md §8 fork 4) —
    telling the model which named topic its inbound `message_thread_id` already resolves to, so a
    channel declaration is an informed choice rather than a guess made blind. Fail-open like every
    other function in this module: never raises, `None` on a missing thread id, an id mapped to no
    stored purpose, or an unreadable state file — the main chat and "unknown" read the same here,
    which is the correct answer for a caller that only wants to know a NAME, not a thread id."""
    try:
        if isinstance(message_thread_id, bool):
            return None
        tid = int(message_thread_id)
    except (TypeError, ValueError):
        return None
    try:
        topics = load_state(state_dir).get("topics") or {}
    except Exception:  # noqa: BLE001 — belt and braces; load_state already fails open on its own
        return None
    for purpose, row in topics.items():
        if isinstance(row, dict) and row.get("message_thread_id") == tid:
            return str(purpose)
    return None


def forget(state_dir: str, purpose: str) -> bool:
    """Drop the remembered id for `purpose`, so the next send creates a fresh topic.

    Called when Telegram **refuses** a send into the stored thread — the owner deleted the topic, or the
    bot was re-pointed at another chat. Returns whether anything was removed. Failing to forget
    costs one rejected send per picker (each of which still lands in the main chat), never a
    message."""
    state = load_state(state_dir)
    if str(purpose) not in (state.get("topics") or {}):
        return False
    state["topics"].pop(str(purpose), None)
    save_state(state_dir, state)
    return True


def create_topic(c: dict, state_dir: str, purpose: str, api=None, now=None, log=None,
                  references_dir: str = REFERENCES_DIR, name: str | None = None):
    """Create the topic for `purpose` and remember its id. Returns the id, or `None`.

    **`createForumTopic` is not idempotent and is deliberately not in `IDEMPOTENT_METHODS`** — every
    successful call makes another thread — so it runs under `telegram_http`'s UNSAFE policy, which
    refuses to retry anything that might already have been delivered. That is the right trade here
    for the same reason it is right for `sendMessage`: an unremembered duplicate topic is visible
    clutter the owner can delete in their client, and this path runs at most once in the life of the
    chat.

    A failed persist does **not** discard the id: the topic exists either way, so using it is
    strictly better than sending to the main chat and creating a third one next time. It is logged
    loudly, because constraint 2 means the id is now only in this process's memory.

    `references_dir`/`state_dir` read `_load_topic_names` fresh rather than the cached module-level
    :data:`TOPIC_NAMES`, so a purpose added to either the reference file or the runtime overlay
    resolves with no code change and no reimport.

    **`name`, if given, is used directly and the lookup is skipped entirely** — the `add` CLI's own
    door, which mints a purpose that may not be in EITHER table yet at the moment of
    creation: `_cmd_add` passes the title it was handed, so the topic can be created and named in one
    command rather than requiring a row to exist first. Every other caller passes no `name` and gets
    today's lookup behaviour, unchanged."""
    if name is None:
        name = _load_topic_names(references_dir, state_dir).get(str(purpose))
    if not name:
        return None
    if api is None:
        from telegram_send import api_call  # lazy, as above
        api = api_call
    try:
        payload = api(c, "createForumTopic", {"chat_id": c.get("chat_id"), "name": name})
    except Exception as e:  # noqa: BLE001 — no topic this time; the picker goes to the main chat
        if log:
            log(f"! telegram topic {purpose!r} could not be created ({e}); "
                f"the message goes to the main chat")
        return None
    result = (payload or {}).get("result")
    tid = result.get("message_thread_id") if isinstance(result, dict) else None
    if not isinstance(tid, int) or isinstance(tid, bool):
        return None
    state = load_state(state_dir)
    state["topics"][str(purpose)] = {"message_thread_id": tid, "name": name,
                                     "created_at": _stamp(now)}
    if not save_state(state_dir, state) and log:
        log(f"! telegram topic {purpose!r} was CREATED as thread {tid} but could not be written to "
            f"{state_path(state_dir)} — the Bot API has no getForumTopics, so the next send will "
            f"create a duplicate topic. Fix the state dir.")
    return tid


def thread_id(c: dict, purpose: str, state_dir: str, api=None, now=None, log=None,
              references_dir: str = REFERENCES_DIR):
    """**The one question this module answers:** which `message_thread_id` should a message for
    `purpose` carry — or `None`, meaning the main chat.

    Never raises, and `None` is a complete, correct answer on every failure: an unknown purpose
    (unknown to the reference table AND the runtime overlay, :func:`_load_topic_names`), topics
    switched off, a `getMe` that could not be reached, an unreadable state file, a creation that
    failed, **a `references_dir` that is missing, unreadable or malformed** — that last one is the
    data file's own fail-open ladder rung, and it costs the same nothing every other rung does. Each
    of those means *"send it where everything goes today"*, which is the behaviour this feature has
    to be invisible against when the @BotFather toggle is off.

    :data:`TOPIC_MAIN_CHAT` is the **named** case of the unknown-purpose branch rather than a case
    of its own, so *"the main chat, on purpose"* costs no code here and cannot drift from what a
    typo does. That equivalence is deliberate — see the module docstring.

    Note what is NOT here: no nudge. This runs inside every `telegram_ask.py` subprocess, and a
    per-picker nudge is exactly the ladder the one-message rule refuses. The nudge is
    :func:`nudge_if_disabled`, called once by the daemon."""
    try:
        if str(purpose) not in _load_topic_names(references_dir, state_dir):
            return None
        if topics_enabled(c, state_dir, api=api, now=now) is not True:
            return None
        tid = stored_thread_id(state_dir, purpose)
        if tid is not None:
            return tid
        return create_topic(c, state_dir, purpose, api=api, now=now, log=log,
                             references_dir=references_dir)
    except Exception as e:  # noqa: BLE001 — belt and braces; every branch above already fails open
        if log:
            log(f"! telegram topic lookup for {purpose!r} failed ({e}); using the main chat")
        return None


def nudge_if_disabled(c: dict, state_dir: str, api=None, now=None, log=None) -> bool:
    """**ONE Telegram line per daemon boot** when private-chat topics are off, and nothing otherwise.
    Returns whether a nudge was queued.

    Loud enough not to be a silent log-and-return, quiet enough not to nag. It exists because the
    switch is in the @BotFather **Mini App**, which no code can reach and which is easy to miss —
    so the alternative to one nudge is a feature that silently never turns on.

    **Silent on "could not tell".** A `getMe` that failed is not evidence the toggle is off, and
    nudging the owner to flip a switch that may already be flipped is worse than saying nothing.

    Fail-open like everything else: an unreachable Mouth costs the line, never the caller."""
    global _NUDGED_THIS_BOOT
    if _NUDGED_THIS_BOOT:
        return False
    try:
        if topics_enabled(c, state_dir, api=api, now=now) is not False:
            return False
        _NUDGED_THIS_BOOT = True   # set BEFORE the enqueue: a failed queue may not become a retry loop
        if mouth.enqueue(state_dir, surface="telegram", kind="nudge", text=TOGGLE_HINT,
                         speaker="daemon") is None:
            if log:
                log("! telegram topics: the toggle nudge could not be queued")
            return False
        if log:
            log("• telegram topics are off for this bot — queued one nudge about the @BotFather "
                "Mini App switch; every picker keeps going to the main chat until it is on")
        return True
    except Exception as e:  # noqa: BLE001 — a nudge may never cost the caller
        if log:
            log(f"! telegram topics nudge check failed (continuing): {e}")
        return False


# --------------------------------------------------------------------- minting at runtime

#: The slug shape `add` accepts — lowercase letters, digits and hyphens, matching
#: `channel_declare.CHANNEL_DECLARATION_RE`'s capture group once lower-cased, so anything minted
#: here is also everything a `[[channel:...]]` declaration could ever name.
_PURPOSE_RE = re.compile(r"^[a-z0-9-]+$")


def _validate_new_purpose(purpose: str, references_dir: str, state_dir: str) -> str | None:
    """`None` if `purpose` may be minted with `add`; otherwise the refusal reason, so the CLI can
    report exactly why rather than guessing. Refuses a purpose that is not the accepted slug shape,
    `_RESERVED_MAIN_CHAT` itself, or one that already resolves through :func:`_load_topic_names`
    (reference OR runtime — minting is create-only, never a silent rename)."""
    if not purpose or not _PURPOSE_RE.match(purpose):
        return "purpose must be lowercase letters, digits and hyphens only"
    if purpose == _RESERVED_MAIN_CHAT:
        return f"{_RESERVED_MAIN_CHAT!r} is reserved for the main chat and can never be a topic"
    if purpose in _load_topic_names(references_dir, state_dir):
        return f"purpose {purpose!r} already exists"
    return None


def retire_overlay_purpose(state_dir: str, purpose: str, now=None,
                            references_dir: str = REFERENCES_DIR) -> str | None:
    """Stop a runtime-added purpose from resolving, without deleting its row or the Telegram-side
    topic it may already have — matching this module's existing no-delete, no-archive posture
    (module docstring). Returns `None` on success, or the refusal reason.

    **A reference-table (base) purpose refuses here.** The daemon never writes the reference table;
    a row there is retired by editing that file, exactly as it is renamed or removed."""
    if purpose in _load_base_topic_names(references_dir):
        return f"purpose {purpose!r} is in the reference table; retire it by editing that file"
    state = load_state(state_dir)
    row = (state.get("topics") or {}).get(str(purpose))
    if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row.get("name"):
        return f"purpose {purpose!r} is not a known runtime-added purpose"
    row = dict(row)
    row["retired"] = True
    row["retired_at"] = _stamp(now)
    state["topics"][str(purpose)] = row
    if not save_state(state_dir, state):
        return f"purpose {purpose!r} could not be written to {state_path(state_dir)}"
    return None


def _cmd_add(args) -> int:
    """`add <purpose> "<Title>"` — validate, then call :func:`create_topic` directly with the title
    it was handed (bypassing the lookup entirely, since neither table knows this purpose yet), so the
    Telegram topic is created and recorded in state in this one command. No file edit anywhere in
    this path — the whole point (module docstring)."""
    reason = _validate_new_purpose(args.purpose, args.references_dir, args.state_dir)
    if reason:
        print(json.dumps({"ok": False, "purpose": args.purpose, "error": reason}, ensure_ascii=False))
        return 2
    import telegram_send as ts  # noqa: PLC0415 — lazy, CLI-only; matches this module's own convention
    creds = ts.load_env(args.env_file)
    c = ts.cfg(creds)
    if args.chat_id:
        c["chat_id"] = args.chat_id
    if not c.get("token") or not c.get("chat_id"):
        print(json.dumps({"ok": False, "purpose": args.purpose,
                          "error": "missing TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                                   "(set in env, --env-file, or --chat-id)"}, ensure_ascii=False))
        return 2
    logs: list[str] = []
    tid = create_topic(c, args.state_dir, args.purpose, name=args.title, log=logs.append,
                       references_dir=args.references_dir)
    for line in logs:
        print(line, file=sys.stderr)
    if tid is None:
        print(json.dumps({"ok": False, "purpose": args.purpose, "title": args.title,
                          "error": "the Telegram topic could not be created; see stderr"},
                         ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "purpose": args.purpose, "title": args.title,
                      "message_thread_id": tid}, ensure_ascii=False))
    return 0


def _cmd_list(args) -> int:
    """`list` — every purpose either table knows, reference or runtime-added, marked with its
    provenance and its stored thread id (if any has ever been created)."""
    base = _load_base_topic_names(args.references_dir)
    overlay_names = _load_overlay_topic_names(args.state_dir)
    overlay_topics = load_state(args.state_dir).get("topics") or {}
    rows = []
    for purpose in sorted(set(base) | set(overlay_names)):
        row = overlay_topics.get(purpose)
        row = row if isinstance(row, dict) else {}
        rows.append({
            "purpose": purpose,
            "name": base.get(purpose, overlay_names.get(purpose)),
            "source": "reference" if purpose in base else "runtime",
            "message_thread_id": row.get("message_thread_id"),
        })
    print(json.dumps({"ok": True, "topics": rows}, ensure_ascii=False))
    return 0


def _cmd_retire(args) -> int:
    """`retire <purpose>` — see :func:`retire_overlay_purpose`."""
    reason = retire_overlay_purpose(args.state_dir, args.purpose, references_dir=args.references_dir)
    if reason:
        print(json.dumps({"ok": False, "purpose": args.purpose, "error": reason}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "purpose": args.purpose, "retired": True}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Split out from `main()` so a test can parse real argv and get the shipped parser's actual
    defaults, the same seam `telegram_ask.build_parser()` offers its own suite."""
    p = argparse.ArgumentParser(
        description="Manage Telegram private-chat topics: mint a purpose at runtime, list every "
                    "known purpose, or retire a runtime-added one.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="seneschal/state")
    p.add_argument("--references-dir", default=REFERENCES_DIR,
                   help="seneschal/references (telegram-topics.json, else the .example.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="mint a purpose and create its Telegram topic now")
    a.add_argument("purpose", help="slug: lowercase letters, digits, hyphens; never 'main'")
    a.add_argument("title", help="the topic's display title in Telegram")
    a.add_argument("--env-file", default=None, help="KEY=VALUE file with TELEGRAM_* settings")
    a.add_argument("--chat-id", default=None, help="override TELEGRAM_CHAT_ID")

    sub.add_parser("list", help="every known purpose, reference and runtime-added, with provenance")

    r = sub.add_parser("retire", help="stop a runtime-added purpose from resolving; never deletes")
    r.add_argument("purpose")
    return p


def main() -> int:
    """The CLI: `add` / `list` / `retire`. **No `rename`.** A purpose already in the reference table
    is renamed by editing that file (the daemon never edits it); a runtime-added purpose
    can be renamed cheaply ONLY before its Telegram topic exists, and once it does, the real topic
    title cannot be changed either way — `editForumTopic` is deliberately never called (module
    docstring: the assistant creates the topic, so it is named explicitly at creation and never
    renamed). That narrow pre-creation case is fully covered by `retire` followed by `add` under
    the corrected name, so a separate `rename` verb would be a second way to do the same thing rather
    than a cheap addition — left out rather than half-built."""
    args = build_parser().parse_args()
    if args.cmd == "add":
        return _cmd_add(args)
    if args.cmd == "list":
        return _cmd_list(args)
    if args.cmd == "retire":
        return _cmd_retire(args)
    return 2  # pragma: no cover — argparse's own `required=True` already refuses an unknown cmd


if __name__ == "__main__":
    sys.exit(main())
