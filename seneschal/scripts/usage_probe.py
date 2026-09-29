#!/usr/bin/env python3
"""Plan-meter telemetry: read `/usage` headlessly, parse it defensively, write ONE row per attempt.

`../docs/usage-telemetry-spec.md` is the plan of record and this module is its phase 1. Read the spec
before changing anything here; the four rules below are its §3.2/§3.3 and they are not style choices.

**IT DOES NOT SPEAK.** A reading produces a row in `state/plan-usage.jsonl` and no message, on every
path. The meters are an observation, never a nag: `SKILL.md`'s Oikonomos entry (Advisor Chain order
15) keeps budget talk to the governor's own rails, and nothing about a usage LEVEL is the plan meter's
to say. Nothing in this file imports a send path, and nothing may be added that does — a test asserts
that against the import graph. Whether the level should ever speak is spec §8.2, and it is the owner's
decision, not this module's to anticipate.

The four invariants, each of which a later edit will be tempted to break:

  1. **Every attempt writes exactly one row.** There is no path that tries a reading and records
     nothing. An authentication outage (no new `claude` session can sign in at all) must appear as
     `auth_failed` rows, not as an absence — an absence is indistinguishable from a quiet morning.
  2. **A non-`ok` row carries NO numeric meter fields at all** — not `0`, not `null` beside a
     populated neighbour. Absent, so a consumer that assumes presence raises instead of averaging a
     zero into a burn rate. `assert_no_meter_fields()` is the executable form of this.
  3. **Never carry forward.** No interpolation, no last-known-good fill. A gap is information.
  4. **Tolerance is paired with a detector.** The parser is deliberately loose (§3.2), which is
     exactly how a wording change gets absorbed unnoticed — so every row carries `parser_version`,
     `cli_version` and `shape_fingerprint`, and a fingerprint change on an otherwise-`ok` row is a
     finding.

**Measured, and it corrects an early spec assumption:** `/usage` is rendered CLIENT-SIDE. The
envelope comes back `num_turns: 0`, `total_cost_usd: 0`, `modelUsage: {}` and an all-zero `usage`
block, and the report is byte-identical to a default-model capture. So the observer effect is **zero
tokens and one session**, and the model pin costs nothing either way; it is kept because a pin cannot
regress if the CLI ever starts routing this through a model. The contamination that matters is the
SESSION COUNT (the number the panel publishes), which is why the cadence is hourly.

**Every row also says WHOSE meter it is** (§4.2.1). An owner who hits a weekly ceiling and switches
the CLI onto a second subscription interleaves two meters with two weekly resets in this one file, and
a delta taken across that seam is arithmetic on unrelated quantities. `read_account` stamps an
`account` block on EVERY outcome, `stamp_account_change` marks the boundary, and an identity that
cannot be read is `{"known": false}` — never the previous row's, which is invariant 3 again.

Fully testable with no spawn, no network and no spend: `runner` and `account_reader` are both
injectable, and `--state-dir` is REQUIRED everywhere — nothing here defaults into the live state
directory, and no test may read the host's real `~/.claude.json` or `~/.claude/.credentials.json`.

This cluster is three modules. `usage_probe.py` (this file) takes the reading; the daemon drives it
on a cadence (a `presence.py` hook, `USAGE_INTERVAL_MIN_DEFAULT` = 60 min) via
`claude -p "/usage" --output-format json` from a scratch cwd. `usage_activity.py` is the *"what was
happening in that interval"* half — a pure read over five other sources. `usage_health.py` is the
one part allowed to speak, and only about the INSTRUMENT: **THE INSTRUMENT SPEAKS; THE METER DOES
NOT, AND THE TWO MAY NOT LEAK INTO EACH OTHER** (spec §8.3 — failure should speak, but nothing
excessive; see its own docstring for the announce/recover contract).

The account-identity fields (`read_account`, `credential_fingerprint`, `account_changed`, the
`state/accounts.json` label map) are documented inline at each function. The summary: **TWO SIGNALS
BECAUSE THEY CAN DISAGREE** — `oauthAccount` is a CACHED blob (hence `profile_fetched_at`),
`credential_fingerprint` is 12 hex chars of SHA-256 (never the token or any prefix of it) over
`claudeAiOauth` ONLY, never the `mcpOAuth` tokens beside it. **THE LABEL MAP IS A CONVENIENCE, NEVER A
DEPENDENCY** — absent, unreadable or unmapped all mean no label, never an error. **ADDITIVE: schema
unmoved, `PARSER_VERSION` unmoved, NO OLD ROW REWRITTEN OR BACKFILLED.** The suites repoint
`CLAUDE_JSON_PATH`/`CREDENTIALS_PATH` so no test can read real credentials even if a call site forgets
to inject. **NO UUID, EMAIL OR FINGERPRINT MAY EVER LAND IN A TRACKED FILE, COMMIT MESSAGE, FIXTURE,
THE RAG CORPUS OR A PR BODY** (§6.3).

Timezone: reset stamps in the report carry no year and are read on the owner's wall clock — the zone
of the aware `now_local` a caller passes, which defaults to `tz_common.local_now()` (the configured
owner zone, machine-local when unconfigured). The zone the report itself names is stored verbatim
beside the parsed instant, so a mismatch is visible in the row rather than absorbed into arithmetic.

Field contract and the full activity schema: `../state/README.md` → `plan-usage.jsonl`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import stateio

try:  # the owner's configured zone; absent → the machine-local clock (tz_common's own fallback)
    import tz_common as _tz_common
except Exception:  # noqa: BLE001 — a missing helper must never cost a reading
    _tz_common = None

SCHEMA = "seneschal.plan-usage/1"

# Bumped by hand whenever the parsing rules below change meaning. It is on every row so a break can
# be correlated with a release rather than guessed at (spec §3.4).
PARSER_VERSION = 1

READINGS_FILENAME = "plan-usage.jsonl"

# Wall-clock ceiling on one reading. Measured at ~14 s end-to-end (~3 s of it inside the CLI). A
# reading that hangs must become a `timeout` ROW, never a stuck child.
DEFAULT_TIMEOUT_SEC = 60
# `claude --version` is a separate, session-less, token-free spawn. Short leash: a missing version is
# a null field, never a lost reading.
VERSION_TIMEOUT_SEC = 20

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# §3.3: on a non-`ok` outcome the raw report is retained so a wording change is diagnosable after the
# fact. The whole artifact is ~1.2 KB, so 4 KB is generous and still bounded.
RAW_CAP_BYTES = 4096
STDERR_TAIL_CAP = 800
UNMATCHED_CAP = 24
UNMATCHED_LINE_CAP = 240

# Outcomes (§3.3). `skipped` is written by the CALLER (the daemon's deferral gate), never here.
OK = "ok"
PARTIAL = "partial"
UNPARSED = "unparsed"
AUTH_FAILED = "auth_failed"
SPAWN_FAILED = "spawn_failed"
TIMEOUT = "timeout"
SKIPPED = "skipped"
OUTCOMES = (OK, PARTIAL, UNPARSED, AUTH_FAILED, SPAWN_FAILED, TIMEOUT, SKIPPED)
# The outcomes that may carry meter numbers at all. Everything else is invariant 2's territory.
NUMERIC_OUTCOMES = (OK, PARTIAL)

# Substrings that mean "the CLI could not authenticate", checked case-folded. **Only consulted on a
# path that has ALREADY failed** (non-zero exit, or zero meters matched) — a healthy report that
# happened to contain one of these words must never be relabelled an outage. That ordering is the
# whole safety of the list, and it is why the list can afford to be broad.
AUTH_SIGNATURES = (
    "authentication_error",
    "authentication failed",
    "invalid api key",
    "invalid bearer token",
    "oauth token",
    "oauth error",
    "not authenticated",
    "unauthorized",
    "please run /login",
    "run `/login`",
    "log in again",
    "session expired",
    "token has expired",
    "credit balance is too low",
)


# --------------------------------------------------------------------------- small stdlib helpers

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    """The owner's wall clock, aware — `tz_common.local_now()` (the configured owner zone, with its
    documented fallback to machine-local). Every row's `at` and every reset stamp's year inference
    reads from this, so both are on the same clock the owner's day is on."""
    if _tz_common is not None:
        return _tz_common.local_now()
    return datetime.now().astimezone()


