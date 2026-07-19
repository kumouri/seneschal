# The Seneschal Cockpit — design spec

**Status: BUILT** — v1 (read-only monitor), v2 (daemon pipe + chat pane), v3 (model dials + Fable
delegation), v3.5 (Oikonomos, the budget governor), v4 (health/workout/meal panels + store-staged
meals), and **v5 (the real OIDC auth stack, the archon SSO proxy, the break-glass recovery ladder,
and the decoy)** have shipped. Public exposure (a tunnel + DNS) remains deferred — everything binds
`127.0.0.1`, and dev-no-auth remains the default until the owner provisions an OIDC app (see
`cockpit/README.md` for the shipped surface, which is authoritative where this spec and the build
differ).

The cockpit is the web observatory for the assistant's whole local-first world: watch the live sessions
and their **oneiroi** (the per-session distillates `mini_dream.py` writes — "mini-dreams" in older
docs; script/file names are unchanged, *oneiroi* is the display name; **canonical pronunciation is the
ancient Greek — "oh-NAY-roy"**, singular *oneiros*), talk to the warm session from a real agent-style
chat pane, see health/workout/meal data, manage the model dials, reach every archon UI through one
login, and recover a wedged daemon from behind a break-glass ladder.

## Rulings

1. **Public exposure is deferred.** The design anticipates a tunnel in front of the cockpit, but
   nothing is exposed yet — everything binds `127.0.0.1`.
2. **An external IdP fronts the authed surface** (Zitadel, self-hosted — see "Auth" below): the
   login round trip, session issuance, and the break-glass ladder all ship. The OIDC app
   registration is a documented human step (`cockpit/zitadel/ZITADEL_SETUP.md`); until the owner
   runs it, the cockpit runs dev-no-auth (`COCKPIT_DEV_NO_AUTH=1`) or answers an honest 503.
3. Dependency ruling **blessed**: the cockpit is its own dependency world (`cockpit/`),
   FastAPI/uvicorn backend + Vite/TypeScript frontend. The daemon's stdlib-first rule is untouched.
4. **Two dials, flipped:** the owner picks the **warm model** and the **max routable model**
   separately. The warm session always runs the warm model and owns the conversation; when a turn
   needs more, it **delegates up** — a Fable one-shot called with the thread context — triggered by
   the router's verdict, the warm session's own judgment, or the `!fable` force-route (bypasses the
   classifier, never the gate). The max-routable dial is the hard ceiling on every delegation; when
   it doesn't admit Fable, **the fable arm doesn't even run**.
5. Break-glass ladder (shipped with ruling 2): **password re-entry → TOTP → a one-time phrase over
   an out-of-band channel** (Telegram).
6. The phone app's health feed extends to export **exercise AND nutrition/meal** data via Health
   Connect. Meal-plan ideas live in the store today; a future health/nutrition archon (unminted)
   becomes their source later — the cockpit reserves its tile ("the nutrition desk").
7. **A budget/pricing tier joins the advisor chain** — the **Oikonomos** advisor (budget governor):
   its own phase, plus a cockpit **Thresholds** section for its knobs (max tokens, effort, turn
   checkpoints, total-turn caps, per-model budgets, …).
8. Oneiroi's canonical pronunciation is the ancient one — "oh-NAY-roy" — stated in `README.md`.
9. The emote folder ships as cockpit `:shortcode:` packs.

Standing assumptions (confirmed): single-user forever; the backend is a resident process on the
desktop next to the daemon (no hosted brain — all interesting data is local + gitignored); every
mutating control rides the existing act-low/ask-high gate; model changes apply at next warm-session
spawn (with an "apply now" that queues a graceful restart); break-glass will be served by a
**separate tiny supervisor**, never the daemon itself; cockpit chat and Telegram are **one
conversation**.

## Architecture

