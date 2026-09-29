#!/usr/bin/env python3
"""Ask the owner a question over Telegram with **tappable buttons**. Standard library only.

Claude Code has `AskUserQuestion`, which renders selectable options; Telegram has no equivalent, so
without this module every decision the assistant needs over Telegram arrives as a prose list. That
matters: a picker is one tap, while a prose list is a writing assignment — reading several
questions, holding them in working memory and composing a reply that answers each in order is
exactly the overhead that makes a decision get deferred, and a half-answered question set is worse
than an unasked one, because the assistant proceeds on the half it got.

**One question per message.** N questions = N messages. There is no paginated wizard here, on
purpose.

THE RULE IS ENFORCED IN THE SHAPE, NOT IN A COMMENT. A good picker has three properties, and two of
them are things a caller can simply forget:

  * *every option carries a real description of what it means and what it costs* — so an option with
    no description is **refused** (exit 2). There is no way to ask a bare-labels question through
    this CLI.
  * *the recommendation comes first, marked `(Recommended)`* — so the **first option IS the
    recommendation** and is marked automatically. `--no-recommendation` is the escape hatch for a
    genuinely open pick, and it has to be typed.
  * *multi-select when the choices aren't mutually exclusive* — `--multi` gives toggling buttons plus
    a Done button.

**THE SAME MOVE FOR CITATIONS.** A picker that cites "§8.2" without saying what §8.2 says makes the
owner stop and go look it up — and a rule that says "describe what you cite" is prose, which does not
bind. So a reference in the question or in an option is **resolved against the repo and its text is
inlined by this module**, and a reference that does not resolve **refuses the send**. The caller has
nothing to remember and there is no wording that satisfies the check while leaving the owner without
the text. Mechanism, the shapes it recognises, and why it fails CLOSED where `origin` fails open:
`ask_citations.py`. Relayed text — a PR title, a PR body — goes through `--quote`, which exempts a
SPAN and changes nothing about the layout.

**Where the descriptions live, and why.** A Telegram inline-button label is a phone-width string that
truncates without telling you, so a real description cannot ride on the button. The **message body**
carries the numbered options with their descriptions and the `(Recommended)` mark; the **keyboard is
the selector**, one numbered button per option. The number is what ties a truncated button back to
its full description.

CREDENTIALS: as `telegram_send.py` — `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `TELEGRAM_API_BASE`,
from the environment or an `--env-file`. This module reuses that script's `load_env`/`cfg`/`api_call`
rather than re-implementing them, so there is one HTTP path to the Bot API and one env loader.

DURABILITY. The pending question lives in `../state/telegram-questions.json` (gitignored), because the
daemon reloads on every merge and a question must survive that. **The record is written BEFORE the
send**, with a null `message_id` stamped in afterwards: `callback_data` carries the question id, so
the record is what makes a tap resolvable, while the `message_id` is only needed to fold the answer
back into the message. A crash between the two therefore costs the message edit, never the answer.

EXPIRY: `QUESTION_TTL_DAYS` (7). A week spans a full weekday/weekend cycle, so a question asked Monday
can still be answered the following Sunday; past that the decision's context has almost certainly
moved and answering it as if fresh is worse than asking the owner to restate. Pruning is **lazy** —
every load-modify-save does it — so there is no scheduled task to forget to wire up. An expired
question leaves a **tombstone** (its text, not its options), so a late tap can say *which* question
expired instead of a shrug.

PROVENANCE: every record carries `origin` — `{session_id, source, cwd, pid, stamped, stamped_by}` —
stamped automatically from `$CLAUDE_CODE_SESSION_ID` and `$SENESCHAL_SESSION_SOURCE` at the moment of
the ask, and onto the tombstone when it expires. Nothing has to be typed for it to fire, deliberately:
a picker sent from a headless background surface (a comms peek, a scheduled job) is exactly the one
whose author is hardest to recover afterwards, and the sending process already carries its own
identity in its environment. Full design: `docs/ask-provenance-spec.md`. It is a RECORDED FIELD AND
NOTHING ELSE: it gates nothing, refuses nothing, and changes no part of the approval path.

**And `list` shows it, because a field nobody can read is not a record.** Every row names its author
in short form (`watch(env) · 194e7d43(env)`), `--origin` carries the object verbatim, and the two
absences stay distinct: a record from before the stamp says so, and is never rendered as an unknown
author. `origin.stamped` says WHICH parts were captured — `stamped_by` is inherited from `jobs.py`
and describes `session_id` alone, so on its own it reads `"none"` on a record that did name a source.

**AND A LANDED PICKER LEAVES A ROW IN THE MOUTH.** `state/assertions.jsonl` is the append-only record
of what the assistant has actually said to the owner, and a question is something it said. So `ask()`
appends one `kind: "question"` row after the send lands, carrying the rendered **body** (the wording
the owner received, options and all), the question id in `item_ids`, and the `origin` as the hand.
`record_assertion` never raises; a lost row costs the row and never the picker.

**A QUESTION CAN BE SETTLED WITHOUT A TAP — in exactly two ways, both of them narrow**
(`docs/picker-state-marking-spec.md`). If a tap were the only thing that ever settled a record, two
failures would follow: a send that does not land leaves a phantom copy of a question the owner *did*
answer on its twin, and a question the owner answers in conversation stays pending forever. So:

  * **an answer on one picker settles every OTHER record of the same question** — when the optional
    `merge_guard` module is installed, it decides which records are "the same question" (it owns the
    `(repo, pr, head_sha)` tuple) and this module writes them in one save. Without it there is no
    twin dedupe: the duplicate stays pending until it expires or is settled by hand, which is the
    safe failure.
  * **`settle-in-chat` lets the assistant settle a picker it HEARD the owner answer**, refusing
    without a verbatim quote of what the owner said.

Neither writes `answered_at` and neither touches `selected`: both stamp `retired_at`/
`retired_reason`/`retired_edit` plus **`retired_by`, which says how**, so a tap, a retirement and a
settle are three things a reader — and every future count — can tell apart. **`settle-in-chat` is the
deliberately manual half and nothing may ever call it automatically**: a background mechanism that
concludes *the owner answered this in chat* can withdraw a live decision the owner was never shown. A
test scans the tree for a caller. It is reversible either way — `unsettle` hands the question back —
and neither verb needs credentials, opens a socket, or edits anything on the owner's phone.

**A tap NEVER degrades to a silent no-op.** `answerCallbackQuery` is called on every path — the
client spins until it is — and every path that cannot record an answer still hands the warm session a
line saying so, so the assistant asks in words rather than the tap vanishing.

FAILURE ROWS: the failures this module swallows on purpose — a corrupt store quarantined, a Mouth row
that could not be written after a landed send, a twin settlement that raised, an ask that failed at
the CLI — each leave one `state/failures.jsonl` row via `failures.record`, so a degraded path is
countable rather than silent.

USAGE:
  telegram_ask.py ask --question "Which base?" \\
      --option "origin/develop|The integration branch; the fix is already in it." \\
      --option "main|The release point — a PR here targets the wrong branch." \\
      [--multi] [--no-recommendation] [--chat-id …] [--env-file …] [--dry-run]
      [--origin-session <id>]   # normally unnecessary: the author is auto-stamped
      [--quote "text"]...       # a span of RELAYED text, exempt from citation resolution
      [--footer "text"]         # one line repeated under the last option (a long picker's title,
                                # readable from either end); the citation block stays above it
      [--topic pull-requests]   # a private-chat TOPIC (telegram_topics.py). DEFAULTS to `decisions`;
                                # `--topic main` forces the main chat. ADVISORY IN EVERY DIRECTION:
                                # topics off, or a thread that won't resolve, sends to the main chat
  telegram_ask.py ask --grid --question "3 items need an owner" \\
      --item "loop-a|Add a collapse toggle" --item "loop-b|Fix the flaky test" \\
      --item "loop-c|Draft the Q4 outline" \\
      --choices "Owner|Assistant|Both|External|Unknown"
                                # a GRID: N --item rows x --choices, one pick per row, editable until
                                # Done. No description per item, no (Recommended) mark — see the
                                # GRID MODE section below. [--meta …]/[--dry-run] etc. as above.
  telegram_ask.py resolve --data "q:a1b2c3d4:0" --callback-id <id> [--message-id N] [--chat-id …]
                                # a grid tap's data is "q:<id>:g<row>c<choice>" or "q:<id>:gdone" —
                                # resolve() dispatches on shape, no separate CLI verb needed
  telegram_ask.py list [--all] [--origin]   # pending questions, each naming WHO ASKED it
  telegram_ask.py settle-in-chat --question-id <id> --quote "what the owner actually said"
  telegram_ask.py unsettle --question-id <id>   # hand a settled question back; the tap still works
  telegram_ask.py prune               # force the lazy sweep now; no network

Prints a one-line JSON result. Exit 0 on success, 1 on send/transport failure, 2 on bad arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling import also works when we're imported, not run

import ask_citations  # noqa: E402 — a picker may not cite what it does not show
import failures  # noqa: E402 — a durable row for a failure that would otherwise vanish silently
import mouth  # noqa: E402 — a landed picker leaves a row in the record of what the assistant said
import telegram_format as tf  # noqa: E402 — one converter, one place
import telegram_topics as tt  # noqa: E402 — which thread a named kind of message belongs in
from telegram_send import (  # noqa: E402 — one HTTP path, one env loader, one re-send rule
    SEND_AMBIGUOUS, SEND_REJECTED, TelegramAPIError, api_call, cfg, classify_send_failure, load_env)

try:  # OPTIONAL: the PR merge-approval guard. It owns "which records are the same question".
    import merge_guard  # noqa: E402
except ImportError:  # not installed — `resolve` then settles no twins (see the module docstring)
    merge_guard = None

DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
QUESTIONS_FILE = "telegram-questions.json"
SCHEMA = "seneschal.telegram.questions/1"

#: How long a never-tapped question stays answerable. See the module docstring for why a week.
QUESTION_TTL_DAYS = 7
#: Expired questions keep their TEXT (not their options) so a late tap can name what it was.
TOMBSTONE_CAP = 50
#: A picker with one option is not a question; a phone-width keyboard past ten rows is unusable.
MIN_OPTIONS, MAX_OPTIONS = 2, 10
#: Telegram truncates a long inline-button label silently. The body carries the real text.
BUTTON_LABEL_CHARS = 40
#: How much of the question the warm session's inbound line quotes back. The CHOSEN LABEL is never
#: truncated — it is the answer, and clipping it would drop the only words the tap delivers.
QUESTION_QUOTE_CHARS = 200
#: The Bot API's own cap on `callback_data`. `q:<8 hex>:<idx>` is 13 bytes; the check is a backstop.
CALLBACK_DATA_LIMIT = 64
#: The session id of the process doing the asking. Set by the Claude Code CLI in every session, and
#: already `jobs.ORIGIN_ENV_VAR` — env and the session registry agree on this identifier, so one name
#: means one thing across the tree. **Never a parent join**: the CLI sets this over whatever was
#: passed in (`docs/job-fanout-spec.md`), so the value a process stamps is *its own* session — which
#: is exactly the actor a reader of a bad outbound message wants named.
ORIGIN_ENV_VAR = "CLAUDE_CODE_SESSION_ID"
#: WHICH KIND of assistant surface is asking — `watch` for the headless comms peek, `daemon` for the
#: warm chat spawn, `scheduled` otherwise. The same variable `jobs.SOURCE_ENV_VAR` reads, so one name
#: means one thing across the tree; a picker sent from a background surface is the one whose author a
#: reader most needs, and this is where it comes from.
SOURCE_ENV_VAR = "SENESCHAL_SESSION_SOURCE"
#: The parts of `origin` that name an ACTOR, in the order `list` prints them. `cwd` and `pid` are
#: deliberately not here: the process reports those about itself, so counting them would make every
#: record look attributed while telling a reader nothing about who was driving.
ORIGIN_IDENTIFYING_KEYS = ("source", "session_id")
#: `list`'s author cell for a record written before `origin` existed. **Not "unknown".**
#: The stamp never ran on these, so there was nothing for it to find — a different fact from
#: `stamped_by: "none"`, where it ran and the environment was silent. Rendering both the same way
#: would make the store's own history read as a provenance failure rate, which is the shape of lie
#: this whole spec is about.
NO_STAMP_LABEL = "not recorded (predates ask provenance)"
#: The stamp ran and had nothing to name. This one IS an unknown author.
NO_AUTHOR_LABEL = "unattributed (stamp ran, environment silent)"
#: The Mouth kind for a landed picker (`docs/ask-provenance-spec.md` phase 2). Without it, the
#: append-only record of *what the assistant has actually said to the owner* would hold no questions
#: at all — ask it what was said on a given morning and it would not mention a single picker.
ASSERTION_KIND = "question"
#: What the Mouth row calls this process. `mouth.origin_hand`'s docstring: the `speaker` is the call
#: site's own label for itself and *"is the field to trust"*.
ASSERTION_SPEAKER = "telegram_ask"
#: **A retired picker's message keeps its inline keyboard KEY, emptied.** Omitting `reply_markup`
#: from `editMessageText` also drops the buttons, but then "it cannot be tapped" is a property of an
#: absence two functions away rather than of the payload a test observes. An explicit empty keyboard
#: is the same outcome, spelled where it can be asserted.
EMPTY_KEYBOARD = {"inline_keyboard": []}
#: What a tap on a retired picker is told, after the reason. It says *nothing happened*, never
#: *you were too slow*: the picker went stale on the assistant's side, and a popup that reads as a
#: scolding for answering a question the assistant left up is the wrong lesson for the wrong person.
RETIRED_TAP_TAIL = "Nothing was recorded and nothing needs approving."
#: **`setMessageReaction` — the ONE method that puts a state ON a picker** (Bot API 7.0,
#: `docs/picker-state-marking-spec.md`). Four properties earn it the job
#: and each is load-bearing: **no admin right is needed in a private chat**, so nothing host-side has
#: to be flipped; **"the update isn't received for reactions set by bots"**, so it cannot echo into
#: `telegram_poll`'s inbound path and loop; **it raises no notification**, which is what makes
#: re-marking the whole queue after every merge free; and **a bot may set exactly ONE reaction per
#: message**, so a picker carries one state and never a rank *and* a state. The emoji must be in
#: Telegram's fixed `ReactionTypeEmoji` allow-list (`docs/telegram-inbound-spec.md` §3.4b) — anything
#: else is `Bad Request: REACTION_INVALID`, which is why :func:`mark` decides nothing about which
#: glyph — the marking pass that calls it owns the known-good set.
REACTION_METHOD = "setMessageReaction"
#: **Bot API 400 descriptions that mean the message is ALREADY where a retirement wants it.** All
#: of them are `error_code: 400` with no machine-readable code of their own, so — uniquely in this
#: tree — the `description` string is the only signal there is (`classify_send_failure` reads
#: `.phase` precisely because it *has* one). Matching them is what makes retirement **convergent**:
#: a message the owner deleted by hand, and a message this pass already edited before it crashed, both
#: settle instead of being retried forever. See :func:`classify_edit_failure` for the guard on it.
SETTLED_EDIT_PHRASES = ("message to edit not found", "message is not modified",
                        "message can't be edited", "message_id_invalid")
#: The edit landed: the picker now reads as a record and has no buttons.
EDIT_LANDED = "edited"
#: Telegram answered and refused for a reason that means the message is already settled — gone, or
#: already carrying this exact text. Indistinguishable from success for retirement's purposes.
EDIT_SETTLED = "already-settled"
#: The record never got a `message_id` (the send died between the record and the response), so there
#: is no message to edit and no keyboard that can be tapped.
EDIT_NO_MESSAGE = "no-message"
#: Anything else — a transport failure, an unclassifiable refusal. **Retirement stops here**: the
#: picker is left exactly as it is and the next pass tries again.
EDIT_FAILED = "failed"
#: **A settle that never went near Telegram.** :func:`retire` records which of the three edit
#: outcomes a withdrawal got; a settle made in the store alone attempted none of them, and saying so
#: is what keeps *"the message was never touched"* distinguishable from *"there was no message to
#: touch"* (:data:`EDIT_NO_MESSAGE`). It is also what :func:`unsettle` reads: a picker whose keyboard
#: is still on the owner's phone can be handed back, and one whose message has been edited away cannot.
EDIT_NOT_ATTEMPTED = "not-attempted"
#: **What `retired_by.how` says when the assistant settled a question the owner answered in
#: conversation.** The twin case spells its own value in `merge_guard`, for the reason `retired_by` is
#: opaque here: the module that decides a record is settled owns the word for why, as it does for `meta`.
SETTLED_BY_IN_CHAT = "in-chat"
#: Who an in-chat settle names as the settler. **Never the owner.** The owner gave the answer; the
#: assistant made the claim that they gave it, and a record that cannot tell those two apart is an
#: inference wearing a coat.
IN_CHAT_SETTLER = "assistant"
#: How much of the owner's actual words the settled record's *reason* carries — the string a later tap sees
#: in its popup, which Telegram caps at 200 characters. The **full** quote is kept verbatim on
#: `retired_by.quote`: the reason is a sentence, the record is the archive.
SETTLE_QUOTE_CHARS = 120
RECOMMENDED_MARK = "(Recommended)"
DONE_LABEL = "✔ Done"
CHECKED, UNCHECKED = "☑", "☐"

#: **GRID MODE** — one picker that answers the same small question about several items at once (the
#: first caller is `owi_unknowns.py`'s unknown-owner batches: one drop-down per work item, each
#: offering the same owner choices). Telegram has no dropdowns, so this is a GRID — one inline-keyboard
#: row per item, one button per choice, single pick per row (a later tap in the same row REPLACES the
#: earlier one, never adds to it — drop-down semantics), a `✓` marks the current pick and can be
#: changed freely, and a Done row submits. **A row left untouched submits as unchanged, never as a
#: guessed `Unknown`** — silence is not an answer, the same polarity `resolve`'s multi-select toggle
#: already has for "nothing selected" vs. "not yet touched."
GRID_KIND = "grid"
#: A picker with zero items is not a question; a keyboard past ten rows plus a Done row is unreadable
#: on a phone. `owi_unknowns.py` only ever asks for `MIN(5, remaining)`, well under this ceiling.
MIN_GRID_ITEMS, MAX_GRID_ITEMS = 1, 10
#: The OWI set's seven — five owners (Owner/Assistant/Both/External/Unknown) plus two TERMINAL choices
#: (Archived/Done) — are the only caller today (`owi_unknowns.CHOICES`); the bound exists so a future caller can't
#: silently grow a row past what a phone keyboard can show. Past `GRID_ROW_WIDTH` buttons the row
#: WRAPS (see `build_grid_keyboard`) rather than letting Telegram truncate every label to three
#: characters; the callback encoding (`g<row>c<choice>`) is untouched by the wrap — `row` is the ITEM
#: index, never the keyboard-row index.
MIN_GRID_CHOICES, MAX_GRID_CHOICES = 2, 7
#: The most buttons one Telegram keyboard row can show at phone width before labels like "External"
#: and "Archived" truncate to ellipses. Five fits; seven wraps to 4 + 3.
GRID_ROW_WIDTH = 5
_GRID_CELL_RE = re.compile(r"^g(\d+)c(\d+)$")


# --------------------------------------------------------------------------- the durable store

#: Internal marker on the dict `load_store` returns, never persisted — `save_store` pops it and
#: refuses to write when it is truthy. Set ONLY when a store existed, failed to parse, and could not
#: be moved aside first (see `load_store`'s docstring); never leaked into the on-disk JSON.
_QUARANTINE_FAILED_KEY = "_quarantine_failed"


class CorruptStoreRefused(RuntimeError):
    """`save_store` refused: the store `load_store` returned was built from bytes it could not parse,
    and the original could not be moved aside first — so writing would silently discard whatever was
    really on disk. Move or delete the file by hand, then retry."""


def store_path(state_dir: str) -> str:
    return os.path.join(state_dir, QUESTIONS_FILE)


def _quarantine_unparseable(path: str, now: datetime | None = None) -> bool:
    """Rename an unparseable store aside to `<path>.corrupt-<stamp>` so the bytes survive even though
    they can no longer be read as a store. Loading a corrupt file as `{}` and letting the next
    `save_store` overwrite the real bytes with that empty shape would discard whatever was actually on
    disk. Returns whether the move succeeded; a caller that gets `False` back must not let the eventual
    write land on the original path unquarantined.

    The class of bug this guards against is a common one: a placeholder built to mean "start empty"
    gets trusted as if it meant "confirmed empty," and is then written back over bytes that were never
    actually empty."""
    if not os.path.exists(path):
        return True  # nothing to protect — this is a fresh/missing store, not a corrupt one
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%f") + "Z"
    dest = f"{path}.corrupt-{stamp}"
    try:
        os.replace(path, dest)
        return True
    except OSError:
        return False


def load_store(path: str) -> dict:
    """The pending-question store. Corrupt, missing or wrong-shaped ⇒ start empty rather
    than raise: a broken store must cost a tap its answer (which degrades honestly, loudly, to a
    "tell me in words" line), never take down the send path that is asking the next question.

    **A store that existed and failed to parse is quarantined, not silently discarded** — the file is
    moved to `<path>.corrupt-<stamp>` (its bytes preserved beside the fresh empty store this function
    still returns) and a `failures.jsonl` row is written, rather than loading as `{}` and being
    overwritten by the next `save_store` call. If the quarantine move itself fails (an unwritable
    directory, most likely), `save_store` refuses to write to `path` at all rather than risk landing
    on top of bytes nobody moved — see `_QUARANTINE_FAILED_KEY`."""
    quarantine_failed = False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        state_dir = os.path.dirname(path)
        moved = _quarantine_unparseable(path)
        failures.record(state_dir, "telegram_ask.load_store", "corrupt_store",
                        detail=f"{type(e).__name__}: {e}; quarantined={moved}")
        quarantine_failed = not moved
        data = {}
    if not isinstance(data, dict):
        data = {}
    questions = data.get("questions")
    expired = data.get("expired")
    store = {
        "schema": SCHEMA,
        "questions": questions if isinstance(questions, dict) else {},
        "expired": [e for e in expired if isinstance(e, dict)] if isinstance(expired, list) else [],
    }
    if quarantine_failed:
        store[_QUARANTINE_FAILED_KEY] = True
    return store


def save_store(path: str, store: dict) -> None:
    """Build-then-`os.replace`, never a truncate-write: `state/` is gitignored, so these files exist
    nowhere else and a half-written one is unrecoverable.

    **Refuses outright if the store it was handed came from a quarantine that failed** — see
    `load_store`. That is the one case where writing `path` would land on top of the unparseable
    original nobody managed to move aside, which is the exact data loss this pair of functions exists
    to end."""
    if store.pop(_QUARANTINE_FAILED_KEY, False):
        raise CorruptStoreRefused(
            f"REFUSED to write {path}: its previous contents could not be parsed AND could not be "
            f"moved aside, so writing now would discard them. Move or delete the file by hand.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _parse_stamp(raw) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def prune_store(store: dict, now: datetime | None = None) -> int:
    """Retire every question older than the TTL into a tombstone, in place. Returns how many went.

    Lazy rather than scheduled, deliberately: every read and every write runs it, so the sweep cannot
    be the thing nobody wired up (`dream_steps.py`'s whole reason for existing). A record we cannot
    date is retired too — an undateable question is one we can never prove is still current, and the
    tombstone path is honest about that where silently keeping it forever would not be.

    **The tombstone carries `origin` and drops everything else** (`docs/ask-provenance-spec.md` §4.2).
    The tombstone is what survives the week, and an expired ask nobody answered is precisely the case
    where *"who asked this?"* is hardest and matters most — the options are gone by design, the author
    is the one thing still worth keeping. A record written before `origin` existed has none, and gets
    none: absent stays absent rather than being guessed at."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=QUESTION_TTL_DAYS)
    retired = 0
    for qid, q in list(store["questions"].items()):
        at = _parse_stamp((q or {}).get("asked_at"))
        if at is not None and at > cutoff:
            continue
        store["questions"].pop(qid, None)
        stone = {"id": qid, "question": (q or {}).get("question", ""), "expired_at": _stamp(now)}
        origin = record_origin(q)
        if origin is not None:
            stone["origin"] = origin
        store["expired"].append(stone)
        retired += 1
    if len(store["expired"]) > TOMBSTONE_CAP:
        store["expired"] = store["expired"][-TOMBSTONE_CAP:]
    return retired


def tombstone(store: dict, qid: str) -> dict | None:
    for entry in reversed(store["expired"]):
        if entry.get("id") == qid:
            return entry
    return None


# --------------------------------------------------------------------------- rendering

def parse_option(raw: str) -> dict:
    """``"Label|What it means and what it costs"`` → an option record.

    The pipe splits **once**, so a description may contain pipes and a label may not. A missing or
    empty description raises — the rule made into a shape a violation cannot fit into, the same move
    `job_analysis.py`'s schema makes for "point, don't diagnose"."""
    label, sep, description = (raw or "").partition("|")
    label, description = label.strip(), description.strip()
    if not label:
        raise ValueError(f"option has no label: {raw!r}")
    if not sep or not description:
        raise ValueError(
            f"option {label!r} has no description. Every option needs one — what it means and what it "
            f'costs. Format: --option "Label|Description"')
    return {"label": label, "description": description}


def parse_grid_item(raw: str) -> dict:
    """``"id|label"`` -> a grid row. Unlike :func:`parse_option`, no description is required — a grid
    row is a data item (an OWI work item's own text), not a choice the assistant is recommending, so
    the "every option needs a real description" rule (`parse_option`'s whole reason to raise) does not
    apply to it."""
    item_id, sep, label = (raw or "").partition("|")
    item_id, label = item_id.strip(), label.strip()
    if not item_id:
        raise ValueError(f"grid item has no id: {raw!r}")
    if not sep or not label:
        raise ValueError(f"grid item {item_id!r} has no label. Format: --item \"id|label\"")
    return {"id": item_id, "label": label}


def render_body(question: str, options: list, multi: bool, recommend: bool,
                footer: str | None = None) -> str:
    """The message text: the question, how to answer it, then the numbered options with their real
    descriptions. This is where the content lives; the keyboard below it is only the selector.

    `footer` is one optional line placed under the last option, after a blank line —
    a caller's own repeat of its title line, so a picker long enough to scroll can be read from
    either end. It is text like the rest of the body: the citation gate scans it and the length
    arithmetic in :func:`ask` counts it. `None`, the default, renders no footer at all."""
    lines = [question.strip(), ""]
    lines.append("Tap all that apply, then Done." if multi else "Tap one.")
    for i, opt in enumerate(options):
        mark = f"  {RECOMMENDED_MARK}" if recommend and i == 0 else ""
        lines.append("")
        lines.append(f"{i + 1}. {opt['label']}{mark}")
        lines.append(f"   {opt['description']}")
    return "\n".join(lines) + footer_band(footer)


def footer_band(footer) -> str:
    """`"\\n\\n<footer>"`, or `""` for no footer. One spelling, so :func:`ask` can place the citation
    block ABOVE the footer and still count the footer's characters against the same cap."""
    text = (footer or "").strip()
    return f"\n\n{text}" if text else ""


def render_grid_body(question: str, items: list) -> str:
    """The grid's message text: the question, how to answer it, then the numbered items — no
    descriptions (see :func:`parse_grid_item`) and no `(Recommended)` mark, since a grid row is not a
    ranked choice. The keyboard below carries the choices; this is only the numbered index the
    truncation-prone buttons tie back to, same split `render_body` makes."""
    lines = [question.strip(), "", "Tap each item's owner below, then Done."]
    for i, item in enumerate(items):
        lines.append("")
        lines.append(f"{i + 1}. {item['label']}")
    return "\n".join(lines)


def _button_label(idx: int, opt: dict, multi: bool, selected) -> str:
    text = f"{idx + 1}. {opt['label']}"
    if multi:
        text = f"{CHECKED if idx in (selected or []) else UNCHECKED} {text}"
    if len(text) > BUTTON_LABEL_CHARS:
        text = text[:BUTTON_LABEL_CHARS - 1].rstrip() + "…"
    return text


def callback_data(qid: str, choice) -> str:
    data = f"q:{qid}:{choice}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:  # a backstop; ids are 8 hex chars
        raise ValueError(f"callback_data over Telegram's {CALLBACK_DATA_LIMIT}-byte cap: {data!r}")
    return data


def build_keyboard(qid: str, options: list, multi: bool = False, selected=None) -> dict:
    """One button per row: descriptions are long, labels can be too, and side-by-side buttons truncate
    on a phone. Multi-select adds the checkbox prefixes and a final Done row — the toggles change
    nothing until Done is tapped, which is what makes an accidental tap free to undo."""
    rows = [[{"text": _button_label(i, opt, multi, selected), "callback_data": callback_data(qid, i)}]
            for i, opt in enumerate(options)]
    if multi:
        rows.append([{"text": DONE_LABEL, "callback_data": callback_data(qid, "done")}])
    return {"inline_keyboard": rows}


def grid_callback_data(qid: str, row: int | None, choice) -> str:
    """``"q:<qid>:g<row>c<choice>"`` for a cell, ``"q:<qid>:gdone"`` for Done. Deliberately a
    **different third-segment shape** from :func:`callback_data` (which is a bare int or the literal
    `"done"`) rather than a fourth colon-separated field — `parse_callback_data`'s `len(parts) != 3`
    check and its `int(parts[2])` conversion both already fail closed on `"g2c3"`/`"gdone"`, so a
    classic and a grid tap can never be confused for one another without either parser changing."""
    payload = "gdone" if choice == "done" else f"g{row}c{choice}"
    data = f"q:{qid}:{payload}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:  # a backstop; ids are 8 hex chars
        raise ValueError(f"callback_data over Telegram's {CALLBACK_DATA_LIMIT}-byte cap: {data!r}")
    return data


def build_grid_keyboard(qid: str, items: list, choices: list, grid_selected: dict | None = None) -> dict:
    """One row per item, one button per choice — a drop-down per item rendered as buttons, since
    Telegram has none. `grid_selected` (``{"<row>": <choice index>}``, string keys because it
    round-trips through JSON) marks the current pick with a leading `✓`; a row absent from it is
    untouched, exactly as `build_keyboard`'s multi-select toggles mark a checkbox only for a selected
    index. A Done row, same label as the multi-select's, closes the grid. An item whose choices
    outnumber `GRID_ROW_WIDTH` spans two keyboard rows (`_wrap_row`) — its `callback_data` still
    names the ITEM index, so `grid_selected` and `_resolve_grid` never learn the keyboard wrapped."""
    grid_selected = grid_selected or {}
    rows = []
    for i, item in enumerate(items):
        picked = grid_selected.get(str(i))
        row = []
        for c_idx, choice in enumerate(choices):
            text = f"{CHECKED} {choice}" if picked is not None and int(picked) == c_idx else choice
            row.append({"text": text, "callback_data": grid_callback_data(qid, i, c_idx)})
        rows.extend(_wrap_row(row))
    rows.append([{"text": DONE_LABEL, "callback_data": grid_callback_data(qid, None, "done")}])
    return {"inline_keyboard": rows}


def _wrap_row(buttons: list) -> list:
    """One item's buttons as one keyboard row when they fit in `GRID_ROW_WIDTH`, else as the fewest
    near-equal rows that do (7 -> 4 + 3, never 5 + 2). Purely a layout decision: every button keeps
    the `callback_data` it was built with, so a wrapped row taps exactly like an unwrapped one."""
    n = len(buttons)
    if n <= GRID_ROW_WIDTH:
        return [buttons]
    parts = -(-n // GRID_ROW_WIDTH)  # ceil
    base, extra = divmod(n, parts)
    out, start = [], 0
    for k in range(parts):
        size = base + (1 if k < extra else 0)
        out.append(buttons[start:start + size])
        start += size
    return out


def parse_callback_data(data: str):
    """``"q:<qid>:<choice>"`` → ``(qid, choice)`` where choice is an int index or ``"done"``.
    ``(None, None)`` for anything else — a button from some other feature, or a mangled payload."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "q" or not parts[1]:
        return None, None
    if parts[2] == "done":
        return parts[1], "done"
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None, None


def parse_grid_callback_data(data: str):
    """``"q:<qid>:g<row>c<choice>"`` -> ``(qid, row, choice)``; ``"q:<qid>:gdone"`` ->
    ``(qid, None, "done")``. ``(None, None, None)`` for anything else — including every classic
    payload, since those never start their third segment with `"g"`."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "q" or not parts[1]:
        return None, None, None
    payload = parts[2]
    if payload == "gdone":
        return parts[1], None, "done"
    m = _GRID_CELL_RE.match(payload)
    if not m:
        return None, None, None
    return parts[1], int(m.group(1)), int(m.group(2))


def chosen_labels(q: dict) -> list:
    options = q.get("options") or []
    return [options[i]["label"] for i in sorted(q.get("selected") or [])
            if isinstance(i, int) and 0 <= i < len(options)]


def render_answered(q: dict) -> str:
    """The original body with the choice folded in underneath, so the question becomes its own record
    in the scrollback instead of leaving a dead keyboard behind."""
    labels = chosen_labels(q)
    tail = "\n".join(f"✓ {lbl}" for lbl in labels) if labels else "✓ (nothing selected)"
    return f"{(q.get('body') or q.get('question') or '').rstrip()}\n\n{tail}"


def _send_text(api, c: dict, method: str, params: dict, source: str):
    """Send `source` as the `text` of `method`, converting Markdown → Telegram HTML when the format
    knob says so — and **retrying once, plain, with the original string, if Telegram ANSWERED AND
    REFUSED it**.

    A question body carries the assistant's prose: the question itself and every option's *"what it
    means and what it costs"* description. Those are written in Markdown like everything else, and
    would otherwise arrive with literal `**` in them.

    **AN AMBIGUOUS FAILURE IS RE-RAISED, NOT RE-SENT.** A blanket `except Exception` would catch the
    one failure `telegram_http` refuses to retry — the request went out, the read timed out, the
    picker may already be on the owner's phone — and re-send it anyway. Here the duplicate is a
    **second live picker for the same question**: two buzzes, two keyboards, and an answer on one
    that leaves the other pending. Classification is
    `telegram_send.classify_send_failure`, so there is one answer to *"may these words go out
    again?"* in the tree, not two. A caller sees the raise; the record in
    `telegram-questions.json` is already written, so a question that may have landed stays
    resolvable if it did.

    Deliberately NOT chunked, unlike `telegram_send.send_text`. A body over 4096 characters would
    have to be split away from its own keyboard, and there is no sane way to leave half a picker on
    screen — with ten options capped at a phone-readable description each, this is unreachable in
    practice, and the plain retry still covers it if a caller ever manages it."""
    html = tf.render_for_api(source, c.get("format"))
    if html is None:
        return api(c, method, {**params, "text": source, "parse_mode": c.get("parse_mode") or None})
    try:
        return api(c, method, {**params, "text": html, "parse_mode": "HTML"})
    except Exception as e:  # noqa: BLE001 — classified, then acted on; never blindly re-sent
        if classify_send_failure(e) == SEND_AMBIGUOUS:
            print(f"! telegram {method} failed AFTER the request went out; NOT re-sending as plain "
                  f"(a blind retry here duplicates the picker): {e}", file=sys.stderr)
            raise
        print(f"! telegram HTML rejected on {method}; re-sending as plain text: {e}", file=sys.stderr)
        return api(c, method, {**params, "text": source, "parse_mode": None})


#: `[text](url)` — the same shape `telegram_format` links. Only used to un-mark a quoted question.
_MD_LINK_RE = re.compile(r"\[([^\[\]]*)\]\(([^()\s]+)\)")


def _quote(s: str, cap: int = QUESTION_QUOTE_CHARS) -> str:
    """The question, short and PLAIN, for the line the warm session reads. Link markup collapses to
    its text and `**` is dropped (a merge picker's title line carries both), so a
    200-character quote is not spent on two URLs. A question with neither is byte-identical."""
    s = _MD_LINK_RE.sub(r"\1", s or "").replace("**", "")
    s = " ".join(s.split())
    return s if len(s) <= cap else s[:cap].rstrip() + "…"


def answer_line(q: dict) -> str:
    """The line the warm session reads, in the established bracketed style — everything in brackets is
    the daemon describing, never the owner speaking (same shape as `[the owner reacted …]`).

    The question is quoted short; **the chosen labels never are.** They are the answer, and clipping
    them would drop the only words the tap exists to deliver."""
    labels = chosen_labels(q)
    picked = ", ".join(f'"{lbl}"' for lbl in labels) if labels else \
        "nothing — the owner tapped Done with no options selected"
    return f'[the owner answered "{_quote(q.get("question", ""))}" → {picked}]'


def render_grid_answered(q: dict, answers: dict, untouched: list) -> str:
    """`render_answered`'s grid twin: the original body with every row's outcome folded in — a picked
    row shows what the owner chose, an untouched row says so explicitly rather than being silently dropped,
    because untouched is a real, distinct outcome (`ask_grid`'s docstring: silence is not an answer)."""
    by_id = {item["id"]: item["label"] for item in (q.get("items") or [])}
    lines = [(q.get("body") or q.get("question") or "").rstrip(), ""]
    for item_id, choice in answers.items():
        lines.append(f"{CHECKED} {by_id.get(item_id, item_id)}: {choice}")
    for item_id in untouched:
        lines.append(f"{UNCHECKED} {by_id.get(item_id, item_id)}: left unchanged")
    return "\n".join(lines)


def grid_answer_line(q: dict, answers: dict, untouched: list) -> str:
    """`answer_line`'s grid twin — everything in brackets is the daemon describing, never the owner
    speaking, same convention. Names what was picked and how many rows were left untouched; never
    invents a choice for a row the owner didn't tap."""
    by_id = {item["id"]: item["label"] for item in (q.get("items") or [])}
    picked = "; ".join(f"{by_id.get(k, k)} → {v}" for k, v in answers.items())
    bits = [picked] if picked else []
    if untouched:
        bits.append(f"{len(untouched)} left unchanged")
    body = ", ".join(bits) if bits else "nothing — the owner tapped Done with no rows touched"
    return f'[the owner answered a grid picker ("{_quote(q.get("question", ""))}") → {body}]'


# --------------------------------------------------------------------------- origin (the author)

def build_origin(*, session_id: str | None = None, env=None) -> dict:
    """Who is asking, stamped from the environment and this process. `docs/ask-provenance-spec.md`
    phase 1.

    ```
    origin.session_id  =  session_id (the --origin-session flag)  # explicit, wins  -> stamped_by "flag"
                       |  $CLAUDE_CODE_SESSION_ID                 # auto, the default -> stamped_by "env"
                       |  (absent)                                # -> stamped_by "none"
    origin.source      =  $SENESCHAL_SESSION_SOURCE               # "watch" / "daemon" / "scheduled"
    origin.cwd, .pid   =  this process
    origin.stamped     =  {part: which door it came through}      # ONLY the parts actually captured
    ```

    **`stamped` is why `stamped_by` alone is not a census.** `stamped_by` was inherited
    from `jobs.py`, where it describes **`session_id` and nothing else** — and it still does here,
    unchanged, so every record already written means exactly what it meant when it was written. But
    carried over verbatim it produces `stamped_by: "none"` on a record carrying `source: "watch"`,
    which is **precisely the unattributed-picker shape** if the CLI ever stops exporting
    `$CLAUDE_CODE_SESSION_ID`: a surface *was* recorded and a census keyed on `stamped_by` would
    count the record as unattributed and never look. So `stamped` states the same fact about the
    whole author rather than one field of it — `{"session_id": "env"|"flag", "source": "env"}`, each
    key present only when that part was really read, `{}` when nothing was. Count
    `origin["stamped"].get("source")`, not `stamped_by`, when you want to know whether the *sender*
    is on the record. Read it through :func:`origin_stamped`, which returns ``None`` — never `{}` —
    for the records written before this field existed, so "we did not capture it" and "we did not yet
    ask" stay separable one layer further back.

    **Auto is the mechanism; the flag is only an override.** The alternative — requiring `--meta` with
    a source field — is the shape this repo has already measured and rejected: `jobs.py`'s own
    docstring on why `origin` is auto-stamped says a field a caller must REMEMBER to populate is not a
    mechanism. The caller most worth attributing is a model typing a command line mid-turn, and that
    caller forgets.

    **The environment already knows.** A background surface that sends (a comms peek, a scheduled
    job) carries `SENESCHAL_SESSION_SOURCE` in its own environment, and `telegram_send.py` reads the
    same variable to arm its ack gate. This module is the other send path reached by the same
    surfaces, so it reads it too — otherwise a picker sent from a peek would leave nothing on the
    record naming the sender, and the attribution would exist only in a transcript outside the repo.

    **Absent, never invented.** A field that cannot be read is left out; `stamped_by` says which of
    "found nothing" and "never ran" you are looking at, so a later census can count
    `stamped_by == "none"` instead of guessing at a missing key. Same rule as
    "absent is not empty" — `(None, err) != ([], None)`.

    Never raises. Every read is individually guarded and the caller wraps the whole thing besides:
    losing the author is cosmetic, losing the picker is not."""
    env = os.environ if env is None else env

    def _env(name: str) -> str | None:
        try:
            return (env.get(name) or "").strip() or None
        except Exception:  # noqa: BLE001 — an exotic mapping must not cost the send
            return None

    sid = None
    try:
        sid = (session_id or "").strip() or None
    except Exception:  # noqa: BLE001
        sid = None
    stamped_by = "flag" if sid else None
    if sid is None:
        sid = _env(ORIGIN_ENV_VAR)
        if sid:
            stamped_by = "env"

    origin: dict = {}
    stamped: dict = {}
    if sid:
        origin["session_id"] = sid
        stamped["session_id"] = stamped_by
    source = _env(SOURCE_ENV_VAR)
    if source:
        origin["source"] = source
        stamped["source"] = "env"
    origin["stamped"] = stamped
    try:
        origin["cwd"] = os.getcwd()
    except Exception:  # noqa: BLE001 — a deleted cwd is a real OSError on POSIX
        pass
    try:
        origin["pid"] = os.getpid()
    except Exception:  # noqa: BLE001
        pass
    origin["stamped_by"] = stamped_by or "none"
    return origin


def stamp_origin(session_id: str | None = None, env=None) -> dict:
    """:func:`build_origin` behind a total try. The stamp may cost itself and nothing else — the
    contract `mouth.py` writes each of its records under, for its reason: a record that fails to say
    who wrote it is a worse record; a picker that fails to arrive is a decision the owner never gets
    to make."""
    try:
        return build_origin(session_id=session_id, env=env)
    except Exception:  # noqa: BLE001 — see the docstring; there is no failure mode worth a raise here
        return {"stamped": {}, "stamped_by": "none"}


def record_origin(rec) -> dict | None:
    """The `origin` on a stored question, or ``None`` for the records written before it existed.
    **Reading must tolerate its absence** — records written before the stamp carry no author and are
    deliberately not migrated (nothing in one says who sent it, so a migration would guess, and a
    wrong guess is worse than a blank)."""
    if not isinstance(rec, dict):
        return None
    origin = rec.get("origin")
    return origin if isinstance(origin, dict) and origin else None


def origin_stamped(rec) -> dict | None:
    """Which identifying parts of the author were captured, and through which door — or ``None`` for
    a record written before `origin.stamped` existed.

    **Never synthesized from `stamped_by`.** `stamped_by` would let a reader recover `session_id`'s
    door and nothing about `source`, so filling the rest in would invent exactly the fact this field
    was added to make countable. ``None`` means *this record cannot answer*; `{}` means *it answered,
    and the answer was nothing*. Same `(None, err) != ([], None)` rule the rest of the stamp follows."""
    origin = record_origin(rec) or {}
    stamped = origin.get("stamped")
    return dict(stamped) if isinstance(stamped, dict) else None


def assertion_hand(origin) -> dict:
    """Phase 1's `origin` projected into the Mouth's `origin_hand` shape, for the phase-2 row.

    **Built here rather than letting `mouth.origin_hand()` run, and the difference is the point.**
    That function reads `source` out of the **session registry**, which answers `build` for a warm
    turn and *nothing at all* for a headless Watch peek — the peek has no registry entry. This
    module's `source` comes from `$SENESCHAL_SESSION_SOURCE`, set on that exact process. They are two
    different facts under one key name, and
    the one worth recording at a send site is **the sender's own surface**.

    So the hand carries phase 1's object verbatim beside a `speaker`, rather than a lossy copy:

      * `speaker` — this module, the label `mouth` says to trust;
      * `source` / `session_id` / `cwd` / `pid` — as phase 1 stamped them, absent if absent;
      * `stamped` / `stamped_by` — carried so a census over `assertions.jsonl` can ask the same
        question `telegram_ask.py list` answers, without joining back to a store that self-liquidates
        in 7 days. The tombstone keeps `origin`; it does not keep the wording.

    Never raises — an unreadable origin costs the enrichment, not the row and certainly not the send.
    """
    hand = {"speaker": ASSERTION_SPEAKER, "source_from": SOURCE_ENV_VAR}
    try:
        if isinstance(origin, dict):
            hand.update({k: v for k, v in origin.items() if v is not None})
    except Exception:  # noqa: BLE001 — see the docstring
        pass
    return hand


def _short_session(sid: str) -> str:
    """The 8-hex prefix every ask id, job id and this spec's own prose uses to name a session."""
    head = str(sid).split("-")[0]
    return head if head else str(sid)


def origin_summary(rec) -> str:
    """One cell naming who asked, for `list`. Lossy by design — the full object rides `--origin`.

    Degrades honestly at each layer rather than collapsing to one word, because the layers are
    different facts and a reader acting on "unknown" would be acting on the wrong one:

      * ``watch(env) · 194e7d43(env)``  — stamped, both parts, doors named
      * ``watch(env)``                  — the mixed case: a surface recorded, no session id to go
        with it. **This is the shape a `stamped_by` census misses.**
      * ``194e7d43(flag)``              — hand-supplied via `--origin-session`
      * ``watch · 194e7d43``            — stamped before `origin.stamped` shipped; the parts are
        known, the doors are not, so no door is printed rather than guessed
      * :data:`NO_AUTHOR_LABEL`         — the stamp ran and the environment was silent
      * :data:`NO_STAMP_LABEL`          — the record predates the stamp entirely
    """
    origin = record_origin(rec)
    if origin is None:
        return NO_STAMP_LABEL
    stamped = origin_stamped(rec) or {}
    bits = []
    for key in ORIGIN_IDENTIFYING_KEYS:
        value = origin.get(key)
        if not value:
            continue
        shown = _short_session(value) if key == "session_id" else str(value)
        door = stamped.get(key)
        bits.append(f"{shown}({door})" if door else shown)
    return " · ".join(bits) or NO_AUTHOR_LABEL


# --------------------------------------------------------------------------- ask

def ask(c: dict, state_dir: str, question: str, options: list, multi: bool = False,
        recommend: bool = True, dry_run: bool = False, api=None, now: datetime | None = None,
        meta: dict | None = None, origin_session: str | None = None,
        quoted: list | None = None, message_thread_id=None,
        head: ask_citations.PRHead | None = None, footer: str | None = None) -> dict:
    """Send one question and record it. `api` is the test seam — the real `api_call` by default, so no
    test can reach the wire without saying so.

    **`message_thread_id` is the topic seam and THIS function's default is still OFF**
    (`telegram_topics.py`). `None` means the picker goes where every picker has always gone, and the
    key is then absent from the send params entirely rather than present-and-null — so with
    private-chat topics switched off (the @BotFather Mini App toggle no PR can flip) the payload
    Telegram receives is byte-identical to the one it received before this parameter existed.

    **The `decisions`-topic default lives at the CLI, not here, and the split is deliberate.** This
    function takes a *resolved thread id*; resolving one is a network call that can CREATE a topic,
    which is exactly what `--dry-run` promises not to do and what the `api=` test seam exists to
    keep out of the suite. So `_cmd_ask` resolves `telegram_topics.DEFAULT_TOPIC` and hands the
    answer down, every real caller reaches this file through that door, and a `None` here still
    means the main chat for the same reasons it always did.

    `meta` is opaque caller data carried on the record and handed back by :func:`resolve`, so a tap
    can mean something to the daemon beyond a line of text. **This module never interprets it** — it
    stores it and gives it back. The dispatch lives in the daemon's callback path, which is what
    keeps this file a picker rather than a place effects accumulate (e.g. an optional merge guard
    writing its approval record when the owner taps Approve).

    `head` rides straight through to `ask_citations.annotate` — a closed allow-list of paths that may
    resolve against a PR's own head when the checkout does not have them yet. `None`, the default,
    resolves against the checkout only.

    **`origin` is written unconditionally and is not `meta`.** `meta` is opaque and optional in both
    directions, which is exactly why it cannot be where an author lives: an omittable field is not a
    mechanism (see :func:`build_origin`). `origin` is a separate, non-omittable key beside it, stamped
    from the environment; `origin_session` (the `--origin-session` flag) is only for a caller that
    knows better than its own environment does. Nothing about the send, the keyboard, the tap, the
    resolve path or the approval gate changes — provenance is a recorded field and nothing else."""
    api = api or api_call
    qid = uuid.uuid4().hex[:8]
    # THE CITATION GATE, and it is HERE rather than in `_cmd_ask` on purpose: the daemon and other
    # callers shell out to this file, but a future in-process caller would bypass an
    # argument-parsing check entirely. Raising from the one function that builds a picker is what
    # makes the refusal unbypassable, which is the same reason `parse_option` raises rather than
    # warns. It runs BEFORE the durable record and before the dry-run branch, so `--dry-run` shows
    # the refusal and a refused question is never half-written to the store.
    #
    # **The footer stays LAST.** `annotate` appends the citation block to whatever it
    # is handed, and a merge picker cites its blocker paths routinely — so a footer rendered inside
    # the body would sit above the excerpts, which defeats "read it from either end". The gate is
    # therefore run over body + footer (the footer is scanned and its characters are counted, same
    # as any other line), and the block is placed BETWEEN them. `footer=None` reproduces `annotate`
    # exactly, so every existing caller renders byte-identically.
    rendered = render_body(question, options, multi, recommend)
    tail = footer_band(footer)
    room = ask_citations.TELEGRAM_MESSAGE_CHARS - ask_citations.CITATION_SAFETY_MARGIN \
        - len(rendered) - len(tail)
    block = ask_citations.citation_block(rendered + tail, quoted=quoted, room=room, head=head)
    body = rendered + block + tail
    keyboard = build_keyboard(qid, options, multi, selected=[])
    record = {"question": question.strip(), "body": body, "options": options, "multi": bool(multi),
              "recommended": bool(recommend), "chat_id": str(c.get("chat_id") or ""),
              "message_id": None, "asked_at": _stamp(now), "selected": [], "answered_at": None,
              "origin": stamp_origin(origin_session)}
    if meta is not None:
        record["meta"] = meta

    if dry_run:
        # The stamp rides the dry run too, so "who would this be attributed to?" is answerable
        # without sending anything — the same reason --dry-run renders the body and the buttons.
        return {"ok": True, "dry_run": True, "question_id": qid, "body": body,
                "buttons": [b[0]["text"] for b in keyboard["inline_keyboard"]], "meta": meta,
                "origin": record["origin"]}

    # Persist BEFORE the send, with a null message_id stamped in after. `callback_data` carries the
    # question id, so THIS record is what makes a tap resolvable; the message_id only fuels the
    # fold-the-answer-back edit. A crash between the two costs the edit, never the answer.
    path = store_path(state_dir)
    store = load_store(path)
    prune_store(store, now)
    store["questions"][qid] = record
    save_store(path, store)

    def send_params(thread) -> dict:
        p = {"chat_id": c["chat_id"], "reply_markup": json.dumps(keyboard, ensure_ascii=False)}
        # ABSENT, not null. `api_call` would drop a None anyway, but the seam every test observes is
        # this dict — so omitting the key is what makes "topics off is byte-identical" an assertion
        # rather than a claim about a filter two functions away.
        if thread is not None:
            p["message_thread_id"] = thread
        return p

    thread_fallback = False
    try:
        res = _send_text(api, c, "sendMessage", send_params(message_thread_id), body)
    except Exception as e:  # noqa: BLE001 — classified, then acted on; never blindly re-sent
        # **A THREAD THAT NO LONGER EXISTS MUST NOT COST THE PICKER.** The topic was deleted, or the
        # bot was re-pointed: Telegram *refuses* the send, so nothing was delivered and re-sending
        # into the main chat cannot duplicate anything. Same classification `_send_text` uses for
        # HTML→plain, and only SEND_REJECTED qualifies: AMBIGUOUS means the request went out and the
        # picker may already be on the phone, while UNDELIVERED is a transport failure the thread had
        # nothing to do with — retrying that one without the thread would dress a network outage up
        # as a stale topic and hide it. Both re-raise, exactly as they do today with no thread.
        if message_thread_id is None or classify_send_failure(e) != SEND_REJECTED:
            raise
        print(f"! telegram refused the picker in thread {message_thread_id} ({e}); re-sending to "
              f"the main chat. The stored topic id is stale.", file=sys.stderr)
        thread_fallback, message_thread_id = True, None
        res = _send_text(api, c, "sendMessage", send_params(None), body)
    mid = (res.get("result") or {}).get("message_id")
    store = load_store(path)
    if qid in store["questions"]:
        store["questions"][qid]["message_id"] = mid
        save_store(path, store)
    # PHASE 2 — the picker enters the record of what the assistant has said
    # (`docs/ask-provenance-spec.md`).
    # AFTER a landed send, which is what `delivered: true` means in that file, and after the
    # message_id is folded back in, so the row is written once about a message that exists.
    #
    # `text` is the BODY, not the question string: the body is what the owner actually received —
    # the question, then the numbered options with their descriptions, then whatever `ask_citations`
    # inlined. `mouth`'s contract is that a row means "in the wording it was received in", and the
    # question alone would leave the options' promises out of the only record that keeps them past the
    # store's 7-day TTL. The keyboard is not in `text` because it is the selector, not the content
    # (see `render_body`); `item_ids` carries the question id, so a tap resolves back to this row.
    #
    # `record_assertion` never raises by contract, and its docstring tells callers not to wrap it.
    # THIS SITE WRAPS IT ANYWAY, for a reason specific to this site rather than distrust of that
    # contract: the send has already landed and the picker is already on the owner's phone, so
    # anything that escapes from here surfaces to every caller as *"the ask failed"* — and a caller
    # that answers a failed ask by asking again sends a duplicate picker. A lost row costs the record;
    # a lost row that also costs the caller's exit code costs the owner a second buzz. So the loss is
    # recorded as a `failures.jsonl` row instead. Same belt-and-braces move
    # `stamp_origin` makes around `build_origin` two hundred lines up, for the same polarity.
    try:
        mouth.record_assertion(state_dir, surface="telegram", kind=ASSERTION_KIND, text=body,
                               speaker=ASSERTION_SPEAKER, item_ids=[qid],
                               origin=assertion_hand(record["origin"]), now=now)
    except Exception as e:  # noqa: BLE001 — see above; the row is never worth the exit code
        print(f"! the Mouth row for question {qid} was not written ({e}); the picker WAS sent",
              file=sys.stderr)
        failures.record(state_dir, "telegram_ask.ask", "mouth_row_failed",
                        detail=f"question {qid}: {type(e).__name__}: {e}", now=now)
    out = {"ok": True, "sent": True, "question_id": qid, "message_id": mid,
           "chat_id": c["chat_id"], "options": len(options), "multi": bool(multi),
           "origin": record["origin"]}
    # Both keys are ADDITIVE and appear only when a topic was actually in play, so a picker sent
    # with topics off returns exactly the object it returned before. `thread_fallback` is what tells
    # `_cmd_ask` to forget the stale id — the send path knows the thread was refused; only the
    # caller knows which purpose it belonged to.
    if message_thread_id is not None:
        out["message_thread_id"] = message_thread_id
    if thread_fallback:
        out["thread_fallback"] = True
    return out


def ask_grid(c: dict, state_dir: str, question: str, items: list, choices: list,
             dry_run: bool = False, api=None, now: datetime | None = None, meta: dict | None = None,
             origin_session: str | None = None, message_thread_id=None) -> dict:
    """Send one GRID picker (see :data:`GRID_KIND`) and record it. Deliberately a sibling of
    :func:`ask` rather than a branch inside it — the two data shapes (ranked options + single/multi
    selection vs. N independent per-row picks) don't share a `selected` field, and forcing them to
    would be the "one flag on a shared verb" shape that makes both callers harder to read.

    **Deliberately skips the citation gate**: an item's `label` is a work item's own text, not the
    assistant's prose making a claim, so there is nothing for it to be right or wrong about — and
    running the citation gate over five
    unrelated OWI item summaries would refuse the send the moment one of them happens to mention a
    `.md` path in its own wording, which has nothing to do with what THIS message asserts.

    `api`/`now`/`meta`/`origin_session`/`message_thread_id` all mean what they mean on :func:`ask`."""
    api = api or api_call
    qid = uuid.uuid4().hex[:8]
    body = render_grid_body(question, items)
    keyboard = build_grid_keyboard(qid, items, choices, {})
    record = {"kind": GRID_KIND, "question": question.strip(), "body": body, "items": items,
              "choices": choices, "grid_selected": {}, "chat_id": str(c.get("chat_id") or ""),
              "message_id": None, "asked_at": _stamp(now), "answered_at": None,
              "origin": stamp_origin(origin_session)}
    if meta is not None:
        record["meta"] = meta

    if dry_run:
        return {"ok": True, "dry_run": True, "question_id": qid, "body": body,
                "rows": [[b["text"] for b in row] for row in keyboard["inline_keyboard"]],
                "meta": meta, "origin": record["origin"]}

    # Persist BEFORE the send, same reasoning as `ask()`: `callback_data` carries the question id, so
    # this record is what makes a tap resolvable at all.
    path = store_path(state_dir)
    store = load_store(path)
    prune_store(store, now)
    store["questions"][qid] = record
    save_store(path, store)

    def send_params(thread) -> dict:
        p = {"chat_id": c["chat_id"], "reply_markup": json.dumps(keyboard, ensure_ascii=False)}
        if thread is not None:
            p["message_thread_id"] = thread
        return p

    thread_fallback = False
    try:
        res = _send_text(api, c, "sendMessage", send_params(message_thread_id), body)
    except Exception as e:  # noqa: BLE001 — classified, then acted on; never blindly re-sent
        if message_thread_id is None or classify_send_failure(e) != SEND_REJECTED:
            raise
        print(f"! telegram refused the grid picker in thread {message_thread_id} ({e}); re-sending "
              f"to the main chat. The stored topic id is stale.", file=sys.stderr)
        thread_fallback, message_thread_id = True, None
        res = _send_text(api, c, "sendMessage", send_params(None), body)
    mid = (res.get("result") or {}).get("message_id")
    store = load_store(path)
    if qid in store["questions"]:
        store["questions"][qid]["message_id"] = mid
        save_store(path, store)
    try:
        mouth.record_assertion(state_dir, surface="telegram", kind=ASSERTION_KIND, text=body,
                               speaker=ASSERTION_SPEAKER, item_ids=[qid],
                               origin=assertion_hand(record["origin"]), now=now)
    except Exception as e:  # noqa: BLE001 — a lost Mouth row may never cost the picker itself
        print(f"! the Mouth row for grid question {qid} was not written ({e}); the picker WAS sent",
              file=sys.stderr)
        failures.record(state_dir, "telegram_ask.ask_grid", "mouth_row_failed",
                        detail=f"question {qid}: {type(e).__name__}: {e}", now=now)
    out = {"ok": True, "sent": True, "question_id": qid, "message_id": mid, "chat_id": c["chat_id"],
           "items": len(items), "kind": GRID_KIND, "origin": record["origin"]}
    if message_thread_id is not None:
        out["message_thread_id"] = message_thread_id
    if thread_fallback:
        out["thread_fallback"] = True
    return out


# --------------------------------------------------------------------------- resolve

def _answer_callback(api, c: dict, callback_id: str, text: str, alert: bool = False) -> None:
    """`answerCallbackQuery` — **the client spins until this lands**, so it is called on every path,
    including the ones that could not record an answer. Never raises: a failed toast must not cost the
    answer we already persisted."""
    if not callback_id:
        return
    try:
        api(c, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200] or None,
                                       "show_alert": "true" if alert else None})
    except Exception:  # noqa: BLE001 — cosmetic; the answer is already on disk
        pass