def parse_iso(s):
    """Tolerant ISO-8601 → aware datetime, or None. Accepts the trailing `Z` the daemon writes."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def readings_path(state_dir: str) -> str:
    return os.path.join(state_dir, READINGS_FILENAME)


def child_env(source: str = "usage") -> dict:
    """Env for the probe child: scrub `ANTHROPIC_API_KEY` so the reading bills the subscription it is
    measuring, and stamp `SENESCHAL_SESSION_SOURCE`.

    Duplicated from `presence.child_env` / `jobs.child_env` rather than imported: the daemon imports
    THIS module, so importing back is circular. The invariant it keeps — nothing under the daemon's
    supervision inherits a stray API key — holds without exception. A reading billed to the metered
    API would measure nothing.

    `usage` is a new source value and is deliberately outside `jobs.ASSISTANT_SURFACES` and every
    send-gate source list — this probe has no send path at all, so it must not resemble one."""
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env["SENESCHAL_SESSION_SOURCE"] = source
    return env


# --------------------------------------------------------------------------- §3.2 the parser

# Rule 3 — anchor on the stable keywords and treat the separator as filler. The real separator is
# U+00B7 MIDDLE DOT (verified by byte dump, twice), and the disclaimer line carries U+2014 EM DASH.
# **Neither appears in any pattern here**, deliberately: a regex holding a literal `·` breaks on the
# first release that switches to an en dash, and that break is invisible because the NUMBERS are
# still in the line.
_METER_RE = re.compile(
    r"^\s*(?P<label>[^:]{1,80}?)\s*:\s*(?P<pct>\d{1,3})\s*%\s*used\b(?P<rest>.*)$",
    re.IGNORECASE,
)
_RESETS_RE = re.compile(r"\bresets\b\s*(?P<when>.+?)\s*$", re.IGNORECASE)
_TZ_RE = re.compile(r"\(([^)]{1,40})\)\s*$")
_BLOCK_HEAD_RE = re.compile(r"^\s*Last\s+(?P<span>[0-9]{1,3}\s*[A-Za-z]{1,3})\b(?P<rest>.*)$",
                            re.IGNORECASE)
_COUNT_RE = r"(?P<n>\d[\d,]*)\s+%s\b"
_BEHAVIOR_RE = re.compile(r"^\s+(?P<pct>\d{1,3})\s*%\s*(?P<phrase>\S.*?)\s*$")
_TOP_RE = re.compile(r"^\s+Top\s+(?P<category>[A-Za-z][A-Za-z ]{0,30}?)\s*:\s*(?P<items>\S.*?)\s*$")
_TOP_ITEM_RE = re.compile(r"^(?P<name>.+?)\s+(?P<pct>\d{1,3})\s*%$")
_PAREN_RE = re.compile(r"\(([^)]{1,60})\)")

# The clock-time in a reset stamp: "3:49pm" and "5pm" both occur (minutes omitted on the hour).
_RESET_WHEN_RE = re.compile(
    r"^(?P<mon>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})\s*,?\s*"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?",
    re.IGNORECASE,
)
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

# What `ok` requires (§3.3). NOT a hard-coded `week_fable`: rule 4 generalises the per-model weekly
# meter to `week_<model_slug>`, so requiring the literal name would turn every row `partial` the day
# Anthropic renames the model — which is a finding the fingerprint already reports, at the cost of
# making the outcome field useless. Required is: the session meter, the all-models weekly meter, and
# AT LEAST ONE per-model weekly meter.
REQUIRED_METERS = ("session", "week_all_models")
REQUIRED_MODEL_WEEK = True


def _slug(text: str) -> str:
    """A canonical key fragment: lowercase, non-alphanumerics collapsed to `_`."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (text or "").lower())).strip("_")


def canonical_meter_key(label: str) -> str | None:
    """Rule 4 — **the meter key is DERIVED, never the label.** The desktop sidebar panel calls the
    first meter *"5-hour limit"*; the headless text calls it *"Current session"*. Same
    meter, two names, and neither may become the key.

    Returns None for anything unrecognised — which is a FINDING, not a discard: the caller appends it
    verbatim to `unrecognised_meters[]` so a third weekly pool announces itself on the row that first
    sees it, instead of being silently under-reported forever."""
    low = (label or "").lower()
    if "week" in low:
        paren = _PAREN_RE.search(label or "")
        if not paren:
            return None
        inner = paren.group(1).strip()
        if re.sub(r"[^a-z]", "", inner.lower()) in ("allmodels", "allmodel"):
            return "week_all_models"
        slug = _slug(inner)
        return f"week_{slug}" if slug else None
    if "session" in low or re.search(r"\b5[\s-]*hour\b", low):
        return "session"
    return None


