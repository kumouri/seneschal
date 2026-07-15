#!/usr/bin/env python3
"""The desktop end of the automatic Health Connect feed — a tiny HTTP listener bound to the tailnet.

The transport is Tailscale/LAN: the owner's phone's ``HealthSyncWorker`` reads Health
Connect on a schedule and POSTs newline-delimited JSON (see ``import_ndjson`` in ``health_import.py``) to
this listener, which appends each batch to ``state/health-feed.ndjson`` and imports it into
``state/health.db``. No cloud in the loop.

**The tailnet is the trust boundary.** By default this binds to the Tailscale interface only (an address
in ``100.64.0.0/10``, Tailscale's CGNAT range), so nothing off the tailnet can reach it. A shared token
(``--token`` / ``HEALTH_INGEST_TOKEN``) is cheap extra insurance and, if set, is required on every POST as
``Authorization: Bearer <token>``. It never binds to ``0.0.0.0`` unless you force ``--host 0.0.0.0``, and
it refuses to start on a public interface without a token.

This receives data the owner's own phone sends and writes a local cache — **act-low**. It exposes no data
(GET returns only a health check) and calls out to nothing. Stdlib only.

Usage:
  python health_listener.py                       # bind the tailnet IP, port 8765
  python health_listener.py --token hunter2        # require a bearer token
  python health_listener.py --host 127.0.0.1       # localhost only (e.g. testing)
  python health_listener.py --no-import            # just archive batches, import later
Endpoints:
  POST /health-ingest     body = NDJSON    -> {"ok":true,"imported":{...}}
  POST /presence-ingest   body = NDJSON    -> {"ok":true,"imported":{...}}  (presence feed, same token)
  GET  /health            -> {"ok":true,"db":"...","last_ingest":"..."}
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from health_common import DEFAULT_DB, DEFAULT_STATE_DIR, connect  # noqa: E402
from health_import import import_ndjson  # noqa: E402
from presence_common import DEFAULT_DB as PRESENCE_DB, connect as presence_connect  # noqa: E402
from presence_import import import_events  # noqa: E402

FEED_LOG = os.path.join(DEFAULT_STATE_DIR, "health-feed.ndjson")
PRESENCE_FEED_LOG = os.path.join(DEFAULT_STATE_DIR, "presence-feed.ndjson")
MAX_BODY = 64 * 1024 * 1024  # 64 MB — a fat nightly batch is a few MB; this is just a runaway guard.


def tailscale_ip() -> str | None:
    """This host's Tailscale address (100.64.0.0/10), if the tailnet is up. ``None`` otherwise."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except socket.gaierror:
        infos = []
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    for *_, sockaddr in infos:
        ip = sockaddr[0]
        try:
            if ipaddress.ip_address(ip) in cgnat:
                return ip
        except ValueError:
            continue
    return None


def _is_local_or_tailnet(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10") or ip.is_private


class Handler(BaseHTTPRequestHandler):
    token = None          # set on the class before serving
    do_import = True
    db_path = DEFAULT_DB
    presence_db = PRESENCE_DB

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # keep the console quiet; we log ingests ourselves
        pass

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            last = None
            if os.path.exists(FEED_LOG):
                last = datetime.fromtimestamp(os.path.getmtime(FEED_LOG), timezone.utc).isoformat()
            self._send(200, {"ok": True, "db": self.db_path, "last_ingest": last})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        route = self.path.rstrip("/")
        if route not in ("/health-ingest", "/presence-ingest"):
            self._send(404, {"ok": False, "error": "not found"})
            return
        if self.token:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {self.token}":
                self._send(401, {"ok": False, "error": "bad or missing bearer token"})
                return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(413, {"ok": False, "error": f"body must be 1..{MAX_BODY} bytes"})
            return
        body = self.rfile.read(length).decode("utf-8", errors="replace")

        feed_log = FEED_LOG if route == "/health-ingest" else PRESENCE_FEED_LOG
        os.makedirs(DEFAULT_STATE_DIR, exist_ok=True)
        with open(feed_log, "a", encoding="utf-8") as fh:   # archive every batch verbatim, always
            fh.write(body if body.endswith("\n") else body + "\n")

        result = {"archived": True}
        if self.do_import:
            if route == "/health-ingest":
                conn = connect(self.db_path)
                try:
                    result["imported"] = import_ndjson(conn, body.splitlines())
                finally:
                    conn.close()
            else:  # /presence-ingest
                conn = presence_connect(self.presence_db)
                try:
                    counts = import_events(conn, body.splitlines())
                    counts.pop("context", None)  # don't echo the whole snapshot back over HTTP
                    result["imported"] = counts
                finally:
                    conn.close()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        print(f"[{stamp}] {route} {length}B -> {result.get('imported', 'archived only')}", flush=True)
        self._send(200, {"ok": True, **result})


def main() -> int:
    p = argparse.ArgumentParser(description="Tailnet listener for the phone's Health Connect feed.")
    p.add_argument("--host", help="bind address (default: this host's Tailscale IP, else 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token", default=os.environ.get("HEALTH_INGEST_TOKEN"),
                   help="require this bearer token on POSTs (or set HEALTH_INGEST_TOKEN)")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--no-import", action="store_true", help="archive batches but don't import them")
    args = p.parse_args()

    host = args.host or tailscale_ip() or "127.0.0.1"
    if not _is_local_or_tailnet(host) and not args.token:
        print(json.dumps({"ok": False, "error": f"refusing to bind public {host} without a --token"}))
        return 2

    Handler.token = args.token
    Handler.do_import = not args.no_import
    Handler.db_path = args.db

    connect(args.db).close()  # ensure the health schema exists before the first POST
    presence_connect(PRESENCE_DB).close()  # ...and the presence schema (for /presence-ingest)
    server = ThreadingHTTPServer((host, args.port), Handler)
    scope = "tailnet" if host == tailscale_ip() else ("localhost" if _is_local_or_tailnet(host) else host)
    print(f"health listener on http://{host}:{args.port}  ({scope}"
          f"{', token required' if args.token else ''})  POST /health-ingest", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