```
              Browser (localhost — public exposure deferred)
                           │
                 ┌─────────▼──────────┐
                 │ cockpit backend    │────────► localhost:<archon ports>
                 │ FastAPI/uvicorn    │          (archons.md port registry;
                 │ + Vite/TS frontend │◄──────    GET-only reverse proxy)
                 └────┬──────────┬────┘
                      │          │ read-only file reads
         localhost ws │          ▼
         (token-auth) │   seneschal/state/* (sessions/, oneiroi log,
                      │   seneschald-health, reminders, presence-context,
                      ▼   health.db, metrics, router-log, approvals)
            ┌──────────────────┐
            │ presence.py      │
            │ (the daemon)     │
            └──────────────────┘
```

- **`cockpit/`** (a top-level sub-project, structured like `phone/`):
  - `cockpit/server/` — Python backend in the uv venv. New deps go in a `cockpit`
    optional-dependency group in `pyproject.toml` (`fastapi`, `uvicorn`); the daemon itself still
    imports only stdlib + `websockets`.
  - `cockpit/web/` — Vite + TypeScript frontend (own `package.json`; CI extends the existing TS
    matrix alongside `runner/` and `phone/`).
- **Posture: ALL reads are read-only.** The backend reads `seneschal/state/*` tolerantly; its only
  writes are the sanctioned, audited ones (the graceful-restart enqueue, the validated
  model-config/governor-config PUTs, the chat-fallback inbox append) — see `cockpit/README.md`.
- The deferred public tier (ruling 1): a tunnel maps a domain → localhost, with the IdP session
  (authorization code + PKCE, signed HttpOnly session cookie, CSRF-protected) in front of everything
  but public liveness, and provider-side WAF/rate-limits in front of that.

## Auth (Zitadel) & the archon SSO portal — v5, shipped

The full stack ships: `auth.py`'s mode precedence, `oidc.py`'s stdlib OIDC client, `session.py`'s
signed-cookie primitives, the `/auth/*` routes in `app.py`, the CSRF header, the break-glass
supervisor (`cockpit/breakglass/`), and the IdP compose stack (`cockpit/zitadel/`).
`cockpit/README.md` → "Auth" documents the enablement path and how the build degrades without it
(dev stub with `COCKPIT_DEV_NO_AUTH=1`; honest 503 when nothing is configured;
`COCKPIT_OIDC_CLIENT_ID` stays unset until the OIDC app is provisioned).

The design as built:

- **An external IdP** (**Zitadel**, self-hosted — `cockpit/zitadel/docker-compose.yml`) is the
  identity source; the OIDC app registration is a documented human step
  (`cockpit/zitadel/ZITADEL_SETUP.md`) — no client id exists until the owner runs through it.
- **Authorization code + PKCE, no local JWT/JWKS verification.** `GET /auth/login` builds the
  authorize URL (S256 challenge + a `state` nonce) and redirects; `GET /auth/callback` validates
  `state`, exchanges the code for tokens (stdlib `urllib.request`, server-to-server), then calls the
  IdP's **userinfo** endpoint with the access token to establish identity. That token exchange +
  userinfo call — not a JWT signature check — is the whole trust chain: both are the same "the
  issuer says so" guarantee a JWKS verification would rely on, without a JWT/JWKS library on the
  cockpit's dependency list (ruling 3). Full reasoning: `cockpit/server/auth.py`'s module docstring.
- **Single-user allowlist:** the userinfo `preferred_username` (or its `sub`) must match
  `COCKPIT_ALLOWED_USER` — anyone else gets a polite, audited 403.
- **Session:** a signed HttpOnly cookie (stdlib `hmac`/`hashlib` — `cockpit/server/session.py`;
  secret auto-generated to a gitignored `state/cockpit-session-secret`), SameSite=Strict, sliding
  expiry. **CSRF:** the `state` param on login plus a custom header
  (`X-Cockpit-Requested-With: cockpit`) every mutating route requires.
