"""The seneschald cockpit's break-glass supervisor (v5, cockpit-spec.md "Break-glass").

Deliberately its OWN dependency world within `cockpit/` — stdlib only, no fastapi/uvicorn, so it keeps
working when the cockpit backend, its venv, or the daemon are all broken (that's the entire point: it
recovers a wedged daemon that `seneschald-update` can't fix on its own). See `supervisor.py`'s module
docstring for the full trust-chain writeup.
"""
