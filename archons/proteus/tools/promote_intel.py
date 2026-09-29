#!/usr/bin/env python3
"""Promote pending company intel into the curated ledger — locally by default, by PR on opt-in.

The split this implements:

    Proteus PROPOSES  ->  state/company-intel-pending.jsonl   (gitignored, append-only)
    promotion RECORDS ->  company-intel.json                  (curated, owner-audited, GITIGNORED)

Why it exists at all: ``company-intel.json`` is the archon's durable company memory — the owner's
data, so it is **gitignored** in this framework (``archons/proteus/.gitignore``) like
``profile.json``. The archon still never writes it itself: a headless archon's whole-file rewrite is
unreliable (a finding it "recorded" can simply never land) and would bypass the invariants below. It
appends proposals instead, and this script is the one reviewed path that merges them.

**Default (``--apply``): a local merge.** The merged ledger is written atomically over the local,
gitignored ``company-intel.json``; queue lines whose content landed are then pruned. **No git, no
commit, no PR** — nothing about the owner's company judgements leaves the machine.

**Opt-in (``--apply --via-pr``): for an owner who keeps the ledger in a PRIVATE fork.** Builds the
change in a transient git worktree off ``origin/<base>`` with ``git add -f`` (the file is ignored) and
OPENS a PR; the merge is Dream's (step 2e: an ask-high PR the merge guard holds for the owner's
approval). **WARNING: this PUBLISHES company-intel.json to whatever remote the checkout pushes to.**
Never use it against a public remote. On this path the queue is pruned lazily — each run first drops
whatever a *prior* promotion already landed — so a PR that never merges can't strand its entries
(see ``prune_landed``).

    python promote_intel.py --dry-run            # show the merge + any refusals, touch nothing
    python promote_intel.py --apply              # merge into the local ledger, prune what landed
    python promote_intel.py --apply --via-pr     # PRIVATE FORKS ONLY: worktree -> commit -> PR

The merge itself is a pure function (``merge_intel``) so every invariant below is unit-testable without
git, a network, or a clock.

**Owner entries.** An entry is the OWNER's (and so protected — see ``_weakens``) when its ``source``
mentions ``owner`` (e.g. ``"owner: seeded from profile.json"``) or it carries ``"by_owner": true``.
The archon's own proposals name their work-up in ``source`` instead.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from proteus_paths import (  # noqa: E402  (proteus_paths also puts seneschal/scripts on sys.path)
    COMPANY_INTEL_FILE, INTEL_PENDING_FILE, owner_today, write_text)

# Optional commit-signing hook: an install that ships a `commit_sign.sign(message)` helper in
# `seneschal/scripts/` gets its promotion commits signed; without one the message goes out as-is.
try:
    from commit_sign import sign as sign_commit  # type: ignore  # noqa: E402
except ImportError:  # the framework ships no signer — identity is the honest default
    def sign_commit(message: str) -> str:
        return message

# Mirrors score_jobs.INTEL_ADJUST_MAX (20.0). Restated rather than imported: these are separate tools
# and a promotion must not start failing because the scorer moved. If they ever diverge, the scorer's
# clamp still wins at scoring time — this one only keeps the stored data honest.
INTEL_ADJUST_MAX = 20.0

GIT_FLAGS = ["-c", "core.fsmonitor=false"]  # fsmonitor can hang a scripted git on a large checkout

#: The integration branch the promotion PR targets. Git Flow repos integrate on `develop`; override
#: with `--base` (or $PROTEUS_INTEL_BASE) for a checkout that integrates elsewhere.
DEFAULT_BASE = os.environ.get("PROTEUS_INTEL_BASE", "develop")


# ---------------------------------------------------------------------------------------------
# Pure merge logic
# ---------------------------------------------------------------------------------------------

def _is_owners(entry: dict) -> bool:
    """Is this entry one of the OWNER's decisions? Those are the ones an archon may never soften.

    Matched on an explicit ``by_owner: true`` or the ``source`` string containing "owner" —
    deliberately loose, because the failure direction matters: mistaking one of the archon's entries
    for the owner's costs a refused promotion the owner can override, while mistaking the OWNER's for
    the archon's silently overwrites a human judgement. Prefer the cheap false positive.
    """
    if entry.get("by_owner") is True:
        return True
    return "owner" in str(entry.get("source", "")).lower()


def _clamp(value: float) -> float:
    return max(-INTEL_ADJUST_MAX, min(INTEL_ADJUST_MAX, value))


def _weakens(old: dict, new: dict) -> str | None:
    """Would applying ``new`` over ``old`` WEAKEN it? Returns a human reason, or None if it's safe.

    "Weaken" is: moving the adjustment toward zero, flipping its sign, dropping tags, or replacing the
    owner's note. An archon may *strengthen* a concern it independently re-confirmed — that is new
    evidence, not a reversal — but it may never quietly soften one of the owner's decisions. A
    charter can say so in prose; this is what enforces it.

    The note check matters on its own, not just as a backstop for the magnitude test below: a
    **reading-rule** entry (``adjust: 0.0``, all its content in prose) makes the magnitude/sign test
    structurally vacuous — ``abs(new_adj) < abs(0)`` can never be true, so nothing about the number can
    ever flag a weakening. Without checking the note directly, ANY proposed entry — including one an
    archon reads as "strengthening" because its adjust is nonzero — silently replaces the owner's
    prose with whatever (or nothing) the new entry carries.
    """
    try:
        old_adj = float(old.get("adjust", 0) or 0)
        new_adj = float(new.get("adjust", 0) or 0)
    except (TypeError, ValueError):
        return "the proposed adjust is not a number"

    if old_adj != 0 and new_adj != 0 and (old_adj > 0) != (new_adj > 0):
        return f"flips the sign of the owner's adjustment ({old_adj:+g} -> {new_adj:+g})"
    if abs(new_adj) < abs(old_adj):
        return f"moves the owner's adjustment toward zero ({old_adj:+g} -> {new_adj:+g})"

    old_tags = {str(t).lower() for t in (old.get("tags") or [])}
    new_tags = {str(t).lower() for t in (new.get("tags") or [])}
    dropped = old_tags - new_tags
    if dropped:
        return f"drops the owner's tag(s): {', '.join(sorted(dropped))}"

    old_note = str(old.get("note", "") or "").strip()
    new_note = str(new.get("note", "") or "").strip()
    if old_note and new_note != old_note:
        return "replaces the owner's note text"
    return None


def merge_intel(tracked: dict, pending: list[dict]) -> tuple[dict, list[str], list[dict]]:
    """Merge ``pending`` entries into the ``tracked`` ledger.

    Returns ``(new_ledger, notes, refused)`` — ``notes`` are human-readable lines for the PR body,
    ``refused`` are the pending entries that were NOT applied (they stay queued for the owner's call).

    Invariants enforced here, in code, rather than asked for in charter prose:
      * clamp `adjust` to ±20
      * update an existing company in place rather than duplicating it
      * NEVER weaken or remove an entry sourced to the owner
    """
    ledger = json.loads(json.dumps(tracked))  # deep copy; never mutate the caller's dict
    companies = ledger.setdefault("companies", {})
    notes: list[str] = []
    refused: list[dict] = []

    for entry in pending:
        name = str(entry.get("company", "")).strip()
        if not name:
            refused.append(entry)
            notes.append("- SKIPPED an entry with no company name")
            continue

        # Match case-insensitively so "acme corp" updates "Acme Corp" instead of creating a twin,
        # but keep the EXISTING key's spelling so the file doesn't churn on capitalisation.
        key = next((k for k in companies if k.lower() == name.lower()), name)
        existing = companies.get(key)

        proposed = {
            "adjust": _clamp(float(entry.get("adjust", 0) or 0)),
            "tags": list(entry.get("tags") or []),
            "note": entry.get("note", ""),
            "source": entry.get("source", ""),
            "updated": entry.get("updated") or owner_today(),
        }
        if proposed["adjust"] != float(entry.get("adjust", 0) or 0):
            notes.append(f"- `{key}`: adjust clamped to {proposed['adjust']:+g} (±{INTEL_ADJUST_MAX:g} ceiling)")

        if existing and _is_owners(existing):
            reason = _weakens(existing, proposed)
            if reason:
                refused.append(entry)
                notes.append(f"- **REFUSED `{key}`** — {reason}. The owner's entry stands; left "
                             "pending for the owner.")
                continue
            notes.append(f"- `{key}`: strengthened one of the owner's entries (allowed — new evidence)")

        companies[key] = proposed
        notes.append(f"- `{key}`: {'updated' if existing else 'added'} {proposed['adjust']:+g} "
                     f"[{', '.join(proposed['tags']) or 'untagged'}]")

    return ledger, notes, refused


# ---------------------------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------------------------

def load_pending(path: Path) -> list[dict]:
    """Read the append-only queue. Tolerant by design: one malformed line must not strand every other
    proposal behind it, so bad lines are skipped rather than fatal."""
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    except OSError:
        return []
    return out


def load_tracked(path: Path) -> dict:
    """Read the curated ledger. A MISSING file is a fresh ledger; a CORRUPT one is not — promoting on
    top of a file we failed to parse would silently drop every company already in it, including the
    owner's. Fail loudly instead."""
    if not path.exists():
        return {"companies": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))  # deliberately unguarded — see docstring
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a JSON object")
    raw.setdefault("companies", {})
    return raw


