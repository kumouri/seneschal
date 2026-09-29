#!/usr/bin/env python3
"""Bounded, always-rebuilt regions inside `state/carry-over.md`. Stdlib only.

Spec: `../docs/carry-over-region-spec.md` — read it for the full design and the why. A Wrap that
composes "tonight's snapshot + the entire previous file" and hands that whole blob to
`memory_write.py write` is indistinguishable at the byte level from a `prepend`: the file grows every
night and nothing is ever pruned. `write-region --name WRAP_SEED` replaces a bounded, marked span in
place instead, the same shape `loops.py render --write` already uses for its own `GENERATED` region
below it in the same file — this module imports that region's markers rather than re-typing them,
and refuses to write it: `loops.py` owns that region alone.

## The regions, and why there are exactly two writable slots and one read-only one

`REGIONS` is the one place a region is declared. `WRAP_SEED` is `writable=True` — this module's own
region, replaced whole every call, created at the top of the file if absent (§2.1 of the spec).
`GENERATED` is `writable=False` — declared here only so `check` can verify its structural shape
alongside `WRAP_SEED`'s, but `write-region --name GENERATED` refuses and names `loops.py render
--write` as the real owner. A generic `--name` that silently accepted any string would let a caller
invent a third region ad hoc, which is exactly the un-bounded-prose failure this file exists to close.

## The hand-written head is never a region

Everything in `carry-over.md` that is not inside a known region's span is the hand-written head
(spec §2.2) — free text, never rebuilt, never truncated by anything here. `check` counts its lines
against a tripwire (default 60, `--head-line-ceiling` to override) and only ever WARNS: the tripwire
is `loops.PROJECTION_TRIPWIRE`'s own posture, reported and never enforced, because a cap that drops
content to stay under a number destroys the only evidence it was ever there.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import loops  # noqa: E402 — GENERATED's markers are loops.py's own; never re-typed here
import memory_write  # noqa: E402

CARRY_OVER_FILE = "carry-over.md"
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))

#: The head-line tripwire (spec §2.2's default). Not a measured optimum — it sits comfortably above
#: a typical head of legitimately-open hand-written content, so it fires on GROWTH, not on the
#: file's honest size.
DEFAULT_HEAD_LINE_CEILING = 60


class RegionError(Exception):
    """A refusal. Carries a real sentence, `loops.LoopsError`'s own convention."""


class Region:
    __slots__ = ("name", "begin", "end", "writer", "writable", "insert_at")

    def __init__(self, name: str, begin: str, end: str, writer: str, writable: bool,
                 insert_at: str = "top"):
        self.name, self.begin, self.end = name, begin, end
        self.writer, self.writable, self.insert_at = writer, writable, insert_at


#: `GENERATED`'s markers are `loops.py`'s own module constants, imported rather than copied — import
#: the predicate, don't copy it, so the two writers can never disagree about where a region ends.
REGIONS = {
    "WRAP_SEED": Region(
        name="WRAP_SEED",
        begin="<!-- WRAP SEED: BEGIN -->",
        end="<!-- WRAP SEED: END -->",
        writer="seneschal/scripts/carryover_region.py write-region --name WRAP_SEED",
        writable=True,
        insert_at="top",
    ),
    "GENERATED": Region(
        name="GENERATED",
        begin=loops.BEGIN_MARK,
        end=loops.END_MARK,
        writer="seneschal/scripts/loops.py render --write",
        writable=False,
        insert_at="bottom",
    ),
}


def _dir(state_dir: str | None) -> str:
    return state_dir or DEFAULT_STATE_DIR


def carry_over_path(state_dir: str | None = None) -> str:
    return os.path.join(_dir(state_dir), CARRY_OVER_FILE)


# --------------------------------------------------------------------------- region scanning

def _find_span(text: str, region: Region) -> tuple | None:
    """`(begin_offset, end_offset)` — `end_offset` is just past the closer — or `None` if the region
    is wholly absent. Raises `RegionError` on any malformed shape: more than one opener/closer, an
    orphan closer, or a closer before its opener. Mirrors `loops.split_hand_written`'s refusals."""
    opens = text.count(region.begin)
    closes = text.count(region.end)
    if opens == 0 and closes == 0:
        return None
    if opens > 1:
        raise RegionError(
            f"{CARRY_OVER_FILE} holds {opens} `{region.name}` openers. REFUSING — with more than "
            f"one, the region's span is ambiguous. Resolve it by hand.")
    if closes > 1:
        raise RegionError(f"{CARRY_OVER_FILE} holds {closes} `{region.name}` closers. REFUSING.")
    if opens == 0:
        raise RegionError(
            f"{CARRY_OVER_FILE} holds a `{region.name}` closer with no opener. REFUSING — the file "
            f"has been hand-edited inside the region and the boundary is unknowable.")
    if closes == 0:
        raise RegionError(
            f"{CARRY_OVER_FILE} holds a `{region.name}` opener with no closer — the region is "
            f"truncated. REFUSING, because replacing it would silently discard whatever followed it.")
    begin_at = text.index(region.begin)
    end_at = text.index(region.end)
    if end_at < begin_at:
        raise RegionError(
            f"{CARRY_OVER_FILE}'s `{region.name}` closer comes before its opener. REFUSING.")
    return begin_at, end_at + len(region.end)


