#!/usr/bin/env python3
"""The /setup wizard's resumability ledger — ``seneschal/state/setup-state.json``.

The unified install wizard walks *chapters* (persona, store, ``env:<id>`` per
env-manifest entry, ``mcp:<id>``, cockpit, ...). This module is the deterministic
substrate that remembers where the walk stands, so a crashed / restarted / partial
setup resumes instead of restarting. Stdlib only; the file is gitignored with the
rest of ``seneschal/state/``.

Schema (version 1)::

    {
      "version": 1,
      "platform": "windows",                # platform.system().lower() at first write
      "started_at": "2026-01-01T00:00:00Z", # first ledger write (UTC)
      "updated_at": "2026-01-01T00:00:00Z", # bumped on every save (UTC)
      "chapters": {
        "<chapter id>": {                   # e.g. "persona", "store", "env:telegram", "mcp:calendar"
          "status": "pending",              # see STATUSES below
          "completed_at": "...",            # only while status == "done"
          "answers_hash": "sha256:<hex16>", # optional: sha256 over the concatenated artifact bytes
          "artifacts": ["path", ...],       # repo-root-relative files this chapter produced
          "summary": "...",                 # one human line for the board
          "step": "...",                    # optional resume cursor inside an in-progress chapter
          "inferred": true                  # present only when `infer` promoted the status
        }
      },
      "features": {},                       # wizard-owned toggles   (W2 writes these)
      "deps": {},                           # wizard-owned dep facts (W2 writes these)
      "models": {}                          # wizard-owned model facts (W2 writes these)
    }

Statuses: ``pending`` | ``in-progress`` | ``done`` | ``declined`` | ``blocked`` |
``awaiting-auth-restart`` | ``stale``.

Rules:
  * ``load()`` NEVER raises — an absent file yields a fresh structure; a corrupt one
    is saved aside as ``setup-state.json.bak`` (best-effort) and replaced fresh.
  * Writes are atomic (temp file in the same directory + ``os.replace``).
  * **No secret values are ever stored.** The ledger only accepts the whitelisted
    fields above (status/summary/step/artifacts/hash) — summaries are prose, artifacts
    are paths, hashes are digests. There is deliberately no free-form payload field.
  * ``infer`` reconciles the ledger with reality from artifact presence:
      - recorded (or known) artifacts all exist while status is ``pending``
        -> ``done`` + ``"inferred": true`` (a re-run of /setup on a machine that was
        set up before the ledger existed self-heals);
      - status ``done`` but artifacts missing, or ``answers_hash`` no longer matching
        -> ``stale`` (the wizard re-verifies that chapter).
    Other statuses (in-progress/declined/blocked/awaiting-auth-restart/stale) are
    never touched by ``infer``. Known-chapter artifact rules come FROM the env
    manifest (``seneschal/setup/env-manifest.json``: chapter ``env:<id>`` -> that
    entry's ``path``) plus two built-ins: ``persona`` -> persona/identity.json +
    persona/persona.md, and ``store`` -> seneschal/store/config.json.

CLI::

    python setup_state.py [--state-file F] [--root DIR] [--manifest M] board
    python setup_state.py ... mark <chapter> <status> [--summary S] [--step S]
                                [--artifacts p1,p2] [--hash-artifacts]
    python setup_state.py ... get <chapter>
    python setup_state.py ... infer
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as _platform
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_STATE = REPO_ROOT / "seneschal" / "state" / "setup-state.json"
DEFAULT_MANIFEST = REPO_ROOT / "seneschal" / "setup" / "env-manifest.json"

STATUSES = (
    "pending",
    "in-progress",
    "done",
    "declined",
    "blocked",
    "awaiting-auth-restart",
    "stale",
)

# ASCII glyphs on purpose — Windows consoles with legacy codepages must render the board.
GLYPHS = {
    "pending": "[ ]",
    "in-progress": "[~]",
    "done": "[x]",
    "declined": "[-]",
    "blocked": "[!]",
    "awaiting-auth-restart": "[R]",
    "stale": "[?]",
}

# Built-in artifact rules for the non-env chapters. env:<id> rules are read from the
# manifest (single source of truth for env file paths) — see known_artifacts().
BUILTIN_ARTIFACTS = {
    "persona": ["persona/identity.json", "persona/persona.md"],
    "store": ["seneschal/store/config.json"],
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fresh() -> dict:
    now = _now()
    return {
        "version": 1,
        "platform": _platform.system().lower() or "unknown",
        "started_at": now,
        "updated_at": now,
        "chapters": {},
        "features": {},
        "deps": {},
        "models": {},
    }


def load(path: Path | str = DEFAULT_STATE) -> dict:
    """Read the ledger. Never raises: absent -> fresh; corrupt -> saved aside + fresh."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return fresh()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("chapters"), dict):
            raise ValueError("not a setup-state object")
    except (ValueError, TypeError):
        try:  # keep the evidence, best-effort — never let the salvage itself raise
            path.replace(path.with_name(path.name + ".bak"))
        except OSError:
            pass
        return fresh()
    # Backfill any missing top-level containers so callers can rely on the shape.
    base = fresh()
    for key, default in base.items():
        data.setdefault(key, default)
    return data