def _edit_call(api, c: dict, chat_id, message_id, text: str | None = None,
               keyboard: dict | None = None) -> None:
    """The edit itself, **raising**. Split out of :func:`_edit` so retirement can tell
    *"Telegram says that message is already gone"* from *"the network is down"* — one swallows the
    exception because the answer is already on disk, the other must not, because a picker left
    tappable is the whole defect being fixed."""
    params = {"chat_id": chat_id, "message_id": message_id}
    if keyboard is not None:
        params["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    if text is None:
        api(c, "editMessageReplyMarkup", params)
    else:
        # The same plain retry the send path takes. It matters less here — this edit is already
        # best-effort and the answer is already on disk — but a fold-back that silently stopped
        # working the day HTML went on would be a mystery worth not creating.
        _send_text(api, c, "editMessageText", params, text)


def _edit(api, c: dict, chat_id, message_id, text: str | None = None, keyboard: dict | None = None) -> bool:
    """Fold the answer back into the question's own message. Cosmetic and best-effort: Telegram
    refuses an edit that changes nothing, refuses one past its edit window, and none of that may cost
    the answer. Returns whether it landed, for the record only."""
    if not chat_id or not message_id:
        return False
    try:
        _edit_call(api, c, chat_id, message_id, text=text, keyboard=keyboard)
        return True
    except Exception:  # noqa: BLE001
        return False


def classify_edit_failure(exc: BaseException) -> str:
    """*"Is this failed edit actually a settled message?"* — :data:`EDIT_SETTLED` or
    :data:`EDIT_FAILED`.

    **Two locks, and the first is the important one.** It answers `EDIT_SETTLED` only from a
    :class:`TelegramAPIError`, which by that type's contract means *Telegram answered and refused* —
    so a timeout, a DNS failure, or a bare exception out of a caller's own `api` seam can never be
    read as *the message is fine*. Only then does it look at the description, and it looks at the
    description because for these refusals there is nothing else to look at: the Bot API returns a
    bare 400 for all of them. That is a deliberate exception to `classify_send_failure`'s *"reads
    `.phase`, never the error string"* — that function has a phase seam to read; this one has a
    sentence, and pretending otherwise would mean a retirement pass that re-edits a deleted message
    every three minutes forever.

    **The polarity is the safe one either way.** `EDIT_FAILED` leaves the picker exactly as it is,
    which is the conservative status quo. Getting `EDIT_SETTLED` wrong costs a record marked
    retired whose message still carries buttons — and a tap on those buttons still lands on
    :func:`resolve`'s retired branch, which records nothing and says so."""
    if not isinstance(exc, TelegramAPIError):
        return EDIT_FAILED
    text = f"{getattr(exc, 'description', '') or ''} {exc}".lower()
    return EDIT_SETTLED if any(p in text for p in SETTLED_EDIT_PHRASES) else EDIT_FAILED


def retire(c: dict, state_dir: str, qid: str, text: str, api=None,
           now: datetime | None = None) -> dict:
    """**Withdraw one never-answered question**: edit its message down to `text`, strip the keyboard
    so it cannot be tapped, and stamp the record `retired_at`. Returns `{ok, retired, edit, reason}`.

    A retired question **was never answered**, and the record says so in a way no later reader can
    confuse with an answer: `answered_at` stays null and `selected` stays `[]` forever. Nothing here
    invents a choice the owner did not make — which is also why an approval check that reads exactly
    those two fields (the optional merge guard's) is untouched by this path.

    **THE EDIT GOES FIRST AND THE STAMP GOES SECOND, which is the opposite of `resolve`'s order and
    is deliberate.** `resolve` is durable-first because losing an answer is unthinkable. Here the
    thing that must not happen is a record marked retired while a live keyboard is still on the
    owner's phone — so nothing is written until the message is provably settled, and a transient edit failure
    leaves the picker *exactly* as it was for the next pass to retry. The reverse failure (edited,
    then the write dies) converges on the next pass: Telegram answers *"message is not modified"*,
    which :func:`classify_edit_failure` reads as settled.

    **A tap that lands mid-flight wins.** The store is re-read after the edit, and an `answered_at`
    that appeared in the meantime aborts the retirement with the answer intact. A last-writer-wins
    window of a few milliseconds remains — the store is one JSON file — and its worst case is a lost
    `answered_at` on a record whose effect the daemon's callback path has already acted on durably
    (the tap's `meta` was handed back the moment it resolved)."""
    api = api or api_call
    path = store_path(state_dir)
    store = load_store(path)
    q = store["questions"].get(qid)
    if not isinstance(q, dict):
        return {"ok": False, "retired": False, "question_id": qid,
                "reason": f"question {qid} is not in the store"}
    if q.get("answered_at"):
        return {"ok": True, "retired": False, "question_id": qid,
                "reason": f"question {qid} was answered at {q['answered_at']} — nothing to retire"}
    if q.get("retired_at"):
        return {"ok": True, "retired": False, "question_id": qid,
                "reason": f"question {qid} was already retired at {q['retired_at']}"}

    chat_id, message_id = q.get("chat_id"), q.get("message_id")
    if not chat_id or not message_id:
        edit = EDIT_NO_MESSAGE
    else:
        try:
            _edit_call(api, c, chat_id, message_id, text=text, keyboard=EMPTY_KEYBOARD)
            edit = EDIT_LANDED
        except Exception as e:  # noqa: BLE001 — classified, then acted on; never assumed settled
            edit = classify_edit_failure(e)
            if edit == EDIT_FAILED:
                return {"ok": False, "retired": False, "question_id": qid, "edit": edit,
                        "reason": f"the message could not be edited, so question {qid} was left "
                                  f"exactly as it is: {e}"}

    store = load_store(path)          # re-read: a tap may have landed while we were on the wire
    prune_store(store, now)
    q = store["questions"].get(qid)
    if not isinstance(q, dict):
        return {"ok": True, "retired": False, "question_id": qid, "edit": edit,
                "reason": f"question {qid} left the store while its message was being edited"}
    if q.get("answered_at"):
        # **Deliberately no `save_store` here**, unlike `resolve`'s abort paths. The only thing this
        # branch would persist is the lazy prune, which the next store access does anyway — and a
        # write on the one path whose entire purpose is *touch nothing* is a write that can only
        # ever lose a concurrent one.
        return {"ok": True, "retired": False, "question_id": qid, "edit": edit,
                "reason": f"the owner answered question {qid} while its message was being edited "
                          f"— the tap wins and the answer stands"}
    q["retired_at"] = _stamp(now)
    q["retired_reason"] = (text or "").strip()
    q["retired_edit"] = edit
    save_store(path, store)
    return {"ok": True, "retired": True, "question_id": qid, "edit": edit,
            "reason": (text or "").strip()}


def mark_settled(q, *, reason: str, by: dict, now: datetime | None = None) -> bool:
    """**Stamp ONE record, in hand, as settled without a tap.** `True` if it was stamped, `False` if
    it was already answered, already settled, or not a record at all.

    **The caller owns the load and the save**, so N records settle in ONE store write — which is what
    makes the twin case atomic instead of N chances to crash half-way through.

    **It writes the vocabulary :func:`retire` already writes** — `retired_at`, `retired_reason`,
    `retired_edit` — plus `retired_by`, which says *how*. A third set of keys would be a third thing
    `list`, :func:`resolve`'s retired branch, a retirement pass and every future counter each have to
    learn, and the two that exist already agree.

    **`answered_at` stays null and `selected` stays `[]`, forever.** That is not tidiness: those are
    exactly the two fields an approval check reads as its floor, so nothing settled here can ever be
    mistaken for a tap — not by that check, not by a reader of `list`, not by a count of how many
    decisions the owner actually made.

    **`by` is opaque, exactly as `meta` is.** This module carries it and never reads it; the module
    that decided the record is settled owns the word for why. :func:`unsettle` reads only whether it
    is *there*."""
    if not isinstance(q, dict) or q.get("answered_at") or q.get("retired_at"):
        return False
    q["retired_at"] = _stamp(now)
    q["retired_reason"] = (reason or "").strip()
    q["retired_edit"] = EDIT_NOT_ATTEMPTED
    q["retired_by"] = by
    return True


def settle(state_dir: str, qid: str, *, reason: str, by: dict, now: datetime | None = None) -> dict:
    """:func:`mark_settled` for one record, with the load and the save around it. Returns
    `{ok, settled, question_id, reason}`.

    **No network, no credentials, no message edit** — so it runs from any checkout, and that is the
    point rather than an omission: `telegram.env` lives only in the daemon's own tree, and a verb the
    assistant can only use from one directory is a verb it will not use. It also keeps the settle
    reversible: the keyboard is still on the owner's phone, so :func:`unsettle` genuinely hands the question
    back rather than restoring a record whose message is gone. A tap that lands in between is safe
    without it — :func:`resolve`'s retired branch records nothing and says so."""
    path = store_path(state_dir)
    store = load_store(path)
    prune_store(store, now)
    q = store["questions"].get(qid)
    if not isinstance(q, dict):
        return {"ok": False, "settled": False, "question_id": qid,
                "reason": f"question {qid} is not in the store"}
    if q.get("answered_at"):
        return {"ok": True, "settled": False, "question_id": qid,
                "reason": f"question {qid} was answered at {q['answered_at']} — nothing to settle"}
    if q.get("retired_at"):
        return {"ok": True, "settled": False, "question_id": qid,
                "reason": f"question {qid} was already settled at {q['retired_at']}"}
    mark_settled(q, reason=reason, by=by, now=now)
    save_store(path, store)
    return {"ok": True, "settled": True, "question_id": qid, "reason": q["retired_reason"],
            "retired_by": q["retired_by"]}


def settle_in_chat(state_dir: str, qid: str, quote: str, now: datetime | None = None) -> dict:
    """**The owner answered this one in conversation and the assistant heard it** — settle the picker
    without a tap (`docs/picker-state-marking-spec.md`), in the EXPLICIT form only.

    **It is a verb, not a sweep, and the difference is the whole safety property.** An automatic
    version is by far the most dangerous way to settle a picker: a background mechanism that
    concludes *the owner answered this in chat* can withdraw a live decision the owner was never
    shown. So this runs only when something invokes it deliberately, nothing in the tree does, and a
    test scans for a caller.

    **THE QUOTE IS REQUIRED AND THE REFUSAL IS HERE, not in the argument parser.** `ask`'s citation
    gate makes the same move for the same reason: a check that lives in `_cmd_*` is a check a future
    in-process caller bypasses without noticing. A settle with no provenance is the inference version
    wearing a coat — the record has to be able to show *what the owner actually said* to whoever
    reads it back, including the reader who thinks the assistant got it wrong.

    The full quote is kept verbatim on `retired_by.quote`; the *reason*, which is what a later tap
    shows in a popup, carries the first :data:`SETTLE_QUOTE_CHARS` of it."""
    words = (quote or "").strip()
    if not words:
        raise ValueError(
            "settle-in-chat needs the words the owner actually said, verbatim (--quote). The quote "
            "is the whole safety property: a settle with no provenance is an inference, and an "
            "inference may not withdraw a decision the owner was never shown.")
    return settle(state_dir, qid,
                  reason=f'Answered in conversation: "{_quote(words, SETTLE_QUOTE_CHARS)}"',
                  by={"how": SETTLED_BY_IN_CHAT, "by": IN_CHAT_SETTLER, "quote": words}, now=now)


def unsettle(state_dir: str, qid: str, now: datetime | None = None) -> dict:
    """**Hand a settled question back**, so a settle made in the store is never a one-way door.

    This is the half that makes :func:`settle_in_chat` safe to be wrong about. The assistant claiming
    it heard an answer is a claim, and the recovery from a wrong claim has to be cheaper than the claim
    — otherwise the safe move is to never settle anything and the verb is decoration.

    **It refuses a retirement whose message was edited away.** :func:`retire` withdraws a picker by
    editing its message down to one settled line with no keyboard; undoing the *record* would leave a
    question that reads live in `list` with nothing on the owner's phone to tap, which is a worse
    state than either end. Those carry a `retired_edit` of :data:`EDIT_LANDED` / :data:`EDIT_SETTLED`
    / :data:`EDIT_NO_MESSAGE` and no `retired_by`; ask again instead.

    The undone stamps are kept on the record rather than dropped, because *"this was settled and then
    un-settled"* is exactly the history someone re-reading a reversed decision wants."""
    path = store_path(state_dir)
    store = load_store(path)
    prune_store(store, now)
    q = store["questions"].get(qid)
    if not isinstance(q, dict):
        return {"ok": False, "unsettled": False, "question_id": qid,
                "reason": f"question {qid} is not in the store"}
    if not q.get("retired_at"):
        return {"ok": True, "unsettled": False, "question_id": qid,
                "reason": f"question {qid} is not settled — there is nothing to undo"}
    if not isinstance(q.get("retired_by"), dict) or q.get("retired_edit") != EDIT_NOT_ATTEMPTED:
        return {"ok": False, "unsettled": False, "question_id": qid,
                "reason": f"question {qid} was retired by editing its message, so its keyboard is "
                          f"already gone from the owner's phone — undoing the record here would "
                          f"leave a question that reads live with nothing to tap. Ask again instead."}
    was = {k: q.pop(k) for k in ("retired_at", "retired_reason", "retired_edit", "retired_by")}
    history = q.get("unsettled")
    q["unsettled"] = (history if isinstance(history, list) else []) + [{"at": _stamp(now), "was": was}]
    save_store(path, store)
    return {"ok": True, "unsettled": True, "question_id": qid, "was": was}


def mark(c: dict, state_dir: str, qid: str, emoji: str, api=None,
         now: datetime | None = None) -> dict:
    """**Put one reaction on a question's own message** and stamp the record with what is now on it.
    Returns `{ok, marked, reaction, reason}`. The marking pass that calls it decides *which* glyph
    and *why*; this is the wire call and the stamp — the same split :func:`retire` makes with its
    caller.

    **It decides nothing about the vocabulary.** Any string goes out as the emoji, and a glyph
    outside Telegram's fixed `ReactionTypeEmoji` set comes back `REACTION_INVALID` (see
    :data:`REACTION_METHOD`). Validating here would mean a second copy of the allow-list, which is
    the thing that goes stale when Telegram adds a glyph.

    **THE WIRE CALL GOES FIRST AND THE STAMP GOES SECOND** — :func:`retire`'s order, for the mirror
    of its reason. There, the thing that must not exist is a record marked retired with a live
    keyboard still on the owner's phone. Here it is a record claiming a reaction the message does not carry,
    because **that record is what stops the next pass re-applying it** — so a stamp that fails costs
    one redundant call three minutes later and converges, while a stamp that lands early would leave
    an unmarked picker looking marked forever.

    **A no-op is refused rather than sent**, which is the second of two idempotence skips (the caller
    has the first). This pass runs every few minutes over a queue that mostly does not change, and
    while Telegram states no rate limit for this call, hammering it is still wrong.

    **An ANSWERED question is refused; a retired one is not.** The answer stands and a reaction
    appearing on a question the owner has already settled is noise on their phone. Retirement is the other
    way round: a retired picker legitimately reads *put to bed*, and the marking pass runs before the
    retiring one precisely so the settled message ends up carrying the marker.

    Never raises for a reason the caller can act on: every failure is a `{"ok": False}` with the
    reason in it. A picker whose record carries no `message_id` — the send died between the record
    and the response — has nothing to react to and says so."""
    api = api or api_call
    if not isinstance(emoji, str) or not emoji.strip():
        return {"ok": False, "marked": False, "question_id": qid, "reason": "no reaction to set"}
    emoji = emoji.strip()
    path = store_path(state_dir)
    store = load_store(path)
    q = store["questions"].get(qid)
    if not isinstance(q, dict):
        return {"ok": False, "marked": False, "question_id": qid,
                "reason": f"question {qid} is not in the store"}
    if q.get("answered_at"):
        return {"ok": True, "marked": False, "question_id": qid,
                "reason": f"question {qid} was answered at {q['answered_at']} — nothing to mark"}
    if q.get("reaction") == emoji:
        return {"ok": True, "marked": False, "question_id": qid, "reaction": emoji,
                "reason": f"question {qid} already carries this reaction"}
    chat_id, message_id = q.get("chat_id"), q.get("message_id")
    if not chat_id or not message_id:
        return {"ok": True, "marked": False, "question_id": qid,
                "reason": f"question {qid} has no message to react to"}
    try:
        api(c, REACTION_METHOD, {
            "chat_id": chat_id, "message_id": message_id,
            "reaction": json.dumps([{"type": "emoji", "emoji": emoji}], ensure_ascii=False)})
    except Exception as e:  # noqa: BLE001 — a marker may never cost the pass that carries it
        return {"ok": False, "marked": False, "question_id": qid,
                "reason": f"the reaction could not be set on question {qid}: {e}"}

    store = load_store(path)          # re-read: a tap may have landed while we were on the wire
    prune_store(store, now)
    q = store["questions"].get(qid)
    if not isinstance(q, dict):
        return {"ok": True, "marked": False, "question_id": qid,
                "reason": f"question {qid} left the store while its message was being marked"}
    q["reaction"] = emoji
    q["reaction_at"] = _stamp(now)
    save_store(path, store)
    return {"ok": True, "marked": True, "question_id": qid, "reaction": emoji}


def _mark_answered(q: dict, now: datetime | None = None) -> None:
    """**The ONE place `answered_at` is ever assigned** — `test_telegram_questions.py` greps the module
    for exactly one occurrence of the assignment, so both `resolve`'s classic Done and `_resolve_grid`'s grid Done call this rather than
    assigning it inline. A settle (`mark_settled`/`settle`/`settle_in_chat`/`unsettle`) may NEVER call
    this — only an actual tap may: *"answered_at is written in exactly one place."* Two tap SHAPES (classic, grid) now share that one place."""
    q["answered_at"] = _stamp(now)


def _resolve_grid(c: dict, state_dir: str, qid: str, row, choice, callback_id: str, message_id,
                  chat_id, api, now: datetime | None) -> dict:
    """`resolve`'s grid branch, split out because the shapes genuinely differ: a row tap only ever
    changes the keyboard (the row's own `✓`, same as a multi-select toggle), while Done is the one tap
    that answers the question and folds `grid_selected` into a `{item_id: choice_label}` map.

    Answers `{ok, line, …}` on the same contract `resolve` does — `answerCallbackQuery` on every path,
    never a silent no-op, `meta` rides back out only on the Done/answered path."""
    path = store_path(state_dir)
    store = load_store(path)
    retired = prune_store(store, now)
    q = store["questions"].get(qid)
    if not isinstance(q, dict) or q.get("kind") != GRID_KIND:
        if retired:
            save_store(path, store)
        dead = tombstone(store, qid)
        save_store(path, store)
        named = f' It was: "{_quote(dead.get("question", ""), 120)}"' if dead and dead.get("question") else ""
        _answer_callback(api, c, callback_id,
                         f"That question has expired — I no longer have it on file.{named} "
                         f"Tell me in a message?", alert=True)
        return {"ok": True, "expired": True, "question_id": qid,
                "line": f"[the owner tapped an answer button on a grid picker I no longer have a "
                        f"record of — it expired after {QUESTION_TTL_DAYS} days; ask them to say it "
                        f"in words]"}

    items, choices = q.get("items") or [], q.get("choices") or []
    target_chat = q.get("chat_id") or (str(chat_id) if chat_id else "")
    target_msg = q.get("message_id") or message_id

    if q.get("answered_at"):
        save_store(path, store)
        _answer_callback(api, c, callback_id, "Already submitted.")
        return {"ok": True, "already_answered": True, "question_id": qid, "line": None}

    if q.get("retired_at"):
        save_store(path, store)
        why = str(q.get("retired_reason") or "that question is no longer live").strip()
        _answer_callback(api, c, callback_id, f"{why} {RETIRED_TAP_TAIL}", alert=True)
        return {"ok": True, "retired": True, "question_id": qid,
                "line": f"[the owner tapped a grid picker I had already retired — {why} "
                        f"{RETIRED_TAP_TAIL} The picker was stale, not the tap.]"}

    if choice == "done":
        selected = q.get("grid_selected") or {}
        answers, untouched = {}, []
        for i, item in enumerate(items):
            picked = selected.get(str(i))
            if isinstance(picked, int) and 0 <= picked < len(choices):
                answers[item["id"]] = choices[picked]
            else:
                untouched.append(item["id"])
        _mark_answered(q, now)
        save_store(path, store)
        _answer_callback(api, c, callback_id,
                         f"Recorded {len(answers)} of {len(items)}." if answers else "Nothing selected.")
        edited = _edit(api, c, target_chat, target_msg, text=render_grid_answered(q, answers, untouched))
        return {"ok": True, "question_id": qid, "answered": True, "grid_answers": answers,
                "untouched": untouched, "message_edited": edited,
                "line": grid_answer_line(q, answers, untouched), "meta": q.get("meta")}

    if not (isinstance(row, int) and 0 <= row < len(items)) or \
       not (isinstance(choice, int) and 0 <= choice < len(choices)):
        save_store(path, store)
        _answer_callback(api, c, callback_id, "That option isn't on this picker any more.", alert=True)
        return {"ok": True, "question_id": qid,
                "line": "[the owner tapped a grid option I can't match to that picker — ask them "
                        "what they picked]"}

    selected = dict(q.get("grid_selected") or {})
    selected[str(row)] = choice
    q["grid_selected"] = selected
    save_store(path, store)
    _answer_callback(api, c, callback_id, f"{items[row]['label']}: {choices[choice]}")
    _edit(api, c, target_chat, target_msg, keyboard=build_grid_keyboard(qid, items, choices, selected))
    # No inbound yet, same reasoning as the classic multi-select's toggle: a row isn't an answer until
    # Done, so the assistant isn't woken once per tap and never reads a half-made grid as the final one.
    return {"ok": True, "question_id": qid, "row": row, "choice": choice,
            "selected": dict(selected), "line": None}


def resolve(c: dict, state_dir: str, data: str, callback_id: str, message_id=None, chat_id=None,
            api=None, now: datetime | None = None) -> dict:
    """Apply one button tap. Returns ``{ok, line, …}`` where `line` is the inbound the daemon should
    enqueue, or ``None`` when there is nothing new to say (a multi-select toggle, or a re-tap of a
    question already answered — both of which still get a popup, so neither is a silent no-op).

    **Order is durable-first**: decide, persist, *then* talk to Telegram. A failed toast leaves the
    answer recorded and delivered; the reverse would tell the owner the tap landed with nothing on
    disk to show for it.

    **A daemon stub-send mode must REFUSE to resolve, deliberately.** `answerCallbackQuery` is as
    much a real send as a message is — the client's button spins on the phone until it lands — so a
    stub mode that pretends to resolve a tap without making that call would be lying about a send
    that never happened."""
    api = api or api_call
    qid, choice = parse_callback_data(data)
    if qid is None:
        # A classic payload never starts its third segment with "g" (`_GRID_CELL_RE`), so this can
        # only fire for a genuinely grid-shaped tap or a genuinely unreadable one — never both parsers
        # matching the same string.
        gqid, grow, gchoice = parse_grid_callback_data(data)
        if gqid is not None:
            return _resolve_grid(c, state_dir, gqid, grow, gchoice, callback_id, message_id, chat_id,
                                 api, now)
    path = store_path(state_dir)
    store = load_store(path)
    retired = prune_store(store, now)

    if qid is None:
        if retired:
            save_store(path, store)
        _answer_callback(api, c, callback_id,
                         "I couldn't read that button. Tell me in a message?", alert=True)
        return {"ok": True, "line": "[the owner tapped a button whose payload I couldn't read — ask "
                                    "them what they meant to pick]", "question_id": None}

    q = store["questions"].get(qid)
    if q is None:
        dead = tombstone(store, qid)
        save_store(path, store)
        named = f' It was: "{_quote(dead.get("question", ""), 120)}"' if dead and dead.get("question") else ""
        _answer_callback(api, c, callback_id,
                         f"That question has expired — I no longer have it on file.{named} "
                         f"Tell me in a message?", alert=True)
        was = f' ("{_quote(dead.get("question", ""))}")' if dead and dead.get("question") else ""
        return {"ok": True, "expired": True, "question_id": qid,
                "line": f"[the owner tapped an answer button on a question I no longer have a record "
                        f"of{was} — the picker expired after {QUESTION_TTL_DAYS} days; ask them to "
                        f"say it in words]"}

    options = q.get("options") or []
    target_chat = q.get("chat_id") or (str(chat_id) if chat_id else "")
    target_msg = q.get("message_id") or message_id

    if q.get("answered_at"):
        save_store(path, store)
        picked = ", ".join(chosen_labels(q)) or "nothing"
        _answer_callback(api, c, callback_id, f"Already answered — {picked}.")
        return {"ok": True, "already_answered": True, "question_id": qid, "line": None}

    if q.get("retired_at"):
        # **AFTER the answered branch, never before it.** A record can only ever carry one of the
        # two (`retire` refuses an answered question, and this branch returns before anything is
        # written), but the order still matters: if a race ever produced both, the answer the owner
        # gave is the one that must be reported back.
        #
        # It returns no `meta` and no `answered`, so the daemon's callback path cannot fire an
        # approval effect from here — a retired picker is structurally incapable of minting a merge
        # approval, rather than merely unlikely to.
        save_store(path, store)
        why = str(q.get("retired_reason") or "that question is no longer live").strip()
        _answer_callback(api, c, callback_id, f"{why} {RETIRED_TAP_TAIL}", alert=True)
        return {"ok": True, "retired": True, "question_id": qid, "line":
                f"[the owner tapped a picker I had already retired — {why} "
                f"{RETIRED_TAP_TAIL} The picker was stale, not the tap.]"}

    if choice != "done" and not (isinstance(choice, int) and 0 <= choice < len(options)):
        save_store(path, store)
        _answer_callback(api, c, callback_id, "That option isn't on this question any more.", alert=True)
        return {"ok": True, "question_id": qid,
                "line": "[the owner tapped an option I can't match to that question — ask them what "
                        "they picked]"}

    if q.get("multi") and choice != "done":
        selected = [i for i in (q.get("selected") or []) if isinstance(i, int)]
        added = choice not in selected
        q["selected"] = sorted(selected + [choice]) if added else [i for i in selected if i != choice]
        save_store(path, store)
        _answer_callback(api, c, callback_id,
                         f"{'Added' if added else 'Removed'}: {options[choice]['label']}")
        _edit(api, c, target_chat, target_msg,
              keyboard=build_keyboard(qid, options, True, q["selected"]))
        # No inbound yet, on purpose: a multi-select answer isn't an answer until Done, so the
        # assistant isn't woken once per checkbox and never reads a half-made choice as the final one.
        return {"ok": True, "question_id": qid, "toggled": choice,
                "selected": list(q["selected"]), "line": None}

    if not q.get("multi"):
        q["selected"] = [choice]
    _mark_answered(q, now)
    save_store(path, store)
    # **THE ANSWER SETTLES EVERY OTHER RECORD OF THE SAME QUESTION**
    # (`docs/picker-state-marking-spec.md`). A send that does not land still leaves a record, and
    # `callback_data` is what makes a record answerable — so the identical question can be live twice
    # with only one of them reachable, and the unreachable twin would otherwise sit pending until it
    # expired and invite the owner to decide the same thing twice.
    #
    # **`merge_guard` decides which records are the same question**, because it already owns that
    # tuple and a second definition of sameness is how two of them start disagreeing. This module
    # writes the store and reads no `meta`, exactly as it reads none for `--meta`. It is an OPTIONAL
    # import: without it (`merge_guard is None`) there is no twin dedupe at all, and a duplicate stays
    # pending until it expires or is settled by hand. The whole block is wrapped besides: the answer
    # is already on disk two lines up, and tidying up after it may never be the thing that costs the
    # tap. A stale twin is the safe failure.
    #
    # **AND THE SETTLED TWIN KEEPS ITS KEYBOARD, which is a residual and not an oversight.** Editing
    # here would put N network calls in front of the owner's spinning button, or behind an
    # `answerCallbackQuery` with nothing to retry them. A tap on the survivor lands on the retired
    # branch above, which records nothing and says so.
    twins = []
    if merge_guard is not None:
        try:
            for s in merge_guard.twin_settlements(store, qid):
                if mark_settled(store["questions"].get(s["question_id"]), reason=s["reason"],
                                by=s["by"], now=now):
                    twins.append(s["question_id"])
            if twins:
                save_store(path, store)
        except Exception as e:  # noqa: BLE001 — a stale duplicate is survivable; a lost answer is not
            print(f"! duplicate pickers for question {qid} were not settled ({e}); the answer stands",
                  file=sys.stderr)
            failures.record(state_dir, "telegram_ask.resolve", "twin_settle_failed",
                            detail=f"question {qid}: {type(e).__name__}: {e}", now=now)
    labels = chosen_labels(q)
    _answer_callback(api, c, callback_id, ", ".join(labels) if labels else "Nothing selected.")
    edited = _edit(api, c, target_chat, target_msg, text=render_answered(q))
    # `meta` rides back out ONLY on the answered path — the one place a tap is a final decision. A
    # toggle and a re-tap return no meta because they are not answers, so a caller acting on meta
    # cannot fire twice or fire on a half-made choice.
    out = {"ok": True, "question_id": qid, "answered": True, "selected": list(q["selected"]),
           "labels": labels, "message_edited": edited, "line": answer_line(q),
           "meta": q.get("meta")}
    # Present ONLY when something was actually settled — the store's own convention for a key that
    # would otherwise read as *"checked and clear"* on every other tap. It is how the daemon gets to
    # log a settle that happened inside this subprocess.
    if twins:
        out["twins_settled"] = twins
    return out


# --------------------------------------------------------------------------- CLI

def _cmd_ask_grid(args) -> int:
    """`--grid`'s own path through `ask` — parsed and validated separately from the classic path
    because the shapes (N independent per-row picks vs. ranked options) genuinely differ; see
    `ask_grid`'s docstring for why it is a sibling function rather than a branch inside `ask`."""
    try:
        items = [parse_grid_item(raw) for raw in (args.item or [])]
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2
    if not (args.question or "").strip():
        print(json.dumps({"ok": False, "error": "no --question"}))
        return 2
    if not MIN_GRID_ITEMS <= len(items) <= MAX_GRID_ITEMS:
        print(json.dumps({"ok": False, "error": f"need {MIN_GRID_ITEMS}-{MAX_GRID_ITEMS} --item "
                                                f"values, got {len(items)}"}))
        return 2
    ids = [it["id"] for it in items]
    if len(set(ids)) != len(ids):
        print(json.dumps({"ok": False, "error": "--grid --item ids must be unique"}))
        return 2
    choices = [c.strip() for c in (args.choices or "").split("|") if c.strip()]
    if not MIN_GRID_CHOICES <= len(choices) <= MAX_GRID_CHOICES:
        print(json.dumps({"ok": False, "error": f"need {MIN_GRID_CHOICES}-{MAX_GRID_CHOICES} "
                                                f"pipe-separated --choices, got {len(choices)}"}))
        return 2

    meta = None
    if args.meta:
        try:
            meta = json.loads(args.meta)
        except ValueError as e:
            print(json.dumps({"ok": False, "error": f"--meta is not JSON: {e}"}))
            return 2
        if not isinstance(meta, dict):
            print(json.dumps({"ok": False, "error": "--meta must be a JSON object"}))
            return 2

    c = cfg(load_env(args.env_file))
    if args.chat_id:
        c["chat_id"] = args.chat_id
    if not args.dry_run and not c["chat_id"]:
        print(json.dumps({"ok": False, "error": "no chat id (set TELEGRAM_CHAT_ID or pass --chat-id)"}))
        return 2
    topic = None
    if getattr(args, "topic", None) and not args.dry_run:
        topic = tt.thread_id(c, args.topic, args.state_dir, log=lambda m: print(m, file=sys.stderr))
    try:
        res = ask_grid(c, args.state_dir, args.question, items, choices, dry_run=args.dry_run,
                       meta=meta, origin_session=args.origin_session, message_thread_id=topic)
    except Exception as e:  # noqa: BLE001
        failures.record(args.state_dir, "telegram_ask.ask_grid", "ask_failed",
                        detail=f"{type(e).__name__}: {e}")
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    if res.get("thread_fallback") and getattr(args, "topic", None):
        tt.forget(args.state_dir, args.topic)
    print(json.dumps(res, ensure_ascii=False))
    return 0


def _cmd_ask(args) -> int:
    if getattr(args, "grid", False):
        return _cmd_ask_grid(args)
    try:
        options = [parse_option(raw) for raw in (args.option or [])]
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2
    if not (args.question or "").strip():
        print(json.dumps({"ok": False, "error": "no --question"}))
        return 2
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        print(json.dumps({"ok": False, "error": f"need {MIN_OPTIONS}-{MAX_OPTIONS} --option values, "
                                                f"got {len(options)}"}))
        return 2

    meta = None
    if args.meta:
        try:
            meta = json.loads(args.meta)
        except ValueError as e:
            print(json.dumps({"ok": False, "error": f"--meta is not JSON: {e}"}))
            return 2
        if not isinstance(meta, dict):
            print(json.dumps({"ok": False, "error": "--meta must be a JSON object"}))
            return 2

    c = cfg(load_env(args.env_file))
    if args.chat_id:
        c["chat_id"] = args.chat_id
    if not args.dry_run and not c["chat_id"]:
        print(json.dumps({"ok": False, "error": "no chat id (set TELEGRAM_CHAT_ID or pass --chat-id)"}))
        return 2
    # WHICH THREAD, resolved before the send and never allowed to prevent one. `thread_id` is
    # contracted never to raise and to answer `None` — the main chat — for every failure there is:
    # topics off, `getMe` unreachable, an unreadable state file, a creation that failed. A dry run
    # asks nothing, because resolving a topic can CREATE one and `--dry-run` promises no effects.
    #
    # `args.topic` is `telegram_topics.DEFAULT_TOPIC` unless the caller named one, so THIS LINE is
    # where "a picker with no --topic lands in the decisions topic" actually happens. The default
    # rides the same fail-open resolution, the same `thread_fallback` re-send and the same `forget`
    # below that an explicit flag rides, rather than acquiring a second path that would have to be
    # kept honest separately.
    topic = None
    if getattr(args, "topic", None) and not args.dry_run:
        topic = tt.thread_id(c, args.topic, args.state_dir,
                             log=lambda m: print(m, file=sys.stderr))
    # `getattr` rather than `args.cite_head_repo`: `_cmd_ask` is also driven directly by test
    # namespaces (`_Args` in `test_ask_citations.py`) that predate this flag and carry none of the
    # three attributes, the same reason `args.topic` above is read the same way.
    cite_head_repo = getattr(args, "cite_head_repo", None)
    cite_head_sha = getattr(args, "cite_head_sha", None)
    head = None
    if cite_head_repo and cite_head_sha:
        head = ask_citations.PRHead(cite_head_repo, cite_head_sha,
                                     getattr(args, "cite_head_path", None))
    try:
        res = ask(c, args.state_dir, args.question, options, multi=args.multi,
                  recommend=not args.no_recommendation, dry_run=args.dry_run, meta=meta,
                  origin_session=args.origin_session, quoted=args.quote,
                  message_thread_id=topic, head=head, footer=getattr(args, "footer", None))
    except ask_citations.CitationError as e:
        # EXIT 2, the same code and the same shape as an option with no description: this is a
        # malformed ASK, not a transport failure, and the caller has to change what it typed. The
        # message names the token that did not resolve so the fix is one edit, and `--dry-run`
        # reaches this branch too, so a refusal is discoverable without sending anything.
        print(json.dumps({"ok": False, "error": str(e), "refused": "citation"}, ensure_ascii=False))
        return 2
    except Exception as e:  # noqa: BLE001
        failures.record(args.state_dir, "telegram_ask.ask", "ask_failed",
                        detail=f"{type(e).__name__}: {e}")
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    if res.get("thread_fallback") and getattr(args, "topic", None):
        # The picker LANDED, in the main chat, and the stored id is stale. Forget it so the next one
        # creates a fresh topic instead of paying a rejected send every time. Best-effort by design:
        # a failed forget costs one extra refused call per picker and never a message.
        tt.forget(args.state_dir, args.topic)
    print(json.dumps(res, ensure_ascii=False))
    return 0


def _cmd_resolve(args) -> int:
    c = cfg(load_env(args.env_file))
    if args.chat_id:
        c["chat_id"] = args.chat_id
    try:
        res = resolve(c, args.state_dir, args.data, args.callback_id,
                      message_id=args.message_id, chat_id=args.chat_id)
    except Exception as e:  # noqa: BLE001 — the daemon reads this JSON; a traceback would be a no-op
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps(res, ensure_ascii=False))
    return 0


