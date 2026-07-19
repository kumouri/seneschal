#!/usr/bin/env python3
"""The /setup wizard's env-file writer — renders real ``*.env`` files from their
tracked ``*.env.example`` templates, driven by ``seneschal/setup/env-manifest.json``.

Input (stdin, JSON)::

    {"manifest_id": "telegram", "values": {"TELEGRAM_BOT_TOKEN": "...", ...}}

``values`` may be a subset — unset vars fall back (in order) to the value already in
the existing env file (merge mode), then the manifest var's ``default`` (which may
reference another var as ``{VAR_NAME}``), then the example file's own value. The
render PRESERVES the example's comment lines and variable order, so the written file
stays as self-documenting as the template. A commented-out example var line
(``# NAME=...``) is activated only when a value for it is provided (or kept from the
existing file); otherwise it stays commented.

Merge contract: re-running setup must never clobber a var the owner set by hand —
provided values overwrite, unprovided existing values are KEPT (including vars the
owner added that the example doesn't know; those are appended at the end).
``--replace`` re-renders from scratch, ignoring the existing file.

Validation: each provided value is checked against the manifest var's ``validate``
regex (``re.fullmatch``). Rejections name the var; for vars marked ``secret`` the
message says ``<redacted>`` — a secret VALUE never appears in any output of this
tool (stdout JSON reports names only).

Permissions: on POSIX the file is created 0o600 via ``os.open`` (temp + atomic
``os.replace``, mode preserved). On Windows the mode bits are a no-op — the file
lands with normal user-profile ACLs, which on a single-user profile is the
equivalent protection; no ACL surgery is attempted.

Output (stdout, JSON)::

    {"ok": true, "path": "...", "vars_set": [...], "vars_kept": [...],
     "vars_defaulted": [...], "warnings": [...]}

``--check <manifest_id>``: no stdin; reports which required vars are missing/empty
in the existing file (NAMES only — the doctor's probe). A required var still equal
to a non-default example placeholder (e.g. ``your-bot-token``) counts as missing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "seneschal" / "setup" / "env-manifest.json"

VAR_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
COMMENTED_VAR_LINE = re.compile(r"^#\s*([A-Z][A-Z0-9_]*)=(.*)$")
TEMPLATE_REF = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


class SetupEnvError(Exception):
    """A user-reportable failure (message is already secret-safe)."""


def load_manifest(path: Path | str = DEFAULT_MANIFEST) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise SetupEnvError(f"cannot read manifest {path}: {exc}") from exc
    except ValueError as exc:
        raise SetupEnvError(f"manifest {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise SetupEnvError(f"manifest {path} has no entries list")
    return data


def find_entry(manifest: dict, manifest_id: str) -> dict:
    for entry in manifest["entries"]:
        if entry.get("id") == manifest_id:
            return entry
    known = ", ".join(e.get("id", "?") for e in manifest["entries"])
    raise SetupEnvError(f"unknown manifest id {manifest_id!r} (known: {known})")


def parse_env_text(text: str) -> dict[str, str]:
    """Uncommented NAME=value pairs, in file order (later duplicates win)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = VAR_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _display(value: str, secret: bool) -> str:
    return "<redacted>" if secret else repr(value)


def validate_values(entry: dict, provided: dict[str, str]) -> None:
    """Reject unknown vars, non-string/multiline values, and validate-regex misses."""
    vars_by_name = {v["name"]: v for v in entry.get("vars", [])}
    for name, value in provided.items():
        var = vars_by_name.get(name)
        if var is None:
            raise SetupEnvError(
                f"unknown var {name!r} for manifest entry {entry['id']!r} "
                f"(known: {', '.join(vars_by_name) or 'none'})"
            )
        secret = bool(var.get("secret"))
        if not isinstance(value, str):
            raise SetupEnvError(f"value for {name} must be a string")
        if "\n" in value or "\r" in value:
            raise SetupEnvError(f"value for {name} contains a newline (env files are line-based)")
        pattern = var.get("validate")
        if pattern and not re.fullmatch(pattern, value):
            raise SetupEnvError(
                f"value for {name} does not match its expected shape "
                f"({pattern}): {_display(value, secret)}"
            )


