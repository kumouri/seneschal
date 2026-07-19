"""Archon tiles + GET-only reverse proxy.

`archon-registry.json` (sibling of this file, **gitignored — per-install**) mirrors
`seneschal/references/archons.md`'s port registry table. Seed it from the tracked
`archon-registry.example.json` and keep it in sync by hand when the roster changes (mint/retire),
same "duplicated, not imported" posture as `model_config.py`/`governor.py` (the cockpit is its own
dependency world). An **absent registry is fine** — the roster just reads empty (one log line, no
error), which is the normal state of a fresh checkout.

`GET /api/archons` (app.py) reads the registry plus a best-effort reachability probe for anything
marked `"status": "live"` — tolerant: a probe failure just reports `reachable: false`, never 500s.

`GET /archons/{id}/{path:path}` (app.py) is a **GET-only** reverse proxy to
`http://127.0.0.1:<port>/<path>` over stdlib `urllib.request` — enough for an archon that ships a
static site; a future archon needing POST would need this widened (a documented limitation, see
cockpit/README.md). Every proxied response streams back the upstream's status + content-type verbatim
(capped at `PROXY_MAX_BYTES` — a static site's largest asset, generously bounded) so the browser
renders it exactly as if it had talked to the archon directly.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Optional

from .config import get_archon_registry_path

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 1.5
PROXY_TIMEOUT_SECONDS = 10.0
PROXY_MAX_BYTES = 25 * 1024 * 1024


def load_registry() -> dict:
    path = get_archon_registry_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        # Normal on a fresh checkout — the real registry is per-install (gitignored); seed it from
        # archon-registry.example.json when there are archons to register.
        logger.info("archon registry %s absent — empty roster", path)
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _probe(port: int) -> bool:
    """Best-effort reachability check — ANY response (even an error status) counts as reachable; only
    a connection failure/timeout means "not reachable". Never raises."""
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=PROBE_TIMEOUT_SECONDS)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001 — a probe is advisory; any other failure just reads "unreachable"
        return False


def _serving_port(entry: dict):
    """The port the cockpit should probe/proxy: `ui_port` (the archon's human-facing site) when
    present, else `port` (the A2A agent endpoint from the archons.md registry). They are different
    servers — proxying the A2A port would show visitors an agent-card, not a UI — which is exactly
    the bug this split fixes."""
    port = entry.get("ui_port", entry.get("port"))
    return port if isinstance(port, int) and not isinstance(port, bool) else None


def list_archons() -> dict:
    registry = load_registry()
    out = []
    for archon_id, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        port = _serving_port(entry)
        status = entry.get("status", "unknown")
        reachable: Optional[bool] = None
        if status == "live" and port is not None:
            reachable = _probe(port)
        out.append({
            "id": archon_id,
            "title": entry.get("title", archon_id),
            "status": status,
            "port": port,
            "reachable": reachable,
        })
    out.sort(key=lambda a: a["id"])
    return {"archons": out}


class ProxyError(Exception):
    """Carries the HTTP status the route handler should answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def proxy_get(archon_id: str, path: str, query: Optional[str] = None) -> tuple[bytes, int, str]:
    """`(body, status_code, content_type)` for a GET proxied to the archon's own server. Raises
    `ProxyError` for an unknown id, a non-live archon, a missing port, or an unreachable upstream —
    the route handler (app.py) turns that into an honest HTTP error, never a 500."""
    registry = load_registry()
    entry = registry.get(archon_id)
    if not isinstance(entry, dict):
        raise ProxyError(404, f"unknown archon {archon_id!r}")
    status = entry.get("status")
    if status != "live":
        raise ProxyError(503, f"{archon_id} is {status or 'unavailable'}, not live")
    port = _serving_port(entry)
    if port is None:
        raise ProxyError(503, f"{archon_id} has no registered port")

    url = f"http://127.0.0.1:{port}/{path.lstrip('/')}"
    if query:
        url += f"?{query}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT_SECONDS) as resp:
            body = resp.read(PROXY_MAX_BYTES + 1)
            if len(body) > PROXY_MAX_BYTES:
                raise ProxyError(502, f"{archon_id}'s response exceeded the proxy size cap")
            content_type = resp.headers.get("Content-Type") or "application/octet-stream"
            return body, resp.status, content_type
    except urllib.error.HTTPError as e:
        with e:
            body = e.read(PROXY_MAX_BYTES + 1)
            content_type = (e.headers.get("Content-Type") if e.headers else None) or "text/plain"
        return body, e.code, content_type
    except urllib.error.URLError as e:
        raise ProxyError(502, f"could not reach {archon_id} on port {port}: {e.reason}")
    except TimeoutError:
        raise ProxyError(504, f"{archon_id} did not respond in time")
