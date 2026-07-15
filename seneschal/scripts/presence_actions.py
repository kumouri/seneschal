#!/usr/bin/env python3
"""Pure action-mapping for the presence feed's Home Assistant / ESPHome layer (Phase 5) — no I/O,
unit-testable, modeled on the tracked no-LLM shape of ``reminders_roll.py`` / ``presence_rules.py``.

Maps a **context edge** (a presence.db event — geofence enter/exit, an activity or sleep transition) to
the Home Assistant automations the owner has configured for it, and classifies each as **act-low**
(pre-approved → fires directly) or **ask-high** (draft-and-hold for approval). **Controlling physical
devices is act-high:** only automations the owner has explicitly ``approved`` fire on their own, and a
``failsafe`` automation (e.g. arming the Berkey / fountain leak cutoff) stays ask-high *always*, per
``../references/autonomy-policy.md``.

Config lives in ``state/presence-automations.json`` (gitignored; copy the tracked ``.example.json``).
Actually calling HA is ``ha_client.call_service`` — this module only decides *what* and *whether it's
pre-approved*.

**Not wired into the live daemon yet** — Home Assistant isn't standing, and device control is act-high.
The daemon integration point + the enable path (stand up HA, approve automations one by one) are in
``HA_SETUP.md``.
"""
from __future__ import annotations

import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "state"))
DEFAULT_CONFIG = os.path.join(DEFAULT_STATE_DIR, "presence-automations.json")


def load_automations(config_path: str = DEFAULT_CONFIG) -> list:
    """Load the configured automations; a missing/broken file → empty list (inert)."""
    try:
        with open(config_path, encoding="utf-8") as fh:
            data = json.load(fh)
        autos = data.get("automations", []) if isinstance(data, dict) else []
        return [a for a in autos if isinstance(a, dict)]
    except (OSError, json.JSONDecodeError):
        return []


def actions_for_edge(edge: dict, automations: list) -> list:
    """Every automation whose ``on`` trigger matches this context edge (kind / label / transition)."""
    kind, label, transition = edge.get("kind"), edge.get("label"), edge.get("transition")
    out = []
    for a in automations:
        on = a.get("on", {})
        if on.get("kind") == kind and on.get("label") == label and on.get("transition") == transition:
            out.append(a)
    return out


def disposition(automation: dict) -> str:
    """``act-low`` (fire directly) iff the automation is pre-approved AND not a failsafe; else ``ask-high``
    (draft-and-hold). A ``failsafe`` (leak cutoff, etc.) is **always** ask-high — it stays gated longest."""
    if automation.get("failsafe"):
        return "ask-high"
    return "act-low" if automation.get("approved") else "ask-high"


def main() -> int:
    import argparse
    import sys
    import ha_client

    p = argparse.ArgumentParser(description="Evaluate a presence context edge against HA automations.")
    p.add_argument("--edge", help='JSON edge, e.g. {"kind":"geofence","label":"home","transition":"enter"}')
    p.add_argument("--fire", action="store_true",
                   help="actually call HA for act-low (pre-approved, non-failsafe) matches")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    args = p.parse_args()
    if not args.edge:
        p.print_help()
        return 1

    edge = json.loads(args.edge)
    matched = actions_for_edge(edge, load_automations(args.config))
    results = []
    for a in matched:
        d = disposition(a)
        rec = {"id": a.get("id"), "disposition": d, "call": a.get("call")}
        if args.fire and d == "act-low":
            c = a.get("call", {})
            rec["result"] = ha_client.call_service(c.get("domain"), c.get("service"), c.get("data"))
        results.append(rec)
    print(json.dumps({"edge": edge, "ha_available": ha_client.available(), "matched": results}, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
