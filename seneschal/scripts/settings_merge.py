#!/usr/bin/env python3
"""Merge the session-registry hooks into the USER's ``~/.claude/settings.json``.

The machine-wide session hooks (``SCHEDULING.md`` §5 — "Session registry hooks") live in the
owner's personal ``~/.claude/settings.json``, never in this repo's shipped settings (a
repo-shipped hook would impose itself on every install and double-fire beside the user copy).
The wizard's ``daemon`` chapter drives this tool to wire them; the manual path in
SCHEDULING.md stays authoritative for what the entries mean.

What one run ensures (and nothing more):

  * each of the four hook events — ``SessionStart`` / ``UserPromptSubmit`` / ``Stop`` /
    ``SessionEnd`` — contains an entry invoking ``python <repo>/seneschal/scripts/
    session_stamp.py`` with ``timeout: 10``, in exactly the shape SCHEDULING.md §5 documents:
    ``{"hooks": [{"type": "command", "command": "python .../session_stamp.py", "timeout": 10}]}``;
  * the ``env`` block carries ``PYTHONUTF8=1`` and ``PYTHONIOENCODING=utf-8`` (added only
    when absent — an existing value, whatever it is, is never clobbered);
  * everything else in the file — other hooks, unknown keys, ordering — survives untouched.

Contracts:
  * **Dry-run by default.** ``--dry-run`` (the default) prints a unified diff of what would
    change and writes nothing; only ``--apply`` writes, and an apply first backs the file up
    to ``settings.json.bak-<utc-stamp>`` then writes atomically (same-dir temp + os.replace).
  * **Append, never remove/reorder.** Existing hook groups are left exactly where they are;
    a missing stamp entry is appended to the event's array. Idempotent: a second run
    produces no diff.
  * **Existing session_stamp.py entries are detected by substring** — including one pointing
    at a *different* checkout. A foreign entry is reported and left alone (two checkouts'
    hooks both firing is the owner's call, not this tool's) unless ``--force-path`` repoints
    its command at this repo in place.
  * **A corrupt settings.json is refused** with a clear message — this tool never "fixes" a
    file it cannot parse (exit 2). An absent file is fine (fresh install: it gets created).
  * Stdlib only; identity-neutral; no secrets anywhere near this file.

CLI::

    python seneschal/scripts/settings_merge.py [--dry-run | --apply] [--force-path]
                                               [--settings F] [--repo DIR]
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
STAMP_MARKER = "session_stamp.py"
HOOK_TIMEOUT = 10
ENV_DEFAULTS = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


class SettingsMergeError(Exception):
    """A condition this tool refuses to push through (corrupt file, unmergeable shape)."""


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def expected_command(repo: Path) -> str:
    """The documented hook command, forward-slashed (works everywhere, incl. Windows)."""
    script = (Path(repo) / "seneschal" / "scripts" / STAMP_MARKER)
    return "python " + str(script).replace("\\", "/")


def _norm(s: str) -> str:
    """Comparison form for command strings: forward slashes, casefolded (Windows paths are
    case-insensitive; on POSIX a same-letters-different-case second checkout would be read
    as this one — vanishingly rare, and the failure mode is merely 'no duplicate appended')."""
    return s.replace("\\", "/").casefold()


def _iter_command_items(event_groups):
    """Yield every {"type": "command", ...} hook item dict in an event's group list."""
    for group in event_groups:
        if not isinstance(group, dict):
            continue
        inner = group.get("hooks")
        if not isinstance(inner, list):
            continue
        for item in inner:
            if isinstance(item, dict) and isinstance(item.get("command"), str):
                yield item