def render(
    entry: dict,
    example_text: str,
    existing: dict[str, str] | None,
    provided: dict[str, str],
) -> tuple[str, dict]:
    """Render the target env file's content. Returns (content, report).

    Precedence per var: provided > existing (merge mode; ``existing=None`` for
    --replace / no file) > manifest default (``{VAR}`` refs expanded) > example value.
    """
    vars_by_name = {v["name"]: v for v in entry.get("vars", [])}
    existing = existing or {}
    vars_set: list[str] = []
    vars_kept: list[str] = []
    vars_defaulted: list[str] = []
    warnings: list[str] = []
    resolved: dict[str, str] = {}
    default_pending: list[str] = []

    def resolve(name: str, example_value: str) -> str:
        if name in provided:
            if name not in vars_set:
                vars_set.append(name)
            return provided[name]
        if name in existing:
            if name not in vars_kept:
                vars_kept.append(name)
            return existing[name]
        var = vars_by_name.get(name, {})
        if "default" in var:
            if name not in vars_defaulted:
                vars_defaulted.append(name)
            default_pending.append(name)  # template refs expand in a second pass
            return var["default"]
        return example_value

    out_lines: list[str] = []
    seen: set[str] = set()
    slots: dict[str, int] = {}  # var name -> index in out_lines (for the template pass)
    for line in example_text.splitlines():
        m = VAR_LINE.match(line)
        if m:
            name, example_value = m.group(1), m.group(2)
            seen.add(name)
            slots[name] = len(out_lines)
            resolved[name] = resolve(name, example_value)
            out_lines.append(f"{name}={resolved[name]}")
            continue
        cm = COMMENTED_VAR_LINE.match(line)
        if cm:
            name = cm.group(1)
            seen.add(name)
            if name in provided or name in existing:
                # Activate the commented-out example var (e.g. "# PUSH_CALL_TO=+1...").
                slots[name] = len(out_lines)
                resolved[name] = resolve(name, "")
                out_lines.append(f"{name}={resolved[name]}")
            else:
                out_lines.append(line)  # leave the commented example line untouched
            continue
        out_lines.append(line)

    # Owner-added vars the example doesn't know: keep them (merge contract).
    extras = [n for n in existing if n not in seen]
    if extras:
        out_lines.append("")
        out_lines.append("# Kept by setup (not in the example template):")
        for name in extras:
            resolved[name] = existing[name]
            if name not in vars_kept:
                vars_kept.append(name)
            out_lines.append(f"{name}={existing[name]}")
            warnings.append(f"kept owner-added var {name} (not in the example template)")

    # Second pass: expand {VAR} references inside applied defaults.
    for name in default_pending:
        def sub(m: re.Match) -> str:
            ref = m.group(1)
            value = resolved.get(ref, "") if ref != name else ""
            if value == "":
                warnings.append(f"default for {name} references {{{ref}}}, which is unset/empty — left empty")
            return value

        expanded = TEMPLATE_REF.sub(sub, resolved[name])
        if expanded != resolved[name]:
            resolved[name] = expanded
            out_lines[slots[name]] = f"{name}={expanded}"

    report = {
        "vars_set": vars_set,
        "vars_kept": vars_kept,
        "vars_defaulted": vars_defaulted,
        "warnings": warnings,
    }
    return "\n".join(out_lines) + "\n", report


def write_secure(path: Path, content: str) -> None:
    """Atomic write; 0o600 on POSIX (see module docstring for the Windows story)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def apply(
    entry: dict,
    provided: dict[str, str],
    root: Path | str = REPO_ROOT,
    replace: bool = False,
) -> dict:
    """Validate + render + write one entry's env file. Returns the stdout report."""
    if entry.get("format", "env") != "env":
        raise SetupEnvError(
            f"manifest entry {entry['id']!r} is format {entry.get('format')!r}, not an env file "
            f"(it is owned by the {entry.get('handled_by', 'owning')!r} chapter)"
        )
    root = Path(root)
    validate_values(entry, provided)
    example_path = root / entry["example"]
    try:
        example_text = example_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SetupEnvError(f"cannot read example {example_path}: {exc}") from exc
    target = root / entry["path"]
    existing = None
    if not replace and target.is_file():
        existing = parse_env_text(target.read_text(encoding="utf-8"))
    content, report = render(entry, example_text, existing, provided)
    write_secure(target, content)
    return {"ok": True, "path": str(target), **report}


def check(entry: dict, root: Path | str = REPO_ROOT) -> dict:
    """Doctor probe: which REQUIRED vars are missing/empty/still-placeholder (names only)."""
    root = Path(root)
    target = root / entry["path"]
    exists = target.is_file()
    live = parse_env_text(target.read_text(encoding="utf-8")) if exists else {}
    example_values: dict[str, str] = {}
    try:
        example_values = parse_env_text((root / entry["example"]).read_text(encoding="utf-8"))
    except OSError:
        pass
    missing: list[str] = []
    required = [v for v in entry.get("vars", []) if v.get("required")]
    for var in required:
        name = var["name"]
        value = live.get(name, "")
        if not value:
            missing.append(name)
            continue
        example_value = example_values.get(name, "")
        # Unchanged from a non-default example placeholder = not really configured.
        if value == example_value and example_value and example_value != var.get("default"):
            missing.append(name)
    return {
        "ok": exists and not missing,
        "path": str(target),
        "exists": exists,
        "required": [v["name"] for v in required],
        "missing": missing,
    }


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Render a real env file from its example, manifest-driven.")
    p.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="env manifest path")
    p.add_argument("--root", default=str(REPO_ROOT), help="repo root paths resolve against")
    p.add_argument("--replace", action="store_true", help="full re-render (ignore the existing file)")
    p.add_argument("--check", metavar="MANIFEST_ID", help="report missing required vars (names only); no stdin")
    args = p.parse_args(argv[1:])

    try:
        manifest = load_manifest(args.manifest)
        if args.check:
            print(json.dumps(check(find_entry(manifest, args.check), args.root), indent=2))
            return 0
        try:
            payload = json.load(sys.stdin)
        except ValueError as exc:
            raise SetupEnvError(f"stdin is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("manifest_id"), str):
            raise SetupEnvError('stdin payload must be {"manifest_id": "...", "values": {...}}')
        values = payload.get("values", {})
        if not isinstance(values, dict):
            raise SetupEnvError('"values" must be an object of VAR -> string')
        entry = find_entry(manifest, payload["manifest_id"])
        print(json.dumps(apply(entry, values, args.root, replace=args.replace), indent=2))
        return 0
    except SetupEnvError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
