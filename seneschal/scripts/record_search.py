#!/usr/bin/env python3
r"""record_search.py — existence-first search over the assistant's durable conversation record.
Stdlib only.

## The measured defect this exists to fix

Asked to find something the owner had said, an assistant turn searched `state/turns.jsonl` with a
hand-rolled `grep -o` carrying a fixed-width context window, found nothing, and **reported the
absence as a fact**. The phrase was in the file several times.

Every miss had the same mechanism:

| query | context padding required | actually present at | `grep -o` result |
|---|---|---|---|
| `.\{500\}some phrase.\{700\}` | 500 chars before the match | 384 chars before it | nothing |
| `.\{400\}other phra.\{500\}` | 500 chars after the match | 267 chars after it | nothing |

`grep -c 'other phra'` on the same file returns the real count. The phrase was always there — but the
context padding was made part of the match, so a phrase sitting nearer the start or end of its line
than the requested padding produced zero output, and that empty output was read as "not in the
record."

A prose rule telling a turn to "search carefully" does not bind. So the fix here is code, not a
sentence — a search whose existence check is structurally incapable of the failure above.

## The four invariants

1. **Existence is a fixed-string count, never a regex and never padded**, computed over the whole
   record before anything about context is even considered. That count is the headline of every
   result, for every term, always.
2. **Context rendering is a separate, later step that can never suppress a hit.** A match one
   character from the start or end of its text still counts and still reports, with whatever context
   the record actually holds around it — never a fixed window whose absence blanks the whole match.
3. **The output always carries `terms`, `matches_per_term`, and `searched`** — "not found" is only
   ever `matches_per_term[term] == 0` sitting beside the exact terms tried, never a bare, unstructured
   absence.
4. **An unreadable or missing store is an ERROR in the output (and a non-zero exit), never a silent
   zero.** The whole point of this tool is that "I could not check" and "I checked and it is not
   there" must never look the same on the page.

## What it searches

`state/turns.jsonl` — the append-only record of what was actually said, both sides, verbatim
(`turns.py`) — and `state/assertions.jsonl` — the append-only record of what the assistant has
actually said (`mouth.py`). Both are append-only JSONL with a `text` field holding real wording and an
`at` stamp, so one reader covers both without a bespoke path per store. `turns.jsonl` rows carry
`speaker` ∈ `owner | assistant`; `assertions.jsonl` never carries a `speaker` field, because every
row in it is something the assistant said — so `--speaker assistant` includes it and
`--speaker owner` excludes it, rather than the filter silently doing nothing on that store. (Either
store may be absent on an install that has not written one yet; that store then reports an ERROR
and the others still search — invariant 4.)

## What this is NOT

No index, no fuzzy match, no ranking, no LLM judgement of relevance. A fixed string either occurs in
the record some number of times, or it occurs zero times — and this tool's only job is to say which,
honestly, before anyone renders a snippet around it.

USAGE:
  python record_search.py --term "some phrase" --term "other phra"
  python record_search.py --term invoice --speaker owner --since 2026-09-01
  python record_search.py --term "..." --store turns --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import paths

ENV_VAR = paths.ENV_VAR  # SENESCHAL_STATE_DIR

DEFAULT_CONTEXT_CHARS = 60
DEFAULT_LIMIT = 20

# Every store this tool knows how to search. `speaker_field=None` + `implicit_speaker` set means the
# store carries no such column and every row in it stands in for that one speaker instead — see the
# module docstring for why that is `assertions.jsonl`'s honest reading rather than a gap.
STORES = {
    "turns": {"file": "turns.jsonl", "text_field": "text", "speaker_field": "speaker",
              "implicit_speaker": None},
    "assertions": {"file": "assertions.jsonl", "text_field": "text", "speaker_field": None,
                   "implicit_speaker": "assistant"},
}


def default_state_dir() -> str:
    """`SENESCHAL_STATE_DIR`, else `seneschal/state` — `paths.state_dir`, read fresh per call."""
    return paths.state_dir()


class StoreError(Exception):
    """A store could not be opened or read at all. Kept as its own exception, never coerced into a
    zero-match result — see invariant 4 in the module docstring."""


def _parse_stamp(value) -> datetime | None:
    """Tolerant read of a record's own `at` field. Anything unparseable reads as None, and every
    caller treats that as "don't include this row against a since/until bound" rather than guessing."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_arg_stamp(value: str) -> datetime | None:
    """As `_parse_stamp`, but also accepts a bare `YYYY-MM-DD` as UTC midnight — same convention
    `turns.py tail --since` already uses, so the two tools agree on what a date-only bound means."""
    parsed = _parse_stamp(value)
    if parsed is not None:
        return parsed
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iter_records(path: str):
    """Every parseable JSON object in the file, in file order.

    Raises `StoreError` if the file cannot be opened at all — a missing or permission-denied store is
    not the same statement as a store that opened cleanly and held zero matching rows, and collapsing
    that distinction is exactly the failure this module exists to end. A single malformed *line*
    inside an otherwise-open file is a different, ordinary condition (every reader in this tree
    tolerates it) and costs that line alone, never the whole store."""
    try:
        fh = open(path, encoding="utf-8")
    except OSError as exc:
        raise StoreError(f"{path}: {exc}") from exc
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _count_occurrences(haystack: str, needle: str) -> int:
    """Fixed-string, overlap-counting occurrences of `needle` in `haystack`. Never a regex, never a
    window — this is the entire fix: whether a match exists must never depend on how much text
    surrounds it."""
    if not needle:
        return 0
    count = 0
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            return count
        count += 1
        start = idx + 1  # advance by 1, not len(needle): overlapping hits are never undercounted