def dump_ledger(ledger: dict) -> str:
    """Serialize in the file's OWN shape: 2-space indent, `_comment` preserved. `register_workup.py`
    writes indent=1; adopting that here would reformat the whole file into an unreviewable diff on the
    first promotion, which defeats the point of routing this through a reviewable PR."""
    return json.dumps(ledger, ensure_ascii=False, indent=2) + "\n"


def dedupe_pending(pending: list[dict]) -> list[dict]:
    """Collapse the queue to the LAST proposal per company (case-insensitive), preserving order.

    Promotion must act on the last word for a company, not the first: if a queue holds Acme -5 then
    Acme -9 (a later work-up saw more), promoting the -5 would REVERT Acme once the -9 had already
    landed. Make it explicit here so the caller never promotes a superseded value.
    """
    by_company: dict[str, dict] = {}
    for entry in pending:
        name = str(entry.get("company", "")).strip().lower()
        if name:
            by_company[name] = entry  # later line for the same company wins
    return list(by_company.values())


def _landed(tracked: dict, entry: dict) -> bool:
    """Is this pending entry's content ALREADY in the ledger — i.e. a prior promotion merged?

    Matched on company + source + clamped adjust. A promoted entry lands with its own source and its
    clamped value, so it matches and can be pruned. A refused-weakening entry never lands (the ledger
    keeps the OWNER's source and value), so it correctly does NOT match and stays queued for the
    owner's decision. Ledger entries don't carry ``recorded_at`` (it's queue metadata ``merge_intel``
    never copies), which is why the match is on content rather than on the recording instant.
    """
    companies = tracked.get("companies") or {}
    name = str(entry.get("company", "")).strip().lower()
    key = next((k for k in companies if k.lower() == name), None)
    if key is None:
        return False
    t = companies[key]
    try:
        return (str(t.get("source", "")) == str(entry.get("source", ""))
                and float(t.get("adjust", 0)) == _clamp(float(entry.get("adjust", 0) or 0)))
    except (TypeError, ValueError):
        return False