def scan_regions(text: str) -> dict:
    """`{name: (begin_offset, end_offset) | None}` for every declared region. Raises on the first
    malformed one — a caller that wants partial results should catch `RegionError` per region name
    itself; nothing in this module needs that, so it is not offered."""
    return {name: _find_span(text, region) for name, region in REGIONS.items()}


def head_line_count(text: str, spans: dict) -> int:
    """Lines outside every known region's span — the hand-written head (spec §2.2). A line that
    falls inside ANY region (by character offset, not by a naive line-range guess) is excluded."""
    ranges = [span for span in spans.values() if span is not None]
    if not ranges:
        return text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    lines = text.splitlines(keepends=True)
    count = 0
    offset = 0
    for line in lines:
        start = offset
        end = offset + len(line)
        offset = end
        if not any(r_start <= start < r_end for r_start, r_end in ranges):
            count += 1
    return count


# --------------------------------------------------------------------------- write-region

def _read_bytes(path: str) -> tuple:
    raw = open(path, "rb").read()
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    return raw, raw.decode(encoding), encoding


def write_region(name: str, body: str, *, state_dir: str | None = None,
                 allow_empty: bool = False) -> dict:
    """Replace `name`'s span with `body` (rendered between fresh markers), touching nothing else.

    Refuses: an unknown or non-writable region name; an empty `body` unless `allow_empty`; a missing
    target file; any malformed region (`scan_regions`); and — after composing — a result that does
    not leave every byte outside the span unchanged, asserted before the write and again after it
    (`carry-over.md` is the most-rewritten file in `state/`; a region tool for its most-written slice
    inherits `memory_write`'s paranoia rather than re-deriving it)."""
    region = REGIONS.get(name)
    if region is None:
        raise RegionError(
            f"no such region `{name}`. Known regions: {', '.join(sorted(REGIONS))}.")
    if not region.writable:
        raise RegionError(
            f"`{name}` is not writable through this tool — its owner is `{region.writer}`. "
            f"A region has exactly one writer; adding a second is refused.")
    if not body and not allow_empty:
        raise RegionError(
            f"REFUSED to write an empty `{name}` body. An empty payload is almost always a producer "
            f"that failed upstream (memory_write.write_text refuses the same shape). Pass "
            f"--allow-empty if the region really is meant to be empty.")

    path = carry_over_path(state_dir)
    if not os.path.exists(path):
        raise RegionError(
            f"{path} does not exist. REFUSING to create it — seed it from "
            f"state/{CARRY_OVER_FILE.replace('.md', '.example.md')} first "
            f"(references/memory.md's bootstrap-on-absence rule).")

    original_bytes, original_text, encoding = _read_bytes(path)
    spans = scan_regions(original_text)  # raises RegionError on any malformed region, incl. others

    newline = memory_write.detect_newline(path) or "\n"
    rendered_body = body if body.endswith("\n") or not body else body + "\n"
    region_text = region.begin + "\n" + rendered_body.rstrip("\n") + "\n" + region.end
    region_text = region_text.replace("\n", newline)

    span = spans[name]
    if span is not None:
        begin_at, end_at = span
        prefix, suffix = original_text[:begin_at], original_text[end_at:]
        new_text = prefix + region_text + suffix
        expected_outside = prefix + suffix
    else:
        # Absent: insert at the position `insert_at` says, with a separator EXACTLY when there is
        # existing content to separate from — the same idempotent-separator reasoning
        # `loops.write_render` uses, needed here only once (a region, once created, always hits the
        # `span is not None` branch above on every later call). `expected_outside` therefore includes
        # the separator: it is bytes this call is deliberately introducing, not bytes it must not
        # touch, so it belongs on both sides of the comparison rather than being asserted away.
        separator = "" if not original_text else newline * 2
        if region.insert_at == "top":
            new_text = region_text + separator + original_text
            expected_outside = separator + original_text
        else:
            new_text = original_text + separator + region_text
            expected_outside = original_text + separator

    # Re-derive "everything outside the span" from the COMPOSED text using the region's own
    # (possibly just-created) markers, and compare against the pre-write computation above — the
    # same before/after shape `loops.write_render` uses for its hand-written-prefix assertion.
    new_spans = scan_regions(new_text)
    new_begin, new_end = new_spans[name]
    outside_after = new_text[:new_begin] + new_text[new_end:]
    if outside_after != expected_outside:
        raise RegionError(
            "REFUSED: composing the new region would change bytes OUTSIDE it. Nothing was written. "
            "This should be unreachable; if it fires, read the file by hand before retrying.")

    memory_write.write_text(path, new_text, newline=None)

    after_bytes, after_text, _ = _read_bytes(path)
    after_spans = scan_regions(after_text)
    a_begin, a_end = after_spans[name]
    if (after_text[:a_begin] + after_text[a_end:]) != expected_outside:
        memory_write.write_text(path, original_text, newline=None)
        raise RegionError(
            f"POST-WRITE ASSERTION FAILED: {path} changed outside the `{name}` region. The original "
            f"{len(original_bytes):,} bytes have been restored. This should be unreachable.")

    return {"path": path, "region": name, "created": span is None,
            "bytes_before": len(original_bytes), "bytes_after": len(after_bytes)}