- **Break-glass** (ruling 5): recover a wedged daemon remotely — the failure the auto-update task
  can't fix. A three-rung ladder (fresh IdP re-auth → TOTP in the same step-up → a one-time phrase
  delivered over an out-of-band channel and typed back), served by a **separate stdlib-only
  supervisor process** (`cockpit/breakglass/supervisor.py`, default port 8499) so a broken
  daemon/venv/cockpit can't be asked to fix itself; every attempt audit-logged
  (`state/breakglass-audit.jsonl`) and pushed to Telegram. Restart-only is the lighter action;
  force-pull (destructive only to uncommitted tracked changes — `state/` is gitignored and
  survives) sits behind an extra confirmation.
- All auth events audit-logged to `state/cockpit-audit.jsonl`.

## Archon SSO tiles + proxy

Shipped (the proxy rides the same auth gate as everything else — the real OIDC session once
configured, the dev stub until then): the
cockpit is the intended **only** front door to archon UIs. `cockpit/server/archon-registry.json`
(**per-install, gitignored** — seed from the tracked `archon-registry.example.json`) mirrors
`seneschal/references/archons.md`'s port table; `GET /api/archons` reads it plus a tolerant
reachability probe for every `"live"` entry, and the **Archons** tile row renders a card per entry.
`GET /archons/{id}/{path}` reverse-proxies **GET-only** to `http://127.0.0.1:<port>/<path>` — archons
never face the internet directly; a future archon needing a write (POST/PUT/…) would need this
widened (a documented limitation; those methods 405 with a clear message).

## The daemon pipe (cockpit ⇄ presence.py)

The **sixth supervised task** in the daemon's reactive core (`asyncio-daemon-design.md`): a
**localhost-only WebSocket server** (the already-sanctioned `websockets` lib), token-auth via a
gitignored `state/cockpit-pipe-token`. One pipe powers:

- **`chat.send`** — cockpit → daemon: a message into the *same* chat queue as Telegram/Discord (the
  cockpit is the third mouth of one brain; one conversation, visible from every surface).
- **`chat.event`** — daemon → cockpit: transcript stream (deltas, tool-use summaries, usage) for the
  agent-style chat pane. Also teed to a ring buffer `state/warm-transcript.jsonl` (gitignored,
  size-capped) so the cockpit can backfill after reconnect.
- **`status`** — turn-in-flight, warm-session up/winding-down, current model, queue depths.
- **Controls** — graceful-restart request (act-low; same path as `request_restart.py`) with ack.

Fallback when the pipe is down: file queues (`state/control-queue`, plus a
`state/cockpit-inbox.jsonl` drained by the scheduler tick) — degraded to ~5 s latency, never lossy.
Everything the cockpit only *reads* (registry, oneiroi, health, metrics, router log, approvals,
seneschald-health) comes straight from `state/` files read-only; the pipe is for live events and
writes the daemon owns.

**Fan-out discipline:** the daemon serves **exactly one pipe client — the cockpit backend** — which
multiplexes to N browser sessions. The daemon never grows client bookkeeping; the sixth task stays
trivially small. (Process-split rationale, for the record: supervised tasks are the concurrency
model *inside* the daemon for small I/O-bound loops sharing one failure domain; separate processes
are used where the domain differs — internet exposure (backend), lifecycle independence (the future
break-glass supervisor must outlive a daemon crash), CPU-bound work (already subprocesses: the
`claude` CLI, scripts). The pipe endpoint lives in-process only because the warm session it fronts
does.)

## Model dials & Fable delegation

- **`state/model-config.json`** (gitignored; seed `state/model-config.example.json`):
  `{"warm_model": "...", "max_routable_model": "...", "updated_at": ...}` — **two dials, set
  separately** in the cockpit. No tracked file changes to switch models. The daemon reads
  `warm_model` at warm-session spawn ("apply now" queues a graceful restart);
  `max_routable_model` is read live, per turn. Validation rejects an incoherent pair (warm model
  above the ceiling).
