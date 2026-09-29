#!/usr/bin/env python3
"""**The Watch runtime ack** — a durable, owner-issued "that's handled" for an arbitrary fact the
Watch comms peek escalated. Standard library + sibling modules only (`reminders_acks`, `stateio`);
never imports `telegram_*` or `presence`, so the Telegram send path can import it optionally.

## What this closes

A Watch peek can escalate the same fact again and again — one bank alert pushed four times in
sixteen hours — even after the owner has said, in chat, *"I already fixed that, stop."* That sentence
used to land in `state/carry-over.md` prose, which the Watch path never reads, so the next push went
out anyway. Dedupe (`reminders_acks.watch_duplicate_blocked`) stops a peek repeating *itself*; it does
nothing when the OWNER is the one who said the fact is handled. This module is the second door: a
durable, timestamped ack for an arbitrary Watch-escalated fact, written the moment the owner says it,
read by the gate before the next escalation of that same fact goes out.

**It is its own module rather than part of `reminders_acks.py`**: dedupe is a predicate over an
existing ledger (`watch-gate.jsonl`); this is a new WRITE surface the warm session uses. Different
shape of problem, different file.

## The store — `state/watch-acks.json`

    {"schema": "seneschal.watch-acks/1",
     "acks": {"<fact_key>": {"text_as_said": "...", "acked_at": "<ISO UTC Z>",
                             "expires_at": "<ISO UTC Z>", "source": "chat"|"reaction"}}}

Keyed by `reminders_acks.escalation_fact_key` — the SAME identity dedupe uses — so an ack and the
escalation it silences agree on identity by construction; there is no second definition of "the same
fact" anywhere in this tree. **Two kinds of key live in the store:**

* a **source key** — `source:alerts@bank.example|notice overdraft|x1234` — for an escalation that
  carries `--source-sender`/`--source-subject` (email-backed): sender + normalized subject + account
  token, derived from the EMAIL, never from the escalation's prose. Every re-escalation of one email
  family shares it, however the peek re-words the alert. An ack on it COVERS THE FAMILY.
* a **prose key** — the bag-of-words `reminders_acks.fact_key` — for a non-source escalation (Slack,
  calendar), and as the fallback identity every escalation still carries.

**Why source keys.** Keyed on prose alone, a peek that re-words every pass mints a NEW key per
rewording — dozens of distinct keys for one alert family in a week — so an ack on one never covered
the next, and an exasperated *"I KNOW, stop"* matched none of them. The prose is what varies; the
email it came from is what doesn't.

Written via `stateio.write_json_atomic` (build-then-`os.replace`, the tree's house rule for
`state/`). Default expiry is **7 days** (`--for '3d'`/`'2w'` to override) — long enough to survive a
slow-to-resolve fact, short enough that a fact which genuinely recurs weeks later (the same account
overdraws again) is not silenced by something that happened long before. Prose-keyed acks keep
working: the gate checks BOTH of an outgoing escalation's keys, so an ack recorded on a prose key
still blocks exactly what it blocked.

## Resolution — NEVER GUESS (the `ack.py` posture)

A guessed ack that lands on the wrong fact is a false suppression, and a suppressed Critical fact
costs incomparably more than a re-ask. So the owner's words alone are NOT run through `fact_key` and
matched against free text — *"I fixed that"* would match nothing distinctive at all, or everything.
Instead, :func:`resolve_fact_key` matches the words against the **candidate pool of facts the peek
actually escalated recently** (:func:`candidate_facts` — the last
:data:`RECENT_ESCALATION_WINDOW_DAYS` days of un-blocked rows in `state/watch-gate.jsonl`, one
distinct `fact_key` each): the ack must name a fact the peek actually said, never an invented one.
Coverage is scored with `reminders_acks.title_coverage` — the SAME function `watch_escalation_blocked`
uses to ask "does this message name that row" — treating each candidate's key as the "title" (a
source key via `reminders_acks.fact_key_match_title`, so it reads as *"bank notice overdraft x1234"*
rather than an address; a prose key as itself) and, equally, the most recent escalation's own prose
key, taking the better of the two — and the ack phrase as the "message". A near-tie is a refusal
naming both candidates, never a pick. **Unmatched or ambiguous refuses (exit 3)** — the caller either
says more of what the escalation named, or resolves the identity itself and passes `--key` or
`--replied-to`.

## `--replied-to` — the owner swipe-replied to the alert itself

Over Telegram a swipe-reply arrives as `(replying to: "<the alert's text>") <the owner's words>`. That
quote IS the alert, so its identity is not a guess: :func:`resolve_replied_to` finds the ledger row
whose logged `text` the quote is a prefix of (whitespace-collapsed, Markdown marks dropped — Telegram
renders `**bold**` before it is quoted) and uses THAT ROW's key — the source key if the row has one,
which is the whole point. No ledger row (the alert predates the ledger, or was cut past 400 chars)
falls back to the quote's own prose key, which is precisely what a caller passing `--key` by hand
would have computed. A chat turn answering an alert the owner replied to MUST pass the quote here (or
its key via `--key`) — see `../modes/chat.md` and `../modes/watch.md`.

## `--key` — the id-first second door

For a caller that already holds the fact key — the warm session, which SAW the escalation message in
the conversation and therefore already knows which fact "that" refers to — `--key` bypasses matching
entirely. This is the expected path for a purely deictic ack ("I fixed that") that carries no
matchable vocabulary of its own: the chat session resolves "that" from context, then calls this tool
with `--key` rather than asking it to guess from a pronoun.

## The gate — `ack_blocks`, ENFORCED, not report-only

Unlike dedupe (which ships report-only under `WATCH_DEDUPE_ENFORCE`, because an automatic judgment
has to earn trust before it can block anything), **an ack IS the owner's explicit instruction** — so
:func:`ack_blocks` blocks unconditionally. The Telegram send path's Watch gate calls it beside the
suppression, reminder-ack and dedupe halves and logs the verdict to the same `state/watch-gate.jsonl`
row shape (`reason: "watch-acked:<acked_at>"`), so it stays measurable even though it is not optional.

`ack_blocks` computes the OUTGOING escalation's keys — `reminders_acks.escalation_fact_key` (the
source key when the sender was given `--source-sender`, else the prose key) AND the prose key — the
same computation `watch_duplicate_blocked` makes — and looks up EITHER in the store. It never
re-derives identity from the ack phrase at send time; that happened once, at ack time, in
:func:`resolve_fact_key` / :func:`resolve_replied_to` or via the `--key` bypass, and the store's key IS
a real escalation key either way. **It never inspects 🚨/🛑/`Call Me`** — same posture as dedupe: an
acked fact is suppressed even carrying a Critical marker, because the owner acked THAT exact fact; a
new critical alert about a DIFFERENT fact carries a different key and is structurally unreachable
here, never specially allowed through.

## Verbs

    python watch_ack.py ack "<the owner's words>" [--key <fact_key> | --replied-to "<quoted alert>"]
                                                  [--for 7d] [--source chat|reaction]
    python watch_ack.py key --text "<alert>" [--source-sender ADDR] [--source-subject TEXT]
    python watch_ack.py list [--all]
    python watch_ack.py unack <fact_key>
    python watch_ack.py status

Exit codes mirror this tree's `ack.py` family: ``0`` success, ``2`` usage, ``3`` refused
(ambiguous/unmatched match, or an `unack` naming a key that isn't in the store).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)  # sibling import also works when we're imported, not run

import reminders_acks as ra  # noqa: E402 — fact_key + the watch-gate.jsonl reader, reused not copied
import stateio  # noqa: E402 — atomic JSON read/write, zero intra-repo imports

SCHEMA = "seneschal.watch-acks/1"
ACKS_FILE = "watch-acks.json"
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

#: How far back to look for a fact the peek actually escalated, when resolving the owner's words with
#: no `--key`. Mirrors `reminders_acks.WATCH_DEDUPE_WINDOW_HOURS`'s reasoning at a coarser grain: long
#: enough to still be resolvable a few days after the fact, short enough that a stale, long-resolved
#: escalation from weeks ago isn't a candidate the owner could accidentally ack.
RECENT_ESCALATION_WINDOW_DAYS = 7

#: How long an ack silences its fact before the gate goes back to judging fresh escalations on their
#: own merits — a fact that genuinely recurs (the same account overdraws again next month) must not
#: stay silenced forever because it was acked once.
DEFAULT_EXPIRY = timedelta(days=7)

#: The bar a candidate fact's distinctive tokens must clear against the ack phrase to count as a match
#: — same value and same reasoning as `reminders_acks.TITLE_MATCH_MIN_COVERAGE`: below it, a phrase
#: naming only a sliver of the fact would risk acking the wrong one; above it, a near-miss correctly
#: refuses rather than silently picking. ITS OWN NAME AND ITS OWN KNOB, deliberately not imported —
#: this predicate is over an ack phrase against a fact_key, a different comparison from a title against
#: a message, and the two thresholds are free to diverge if either is ever tuned.
FACT_MATCH_MIN_COVERAGE = 0.6

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([hdw]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"h": 3600, "d": 86400, "w": 604800}


class DurationError(ValueError):
    """`--for` didn't parse. Its own type so the CLI can print a clean refusal rather than a traceback."""


