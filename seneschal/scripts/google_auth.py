#!/usr/bin/env python3
"""One-time OAuth consent for a Google account — mints and stores a durable refresh token.

Run this once per account (interactively, in a browser session). It opens Google's consent screen, catches
the redirect on a localhost loopback, exchanges the code, and saves the refresh token into the token store
so `gcal_api.py` / `gmail_api.py` can run unattended forever after.

  python google_auth.py --account personal --env-file google.env
  python google_auth.py --account work     --env-file google.env
  python google_auth.py --list             --env-file google.env   # show stored accounts, no auth

WHY A REFRESH TOKEN CAN GO STALE: if the OAuth app is left in "Testing" publishing status, Google expires
refresh tokens for sensitive/restricted scopes after 7 days. Publish the app to "In production" on the
OAuth consent screen (you can stay unverified for personal use — just click through the "unverified app"
warning) and the refresh token becomes durable. This script always requests access_type=offline &
prompt=consent so Google actually returns a refresh token. Full walkthrough: GOOGLE_SETUP.md.

Standard library only (http.server for the loopback catch, urllib for the exchange).
"""
from __future__ import annotations

import argparse
import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import google_common as gc

USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Catches the single OAuth redirect on the loopback address and stashes the query params."""

    result: dict = {}

    def do_GET(self):  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/", "/callback"):
            self.send_response(404)
            self.end_headers()
            return
        _CallbackHandler.result = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        ok = "code" in _CallbackHandler.result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = ("The assistant is connected to this Google account. You can close this tab."
               if ok else "Authorization failed or was denied. You can close this tab and retry.")
        self.wfile.write(
            f"<html><body style='font-family:sans-serif;padding:3rem'><h2>{msg}</h2></body></html>"
            .encode("utf-8")
        )

    def log_message(self, *args):  # silence the default stderr access log
        return


def _run_consent(c: dict, account: str, open_browser: bool) -> dict:
    server = http.server.HTTPServer(("127.0.0.1", c["oauth_port"]), _CallbackHandler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    state = secrets.token_urlsafe(24)

    auth_url = gc.AUTH_ENDPOINT + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": c["scopes"],
        "access_type": "offline",     # ask for a refresh token
        "prompt": "consent",          # force it even on re-auth
        "include_granted_scopes": "true",
        "state": state,
    })

    print(f"\nOpen this URL in a browser signed in as the '{account}' Google account:\n\n{auth_url}\n")
    print(f"Listening for the redirect on {redirect_uri} ...")
    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001
            pass

    thread = threading.Thread(target=server.handle_request)  # serve exactly one request
    thread.start()
    thread.join(timeout=300)
    server.server_close()

    res = _CallbackHandler.result
    if not res:
        raise RuntimeError("Timed out (5 min) waiting for the OAuth redirect.")
    if res.get("state") != state:
        raise RuntimeError("OAuth state mismatch — aborting (possible CSRF).")
    if "code" not in res:
        raise RuntimeError(f"Consent did not return a code: {res.get('error', res)}")

    tok = gc.exchange_code(c, res["code"], redirect_uri)
    if not tok.get("refresh_token"):
        raise RuntimeError(
            "No refresh_token returned. Revoke the assistant's access at "
            "https://myaccount.google.com/permissions and re-run (Google only returns it on first consent)."
        )
    return tok


def _whoami(access_token: str) -> str:
    try:
        info = gc.http_json("GET", USERINFO_ENDPOINT,
                            headers={"Authorization": f"Bearer {access_token}"}, where="userinfo")
        return info.get("email", "")
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    p = argparse.ArgumentParser(description="One-time OAuth consent for a Google account.")
    p.add_argument("--account", help="account label to authorize (e.g. personal, work)")
    p.add_argument("--env-file", help="KEY=VALUE file with GOOGLE_* settings (kept untracked)")
    p.add_argument("--no-browser", action="store_true", help="print the URL but don't auto-open a browser")
    p.add_argument("--list", action="store_true", help="list stored accounts and exit (no auth)")
    args = p.parse_args()

    c = gc.cfg(gc.load_env(args.env_file))

    if args.list:
        tokens = gc.load_tokens(c["token_store"])
        rows = {a: {"email": r.get("email"), "scopes": r.get("scopes"), "obtained_at": r.get("obtained_at")}
                for a, r in tokens["accounts"].items()}
        print(json.dumps({"ok": True, "token_store": c["token_store"], "accounts": rows}, indent=2))
        return 0

    if not args.account:
        print(json.dumps({"ok": False, "error": "provide --account (or --list)"}))
        return 2

    try:
        gc.require_client(c)
        tok = _run_consent(c, args.account, open_browser=not args.no_browser)
        email = _whoami(tok.get("access_token", ""))
        gc.upsert_account(c, args.account, {
            "email": email,
            "refresh_token": tok["refresh_token"],
            "scopes": tok.get("scope", c["scopes"]),
            "client_id": c["client_id"],
            "obtained_at": gc.rfc3339(gc.now_utc()),
        })
        print(json.dumps({"ok": True, "account": args.account, "email": email,
                          "token_store": c["token_store"], "stored": True}))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "account": args.account, "error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
