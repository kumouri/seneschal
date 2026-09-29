#!/usr/bin/env python3
"""**Thread reconciliation for the Watch peek.** Standard library + the sibling `reminders_acks` only,
no LLM in the gate — deterministic classification over messages fetched through the same stdlib comms
doors the rest of this tree already uses. Never imports `telegram_*` or `presence`, so the Telegram
send path can import it optionally.

## What this closes

A bank's overdraft notice escalates, and the same sender's follow-up hours later — *"you are no longer
in low-balance mode"* — never reaches the gate at all, because the peek reads one message at a time
and never reconciles a thread. Dedupe (`reminders_acks.watch_duplicate_blocked`) stops a peek
repeating *itself*; the runtime ack (`watch_ack.ack_blocks`) stops it when the OWNER says a fact is
handled. Neither helps when the SOURCE itself later says the fact is resolved and nobody told the
assistant in words — this module reads the thread.

## The classification, and why it fails toward sending

Given a candidate escalation (`sender`, `subject`, `text`, `received_at`), :func:`classify` answers
one of three ways:

* **SUPERSEDED** — a later message from the SAME sender, inside `window_hours` (default 48h), names
  the SAME fact (`reminders_acks.fact_key` identity, the same normalizer dedupe uses) and carries a
  resolution signal (`../references/watch-resolution-signals.json` — a tracked list, so the vocabulary
  grows without a code change).
* **STILL_OPEN** — a later message from the sender exists inside the window, but none of them both
  name the same fact and carry a resolution signal (a genuinely unrelated later message, or a related
  one that doesn't resolve anything).
* **UNKNOWN** — there is no later message to check, or the door itself couldn't be asked (no
  credentials configured, the bridge is down, a raise anywhere). **Fail-open**: like every gate in
  this tree, an unanswerable question sends, exactly as before.

## The door — reused, never a new one

The Watch peek reads the owner's Gmail and Proton inboxes (`../references/comms-mapping.md`). This
module cannot use the peek's own MCP tools (it runs as a plain subprocess, not a Claude turn), so it
reuses the two STDLIB doors those same channels already have — `gmail_api.py` and `proton_read.py` —
tried in that order: Gmail, then Proton, and only when BOTH fail is the door truly unavailable. Both
are invoked exactly as their own CLIs (a subprocess, parsing the one-line JSON they already print) —
never re-implemented in-process — so a credential change, a bridge outage, or an API shape change
costs nothing here; it costs exactly what it already costs those scripts' other callers. The Gmail
account label is `SENESCHAL_GMAIL_ACCOUNT` (default `personal`, the label the rest of the tree's
examples use).

## Wiring — report-only first

The Telegram send path's Watch gate calls :func:`classify` beside suppression / the reminder-ack gate
/ dedupe / the runtime ack — the same chokepoint every Watch send already funnels through (the prose
peek decides WHAT to say; the code chokepoint decides whether it actually SENDS). It ships
REPORT-ONLY, like dedupe: every armed send computes and logs a `reconcile` verdict, but only a
SUPERSEDED verdict actually blocks the send once `WATCH_RECONCILE_ENFORCE` is set on the sender. It
never inspects 🚨/🛑/`Call Me` — same posture as dedupe and the runtime ack: a superseded fact is
suppressed even carrying a Critical marker (the fact is resolved), and a genuinely new critical alert
about a DIFFERENT fact carries a different `fact_key` and is structurally unreachable here.

## What the peek must supply

Reconciliation only runs when the peek passes `--source-sender` (and, ideally, `--source-subject`)
and `--source-received-at` to `telegram_send.py` alongside `--text` — fields the peek already has from
reading the email it is escalating (`../modes/watch.md`). Omit them and `classify` answers UNKNOWN
without even touching a door (cheap, and correctly conservative: no source, no reconciliation).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling import also works when we're imported, not run

import reminders_acks as ra  # noqa: E402 — fact_key + title_coverage, reused not copied

REFERENCES_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "references"))
SIGNALS_FILE = "watch-resolution-signals.json"

#: The env var naming the Gmail account label door 1 reads (`gmail_api.py --account`).
GMAIL_ACCOUNT_ENV_VAR = "SENESCHAL_GMAIL_ACCOUNT"
DEFAULT_GMAIL_ACCOUNT = "personal"

#: The three verdicts. Named strings (not an IntEnum) so a log line and a CLI print are the same value
#: with no translation step.
SUPERSEDED = "SUPERSEDED"
STILL_OPEN = "STILL_OPEN"
UNKNOWN = "UNKNOWN"

#: Default lookback for "does the source have a later message about this" — long enough to catch a
#: slow-to-resolve fact (a notice and its resolution can be hours apart) with room either side, short
#: enough that a fact which genuinely recurs weeks later isn't matched against ancient mail.
DEFAULT_WINDOW_HOURS = 48.0

#: The bar a later message's tokens must clear against the escalation's `fact_key` to count as the
#: SAME fact. Same value as `reminders_acks.TITLE_MATCH_MIN_COVERAGE`, for the same reason: a lower bar
#: over-matches — two DIFFERENT alerts from one institution (a low-balance notice and an unrelated
#: savings-balance alert) share enough generic vocabulary (the bank's name, "account") that a 0.5 bar
#: lets the wrong fact's resolution silence a real one. 0.6 still clears a genuine notice/resolution
#: pair while separating two facts that merely share an institution.
THREAD_MATCH_MIN_COVERAGE = 0.6


def _parse_dt(value) -> datetime | None:
    """ISO 8601 (tolerant of a trailing ``Z``) or an RFC 2822 ``Date:`` header — the two shapes the
    doors below hand back — to an aware `datetime`. `None` for anything else; total, never raises."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_signals(references_dir: str | None = None) -> list:
    """The tracked resolution-signal vocabulary (`../references/watch-resolution-signals.json`) —
    lowercased, non-empty strings only. Fail-open to `[]`: a missing/corrupt file must never raise
    into the gate, it only ever means nothing can classify as SUPERSEDED (the safe direction)."""
    path = os.path.join(references_dir or REFERENCES_DIR, SIGNALS_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    signals = data.get("signals") if isinstance(data, dict) else None
    if not isinstance(signals, list):
        return []
    return [s.lower() for s in signals if isinstance(s, str) and s.strip()]


def signal_hit(text, signals) -> str | None:
    """The first tracked signal that appears as a literal (case-insensitive) substring of `text`, or
    `None`. Substring, not tokenized — a signal like "no longer" must survive as a phrase, the same
    convention `reminders_acks.NAG_CUES`/`message_chases` already use."""
    if not isinstance(text, str):
        return None
    low = text.lower()
    for sig in signals:
        if sig in low:
            return sig
    return None


# ============================================================================== the doors (reused)

def _run_script(script_name: str, *cli_args: str, timeout: float = 25.0) -> dict | None:
    """Run a sibling stdlib script exactly as its own CLI and parse the one-line JSON it prints.
    `None` on anything that means "this door could not be asked" — the script is absent, the call
    times out, the exit is non-zero, or the output isn't parseable JSON. Never re-implements the
    script's own logic; a change to either script's contract costs exactly what it already costs that
    script's other callers."""
    path = os.path.join(SCRIPT_DIR, script_name)
    if not os.path.exists(path):
        return None
    try:
        res = subprocess.run([sys.executable, path, *cli_args], capture_output=True, text=True,
                             timeout=timeout, cwd=SCRIPT_DIR)
    except Exception:  # noqa: BLE001 — a door that can't run is a door that's unavailable
        return None
    if res.returncode != 0 or not res.stdout:
        return None
    try:
        return json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def _env_file_if_exists(name: str) -> str | None:
    path = os.path.join(SCRIPT_DIR, name)
    return path if os.path.exists(path) else None


def _fetch_via_gmail(sender: str, since: datetime, *, state_dir=None) -> list | None:
    """Messages from `sender` on/after `since`'s calendar day, via `gmail_api.py list` — door 1.
    `None` means the door could not be asked at all (no OAuth token configured on this host, the
    script raised) — NOT the same as an empty, successfully-checked inbox, which is `[]`."""
    account = os.environ.get(GMAIL_ACCOUNT_ENV_VAR) or DEFAULT_GMAIL_ACCOUNT
    query = f"from:{sender} after:{since.strftime('%Y/%m/%d')}"
    cli = ["list", "--account", account, "--query", query, "--max", "25"]
    env_file = _env_file_if_exists("google.env")
    if env_file:
        cli += ["--env-file", env_file]
    payload = _run_script("gmail_api.py", *cli)
    if not payload or not payload.get("ok"):
        return None
    out = []
    for m in payload.get("messages") or []:
        out.append({"id": m.get("id"), "from": m.get("from"), "subject": m.get("subject"),
                    "text": m.get("snippet") or "", "date": m.get("date")})
    return out


def _fetch_via_proton(sender: str, since: datetime, *, state_dir=None) -> list | None:
    """Messages from `sender` on/after `since`'s calendar day, via `proton_read.py` — door 2.
    `proton_read.py` has no server-side sender filter, so this fetches everything since `since`
    (bounded by `--limit`) and filters by sender in Python. `None` means the door could not be asked
    at all (Bridge not running, no credentials, a raise)."""
    cli = ["--mailbox", "INBOX", "--since", since.strftime("%Y-%m-%d"), "--limit", "50", "--bodies"]
    env_file = _env_file_if_exists("proton.env")
    if env_file:
        cli += ["--env-file", env_file]
    payload = _run_script("proton_read.py", *cli)
    if not payload or not payload.get("ok"):
        return None
    needle = sender.lower()
    out = []
    for m in payload.get("messages") or []:
        if needle not in (m.get("from") or "").lower():
            continue
        out.append({"id": m.get("uid"), "from": m.get("from"), "subject": m.get("subject"),
                    "text": m.get("body") or m.get("snippet") or "", "date": m.get("date")})
    return out


def default_fetch_messages(sender: str, since: datetime, *, state_dir=None):
    """Try Gmail, then Proton — two doors, in order. Returns `(messages, door)`; `(None, None)` means
    unavailable — never a third door, never a guess."""
    for door, fetch in (("gmail", _fetch_via_gmail), ("proton", _fetch_via_proton)):
        try:
            messages = fetch(sender, since, state_dir=state_dir)
        except Exception:  # noqa: BLE001 — a broken door is unavailable, never a raise into the gate
            messages = None
        if messages is not None:
            return messages, door
    return None, None


# ==================================================================================== classification

def classify(candidate: dict, *, window_hours: float = DEFAULT_WINDOW_HOURS, now: datetime | None = None,
             state_dir: str | None = None, references_dir: str | None = None,
             fetch_messages=None) -> dict:
    """**The classifier.** `candidate` is `{"sender", "subject", "text", "received_at"}` — what the
    peek has about the escalation it is about to send. Returns::

        {"verdict": SUPERSEDED | STILL_OPEN | UNKNOWN, "reason": str, "fact_key": str,
         "matched": {"message_id", "at", "signal"} | None, "door": "gmail" | "proton" | None}

    Total and fail-open in the UNKNOWN direction: a missing sender/timestamp, an unreadable door, a
    raise anywhere in the fetcher all read the same way — nothing here may be the reason a genuinely
    open fact goes unescalated, and nothing here may be the reason a resolved one keeps firing without
    ever being asked.

    `fetch_messages` is the test seam — `(sender, since, *, state_dir=None) -> (messages, door)` —
    defaulting to :func:`default_fetch_messages`; production passes nothing."""
    text = candidate.get("text") or ""
    key = ra.fact_key(text)
    sender = (candidate.get("sender") or "").strip()
    received_at = _parse_dt(candidate.get("received_at"))
    if not sender or received_at is None:
        return {"verdict": UNKNOWN, "reason": "no-source-metadata", "fact_key": key,
                "matched": None, "door": None}

    fetch = fetch_messages or default_fetch_messages
    try:
        messages, door = fetch(sender, received_at, state_dir=state_dir)
    except Exception:  # noqa: BLE001 — a raising fetcher is exactly a door failure
        messages, door = None, None
    if messages is None:
        return {"verdict": UNKNOWN, "reason": "door-unavailable", "fact_key": key,
                "matched": None, "door": door}

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    cutoff = received_at + timedelta(hours=window_hours)

    later = []
    for m in messages:
        dt = _parse_dt(m.get("date"))
        if dt is None or dt <= received_at or dt > cutoff or dt > instant:
            continue
        later.append((dt, m))
    later.sort(key=lambda pair: pair[0])
    if not later:
        return {"verdict": UNKNOWN, "reason": "no-later-message", "fact_key": key,
                "matched": None, "door": door}

    signals = load_signals(references_dir)
    identity = key or (candidate.get("subject") or text)
    for dt, m in later:
        blob = " ".join(x for x in (m.get("subject"), m.get("text")) if x)
        if ra.title_coverage(identity, blob) < THREAD_MATCH_MIN_COVERAGE:
            continue
        sig = signal_hit(blob, signals)
        if sig:
            stamp = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"verdict": SUPERSEDED, "reason": f"superseded-by:{m.get('id') or stamp}",
                    "fact_key": key, "door": door,
                    "matched": {"message_id": m.get("id"), "at": stamp, "signal": sig}}

    return {"verdict": STILL_OPEN, "reason": "no-resolution-signal", "fact_key": key,
            "matched": None, "door": door}


# =========================================================================================== CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Classify whether a Watch escalation was superseded by a later source message.")
    p.add_argument("--sender", required=True)
    p.add_argument("--subject", default=None)
    p.add_argument("--text", required=True)
    p.add_argument("--received-at", required=True, metavar="ISO8601")
    p.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    p.add_argument("--state-dir", default=None)
    args = p.parse_args(argv)
    result = classify(
        {"sender": args.sender, "subject": args.subject, "text": args.text,
         "received_at": args.received_at},
        window_hours=args.window_hours, state_dir=args.state_dir)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
