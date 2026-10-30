#!/usr/bin/env python3
"""Named Claude logins for `jobs.py start --account <name>` — which subscription a job bills to.

An install can hold more than one Claude subscription; without this, every job ran on whichever
login the launching process happened to inherit.

**An account is a Claude config dir.** The `claude` CLI keeps its whole identity — `.credentials.json`,
its own `.claude.json`, settings, history — under `CLAUDE_CONFIG_DIR` when that is set, and under
`~/.claude` when it is not. So naming an account is naming a directory, and running a job on it is setting one env var on the
job's child processes. Nothing else changes: `ANTHROPIC_API_KEY` stays scrubbed for every account
(`jobs.child_env`), so every account is subscription-billed.

**The registry** is `state/claude-accounts.json` (gitignored; seed `claude-accounts.example.json`):
`{"default": null, "2": "C:\\\\Users\\\\example\\\\.claude-2"}`. A `null` value — or the name `default`
with no registry at all — means the normal `~/.claude` login. `~` and `%VARS%`/`$VARS` are expanded.

**Refuse up front, one line naming the fix.** `resolve()` raises `AccountError` for an unknown name, a
missing/unreadable registry, a directory that does not exist, or one with no `.credentials.json` (the
login was never done). `jobs.py start` turns that into exit 2 with nothing written — the same kind of
answer as the spawn preflight: you asked for something that cannot work. The login itself is
host-side and no PR can do it: `CLAUDE_ACCOUNTS_SETUP.md`.

**Only the CREDENTIALS FILE is checked**, not whether the token is still valid — that would mean
spawning `claude` per start. An expired login fails inside the job and reports like any other
failure; the registry check exists to catch the typo and the never-logged-in directory, which would
otherwise start a job that silently runs on nothing.

**THE RAILS MUST TRAVEL WITH THE LOGIN — refused in code, not asked for in prose.** Claude Code reads
its hooks from `<config dir>/settings.json` and the owner's global instructions from
`<config dir>/CLAUDE.md`. A fresh second config dir has neither, so a job on it would run with no
`merge_guard.py` (it could `gh pr merge` unguarded), no `send_gate_hook.py`, no
`job_background_guard.py`, and none of the owner's standing rules (merge commits only, never merge
red CI).
So `resolve()` also refuses an account whose `settings.json` `hooks` block, or whose `CLAUDE.md`
bytes, differ from the default login's (`~/.claude`) — linking the two files is the setup step, and a
link that later breaks (an editor that saves by rename turns a hard link into a copy) is caught the
next time a job asks for that account. When the default login has no such file (a CI box), there is
nothing to match and nothing is refused.
"""
from __future__ import annotations

import json
import os

REGISTRY_FILE = "claude-accounts.json"
DEFAULT_NAME = "default"
CREDENTIALS_FILE = ".credentials.json"
SETTINGS_FILE = "settings.json"
INSTRUCTIONS_FILE = "CLAUDE.md"
SETUP_DOC = "seneschal/scripts/CLAUDE_ACCOUNTS_SETUP.md"


def default_config_dir() -> str:
    """The normal login's config dir — the reference every other account's rails must match. Always
    `~/.claude`, never this process's own `CLAUDE_CONFIG_DIR`: a launcher already running on account 2
    must not become the yardstick for account 2."""
    return os.path.join(os.path.expanduser("~"), ".claude")


def _hooks_of(path: str):
    """`(present, hooks)` for a settings.json — `hooks` is the parsed block or `None`."""
    if not os.path.isfile(path):
        return False, None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError):
        return True, "<unreadable>"
    return True, data.get("hooks") if isinstance(data, dict) else None


def _bytes_of(path: str):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def rails_problem(config_dir: str, reference_dir: str | None = None) -> str | None:
    """Why `config_dir` would run a job without the default login's hooks or global instructions, or
    `None` when it carries the same ones (or the default login has none to carry)."""
    ref = reference_dir or default_config_dir()
    if os.path.normcase(os.path.abspath(ref)) == os.path.normcase(os.path.abspath(config_dir)):
        return None
    ref_present, ref_hooks = _hooks_of(os.path.join(ref, SETTINGS_FILE))
    if ref_present and ref_hooks:
        _, hooks = _hooks_of(os.path.join(config_dir, SETTINGS_FILE))
        if hooks != ref_hooks:
            return (f"its {SETTINGS_FILE} does not carry the same hooks as {ref} — a job there would "
                    f"run without merge_guard/send_gate_hook/job_background_guard; link the file "
                    f"({SETUP_DOC} step 2)")
    ref_md = _bytes_of(os.path.join(ref, INSTRUCTIONS_FILE))
    if ref_md:
        if _bytes_of(os.path.join(config_dir, INSTRUCTIONS_FILE)) != ref_md:
            return (f"its {INSTRUCTIONS_FILE} differs from {ref}'s — a job there would run without "
                    f"the owner's global instructions; link the file ({SETUP_DOC} step 2)")
    return None