def parse_duration(spec: str) -> timedelta:
    """`"7d"` / `"3d"` / `"2w"` / `"12h"` / a bare `"7"` (days) → a `timedelta`. Raises
    :class:`DurationError` on anything else — a malformed `--for` must refuse, not silently fall back
    to the default expiry, which would look like it worked."""
    if not isinstance(spec, str):
        raise DurationError(f"--for must be a string like '7d', got {spec!r}")
    m = _DURATION_RE.match(spec)
    if not m:
        raise DurationError(f"--for {spec!r} isn't <number><h|d|w> (examples: 7d, 3d, 2w, 12h)")
    n = int(m.group(1))
    unit = (m.group(2) or "d").lower()
    return timedelta(seconds=n * _UNIT_SECONDS[unit])


def default_state_dir() -> str:
    return DEFAULT_STATE_DIR


def acks_path(state_dir: str | None = None) -> str:
    return os.path.join(state_dir or DEFAULT_STATE_DIR, ACKS_FILE)


def _stamp(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now(now: datetime | None = None) -> datetime:
    dt = now or datetime.now(timezone.utc)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _parse_stamp(value) -> datetime | None:
    """Tolerant `...Z` ISO parse → aware UTC, or `None`. A row this module wrote is always
    parseable; this only guards against a hand-edited or corrupt store."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- store I/O

def load_store(state_dir: str | None = None) -> dict:
    """`{"schema", "acks": {fact_key: {...}}}`. Absent, unreadable, or malformed reads as an EMPTY
    store — fail-open, matching every reader in this tree; a broken store must not crash the gate
    (which would silently stop blocking anything) nor the ack CLI (which would look like data loss)."""
    path = acks_path(state_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"schema": SCHEMA, "acks": {}}
    if not isinstance(data, dict) or not isinstance(data.get("acks"), dict):
        return {"schema": SCHEMA, "acks": {}}
    return {"schema": data.get("schema", SCHEMA),
            "acks": {k: v for k, v in data["acks"].items() if isinstance(v, dict)}}


def save_store(state_dir: str | None, data: dict) -> None:
    stateio.write_json_atomic(acks_path(state_dir), data)


# --------------------------------------------------------------------------- writing

def record_ack(state_dir: str | None, fact_key: str, text: str, *, source: str = "chat",
               for_spec: str | None = None, now: datetime | None = None) -> dict:
    """Write (or overwrite) the ack row for `fact_key`. Overwrites deliberately — a second ack of the
    same fact just refreshes when it expires, mirroring `reminders_acks.record_ack`'s "only the most
    recent ack matters" rule. Returns the row actually written, `fact_key` included."""
    key = (fact_key or "").strip()
    if not key:
        raise ValueError("record_ack needs a non-empty fact_key")
    expiry = parse_duration(for_spec) if for_spec else DEFAULT_EXPIRY
    acked_at = _now(now)
    row = {
        "text_as_said": text or "",
        "acked_at": _stamp(acked_at),
        "expires_at": _stamp(acked_at + expiry),
        "source": source,
    }
    data = load_store(state_dir)
    data["acks"][key] = row
    save_store(state_dir, data)
    return {"fact_key": key, **row}


def remove_ack(state_dir: str | None, fact_key: str) -> bool:
    """Delete the ack row for `fact_key`. Returns whether a row existed to remove — `unack` on an
    absent key is a fact for the caller to report, not a silent no-op."""
    key = (fact_key or "").strip()
    data = load_store(state_dir)
    if key not in data["acks"]:
        return False
    del data["acks"][key]
    save_store(state_dir, data)
    return True


# --------------------------------------------------------------------------- reading

def is_expired(row: dict, now: datetime | None = None) -> bool:
    """An unparseable `expires_at` reads as EXPIRED, not standing — a corrupt row must not silence a
    fact forever; the safer failure is the ack falling off rather than sticking."""
    expires = _parse_stamp(row.get("expires_at"))
    if expires is None:
        return True
    return _now(now) >= expires


def get_ack(state_dir: str | None, fact_key: str) -> dict | None:
    """The raw row for `fact_key` (with `fact_key` merged in), expired or not — `None` if absent.
    Use :func:`active_ack` for the expiry-aware read the gate needs."""
    key = (fact_key or "").strip()
    if not key:
        return None
    row = load_store(state_dir)["acks"].get(key)
    return {"fact_key": key, **row} if isinstance(row, dict) else None


def active_ack(state_dir: str | None, fact_key: str, now: datetime | None = None) -> dict | None:
    """The row for `fact_key` iff it exists AND has not expired. Fail-open on every error (an
    unreadable store, a corrupt row) — this is what the gate calls, and a broken read here must never
    be the reason an ack silently fails to fire OR the reason it wrongly blocks; :func:`load_store`
    already fails open to empty, and :func:`is_expired` already fails open to expired, so the only
    remaining failure mode is an exception in between, caught here."""
    try:
        row = get_ack(state_dir, fact_key)
        if row is None or is_expired(row, now):
            return None
        return row
    except Exception:  # noqa: BLE001 — never the reason a send decision raises
        return None


def list_acks(state_dir: str | None = None, *, now: datetime | None = None,
              include_expired: bool = False) -> list:
    """Every ack row, oldest-first by `acked_at`, each carrying its own `expired` bool. Active-only
    unless `include_expired`."""
    acks = load_store(state_dir)["acks"]
    rows = []
    for key, row in acks.items():
        if not isinstance(row, dict):
            continue
        expired = is_expired(row, now)
        if expired and not include_expired:
            continue
        rows.append({"fact_key": key, "expired": expired, **row})
    rows.sort(key=lambda r: r.get("acked_at") or "")
    return rows


# --------------------------------------------------------------------------- resolution (NEVER GUESS)

def recent_escalations(state_dir: str | None, *, now: datetime | None = None,
                       window_days: float = RECENT_ESCALATION_WINDOW_DAYS) -> list:
    """Every un-blocked `watch-gate.jsonl` row inside `window_days`, as `(ts, key, row)` triples,
    oldest-first — the raw material :func:`candidate_facts` collapses per key and
    :func:`resolve_replied_to` must NOT collapse (a family's earlier rewording is a different text
    from its latest, and the owner may have replied to either). A row with no `fact_key` field derives
    one from its own `text`, exactly like `watch_duplicate_blocked`. Total and fail-open: an
    unreadable ledger contributes nothing."""
    out = []
    try:
        instant = _now(now)
        cutoff = instant - timedelta(days=window_days)
        for row in ra._read_watch_gate_rows(state_dir or DEFAULT_STATE_DIR):
            if row.get("blocked"):
                continue  # never reached the owner — not a fact they could be acking
            key = row.get("fact_key") or ra.fact_key(row.get("text") or "")
            if not key:
                continue
            ts_str = row.get("ts")
            if not isinstance(ts_str, str):
                continue
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001 — an unparseable stamp contributes nothing
                continue
            if cutoff <= ts <= instant:
                out.append((ts, key, row))
    except Exception:  # noqa: BLE001 — an unreadable ledger yields nothing, never raises
        return []
    return out


def candidate_facts(state_dir: str | None, *, now: datetime | None = None,
                    window_days: float = RECENT_ESCALATION_WINDOW_DAYS) -> dict:
    """`{fact_key: {"text": <most recent escalation text>, "ts": <most recent ts>, "title": <what
    the owner's words are scored against>, "text_key": <the prose key of that text>}}` — every
    distinct fact the peek actually escalated (reached the owner — `blocked` is falsy) in the last
    `window_days`, read from `state/watch-gate.jsonl` via `reminders_acks`'s own reader: a generated
    read over an existing record beats a hand-synced second copy — no new store, the same reasoning
    `watch_duplicate_blocked` already uses for this exact file.

    A row with no `fact_key` field (written by an older sender, or by some other caller) is derived on
    the fly from its own truncated `text`, exactly like `watch_duplicate_blocked` does — so this is
    retroactive against the whole ledger too. Total and fail-open: an unreadable ledger contributes
    nothing rather than raising."""
    out: dict = {}
    try:
        for ts, key, row in recent_escalations(state_dir, now=now, window_days=window_days):
            prior = out.get(key)
            if prior is None or ts > prior["ts"]:
                text = row.get("text") or ""
                out[key] = {"text": text, "ts": ts, "title": ra.fact_key_match_title(key),
                            "text_key": row.get("fact_key_text") or ra.fact_key(text)}
    except Exception:  # noqa: BLE001 — an unreadable ledger yields no candidates, never raises
        return {}
    return out


_MARKDOWN_MARKS_RE = re.compile(r"[*_`~]+")
_WS_RE = re.compile(r"\s+")


def _quote_norm(text) -> str:
    """A swipe-reply quote and a ledger row's logged text, made comparable: Markdown marks dropped
    (Telegram renders them before they are quoted), whitespace collapsed, a trailing ellipsis dropped,
    lowercased."""
    if not isinstance(text, str):
        return ""
    s = _MARKDOWN_MARKS_RE.sub("", text)
    s = _WS_RE.sub(" ", s).strip().lower()
    return s[:-1].rstrip() if s.endswith("…") else s


def resolve_replied_to(quoted: str, state_dir: str | None = None, *,
                       now: datetime | None = None) -> dict:
    """Resolve the alert the owner swipe-replied to — its quoted text, as the Telegram poller hands
    it to the warm session — to the key of the ledger row that sent it. **Not a guess**: the quote is
    the alert. Prefers the ledger (so a source-keyed row answers with its SOURCE key, and the ack
    covers the family); falls back to the quote's own prose key when no row matches, which is what a
    caller passing `--key` by hand would have computed.

    Returns `{"ok", "fact_key", "matched_by": "ledger"|"quote", "matched_text", "error"}`."""
    result = {"ok": False, "fact_key": None, "matched_by": None, "matched_text": None, "error": None}
    needle = _quote_norm(quoted)
    if not needle:
        result["error"] = "empty --replied-to quote"
        return result
    best = None  # (ts, key, text)
    for ts, key, row in recent_escalations(state_dir, now=now):
        hay = _quote_norm(row.get("text"))
        if not hay:
            continue
        # Prefix only, and only this direction: the row holds 400 chars, the quote at most 300, so a
        # genuine match is always the quote leading the row. Under-matching falls back to the
        # quote's own prose key, which is the safe direction.
        if hay.startswith(needle):
            if best is None or ts > best[0]:
                best = (ts, key, row.get("text"))
    if best is not None:
        result.update(ok=True, fact_key=best[1], matched_by="ledger", matched_text=best[2])
        return result
    key = ra.fact_key(quoted)
    if not key:
        result["error"] = (f"the quoted alert {quoted[:80]!r} matches no recent escalation and has no "
                           f"identifying words of its own — pass --key <fact_key>")
        return result
    result.update(ok=True, fact_key=key, matched_by="quote", matched_text=quoted)
    return result


def resolve_fact_key(text: str, state_dir: str | None = None, *,
                     now: datetime | None = None) -> dict:
    """Resolve the owner's ack words to a `fact_key` the peek actually escalated recently. **Never
    guesses.**

    Returns `{"ok", "fact_key", "matched_text", "coverage", "candidates", "error"}`. On success: `ok`
    True, `fact_key` + the escalation `matched_text` it came from + the `coverage` score. On failure:
    `ok` False, `error` naming what would fix it, and `candidates` populated for an ambiguous tie.

    Scored with `reminders_acks.title_coverage` — treating each candidate's `fact_key` (already a
    stripped, sorted, distinctive-token string) as the "title" and the owner's words as the "message",
    the SAME direction `watch_escalation_blocked` already asks ("does the message name the row", never
    the reverse). A tie at the best score is an ambiguous refusal naming both, never a pick."""
    result = {"ok": False, "fact_key": None, "matched_text": None, "coverage": None,
              "candidates": [], "error": None}
    if not isinstance(text, str) or not text.strip():
        result["error"] = "empty phrase"
        return result
    candidates = candidate_facts(state_dir, now=now)
    if not candidates:
        result["error"] = (
            f"no Watch escalation in the last {RECENT_ESCALATION_WINDOW_DAYS} days to ack against — "
            f"there is nothing on record for {text!r} to match; if you already know which fact this "
            f"is, resolve it yourself and pass --key <fact_key>")
        return result
    scored = []
    for key, info in candidates.items():
        # The better of: the key as a title (a source key rendered sayable), and the most recent
        # escalation's own prose key — the owner's words name what the peek SAID, which for a
        # source-keyed family is the prose, not the address.
        cov = max(ra.title_coverage(info.get("title") or key, text),
                  ra.title_coverage(info.get("text_key") or "", text))
        if cov >= FACT_MATCH_MIN_COVERAGE:
            scored.append((key, info, cov))
    if not scored:
        result["error"] = (
            f"{text!r} doesn't name any Watch escalation from the last "
            f"{RECENT_ESCALATION_WINDOW_DAYS} days — refusing rather than guessing which fact you "
            f"mean. Say more of what the escalation named, or resolve it yourself and pass "
            f"--key <fact_key>. Recent facts: {', '.join(sorted(candidates)) or '(none)'}")
        return result
    best_cov = max(c for _, _, c in scored)
    winners = [s for s in scored if s[2] == best_cov]
    if len(winners) > 1:
        result["candidates"] = sorted(k for k, _, _ in winners)
        result["error"] = (
            f"ambiguous — {text!r} matches {len(winners)} recent escalated facts equally well: "
            + ", ".join(result["candidates"])
            + ". Refusing rather than guessing; ack the intended one with --key <fact_key>.")
        return result
    key, info, cov = winners[0]
    result.update(ok=True, fact_key=key, matched_text=info.get("text"), coverage=round(cov, 3))
    return result


# --------------------------------------------------------------------------- the gate

def ack_blocks(state_dir: str | None, text: str, now: datetime | None = None, *,
               sender=None, subject=None) -> dict | None:
    """**The gate.** `None` = send it. A dict = this escalation names a fact the owner already acked,
    and must not go out.

    Computes the OUTGOING escalation's keys — `reminders_acks.escalation_fact_key(text, sender,
    subject)` (the source key for an email-backed escalation, else the prose key) and, always, the
    prose key — the same computation dedupe already makes — and looks up either. The source key is
    what makes an ack COVER THE FAMILY: three re-wordings of one alert email all resolve to it, so an
    ack on any of them blocks the fourth. The prose key is still checked so an ack recorded on a prose
    key keeps blocking exactly what it did. ENFORCES unconditionally (no report-only flag): an ack is
    the owner's explicit instruction, and the instrument-before-gate doctrine is about earning trust
    for an automatic judgment, not about a thing the owner said. Fail-open on everything: an
    unidentifiable text, an unreadable store, a raise anywhere → `None`, so a broken ack store can only
    ever fail to suppress, never wrongly suppress."""
    try:
        primary = ra.escalation_fact_key(text, sender, subject)
        if not primary:
            return None
        row, key = None, None
        for key in (primary, ra.fact_key(text)):
            if not key:
                continue
            row = active_ack(state_dir, key, now=now)
            if row is not None:
                break
        if row is None:
            return None
        return {"reason": f"watch-acked:{row['acked_at']}", "fact_key": key,
                "acked_at": row["acked_at"], "expires_at": row.get("expires_at"),
                "source": row.get("source"), "text_as_said": row.get("text_as_said")}
    except Exception:  # noqa: BLE001 — fail-open: this gate may never be the reason the owner wasn't told
        return None


# --------------------------------------------------------------------------- CLI

def _print(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def main(argv=None, *, now: datetime | None = None) -> int:
    p = argparse.ArgumentParser(description="The Watch runtime ack — the owner's 'that's handled'.")
    p.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ack", help="ack a Watch-escalated fact in the owner's own words")
    a.add_argument("text", help="the owner's words, verbatim")
    a.add_argument("--key", default=None, metavar="FACT_KEY",
                   help="bypass matching — for a caller that already resolved the fact_key from "
                        "context (e.g. a deictic 'I fixed that' the chat session already knows the "
                        "referent of)")
    a.add_argument("--replied-to", default=None, metavar="QUOTED_ALERT",
                   help="the alert text the owner swipe-replied to (the '(replying to: \"...\")' "
                        "quote) — resolved to THAT alert's ledger key, never matched against the words")
    a.add_argument("--for", dest="for_spec", default=None, metavar="SPAN",
                   help="how long the ack stands, e.g. 7d / 3d / 2w / 12h (default 7d)")
    a.add_argument("--source", default="chat", choices=["chat", "reaction"],
                   help="where the ack came from (default chat)")

    k = sub.add_parser("key", help="print the key an escalation would be gated on (no write)")
    k.add_argument("--text", required=True, help="the escalation text")
    k.add_argument("--source-sender", default=None, metavar="ADDR")
    k.add_argument("--source-subject", default=None, metavar="TEXT")

    ls = sub.add_parser("list", help="print active ack rows")
    ls.add_argument("--all", action="store_true", help="include expired rows")

    un = sub.add_parser("unack", help="remove an ack by its exact fact_key")
    un.add_argument("fact_key")

    sub.add_parser("status", help="store path + active/expired counts")

    args = p.parse_args(argv)

    if args.cmd == "ack":
        # Validate --for BEFORE any matching/writing, so a bad span refuses (exit 2) the same way for
        # the --key door and the text-resolution door rather than surfacing mid-write on one and not
        # the other.
        if args.for_spec is not None:
            try:
                parse_duration(args.for_spec)
            except DurationError as e:
                print(f"watch_ack: refused — {e}", file=sys.stderr)
                _print({"ok": False, "error": str(e)})
                return 2
        if args.key:
            row = record_ack(args.state_dir, args.key, args.text, source=args.source,
                             for_spec=args.for_spec, now=now)
            _print({"ok": True, "matched_by": "key", **row})
            return 0
        if args.replied_to:
            resolved = resolve_replied_to(args.replied_to, args.state_dir, now=now)
            if not resolved["ok"]:
                print(f"watch_ack: refused — {resolved['error']}", file=sys.stderr)
                _print({"ok": False, "error": resolved["error"], "candidates": []})
                return 3
            row = record_ack(args.state_dir, resolved["fact_key"], args.text, source=args.source,
                             for_spec=args.for_spec, now=now)
            _print({"ok": True, "matched_by": f"replied-to-{resolved['matched_by']}",
                    "matched_text": resolved["matched_text"], **row})
            return 0
        resolved = resolve_fact_key(args.text, args.state_dir, now=now)
        if not resolved["ok"]:
            print(f"watch_ack: refused — {resolved['error']}", file=sys.stderr)
            _print({"ok": False, "error": resolved["error"], "candidates": resolved["candidates"]})
            return 3
        row = record_ack(args.state_dir, resolved["fact_key"], args.text, source=args.source,
                         for_spec=args.for_spec, now=now)
        _print({"ok": True, "matched_by": "text", "matched_text": resolved["matched_text"],
                "coverage": resolved["coverage"], **row})
        return 0

    if args.cmd == "key":
        primary = ra.escalation_fact_key(args.text, args.source_sender, args.source_subject)
        _print({"ok": bool(primary), "fact_key": primary or None,
                "kind": "source" if ra.is_source_fact_key(primary) else "text",
                "fact_key_text": ra.fact_key(args.text) or None})
        return 0 if primary else 3

    if args.cmd == "list":
        for row in list_acks(args.state_dir, now=now, include_expired=args.all):
            _print(row)
        return 0

    if args.cmd == "unack":
        removed = remove_ack(args.state_dir, args.fact_key)
        if not removed:
            print(f"watch_ack: no ack on record for {args.fact_key!r}", file=sys.stderr)
            _print({"ok": False, "fact_key": args.fact_key})
            return 3
        _print({"ok": True, "fact_key": args.fact_key})
        return 0

    # status
    active = list_acks(args.state_dir, now=now, include_expired=False)
    all_rows = list_acks(args.state_dir, now=now, include_expired=True)
    _print({"ok": True, "path": acks_path(args.state_dir), "active": len(active),
            "expired": len(all_rows) - len(active)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
