#!/usr/bin/env python3
"""governor.py — **Oikonomos**, the budget-governor advisor (order ~15 in the Advisor Chain;
cockpit-spec.md "Oikonomos — the budget governor", v3.5). Stdlib only.

Oikonomos spans two worlds, and this module is honest about which knob lives where (see
``references/advisor-chain.md``'s rails-vs-advisory table):

  * **Code-backed rails** (enforced HERE, testable): the Fable-delegation quotas (daily count,
    per-conversation count, concurrency) and per-model token metering + threshold alerting.
  * **Advisory policy** (prompt/doc-side guidance the reasoning loop honors, NOT enforced in code):
    per-turn max output tokens, reasoning-effort tiers, turn checkpoints, total-turn caps, context-fill
    wind-down, proactive-push rate caps. These knobs are still schema-driven and cockpit-editable —
    they just aren't *mechanically* gated here. Do not pretend otherwise.

**Config** — ``state/governor-config.json`` (gitignored; tracked ``governor-config.example.json``).
Schema-driven: ``SCHEMA`` is a plain dict (knob -> {type, label, unit, kind, default, ...}) so the
cockpit's Thresholds panel renders its form from it and a future knob never needs UI rework.
``load()`` is tolerant (missing file / bad value for a knob -> that knob's default, never raises);
``save()`` validates every touched key against ``SCHEMA`` and raises ``ValueError`` on the first batch
of problems (like ``model_config.save``, writes are the strict side, reads stay tolerant). Keys present
in the file but not in ``SCHEMA`` (a newer version's knob, read by an older one) are preserved verbatim
on save — forward compat, never dropped.

**Spend ledger** — ``state/governor-ledger.jsonl``: one JSON line per governed spend event,
``{ts, kind, model?, tokens?, billable_tokens?, components?, basis?, metered?, conversation_id?,
turn_id?, levers?}``. Two kinds are written today: ``"tokens"`` (a turn's usage, appended by
``presence.py``'s stream tee) and ``"fable_oneshot"`` (a successful Fable delegation, appended by
``fable_delegate.py``).

**Two bases, and the rails use the second one.** ``tokens`` is the raw sum of every token field the CLI
reported — it keeps exactly the meaning it has always had, so rows written before the billable basis
existed are never silently reinterpreted. ``billable_tokens`` is that same usage run through
``TOKEN_WEIGHTS`` below, and it is what every rail, rollup and alert actually reads (falling back to
``tokens`` for a legacy row that has no breakdown). The flat sum is the wrong basis for a rail: a cache
**read** is not the same cost as a fresh input token (Anthropic meters it at 0.1x), and because the warm
session re-reads its whole conversation every turn, the flat sum grows with conversation LENGTH rather
than with spend — the longer a session runs (i.e. the *cheaper* each turn gets), the louder a flat-sum
rail cries wolf. On a cache-heavy day the raw sum can overstate the billable figure several-fold. **Do
not restore the flat sum as the rails' basis.** (It is still recorded, as ``tokens``; it is just not
what anything decides on.)

``rollups()`` computes day/week totals from the ledger, gated on the **owner's local day boundaries** — the house rule (rule 5: after-midnight activity counts as the prior
day; date logic runs in the owner's timezone, never UTC). The day-boundary math is delegated to the
sibling ``tz_common`` (configured owner zone → machine-local fallback), imported guarded exactly the
way ``presence.py`` guards it — without the module the day keys degrade to the machine-local clock and
nothing here ever fails to import on a bare interpreter.

**The rail gate** — ``check(kind, state_dir, **ctx) -> Verdict``. Today's one real caller is
``fable_delegate.py``, kind ``"fable_oneshot"``: it enforces, in order, the daily quota, the
per-conversation quota, delegation concurrency, and (the one place a token budget is actually
*blockable* in this architecture — the warm session's own turn loop can't be interrupted mid-stream)
the Fable model's own daily/weekly token budget. A refusal's ``reason`` is a complete, human-readable
sentence naming the quota and when it resets, so the warm session can relay it honestly instead of
inventing an excuse.

Everything here is fail-open on read: a missing/corrupt config or ledger degrades to defaults/empty,
never raises, never blocks a legitimate call over an I/O hiccup. Writes (``save``, the ledger append,
the alert-state write) use the same atomic tmp-then-``os.replace`` pattern as ``model_config.py``.

``cockpit/server/governor.py`` duplicates the SCHEMA and the basis-aware rollups (cockpit-spec.md
ruling 3); ``cockpit/server/test_parity.py`` fails CI when the two drift. ``TOKEN_WEIGHTS`` is THE ONE
PLACE the billable basis is defined; the invariant guarding it is that ``tokens`` stays the RAW sum
forever while everything that DECIDES reads ``billable_tokens`` with a per-row raw fallback, and every
consumer names which basis it used.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

# Owner-timezone plumbing (tz_common: configured identity zone → machine-local fallback). Guarded
# like presence.py's import of the same module: a missing/broken tz_common degrades every
# day-boundary computation below to the machine-local clock — it never keeps this module (or the
# daemon importing it) from loading.
try:
    import tz_common as _tz_common
except ImportError:  # pragma: no cover — behave like an unconfigured install (machine-local dates)
    _tz_common = None

CONFIG_FILE = "governor-config.json"
LEDGER_FILE = "governor-ledger.jsonl"
ALERT_STATE_FILE = "governor-alert-state.json"
INFLIGHT_FILE = "governor-inflight.json"

DEFAULT_ALERT_PCT = 80
ALERT_REALERT_HOURS = 6  # per-knob dedupe window: re-alert on a still-hot threshold at most every 6h


# ------------------------------------------------------------------------------- the billable basis
# ONE named weight table. Do not scatter these ratios across call sites — a rail whose unit is defined
# in three places is a rail nobody can audit.
#
# The weights are Anthropic's PUBLISHED prompt-caching price ratios, each expressed relative to one
# base (uncached) input token — https://docs.claude.com/en/docs/build-with-claude/prompt-caching#pricing
#   * cache READ ............ 0.1x base input
#   * cache WRITE, 5m TTL ... 1.25x base input
#   * cache WRITE, 1h TTL ... 2x base input
#   * base input ............ 1x, by definition
#
# Output is deliberately weighted AT PAR (1.0). This basis is an **input-token-equivalent**, not a
# dollar-equivalent: these knobs are *token* budgets, and a token budget that silently folded in the
# output/input price ratio would be a third unit nobody asked for. The dollar figure is already
# tracked, separately and exactly, by the CLI itself — `metrics.jsonl`'s `cost_usd`/`session_cost_usd`.
BILLABLE_BASIS = "input_token_equivalent_v1"

TOKEN_WEIGHTS: dict = {
    "input": 1.0,
    "output": 1.0,
    "cache_read": 0.1,
    "cache_write_5m": 1.25,
    "cache_write_1h": 2.0,
    # A `cache_creation_input_tokens` total with no ephemeral_5m/_1h breakdown to explain it. Anthropic's
    # default cache TTL is 5 minutes, so that is what an unqualified cache write is priced at.
    "cache_write_unspecified": 1.25,
}

# The raw token fields presence.py has always summed into `tokens`. Kept as a named list so the raw
# basis and the billable basis are visibly reading the SAME usage object, not two different subsets.
RAW_TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
)


def _int_field(d, key) -> int:
    """One non-negative int out of a usage dict, tolerantly. Anything else reads as 0 — a usage block
    is provider-shaped data this module doesn't control, so it is parsed defensively throughout."""
    if not isinstance(d, dict):
        return 0
    val = d.get(key)
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return max(0, int(val))
    return 0


