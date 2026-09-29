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

**Optional guard hooks** (``--guard NAME``, repeatable; ``--guard all``). The PR/shell guards are
``PreToolUse`` / ``PostToolUse(Failure)`` hooks the owner may opt into; each ``*_SETUP.md`` guide
documents its block and this tool writes exactly that block, under the same contracts below
(diff-first, append-only, idempotent, foreign-checkout entries reported and left alone unless
``--force-path``). :data:`GUARDS` is the table:

  * ``bash-path``     — ``PreToolUse`` ``Bash`` → ``bash_path_guard.py`` (BASH_PATH_GUARD_SETUP.md)
  * ``script-file``   — ``PreToolUse`` ``Bash|PowerShell`` → ``script_file_guard.py``
  * ``merge``         — ``PreToolUse`` ``Bash|PowerShell`` → ``merge_guard.py``, plus
    ``PostToolUseFailure`` → ``merge_guard.py --post-tool-use`` (MERGE_GUARD_SETUP.md)
  * ``branch-delete`` — ``PreToolUse`` ``Bash|PowerShell`` → ``branch_delete_guard.py``
  * ``query-shape``   — ``PostToolUse`` on the Notion query tool → ``query_shape_hook.py``.
    **Notion backend only**, so ``all`` does not include it; name it explicitly.
  * ``instructions-loaded`` — ``InstructionsLoaded`` (no matcher) → ``instructions_loaded.py``, the
    sub-router load logger (its own module docstring is the guide). Observability, not a guard,
    so ``all`` does not include it either; name it explicitly.

None of them is installed unless named: the guards refuse commands, and refusing is the owner's
call; the logger fires on every session on the machine, which is the owner's call too.

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
                                               [--guard NAME ...] [--settings F] [--repo DIR]
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

#: The optional guard hooks: name -> [(event, matcher, script, extra args, timeout)]. Each row is
#: the block its ``*_SETUP.md`` documents; a test pins the two against each other. A ``None``
#: matcher writes a matcher-less group (an event with no tool to match, e.g. InstructionsLoaded).
GUARDS = {
    "bash-path": [("PreToolUse", "Bash", "bash_path_guard.py", "", 10)],
    "script-file": [("PreToolUse", "Bash|PowerShell", "script_file_guard.py", "", 10)],
    "merge": [("PreToolUse", "Bash|PowerShell", "merge_guard.py", "", 90),
              ("PostToolUseFailure", "Bash|PowerShell", "merge_guard.py", " --post-tool-use", 90)],
    "branch-delete": [("PreToolUse", "Bash|PowerShell", "branch_delete_guard.py", "", 60)],
    "query-shape": [("PostToolUse", "mcp__notion__notion-query-data-sources",
                     "query_shape_hook.py", "", 10)],
    "instructions-loaded": [("InstructionsLoaded", None, "instructions_loaded.py", "", 10)],
}
#: What ``--guard all`` means: every guard except the Notion-only one (and the load logger,
#: which is observability rather than a guard).
ALL_GUARDS = ("bash-path", "script-file", "merge", "branch-delete")


class SettingsMergeError(Exception):
    """A condition this tool refuses to push through (corrupt file, unmergeable shape)."""


def claude_config_dir(environ=None, home=None) -> Path:
    """The directory Claude Code reads the user's `settings.json` from: `CLAUDE_CONFIG_DIR` when set
    (Claude Code relocates its whole user config there — a second profile, a clean test account),
    else `~/.claude`. Writing the hooks anywhere else would install them where the CLI never looks."""
    env = os.environ if environ is None else environ
    override = env.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return (Path(home) if home else Path.home()) / ".claude"


def default_settings_path() -> Path:
    return claude_config_dir() / "settings.json"


def expected_command(repo: Path, script_name: str = STAMP_MARKER) -> str:
    """The documented hook command, forward-slashed (works everywhere, incl. Windows)."""
    script = (Path(repo) / "seneschal" / "scripts" / script_name)
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


def resolve_guards(names) -> list[str]:
    """``["all"]`` / names -> the ordered, de-duplicated guard list. Unknown -> SettingsMergeError."""
    out: list[str] = []
    for name in names or ():
        for n in (ALL_GUARDS if name == "all" else (name,)):
            if n not in GUARDS:
                raise SettingsMergeError(
                    f"unknown guard {n!r} - choose from: all, {', '.join(sorted(GUARDS))}.")
            if n not in out:
                out.append(n)
    return out


def _merge_guard_rows(hooks: dict, repo: Path, guards, force_path: bool, notes: list) -> None:
    """Append each named guard's documented block where absent (same rules as the stamp hooks)."""
    for name in guards:
        for event, matcher, script, extra, timeout in GUARDS[name]:
            groups = hooks.setdefault(event, [])
            if not isinstance(groups, list):
                raise SettingsMergeError(
                    f"hooks.{event} is not a list - refusing to modify a shape I don't understand.")
            base_cmd = expected_command(repo, script)
            want_cmd = base_cmd + extra
            want_script = _norm(base_cmd.split(" ", 1)[1])
            ours, foreign = [], []
            for item in _iter_command_items(groups):
                if script not in item["command"]:
                    continue
                (ours if want_script in _norm(item["command"]) else foreign).append(item)
            if ours:
                continue
            if foreign:
                for item in foreign:
                    if force_path:
                        notes.append(f"{event}: repointed the existing {script} hook at this "
                                     f"checkout (was: {item['command']})")
                        item["command"] = want_cmd
                        item.setdefault("timeout", timeout)
                    else:
                        notes.append(f"{event}: an existing {script} hook points at a DIFFERENT "
                                     f"checkout ({item['command']}) - left unchanged. Re-run with "
                                     "--force-path to repoint it here.")
                continue
            group = {"matcher": matcher} if matcher is not None else {}
            group["hooks"] = [{"type": "command", "command": want_cmd, "timeout": timeout}]
            groups.append(group)


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


def merge(settings: dict, repo: Path, force_path: bool = False,
          guards=()) -> tuple[dict, list[str]]:
    """Pure merge: returns (new_settings, notes). Never mutates the input. ``guards`` names the
    optional guard hooks to add as well (see :data:`GUARDS`; resolve ``all`` first)."""
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
    _merge_guard_rows(hooks, repo, guards, force_path, notes)
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
    p.add_argument("--guard", action="append", default=[], metavar="NAME",
                   help="also add an optional guard hook (repeatable): "
                        + ", ".join(sorted(GUARDS))
                        + ", or all (every one but query-shape and instructions-loaded)")
    p.add_argument("--settings", default=None, help="settings.json path override (tests)")
    p.add_argument("--repo", default=None, help="repo root override (default: this checkout)")
    args = p.parse_args(argv[1:])

    path = Path(args.settings) if args.settings else default_settings_path()
    repo = Path(args.repo).resolve() if args.repo else REPO_ROOT

    try:
        settings = load_settings(path)
        merged, notes = merge(settings, repo, force_path=args.force_path,
                              guards=resolve_guards(args.guard))
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
