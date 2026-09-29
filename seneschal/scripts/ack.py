#!/usr/bin/env python3
"""**The front door for acking reminders** — free-text phrase in, durable local ack out.

    python ack.py "watered the plants" "evening stretch"

Everything downstream of this already existed: ``reminders_dequeue.py`` pulls the obsolete queued
nudges **and** records the ack to ``state/acks.json`` that the fire-time gate reads, and — on the
**Notion backend** — ``outbox.py ack`` journals ``Status=Done`` / ``Last Acknowledged=<today>`` /
``Consecutive Misses=0`` durably. What did not exist was **anything that maps the owner's words to a
row**, so every ack was hand-resolved and then fanned out into several separate calls per row. This is
one call. It **reuses** those modules rather than reimplementing them: there is exactly one definition
of an ack payload in this tree and it is still ``outbox.cmd_ack``, and exactly one holder of the queue
lock, ``reminders_dequeue.main``.

## Resolution — NEVER GUESS

An ack written to the wrong row is a **false record**, and the evening Wrap counts ``Last
Acknowledged`` as truth. A wrong ack is strictly worse than no script, so:

* The owner's phrasings live in ``../references/reminder-aliases.json`` — per-install and gitignored,
  seeded from the tracked ``reminder-aliases.example.json``, which :func:`load_aliases` falls back to
  when the live file is absent. The row **ids** stay in the gitignored runtime cache
  ``state/reminders-id-cache.md``, read through :func:`reminders_acks.id_cache_titles` (the parser the
  Watch gate already depends on, not a second one). Alias → title → row id: two files, one hop.
* An alias matches when its tokens appear **contiguously** in the normalized phrase; the **longest**
  match wins, so ``evening req stretch`` beats the bare ``stretch``.
* **Ambiguous ⇒ refuse that phrase**, name the candidates, exit non-zero. Not caution for its own
  sake: two rows that share a word (a required and an optional variant of the same habit) are exactly
  where a "likelier row" guess lands the ack on the wrong one, and the next Wrap has to walk it back.
  Refusing hands the phrase back to the session, which can still resolve it by hand — the slow path
  stays, it just stops being the only path.
* **Unresolvable ⇒ refuse.** A title that is not in the id cache, or that the cache lists twice, is an
  error naming the title. It is never a best guess and never a store query.

## Stage 2 — the rows nobody wrote an alias for

Stage 1 is right for **habit** rows and wrong for **todo** rows. Habits are stable vocabulary; a todo
exists for a few days and then retires, so hand-authoring an alias for one costs more than the hand
lookup it replaces and is permanently behind. So on a stage-1 **miss** the phrase is matched against
the **titles of the reminder rows that are active right now** (:mod:`reminders_live`), and todo rows
work for free with nobody maintaining vocabulary. That live read is **Notion-backend only**; on a
filesystem backend :mod:`reminders_live` declines without spawning anything and a stage-1 miss refuses
(its docstring says why).

Four rules, and every one of them is the refusal facing outward:

1. **Aliases win, always.** Stage 2 runs only when **no alias matched at all**. An alias *ambiguity* is
   a refusal and stays one — falling through to a fuzzier stage after a safe refusal would lower the
   bar precisely where it was working.
2. **Stage 2's bar is HIGHER than stage 1's**, because inference is structurally more dangerous than a
   curated table. Stage 1 lets the phrase carry words the vocabulary cannot explain (*"evening req
   stretch done!"*), whereas stage 2 requires the row's title to account for **every distinctive word
   the owner said** — one unexplained word is a refusal. And a **near-tie is a refusal that names
   both**, never a pick. A refusal costs one clarifying word; a wrong ack corrupts the record.
3. **Active rows only** — the allow-list in :data:`reminders_live.ACTIVE_STATUSES`. A ``Finished`` row
   is not a candidate: matching one would resurrect a dead todo.
4. **A stage-2 match says so.** ``matched_by`` is ``"title"``, the full row title is printed, and the
   human summary carries an explicit "not a curated alias" line — so a wrong match is visible in the
   confirmation the owner reads rather than buried in an id.

**The stale-cache trap, decided explicitly:** stage 2 does **not** consult
``state/reminders-id-cache.md``. That file has no ``Status`` column, so it cannot satisfy rule 3 at
all, and it is only as fresh as its last reconcile. It reads live or it refuses; a stale-cache miss
must never become a wrong match. It also does not *write* that cache back (see :mod:`reminders_live`).
The live read is the slow path by definition and is memoized to **at most one lookup per run** no
matter how many phrases miss; ``--no-live`` turns it off entirely.

The write path is unchanged and unforked: a stage-2 match goes through the same :func:`apply_ack`.

## The AM/PM rule

Rows that exist in an AM and a PM copy are declared as a ``clock_pairs`` entry carrying the aliases
that name **neither** side. Those resolve off the **owner's local clock**, not off the phrase: *"walked
the dog"* at 18:33 is ``Walk the dog (PM)``. Each pair declares its own ``pm_from`` cutover in the data
file (noon by default). A phrase that *does* name a side matches that row's own alias and never reaches
the clock; a phrase naming the side the clock did **not** pick refuses (:func:`_named_side`). The chosen
side and the reason are always printed.

**The clock is the owner's, via ``clock``/``tz_common``** — the configured ``owner.timezone`` with its
machine-local fallback — so a daemon hosted in another zone still reads the owner's morning as morning.
Tests **inject the instant** through the ``now=`` seam on :func:`resolve` and :func:`main`: a **naive**
``now`` is taken as the owner's wall time (so a pinned AM/PM case passes on any runner), an **aware**
one is converted into the owner's zone.

## The after-midnight rule

The ack **ledger** stamps the owner's *calendar* date (``--ack-date`` or today), because the fire-time
gate compares against that same calendar function. The **dequeue** is scoped by the *activity* day
(``activity_day.from_local``: before ``owner.dayBoundaryHour`` it is still yesterday), so an ack at
01:30 cancels the evening's nudges it made obsolete, not the next day's. An explicit ``--ack-date`` is a
backfill and names the day outright for both.

## The store write is the caller's

This script never writes the store itself — stdlib, no MCP. On the Notion backend it journals the ack
to the write-behind outbox and leaves the entry ``pending``; the replay (``store-update`` by the row's
cached page id) and ``outbox.py mark --done`` belong to the same turn (``modes/chat.md``). On a
filesystem backend there is no outbox (``store/notion/mapping.md``: the outbox is Notion-only), so that
step is reported as ``skipped`` and the turn writes the row directly through the store verbs.

## Exit codes

``0`` every phrase resolved and every write enqueued · ``2`` usage · ``3`` at least one phrase did not
resolve · ``4`` all resolved but at least one write failed. A partial success is **legible, never
averaged into "ok"**: the summary names each phrase's own outcome.

Stdlib only. The last line of output is always one JSON object (``--json`` prints only that).
"""
import argparse
import io
import json
import os
import re
import sys
from contextlib import redirect_stdout
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import activity_day as ad  # noqa: E402 — the after-midnight rule that scopes which day's nudges go
import clock  # noqa: E402 — the owner's wall clock (tz_common underneath)
import outbox as outbox_cli  # noqa: E402 — REUSED as a module: one definition of the ack payload
import outbox_common as ob  # noqa: E402 — read-only: the idempotency key + a same-day lookup
import reminders_acks as ra  # noqa: E402 — the id-cache parser + the ack-ledger key shape
import reminders_dequeue as rd  # noqa: E402 — REUSED: it holds the queue lock and records the ack
import reminders_live as rl  # noqa: E402 — stage 2's live row list + the store-backend gate