def prune_landed(path: Path, tracked: dict) -> int:
    """Drop queue lines whose content is now in the ledger — the post-merge cleanup, run at the START
    of every invocation rather than after opening a PR.

    This is what makes the whole flow self-healing. Pruning at PR-OPEN time would mean a PR that went
    red or was held lost its entries from the queue while never landing them in the ledger. Pruning
    what's CONFIRMED landed instead means: a merged promotion's entries are dropped next run; a
    held/red one's entries stay pending and get re-promoted until they land; a refused one stays
    pending for the owner indefinitely. Nothing is ever lost by a PR that didn't merge.
    """
    if not path.exists():
        return 0
    kept: list[str] = []
    removed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except ValueError:
            kept.append(line)  # keep unparseable lines rather than silently eating them
            continue
        if isinstance(obj, dict) and _landed(tracked, obj):
            removed += 1
            continue
        kept.append(line)
    if not removed:
        return 0  # nothing to prune — leave the file (and its mtime) exactly as it was
    # Atomic: this is the queue of intel Proteus has proposed but not yet landed. Truncating it on a
    # failed write would drop every pending proposal with nothing to rebuild them from.
    # `allow_empty` because this is the one legitimate drain-to-empty here: every queued proposal
    # landing in the ledger is the SUCCESS case, not a producer that failed, and `write_text` refuses
    # a zero-byte payload over a non-empty file otherwise. The flag is deliberate and must not be
    # copied to a caller that merely *might* come up empty — there, the refusal is the point.
    write_text(str(path), ("\n".join(kept) + "\n") if kept else "", allow_empty=True)
    return removed


# ---------------------------------------------------------------------------------------------
# git / gh — behind a seam so the merge logic above stays testable without either
# ---------------------------------------------------------------------------------------------