def save(state: dict, path: Path | str = DEFAULT_STATE) -> None:
    """Atomic write: temp file in the same directory, then os.replace."""
    path = Path(path)
    state["updated_at"] = _now()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def hash_artifacts(artifacts: list[str], root: Path | str = REPO_ROOT) -> str:
    """sha256 over the concatenated artifact bytes (in list order) -> ``sha256:<hex16>``."""
    root = Path(root)
    h = hashlib.sha256()
    for rel in artifacts:
        h.update(Path(root, rel).read_bytes())
    return "sha256:" + h.hexdigest()[:16]


def mark(
    state: dict,
    chapter: str,
    status: str,
    summary: str | None = None,
    step: str | None = None,
    artifacts: list[str] | None = None,
    answers_hash: str | None = None,
) -> dict:
    """Set a chapter's status (+ optional whitelisted fields). Returns the chapter record."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r} (choose from: {', '.join(STATUSES)})")
    if not chapter:
        raise ValueError("chapter id must be non-empty")
    ch = state["chapters"].setdefault(chapter, {"status": "pending"})
    ch["status"] = status
    if summary is not None:
        ch["summary"] = summary
    if artifacts is not None:
        ch["artifacts"] = list(artifacts)
    if answers_hash is not None:
        ch["answers_hash"] = answers_hash
    if step is not None:
        ch["step"] = step
    elif status == "done":
        ch.pop("step", None)  # a finished chapter has no resume cursor
    if status == "done":
        ch["completed_at"] = _now()
    else:
        ch.pop("completed_at", None)
    ch.pop("inferred", None)  # an explicit mark supersedes any inferred status
    return ch


def known_artifacts(manifest_path: Path | str = DEFAULT_MANIFEST) -> dict[str, list[str]]:
    """Chapter -> artifact-path rules: built-ins + one ``env:<id>`` rule per manifest entry.

    Reading the manifest keeps the env-file paths in ONE place; a missing/corrupt
    manifest just means only the built-in rules apply (infer still reconciles any
    artifacts recorded in the ledger itself).
    """
    rules = {k: list(v) for k, v in BUILTIN_ARTIFACTS.items()}
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        for entry in data.get("entries", []):
            eid, path = entry.get("id"), entry.get("path")
            if eid and path:
                rules[f"env:{eid}"] = [path]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return rules


def infer(
    state: dict,
    root: Path | str = REPO_ROOT,
    manifest_path: Path | str = DEFAULT_MANIFEST,
) -> list[str]:
    """Artifact-presence reconciliation (see module docstring). Returns change lines."""
    root = Path(root)
    rules = known_artifacts(manifest_path)
    changes: list[str] = []
    for chapter in sorted(set(state["chapters"]) | set(rules)):
        ch = state["chapters"].get(chapter)
        status = ch.get("status", "pending") if ch else "pending"
        artifacts = (ch or {}).get("artifacts") or rules.get(chapter) or []
        if not artifacts:
            continue
        all_exist = all(Path(root, rel).is_file() for rel in artifacts)
        if status == "pending" and all_exist:
            ch = state["chapters"].setdefault(chapter, {})
            ch.update(
                {
                    "status": "done",
                    "inferred": True,
                    "artifacts": list(artifacts),
                    "completed_at": _now(),
                }
            )
            changes.append(f"{chapter}: pending -> done (inferred; artifacts present)")
        elif status == "done":
            if not all_exist:
                ch["status"] = "stale"
                ch.pop("completed_at", None)
                changes.append(f"{chapter}: done -> stale (artifacts missing)")
            elif ch.get("answers_hash"):
                if hash_artifacts(artifacts, root) != ch["answers_hash"]:
                    ch["status"] = "stale"
                    ch.pop("completed_at", None)
                    changes.append(f"{chapter}: done -> stale (artifact hash mismatch)")
    return changes


def board_lines(state: dict) -> list[str]:
    """The aligned human table: chapter, status glyph, summary."""
    chapters = state.get("chapters", {})
    if not chapters:
        return ["(no chapters recorded yet — run `infer`, or `mark` one)"]
    rows = []
    for chapter, ch in chapters.items():
        status = ch.get("status", "pending")
        glyph = GLYPHS.get(status, "[?]")
        rows.append((chapter, f"{glyph} {status}", ch.get("summary", "")))
    w1 = max(len(r[0]) for r in rows)
    w2 = max(len(r[1]) for r in rows)
    return [f"{c:<{w1}}  {s:<{w2}}  {m}".rstrip() for c, s, m in rows]


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="The /setup wizard's resumability ledger.")
    p.add_argument("--state-file", default=str(DEFAULT_STATE), help="ledger path (default: seneschal/state/setup-state.json)")
    p.add_argument("--root", default=str(REPO_ROOT), help="repo root artifacts resolve against")
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="env manifest for known-chapter artifact rules")
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("mark", help="set a chapter's status")
    pm.add_argument("chapter")
    pm.add_argument("status", choices=STATUSES)
    pm.add_argument("--summary", help="one human line for the board")
    pm.add_argument("--step", help="resume cursor inside an in-progress chapter")
    pm.add_argument("--artifacts", help="comma-separated repo-root-relative paths this chapter produced")
    pm.add_argument(
        "--hash-artifacts",
        action="store_true",
        help="store sha256:<hex16> over the concatenated artifact bytes (uses --artifacts, else the recorded list)",
    )

    pg = sub.add_parser("get", help="print one chapter record as JSON")
    pg.add_argument("chapter")

    sub.add_parser("board", help="print the aligned chapter/status/summary table")
    sub.add_parser("infer", help="reconcile statuses from artifact presence")

    args = p.parse_args(argv[1:])
    state_file, root = Path(args.state_file), Path(args.root)
    state = load(state_file)

    if args.cmd == "mark":
        artifacts = [a.strip() for a in args.artifacts.split(",") if a.strip()] if args.artifacts else None
        answers_hash = None
        if args.hash_artifacts:
            to_hash = artifacts or state["chapters"].get(args.chapter, {}).get("artifacts")
            if not to_hash:
                print("error: --hash-artifacts needs --artifacts (or a previously recorded artifact list)", file=sys.stderr)
                return 2
            try:
                answers_hash = hash_artifacts(to_hash, root)
            except OSError as exc:
                print(f"error: cannot hash artifacts: {exc}", file=sys.stderr)
                return 2
        try:
            ch = mark(state, args.chapter, args.status, args.summary, args.step, artifacts, answers_hash)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        save(state, state_file)
        print(json.dumps({"ok": True, "chapter": args.chapter, "status": ch["status"]}))
        return 0

    if args.cmd == "get":
        ch = state["chapters"].get(args.chapter, {"status": "pending"})
        print(json.dumps(ch, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "board":
        for line in board_lines(state):
            print(line)
        return 0

    if args.cmd == "infer":
        changes = infer(state, root, Path(args.manifest))
        if changes:
            save(state, state_file)
            for line in changes:
                print(line)
        else:
            print("(no changes)")
        return 0

    return 2  # unreachable — subparsers are required


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
