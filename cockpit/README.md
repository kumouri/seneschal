# Seneschal Cockpit

A local-first **web observatory** over the seneschal daemon: live sessions, oneiroi, deploy health,
presence, reminders, plan usage, a graceful-restart control, a live agent-style chat pane fed by the
daemon pipe, the two model dials + the router dashboard, **Oikonomos** (the budget governor's
Thresholds panel), sleep/workout/nutrition panels + a staged Meals card, archon tiles with a
GET-only reverse proxy, and the **real OIDC auth stack** (login round trip, session issuance, the
break-glass recovery ladder, and the public decoy chat).

Four pieces:

- **`server/`** — a FastAPI backend (Python; the `cockpit` uv extras group — fastapi + uvicorn).
  Reads the daemon's `seneschal/state/*` tolerantly, holds the one outbound connection to the
  daemon's cockpit pipe, and exposes everything under `/api/*` on `127.0.0.1:8760`.
- **`web/`** — a Vite + TypeScript + React frontend. `npm run build` produces `web/dist`, which the
  backend serves as static files, so one uvicorn process serves both the API and the UI.
- **`zitadel/`** + **`breakglass/`** — the self-hosted IdP compose stack and the separate,
  stdlib-only emergency-recovery supervisor (see "Auth" below).
- **`decoy/`** — the public, unauthenticated honeypot chat, a fully isolated separate process
  (see "Auth" below and `decoy/README.md`).

**Dev-no-auth remains the default.** The real auth stack ships in this repo, but it stays off until
the owner provisions an OIDC app (see "Auth" below) — `auth.py`'s mode precedence degrades
naturally: with `COCKPIT_DEV_NO_AUTH=1` every gated route is open (the dev stub); with nothing
configured every gated route answers an honest 503 `"auth not configured"`; with
`COCKPIT_OIDC_CLIENT_ID` set, the real OIDC session gates everything. Either way the server **only
ever binds `127.0.0.1`** — the flags control the auth mode, not exposure.

**Oneiroi** (singular *oneiros*) is the display name for the per-session distillates
`seneschal/scripts/mini_dream.py` writes to `state/session-distillations.jsonl` ("mini-dreams" in
older docs — script/file names are unchanged).

## Layout

```
cockpit/
  server/   FastAPI backend (Python, uv `cockpit` extras group — fastapi + uvicorn)
    app.py           the app itself — routes (incl. /auth/*), the localhost-only middleware, the
                     lifespan-managed pipe client
    config.py        env resolution (SENESCHAL_STATE_DIR, pipe knobs, the OIDC getters)
    auth.py          mode precedence (oidc/dev/unconfigured), require_auth/require_csrf, allowlist
    oidc.py          stdlib OIDC client (discovery, PKCE, code exchange, userinfo — no JWT/JWKS lib)
    session.py       signed session-cookie + pending-login-cookie primitives (stdlib hmac/hashlib)
    _fake_oidc_server.py   in-process fake OIDC provider — the auth tests' fixture, not a test file
    pipe_client.py   the ONE outbound connection to the daemon's cockpit pipe (reconnect+backoff)
    ws_hub.py        fans daemon-relayed frames out to N browser tabs (GET /api/ws)
    transcript.py    GET /api/transcript backfill (tolerant tail of warm-transcript.jsonl)
    readers.py       tolerant readers for sessions/oneiroi/deploy-health/presence/reminders/usage/router-stats
    control.py       the graceful-restart enqueue + the cockpit-audit.jsonl append
    emotes.py        GET /api/emotes listing + file resolution (COCKPIT_EMOTE_DIR)
    model_config.py  the two-dial rank/coherence table, duplicated from seneschal/scripts/model_config.py
    governor.py      Oikonomos's SCHEMA + config load/save/rollups, duplicated from seneschal/scripts/governor.py
    health.py        tolerant reads over state/health.db (sleep/workouts/nutrition) + state/meals.json
    archons.py       the archon registry reader + GET-only reverse-proxy client
    test_parity.py   CI tripwire: asserts the duplicated model_config/governor tables still match the daemon side
    archon-registry.example.json   tracked seed for the per-install (gitignored) archon-registry.json
  web/      Vite + TypeScript + React frontend
    src/useChatSocket.ts       the chat pane's live /api/ws connection (reconnect+backoff)
    src/chatEvents.ts          pure chat.event -> per-turn folding (shared by backfill + live)
    src/emotes.tsx             minimal :shortcode: rendering
    src/components/ChatPanel.tsx, ChatComposer.tsx   the chat pane itself (incl. the force-route toggle)
    src/components/ModelDialsPanel.tsx, RouterPanel.tsx   the two dials + the router dashboard
    src/components/ThresholdsPanel.tsx   Oikonomos — schema-rendered rail/advisory knobs + spend meters
    src/components/HealthSummaryPanel.tsx, SleepPanel.tsx, WorkoutsPanel.tsx, NutritionPanel.tsx,
      MealsPanel.tsx   the health/workout/meal panel group
    src/components/ArchonsPanel.tsx   the archon tile row (from the per-install registry)
    src/components/BreakglassPage.tsx + src/breakglassApi.ts   the break-glass ladder UI (talks to
      the separate supervisor directly)
  zitadel/  the self-hosted IdP: docker-compose stack + .env.example + ZITADEL_SETUP.md (the
            one-time app-registration walkthrough)
  breakglass/  the emergency-recovery supervisor — deliberately stdlib-only, its own process/port
    supervisor.py    the HTTP server (rung 3: the Telegram phrase; executes restart/force-pull)
    actions.py       the two actions behind a Runner seam (tests never touch the real machine)
    assertion.py     the short-lived HMAC assertion minted by the backend, verified here (the ONE
                     shared module between cockpit/server and this package)
  decoy/    the public honeypot chat — separate process, zero tools/data/secrets (own README.md)
```