def _contexts(display: str, search_text: str, needle: str, width: int) -> list:
    """One excerpt per occurrence, clamped to the record's own bounds.

    Clamped, never padded: a match near the very start or end of `display` gets whatever context
    exists on that side and nothing pretends there is more. Nothing here can turn a real match into
    no output, which is the property the original `grep -o` window lacked."""
    out = []
    start = 0
    while True:
        idx = search_text.find(needle, start)
        if idx == -1:
            return out
        lo = max(0, idx - width)
        hi = min(len(display), idx + len(needle) + width)
        out.append(display[lo:hi])
        start = idx + 1


def _row_speaker(row: dict, spec: dict):
    if spec["implicit_speaker"] is not None:
        return spec["implicit_speaker"]
    field = spec["speaker_field"]
    return row.get(field) if field else None


def search(state_dir: str | None, terms: list, *, stores=None, speaker: str | None = None,
          since: datetime | None = None, until: datetime | None = None,
          surface: str | None = None, case_sensitive: bool = False,
          limit: int = DEFAULT_LIMIT, context_chars: int = DEFAULT_CONTEXT_CHARS) -> dict:
    """The one function every caller (CLI, a future advisor, a test) goes through.

    Always returns a dict carrying `terms`, `matches_per_term` and `searched`, whatever happened —
    see invariant 3 in the module docstring. `ok` is False iff at least one configured store could
    not be read; matches from stores that DID read cleanly are still reported, because a readable
    store's zero is still an honest zero even when a sibling store errored.
    """
    state_dir = state_dir or default_state_dir()
    store_names = list(stores) if stores else list(STORES)
    needles = [t if case_sensitive else t.lower() for t in terms]

    searched = []
    matches_per_term = {t: 0 for t in terms}
    hits_per_term = {t: [] for t in terms}
    any_error = False

    for name in store_names:
        spec = STORES[name]
        path = os.path.join(state_dir, spec["file"])
        entry = {"store": name, "path": path, "ok": False, "records": 0, "error": None}
        try:
            records = 0
            for row in _iter_records(path):
                records += 1
                text = row.get(spec["text_field"])
                if not isinstance(text, str) or not text:
                    continue
                row_surface = row.get("surface")
                if surface is not None and row_surface != surface:
                    continue
                row_speaker = _row_speaker(row, spec)
                if speaker is not None and row_speaker != speaker:
                    continue
                at = _parse_stamp(row.get("at"))
                if since is not None and (at is None or at < since):
                    continue
                if until is not None and (at is None or at > until):
                    continue
                haystack = text if case_sensitive else text.lower()
                for term, needle in zip(terms, needles):
                    n = _count_occurrences(haystack, needle)
                    if not n:
                        continue
                    matches_per_term[term] += n
                    if len(hits_per_term[term]) < limit:
                        hits_per_term[term].append({
                            "store": name, "at": row.get("at"), "surface": row_surface,
                            "speaker": row_speaker, "occurrences": n,
                            "context": _contexts(text, haystack, needle, context_chars),
                        })
            entry["records"] = records
            entry["ok"] = True
        except StoreError as exc:
            entry["error"] = str(exc)
            any_error = True
        searched.append(entry)

    return {
        "terms": list(terms),
        "case_sensitive": bool(case_sensitive),
        "filters": {
            "speaker": speaker, "surface": surface,
            "since": since.isoformat().replace("+00:00", "Z") if since else None,
            "until": until.isoformat().replace("+00:00", "Z") if until else None,
        },
        "searched": searched,
        "matches_per_term": matches_per_term,
        "matches": hits_per_term,
        "ok": not any_error,
    }


