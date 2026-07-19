#!/usr/bin/env python3
"""The break-glass supervisor — cockpit-spec.md "Break-glass" (v5). DELIBERATELY stdlib-ONLY
(`http.server`, no fastapi/uvicorn): its whole reason to exist is surviving everything else being
broken — a wedged daemon, a broken venv, a broken cockpit backend. If it depended on the SAME
dependency world as the thing it's meant to rescue, a break in that world could take the rescuer down
with it. Own localhost port (default 8499).

## Trust chain — read this before trusting anything here

- **Rungs 1-2** (a FRESH Zitadel re-auth: password + TOTP, `prompt=login&max_age=0`) live ENTIRELY in
  the main cockpit backend (`cockpit/server/`, fastapi). This process is never told a password, a TOTP
  code, or anything about Zitadel at all. Zitadel is what actually re-verifies "this is the owner,
  right now" for rungs 1-2; the backend just relays that outcome by minting a short-lived, single-use,
  HMAC-signed **assertion** (`assertion.py` — a stdlib module SHARED, not duplicated, between the
  backend and this process, because minter and verifier must agree byte-for-byte on the format).
- This supervisor trusts exactly TWO things: (1) a valid, unspent, unexpired assertion (proof rungs 1-2
  just happened for the requested action) and (2) its own freshly-generated nonce phrase, delivered
  out-of-band over Telegram and typed back within a short window (**rung 3**). It does NOT trust — and
  never even reads — the cockpit's session cookie. A stolen browser session is not enough to reach
  here: an attacker would also need to force a fresh Zitadel re-auth (password + TOTP) AND read the
  owner's Telegram.
- **Every attempt, every rung, success or failure, is appended to `<state_dir>/breakglass-audit.jsonl`
  and pushed to Telegram** (`seneschal/scripts/telegram_send.py`, called as a subprocess — this process
  never imports Telegram credential-handling code directly) — so a break-glass attempt the owner didn't
  initiate is visible on the one channel a browser-session thief doesn't hold.
- Restart-only (no pull) is the lighter recovery; `force-pull` additionally hard-resets the live
  checkout to `origin/<deploy_branch>` — see `actions.py`'s docstring for exactly what that discards
  (only uncommitted tracked changes; `state/` is gitignored and survives).

**CORS is deliberately permissive** (every response echoes the request's `Origin` back in
`Access-Control-Allow-Origin`, and `OPTIONS` always answers 204): the cockpit frontend calls this
process cross-origin (it runs on its own port, never proxied through the cockpit backend's `/api/*` —
see the trust-chain point above about surviving a broken backend), and there is no meaningful origin
to restrict TO — this process only ever binds `127.0.0.1`, so nothing off the machine can reach it
regardless of what Origin header a request carries. The ladder is the access control here, not CORS.

## Run

    python cockpit/breakglass/supervisor.py --port 8499

Its own scheduled-task registration follows the `health_listener.py` pattern (see
`seneschal/scripts/SCHEDULING.md`) but is **NOT** registered by this change — a deliberate follow-up, same
posture as v0's Zitadel container needing a documented human step before it does anything.

## Endpoints (all POST except /health)

    GET  /health                       -> {"ok": true}                        liveness only, no secrets
    POST /start   {assertion, action}  -> {"attempt_id", "expires_in"}         rung 3: sends the phrase
    POST /verify  {attempt_id, phrase} -> {"ok": true, "action", "steps"}      executes on a match
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from actions import BreakglassConfig, DefaultRunner, do_force_pull, do_restart  # noqa: E402
from assertion import ASSERTION_TTL_SECONDS, get_or_create_secret  # noqa: E402
from assertion import verify as verify_assertion  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_PORT = 8499
ALLOWED_ACTIONS = ("restart", "force-pull")
PHRASE_WINDOW_SECONDS = 300      # 5 minutes to type the phrase back
MAX_PHRASE_ATTEMPTS = 5
SPENT_JTI_TTL_SECONDS = ASSERTION_TTL_SECONDS + 300  # generous slack past the assertion's own TTL
AUDIT_FILE = "breakglass-audit.jsonl"

# A short, unambiguous word list for the rung-3 phrase (no lookalikes, nothing offensive) — a phrase
# like "amber-otter-forty" is much easier to read back correctly over Telegram than a hex string.
WORDS = [
    "amber", "birch", "cedar", "coral", "delta", "ember", "falcon", "garnet", "harbor", "indigo",
    "jasper", "kelp", "lagoon", "maple", "nectar", "onyx", "prairie", "quartz", "raven", "sable",
    "tundra", "umber", "violet", "willow", "xenon", "yarrow", "zephyr", "anchor", "basalt", "canyon",
    "denim", "ferry", "granite", "harvest", "ivory", "juniper", "keel", "lantern", "meadow", "nutmeg",
    "otter", "pebble", "quill", "ridge", "spruce", "thistle", "urchin", "velvet",
]


def _now() -> float:
    return time.time()


def _new_phrase() -> str:
    return "-".join(secrets.choice(WORDS) for _ in range(3))


class SupervisorState:
    """In-process state: pending rung-3 attempts + a spent-assertion-jti ledger. Deliberately IN
    MEMORY, not on disk — the phrase itself never touches disk, and a supervisor restart mid-attempt
    just cancels it (the owner starts the ladder over; no security cost either way)."""

    def __init__(self):
        self.attempts: dict[str, dict] = {}
        self.spent_jti: dict[str, float] = {}

    def _prune(self) -> None:
        now = _now()
        self.attempts = {k: v for k, v in self.attempts.items() if v["expires_at"] > now}
        self.spent_jti = {k: v for k, v in self.spent_jti.items() if now - v < SPENT_JTI_TTL_SECONDS}

    def is_spent(self, jti: str) -> bool:
        self._prune()
        return jti in self.spent_jti

    def mark_spent(self, jti: str) -> None:
        self.spent_jti[jti] = _now()

    def start_attempt(self, action: str, subject: str) -> tuple[str, str]:
        self._prune()
        attempt_id = secrets.token_hex(8)
        phrase = _new_phrase()
        self.attempts[attempt_id] = {
            "phrase": phrase, "action": action, "subject": subject,
            "expires_at": _now() + PHRASE_WINDOW_SECONDS, "tries": 0,
        }
        return attempt_id, phrase

    def get_attempt(self, attempt_id: str) -> Optional[dict]:
        self._prune()
        return self.attempts.get(attempt_id)

    def burn_attempt(self, attempt_id: str) -> None:
        self.attempts.pop(attempt_id, None)

    def bump_tries(self, attempt_id: str) -> int:
        att = self.attempts.get(attempt_id)
        if att is None:
            return 0
        att["tries"] += 1
        return att["tries"]


def append_audit(state_dir, action: str, detail: dict) -> None:
    path = Path(state_dir) / AUDIT_FILE
    line = {"ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "action": action, "detail": detail}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        pass


def telegram_notify(telegram_env: Optional[Path], text: str) -> bool:
    """Best-effort Telegram push via `telegram_send.py` (subprocess — this process never touches
    Telegram credentials directly). `False` (never raises) on any failure; the audit line is the
    durable record of the attempt either way."""
    if not telegram_env or not Path(telegram_env).is_file():
        return False
    script = REPO_ROOT / "seneschal" / "scripts" / "telegram_send.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--text", text, "--env-file", str(telegram_env)],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001 — a Telegram outage must never crash the supervisor
        return False
    if proc.returncode != 0:
        return False
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return False
    return bool(result.get("ok"))


class Handler(BaseHTTPRequestHandler):
    """Class-level attributes are set once by `main()` (or a test) before `serve_forever()` — every
    request handles through the SAME `state`/`cfg`/`secret`/`runner`, matching `health_listener.py`'s
    pattern (`Handler.token = ...` etc.) rather than per-instance construction (`BaseHTTPRequestHandler`
    is instantiated fresh per connection by the base server)."""

    state: SupervisorState
    secret: bytes
    cfg: BreakglassConfig
    telegram_env: Optional[Path] = None
    runner = None  # DefaultRunner() constructed lazily in _handle_verify; tests set this to a RecordingRunner

    def log_message(self, fmt, *args):  # keep the console quiet; the audit log is the record
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self) -> None:
        """See the module docstring's "CORS is deliberately permissive" note — the frontend calls this
        process cross-origin by design, and there is nothing meaningful to restrict TO since it only
        ever binds 127.0.0.1."""
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):  # noqa: N802 — the CORS preflight for POST /start and /verify
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def _read_json_body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length <= 0 or length > 65536:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None

    def _telegram(self, text: str) -> bool:
        return telegram_notify(self.telegram_env, text)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("", "/health"):
            self._send(200, {"ok": True, "service": "breakglass-supervisor"})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        route = self.path.rstrip("/")
        if route == "/start":
            self._handle_start()
        elif route == "/verify":
            self._handle_verify()
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def _handle_start(self):
        body = self._read_json_body()
        if body is None:
            self._send(400, {"ok": False, "error": "bad JSON body"})
            return
        action = body.get("action")
        if action not in ALLOWED_ACTIONS:
            self._send(400, {"ok": False, "error": f"action must be one of {ALLOWED_ACTIONS}"})
            return
        token = body.get("assertion")
        payload = verify_assertion(self.secret, token, expected_action=action) if isinstance(token, str) else None
        if payload is None:
            append_audit(self.cfg.state_dir, "breakglass.start_rejected",
                         {"action": action, "reason": "bad or expired assertion"})
            self._telegram(f"Break-glass: a '{action}' attempt was REJECTED "
                            f"(bad/expired assertion). No action taken.")
            self._send(403, {"ok": False, "error": "invalid or expired assertion"})
            return

        jti = payload["jti"]
        if self.state.is_spent(jti):
            append_audit(self.cfg.state_dir, "breakglass.start_rejected",
                         {"action": action, "reason": "assertion already used"})
            self._telegram(f"Break-glass: a '{action}' attempt reused an "
                            f"already-spent assertion. REJECTED.")
            self._send(403, {"ok": False, "error": "assertion already used"})
            return
        self.state.mark_spent(jti)

        subject = payload["sub"]
        attempt_id, phrase = self.state.start_attempt(action, subject)
        append_audit(self.cfg.state_dir, "breakglass.rung3_started",
                     {"action": action, "subject": subject, "attempt_id": attempt_id})
        sent = self._telegram(
            f"Break-glass: rungs 1-2 cleared for '{action}'. Rung 3 phrase: {phrase}\n"
            f"Type it back in the cockpit within {PHRASE_WINDOW_SECONDS // 60} minutes to proceed."
        )
        if not sent:
            self.state.burn_attempt(attempt_id)
            append_audit(self.cfg.state_dir, "breakglass.rung3_notify_failed",
                         {"action": action, "attempt_id": attempt_id})
            self._send(502, {"ok": False,
                              "error": "could not notify Telegram — rung 3 needs that channel, try again"})
            return
        self._send(200, {"ok": True, "attempt_id": attempt_id, "expires_in": PHRASE_WINDOW_SECONDS})

    def _handle_verify(self):
        body = self._read_json_body()
        if body is None:
            self._send(400, {"ok": False, "error": "bad JSON body"})
            return
        attempt_id = body.get("attempt_id")
        phrase = body.get("phrase")
        att = self.state.get_attempt(attempt_id) if isinstance(attempt_id, str) else None
        if att is None:
            append_audit(self.cfg.state_dir, "breakglass.verify_rejected",
                         {"reason": "no such attempt or it expired"})
            self._send(400, {"ok": False, "error": "no such attempt, or it expired — start over"})
            return

        if not isinstance(phrase, str) or phrase.strip() != att["phrase"]:
            tries = self.state.bump_tries(attempt_id)
            if tries >= MAX_PHRASE_ATTEMPTS:
                self.state.burn_attempt(attempt_id)
                append_audit(self.cfg.state_dir, "breakglass.rung3_failed",
                             {"action": att["action"], "reason": "too many wrong phrase attempts"})
                self._telegram(f"Break-glass: '{att['action']}' FAILED: too many wrong "
                                f"phrase attempts. No action taken.")
                self._send(403, {"ok": False, "error": "too many wrong attempts — start over"})
                return
            append_audit(self.cfg.state_dir, "breakglass.verify_wrong_phrase",
                         {"action": att["action"], "tries": tries})
            self._send(403, {"ok": False, "error": "wrong phrase",
                              "tries_remaining": MAX_PHRASE_ATTEMPTS - tries})
            return

        self.state.burn_attempt(attempt_id)
        append_audit(self.cfg.state_dir, "breakglass.rung3_verified",
                     {"action": att["action"], "subject": att["subject"]})
        runner = self.runner or DefaultRunner()
        try:
            if att["action"] == "restart":
                result = do_restart(self.cfg, runner)
            else:
                result = do_force_pull(self.cfg, runner)
        except Exception as e:  # noqa: BLE001 — never let an action crash the response
            append_audit(self.cfg.state_dir, "breakglass.action_failed",
                         {"action": att["action"], "error": str(e)})
            self._telegram(f"Break-glass: '{att['action']}' EXECUTION FAILED: {e}")
            self._send(500, {"ok": False, "error": f"action failed: {e}"})
            return
        append_audit(self.cfg.state_dir, "breakglass.action_done",
                     {"action": att["action"], "result": result})
        self._telegram(f"Break-glass: '{att['action']}' executed. Steps: "
                        + "; ".join(result.get("steps", [])))
        self._send(200, result)


def main() -> int:
    p = argparse.ArgumentParser(description="The seneschald cockpit's break-glass supervisor (stdlib-only).")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--state-dir", default=os.environ.get(
        "SENESCHAL_STATE_DIR", str(REPO_ROOT / "seneschal" / "state")))
    p.add_argument("--repo-root", default=os.environ.get(
        "SENESCHAL_REPO_ROOT", str(REPO_ROOT)))
    p.add_argument("--run-presence-cmd", default=None,
                   help="default: <repo-root>/seneschal/scripts/run-presence.cmd")
    p.add_argument("--telegram-env", default=None,
                   help="default: <repo-root>/seneschal/scripts/telegram.env")
    p.add_argument("--deploy-branch", default="main")
    args = p.parse_args()

    state_dir = Path(args.state_dir)
    repo_root = Path(args.repo_root)
    run_presence_cmd = (Path(args.run_presence_cmd) if args.run_presence_cmd
                         else repo_root / "seneschal" / "scripts" / "run-presence.cmd")
    telegram_env = (Path(args.telegram_env) if args.telegram_env
                    else repo_root / "seneschal" / "scripts" / "telegram.env")

    cfg = BreakglassConfig(repo_root=repo_root, state_dir=state_dir, run_presence_cmd=run_presence_cmd,
                           deploy_branch=args.deploy_branch)

    Handler.state = SupervisorState()
    Handler.secret = get_or_create_secret(state_dir)
    Handler.cfg = cfg
    Handler.telegram_env = telegram_env if telegram_env.is_file() else None
    Handler.runner = None  # DefaultRunner() constructed per action in _handle_verify

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"break-glass supervisor on http://{args.host}:{args.port} "
          f"(state={state_dir}, repo={repo_root}, telegram={'configured' if Handler.telegram_env else 'NOT configured'})",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