- **The warm session owns the conversation.** It always runs `warm_model`; continuity — the thread,
  the persona, the approval gate — lives there and never migrates.
- **Delegating up:** when a turn needs more than the warm model, the warm session calls a
  **Fable one-shot** (`claude -p --model <fable id>`) seeded with the thread context; the result
  returns through the warm session and is badged in the transcript (per-turn model badge — the
  seams stay visible). Triggers, any of:
  1. the **router's fable arm** (`router.py`): classifies inbound escalations **standard vs
     Fable-level** (≈ the warm model unlikely to do it well, or Fable substantially better — deep
     synthesis, long-horizon planning, hard debugging) and attaches the verdict as a *hint* the
     warm session honors;
  2. the **warm session's own judgment** mid-turn (the problem turns out bigger than it looked);
  3. **force-route** — a "send to Fable" toggle on the cockpit composer; over Telegram/Discord, a
     `!fable` message prefix. Bypasses the classifier, **never** the approval gate.
- **The ceiling binds every delegation.** A non-Fable `max_routable_model` ⇒ no Fable call ever
  happens, by any trigger; the fable arm only activates when the ceiling admits Fable.
- Trivial-vs-escalate local handling stays **shadow-only** until separately graduated on evidence;
  fable-arm verdicts log to `router-log.jsonl` alongside, and the cockpit charts both (the phase-2
  evidence dashboard).

## Oikonomos — the budget governor (advisor + cockpit Thresholds) — v3.5, shipped

A **pricing/budget tier** in the Advisor Chain (οἰκονόμος, the household steward — "economy" is its
descendant): slots at **order 15**, between Orientation (10) and Retrieval (20), so its envelope
wraps everything a turn spends. `in:` computes the turn's **budget envelope** from config +
spend-to-date; `out:` meters actuals into `state/governor-ledger.jsonl`, fires threshold alerts, and
(for the advisory knobs) is the reasoning loop's own job to honor turn checkpoints. Full spec, the
rails-vs-advisory split table, and per-mode composition: `../references/advisor-chain.md`;
advisor-table row: `../SKILL.md`.

**Design honesty — rails vs advisory.** Not every knob is mechanically enforced, and pretending
otherwise would be worse than not shipping it. **Code-backed rails** (`seneschal/scripts/governor.py`,
testable): the Fable-delegation quota stack (daily count, per-conversation count, concurrency) and
per-model token metering + threshold alerting — with the caveat that **only Fable's own budget is
also a hard block** (via the fable_oneshot gate); the warm session's own turn loop has no
interruptible point mid-stream, so its budgets stay meter-and-alert only. **Advisory policy**
(prompt/doc-side guidance the reasoning loop honors, not enforced in code): per-turn max output
tokens, reasoning-effort tier per mode, turn checkpoints, total-turn cap, context-fill wind-down,
proactive-push rate cap. The cockpit's Thresholds panel labels every knob `rail` or `advisory` right
on the form.

- **Config:** `state/governor-config.json` (gitignored; tracked `governor-config.example.json`).
  **Schema-driven** — `governor.SCHEMA` is a plain dict the cockpit **Thresholds** panel renders its
  form from, so adding a knob never needs UI rework.
- **Spend ledger:** `state/governor-ledger.jsonl` (gitignored; tracked `.example`) — one line per
  governed spend event; `governor.rollups()` computes day/week totals gated on the **owner's local
  calendar-day boundaries** (the after-midnight-is-still-yesterday house rule, via `tz_common`).