class Runner:
    """Thin subprocess seam so tests can assert the command sequence without spawning git."""

    def __init__(self, cwd: str | None = None):
        self.cwd = cwd

    def run(self, args: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(args, cwd=cwd or self.cwd, check=check,
                              capture_output=True, text=True)


def _repo_root() -> Path:
    return COMPANY_INTEL_FILE.parents[2]  # archons/proteus/company-intel.json -> repo root


# ---------------------------------------------------------------------------------------------
# Dream's step ledger
# ---------------------------------------------------------------------------------------------

def _live_state_dir() -> Path:
    """The assistant's LIVE state dir — the same directory ``dream_steps.py status`` reads.

    Resolved through ``seneschal/scripts/paths.py`` (``SENESCHAL_STATE_DIR``, else the repo's
    ``seneschal/state``), never hardcoded: this script runs from a scheduled Dream in the daemon's
    checkout, and a stamp written to the wrong ``state/`` is *worse than no stamp at all* — ``status``
    keeps reporting ``never`` while a phantom ledger quietly accumulates somewhere nobody looks.
    """
    try:
        import paths
        return Path(paths.state_dir())
    except Exception:  # noqa: BLE001 — fall back to the repo-relative default
        return _repo_root() / "seneschal" / "state"


def stamp_dream_step(step: str, state_dir=None) -> None:
    """Record that a Dream step actually ran (``seneschal/scripts/dream_steps.py``).

    Same contract as ``rag_index.stamp_dream_step``: **lazy import, swallowed whole.** Bookkeeping
    that cannot import must never stop the work it was measuring — a failed stamp costs the row,
    never the promotion.

    Two things this script has to get right that the in-tree owners don't, both because it lives in
    ``archons/proteus/tools/`` rather than ``seneschal/scripts/``:

    1. ``dream_steps`` is importable only because ``proteus_paths`` put ``seneschal/scripts`` on
       ``sys.path`` — and the import is inside the ``try`` deliberately, so a moved/renamed/absent
       module is a silent no-op rather than an ``ImportError`` out of a promotion that already
       opened its PR.
    2. The state dir is the *live* one above, not this script's neighbour.

    ``dream_steps.STEPS`` declares this script as 2e's owner, and an owner is what makes a step
    nudgeable — so an owner that never calls ``record`` reads ``never`` while running correctly and
    nudges the owner about a step that works. A false alarm in the smoke detector trains exactly the
    deafness the ledger exists to cure.
    """
    try:
        import dream_steps
        dream_steps.record(str(Path(state_dir) if state_dir else _live_state_dir()), step)
    except Exception:  # noqa: BLE001 — see the docstring; this must never fail a promotion
        pass


def write_local(path: Path, ledger: dict) -> None:
    """DEFAULT path: atomically replace the local, gitignored ledger. No git of any kind.

    Atomic (``proteus_paths.write_text``): a failed write leaves the previous ledger intact rather
    than truncating the owner's company memory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(str(path), dump_ledger(ledger))


def promote(runner: Runner, ledger: dict, notes: list[str], count: int, log=print,
            base: str = DEFAULT_BASE) -> str | None:
    """OPT-IN (``--via-pr``): build the change in a TRANSIENT worktree and open a PR. Returns the PR
    url, or None.

    **WARNING: this publishes ``company-intel.json`` — the owner's private company judgements — to
    whatever remote the checkout pushes to.** The file is gitignored, so it is staged with
    ``git add -f``; use this only from a PRIVATE fork. The default path is ``write_local``.

    Never touches the live checkout: the daemon runs off its own branch and a stray `git checkout`
    there is exactly what this whole split exists to avoid.
    """
    root = _repo_root()
    date = owner_today()
    branch = f"proteus/intel-{date}"
    tmp = tempfile.mkdtemp(prefix="intel-promote-")

    try:
        runner.run(["git", *GIT_FLAGS, "fetch", "origin", base], cwd=str(root))
        runner.run(["git", *GIT_FLAGS, "worktree", "add", "-b", branch, tmp, f"origin/{base}"],
                   cwd=str(root))

        rel = COMPANY_INTEL_FILE.relative_to(root)
        target = Path(tmp) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump_ledger(ledger), encoding="utf-8")

        # ONLY this file, never `add -A`; `-f` because the ledger is gitignored (see the WARNING above).
        runner.run(["git", *GIT_FLAGS, "add", "-f", str(rel)], cwd=tmp)
        body = "\n".join(notes)
        msg = sign_commit(
            f"chore(proteus): promote {count} company-intel finding(s)\n\n{body}\n\n"
            "Promoted from state/company-intel-pending.jsonl by promote_intel.py. Recording only —\n"
            "this does not apply anything; the scorer already saw these via the pending overlay.\n")
        runner.run(["git", *GIT_FLAGS, "commit", "-m", msg], cwd=tmp)
        runner.run(["git", *GIT_FLAGS, "push", "-u", "origin", branch], cwd=tmp)

        pr_body = (f"Promotes {count} company-intel finding(s) from Proteus's pending queue into the "
                   f"curated ledger.\n\n{body}\n\n"
                   "Recording only — the scorer already applied these via the pending overlay, so this "
                   "changes no behaviour.")
        out = runner.run(["gh", "pr", "create", "--base", base,
                          "--title", f"chore(proteus): promote {count} company-intel finding(s)",
                          "--body", pr_body], cwd=tmp)
        url = (out.stdout or "").strip().splitlines()[-1] if out.stdout else None
        log(f"promote_intel: opened {url}")
        return url
    finally:
        try:
            runner.run(["git", *GIT_FLAGS, "worktree", "remove", "--force", tmp], cwd=str(root),
                       check=False)
        except Exception:  # noqa: BLE001 — a leaked temp worktree must never fail the promotion
            pass


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Promote pending company intel into the curated ledger.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="show the merge + refusals, change nothing")
    mode.add_argument("--apply", action="store_true",
                      help="merge into the local (gitignored) ledger; no git")
    ap.add_argument("--via-pr", action="store_true",
                    help="with --apply: PRIVATE FORKS ONLY — commit the ledger (git add -f) in a "
                         "transient worktree and open a PR. PUBLISHES company-intel.json to the "
                         "checkout's remote")
    ap.add_argument("--pending", default=None, help="override the pending queue path (tests)")
    ap.add_argument("--tracked", default=None, help="override the curated ledger path (tests)")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"branch the promotion PR targets (default: {DEFAULT_BASE})")
    ap.add_argument("--state-dir", default=None,
                    help="override the assistant's state dir for the Dream-step stamp (tests). "
                         "Defaults to the LIVE state dir (paths.state_dir()).")
    args = ap.parse_args(argv)
    if args.via_pr and not args.apply:
        ap.error("--via-pr only makes sense with --apply")

    pending_path = Path(args.pending) if args.pending else INTEL_PENDING_FILE
    tracked_path = Path(args.tracked) if args.tracked else COMPANY_INTEL_FILE

    try:
        tracked = load_tracked(tracked_path)
    except (OSError, ValueError) as exc:
        print(f"promote_intel: REFUSING — curated ledger unreadable ({exc}). "
              f"Promoting over a file we can't parse would drop every company already in it.",
              file=sys.stderr)
        return 1

    # Post-merge cleanup FIRST: drop anything a prior promotion already landed in the ledger.
    # Self-healing — a red/held PR keeps its entries queued; only a merged one clears them (see
    # prune_landed). On --dry-run we leave the queue untouched.
    if not args.dry_run:
        cleared = prune_landed(pending_path, tracked)
        if cleared:
            print(f"promote_intel: pruned {cleared} already-landed entr(y/ies) from the queue")

    pending = dedupe_pending(load_pending(pending_path))
    if not pending:
        print("promote_intel: nothing pending.")
        # 2e RAN. It is stamped on an empty queue too, and that is the point rather than a
        # loophole: "nothing pending" is most Dream nights, so a ledger that only moved when work
        # was promoted would read stale on every quiet night and nudge the owner about a step that did
        # exactly what it should. The ledger measures whether the step HAPPENED, not whether it had
        # work.
        if not args.dry_run:
            stamp_dream_step("2e", args.state_dir)
        return 0

    ledger, notes, refused = merge_intel(tracked, pending)
    applied = [e for e in pending if e not in refused]

    print(f"promote_intel: {len(applied)} to promote, {len(refused)} refused")
    for n in notes:
        print(f"  {n}")

    if args.dry_run:
        print("--- resulting ledger ---")
        print(dump_ledger(ledger))
        return 0  # deliberately NO stamp: a dry run changed nothing, so 2e did not run

    if not applied:
        print("promote_intel: everything was refused — nothing promoted.")
        stamp_dream_step("2e", args.state_dir)
        return 0

    if not args.via_pr:
        # DEFAULT: merge into the local, gitignored ledger — no git, no commit, no PR. The merged
        # entries have now landed, so prune them at once (refused ones don't match and stay queued).
        write_local(tracked_path, ledger)
        prune_landed(pending_path, ledger)
        print(f"promote_intel: merged {len(applied)} finding(s) into {tracked_path.name} (local)")
        stamp_dream_step("2e", args.state_dir)
        return 0

    # --via-pr (PRIVATE FORKS ONLY — publishes the ledger to the checkout's remote). No prune here:
    # the queue is pruned only by prune_landed above, once a promotion has actually merged into the
    # ledger — so a PR that never merges can never strand its entries.
    promote(Runner(), ledger, notes, len(applied), base=args.base)
    # AFTER `promote`, never before: `Runner.run` uses `check=True`, so a failed git/gh raises
    # straight past this and the row correctly stays where it was. Only a successful run stamps.
    stamp_dream_step("2e", args.state_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
