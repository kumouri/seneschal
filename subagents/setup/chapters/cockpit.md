# Chapter: cockpit

The local web observatory — live chat over the daemon pipe, the model dials, the governor's
Thresholds panel, health panels, archon tiles (`cockpit/README.md`). **Optional**, and it
carries real build steps, so it opens with an enable question and every install command is
individually confirmed.

## 0 — Enable question

"Want the cockpit — a localhost-only web dashboard over the daemon (live chat, model dials,
budget thresholds, health panels)? It needs `uv` and Node, and adds two Python packages to
the optional venv extra." Decline →

```
python seneschal/scripts/setup_state.py mark cockpit declined --summary "no cockpit; Telegram/Discord + the CLI remain the surfaces"
python -c "import sys; sys.path.insert(0, 'seneschal/scripts'); import setup_state; s = setup_state.load(); s['features']['cockpit'] = False; setup_state.save(s)"
```

## 1 — Backend deps (confirm-to-run)

Check `uv --version` (preflight's inventory already knows). Then, confirmed:

```
uv sync --extra cockpit
```

— installs fastapi + uvicorn into `.venv` as the optional extra (the daemon's own
`uv sync --frozen` never pulls these in; the prod venv stays lean). No `uv` → offer its
install (per-OS, confirmed) or hold the chapter (`blocked --summary "no uv"`).

## 2 — Frontend build (confirm-to-run)

Needs Node 22+ (`node --version`). Confirmed:

```
cd cockpit/web && npm ci && npm run build
```

— produces `cockpit/web/dist`, which the backend serves statically, so one process serves
API + UI. No Node / build fails → the API still works without the UI; offer to hold
(`blocked`) or continue backend-only (say which).

## 3 — cockpit.env (dev-no-auth default)

Render `cockpit/server/cockpit.env` from its example via the manifest (id `cockpit` —
`format: env`, so `setup_env.py` handles it; this chapter is its `handled_by` owner):
payload `{"manifest_id": "cockpit", "values": {}}` through the stdin-payload pattern from
the env walker — an empty `values` renders the example's defaults, which is exactly right.

State the OIDC warning from `cockpit/README.md` plainly: **this build is dev-no-auth only.**
The real auth stack (OIDC login round trip, sessions, break-glass) is a deferred follow-up —
**leave `COCKPIT_OIDC_CLIENT_ID` unset** (setting it flips auth mode to `oidc` with no login
routes to serve it, and every gated route just 401s). The server only ever binds
`127.0.0.1` either way; the session-cookie secret is deliberately not an env var
(auto-generated to `state/cockpit-session-secret`).

## 4 — Launch + what they'll see

How to run it (the owner's step, not the wizard's):

```
# Windows (PowerShell):  $env:COCKPIT_DEV_NO_AUTH = "1"
# Linux / macOS:         export COCKPIT_DEV_NO_AUTH=1
uv run uvicorn cockpit.server.app:app --host 127.0.0.1 --port 8760
```

Then `http://127.0.0.1:8760` — the dashboard panels render immediately from
`seneschal/state/*` (tolerantly — a fresh install shows honest empty states); the **chat
pane and live status need the daemon running** (its cockpit pipe), which is the daemon
chapter's territory — until that chapter lands, `seneschal/scripts/SCHEDULING.md` is the
by-hand path. `GET /api/health` answering `{"ok": true, ...}` is the smoke test.

## Close

```
python seneschal/scripts/setup_state.py mark cockpit done --artifacts cockpit/server/cockpit.env --summary "built + env rendered; dev-no-auth; launch line shown"
python -c "import sys; sys.path.insert(0, 'seneschal/scripts'); import setup_state; s = setup_state.load(); s['features']['cockpit'] = True; setup_state.save(s)"
```
