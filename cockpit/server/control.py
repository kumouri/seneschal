"""The cockpit backend's ONLY writes: the graceful-restart enqueue + the audit log.

Everything else in this app is read-only (readers.py). `enqueue_restart` replicates
seneschal/scripts/request_control.py's `enqueue_control(state_dir, "restart", defer=True)` byte-for-byte
(same file, same key names, same action-dedupe rule) rather than importing it, so the cockpit stays
its own dependency world (cockpit-spec.md ruling 3) and never needs the daemon's script package on its
import path. If request_control.py's queue shape ever changes, mirror the change here too.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

CONTROL_QUEUE_FILE = "control-queue.json"  # must match presence.CONTROL_QUEUE / request_control.py
AUDIT_LOG_FILE = "cockpit-audit.jsonl"


def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def enqueue_restart(state_dir: Path, reason: str = "") -> tuple[Path, bool]:
    """Append a graceful `restart` control entry (deduped by action — a second click while one is
    already queued is a no-op, matching request_control.py). Returns (queue_path, already_queued)."""
    path = state_dir / CONTROL_QUEUE_FILE
    items = _load_json(path, [])
    if not isinstance(items, list):
        items = []
    if any(isinstance(i, dict) and i.get("action") == "restart" for i in items):
        return path, True
    items.append({
        "action": "restart",
        "defer_until_idle": True,
        "reason": reason,
        "requested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })
    _save_json(path, items)
    return path, False


def append_audit(state_dir: Path, action: str, detail: dict | None = None) -> Path:
    """Append one line to cockpit-audit.jsonl — every mutating call, success or no-op alike."""
    path = state_dir / AUDIT_LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "action": action,
        "detail": detail or {},
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    return path