def raw_tokens(usage) -> int | None:
    """The flat sum of every token field, i.e. `tokens`'s long-standing meaning. Returns None when the
    usage block carries no recognizable token field at all (so a caller can tell "nothing reported"
    apart from "reported zero")."""
    if not isinstance(usage, dict):
        return None
    total = 0
    found = False
    for key in RAW_TOKEN_FIELDS:
        val = usage.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            total += int(val)
            found = True
    return total if found else None


def usage_components(usage) -> dict | None:
    """Normalize a claude-CLI usage block into the flat component dict `TOKEN_WEIGHTS` is keyed by.

    The CLI reports cache writes twice over: a `cache_creation_input_tokens` total AND, when it knows
    the TTLs, a `cache_creation: {ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}` breakdown. The
    breakdown is only trusted when its parts actually add up to the total — otherwise the remainder is
    booked as `cache_write_unspecified` so no cache write is ever silently dropped or double-counted.

    Returns None when the block carries no token fields at all."""
    if raw_tokens(usage) is None:
        return None
    created = _int_field(usage, "cache_creation_input_tokens")
    breakdown = usage.get("cache_creation") if isinstance(usage, dict) else None
    write_5m = _int_field(breakdown, "ephemeral_5m_input_tokens")
    write_1h = _int_field(breakdown, "ephemeral_1h_input_tokens")
    if write_5m + write_1h > created:
        # The breakdown claims more than the total — don't trust either half of it over the total.
        write_5m = write_1h = 0
    return {
        "input": _int_field(usage, "input_tokens"),
        "output": _int_field(usage, "output_tokens"),
        "cache_read": _int_field(usage, "cache_read_input_tokens"),
        "cache_write_5m": write_5m,
        "cache_write_1h": write_1h,
        "cache_write_unspecified": created - write_5m - write_1h,
    }