def _cmd_list(args) -> int:
    """The sanctioned read-only door into the store — and the one that ANSWERS *"who asked this?"*.

    A provenance field that `list` did not show would still mean opening the JSON by hand to find the
    author. Every row therefore carries `author`, always — :func:`origin_summary`, a short
    `source · session` form, never absent and never invented.

    **One document, not two.** There is one output here and it is JSON that both a person and a
    script read, so rather than fork a human renderer this gives the human the short `author` cell
    unconditionally and the machine the verbatim object behind `--origin` (`null` where the record
    has none — distinguishable from `{}`, which no record carries). Nothing is shown in one shape and
    withheld in the other; the flag only chooses how much."""
    store = load_store(store_path(args.state_dir))
    prune_store(store)
    rows = []
    for qid, q in sorted(store["questions"].items(), key=lambda kv: kv[1].get("asked_at") or ""):
        if q.get("answered_at") and not args.all:
            continue
        row = {"id": qid, "question": q.get("question"), "multi": q.get("multi"),
               "asked_at": q.get("asked_at"), "answered_at": q.get("answered_at"),
               "selected": chosen_labels(q), "author": origin_summary(q)}
        if q.get("kind") == GRID_KIND:
            row["kind"] = GRID_KIND
            row["items"] = len(q.get("items") or [])
            row["touched"] = len(q.get("grid_selected") or {})
        # RETIRED is not ANSWERED, and a reader of this list has to be able to tell. Present only on
        # a record that was actually withdrawn: a `retired_at: null` on every other row would invite
        # reading an absence as a claim.
        if q.get("retired_at"):
            row["retired_at"] = q.get("retired_at")
            row["retired_reason"] = q.get("retired_reason")
            # HOW it was settled, on the records that were settled without a tap (`retired_by`;
            # :func:`retire`'s message-edit retirements carry none). `list` is the door that answers
            # *what is still open?*, so a row that renders a tap, a retirement and a settle the same
            # way is how a decision gets asked for twice.
            if isinstance(q.get("retired_by"), dict):
                row["retired_by"] = q.get("retired_by")
        if args.origin:
            row["origin"] = record_origin(q)
        rows.append(row)
    out = {"ok": True, "pending": rows}
    if args.all:
        # The tombstone is the case the spec calls hardest — a week on, the options are gone by
        # design and the author is the only thing left worth having — so it gets the same cell.
        out["expired"] = [dict(e, author=origin_summary(e)) for e in store["expired"][-10:]]
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _cmd_prune(args) -> int:
    path = store_path(args.state_dir)
    store = load_store(path)
    retired = prune_store(store)
    save_store(path, store)
    print(json.dumps({"ok": True, "expired": retired, "pending": len(store["questions"])}))
    return 0