## Running it locally

### Backend

```
uv sync --extra cockpit                    # installs fastapi + uvicorn into .venv
$env:COCKPIT_DEV_NO_AUTH = "1"             # dev-only auth bypass — see "Auth modes" below
uv run uvicorn cockpit.server.app:app --host 127.0.0.1 --port 8760 --reload
```

`GET /api/health` should answer `{"ok": true, ...}` once it's up. The state dir defaults to this
checkout's own `seneschal/state/` — override with `SENESCHAL_STATE_DIR` to point at another checkout
or a test fixture. If the daemon (`presence.py`) is running against the same state dir, the
backend's `PipeClient` connects to its cockpit pipe automatically — no separate config needed for
the common case.

### Frontend

```
cd cockpit/web
npm ci
npm run dev
```

Vite's dev server proxies `/api/*` to `http://127.0.0.1:8760` (see `vite.config.ts`; `ws: true`
covers the chat pane's WebSocket upgrade too) — no CORS to configure. Open the printed
`http://localhost:5173`.

### Production-ish (single process)

```
cd cockpit/web && npm ci && npm run build
```

produces `cockpit/web/dist`; when that directory exists, the FastAPI app serves it as static files
at `/`, so a single `uv run uvicorn cockpit.server.app:app --host 127.0.0.1 --port 8760` origin
serves both the API and the built UI.

## Environment variables