def billable_from_components(components) -> int | None:
    """Apply TOKEN_WEIGHTS to a `usage_components` dict. Rounded to a whole token — the ledger's unit is
    tokens, and a fractional token in a budget meter is noise pretending to be precision."""
    if not isinstance(components, dict):
        return None
    total = 0.0
    for key, weight in TOKEN_WEIGHTS.items():
        total += _int_field(components, key) * weight
    return int(round(total))


def billable_tokens(usage) -> int | None:
    """usage block -> billable tokens on BILLABLE_BASIS. None when nothing is metered."""
    return billable_from_components(usage_components(usage))


# ------------------------------------------------------------------------------------------- SCHEMA
# Every knob cockpit-spec.md's "Oikonomos" section lists, initial set. `kind` is "rail" (enforced in
# Python, see `check()`/the metering below) or "advisory" (prompt/doc-side guidance only — see the
# module docstring). `alert_at_pct`/`hard_stop` are per-knob UI/behavior attributes, not separate knobs.
SCHEMA: dict = {
    "max_output_tokens_per_turn": {
        "type": "int", "label": "Max output tokens per turn", "unit": "tokens",
        "kind": "advisory", "default": 8000, "min": 256, "max": 64000,
    },
    "reasoning_effort_by_mode": {
        "type": "dict_enum", "label": "Reasoning effort tier (per mode)", "unit": "tier",
        "kind": "advisory", "options": ["low", "medium", "high"],
        "default": {
            "Chat": "medium", "Brief": "medium", "Wrap": "medium", "Triage": "medium",
            "Ask": "medium", "Reminders": "low", "Forge": "medium", "Archive": "low",
            "Watch": "low", "Dream": "high",
        },
    },
    "turn_checkpoint_n": {
        "type": "int", "label": "Turn checkpoints (autonomous turns before pausing to ask)",
        "unit": "turns", "kind": "advisory", "default": 8, "min": 1, "max": 100,
    },
    "max_turns_per_conversation": {
        "type": "int", "label": "Total-turn cap per conversation", "unit": "turns",
        "kind": "advisory", "default": 40, "min": 1, "max": 1000,
        "alert_at_pct": 80, "hard_stop": False,
    },
    # Opus budgets are sized for a warm session, not guessed from a one-shot: every turn re-reads the
    # whole conversation, so a normal day is millions of (overwhelmingly cached) tokens, and a budget
    # sized before cache reads were metered would sit roughly two orders of magnitude low. They are set
    # above a busy day so the alert means "today is anomalous", not "today was busy". A model with NO
    # entry here is metered but never alerted (`due_alerts` skips it) — keep every warm-eligible model
    # (model_config.RANK) listed so a dial change can't leave the warm session unrailed.
    "daily_token_budget_by_model": {
        "type": "dict_int", "label": "Daily token budget (per model)", "unit": "tokens/day",
        "kind": "rail", "min": 0, "max": 50_000_000, "alert_at_pct": 80, "hard_stop": False,
        "default": {
            "claude-haiku-4-5": 2_000_000, "claude-sonnet-5": 1_000_000,
            "claude-opus-4-8": 30_000_000, "claude-opus-5": 30_000_000,
            "claude-opus-5-5": 30_000_000,
            "claude-fable-5": 150_000, "claude-fable-5-1": 150_000,
        },
    },
    "weekly_token_budget_by_model": {
        "type": "dict_int", "label": "Weekly token budget (per model)", "unit": "tokens/week",
        "kind": "rail", "min": 0, "max": 200_000_000, "alert_at_pct": 80, "hard_stop": False,
        "default": {
            "claude-haiku-4-5": 10_000_000, "claude-sonnet-5": 5_000_000,
            "claude-opus-4-8": 150_000_000, "claude-opus-5": 150_000_000,
            "claude-opus-5-5": 150_000_000,
            "claude-fable-5": 750_000, "claude-fable-5-1": 750_000,
        },
    },
    "fable_oneshots_per_day": {
        "type": "int", "label": "Fable one-shots per day", "unit": "one-shots/day",
        "kind": "rail", "default": 5, "min": 0, "max": 200,
        "alert_at_pct": 80, "hard_stop": True,
    },
    "fable_oneshots_per_conversation": {
        "type": "int", "label": "Max Fable one-shots per conversation", "unit": "one-shots",
        "kind": "rail", "default": 2, "min": 0, "max": 50,
        "alert_at_pct": 100, "hard_stop": True,
    },
    "fable_concurrency_max": {
        "type": "int", "label": "Delegation concurrency", "unit": "concurrent one-shots",
        "kind": "rail", "default": 1, "min": 1, "max": 10,
        "alert_at_pct": 100, "hard_stop": True,
    },
    "context_fill_winddown_pct": {
        "type": "int", "label": "Context-fill wind-down threshold", "unit": "% full",
        "kind": "advisory", "default": 80, "min": 10, "max": 100,
    },
    "proactive_push_rate_per_hour": {
        "type": "int", "label": "Proactive-push rate cap", "unit": "nudges/hour",
        "kind": "advisory", "default": 3, "min": 0, "max": 60,
        "alert_at_pct": 100, "hard_stop": False,
    },
}