REFERENCES_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "references"))
ALIASES_FILE = "reminder-aliases.json"
ALIASES_EXAMPLE_FILE = "reminder-aliases.example.json"

#: Everything that is not a letter or a digit dissolves to a token break, so ``plants watered!``,
#: ``Trash out.`` and ``evening req stretch done`` all tokenize the way the alias table spells them.
_WORD_RE = re.compile(r"[^0-9a-z]+")

#: Dash variants collapse when comparing a title in the alias file against the title in the runtime id
#: cache: ``Evening stretch — required`` must not fail to match itself over an em dash someone retyped
#: as a hyphen. Tolerant READER, strict writer. A normalization that collided two genuinely different
#: rows would be caught: :func:`title_index` refuses a duplicate rather than picking one.
_DASHES = str.maketrans({"—": "-", "–": "-", "‒": "-", "−": "-"})


def tokens(text) -> list:
    """The comparable word tokens of ``text``. Total — a non-string is ``[]``."""
    if not isinstance(text, str):
        return []
    return _WORD_RE.sub(" ", text.lower()).split()


def norm_title(text) -> str:
    """A reminder title reduced to its comparable form: casefolded, dash variants unified, whitespace
    collapsed. Used only to join the alias file to the runtime id cache."""
    if not isinstance(text, str):
        return ""
    return " ".join(text.translate(_DASHES).casefold().split())


