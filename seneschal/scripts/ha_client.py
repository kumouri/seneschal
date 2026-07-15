#!/usr/bin/env python3
"""Minimal Home Assistant REST client (stdlib) for the presence feed's action layer.

Calls an HA service (e.g. ``light.turn_on``) via HA's REST API. Config — the HA base URL + a long-lived
access token — lives in a gitignored ``ha.env`` next to this script (copy ``ha.env.example``). Without it,
``available()`` is False and nothing calls out, so the whole action layer is **inert** until the owner stands
up Home Assistant. **Controlling devices is act-high**: the daemon only ever fires *pre-approved*
automations (see ``presence_actions.py``); everything else is draft-and-hold.

Stdlib only (urllib) — no package install.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV = os.path.join(SCRIPT_DIR, "ha.env")


def load_config(env_path: str = DEFAULT_ENV) -> dict:
    """Parse ``HA_URL`` + ``HA_TOKEN`` from a KEY=VALUE env file. Missing file → empty dict."""
    cfg: dict = {}
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return cfg


def available(env_path: str = DEFAULT_ENV) -> bool:
    """True iff HA is configured (URL + token present) — else the action layer stays inert."""
    cfg = load_config(env_path)
    return bool(cfg.get("HA_URL") and cfg.get("HA_TOKEN"))


def call_service(domain: str, service: str, data: dict | None = None,
                 env_path: str = DEFAULT_ENV, timeout: float = 15.0) -> dict:
    """Call ``<domain>.<service>`` on Home Assistant. Returns ``{"ok": bool, ...}``; never raises."""
    cfg = load_config(env_path)
    url, token = cfg.get("HA_URL"), cfg.get("HA_TOKEN")
    if not url or not token:
        return {"ok": False, "error": "HA not configured (no ha.env)"}
    endpoint = f"{url.rstrip('/')}/api/services/{domain}/{service}"
    body = json.dumps(data or {}).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": 200 <= resp.status < 300, "status": resp.status}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": e.reason}
    except (urllib.error.URLError, OSError) as e:
        return {"ok": False, "error": str(e)}