# --------------------------------------------------------------------------- check

def check(state_dir: str | None = None, *, head_line_ceiling: int = DEFAULT_HEAD_LINE_CEILING) -> dict:
    """Read-only. `{"path", "exists", "regions": {name: {...}}, "head_lines", "head_line_ceiling",
    "head_tripwire", "malformed": [...]}`. A missing file is not a finding (bootstrap-on-absence)."""
    path = carry_over_path(state_dir)
    if not os.path.exists(path):
        return {"path": path, "exists": False, "regions": {}, "head_lines": 0,
                "head_line_ceiling": head_line_ceiling, "head_tripwire": False, "malformed": []}

    _, text, _ = _read_bytes(path)
    regions_out = {}
    malformed = []
    spans = {}
    for name, region in REGIONS.items():
        try:
            span = _find_span(text, region)
        except RegionError as exc:
            malformed.append(str(exc))
            span = None
        spans[name] = span
        regions_out[name] = {"present": span is not None, "writable": region.writable,
                             "writer": region.writer}

    head_lines = head_line_count(text, spans)
    return {"path": path, "exists": True, "regions": regions_out, "head_lines": head_lines,
            "head_line_ceiling": head_line_ceiling, "head_tripwire": head_lines > head_line_ceiling,
            "malformed": malformed}


def render_check(result: dict) -> str:
    if not result["exists"]:
        lines = [f"{result['path']}: not present (bootstrap-on-absence; nothing to check)"]
        return "\n".join(lines)
    lines = [f"{result['path']}:"]
    for name, info in sorted(result["regions"].items()):
        state = "present" if info["present"] else "absent"
        lines.append(f"  {name}: {state} (writer: {info['writer']})")
    lines.append(f"  hand-written head: {result['head_lines']} line(s) "
                f"(ceiling {result['head_line_ceiling']})")
    if result["head_tripwire"]:
        lines.append(f"  ⚠ TRIPWIRE: head is past the ceiling. Tripwire, not a quota — nothing was "
                    f"dropped. If this keeps growing, dated ad-hoc notes probably belong in the "
                    f"register (`loops.py add`) or a dated notes file instead "
                    f"(carry-over-region-spec.md §2.4).")
    if result["malformed"]:
        lines.append(f"  {len(result['malformed'])} STRUCTURAL finding(s):")
        for m in result["malformed"]:
            lines.append(f"    {m}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Bounded regions inside state/carry-over.md (carry-over-region-spec.md).")
    p.add_argument("--state-dir", default=None, help="override seneschal/state/ (tests MUST pass this)")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write-region", help="replace one region's span; body on stdin")
    w.add_argument("--name", required=True, choices=sorted(REGIONS))
    w.add_argument("--allow-empty", action="store_true")

    c = sub.add_parser("check", help="read-only: region presence/well-formedness + head tripwire")
    c.add_argument("--head-line-ceiling", type=int, default=DEFAULT_HEAD_LINE_CEILING)
    c.add_argument("--json", action="store_true")
    c.add_argument("--enforce", action="store_true",
                   help="exit 1 on a STRUCTURAL finding only; the head tripwire never fails")

    args = p.parse_args(argv)
    sd = args.state_dir

    try:
        if args.cmd == "write-region":
            body = sys.stdin.read()
            result = write_region(args.name, body, state_dir=sd, allow_empty=args.allow_empty)
            print(json.dumps({"ok": True, **result}, ensure_ascii=False))
            return 0
        result = check(sd, head_line_ceiling=args.head_line_ceiling)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(render_check(result))
        return 1 if (args.enforce and result["malformed"]) else 0
    except RegionError as exc:
        print(f"carryover_region: refused — {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "refused": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