# --------------------------------------------------------------------------------- config load/save

def config_path(state_dir) -> str:
    return os.path.join(state_dir, CONFIG_FILE)


def _raw_load(state_dir) -> dict:
    try:
        with open(config_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        data = None
    return data if isinstance(data, dict) else {}


def _validate(key: str, spec: dict, value) -> tuple[bool, str | None]:
    """Validate one knob's value against its SCHEMA entry. Returns (ok, error_message)."""
    t = spec.get("type")
    if t == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            return False, f"{key}: expected an integer, got {value!r}"
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and value < lo:
            return False, f"{key}: {value} is below the minimum ({lo})"
        if hi is not None and value > hi:
            return False, f"{key}: {value} is above the maximum ({hi})"
        return True, None
    if t == "dict_int":
        if not isinstance(value, dict):
            return False, f"{key}: expected an object mapping model -> integer"
        lo, hi = spec.get("min"), spec.get("max")
        for sub_key, sub_val in value.items():
            if not isinstance(sub_val, int) or isinstance(sub_val, bool):
                return False, f"{key}.{sub_key}: expected an integer, got {sub_val!r}"
            if lo is not None and sub_val < lo:
                return False, f"{key}.{sub_key}: {sub_val} is below the minimum ({lo})"
            if hi is not None and sub_val > hi:
                return False, f"{key}.{sub_key}: {sub_val} is above the maximum ({hi})"
        return True, None
    if t == "dict_enum":
        options = spec.get("options") or []
        if not isinstance(value, dict):
            return False, f"{key}: expected an object mapping mode -> tier"
        for sub_key, sub_val in value.items():
            if sub_val not in options:
                return False, f"{key}.{sub_key}: {sub_val!r} is not one of {options}"
        return True, None
    return False, f"{key}: unknown schema type {t!r}"


def load(state_dir) -> dict:
    """Tolerant load: every SCHEMA knob resolves to its stored value if valid, else its default.
    Never raises. Extra keys in the file that aren't in SCHEMA are simply not surfaced here (they're
    still preserved by `save`, below — this is the read side, forward-compat lives in the write side)."""
    raw = _raw_load(state_dir)
    out: dict = {}
    for key, spec in SCHEMA.items():
        if key in raw:
            ok, _ = _validate(key, spec, raw[key])
            if ok:
                out[key] = raw[key]
                continue
        out[key] = copy.deepcopy(spec["default"])
    return out


def save(state_dir, updates: dict) -> dict:
    """Validate every key in `updates` against SCHEMA, then atomically merge + write. Raises
    ValueError (joined messages) on ANY invalid key/value and writes nothing in that case — same
    strict-writes/tolerant-reads split as model_config.py. Keys already in the file that `updates`
    doesn't touch (including ones unknown to this version of SCHEMA) are preserved verbatim."""
    raw = _raw_load(state_dir)
    errors: list[str] = []
    for key, value in updates.items():
        spec = SCHEMA.get(key)
        if spec is None:
            errors.append(f"unknown knob {key!r}")
            continue
        ok, err = _validate(key, spec, value)
        if not ok:
            errors.append(err)
            continue
        raw[key] = value
    if errors:
        raise ValueError("; ".join(errors))
    raw["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    os.makedirs(state_dir, exist_ok=True)
    path = config_path(state_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2)
    os.replace(tmp, path)
    return load(state_dir)


# ---------------------------------------------------------------------- owner-local day boundary
# Rule 5: date/day-boundary logic runs in the OWNER's timezone. The math itself lives in the sibling
# tz_common (configured identity zone → machine-local fallback); these two thin helpers are the only
# seam, so a missing tz_common degrades to the machine-local clock and nothing else here changes.
# Pure-UTC *interval* math (the alert dedupe window below) deliberately stays owner-tz-free.

def _local_today() -> date:
    """The owner's current local calendar date (machine-local when tz_common is unavailable)."""
    if _tz_common is not None:
        return _tz_common.local_now().date()
    return datetime.now().astimezone().date()


def _to_local_date(dt: datetime) -> date:
    """Convert an aware (or naive-assumed-UTC) instant to the owner's local calendar date — the
    correct after-midnight-is-still-yesterday math the house rule requires."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if _tz_common is not None:
        return _tz_common.to_local(dt).date()
    return dt.astimezone().date()


def _week_key(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ------------------------------------------------------------------------------------- spend ledger

def ledger_path(state_dir) -> str:
    return os.path.join(state_dir, LEDGER_FILE)


METERED_UNAVAILABLE = "unavailable"


def append_spend(state_dir, kind: str, model: str | None = None, tokens: int | None = None,
                 conversation_id: str | None = None, usage=None,
                 metered: str | None = None, turn_id: str | None = None,
                 levers: dict | None = None) -> None:
    """Append one governed-spend line. Fail-open: an I/O error here must never break the caller's
    real work (a chat turn, a delegation) — swallow and move on, same posture as the cockpit
    transcript tee (cockpit_pipe.append_transcript_event).

    Pass `usage` (the claude-CLI's raw usage block) and this derives the whole row: `tokens` stays the
    flat raw sum it has always been, and `components`/`billable_tokens`/`basis` carry the breakdown the
    rails actually read. An explicit `tokens=` still wins, so a caller that only knows a scalar can say
    so — such a row is simply legacy-shaped and rolls up on the raw basis.

    `metered=METERED_UNAVAILABLE` marks a spend event that genuinely happened but whose usage could not
    be read. That row deliberately carries NO token count at all. **Never write a 0 for unknown spend:**
    a 0 is indistinguishable from "measured, and it was free", and the fable_oneshot gate *trusts* the
    number it reads — an unread delegation metered as zero would sail under the tightest budget on the
    board.

    `turn_id` is the **join key** to the `metrics.jsonl` row for the same turn. The two rows are written
    moments apart by the same function, and without a shared key the only way to pair them is
    timestamp proximity — a heuristic that breaks the moment two turns finish in the same second or a
    delegation writes a row between them. One field makes every lever `metrics.jsonl` already carries
    (turns served, server tools, context estimate, cost) joinable to the spend it produced. It is
    omitted, not zeroed, when the caller has no turn to name — a delegation or a job child is not a warm
    turn, and the never-write-a-0 reasoning applies just as much to a fabricated key.

    `levers` is the **cause** block — what the turn actually did, as counted at the stream tee.
    Decomposing by token type turned a wrong number into a right one; this decomposes a right number
    into an actionable one. It is written **only when the caller measured something**: a None or an
    empty dict leaves the key off entirely rather than writing zeros, because a 0 reads as *measured,
    and free*. Purely additive: no existing field changes meaning, and a row written without levers is
    byte-identical to what this function has always produced. **It is recorded, never enforced** —
    nothing in this module reads it, and the rails keep deciding on `billable_tokens` alone."""
    line: dict = {"ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "kind": kind}
    if model is not None:
        line["model"] = model
    if metered == METERED_UNAVAILABLE:
        line["metered"] = METERED_UNAVAILABLE
        usage = None
        tokens = None
    components = usage_components(usage)
    if tokens is None:
        tokens = raw_tokens(usage)
    if tokens is not None:
        line["tokens"] = int(tokens)
    if components is not None:
        line["components"] = components
        line["billable_tokens"] = billable_from_components(components)
        line["basis"] = BILLABLE_BASIS
    if conversation_id is not None:
        line["conversation_id"] = conversation_id
    if turn_id:
        line["turn_id"] = str(turn_id)
    if isinstance(levers, dict) and levers:
        line["levers"] = levers
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(ledger_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _read_ledger(state_dir) -> list[dict]:
    """Tolerant JSONL read: a corrupt/garbage line is skipped, never raises, missing file -> []."""
    try:
        with open(ledger_path(state_dir), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            rec = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


BASIS_BILLABLE = "billable"    # every counted row carried a billable_tokens breakdown
BASIS_RAW = "raw"              # every counted row was legacy (flat sum, cache reads at full weight)
BASIS_MIXED = "mixed"          # both — the normal case while a ledger straddles the upgrade


def _resolve_basis(seen: set) -> str | None:
    """A model's rollup basis from the set of per-row bases that fed it. None means "no basis" — no
    counted rows at all — and callers that must name their unit (due_alerts) decline to fire on it."""
    if not seen:
        return None
    if seen == {BASIS_BILLABLE}:
        return BASIS_BILLABLE
    if seen == {BASIS_RAW}:
        return BASIS_RAW
    return BASIS_MIXED


def rollups(state_dir, now: datetime | None = None) -> dict:
    """Day/week spend totals from the ledger, gated on the owner's local calendar-day boundaries
    (the house rule — after-midnight activity counts as the PRIOR day because it converts each
    record's actual UTC timestamp to the owner's wall-clock date via tz_common, not by re-bucketing
    at a fixed UTC offset).

    Token totals, deliberately side by side:
      * ``tokens_by_model`` — the RAW flat sum, unchanged in meaning since the first row was written.
        Kept so history stays readable and nothing silently reinterprets existing rows.
      * ``billable_by_model`` — the basis every rail/alert decides on: each row's ``billable_tokens``
        when it has one, **falling back to its raw ``tokens``** when it doesn't (a legacy row). The
        fallback is why a pre-upgrade day still rolls up at all instead of reading as free.
      * ``basis_by_model`` — which of the two fed each model's billable figure ("billable" / "raw" /
        "mixed"), so a consumer names its unit instead of quietly averaging two of them.
      * ``unmetered_by_model`` — count of spend events that happened but could not be measured
        (``metered: "unavailable"``). They add nothing to either total; surfacing the count is how a
        meter stays honest about a gap rather than showing it as zero spend.

    Token totals accrue from ANY row carrying token data, not only ``kind == "tokens"`` — a
    ``fable_oneshot`` row that reports its own usage must count that cost, not just the delegation.

    Returns:
        {"day": "YYYY-MM-DD", "week": "YYYY-Www",
         "fable_oneshots": {"day": N, "week": N},
         "tokens_by_model":    {"day": {model: n}, "week": {model: n}},
         "billable_by_model":  {"day": {model: n}, "week": {model: n}},
         "basis_by_model":     {"day": {model: str}, "week": {model: str}},
         "unmetered_by_model": {"day": {model: n}, "week": {model: n}}}
    """
    today_local = _to_local_date(now) if now is not None else _local_today()
    day_key = today_local.isoformat()
    week_key = _week_key(today_local)

    fable_day = fable_week = 0
    tokens_day: dict[str, int] = {}
    tokens_week: dict[str, int] = {}
    billable_day: dict[str, int] = {}
    billable_week: dict[str, int] = {}
    seen_day: dict[str, set] = {}
    seen_week: dict[str, set] = {}
    unmetered_day: dict[str, int] = {}
    unmetered_week: dict[str, int] = {}

    for rec in _read_ledger(state_dir):
        ts = _parse_iso(rec.get("ts"))
        if ts is None:
            continue
        rec_date = _to_local_date(ts)
        same_day = rec_date.isoformat() == day_key
        same_week = _week_key(rec_date) == week_key
        if not same_day and not same_week:
            continue
        if rec.get("kind") == "fable_oneshot":
            if same_day:
                fable_day += 1
            if same_week:
                fable_week += 1

        model = rec.get("model") if isinstance(rec.get("model"), str) else "unknown"
        if rec.get("metered") == METERED_UNAVAILABLE:
            if same_day:
                unmetered_day[model] = unmetered_day.get(model, 0) + 1
            if same_week:
                unmetered_week[model] = unmetered_week.get(model, 0) + 1
            continue

        raw = rec.get("tokens")
        raw = int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
        bill = rec.get("billable_tokens")
        bill = int(bill) if isinstance(bill, (int, float)) and not isinstance(bill, bool) else None
        if raw is None and bill is None:
            continue  # a non-spend row (e.g. a fable_oneshot marker that carries no usage)
        basis = BASIS_BILLABLE if bill is not None else BASIS_RAW
        if bill is None:
            bill = raw
        if raw is None:
            raw = 0

        for same, tok_acc, bill_acc, seen_acc in (
            (same_day, tokens_day, billable_day, seen_day),
            (same_week, tokens_week, billable_week, seen_week),
        ):
            if not same:
                continue
            tok_acc[model] = tok_acc.get(model, 0) + raw
            bill_acc[model] = bill_acc.get(model, 0) + bill
            seen_acc.setdefault(model, set()).add(basis)

    return {
        "day": day_key, "week": week_key,
        "fable_oneshots": {"day": fable_day, "week": fable_week},
        "tokens_by_model": {"day": tokens_day, "week": tokens_week},
        "billable_by_model": {"day": billable_day, "week": billable_week},
        "basis_by_model": {
            "day": {m: _resolve_basis(s) for m, s in seen_day.items()},
            "week": {m: _resolve_basis(s) for m, s in seen_week.items()},
        },
        "unmetered_by_model": {"day": unmetered_day, "week": unmetered_week},
    }


def describe_basis(basis: str | None) -> str:
    """One clause naming a rollup's unit, for an alert or refusal to append. A mixed-basis figure must
    SAY it is mixed rather than present two units as one number."""
    if basis == BASIS_BILLABLE:
        return "billable basis — cache reads discounted per Anthropic's published cache ratios"
    if basis == BASIS_RAW:
        return ("legacy raw basis — cache reads counted at full weight, which overstates a "
                "long warm session substantially")
    if basis == BASIS_MIXED:
        return ("MIXED basis — part billable (cache reads discounted), part legacy raw (cache reads "
                "at full weight); this total is not a single unit")
    return "unknown basis"


def _conversation_fable_count(state_dir, conversation_id: str | None) -> int:
    """All-time count of successful Fable one-shots for one conversation (not day/week-bounded — the
    per-conversation cap is cumulative across the conversation's whole lifetime, however long)."""
    if not conversation_id:
        return 0
    return sum(
        1 for rec in _read_ledger(state_dir)
        if rec.get("kind") == "fable_oneshot" and rec.get("conversation_id") == conversation_id
    )


# ------------------------------------------------------------------------- fable concurrency counter

_inflight_lock = threading.Lock()


def _inflight_path(state_dir) -> str:
    return os.path.join(state_dir, INFLIGHT_FILE)


def _read_inflight(state_dir) -> int:
    try:
        with open(_inflight_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return 0
    n = data.get("count") if isinstance(data, dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else 0


def _write_inflight(state_dir, n: int) -> None:
    try:
        os.makedirs(state_dir, exist_ok=True)
        path = _inflight_path(state_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"count": max(0, n)}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def begin_fable_call(state_dir) -> None:
    """Best-effort increment of the in-flight-Fable-call counter (the concurrency rail's bookkeeping).
    ALWAYS pair with `end_fable_call` in a `finally` block. Fail-open: a write failure here just means
    this call isn't tracked for concurrency purposes — it never blocks the call itself."""
    with _inflight_lock:
        _write_inflight(state_dir, _read_inflight(state_dir) + 1)


def end_fable_call(state_dir) -> None:
    with _inflight_lock:
        _write_inflight(state_dir, max(0, _read_inflight(state_dir) - 1))


# ------------------------------------------------------------------------------------- the rail gate

@dataclass
class Verdict:
    allowed: bool
    reason: str | None = None
    remaining: dict = field(default_factory=dict)


def check(kind: str, state_dir, **ctx) -> Verdict:
    """The rail gate. `kind` in use today: "fable_oneshot" (the delegation quota stack fable_delegate.py
    consults before spawning). An unrecognized `kind` fails OPEN (allowed) — this function gates known
    rails, it doesn't invent new ones to refuse against."""
    if kind == "fable_oneshot":
        return _check_fable_oneshot(state_dir, conversation_id=ctx.get("conversation_id"))
    return Verdict(True, None, {})


def _check_fable_oneshot(state_dir, conversation_id: str | None) -> Verdict:
    cfg = load(state_dir)
    roll = rollups(state_dir)

    day_limit = cfg["fable_oneshots_per_day"]
    day_used = roll["fable_oneshots"]["day"]
    if day_used >= day_limit:
        return Verdict(False, (
            f"the daily Fable one-shot quota is used up ({day_used}/{day_limit}); it resets at local "
            "midnight (the owner's day boundary). Raise fable_oneshots_per_day in the cockpit's "
            "Thresholds panel if this is too tight."
        ), {"day": 0})

    conv_limit = cfg["fable_oneshots_per_conversation"]
    conv_used = _conversation_fable_count(state_dir, conversation_id)
    if conversation_id and conv_used >= conv_limit:
        return Verdict(False, (
            f"this conversation has reached its Fable one-shot cap ({conv_used}/{conv_limit}); it doesn't "
            "reset until a new conversation starts. Raise fable_oneshots_per_conversation in the cockpit's "
            "Thresholds panel if this is too tight."
        ), {"conversation": 0})

    concurrency_limit = cfg["fable_concurrency_max"]
    inflight = _read_inflight(state_dir)
    if inflight >= concurrency_limit:
        return Verdict(False, (
            f"Fable delegation concurrency limit reached ({inflight}/{concurrency_limit} in flight); wait "
            "for the current one-shot to finish before starting another."
        ), {"concurrency": 0})

    # The token budget reads the BILLABLE basis (falling back per-row to raw for legacy rows) — see
    # `rollups`. This is the one token budget that actually blocks, so the unit it compares against had
    # better be the unit Anthropic bills in.
    fable_model = "claude-fable-5"
    daily_budget = cfg["daily_token_budget_by_model"].get(fable_model)
    weekly_budget = cfg["weekly_token_budget_by_model"].get(fable_model)
    tok_day = roll["billable_by_model"]["day"].get(fable_model, 0)
    tok_week = roll["billable_by_model"]["week"].get(fable_model, 0)
    basis_day = describe_basis(roll["basis_by_model"]["day"].get(fable_model))
    basis_week = describe_basis(roll["basis_by_model"]["week"].get(fable_model))
    if isinstance(daily_budget, int) and daily_budget > 0 and tok_day >= daily_budget:
        return Verdict(False, (
            f"Fable's daily token budget is used up ({tok_day:,}/{daily_budget:,} tokens, {basis_day}); "
            "it resets at local midnight (the owner's day boundary)."
        ), {"day_tokens": 0})
    if isinstance(weekly_budget, int) and weekly_budget > 0 and tok_week >= weekly_budget:
        return Verdict(False, (
            f"Fable's weekly token budget is used up ({tok_week:,}/{weekly_budget:,} tokens, "
            f"{basis_week}); it resets Monday (the owner's local week)."
        ), {"week_tokens": 0})

    return Verdict(True, None, {
        "day": max(0, day_limit - day_used),
        "conversation": max(0, conv_limit - conv_used) if conversation_id else None,
    })


# --------------------------------------------------------------------------------- threshold alerts

def _alert_state_path(state_dir) -> str:
    return os.path.join(state_dir, ALERT_STATE_FILE)


def _read_alert_state(state_dir) -> dict:
    try:
        with open(_alert_state_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_alert_state(state_dir, data: dict) -> None:
    try:
        os.makedirs(state_dir, exist_ok=True)
        path = _alert_state_path(state_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def should_alert(state_dir, knob_key: str, used: float, limit: float, alert_at_pct: float,
                now: datetime | None = None) -> bool:
    """True iff `used` has crossed `alert_at_pct` of `limit` for `knob_key` AND we haven't already
    alerted on it within ALERT_REALERT_HOURS. The dedupe (re-alert at most every 6h) is persisted
    PER KNOB (a dict keyed by knob_key) rather than one global `last_alert` field, since several
    rails can cross threshold independently."""
    if not limit or limit <= 0:
        return False
    if (used / limit) * 100.0 < alert_at_pct:
        return False
    last = _read_alert_state(state_dir).get(knob_key)
    if last:
        last_dt = _parse_iso(last)
        now = now or datetime.now(timezone.utc)
        if last_dt is not None and (now - last_dt) < timedelta(hours=ALERT_REALERT_HOURS):
            return False
    return True


def record_alert_sent(state_dir, knob_key: str, now: datetime | None = None) -> None:
    """Call ONLY after a send that actually landed — never rate-limit a failed alert away (a send
    failure must retry next time, not go quiet)."""
    state = _read_alert_state(state_dir)
    state[knob_key] = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    _write_alert_state(state_dir, state)


def _unmetered_clause(count: int) -> str:
    """Never let an unmeasurable spend event vanish into a clean-looking total."""
    if not count:
        return ""
    noun = "call" if count == 1 else "calls"
    return f" ({count} {noun} could not be metered and is NOT in that figure.)"


def due_alerts(state_dir, model: str, now: datetime | None = None) -> list[dict]:
    """Which of `model`'s daily/weekly token-budget alerts are due right now, WITHOUT marking them
    sent (the caller does that via `record_alert_sent` only after a send actually lands). Each item:
    ``{"knob": "<dedupe key>", "text": "<ready-to-send message>"}``.

    Reads the BILLABLE basis and **names it in the text**. An alert whose basis can't be resolved does
    not fire at all: a budget warning that can't say what unit its number is in is the failure mode the
    billable basis exists to remove, and a silent non-alert is the safer of the two wrong answers (the
    number is still on the cockpit's Thresholds panel either way)."""
    cfg = load(state_dir)
    roll = rollups(state_dir, now=now)
    out: list[dict] = []

    for period, knob_key, used_map, when in (
        ("today", "daily_token_budget_by_model", "day",
         "Daily budget resets at local midnight (owner-local)."),
        ("this week", "weekly_token_budget_by_model", "week",
         "Weekly budget resets Monday (owner-local)."),
    ):
        budget = cfg[knob_key].get(model)
        if not isinstance(budget, int) or budget <= 0:
            continue
        basis = roll["basis_by_model"][used_map].get(model)
        if basis is None:
            continue  # no basis => no unit => no alert (see the docstring)
        used = roll["billable_by_model"][used_map].get(model, 0)
        alert_pct = SCHEMA[knob_key].get("alert_at_pct", DEFAULT_ALERT_PCT)
        key = f"{knob_key}:{model}"
        if not should_alert(state_dir, key, used, budget, alert_pct, now=now):
            continue
        pct = (used / budget) * 100.0
        unmetered = _unmetered_clause(roll["unmetered_by_model"][used_map].get(model, 0))
        out.append({"knob": key, "text": (
            f"Heads up — {model} has used {used:,}/{budget:,} tokens {period} "
            f"({pct:.0f}%, {describe_basis(basis)}). {when}{unmetered}"
        )})

    return out


def _main(argv: list[str]) -> int:
    """Smoke test: print the current config + rollups (or a fresh path's tolerant defaults) as JSON."""
    here = os.path.dirname(os.path.abspath(__file__))
    state_dir = argv[1] if len(argv) > 1 else os.path.normpath(os.path.join(here, "..", "state"))
    print(json.dumps({"config": load(state_dir), "rollups": rollups(state_dir)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