class AccountError(ValueError):
    """An account that cannot be used. The message is one line naming the fix."""


def registry_path(state_dir: str) -> str:
    return os.path.join(state_dir, REGISTRY_FILE)


def load_registry(state_dir: str) -> dict | None:
    """`{name: config_dir-or-None}`, or `None` when there is no registry file. Raises `AccountError`
    for a registry that exists but cannot be read — a broken registry must not quietly mean "no
    accounts", because then `--account 2` would read as a typo instead of as the real problem."""
    path = registry_path(state_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise AccountError(f"{path} is unreadable ({e.__class__.__name__}) — fix the JSON "
                           f"(see {SETUP_DOC})") from e
    if not isinstance(raw, dict):
        raise AccountError(f"{path} must be a JSON object of name -> config dir (see {SETUP_DOC})")
    out = {}
    for name, value in raw.items():
        if not isinstance(name, str) or name.startswith("_"):
            continue  # `_note`-style keys are for the human editing the file
        if value is not None and not isinstance(value, str):
            raise AccountError(f"{path}: account {name!r} must map to a directory path or null")
        out[name] = value
    return out


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(path)))


def resolve(state_dir: str, name: str, *, exists=os.path.isdir, isfile=os.path.isfile,
            reference_dir: str | None = None) -> dict:
    """`{"name": name, "config_dir": <abs path> | None}` for a usable account, else `AccountError`.

    `config_dir: None` means the normal `~/.claude` login — `jobs.child_env` then REMOVES any
    inherited `CLAUDE_CONFIG_DIR`, because naming an account is a claim about which login the job
    uses, and an inherited one would silently contradict it."""
    name = (name or "").strip()
    if not name:
        raise AccountError("--account needs a name (see `jobs.py start --help`)")
    registry = load_registry(state_dir)
    if registry is None:
        if name == DEFAULT_NAME:
            return {"name": name, "config_dir": None}
        raise AccountError(f"unknown account {name!r}: there is no {registry_path(state_dir)} yet — "
                           f"create it and log the account in ({SETUP_DOC})")
    if name not in registry:
        if name == DEFAULT_NAME:
            return {"name": name, "config_dir": None}
        known = ", ".join(sorted(registry)) or "none"
        raise AccountError(f"unknown account {name!r} (registered: {known}) — add it to "
                           f"{registry_path(state_dir)} ({SETUP_DOC})")
    raw = registry[name]
    if raw is None or not raw.strip():
        return {"name": name, "config_dir": None}
    config_dir = _expand(raw)
    if not exists(config_dir):
        raise AccountError(f"account {name!r}: config dir {config_dir} does not exist — run "
                           f"the login step in {SETUP_DOC}")
    if not isfile(os.path.join(config_dir, CREDENTIALS_FILE)):
        raise AccountError(f"account {name!r}: {config_dir} has no {CREDENTIALS_FILE}, so it was "
                           f"never logged in — run `claude /login` with CLAUDE_CONFIG_DIR set to it "
                           f"({SETUP_DOC})")
    problem = rails_problem(config_dir, reference_dir)
    if problem:
        raise AccountError(f"account {name!r}: {problem}")
    return {"name": name, "config_dir": config_dir}


def account_of(rec: dict) -> dict | None:
    """The `account` block a job record carries, or `None` (no `--account` was passed: today's
    behaviour, the inherited login)."""
    acct = rec.get("account") if isinstance(rec, dict) else None
    if not isinstance(acct, dict) or not isinstance(acct.get("name"), str):
        return None
    return acct


def account_label(rec: dict) -> str:
    """The account name worth SHOWING — `""` for no flag and for the explicit `default`, so a list
    of jobs only grows a word where usage actually went somewhere else."""
    acct = account_of(rec)
    if acct is None or acct["name"] == DEFAULT_NAME:
        return ""
    return acct["name"]


def apply_env(env: dict, account: dict | None) -> dict:
    """Point `env` at `account`'s login, in place, and return it. `None` leaves `env` untouched —
    byte-for-byte the pre-`--account` behaviour."""
    if not isinstance(account, dict):
        return env
    config_dir = account.get("config_dir")
    if isinstance(config_dir, str) and config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    return env