- **Knobs shipped:**
  - per-turn max output tokens (advisory); reasoning-effort tier **per mode** (advisory — a
    per-model axis can extend the same dict-shaped knob later);
  - **turn checkpoints** — N turns of autonomous work before pausing to ask "continue?" (advisory);
  - total-turn cap per conversation (advisory);
  - daily + weekly token budgets, **per model** (rail: metered + alerting) — Fable's own budget is
    also hard-blocking, see above;
  - **Fable one-shots per day** (rail, hard); **max Fable one-shots per conversation** (rail, hard,
    cumulative over the conversation's lifetime); **delegation concurrency** (rail, hard, backed by
    an in-flight counter);
  - context-fill wind-down threshold (advisory);
  - proactive-push rate cap (advisory);
  - **per-knob `alert_at_pct` + `hard_stop`** — schema attributes on every threshold-bearing knob
    (not a separate knob), rendered as a badge on the Thresholds form.
  - **Deferred from the original ask:** a distinct **per-mode token-budget ceiling** (Watch/Dream/
    Brief each with its own numeric spend cap, separate from the effort-tier knob) wasn't built as
    its own SCHEMA entry — the shipped set covers per-model budgets + per-mode effort tiers instead.
    Easy to add later (one more `dict_int` SCHEMA entry) if the finer axis is wanted.
- **Wired:** `fable_delegate.py` consults `governor.check("fable_oneshot", ...)` before spawning and
  refuses with a clear, complete reason on quota (fail-open on governor trouble); `presence.py`'s
  stream tee (`_make_stream_tee` → `_governor_meter_turn_usage`) meters every turn's token usage and
  self-pushes at most one Telegram alert per knob per 6h. Cockpit: `GET`/`PUT /api/governor-config`
  (`cockpit/server/governor.py`, duplicated SCHEMA per ruling 3) + the **Thresholds** panel
  (`cockpit/web/src/components/ThresholdsPanel.tsx`).

## Data surfaces (post-login)

The design's panel set; `cockpit/README.md`'s API table is authoritative for what the current build
exposes (approval actions, ack buttons, and quiet controls are design targets, not yet routes).

| Panel | Source | Notes |
|---|---|---|
| Live sessions | `state/sessions/*.json` | who's live, `working_on`, TTL freshness |
| Oneiroi feed | `state/session-distillations.jsonl` | display name **oneiroi**; per-session distillates |
| Chat (the main event) | daemon pipe | agent-style pane: streaming turns, tool-use detail, per-turn model badge, turn-in-flight |
| Transcript | pipe + `warm-transcript.jsonl` | live + recent backfill |
| Health / workouts / meals | `state/health.db` (+ store-staged `state/meals.json`) | sleep, exercise + nutrition all land in `health.db`; the panels read `GET /api/health/{sleep,workouts,nutrition,summary}` + `GET /api/meals` |
| Deploy health | `state/seneschald-health.json` | `last_ok` is the watch-the-watcher field |
| Reminders + quiet window | `state/reminders.json`, `quiet.json` | pending summary; ack buttons + quiet-set/clear are design targets |
| Pending approvals | `state/pending-approvals.json` | first-class gate surface: preview, approve/reject (= `send a7`/`drop a7`) — design target |
| Model dials | `state/model-config.json` | warm + max-routable selectors, apply-now |
| Thresholds (Oikonomos) | `state/governor-config.json` | schema-driven budget knobs + spend meters |
| Plan usage (est.) | `state/metrics.jsonl` + stream usage | **best-effort** tokens/turns by model — no official quota API; labeled estimated |
| Context gauge | stream usage events | warm-session context fill — design target |
| Router record | `state/router-log.jsonl` | verdict record over time, both arms |
| Archon tiles | `archons.md` registry | proxied UIs; per-install registry |
| Presence | `state/presence-context.json` | where/what/asleep(informational) |
| Audit log | `state/cockpit-audit.jsonl` | every mutating action + auth events (login/logout/forbidden/break-glass) |

Chat pane extra: **custom emoji** — the renderer supports `:shortcode:` packs from a user-supplied
emote directory (`COCKPIT_EMOTE_DIR`; `GET /api/emotes`). Off — an empty list — when unset.

## Health pipeline extension (v4) — shipped