def parse_reset_stamp(when: str, now_local: datetime | None = None):
    """`"Aug 27, 3:49pm (Region/City)"` → `(iso_string_or_None, tz_text_or_None)`.

    The stamp carries **no year**, so the year is inferred as whichever of {this, prev, next} puts
    the instant closest to `now_local` — which is what makes a late-December reset that resets in
    January come out right instead of eleven months wrong.

    The wall-clock time is read in the zone of `now_local` — the owner's clock (`local_now`, i.e.
    `tz_common`), because the CLI renders the report in the zone of the machine it runs on and the
    owner's configured zone is the tree's one answer to "which zone is that". A `ZoneInfo` zone is
    DST-correct at the reset instant; the machine-local fallback is a fixed-offset snapshot. The zone
    named in the report is returned SEPARATELY and stored verbatim beside the parsed value, so if the
    report ever names a zone the owner is not configured in, the mismatch is visible in the row rather
    than silently absorbed into arithmetic."""
    tz_text = None
    text = (when or "").strip()
    m_tz = _TZ_RE.search(text)
    if m_tz:
        tz_text = m_tz.group(1).strip()
        text = text[: m_tz.start()].strip()
    m = _RESET_WHEN_RE.match(text)
    if not m:
        return None, tz_text
    month = _MONTHS.get(m.group("mon")[:3].lower())
    if not month:
        return None, tz_text
    hour = int(m.group("hour"))
    minute = int(m.group("minute") or 0)
    ampm = (m.group("ampm") or "").replace(".", "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None, tz_text
    now_local = now_local or local_now()
    if now_local.tzinfo is None:
        now_local = now_local.astimezone()
    zone = now_local.tzinfo
    best = None
    for year in (now_local.year - 1, now_local.year, now_local.year + 1):
        try:
            naive = datetime(year, month, int(m.group("day")), hour, minute)
        except ValueError:
            continue  # e.g. Feb 29 in a non-leap year
        aware = naive.replace(tzinfo=zone)  # the owner's wall clock (see the docstring)
        delta = abs((aware - now_local).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, aware)
    if best is None:
        return None, tz_text
    return best[1].isoformat(), tz_text


def _int(text: str):
    try:
        return int(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _canon_span(span: str) -> str:
    return "last_" + _slug(span)


def parse_report(text: str, now_local: datetime | None = None) -> dict:
    """The whole §3.2 parser. Pure — no clock beyond `now_local`, no I/O, no spawn.

    Returns a dict with `meters`, `unrecognised_meters`, `blocks`, `unmatched`, `line_kinds` and
    `shape_fingerprint`. It NEVER raises and it never guesses: a line it does not recognise goes to
    `unmatched`, and the caller decides what that means for the outcome."""
    meters: dict = {}
    unrecognised: list = []
    blocks: dict = {}
    unmatched: list = []
    kinds: set = set()
    fp_labels: list = []
    fp_phrases: list = []
    fp_tops: list = []
    fp_spans: list = []
    current = None

    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        indented = line[:1].isspace()

        m = _METER_RE.match(line) if not indented else None
        if m:
            kinds.add("meter")
            label = m.group("label").strip()
            fp_labels.append(label)
            rest = m.group("rest") or ""
            m_res = _RESETS_RE.search(rest)
            resets_text = m_res.group("when").strip() if m_res else None
            resets_at, resets_tz = (parse_reset_stamp(resets_text, now_local)
                                    if resets_text else (None, None))
            entry = {"pct": int(m.group("pct")), "label": label}
            if resets_text:
                entry["resets_text"] = resets_text  # verbatim, so a bad parse is still diagnosable
            if resets_at:
                entry["resets_at"] = resets_at
            if resets_tz:
                entry["resets_tz"] = resets_tz
            key = canonical_meter_key(label)
            if key is None or key in meters:
                # Rule 4: an unknown meter — or a second meter claiming a key already taken — is a
                # finding, kept verbatim. Never dropped, never allowed to overwrite.
                unrecognised.append(dict(entry, reason="unknown_label" if key is None
                                         else f"duplicate_key:{key}"))
            else:
                meters[key] = entry
            current = None
            continue

        m = _BLOCK_HEAD_RE.match(line) if not indented else None
        if m:
            kinds.add("block_head")
            span = re.sub(r"\s+", "", m.group("span"))
            fp_spans.append(span)
            rest = m.group("rest") or ""
            block: dict = {"span": span}
            for field, word in (("requests", "requests"), ("sessions", "sessions")):
                found = re.search(_COUNT_RE % word, rest, re.IGNORECASE)
                value = _int(found.group("n")) if found else None
                if value is not None:
                    block[field] = value
            key = _canon_span(span)
            blocks.setdefault(key, block)
            current = blocks[key]
            continue

        if indented and current is not None:
            m = _BEHAVIOR_RE.match(line)
            if m:
                kinds.add("behavior")
                # Rule 5 — the phrase is the key, VERBATIM and un-normalised. Not an enum: new
                # behaviour categories will appear, and a phrase-keyed dict absorbs them where an
                # enum drops them without a sound. The leading "of your usage " is NOT stripped
                # either — stripping a fixed prefix is itself a normalisation that can break, and the
                # bytes it saves are worth nothing. (The spec's §4.2 example row shows the stripped
                # form; it is illustrative, and rule 5 is the binding half. Amended there.)
                phrase = m.group("phrase")
                fp_phrases.append(phrase)
                current.setdefault("behaviors", {})[phrase] = int(m.group("pct"))
                continue
            m = _TOP_RE.match(line)
            if m:
                kinds.add("top")
                category = _slug(m.group("category"))
                fp_tops.append(m.group("category").strip())
                items: dict = {}
                leftovers: list = []
                for chunk in m.group("items").split(","):
                    chunk = chunk.strip()
                    if not chunk:
                        continue
                    im = _TOP_ITEM_RE.match(chunk)
                    if im:
                        items[im.group("name").strip()] = int(im.group("pct"))
                    else:
                        leftovers.append(chunk[:UNMATCHED_LINE_CAP])
                if items:
                    current.setdefault("top", {})[category] = items
                if leftovers:
                    current.setdefault("top_unparsed", {})[category] = leftovers
                continue

        # Rule 2 — every attribution line is optional, and so is every line of prose. The preamble
        # and the disclaimer are recognised only so they do not masquerade as a structural change.
        low = line.lower()
        if (low.startswith("you are currently using")
                or low.startswith("what's contributing")
                or low.startswith("approximate, based on")):
            kinds.add("preamble")
            continue
        kinds.add("unmatched")
        if len(unmatched) < UNMATCHED_CAP:
            unmatched.append(line.strip()[:UNMATCHED_LINE_CAP])

    return {
        "meters": meters,
        "unrecognised_meters": unrecognised,
        "blocks": blocks,
        "unmatched": unmatched,
        "line_kinds": sorted(kinds),
        "shape_fingerprint": shape_fingerprint(sorted(kinds), fp_labels, fp_phrases, fp_tops,
                                               fp_spans),
    }


def shape_fingerprint(kinds, labels, phrases, tops, spans) -> str:
    """§3.4 — a short stable hash of the report's STRUCTURE and not its numbers.

    Without it, *"it still parses"* and *"nothing changed"* are the same observation. With it they
    are two, and a fingerprint change on an otherwise-`ok` row is the early warning that the panel's
    wording moved while the parse happened to survive.

    Every digit is replaced by `#` before hashing, so the percentages, request counts and reset times
    that change on every single reading cannot move it — only wording, ordering-independent
    membership, and the set of line kinds can."""
    def norm(values):
        return sorted({re.sub(r"\d+", "#", str(v).strip()) for v in values})
    payload = json.dumps({
        "v": PARSER_VERSION,
        "kinds": sorted(kinds),
        "labels": norm(labels),
        "phrases": norm(phrases),
        "tops": norm(tops),
        "spans": norm(spans),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def classify(parsed: dict) -> tuple:
    """(outcome, missing_required) for an exit-0 reading, per §3.3."""
    meters = parsed.get("meters") or {}
    missing = [k for k in REQUIRED_METERS if k not in meters]
    if REQUIRED_MODEL_WEEK and not any(
            k.startswith("week_") and k != "week_all_models" for k in meters):
        missing.append("week_<model>")
    if not meters:
        return UNPARSED, missing
    return (OK if not missing else PARTIAL), missing


def looks_like_auth_failure(*texts) -> str | None:
    """The matched signature, or None. See `AUTH_SIGNATURES` for why this is only ever consulted on
    a path that has already failed."""
    blob = " ".join(t for t in texts if isinstance(t, str)).lower()
    for sig in AUTH_SIGNATURES:
        if sig in blob:
            return sig
    return None


# --------------------------------------------------------------------------- §4.2.1 whose meter is it

# The two independent halves of *"which subscription is this reading measuring?"*, as module
# constants so a test can point them somewhere harmless. **No test may read the real pair**, and
# nothing below ever reaches a path outside the two it is handed.
CLAUDE_JSON_PATH = os.path.join(os.path.expanduser("~"), ".claude.json")
CREDENTIALS_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")

# The OPTIONAL human-label map, in the state dir beside the readings. A convenience, never a
# dependency — see `account_label`.
ACCOUNTS_MAP_FILENAME = "accounts.json"
ACCOUNTS_MAP_SCHEMA = "seneschal.accounts/1"

# 48 bits. Enough that two accounts never collide; far too little to attack the token behind it.
# **A truncated hash is not a truncated secret** — the distinction the whole field rests on.
FINGERPRINT_CHARS = 12

# `oauthAccount` key → row key. Deliberately NOT the whole blob: `displayName`, `fullName` and the
# onboarding flags say nothing about which meter a row belongs to, and would put a person's name in
# a machine series for no gain.
_ACCOUNT_FIELDS = (
    ("accountUuid", "account_uuid"),
    ("emailAddress", "email"),
    ("organizationUuid", "organization_uuid"),
    ("billingType", "billing_type"),
    ("subscriptionCreatedAt", "subscription_created_at"),
    ("accountCreatedAt", "account_created_at"),
)


def _read_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def credential_fingerprint(path: str | None = None) -> tuple:
    """`(fingerprint, detail)` — the first `FINGERPRINT_CHARS` hex chars of a SHA-256 over the live
    OAuth access token, or `(None, why)`.

    **The token never leaves this function, and neither does any prefix of it.** What is logged is a
    truncated hash; what is read is written nowhere.

    Why a second signal exists at all: `~/.claude.json`'s `oauthAccount` is a **cached profile blob**
    carrying its own `profileFetchedAt`, while `.credentials.json` is what the CLI actually
    authenticates with. A fingerprint that moves while the uuid does not is exactly the discrepancy
    worth catching — the row would otherwise name an account the CLI has already left.

    **Only `claudeAiOauth` is hashed.** The same file holds an `mcpOAuth` token per connected MCP
    server; those are other services' secrets, they say nothing about which subscription is billed,
    and they are not touched."""
    path = path or CREDENTIALS_PATH
    try:
        data = _read_json(path)
    except OSError as e:
        return None, type(e).__name__
    except ValueError:
        return None, "not JSON"
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token.strip():
        return None, "no claudeAiOauth.accessToken"
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS], None


def account_label(uuid, *, state_dir: str | None = None, path: str | None = None):
    """The human label for a uuid from the optional `state/accounts.json`, or None.

    **Absent, unreadable, wrong-schema and unmapped are all simply "no label"** — never an error and
    never a guess. Reading a meter must not be able to fail because a nicety was missing; an
    instrument that breaks over its own garnish is worse than one with no garnish at all.

    Only `label` is taken. The map's `email` is hand-typed convenience while the profile's is the
    measurement, and letting a stale map overwrite a fresh read is how a mislabelled row is born."""
    if not uuid:
        return None
    if not path:
        if not state_dir:
            return None
        path = os.path.join(state_dir, ACCOUNTS_MAP_FILENAME)
    try:
        data = _read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != ACCOUNTS_MAP_SCHEMA:
        return None
    accounts = data.get("accounts")
    entry = accounts.get(uuid) if isinstance(accounts, dict) else None
    label = entry.get("label") if isinstance(entry, dict) else None
    return label if isinstance(label, str) and label.strip() else None


def read_account(*, state_dir: str | None = None, claude_json: str | None = None,
                 credentials: str | None = None, accounts_map: str | None = None) -> dict:
    """**Whose meter is this row?** — read FRESH at reading time, never remembered between readings.

    An owner can run one subscription to its weekly ceiling and switch the CLI onto a second one
    mid-file. A row that says only what the meter read then silently interleaves two meters with two
    weekly resets: **any burn rate computed across that seam is arithmetic on unrelated quantities.**
    The fix is not to clean up old rows — it is to make every row say which account it belongs to.

    Two independent signals, deliberately, because they can disagree: the cached `oauthAccount`
    profile and a fingerprint of the live credential (spec §10.5 measured what the two can and
    cannot testify to about the moment of a switch).

    **Unknown is a value.** Every failure path returns `{"known": false, "detail": …}` and the row
    still writes — invariant 3 forbids inheriting the previous row's identity, and that inheritance
    is precisely what would make this instrument lie on the one row where it mattered."""
    src = claude_json or CLAUDE_JSON_PATH
    cred_src = credentials or CREDENTIALS_PATH
    block: dict = {"known": False}

    detail = None
    data = None
    try:
        data = _read_json(src)
    except OSError as e:
        detail = f"{os.path.basename(src)}: {type(e).__name__}"
    except ValueError:
        detail = f"{os.path.basename(src)}: not JSON"
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    if detail is None and (not isinstance(oauth, dict) or not oauth.get("accountUuid")):
        detail = f"{os.path.basename(src)}: no oauthAccount.accountUuid"

    if detail is not None:
        block["detail"] = detail
    else:
        block["known"] = True
        for key, out in _ACCOUNT_FIELDS:
            value = oauth.get(key)
            if isinstance(value, str) and value.strip():
                block[out] = value
            elif isinstance(value, (bool, int, float)):
                block[out] = value
        fetched = oauth.get("profileFetchedAt")
        if isinstance(fetched, (int, float)) and not isinstance(fetched, bool) and fetched > 0:
            # Epoch ms → local ISO: how STALE the identity above is. The profile is a CACHE, so a
            # row can name an account the credential beside it has already left, and this is the
            # only field that lets a reader see that rather than assume it away.
            instant = datetime.fromtimestamp(fetched / 1000, timezone.utc)
            local = (_tz_common.to_local(instant) if _tz_common is not None
                     else instant.astimezone())
            block["profile_fetched_at"] = local.isoformat(timespec="seconds")
        label = account_label(block.get("account_uuid"), state_dir=state_dir, path=accounts_map)
        if label:
            block["label"] = label

    fingerprint, fp_detail = credential_fingerprint(cred_src)
    if fingerprint:
        block["credential_fingerprint"] = fingerprint
    elif fp_detail:
        block["credential_detail"] = fp_detail
    block["source"] = src
    block["credential_source"] = cred_src
    return block


def account_changed(previous, current) -> bool:
    """Did the logged-in account change between two rows' `account` blocks? Pure, and public: any
    consumer of `plan-usage.jsonl` can call it with two rows' blocks and get the writer's answer.

    True **only** when both sides name a uuid and the two differ. An unknown side is neither a change
    nor a non-change, and the False it returns is not a claim that nothing happened — it is the
    absence of evidence, which `stamp_account_change` records separately so that a reader cannot
    mistake it for continuity."""
    prev = previous.get("account_uuid") if isinstance(previous, dict) else None
    cur = current.get("account_uuid") if isinstance(current, dict) else None
    return bool(prev and cur and prev != cur)


def stamp_account_change(account: dict, previous_row) -> dict:
    """Mark the boundary on `account`, in place, and return it. Three cases, deliberately distinct:

    - **no previous row at all** → `first_reading: true`. The first row of a file is not evidence of
      a switch, and calling it one puts a fake boundary at the head of every fresh state dir;
    - **a previous row with no known uuid** → `previous_account_known: false`. Every row written
      before this field existed lands here, and it reads as UNKNOWN rather than as *"the main
      account"* — assuming which meter an old row belonged to is the exact error this exists to stop;
    - **a real switch** → `changed: true` plus `previous_account_uuid`.

    **A reader that computes a delta across a `changed: true` boundary is comparing two different
    meters** — two subscriptions, two weekly pools, two reset clocks. That difference is not a burn
    rate and no framing makes it one; the interval must be SPLIT at the boundary, never spanned."""
    if not isinstance(account, dict):
        return account
    if previous_row is None:
        account["first_reading"] = True
        return account
    previous = previous_row.get("account") if isinstance(previous_row, dict) else None
    prev_uuid = previous.get("account_uuid") if isinstance(previous, dict) else None
    if not prev_uuid:
        account["previous_account_known"] = False
    elif account_changed(previous, account):
        account["changed"] = True
        account["previous_account_uuid"] = prev_uuid
    return account


def account_for_row(state_dir: str, previous_row=None, reader=None) -> dict:
    """The one door a caller should use: `read_account` plus the boundary stamp, wrapped so that
    **nothing here can ever cost a row** (invariant 1). `reader` is injectable exactly as `runner`
    is, which is what keeps the whole path testable without touching the host's real credentials."""
    reader = reader or read_account
    try:
        account = reader(state_dir=state_dir)
    except Exception as e:  # noqa: BLE001 — identity is a stamp on the row, never a gate on it
        account = {"known": False, "detail": f"{type(e).__name__}: {e}"}
    if not isinstance(account, dict):
        account = {"known": False, "detail": f"reader returned {type(account).__name__}"}
    return stamp_account_change(account, previous_row)


# --------------------------------------------------------------------------- invariant 2, executable

# `account` is deliberately NOT here and must never be added: account identity is not a meter
# reading. Invariant 2 withholds NUMBERS from a failed row — whose subscription failed to be read is
# a fact the failure itself does not put in doubt, and withholding it would recreate the very
# ambiguity this field exists to remove.
_METER_BEARING_KEYS = ("meters", "week_window_id", "last_24h", "last_7d", "blocks")


def assert_no_meter_fields(row: dict) -> None:
    """Invariant 2, as code rather than as a comment. A non-`ok`/`partial` row must carry no meter
    numbers **at all** — absent, so a consumer raises instead of averaging a zero into a burn rate.

    Raises AssertionError; called on every write and asserted directly in the tests. The `probe`
    block is deliberately NOT in scope: it is the instrument's own measured cost, which is real on a
    failed reading too and is the one number a failed reading can still honestly report."""
    if row.get("outcome") in NUMERIC_OUTCOMES:
        return
    present = [k for k in _METER_BEARING_KEYS if k in row]
    present += [k for k in row if k.startswith("last_") and k not in present]
    assert not present, (
        f"invariant 2 violated: outcome={row.get('outcome')!r} carries meter fields {present!r}")


# --------------------------------------------------------------------------- the spawn

def build_argv(claude_bin: str, model: str | None) -> list:
    """§2.1/§2.2 — an argv LIST, never a shell string.

    A shell string routed through Git Bash lets MSYS path conversion rewrite the prompt `/usage`
    into `C:/Program Files/Git/usage`, and the model correctly answers that no such file exists. **Exit 0, plausible prose, wrong answer.** A list cannot regress that way, which is why
    it is the form and not merely the preference.

    `--mcp-config` is deliberately absent: the reading needs no Notion and no Slack, and every MCP
    server threaded in would be tool-schema bytes on a request that does no tool work."""
    argv = [claude_bin, "-p", "/usage", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    return argv


def scratch_dir(explicit: str | None = None) -> str:
    """§2.3 — an empty directory outside every repo.

    `claude` auto-loads the `CLAUDE.md` hierarchy for its cwd, so a reading spawned at the repo root
    pays for a root `CLAUDE.md` it will never consult. It also keeps the child out of any
    checkout, which is the hazard class `../docs/delegated-work-isolation-spec.md` exists for."""
    path = explicit or os.path.join(tempfile.gettempdir(), "seneschal-usage-probe")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return tempfile.gettempdir()
    return path


def read_cli_version(runner=subprocess.run, claude_bin: str = "claude",
                     timeout: int = VERSION_TIMEOUT_SEC):
    """`claude --version` → `"2.1.243"`, or None. A session-less, token-free spawn (verified: it
    prints and exits). A failure costs the field, never the reading — `null` here means *asked and
    could not tell*, which is a different fact from a row written before the field existed."""
    try:
        proc = runner([claude_bin, "--version"], capture_output=True, text=True, encoding="utf-8",
                      errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 — a version probe may never strand a reading
        return None
    out = (getattr(proc, "stdout", "") or "").strip()
    m = re.search(r"\d+\.\d+\.\d+", out)
    return m.group(0) if m else (out.splitlines()[0][:40] if out else None)


def spawn_reading(*, runner=subprocess.run, claude_bin: str = "claude",
                  model: str | None = DEFAULT_MODEL, timeout: int = DEFAULT_TIMEOUT_SEC,
                  cwd: str | None = None) -> dict:
    """Run one `/usage` and return `{status, stdout, stderr, returncode, elapsed_ms}`.

    `status` is one of `completed` / `timeout` / `spawn_failed` — the transport outcome only. What
    the text MEANS is `take_reading`'s job.

    `stdin=DEVNULL` is not optional: without it the CLI waits 3 s for piped input and warns on
    stderr (measured). `encoding="utf-8"` is forced because the report is full of the
    middot and em dash that Windows would otherwise decode as cp1252 and mangle — the same fix
    `presence.py`'s warm-session `Popen` already carries."""
    argv = build_argv(claude_bin, model)
    started = time.monotonic()
    try:
        proc = runner(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                      timeout=timeout, env=child_env(), cwd=cwd or scratch_dir(),
                      stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        return {"status": TIMEOUT, "stdout": _decode(getattr(e, "output", None)),
                "stderr": _decode(getattr(e, "stderr", None)), "returncode": None,
                "elapsed_ms": int((time.monotonic() - started) * 1000), "argv": argv}
    except Exception as e:  # noqa: BLE001 — OSError, ValueError, anything the runner raises
        return {"status": SPAWN_FAILED, "stdout": "", "stderr": f"{type(e).__name__}: {e}",
                "returncode": None, "elapsed_ms": int((time.monotonic() - started) * 1000),
                "argv": argv}
    return {"status": "completed",
            "stdout": getattr(proc, "stdout", "") or "",
            "stderr": getattr(proc, "stderr", "") or "",
            "returncode": getattr(proc, "returncode", None),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "argv": argv}


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


# --------------------------------------------------------------------------- the row

def _probe_block(spawn: dict, envelope: dict | None) -> dict:
    """§2.4 — what the reading itself cost, from Anthropic's own accounting.

    An instrument that measures spend and cannot state its own overhead is not trustworthy. Measured:
    `/usage` is rendered client-side, so this block reads all zeros for tokens and cost
    while `elapsed_ms` is real. **That is the honest answer, not a broken one** — the reading's true
    cost is one SESSION and its requests, which is the number §8.1's cadence argument turns on and
    which no field here can report because the panel does not attribute per-session."""
    block = {"elapsed_ms": spawn.get("elapsed_ms")}
    if spawn.get("returncode") is not None:
        block["exit_code"] = spawn["returncode"]
    if not isinstance(envelope, dict):
        return block
    for key, out in (("total_cost_usd", "cost_usd"), ("duration_ms", "duration_ms"),
                     ("duration_api_ms", "duration_api_ms"), ("num_turns", "num_turns"),
                     ("is_error", "is_error"), ("session_id", "session_id"),
                     ("subtype", "subtype")):
        if key in envelope:
            block[out] = envelope[key]
    usage = envelope.get("usage")
    if isinstance(usage, dict):
        block["usage"] = usage
    model_usage = envelope.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        block["model_usage"] = model_usage
    return block


def _truncate(text: str, cap: int = RAW_CAP_BYTES) -> str:
    text = text or ""
    return text if len(text) <= cap else text[:cap] + "…[truncated]"


def build_row(spawn: dict, *, at: str, model: str | None, cli_version, account, interval=None,
              activity=None, now_local: datetime | None = None) -> dict:
    """Turn one spawn result into exactly one row. Pure — fully exercised in the tests with a fake
    spawn dict and no subprocess anywhere.

    `account` is a REQUIRED keyword and this function does no I/O to obtain it, deliberately: a
    default that went and read `~/.claude.json` would make every existing test of this pure builder
    a reader of the host's real credentials. `account_for_row` is where the reading happens."""
    row: dict = {
        "schema": SCHEMA,
        "at": at,
        "outcome": None,
        "parser_version": PARSER_VERSION,
        "cli_version": cli_version,
        "model": model,
        # §4.2.1 — on EVERY outcome, including the ones that carry no meter at all. A failed reading
        # still belongs to an account, and a row that does not say which one cannot be placed in a
        # series that spans a switch.
        "account": account,
    }
    envelope = None
    result_text = ""
    stdout = spawn.get("stdout") or ""
    stderr = spawn.get("stderr") or ""

    if stdout.strip():
        try:
            candidate = json.loads(stdout)
            if isinstance(candidate, dict):
                envelope = candidate
                result_text = candidate.get("result") or ""
                if not isinstance(result_text, str):
                    result_text = ""
        except (ValueError, TypeError):
            envelope = None

    if spawn.get("status") == TIMEOUT:
        row["outcome"] = TIMEOUT
        row["detail"] = f"no result within {spawn.get('elapsed_ms')} ms"
        row["probe"] = _probe_block(spawn, envelope)
        _finish(row, spawn, stderr, raw=stdout)
        return row

    if spawn.get("status") == SPAWN_FAILED:
        sig = looks_like_auth_failure(stderr, stdout)
        row["outcome"] = AUTH_FAILED if sig else SPAWN_FAILED
        if sig:
            row["auth_signature"] = sig
        row["detail"] = _truncate(stderr, STDERR_TAIL_CAP) or "the child never started"
        row["probe"] = _probe_block(spawn, envelope)
        _finish(row, spawn, stderr, raw=stdout)
        return row

    parsed = parse_report(result_text, now_local) if result_text else parse_report("", now_local)
    outcome, missing = classify(parsed)
    rc = spawn.get("returncode")

    if rc not in (0, None):
        sig = looks_like_auth_failure(stderr, stdout, result_text)
        row["outcome"] = AUTH_FAILED if sig else SPAWN_FAILED
        if sig:
            row["auth_signature"] = sig
        row["detail"] = f"exit {rc}: {_truncate(stderr, STDERR_TAIL_CAP)}".strip()
        row["probe"] = _probe_block(spawn, envelope)
        _finish(row, spawn, stderr, raw=stdout)
        return row

    if outcome == UNPARSED:
        # Exit 0 and zero meters. An unparseable ENVELOPE lands here too, which is right — from a
        # consumer's point of view "the report did not arrive" and "the report arrived and said
        # nothing recognisable" are the same absence of numbers, and `detail` distinguishes them.
        sig = looks_like_auth_failure(stderr, stdout, result_text)
        row["outcome"] = AUTH_FAILED if sig else UNPARSED
        if sig:
            row["auth_signature"] = sig
        row["detail"] = ("the envelope was not JSON" if envelope is None and stdout.strip()
                         else "no output at all" if not stdout.strip()
                         else "zero meters matched")
        row["shape_fingerprint"] = parsed["shape_fingerprint"]
        row["line_kinds"] = parsed["line_kinds"]
        if parsed["unmatched"]:
            row["unmatched"] = parsed["unmatched"]
        row["probe"] = _probe_block(spawn, envelope)
        _finish(row, spawn, stderr, raw=result_text or stdout)
        return row

    # ok / partial — the only two outcomes allowed to carry meter numbers.
    row["outcome"] = outcome
    row["meters"] = parsed["meters"]
    row["unrecognised_meters"] = parsed["unrecognised_meters"]
    row["shape_fingerprint"] = parsed["shape_fingerprint"]
    row["line_kinds"] = parsed["line_kinds"]
    window = (parsed["meters"].get("week_all_models") or {}).get("resets_at")
    if window:
        # Q3 — stored, not derived at read time. It is the field that stops the mistake of
        # differencing two readings from DIFFERENT weekly windows and calling it a burn rate.
        row["week_window_id"] = window
    for key, block in parsed["blocks"].items():
        row[key] = block
    if outcome == PARTIAL:
        row["missing_meters"] = missing
        if parsed["unmatched"]:
            row["unmatched"] = parsed["unmatched"]
        row["raw"] = _truncate(result_text)
    row["probe"] = _probe_block(spawn, envelope)
    _finish(row, spawn, stderr, raw=None)
    _attach_context(row, interval, activity)
    return row


def _finish(row: dict, spawn: dict, stderr: str, raw) -> None:
    if raw is not None:
        row["raw"] = _truncate(raw)
    tail = (stderr or "").strip()
    if tail:
        row["stderr_tail"] = _truncate(tail, STDERR_TAIL_CAP)
    assert_no_meter_fields(row)


def _attach_context(row: dict, interval, activity) -> None:
    if interval is not None:
        row["interval"] = interval
    if activity is not None:
        row["activity"] = activity


def build_skipped_row(*, at: str, gate: str, account, interval=None, activity=None,
                      model: str | None = None, cli_version=None) -> dict:
    """§2.6 — a reading deferred past its window is SKIPPED and recorded as skipped, never queued to
    fire late. A meter reading is only meaningful at the moment it was taken, so a late one would be
    a carry-forward wearing a timestamp.

    `account` is required here for the same reason it is on `build_row`, and carried for the same
    one: a skip is an attempt, and an attempt belongs to an account."""
    row = {
        "schema": SCHEMA,
        "at": at,
        "outcome": SKIPPED,
        "detail": f"deferred past its window by the {gate} gate",
        "gate": gate,
        "parser_version": PARSER_VERSION,
        "cli_version": cli_version,
        "model": model,
        "account": account,
    }
    _attach_context(row, interval, activity)
    assert_no_meter_fields(row)
    return row


def append_row(state_dir: str, row: dict) -> str:
    """One row, one line, append-only. Raises nothing the caller has to handle beyond OSError —
    invariant 1 means a failure to write is the ONLY way an attempt goes unrecorded, so it is left
    loud rather than swallowed here; the daemon wrapper logs it and carries on."""
    assert_no_meter_fields(row)
    path = readings_path(state_dir)
    stateio.append_jsonl(path, row)  # the tree's single-line-append primitive (creates the dir)
    return path


def last_row(state_dir: str, tail_bytes: int = 65536):
    """The most recent row, or None. Bounded tail read — the file is append-only and grows forever
    (§4.4 keeps everything), so this must never become a full-file read."""
    path = readings_path(state_dir)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            data = fh.read()
    except OSError:
        return None
    text = data.decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # a torn last line (a crash mid-append) is skipped, never repaired
        if isinstance(obj, dict):
            return obj
    return None


# --------------------------------------------------------------------------- the whole reading

def take_reading(state_dir: str, *, runner=subprocess.run, claude_bin: str = "claude",
                 model: str | None = DEFAULT_MODEL, timeout: int = DEFAULT_TIMEOUT_SEC,
                 interval_min: int = 60, cwd: str | None = None, collect=None,
                 cli_version=..., write: bool = True, now_local: datetime | None = None,
                 account_reader=None) -> dict:
    """Spawn → read the identity → parse → snapshot the interval → append exactly one row.

    `collect` is the activity collector (`usage_activity.collect`), injectable so the whole path
    runs in a test with no spawn, no network and no spend. `cli_version` defaults to the sentinel
    `...`, meaning *go and ask*; pass an explicit value (including None) to skip that spawn.
    `account_reader` is `read_account`'s seam, injected exactly as `runner` is — a test passes its
    own so that nothing here ever opens the host's real `~/.claude.json`."""
    now_local = now_local or local_now()
    previous = last_row(state_dir)
    interval, activity = _window(state_dir, previous, now_local, interval_min, collect)
    # Read BEFORE the spawn: the question is which account this reading was taken under, and a
    # ~14 s spawn is long enough for a switch to land in the middle of it.
    account = account_for_row(state_dir, previous, account_reader)
    if cli_version is ...:
        cli_version = read_cli_version(runner=runner, claude_bin=claude_bin)
    spawn = spawn_reading(runner=runner, claude_bin=claude_bin, model=model, timeout=timeout, cwd=cwd)
    row = build_row(spawn, at=now_local.isoformat(timespec="seconds"), model=model,
                    cli_version=cli_version, account=account, interval=interval, activity=activity,
                    now_local=now_local)
    # A non-`ok` row gets the interval and the activity too: WHEN a reading failed and WHAT was
    # running while it failed is the whole reason an auth outage has to be legible as an
    # outage. Only the METER numbers are withheld (invariant 2).
    _attach_context(row, interval, activity)
    assert_no_meter_fields(row)
    if write:
        append_row(state_dir, row)
    return row


def _window(state_dir, previous, now_local, interval_min, collect):
    """(interval, activity) — the explicit window this row's activity fields cover.

    **The window is previous-reading → this one, and it is stated on the row.** Without it a gap
    (daemon down, auth outage, machine asleep) is silently attributed to whatever period the reader
    assumes, which is the failure the whole activity snapshot exists to prevent.

    A first-ever reading has NO previous row and therefore no window, so it carries no activity at
    all rather than a window invented from the nominal cadence. That is invariant 3 (never carry
    forward) applied at the other end: an assumed window is a fabricated measurement."""
    nominal = max(1, int(interval_min)) * 60
    since = parse_iso((previous or {}).get("at"))
    if since is None:
        return {"first_reading": True, "since": None,
                "until": now_local.isoformat(timespec="seconds"),
                "nominal_seconds": nominal,
                "note": "no previous reading — there is no interval to describe, so no activity "
                        "fields are recorded (never an assumed window)"}, None
    seconds = (now_local - since).total_seconds()
    interval = {
        "first_reading": False,
        "since": since.isoformat(timespec="seconds"),
        "until": now_local.isoformat(timespec="seconds"),
        "seconds": round(seconds, 1),
        "nominal_seconds": nominal,
        "since_outcome": (previous or {}).get("outcome"),
        # A window materially longer than the cadence is a GAP — the daemon was down, the machine
        # was asleep, or readings were skipped. Flagged, never smoothed over and never explained
        # away: nothing here knows why, and guessing would be the carry-forward again.
        "gap": seconds > 1.75 * nominal,
    }
    if collect is None:
        return interval, None
    try:
        activity = collect(state_dir, since, now_local)
    except Exception as e:  # noqa: BLE001 — a broken snapshot may never cost the reading (inv. 1)
        activity = {"error": f"{type(e).__name__}: {e}"}
    return interval, activity


# --------------------------------------------------------------------------- CLI

def _cmd_read(args) -> int:
    from usage_activity import collect as collect_activity  # local: keeps import cost off `parse`
    row = take_reading(args.state_dir, claude_bin=args.claude_bin, model=args.model,
                       timeout=args.timeout, interval_min=args.interval_min,
                       cwd=args.scratch_dir, collect=collect_activity, write=not args.no_write)
    json.dump(row, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if row.get("outcome") in NUMERIC_OUTCOMES else 1


def _cmd_parse(args) -> int:
    """Offline diagnosis of a captured report or envelope — the door you use when a `raw` blob shows
    up on a `partial` row and you need to know which rule stopped matching. No spawn."""
    text = open(args.file, encoding="utf-8").read() if args.file else sys.stdin.read()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and isinstance(obj.get("result"), str):
            text = obj["result"]
    except ValueError:
        pass
    parsed = parse_report(text)
    outcome, missing = classify(parsed)
    parsed["outcome"] = outcome
    parsed["missing_meters"] = missing
    json.dump(parsed, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("read", help="take one reading and append one row")
    # REQUIRED, with no default. A flag that defaults to the live state directory turns an existing
    # test into a live writer — that has bitten this repo before and is why there is no default here.
    r.add_argument("--state-dir", required=True)
    r.add_argument("--claude-bin", default="claude")
    r.add_argument("--model", default=DEFAULT_MODEL,
                   help="pinned cheap tier; /usage renders client-side so this costs nothing either "
                        "way (measured) — the pin is so it cannot regress")
    r.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC)
    r.add_argument("--interval-min", type=int, default=60,
                   help="the NOMINAL cadence, recorded on the row so a gap is detectable")
    r.add_argument("--scratch-dir", default=None)
    r.add_argument("--no-write", action="store_true", help="print the row, append nothing")
    r.set_defaults(func=_cmd_read)

    p = sub.add_parser("parse", help="parse a captured report/envelope offline (no spawn)")
    p.add_argument("--file", default=None, help="path to a report or an --output-format json envelope")
    p.set_defaults(func=_cmd_parse)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