def load_settings(path: Path) -> dict:
    """Read the settings file. Absent -> {}. Corrupt / non-object -> SettingsMergeError."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SettingsMergeError(
            f"{path} is not valid JSON ({exc}). Refusing to touch a file I can't parse - "
            "fix or remove it, then re-run."
        )
    if not isinstance(data, dict):
        raise SettingsMergeError(
            f"{path} is not a JSON object. Refusing to touch a file with a shape I don't "
            "understand."
        )
    return data


def merge(settings: dict, repo: Path, force_path: bool = False) -> tuple[dict, list[str]]:
    """Pure merge: returns (new_settings, notes). Never mutates the input."""
    out = copy.deepcopy(settings)
    notes: list[str] = []
    want_cmd = expected_command(repo)
    want_script = _norm(want_cmd.split(" ", 1)[1])  # the script path, comparison form

    env = out.setdefault("env", {})
    if not isinstance(env, dict):
        raise SettingsMergeError('the "env" block is not a JSON object - refusing to modify it.')
    for key, value in ENV_DEFAULTS.items():
        if key not in env:
            env[key] = value
        elif str(env[key]) != value:
            notes.append(f'note: env {key} is already set to {env[key]!r} - left as-is.')

    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SettingsMergeError('the "hooks" block is not a JSON object - refusing to modify it.')

    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise SettingsMergeError(
                f'hooks.{event} is not a list - refusing to modify a shape I don\'t understand.'
            )
        ours, foreign = [], []
        for item in _iter_command_items(groups):
            if STAMP_MARKER not in item["command"]:
                continue
            (ours if want_script in _norm(item["command"]) else foreign).append(item)
        if ours:
            continue  # already wired to this checkout - idempotent no-op
        if foreign:
            for item in foreign:
                if force_path:
                    notes.append(
                        f"{event}: repointed the existing session_stamp.py hook at this "
                        f"checkout (was: {item['command']})"
                    )
                    item["command"] = want_cmd
                    item.setdefault("timeout", HOOK_TIMEOUT)
                else:
                    notes.append(
                        f"{event}: an existing session_stamp.py hook points at a DIFFERENT "
                        f"checkout ({item['command']}) - left unchanged. Re-run with "
                        "--force-path to repoint it here."
                    )
            continue  # never append a second stamp entry beside a foreign one
        groups.append(
            {"hooks": [{"type": "command", "command": want_cmd, "timeout": HOOK_TIMEOUT}]}
        )
    return out, notes


def render(settings: dict) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def unified_diff(old_text: str, new_text: str, path: Path) -> str:
    lines = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=str(path),
        tofile=f"{path} (proposed)",
    )
    return "".join(lines)


def backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return path.with_name(path.name + f".bak-{stamp}")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Merge the session_stamp.py hooks into ~/.claude/settings.json "
                    "(SCHEDULING.md section 5 shape). Dry-run by default."
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the diff, write nothing (default)")
    mode.add_argument("--apply", action="store_true", help="back up + write the merged settings")
    p.add_argument("--force-path", action="store_true",
                   help="repoint an existing session_stamp.py hook from another checkout at this repo")
    p.add_argument("--settings", default=None, help="settings.json path override (tests)")
    p.add_argument("--repo", default=None, help="repo root override (default: this checkout)")
    args = p.parse_args(argv[1:])

    path = Path(args.settings) if args.settings else default_settings_path()
    repo = Path(args.repo).resolve() if args.repo else REPO_ROOT

    try:
        settings = load_settings(path)
        merged, notes = merge(settings, repo, force_path=args.force_path)
    except SettingsMergeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    existed = path.is_file()
    old_text = path.read_text(encoding="utf-8") if existed else ""
    new_text = render(merged)

    for note in notes:
        print(note)

    if old_text == new_text:
        print(f"{path}: already up to date - no changes.")
        return 0

    diff = unified_diff(old_text, new_text, path)
    if not args.apply:
        print(diff, end="" if diff.endswith("\n") else "\n")
        print("(dry run - nothing written. Re-run with --apply to write.)")
        return 0

    if existed:
        bak = backup_path(path)
        bak.write_bytes(path.read_bytes())
        print(f"backed up to {bak}")
    atomic_write(path, new_text)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