The phone app (`phone/android/`) adds Health Connect **ExerciseSession** and **Nutrition** record
export alongside sleep, POSTing NDJSON to `health_listener.py` as before; `health_import.py` grows
`workouts` and `nutrition` tables in `state/health.db`. Meal-plan *ideas*: Dream stages current
meal pages from the store into `state/meals.json` nightly (no cockpit→store coupling); the future
nutrition archon replaces that feed when minted (its mint is a separate ask-high conversation).

**Export + import half.** `HealthSyncWorker` reads `ExerciseSessionRecord` and `NutritionRecord`
alongside sleep/heart-rate/SpO₂/steps, over the same NDJSON pipe (`t: "workout"` /
`t: "nutrition"`); a workout's calories/distance are folded in from Health Connect's separate
`TotalCaloriesBurnedRecord`/`DistanceRecord` entries (not fields on the session itself) before the
phone ever posts. `health_import.py` gained the `workouts`/`nutrition` tables (live-feed only,
idempotent upserts, tolerant of malformed/unknown lines) — see `seneschal/scripts/HEALTH_SETUP.md`
and `phone/android/README.md`.

**Panels + meal staging half.** Backend: five tolerant, read-only endpoints in
`cockpit/server/health.py` — `GET /api/health/sleep` (recent nights, sessions summed per calendar
night, primary stats from that night's longest session), `GET /api/health/workouts` (recent
sessions + a per-ISO-week rollup of count/minutes/kcal), `GET /api/health/nutrition` (per-day
kcal/protein/carbs/fat totals + that day's entries), `GET /api/health/summary` (one compact card:
last night's sleep, this week's workouts, today's kcal/protein so far), and `GET /api/meals` (the
Dream-staged snapshot below). A missing `health.db` or a missing/older-schema table never 500s —
every endpoint answers an honest `"available": false` instead. Frontend: `HealthSummaryPanel`,
`SleepPanel` (a simple bar sparkline of recent nights), `WorkoutsPanel` (week rollup + recent
list), `NutritionPanel` (today's tiles + short history), and `MealsPanel` (staged plans as link
cards; empty state: the reserved nutrition desk). Dream-side staging: `seneschal/SKILL.md`'s Dream
mode searches the store for meal-plan/meal-idea pages by title/tag convention (no hardcoded
database ids — none exist yet) and overwrites `state/meals.json` (`{"staged_at", "plans":
[{"title","url","summary"?,"tags"?}, ...]}`); no matches → still writes a valid empty-`plans` file;
the store unreachable → skips the write, leaving the prior `staged_at` standing so the UI shows
staleness honestly. Seed: `seneschal/state/meals.example.json`.

## The decoy — shipped

- Separate process, public-by-design, **zero tools, zero data access, zero shared secrets** with
  the authed backend; talks only to local Ollama (`gemma4:12b` by default — env-swappable via
  `DECOY_MODEL`).
- In character as **the steward at the reception desk**: dry, unhelpful-by-design about anything
  real, stocked with easter eggs (`cockpit/decoy/eggs.md`, tracked — the fun is reviewable).
- Hard per-IP and global rate limits, small context, no persistence of visitor chats beyond an
  anonymized counter. Prompt-injection is *expected* and inert: there is nothing to steal and no
  tool to fire — the page exists to demonstrate exactly that.
- Ships as `cockpit/decoy/` — its own FastAPI app + uvicorn entry (default `127.0.0.1:8490`,
  separate from the authed backend's `8760`), persona in `receptionist.md` + `eggs.md` (both
  tracked), rate limiting + request-size guards in `ratelimit.py`, the Ollama call in
  `ollama_client.py` (stdlib `urllib`, no new dependency). Still **local-only** (ruling 1) — no
  tunnel yet. See `cockpit/decoy/README.md` for the full run/env story.

## Threat model

- **Assets:** the daemon's control surface; the owner's personal data (health, transcripts,
  approvals, ledgers); the machine itself.