def aliases_path(references_dir: str = REFERENCES_DIR) -> str:
    """The alias file :func:`load_aliases` reads: the live per-install ``reminder-aliases.json`` when
    it exists, else the tracked ``reminder-aliases.example.json`` seed."""
    live = os.path.join(references_dir, ALIASES_FILE)
    return live if os.path.exists(live) else os.path.join(references_dir, ALIASES_EXAMPLE_FILE)


def load_aliases(references_dir: str = REFERENCES_DIR) -> dict:
    """The alias vocabulary. Raises on a missing/malformed file **on purpose** — this file is the whole
    basis for deciding which row an ack means, and silently resolving nothing against an empty table
    would look exactly like "the owner said something I don't know"."""
    with open(aliases_path(references_dir), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _local_now(now: datetime | None = None) -> datetime:
    """The owner's wall clock as a naive datetime.

    ``None`` reads the clock (``clock.now_local``). A **naive** ``now`` is taken as the owner's wall
    time — the test seam that lets an AM/PM case be pinned on any runner. An **aware** one is
    converted into the owner's zone (``clock.to_local``)."""
    if now is None:
        return clock.now_local()
    if now.tzinfo is None:
        return now
    return clock.to_local(now)


def _local_date(now: datetime | None = None) -> str:
    """The owner's calendar date through **this module's** naive-is-owner-wall-time seam.

    Deliberately not :func:`reminders_acks.local_today`, which reads a *naive* instant as **UTC** —
    right there (its caller holds a UTC stamp), wrong here, where every ``now=`` is the owner's wall
    clock the AM/PM rule reads. With no argument the two agree."""
    return _local_now(now).strftime("%Y-%m-%d")


def _parse_hhmm(value, default=(12, 0)) -> tuple:
    """``"12:00"`` → ``(12, 0)``. An unparseable cutover falls back to noon rather than raising: a
    typo in the data file must not make an ack impossible, and noon is the conservative split."""
    if isinstance(value, str):
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", value)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            return int(m.group(1)), int(m.group(2))
    return default


def dashed_id(page_id: str) -> str:
    """Put the dashes back on a 32-hex page id: ``8-4-4-4-12``.

    :func:`reminders_acks.id_cache_titles` keys by ``norm_key``, which strips them — correct for
    *matching* (every id comparison in this tree normalizes both sides) but wrong for what gets
    **written**: the ``reminder_id`` in an ack payload is handed to the store's update call by whatever
    drains the outbox, and every other row carries the canonical dashed form. Re-dashing is exact
    rather than a guess: the input is fixed-width hex. Anything else passes through unchanged."""
    if isinstance(page_id, str) and ra._HEX32_RE.match(page_id):
        return "-".join((page_id[:8], page_id[8:12], page_id[12:16], page_id[16:20], page_id[20:]))
    return page_id


def title_index(state_dir: str) -> dict:
    """``{normalized title: page id | None}`` from ``state/reminders-id-cache.md``.

    Built from :func:`reminders_acks.id_cache_titles` rather than a second reader of the same table,
    so the id cache has exactly one parser in this tree. A title the cache lists **twice** maps to
    ``None``, which :func:`resolve` reports as an error — ambiguity is never resolved by picking."""
    out: dict = {}
    for page_id, rec in ra.id_cache_titles(state_dir).items():
        key = norm_title(rec.get("title"))
        if not key:
            continue
        out[key] = None if key in out else dashed_id(page_id)
    return out


def _index(aliases: dict) -> list:
    """``[(alias_tokens, kind, target), ...]`` — the flat match table. ``kind`` is ``"row"`` (target:
    the row dict) or ``"pair"`` (target: the clock_pairs dict). Built fresh per run."""
    out = []
    for row in aliases.get("rows") or []:
        if not isinstance(row, dict) or not row.get("title"):
            continue
        for alias in row.get("aliases") or []:
            toks = tokens(alias)
            if toks:
                out.append((toks, "row", row))
    for pair in aliases.get("clock_pairs") or []:
        if not isinstance(pair, dict) or not (pair.get("am") and pair.get("pm")):
            continue
        for alias in pair.get("aliases") or []:
            toks = tokens(alias)
            if toks:
                out.append((toks, "pair", pair))
    return out


def _contains(haystack: list, needle: list) -> bool:
    """Does ``needle`` appear as a **contiguous** run of tokens in ``haystack``? Contiguity is what keeps
    ``evening stretch`` from matching *"evening, I'll stretch later"* — a looser containment would match
    more phrases than the vocabulary actually covers, which is how a wrong ack happens."""
    n, m = len(haystack), len(needle)
    if m == 0 or m > n:
        return False
    return any(haystack[i:i + m] == needle for i in range(n - m + 1))


def resolve(phrase: str, aliases: dict, titles: dict, now: datetime | None = None,
            live_rows=None) -> dict:
    """Resolve one free-text phrase to a reminder row. **Never guesses.**

    Returns a dict that always carries ``phrase`` and ``ok``. On success: ``title``, ``reminder_id``,
    ``matched_alias``, ``matched_by`` (``"alias"``/``"title"``), ``side`` (``"am"``/``"pm"``/``None``)
    and ``why`` — the human sentence naming what matched and, for a clock pair, which side the clock
    chose and against what cutover. On failure: ``error`` and, for an ambiguity, the ``candidates`` it
    was torn between.

    ``live_rows`` is stage 2: a **zero-argument callable** returning ``(active rows, error)``,
    consulted **only** when no alias matched at all. A callable rather than a list so the lookup stays
    lazy — a run whose every phrase resolves off the vocabulary never pays for it — and so one memoized
    provider serves every phrase in the run. ``None`` means stage 2 is not wired for this call, which is
    a refusal like any other."""
    result = {"phrase": phrase, "ok": False, "title": None, "reminder_id": None,
              "matched_alias": None, "matched_by": None, "side": None, "why": None,
              "error": None, "candidates": []}
    phrase_tokens = tokens(phrase)
    if not phrase_tokens:
        result["error"] = "empty phrase"
        return result

    hits = [(toks, kind, target) for toks, kind, target in _index(aliases)
            if _contains(phrase_tokens, toks)]
    if not hits:
        return _resolve_by_title(result, phrase, live_rows)

    best = max(len(toks) for toks, _, _ in hits)
    wall = _local_now(now)
    winners = {}  # normalized title -> (title, alias_text, side, why)
    for toks, kind, target in hits:
        if len(toks) != best:
            continue
        alias_text = " ".join(toks)
        if kind == "row":
            title, side = target["title"], None
            why = f"matched *{title}* — alias {alias_text!r}"
        else:
            hour, minute = _parse_hhmm(target.get("pm_from"))
            is_pm = (wall.hour, wall.minute) >= (hour, minute)
            side = "pm" if is_pm else "am"
            title = target["pm"] if is_pm else target["am"]
            said = _named_side(phrase_tokens, aliases)
            if said and said != side:
                result["candidates"] = sorted([target["am"], target["pm"]])
                result["error"] = (
                    f"ambiguous — the clock says {side.upper()} (local {wall.strftime('%H:%M')} "
                    f"vs a {hour:02d}:{minute:02d} cutover) but {phrase!r} says {said.upper()}. "
                    f"Refusing rather than overruling either; ack the intended row by hand.")
                return result
            why = (f"matched *{title}* — {'evening' if is_pm else 'morning'} "
                   f"(local {wall.strftime('%H:%M')} {'≥' if is_pm else '<'} "
                   f"{hour:02d}:{minute:02d} cutover), alias {alias_text!r}")
        winners.setdefault(norm_title(title), (title, alias_text, side, why))

    if len(winners) > 1:
        candidates = sorted(v[0] for v in winners.values())
        result["candidates"] = candidates
        result["error"] = (f"ambiguous — {phrase!r} matches {len(candidates)} rows: "
                           + ", ".join(candidates)
                           + ". Refusing rather than guessing; ack the intended row by hand.")
        return result

    title, alias_text, side, why = next(iter(winners.values()))
    key = norm_title(title)
    if key not in titles:
        result["error"] = (f"row {title!r} is not in state/reminders-id-cache.md — no row id, so "
                           f"nothing to ack. Reconcile the cache (Dream) or ack by hand.")
        return result
    page_id = titles[key]
    if page_id is None:
        result["error"] = f"row {title!r} appears more than once in state/reminders-id-cache.md"
        return result

    result.update(ok=True, title=title, reminder_id=page_id, matched_alias=alias_text,
                  matched_by="alias", side=side, why=why)
    return result


def title_candidates(phrase: str, rows: list) -> list:
    """The active rows whose **title accounts for every distinctive word of the phrase**.

    Stage 2's whole matching rule, and its direction is the opposite of stage 1's on purpose. Stage 1
    asks *"does the phrase contain a curated alias?"*, which lets the owner say anything else around
    it. Stage 2 asks *"can this row's name explain everything they said?"* — so *"release PR"* finds
    *"Open the release-notes PR"*, while *"release PR and the dishes"* finds nothing, because
    ``dishes`` is unaccounted for. That asymmetry is the higher bar.

    Tokenized by :func:`reminders_acks.significant_tokens` — the tree's one distinctive-token reader —
    so filler and sub-3-character fragments dissolve and a phrase with nothing distinctive (*"did
    it"*) matches **no** row rather than every row. Deduped by page id; two genuinely different rows
    sharing a title stay two candidates, which the caller reads as the ambiguity it is."""
    want = ra.significant_tokens(phrase)
    if not want:
        return []
    out, seen = [], set()
    for row in rows or []:
        title = (row or {}).get("title")
        if not isinstance(title, str) or row.get("key") in seen:
            continue
        if want <= ra.significant_tokens(title):
            seen.add(row.get("key"))
            out.append(row)
    return out


def _resolve_by_title(result: dict, phrase: str, live_rows) -> dict:
    """Stage 2, reached only on a stage-1 miss. Fills ``result`` in place and returns it.

    Every exit but one is a refusal, and each names what would fix it: a phrase the alias table could
    have covered, a lookup that could not run, a row that is retired, a tie between two live rows."""
    base = (f"no alias matches {phrase!r} — add the owner's wording to references/{ALIASES_FILE} "
            f"rather than guessing a row")
    if live_rows is None:
        result["error"] = f"{base}; no live reminder title lookup was wired for this call"
        return result
    if not ra.significant_tokens(phrase):
        # Checked BEFORE the lookup: a phrase with nothing distinctive in it cannot match any title,
        # so there is nothing a model spawn could buy here except the delay.
        result["error"] = (f"{base}; and {phrase!r} carries no distinctive word to match a row title "
                           f"against, so matching it would name every row equally")
        return result
    rows, error = live_rows()
    if rows is None:
        result["error"] = f"{base}; the live reminder title lookup could not answer either ({error})"
        return result

    hits = title_candidates(phrase, rows)
    if not hits:
        result["error"] = (f"{base}; and no ACTIVE reminder row title accounts for every distinctive "
                           f"word of {phrase!r} ({len(rows)} active row(s) checked)")
        return result
    if len(hits) > 1:
        result["candidates"] = sorted(r["title"] for r in hits)
        result["error"] = (f"ambiguous by title — {phrase!r} fits {len(hits)} active reminder rows: "
                           + ", ".join(result["candidates"])
                           + ". Refusing a near-tie rather than picking; say one more word from the "
                             "row you mean, or ack it by hand.")
        return result

    row = hits[0]
    result.update(ok=True, title=row["title"], reminder_id=dashed_id(row["key"]),
                  matched_alias=None, matched_by="title", side=None,
                  why=(f"matched *{row['title']}* — BY TITLE, not by an alias (nothing in "
                       f"references/{ALIASES_FILE} matched {phrase!r}; live reminder lookup, "
                       f"status {row['status']})"))
    return result


def _named_side(phrase_tokens: list, aliases: dict) -> str | None:
    """``"am"`` / ``"pm"`` if the phrase itself names a side (``side_words`` in the alias file), else
    ``None``. Both named, or neither, is ``None`` — only an unambiguous statement counts.

    This exists for one failure: *"walked the dog this morning"* typed at 19:00. The declared rule is
    that the clock decides, so the clock would write the **PM** row — a false record on a row not done,
    with the AM row that *was* done left open. :func:`resolve` refuses instead. It does not flip to the
    named side either: overriding the declared rule silently is the same guess facing the other way."""
    said = {side for side in ("am", "pm")
            for word in (aliases.get("side_words") or {}).get(side) or []
            if word in phrase_tokens}
    return said.pop() if len(said) == 1 else None


def _run_cli(func, argv: list) -> tuple:
    """Run a reused module's ``main(argv)``, capturing its JSON line. ``(rc, parsed)``.

    Capturing rather than reimplementing is the point: ``outbox.cmd_ack`` stays the only place an ack
    payload or an idempotency key is constructed, and ``reminders_dequeue.main`` stays the only place
    that takes the queue lock. A non-JSON line is returned under ``raw`` rather than raising."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = func(argv)
    except SystemExit as e:  # argparse's own exit path (a bad flag) — a failure, not a crash
        rc = e.code if isinstance(e.code, int) else 2
    lines = [ln for ln in buf.getvalue().strip().splitlines() if ln.strip()]
    if not lines:
        return rc, {}
    try:
        return rc, json.loads(lines[-1])
    except ValueError:
        return rc, {"raw": lines[-1]}


def _journaled_status(state_dir: str, reminder_id: str, date: str) -> str | None:
    """The ``Status`` already journaled for this row on this date, or ``None``. Read-only: it opens the
    outbox, looks one key up and closes it. Any error is ``None``, which the caller treats as "can't
    confirm the retire landed", i.e. the loud side."""
    try:
        conn = ob.connect(state_dir)
        try:
            entry = ob.get_by_key(conn, ob.ack_key(reminder_id, date))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — a lookup failure may only ever make the report MORE cautious
        return None
    payload = (entry or {}).get("payload")
    return payload.get("status") if isinstance(payload, dict) else None


def _outbox_ack(res: dict, args, date: str) -> dict:
    """The durable-journal step. **Notion backend only**, gated exactly like the outbox itself
    (``reminders_live.store_backend``): on any other backend the store write is the turn's direct
    ``store-update`` and there is nothing to journal, so the step reports ``skipped`` and succeeds."""
    backend = rl.store_backend()
    if backend != "notion":
        return {"op": "outbox.ack", "ok": True, "skipped": True, "status": args.status,
                "reason": (f"store backend is {backend or 'unconfigured'}, not notion — no outbox; "
                           f"write the row directly through the store verbs")}
    rc, out = _run_cli(outbox_cli.main, ["--state-dir", args.state_dir, "ack",
                                         "--reminder-id", res["reminder_id"],
                                         "--ack-date", date, "--status", args.status])
    ack_ok, ack_error = rc == 0, out.get("error")
    if ack_ok and args.status == "Finished" and out.get("created") is False:
        # The outbox dedups on `ack:<row>:<date>`, which does NOT carry the status — so a `Finished`
        # after a same-day `Done` is a silent no-op, and the row the owner asked to RETIRE keeps
        # firing tomorrow. Widening that key is not this script's call (the dedup is load-bearing),
        # so the no-op is surfaced instead: a refused retire they can see beats a swallowed one.
        if _journaled_status(args.state_dir, res["reminder_id"], date) != "Finished":
            ack_ok = False
            ack_error = (f"an ack for this row is already journaled for {date}, and the outbox dedups "
                         f"on row+date regardless of status — so Finished did NOT land and the row is "
                         f"not retired. Drain the outbox, then re-run with --status Finished.")
    # `revived` rides through unchanged from `ob.enqueue`'s own answer: `created=False` alone cannot
    # tell a genuine same-day repeat (nothing to drain) from a dead-lettered entry just re-armed to
    # pending (a real write that needs draining same as a fresh one).
    return {"op": "outbox.ack", "ok": ack_ok, "status": args.status,
            "created": out.get("created"), "revived": out.get("revived"),
            "id": out.get("id"), "error": ack_error}


def apply_ack(res: dict, args, now: datetime | None = None) -> dict:
    """Ack one **resolved** row: outbox ack (Notion backend) → dequeue + ledger.

    Returns the resolution dict enriched with ``writes`` (one record per step) and ``ok``. Order
    matters: the durable journal is written first, so a failure in the dequeue can never leave the
    ack itself unrecorded.

    The dequeue is handed an explicit ``--activity-day``: this path *always* passes an ``--ack-date``
    (the calendar date), so leaving the scoping to that flag's fallback would aim a post-midnight ack
    at the wrong day's nudges."""
    date = args.ack_date or _local_date(now)
    res["ack_date"] = date
    writes = [_outbox_ack(res, args, date)]

    # The ledger date and the ACTIVITY day are different questions, and they disagree from midnight
    # until the day boundary. The ledger keeps the calendar date (the fire-time gate compares against
    # the same function); the dequeue is scoped by the after-midnight rule on the owner's clock. An
    # explicit --ack-date is a backfill and names the day outright, so it wins for both.
    scope_day = args.ack_date or ad.from_local(_local_now(now)).isoformat()
    rc, out = _run_cli(rd.main, ["--reminder-id", res["reminder_id"],
                                 "--state-dir", args.state_dir, "--ack-date", date,
                                 "--activity-day", scope_day])
    writes.append({"op": "reminders_dequeue", "ok": rc == 0,
                   "removed": out.get("removed"), "acked": out.get("acked"),
                   "activity_day": out.get("activity_day"), "error": out.get("error")})

    res["writes"] = writes
    res["ok"] = all(w["ok"] for w in writes)
    if not res["ok"]:
        failed = [w["op"] for w in writes if not w["ok"]]
        res["error"] = "write failed: " + ", ".join(sorted(set(failed)))
    return res


def plan_ack(res: dict, args, now: datetime | None = None) -> dict:
    """``--dry-run``: the same decisions, no writes."""
    date = args.ack_date or _local_date(now)
    res["ack_date"] = date
    backend = rl.store_backend()
    planned = []
    if backend == "notion":
        planned.append({"op": "outbox.ack", "reminder_id": res["reminder_id"], "status": args.status,
                        "last_acknowledged": date})
    planned.append({"op": "reminders_dequeue", "reminder_id": res["reminder_id"], "ack_date": date,
                    "activity_day": args.ack_date or ad.from_local(_local_now(now)).isoformat()})
    res["writes"] = planned
    res["ok"] = True
    return res


def _human(results: list, dry_run: bool) -> list:
    """The readable half of the summary — one block per phrase, successes and refusals alike."""
    lines = []
    for r in results:
        if not r["ok"]:
            lines.append(f"✗ {r['phrase']!r} — {r['error']}")
            for c in r.get("candidates") or []:
                lines.append(f"    candidate: {c}")
            continue
        verb = "would ack" if dry_run else "acked"
        lines.append(f"✓ {r['phrase']!r} → {verb} {r['why']}")
        if r.get("matched_by") == "title":
            # Rule 4: a title match must be visible in the confirmation the owner reads, with the row
            # named in full — an inferred match that looks identical to a curated one is unreviewable.
            lines.append(f"    ⚠ matched by ROW TITLE, not by a curated alias. The row is "
                         f"{r['title']!r} — if that is not the one meant, this ack is wrong. "
                         f"Worth adding the phrasing to references/{ALIASES_FILE}.")
        for w in r.get("writes") or []:
            if w.get("skipped"):
                lines.append(f"    ({w['op']} skipped: {w.get('reason')})")
    return lines


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ack one or more reminders from free-text phrases (resolve → journal → dequeue).")
    p.add_argument("phrases", nargs="*", metavar="PHRASE",
                   help="what the owner said, e.g. \"watered the plants\" \"evening stretch\"")
    p.add_argument("--state-dir", default=ra.DEFAULT_STATE_DIR,
                   help="dir holding reminders-id-cache.md / reminders.json / the outbox")
    p.add_argument("--references-dir", default=REFERENCES_DIR,
                   help=f"dir holding {ALIASES_FILE} (else {ALIASES_EXAMPLE_FILE})")
    p.add_argument("--ack-date", metavar="YYYY-MM-DD",
                   help="owner-local ack date (default: today). Backfill only.")
    p.add_argument("--status", default="Done", choices=["Done", "Finished"],
                   help="Done = done-for-today (default). Finished RETIRES the row and is only ever "
                        "correct on the owner's explicit \"I'm finished with it\".")
    p.add_argument("--no-live", action="store_true",
                   help="skip stage 2 — resolve off the alias vocabulary only, never against live "
                        "reminder row titles. A phrase no alias covers then simply refuses.")
    p.add_argument("--claude-bin", default="claude", help="the CLI stage 2's live read spawns")
    p.add_argument("--notion-mcp", action="append", metavar="PATH",
                   help="MCP config for that read (repeatable). Default: the store config's "
                        "backends.notion.mcp_config, else scripts/notion-mcp.json, else whatever "
                        "claude inherits at user scope.")
    p.add_argument("--live-model", help="model for the live read — it copies a table out, it does "
                                        "not judge, so the cheap one is the right one")
    p.add_argument("--live-timeout", type=int, default=rl.DEFAULT_TIMEOUT_SEC)
    p.add_argument("--dry-run", action="store_true", help="resolve and print every write; write nothing")
    p.add_argument("--json", action="store_true", help="print only the JSON summary line")
    return p


class _LiveLookup:
    """Stage 2's row list, fetched **at most once per run** and only if some phrase actually misses.

    The memo is the whole point: a two-phrase ack where one phrase is unknown must cost one live read,
    not one per phrase, and a run whose phrases all resolve off the alias table must cost none. A failed
    lookup is memoized too — retrying it per phrase would multiply a timeout by the phrase count while
    changing no answer."""

    def __init__(self, args, runner=None):
        self._args, self._runner, self._done = args, runner, False
        self._rows, self._error = None, None

    def __call__(self) -> tuple:
        if not self._done:
            self._done = True
            self._rows, self._error = rl.fetch_active_rows(
                runner=self._runner, claude_bin=self._args.claude_bin,
                mcp_configs=self._args.notion_mcp, model=self._args.live_model,
                timeout=self._args.live_timeout)
        return self._rows, self._error


def _live_off() -> tuple:
    """The ``--no-live`` provider: stage 2 is reachable but declines, and says which flag did it."""
    return None, "--no-live was passed, so only the alias vocabulary was consulted"


def main(argv=None, now: datetime | None = None, runner=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.phrases:
        print(json.dumps({"ok": False, "error": "give at least one phrase"}))
        return 2
    try:
        aliases = load_aliases(args.references_dir)
    except (OSError, ValueError) as e:
        print(json.dumps({"ok": False, "error": f"cannot load the reference data: {e}"}))
        return 2

    titles = title_index(args.state_dir)
    live = _live_off if args.no_live else _LiveLookup(args, runner)
    results, unresolved, write_failed = [], False, False
    for phrase in args.phrases:
        res = resolve(phrase, aliases, titles, now, live_rows=live)
        if not res["ok"]:
            unresolved = True
        elif args.dry_run:
            res = plan_ack(res, args, now)
        else:
            res = apply_ack(res, args, now)
            if not res["ok"]:
                write_failed = True
        results.append(res)

    rc = 3 if unresolved else (4 if write_failed else 0)
    if not args.json:
        for line in _human(results, args.dry_run):
            print(line)
    print(json.dumps({"ok": rc == 0, "exit": rc, "dry_run": bool(args.dry_run),
                      "acked": [r["title"] for r in results if r["ok"]],
                      # Surfaced at the top level, not only per-result: a caller that reads just this
                      # line (every --json consumer) still sees which acks were INFERRED from a row
                      # title rather than matched against the curated vocabulary.
                      "by_title": [r["title"] for r in results
                                   if r["ok"] and r.get("matched_by") == "title"],
                      "refused": [r["phrase"] for r in results if not r["ok"]],
                      "aliases_file": os.path.basename(aliases_path(args.references_dir)),
                      "results": results}, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
