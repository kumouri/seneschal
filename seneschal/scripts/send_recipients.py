#!/usr/bin/env python3
"""`state/send-recipients.jsonl` — the non-content outbound-recipient-class ledger. Stdlib only.

Every outbound call site (`proton_send.py`, `gmail_api.py send`/`send-draft`, `gcal_api.py
create-event`/`delete-event`, `push_sms.py`, `push_call.py`, `discord_send.py`) calls `record()` once
per real send attempt, classifying who it reached — `owner` (the owner themselves), `third_party`
(anyone else) or `unknown` (cannot be decided from what the call site has) — WITHOUT ever writing
the address, number, handle, subject or body. A transcript answers "what did the assistant say to
the owner"; it never answers "who did an outbound send actually reach". This file does, and
`send_gate.py blast-radius` reads it back.

## `record()` MUST NEVER RAISE, AND MUST NEVER SLOW OR REFUSE A SEND

This is instrumentation, not a gate — `send_gate.py` is the gate. Every call site invokes this from
the same spot as its `send_gate.require_approval` call, before the network call fires, and hands the
verdict in as `gate=` so the row records what the gate decided; a raise here would turn an
instrument into an accidental enforcement. Same contract as `failures.record`. A classification
failure at a call site is handled the same way: catch it there and pass `"unknown"` in, never let it
propagate into the send.

## Classification never stores the address

The owner's own addresses are **configuration, never code**: `owner.email` plus the optional
`owner.emails` list in `persona/identity.json`, read through `identity_common.owner_emails`. The
assistant's own send-from address (`assistant.email`) counts as self too — a cc to the assistant's
own mailbox reaches no third party. Nothing configured means the self set is empty, so every
address classifies `third_party`: the fail-closed direction for the gate that reads this.

`classify_emails` takes the raw recipient strings only to compare them (case-insensitively) against
that set, and returns one of the three class strings and nothing else; the row `record()` writes
never carries the values it was given to classify.

`classify_single_recipient` is the phone-number / channel-id sibling for the three
single-recipient-by-design channels (`push_sms.py`, `push_call.py`, `discord_send.py` — each only
ever reaches the owner's own phone or the assistant's one private Discord channel, by construction of
the surface itself): falling through to the channel's own built-in default (no explicit override
given) is `owner`; an explicit override that cannot be verified locally is `unknown`, never guessed
into either firm class.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import identity_common  # noqa: E402
import paths  # noqa: E402
import stateio  # noqa: E402

FILENAME = "send-recipients.jsonl"

_CLASSES = ("owner", "third_party", "unknown")


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def self_addresses(identity: dict | None = None) -> frozenset:
    """The lowercased addresses that count as "the owner" for classification: the owner's own
    (`identity_common.owner_emails` — `owner.email` + `owner.emails`) plus the assistant's own
    send-from address (`assistant.email`). Read fresh from `persona/identity.json` when `identity`
    is not given — sends are rare and the file is small, so an owner who adds an address needs no
    restart. Never raises (`load_identity` never does)."""
    ident = identity if isinstance(identity, dict) else identity_common.load_identity()
    out = set(identity_common.owner_emails(ident))
    mine = identity_common.get_str(ident, "assistant", "email")
    if mine and "@" in mine:
        out.add(mine.lower())
    return frozenset(out)


def classify_emails(*addresses, identity: dict | None = None) -> str:
    """`owner` / `third_party` / `unknown` for a batch of raw recipient strings — each may itself be
    comma-separated (every call site's own `--to`/`--cc` shape is accepted as-is rather than requiring
    a pre-split list), and `None`/empty entries are ignored.

    `owner` only when every address found is one of :func:`self_addresses`; `third_party` the moment
    any single address is not; `unknown` when no address could be read at all (an empty batch, or
    every entry blank)."""
    mine = None
    found = False
    for raw in addresses:
        if not raw:
            continue
        for part in str(raw).split(","):
            addr = part.strip().lower()
            if not addr:
                continue
            found = True
            if mine is None:
                mine = self_addresses(identity)
            if addr not in mine:
                return "third_party"
    return "owner" if found else "unknown"


def classify_single_recipient(resolved, default=None) -> str:
    """`owner` when `resolved` is empty (the call falls through to the channel's own built-in
    default) or equals `default` exactly; `unknown` when an explicit, different value was given and
    there is no local way to tell whose it is. Never `third_party` — none of the three channels this
    is for can be pointed at anyone but the owner without hand-editing an env file to do it, which
    is a channel-provisioning decision, not a per-send one this ledger can see."""
    if not resolved or (default and resolved == default):
        return "owner"
    return "unknown"


def record(channel: str, recipient_class: str, *, state_dir: str | None = None,
           now: datetime | None = None, gate: dict | None = None) -> None:
    """Append one row: `{at, channel, recipient_class[, gate]}`. NEVER RAISES, and writes nothing
    that could identify who was actually reached — no address, number, handle, subject or body,
    ever. An unrecognized `recipient_class` is coerced to `"unknown"` rather than written verbatim or
    raised on, same posture as an unreadable classification at the call site.

    `gate` is `send_gate.require_approval`'s verdict — only its `allowed`/`reason`/`approval_id` are
    copied in, never the verdict's `recipients` list, so this file stays non-content. A refused send
    still writes a row (`gate.allowed == false`): the ledger is the measurement."""
    try:
        if recipient_class not in _CLASSES:
            recipient_class = "unknown"
        path = os.path.join(paths.state_dir(state_dir), FILENAME)
        row = {"at": _stamp(now), "channel": str(channel), "recipient_class": recipient_class}
        if isinstance(gate, dict):
            row["gate"] = {"allowed": bool(gate.get("allowed")), "reason": str(gate.get("reason")),
                           "approval_id": gate.get("approval_id")}
        stateio.append_jsonl(path, row)
    except Exception:  # noqa: BLE001 — instrumentation must never cost the send
        pass