- **Adversaries (once public):** internet-random scanners; a targeted attacker with the domain; a
  browser-session thief; a prompt-injector on the decoy; a compromised archon UI.
- **Mitigations:** localhost-only binds until public exposure (belt-and-braces: the uvicorn
  host flag plus a middleware rejecting non-loopback clients); the authed surface behind
  OIDC + PKCE + TOTP; the decoy fully isolated (own process, no tools/data/secrets); mutations
  CSRF-protected + audit-logged; break-glass = a 3-factor
  ladder incl. an out-of-band channel; daemon pipe localhost-only + token; archons never
  internet-facing (proxied under the session); all state gitignored/local; secrets never in the
  repo; the break-glass supervisor is the only process that can hard-reset, and it can't be reached
  without the ladder.

## Phases

| Phase | Ships | Depends on |
|---|---|---|
| **v0** | `cockpit/` skeleton (backend+frontend+CI), dev-no-auth mode precedence, the auth seams | — |
| **v1** ✅ shipped | Read-only monitor: sessions, oneiroi, seneschald-health, presence, reminders view, plan-usage gauge, turn-in-flight — + the graceful-restart button | v0 |
| **v2** ✅ shipped | Daemon pipe; live transcript; the agent-style chat pane (third inbound surface); custom emoji | v1 |
| **v3** ✅ shipped | Model dials (two-dial `model-config.json` + spawn wiring) + Fable delegation + force-route + router dashboard | v2 |
| **v3.5** ✅ shipped | **Oikonomos**: the budget-governor advisor + `governor-config.json` + the cockpit Thresholds section | v3 |
| **v4** ✅ shipped | Phone-app exercise+nutrition export; health/workout/meal cockpit panels; store meal staging (Dream) | v1 |
| **v5** ✅ shipped | Real OIDC auth (auth-code + PKCE, session cookie, CSRF); archon SSO login; break-glass supervisor + ladder; the Zitadel compose stack; the decoy | v0, v3 |
| **Public exposure** — deferred | A tunnel + DNS in front of the cockpit (the decoy as the public face) | v5 |

Each phase lands as a normal PR (merge-commit, green CI only); daemon-side changes ride the Path A
auto-reload. Docs updated in the same PRs (`asyncio-daemon-design.md` for the sixth task,
`README.md`/`CLAUDE.md` tables, `ROUTER_SETUP.md` for the fable-arm semantics,
`../references/advisor-chain.md` for Oikonomos).

## Open items

- **Public exposure** — a tunnel + DNS in front of the cockpit. When it happens: flip the session
  cookie's `secure` flag (`auth.py`), move Zitadel's `EXTERNALDOMAIN`/`EXTERNALSECURE` to the real
  domain + TLS, and turn the Zitadel app's dev-mode/allow-HTTP back off — all in the same change as
  the tunnel. (Tooling
  note for that day: tunnels are `cloudflared tunnel create/route dns`, not wrangler — `route dns`
  mints the CNAME itself.)
- **Ops note:** the cockpit backend runs from a **non-daemon checkout/venv** (with the state dir
  pointed at the live `seneschal/state/`). Reason: the auto-update task runs `uv sync --frozen` on
  the live checkout every ~10 min, which prunes anything outside the default groups — a cockpit
  venv there would lose fastapi on every cycle.
- ~~Whether `claude -p` one-shots bill/behave acceptably for Fable delegation~~ **resolved in v3**:
  `fable_delegate.py` scrubs `ANTHROPIC_API_KEY` (the same subscription rule as the warm session),
  so it bills the logged-in subscription like every other headless spawn; low-frequency by design
  (the ceiling gate + hint-only fable arm keep it rare).
- ~~Exact Fable model id~~ **resolved in v3**: `claude-fable-5`
  (`seneschal/scripts/model_config.py`'s `RANK` list; alias `fable`).