def _cmd_settle_in_chat(args) -> int:
    """**The refusal is a sentence, not an argparse usage dump** — which is why `--quote` is not
    `required=True`. A caller who forgot it is a caller who has to be told *why* the quote is the
    mechanism, and `_cmd_ask` refuses a description-less option in exactly this shape for exactly
    this reason. Exit 2 is *bad arguments*, the module's own contract."""
    try:
        res = settle_in_chat(args.state_dir, args.question_id, args.quote or "")
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


def _cmd_unsettle(args) -> int:
    res = unsettle(args.state_dir, args.question_id)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    """The argv contract, split out of :func:`main` so a test can read a DEFAULT without running a
    command. `--topic`'s default is the one that needs it: *"an ordinary picker lands in the
    decisions topic"* is a claim about this parser and about nothing else, and asserting it through
    a live `ask` would need a chat id, a socket and a resolved thread."""
    p = argparse.ArgumentParser(description="Ask the owner a question with tappable Telegram buttons.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="seneschal/state (the question store)")
    p.add_argument("--env-file", help="KEY=VALUE file with TELEGRAM_* settings (kept untracked)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="send one question with tappable options")
    a.add_argument("--question", required=True, help="the question itself")
    a.add_argument("--option", action="append", metavar='"Label|Description"',
                   help="an option and what it means/costs. Repeatable. The FIRST is the "
                        "recommendation and is marked so. A description is REQUIRED.")
    a.add_argument("--multi", action="store_true",
                   help="the choices aren't mutually exclusive: toggling buttons + a Done button")
    a.add_argument("--grid", action="store_true",
                   help="a GRID picker instead of a classic one: N --item rows x fixed --choices, one "
                        "pick per row, editable until Done. Mutually exclusive with --option/--multi/"
                        "--no-recommendation — see _cmd_ask_grid for the separate path this takes.")
    a.add_argument("--item", action="append", metavar='"id|label"',
                   help="grid mode: one row (repeatable). No description — a grid row is data, not a "
                        "ranked choice; see parse_grid_item.")
    a.add_argument("--choices", default=None, metavar="A|B|C|...",
                   help="grid mode: pipe-separated choice labels applied to EVERY row, e.g. "
                        "\"Owner|Assistant|Both|External|Unknown\"")
    a.add_argument("--no-recommendation", action="store_true",
                   help="a genuinely open pick — don't mark the first option (Recommended)")
    a.add_argument("--chat-id", help="recipient chat id (default TELEGRAM_CHAT_ID)")
    a.add_argument("--meta", metavar="JSON",
                   help="opaque JSON object carried on the record and handed back by `resolve`, so "
                        "a tap can mean something to the daemon. NEVER interpreted here — the "
                        "dispatch lives in the daemon's callback path.")
    a.add_argument("--origin-session", default=None, metavar="ID",
                   help=f"the session doing the asking. Defaults to ${ORIGIN_ENV_VAR} (auto), which "
                        "is the mechanism — this flag is only for a caller that knows better than "
                        "its own environment. Recorded as origin.stamped_by=flag.")
    a.add_argument("--footer", default=None, metavar="TEXT",
                   help="one line repeated under the last option (after a blank line) — a caller's "
                        "own title line, so a long picker reads from either end. Scanned by the "
                        "citation gate like the rest of the body; the excerpt block, if any, sits "
                        "ABOVE it so it stays last.")
    a.add_argument("--quote", action="append", metavar="TEXT",
                   help="a span of the question that is RELAYED text (a PR title, a PR body) rather "
                        "than the assistant's own words. References inside it are not resolved and not "
                        "excerpted. Exempts a span; changes nothing about the layout. A span that "
                        "does not occur in the question is refused, not ignored.")
    # Resolve citations against the PR's own head: a path the picker's own PR CREATES does not exist
    # in the checkout `ask_citations.resolve_doc` reads by default, so the citation gate would refuse
    # it forever. These
    # three build one `ask_citations.PRHead` — a closed allow-list, never a general "cite anything"
    # door — and all three are required together for it to do anything at all.
    a.add_argument("--cite-head-repo", default=None, metavar="OWNER/REPO",
                   help="paired with --cite-head-sha/--cite-head-path: when a cited path is not in "
                        "the checkout, retry it against THIS repo's head via `gh api` instead of "
                        "refusing. Only a path also passed as --cite-head-path is ever looked up "
                        "this way.")
    a.add_argument("--cite-head-sha", default=None, metavar="SHA",
                   help="the commit --cite-head-path resolves against. The excerpt shown, if any, "
                        "is THIS commit's content, never the checkout's.")
    a.add_argument("--cite-head-path", action="append", default=None, metavar="PATH",
                   help="a path (repeatable) that may fall back to --cite-head-sha when the "
                        "checkout does not have it. A path not named here never falls back, however "
                        "plausible it looks — the caller's own changed-file list, never every path "
                        "the PR touches.")
    # **DELIBERATELY NO argparse `choices`.** A purpose that isn't in the table resolves to None and
    # the picker goes to the main chat; `choices` would make the same typo an exit-2 and swallow the
    # message entirely. Nothing about a topic may ever be a reason a picker does not arrive.
    #
    # **AND THE DEFAULT IS A TOPIC, NOT THE MAIN CHAT**: open pickers belong in one place the owner
    # can scroll to see what is still undecided, rather than scattered through the conversation. A
    # flag every existing caller would have to be edited to pass would route only the pickers written
    # after somebody read `telegram_topics.py`, which is not the same thing as routing the pickers.
    a.add_argument("--topic", default=tt.DEFAULT_TOPIC, metavar="PURPOSE",
                   help="which private-chat TOPIC to send into "
                        f"({', '.join(sorted(tt.TOPIC_NAMES))}); default {tt.DEFAULT_TOPIC}, so an "
                        "ordinary picker lands with the other open decisions rather than in the "
                        f"conversation. `--topic {tt.TOPIC_MAIN_CHAT}` forces the main chat. "
                        "Advisory: if topics are off for this bot, or the thread cannot be "
                        "resolved, the picker goes to the main chat exactly as it does today. "
                        "Never a reason for a picker not to arrive.")
    a.add_argument("--dry-run", action="store_true", help="render the body + buttons; no network, no record")
    a.set_defaults(func=_cmd_ask)

    r = sub.add_parser("resolve", help="apply one button tap (the daemon's callback_query path)")
    r.add_argument("--data", required=True, help="the tap's callback_data")
    r.add_argument("--callback-id", required=True, help="callback_query.id — answered on every path")
    r.add_argument("--message-id", type=int, default=None, help="fallback if the record has none")
    r.add_argument("--chat-id", default=None, help="fallback if the record has none")
    r.set_defaults(func=_cmd_resolve)

    ls = sub.add_parser("list", help="pending questions, each naming who asked it")
    ls.add_argument("--all", action="store_true", help="include answered + recent expired")
    ls.add_argument("--origin", action="store_true",
                    help="carry each record's full `origin` object too, verbatim. Every row already "
                         "names its author in short form; this is for a census that needs cwd, pid "
                         "and which door each part came through.")
    ls.set_defaults(func=_cmd_list)

    sc = sub.add_parser("settle-in-chat",
                        help="the owner answered this one in conversation — settle it without a tap")
    sc.add_argument("--question-id", required=True, metavar="ID",
                    help="the question id, as `list` prints it")
    # DELIBERATELY NOT `required=True`. See `_cmd_settle_in_chat`: the missing-quote path is the one
    # a caller most needs a reason for, and argparse's answer to it is a usage dump.
    sc.add_argument("--quote", default=None, metavar="TEXT",
                    help="VERBATIM what the owner said. REQUIRED — the settle is refused without it. "
                         "This is the whole safety property: the record has to be able to show what "
                         "the owner actually said, including to a reader who thinks the assistant "
                         "misheard.")
    sc.set_defaults(func=_cmd_settle_in_chat)

    un = sub.add_parser("unsettle", help="undo a settle made without a tap; the picker stays tappable")
    un.add_argument("--question-id", required=True, metavar="ID", help="the question id")
    un.set_defaults(func=_cmd_unsettle)

    pr = sub.add_parser("prune", help="run the lazy expiry sweep now (no network)")
    pr.set_defaults(func=_cmd_prune)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
