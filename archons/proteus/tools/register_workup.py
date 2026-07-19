#!/usr/bin/env python3
"""Append or update one entry in ``out/workups.json`` — the reliable manifest-write path.

The Proteus archon writes its *deliverables* with the Write tool (new files under
``out/<run-date>/`` — that works), but modifying the EXISTING manifest via the Edit tool is
intermittently denied in the headless ``claude -p`` runtime (Edit isn't in Proteus's granted
tools). So the charter has the archon register each worked-up role by running THIS helper via
Bash (a granted tool) instead::

    python register_workup.py --json "{\"slug\": \"acme-swe\", \"company\": \"Acme\", ...}"

Read-modify-write, de-duped by ``slug`` (else ``jd_url``); an existing entry is updated in place
rather than duplicated. Preserves the file's ``{ "workups": [...] }`` shape. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys

from proteus_paths import MANIFEST_FILE  # out/workups.json


def _load() -> tuple[dict | None, list]:
    """Return (container, workups). ``container`` is the dict wrapper when the file uses the
    ``{"workups": [...]}`` shape (so we can write it back unchanged), else None for a bare list."""
    if not MANIFEST_FILE.exists():
        return {"workups": []}, []  # brand-new manifest gets the canonical {"workups": [...]} shape
    try:
        raw = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, []
    if isinstance(raw, dict) and isinstance(raw.get("workups"), list):
        return raw, raw["workups"]
    return None, (raw if isinstance(raw, list) else [])


def _key(w: dict) -> str:
    return (w.get("slug") or "").strip().lower() or (w.get("jd_url") or "").strip().lower()


def register(entry: dict) -> tuple[int, str]:
    """Append or update ``entry`` in the manifest. Returns (count, action)."""
    container, workups = _load()
    k = _key(entry)
    action = "appended"
    for i, w in enumerate(workups):
        if _key(w) == k:
            workups[i] = {**w, **entry}  # update in place, keep any fields the caller omitted
            action = "updated"
            break
    else:
        workups.append(entry)
    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = container if container is not None else workups
    if container is not None:
        container["workups"] = workups
    MANIFEST_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(workups), action


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Register a workup entry in out/workups.json.")
    ap.add_argument("--json", required=True, help="the workup entry as a JSON object")
    args = ap.parse_args(argv)
    try:
        entry = json.loads(args.json)
    except ValueError as exc:
        print(f"register_workup: --json is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(entry, dict) or not (entry.get("slug") or entry.get("jd_url")):
        print("register_workup: entry must be a JSON object with at least a slug or jd_url",
              file=sys.stderr)
        return 2
    count, action = register(entry)
    print(f"{action} '{entry.get('company', '?')} — {(entry.get('title') or '')[:40]}' "
          f"({count} workups in the manifest)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