# --------------------------------------------------------------------------- CLI

def render(result: dict, out=None) -> None:
    out = out or sys.stdout
    headline = ", ".join(f"{t!r}: {result['matches_per_term'][t]}" for t in result["terms"])
    out.write(f"record-search: {headline}\n")
    for entry in result["searched"]:
        if entry["ok"]:
            out.write(f"  {entry['store']}: {entry['records']} record(s) searched\n")
        else:
            out.write(f"  {entry['store']}: ERROR — {entry['error']}\n")
    for term in result["terms"]:
        hits = result["matches"].get(term) or []
        if not hits:
            continue
        total = result["matches_per_term"][term]
        out.write(f"\n{term!r} ({total} total occurrence(s), showing {len(hits)} record(s))\n")
        for h in hits:
            who = h["speaker"] or "?"
            for ctx in h["context"]:
                out.write(f"  [{h['at']} {h['store']}/{who}] …{ctx}…\n")
    if not result["ok"]:
        out.write("\n[error] one or more stores could not be read — see above. This is NOT a zero "
                  "result; it is a failure to check, and must never be reported as absence.\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Existence-first search over the assistant's durable conversation record.")
    p.add_argument("--state-dir", default=None)
    p.add_argument("--term", action="append", dest="terms", required=True,
                   help="a fixed string to search for; repeatable")
    p.add_argument("--store", action="append", dest="stores", choices=sorted(STORES),
                   help="restrict to one or more stores by name (default: every store)")
    p.add_argument("--speaker", default=None)
    p.add_argument("--surface", default=None)
    p.add_argument("--since", default=None, metavar="YYYY-MM-DD|ISO")
    p.add_argument("--until", default=None, metavar="YYYY-MM-DD|ISO")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help="max records kept for CONTEXT per term (matches_per_term is never capped)")
    p.add_argument("--context", type=int, default=DEFAULT_CONTEXT_CHARS, dest="context_chars",
                   help="characters of context on each side of a match")
    p.add_argument("--case-sensitive", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    since = until = None
    if args.since:
        since = _parse_arg_stamp(args.since)
        if since is None:
            print(json.dumps({"ok": False, "error": f"unparseable --since: {args.since!r}"}))
            return 2
    if args.until:
        until = _parse_arg_stamp(args.until)
        if until is None:
            print(json.dumps({"ok": False, "error": f"unparseable --until: {args.until!r}"}))
            return 2

    result = search(args.state_dir, args.terms, stores=args.stores, speaker=args.speaker,
                    since=since, until=until, surface=args.surface,
                    case_sensitive=args.case_sensitive, limit=args.limit,
                    context_chars=args.context_chars)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        render(result)

    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