| Var | Default | What |
|---|---|---|
| `SENESCHAL_STATE_DIR` | `<this checkout>/seneschal/state` | The daemon's state directory this backend reads. Override for dev/tests. |
| `COCKPIT_DEV_NO_AUTH` | unset | `1` allows every gated `/api/*` route (REST and `/api/ws`) without a session — the dev stub. With neither this nor OIDC set, every route except `/api/health` and `/api/auth/status` 503s `"auth not configured"` (the websocket closes with code 4401). The server only ever binds `127.0.0.1` regardless (belt-and-braces enforced twice: the uvicorn `--host` flag, plus a middleware that rejects any non-loopback client — that middleware doesn't run for websocket connections, a Starlette limitation, so `/api/ws` re-checks the same mode itself). |
| `COCKPIT_OIDC_ISSUER` / `COCKPIT_OIDC_CLIENT_ID` / `COCKPIT_OIDC_REDIRECT` / `COCKPIT_ALLOWED_USER` | see `cockpit.env.example` | **Real-auth config.** Setting `COCKPIT_OIDC_CLIENT_ID` flips `auth.auth_mode()` to `"oidc"` and turns on the login round trip (`/auth/login` → the IdP → `/auth/callback` → session cookie). Leave it unset until the OIDC app is provisioned — `zitadel/ZITADEL_SETUP.md` is the one-time walkthrough. |
| `COCKPIT_ARCHON_REGISTRY_PATH` | `<server dir>/archon-registry.json` | Override for the archon registry file (tests point this at a throwaway file). The real registry is per-install and gitignored; seed it from `archon-registry.example.json`. Absent → empty roster, one log line, no error. |
| `COCKPIT_PIPE_HOST` | `127.0.0.1` | Host of the daemon's cockpit pipe (`pipe_client.py`) — the daemon only ever binds loopback, so this should stay `127.0.0.1` outside of an unusual dev setup. |
| `COCKPIT_PIPE_PORT` | `8471` | Port of the daemon's cockpit pipe — must match `presence.py --cockpit-port` (same default). |
| `COCKPIT_PIPE_TOKEN_PATH` | `<SENESCHAL_STATE_DIR>/cockpit-pipe-token` | Override for the pipe's auth token file (`seneschal/scripts/cockpit_pipe.py` writes the real one; tests point this at a temp file). |
| `COCKPIT_EMOTE_DIR` | unset | A folder of `:shortcode:` emote images (`pog.png` → `:pog:`). `GET /api/emotes` returns an empty list — feature fully OFF — when unset. |

## Posture: localhost-only, audited, degrades honestly

- Binds `127.0.0.1` only (never `0.0.0.0`). Public exposure (a tunnel + the real auth stack) is
  deliberately deferred.
- **Writes are minimal and explicit:** the restart enqueue + its audit line, the validated
  `PUT /api/model-config` and `PUT /api/governor-config` writes (each 400s instead of writing a bad
  config, audited on every success), and the chat-fallback inbox append when the daemon pipe is
  down. Every other route is a tolerant read.
- `POST /api/control/restart` (and the WebSocket's `control.restart` frame) enqueues a **graceful**
  daemon restart the identical way `seneschal/scripts/request_control.py` does — same
  `state/control-queue.json` shape, same `defer_until_idle: true`, same action-dedupe — so the
  daemon applies it once its warm session goes idle, never mid-conversation.
  `cockpit/server/control.py` replicates that logic directly (not an import) so the cockpit stays
  its own dependency world.
- Every mutating call appends one line to `state/cockpit-audit.jsonl` (`{ts, action, detail}`),
  win or no-op (a deduped double-click still gets its own audit line).
- **The daemon pipe degrades honestly, never silently.** If the outbound connection to the daemon
  (`pipe_client.py`) is down: `chat.send` falls back to `state/cockpit-inbox.jsonl` (drained by the
  daemon's scheduler tick, ~tick latency, never lossy); `status.get` and `GET /api/status` report
  `"pipe": "down"` instead of guessing; every `chat.send`/`status.get` still gets ack'd/answered so
  the UI never just hangs.

## The daemon pipe

`cockpit/server/pipe_client.py` holds the ONE outbound WebSocket connection to `presence.py`'s
cockpit pipe (a supervised task of the daemon — `seneschal/docs/asyncio-daemon-design.md`), as a
`lifespan`-managed background task (`app.py`): connects to
`ws://<COCKPIT_PIPE_HOST>:<COCKPIT_PIPE_PORT>`, authenticates with the token at
`COCKPIT_PIPE_TOKEN_PATH`, and reconnects with exponential backoff (capped at 30s) on any drop.
Sending back to the daemon (`PipeClient.send`) is a bounded, NEVER-blocking enqueue — a
stalled/absent daemon connection can never backpressure a request handler; a full queue drops the
oldest frame (with a counter), mirroring the daemon-side `PipeHub`'s own fail-open discipline.

`GET /api/ws` (`ws_hub.py`'s `BrowserHub`) fans every daemon-relayed frame
(`chat.event`/`status`, plus locally-synthesized frames like a fallback `chat.ack` or a "pipe down"
status) out to every connected browser tab, each with its own bounded outbound queue — one
slow/wedged tab can never affect another tab, the daemon pipe, or a request handler.
Browser-originated `chat.send`/`status.get` prefer the live pipe and fall back as described above;
`control.restart` always uses the always-available file write.

## API

All under `/api/` (the archon proxy's `/archons/*` routes and the `/auth/*` login round trip are
top-level, not under `/api/`). Every route except `/api/health` and `/api/auth/status` requires a
session in `oidc` mode or `COCKPIT_DEV_NO_AUTH=1` in dev mode (mode precedence: `auth.auth_mode()`)
— `/api/ws` re-checks the same mode itself.

| Route | Reads | Notes |
|---|---|---|
| `GET /health` | — | Liveness + config echo (state dir, dev-auth flag, whether `web/dist` is built). No secrets. Public — no auth gate. |
| `GET /auth/status` | — | **PUBLIC — no auth gate.** `{"mode","authenticated","user"}` — lets the frontend show a login screen instead of a wall of 401s once real auth lands. |
| `GET /sessions` | `state/sessions/*.json` | Every entry, tolerant of garbage files, with a computed `live` flag (`daemon`/`desktop` gate at 120s TTL; `build`/`scheduled` are awareness-only at a 1h TTL). |
| `GET /oneiroi?limit=N` | `state/session-distillations.jsonl` | Last N valid records, newest first (default 20). |
| `GET /seneschald-health` | `state/seneschald-health.json` | Passthrough + a derived `last_ok_age_seconds` — the watch-the-watcher field. |
| `GET /presence` | `state/presence-context.json` | Passthrough (`at_place`/`activity`/`asleep`/`since`). |
| `GET /reminders` | `state/reminders.json` | Summarized: pending count + next 5 by `due_at`. |
| `GET /usage` | `state/metrics.jsonl` | Best-effort turns/tokens per day/model. Always `"estimated": true` (`tokens_available` tells the UI whether the schema carried tokens). |
| `GET /status` | daemon pipe (live) or `state/sessions/*.json` (fallback) | Once at least one `status` frame has arrived over the pipe, this returns that live snapshot (turn-in-flight, model, queue depth) — either way, honestly labeled `"pipe": "up"\|"down"`. |
| `GET /transcript?limit=N` | `state/warm-transcript.jsonl` | Tolerant tail backfill of the chat-pane transcript ring buffer (default 200, capped ~2000). |
| `GET /emotes` / `GET /emotes/{filename}` | `COCKPIT_EMOTE_DIR` | Emote listing / one image file; empty list (feature off) when unset; path-traversal guarded. |
| `GET /ws` (WebSocket) | daemon pipe (live) | The chat pane's transport. Streams `chat.event`/`status` frames out; accepts `chat.send`/`status.get`/`control.restart` in. |
| `POST /control/restart` | — (writes) | Enqueues the graceful restart + audits it. Same effect as the WS `control.restart` frame. |
| `GET /model-config` | `state/model-config.json` | The two dials, tolerant passthrough (`model_config.load`) — missing/corrupt file -> both fields `null`. |
| `PUT /model-config` | — (writes) | Validated write of both dials together — 400s on an unrecognized id or an incoherent pair (warm outranking the ceiling); audits every successful write. |
| `GET /router-stats?limit=N` | `state/router-log.jsonl` | Tolerant summary: verdict counts by arm (`triage`/`fable`) + the last N decisions, newest first. |
| `GET /governor-config` | `state/governor-config.json` + `state/governor-ledger.jsonl` | Oikonomos's schema-driven config (tolerant) + `governor.SCHEMA` itself + today/this-week spend rollups — one response gives the Thresholds panel its whole form and its spend meters. |
| `PUT /governor-config` | — (writes) | Partial, SCHEMA-validated update (`{"updates": {...}}`) — only the touched knobs change; an unknown knob or a bad value 400s; audits every successful write. |
| `GET /health/sleep?days=N` | `state/health.db` (`sleep_session`) | Recent nights (default 14), sessions summed per calendar night. Missing db/table -> `{"available": false, ...}`. |
| `GET /health/workouts?days=N` | `state/health.db` (`workouts`) | Recent sessions + a per-ISO-week rollup (count/minutes/kcal). Same tolerance. |
| `GET /health/nutrition?days=N` | `state/health.db` (`nutrition`) | Per-day kcal/protein/carbs/fat totals + that day's logged entries. Same tolerance. |
| `GET /health/summary` | `state/health.db` | One compact card: last night's sleep, this week's workouts, today's kcal/protein so far. |
| `GET /meals` | `state/meals.json` | The Dream-staged meal-plan/meal-idea snapshot. Absent/corrupt -> `{"available": false, "staged_at": null, "plans": []}`. |
| `GET /api/archons` | archon registry | The per-install registry + a tolerant reachability probe for each `"live"` entry. Absent registry -> empty roster. |
| `GET /archons/{id}/{path:path}` | — (proxies) | GET-only reverse proxy to `http://127.0.0.1:<port>/<path>` for a `"live"` archon. 404 unknown id, 503 not-live/no-port, 502/504 unreachable/timeout. Non-GET → 405 with a clear message (a documented limitation). |
| `GET /auth/login` (top-level) | — | Starts the OIDC round trip: builds the issuer's authorize URL (PKCE S256 + a `state` nonce), stashes the verifier/state in a short-lived signed cookie, redirects. 503 when OIDC isn't configured. |
| `GET /auth/callback` (top-level) | — (writes cookies + audit) | Validates `state`, exchanges the code (server-to-server, stdlib urllib), calls userinfo, checks the single-user allowlist, sets the session cookie, redirects to `/`. Doubles as the break-glass rungs-1-2 callback (redirects to `/#breakglass-assertion=...` instead of minting a session). |
| `GET /auth/logout` (top-level) | — (writes cookie + audit) | Clears the session cookie and redirects to `/`. Never gated, never errors. |
| `GET /api/breakglass/reauth/start?action=` | — (writes audit) | Break-glass rungs 1-2: a FRESH IdP re-auth (`prompt=login&max_age=0`) for `restart` or `force-pull`. Requires an already-authenticated session (a step-UP, not a bypass). |

## The chat pane + model dials & router

The dashboard's marquee surface (`src/components/ChatPanel.tsx` + `ChatComposer.tsx`, fed by
`src/useChatSocket.ts`): a streaming conversation view, backfilled from `GET /api/transcript` on
load and then live over `GET /api/ws`.

- **Turns**, grouped by source (`telegram`/`discord`/`cockpit` badges) and folded from the raw
  `chat.event` stream by `src/chatEvents.ts` (pure, shared between backfill and live — keyed on each
  event's `turn_id`). Each turn shows a per-turn model badge, a timestamp, collapsible tool-use
  summaries, and an error pill if the turn errored. A delegated one-shot's turn keeps its own model
  badge — the seam stays visible.
- **Turn-in-flight indicator** ("Assistant is working…" vs "idle") straight from the live `status`
  frame.
- **Composer:** Enter to send (Shift+Enter for a newline), a visible pending/acked state per
  message, disabled with an honest banner when the socket itself is down, and a separate banner —
  composer still enabled — when the *daemon pipe* is down (messages queue via the file fallback).
  The force-route checkbox sets `force_fable: true` on the `chat.send` frame — the daemon still
  refuses via its delegation gate if the max-routable-model ceiling doesn't admit it.
- **`:shortcode:` emotes** render inline when `COCKPIT_EMOTE_DIR` is configured (`src/emotes.tsx`).
- **Model dials panel** (`ModelDialsPanel.tsx`): two selects (warm model / max-routable ceiling)
  seeded from `GET /api/model-config` on first load (not re-clobbered by the background poll once
  edited), a **Save** button (`PUT`, inline validation errors), and an **Apply now** button that
  queues the same graceful restart as the header's restart control (confirm-gated).
- **Router panel** (`RouterPanel.tsx`): verdict counts per arm, the last ~10 logged decisions, and
  a volume-by-day bar chart — labeled honestly as volume, not accuracy.

## Thresholds — Oikonomos, the budget governor

`ThresholdsPanel.tsx` renders its ENTIRE form from `GET /api/governor-config`'s `schema` field
(`seneschal/scripts/governor.py`'s `SCHEMA`, mirrored in `cockpit/server/governor.py` per the
own-dependency-world posture; `test_parity.py` alarms if the two drift) — a future knob is a new
SCHEMA entry, never a UI change.

- **Grouped by `rail`/`advisory`.** Every knob is labeled which it is — code-enforced (metered,
  gated, or hard-refused) vs prompt/doc-side guidance the reasoning loop honors. Design-honesty
  rule: never let the two blur together.
- **Per-knob alert-at-% / hard-stop badge**, read straight from the schema entry.
- **Spend meters**: delegation one-shots used/remaining today, and metered tokens per model
  today/this-week — both from the same response's `rollups` (gated on the owner's local calendar-day
  boundaries — the after-midnight-is-still-yesterday house rule).
- **Save** sends the whole edited form as a partial `PUT`; a rejected value surfaces the backend's
  validation message inline. The form is seeded from the backend exactly once — the ~5s background
  poll never clobbers in-progress edits.

## Health / workouts / meals

Five panels reading five tolerant, read-only endpoints (`cockpit/server/health.py`) — all degrade
to an honest empty/`available: false` state rather than 500ing on a missing `state/health.db` or a
missing table. `HealthSummaryPanel` (the compact card), `SleepPanel` (bar-per-night sparkline),
`WorkoutsPanel` (this-week rollup + recent sessions), `NutritionPanel` (today's macros + history),
`MealsPanel` (the Dream-staged meal-plan snapshot as link cards). No charting library — the visuals
are plain CSS width percentages.

## Archon tiles + proxy

`cockpit/server/archon-registry.json` (**per-install, gitignored** — seed from the tracked
`archon-registry.example.json`) mirrors `seneschal/references/archons.md`'s port table:
`{"<id>": {"title", "port", "ui_port"?, "status"}}`. `GET /api/archons` (`archons.py`) reads it plus
a tolerant reachability probe for every `"live"` entry — a probe failure just reports
`"reachable": false`, never 500s; an absent registry is an empty roster, the normal state of a fresh
checkout. The **Archons** tile row (`ArchonsPanel.tsx`) renders a card per entry: a live archon
opens its proxied UI in a new tab; a reserved one shows a greyed placeholder.

`GET /archons/{id}/{path:path}` is a **GET-only** reverse proxy to `http://127.0.0.1:<port>/<path>`
over stdlib `urllib.request` (body cap, content-type passthrough, sensible timeouts) — archons never
face the internet directly. **A documented limitation:** an archon needing a write
(POST/PUT/PATCH/DELETE) would need this widened — those methods 405 with a clear message rather
than silently failing.

## Auth

**Dev-no-auth remains the default** — a fresh install needs none of this section. Enabling real
auth is three opt-in pieces, each optional past the first:

1. **Stand up Zitadel (the IdP):** `cockpit/zitadel/` — copy `.env.example` → `.env` (generate real
   secrets; the file has a generation note), `docker compose -p seneschald-zitadel up -d`, then the
   one-time OIDC app registration + first-login walkthrough in `zitadel/ZITADEL_SETUP.md`.
2. **Wire the env config:** copy `server/cockpit.env.example` → `server/cockpit.env` (gitignored)
   and fill `COCKPIT_OIDC_ISSUER` / `COCKPIT_OIDC_CLIENT_ID` / `COCKPIT_OIDC_REDIRECT` /
   `COCKPIT_ALLOWED_USER`. Setting the client id is the flip that turns real auth on; restart the
   backend with the file loaded (`--env-file cockpit/server/cockpit.env`, or export the vars).
3. **The opt-in extras:**
   - **The decoy** (`decoy/`) — the public, unauthenticated honeypot chat: a fully separate
     process (default `127.0.0.1:8490`) with zero tools, zero data access, zero shared secrets;
     prompt-injection against it is inert by construction. Run/env story: `decoy/README.md`.
   - **Break-glass** (`breakglass/` + the cockpit UI's Break-glass page) — see "Break-glass" below.

### Auth modes

`auth.auth_mode()` picks ONE of three, freshly on every call (no caching, no restart needed to pick
up an env change):

1. **`oidc`** — `COCKPIT_OIDC_CLIENT_ID` is set. Real session-cookie auth: the authorization-code +
   PKCE round trip (`/auth/login` → the issuer → `/auth/callback`), the userinfo call as the trust
   anchor (deliberately no local JWT/JWKS verification — see `auth.py`'s module docstring), a
   single-user allowlist (`COCKPIT_ALLOWED_USER`), and a signed HttpOnly SameSite=Strict session
   cookie with sliding expiry.
2. **`dev`** — no OIDC config, but `COCKPIT_DEV_NO_AUTH=1`. The dev stub: every gated route open,
   no session, no CSRF check (there's no session to hijack).
3. **`unconfigured`** — neither. Every gated route 503s `"auth not configured"`.

**CSRF:** every **mutating** route (`POST /api/control/restart`, `PUT /api/model-config`,
`PUT /api/governor-config`) also depends on `require_csrf`, which demands a custom header
(`X-Cockpit-Requested-With: cockpit` — `cockpit/web/src/api.ts` sends it on every request) — a no-op
outside `oidc` mode. Full reasoning for why the header + SameSite=Strict suffice: `auth.py`'s
module docstring.

### Break-glass

The emergency ladder for a wedged daemon the auto-update task can't fix — three rungs: a **fresh
IdP re-auth** (password + TOTP, `prompt=login&max_age=0`; rungs 1-2, served by the backend's
`/api/breakglass/reauth/start` + the `bg`-flagged `/auth/callback`, which mint a short-lived
single-use HMAC **assertion**), then a **one-time phrase over Telegram** typed back within 5
minutes (rung 3, served by the SEPARATE supervisor). Two actions: `restart` (kill + relaunch the
daemon) and `force-pull` (hard-reset the live checkout to the deploy branch first — destructive
only to uncommitted tracked changes; `state/` is gitignored and survives). Every rung, success or
failure, is appended to `state/breakglass-audit.jsonl` and pushed to Telegram.

The supervisor is **deliberately its own stdlib-only process** (no fastapi — it must survive the
cockpit backend, the venv, and the daemon all being broken):

```
python cockpit/breakglass/supervisor.py --port 8499
```

The frontend's Break-glass page talks to it directly (`http://127.0.0.1:8499`, never proxied
through `/api/*`). Full trust-chain writeup: `breakglass/supervisor.py`'s module docstring.

## Testing

```
uv sync --extra cockpit --group test
uv run python -m unittest discover -s cockpit/server -p "test_*.py"
uv run python -m unittest discover -s cockpit/decoy -p "test_*.py"
uv run python -m unittest discover -s cockpit/breakglass -p "test_*.py"
cd cockpit/web && npm ci && npm run typecheck && npm run build
```

The backend + decoy tests SKIP (not error) when fastapi isn't installed, so the repo's stdlib suite
(`python -m unittest discover -s seneschal/scripts`) stays green on a bare interpreter; the
break-glass suite is stdlib-only and runs anywhere. `test_parity.py` runs unconditionally — it's
the tripwire that fires when the hand-duplicated `model_config.py`/`governor.py` tables drift from
their `seneschal/scripts/` originals. The auth tests run against a real in-process fake OIDC server
(`server/_fake_oidc_server.py`) — no network, no real IdP needed.

## What's genuinely next

- **Public exposure:** a tunnel in front of the cockpit (+ the decoy as the public face) — a
  deliberately separate step. Everything binds `127.0.0.1` until then; flip the session cookie's
  `secure` flag in the same change that stands up TLS (see `auth.py`).
