# The Seneschal Cockpit — design spec

**Status:** `PARTIAL(v1-v5 + the Jobs, Trace and Open-specs panels, the cockpit supervision task and the transcript archive BUILT; public exposure deferred)` —
v1 (read-only monitor), v2 (daemon pipe + chat pane), v3 (model dials + Fable delegation), v3.5
(Oikonomos, the budget governor), v4 (health/workout/meal panels + store-staged meals), and **v5 (the
real OIDC auth stack, the archon SSO proxy, the break-glass recovery ladder, and the decoy)** have
shipped. So have the **Jobs panel**, the **Trace panel** (`session-trace-spec.md`), and the
**Open-specs ledger** (`GET /api/doc-status` + `cockpit/server/doc_status.py` +
`cockpit/web/src/components/DocStatusPanel.tsx` + the DOM-free `docStatusView.ts`) — **entirely
derived** from each document's own status header via `seneschal/scripts/check_doc_status.py`, storing
nothing, read-only with no route to edit a status, and a **grid tile**, deliberately unlike the Trace
panel: a reference list the owner goes looking for rather than a read-along of the conversation.
The daemon supervises the backend (`cockpit_app_task` over `seneschal/scripts/cockpit_site.py`,
§"Cockpit supervision") and backfills the transcript archive at boot, and Dream runs the archive's
size sensor (§"The transcript archive"). Public exposure (a tunnel + DNS) remains deferred — everything binds `127.0.0.1`, and dev-no-auth
remains the default until the owner provisions an OIDC app (see `cockpit/README.md` for the shipped
surface, which is authoritative where this spec and the build differ). Per-directory rules a tidy-up
could undo: `cockpit/CLAUDE.md`.

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
spawn (with an "apply now" that queues a graceful restart); break-glass is served by a **separate
tiny supervisor**, never the daemon itself — a third dependency world that is **DELIBERATELY
stdlib-only**, because the rescuer must not share a dependency with the things it exists to rescue (a
broken venv, a dead daemon, a wedged backend); cockpit chat and Telegram are **one conversation**.

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
  size-capped) so the cockpit can backfill after reconnect — **and on through to the durable archive
  below**, which is where the history lives.
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

## The transcript archive (`seneschal/scripts/transcript_archive.py`)

**It is deliberately the store and not the view.** A view built over a file that is still eating
itself *looks like* a full transcript and is not; so the destruction is stopped first, and a
full-transcript view is a later step that is small once the history exists.

**The problem.** `append_transcript_event` writes every `chat.event` to `state/warm-transcript.jsonl`,
and `_maybe_trim` **rewrites that file down to its retained tail** once it grows past the cap. Older
events are *deleted from disk* — no reader can recover them.

**The fix, and what it deliberately does NOT do.** The same events are also appended to
`state/transcripts/YYYY-MM-DD.jsonl`, dated, **never rewritten** (`cockpit_pipe.py` tees every event
there). Purely additive: the ring buffer keeps its cap, its trim and its readers. The ring is the
window; the archive is the history. The ring's retention unit is the **session** — last N whole
sessions, with the row cap as a backstop only (`session-trace-spec.md` §9.3) — which takes effect once
the daemon stamps `session_id` on transcript events; rows without one share a single bucket, so until
then the row cap governs.

**It is not a third copy of the conversation, and that distinction is the design.** Three stores hold
overlapping slices of a turn:

| Store | Holds | Retention |
|---|---|---|
| `turns.jsonl` | **what was SAID, both sides, verbatim** | indefinite |
| `assertions.jsonl` | what **the assistant** said, per send site, one side | bounded |
| `state/transcripts/` (this) | the **agent event stream** — turn boundaries, tool calls, model, cost, usage, duration | **keep everything** |

`turns.jsonl` already satisfies the "both sides, verbatim, durable" half, so this store does not
re-record it, and a view must *join* to it rather than duplicate it. What only the transcript stream
holds is the agent activity *around* each turn — and that is what was being destroyed. It gets strictly
more valuable as `session-trace-spec.md` phases 2-3 widen these rows (uncapped tool input, then tool
results): archiving is what makes that capture worth paying for.

### Retention — keep everything, and the size sensor

**`RETENTION_DAYS = 0` is a decision, not a default awaiting one.** Nothing prunes; `prune` exists, is
tested, deletes **whole day files** (never a partial rewrite) and **nothing in the tree calls it** —
asserted by a test, because the failure to guard against is a later copy-paste of some other log's
30-day sweep. A complete chat log is among the most personal things in `state/`, and `state/` has no
backup; that is why this was a question, and why it was answered with *keep*.

**A threshold with nothing watching it is not a plan.** The decision was "keep everything, and look
again if it passes ~200 MB" — so the decision and its sensor ship together.
`seneschal/scripts/transcript_size_watch.py`, meant to run in Dream beside the nightly sweeps (and
sweeping nothing itself):

- sums the directory with `st_size` — no content is read, so the check stays cheap at any size;
- **fires once** at `THRESHOLD_BYTES` (200 MiB), then goes quiet. A nightly re-nag is one the owner
  mutes, and a muted alarm is worse than none. The marker (`state/transcript-size-alert.json`) records
  **the threshold it fired at**, so re-deciding the number re-arms the sensor mechanically rather than
  by anyone remembering to delete a file;
- **is marked only after the push lands.** There is exactly one alert to spend and it is spent on
  delivery, not on the attempt — a failed send that still burned the one-shot would leave the archive
  over threshold and the sensor permanently silent;
- reports the **measured** rate, not just the breach — current size, observed bytes/day computed from
  the archive's whole days (today's partial file excluded: counting it under-states growth and so
  over-states the horizon), and the resulting horizon. On a busy install the rate is a few hundred KB
  a day, which puts 200 MB years out — a figure that lives in prose and deliberately nowhere in code;
- **cannot cost a Dream run.** Absent directory, unreadable marker, no Telegram config, any raise at
  all: a quiet no-op and exit 0, the same fail-open contract as every other observer in `scripts/`.

**It is a thermometer, not a thermostat.** No pruning, no rotation, no compression, no cap. Wiring it
into Dream's nightly step (`../modes/dream.md`) is part of the same pending wiring as the daemon side.

**Crash safety comes from having nothing to interrupt.** There is no roll step, no move and no
read-modify-write: an event is appended to the file its own `ts` names, so the day file changes because
the day changed, not because anything rotated it. The one residual — a line torn by a kill — is bounded
to that row by `_append_lines`, which heals a missing trailing newline before appending; without it the
fragment would swallow the *next* event into one unparseable line. A boot-time `backfill_from_ring` is
idempotent and rescues whatever is still in the ring buffer — the only copy of everything that
happened before the archive existed — once the daemon calls it at start-up.

**Day files are named by the owner-local date** (`tz_common`), and nothing *decides* on that value — it
picks a filename; each row keeps its authoritative UTC `ts`, and a reader must join on `ts` rather than
assume a day file holds exactly one UTC calendar day.

## Cockpit supervision (the daemon starts its own cockpit) — module shipped, daemon wiring pending

The backend stays a **separate process** for exactly the reason above (internet exposure is a
different failure domain than the warm session) — what changes is *who starts it*. Hand-starting it
means the observatory is down precisely when nobody is watching. The design is a **supervised task**
(`seneschal/scripts/cockpit_site.py`, driven from a `presence.py` task) that reconciles it the same way
the archon-sites task (`seneschal/scripts/archon_sites.py`) does archon UIs. **The module ships; the
`presence.py` task that runs it is not yet wired**, so until it is, start the backend by hand
(`cockpit/README.md` → "Running it locally").

Each ~20 s pass: health-check `GET /api/health` (deliberately the **public** route — a 503 auth wall
must not read as a dead process), (re)spawn detached when down, and clear a wedged one first. Detached
survivors + `state/cockpit-site.pid.json` mean a daemon reload **adopts** rather than duplicates, which
matters more here than for archon sites: the pipe serves exactly one client, so a rival backend would
fight the adopted one for it.

**Three decisions worth recording, all deliberate:**

1. **Deploy bounce via recorded git rev, not a shutdown hook.** The PID file also stores the checkout's
   HEAD at spawn time; when it stops matching, the loop bounces. Hooking the daemon's reload path would
   have covered *some* restart paths and silently missed the rest — a manual restart, a break-glass
   force-pull, a crash-restart. The rev comparison covers all of them and is decidable from two files,
   so it tests with no processes. `read_head_rev` reads `.git` directly and never shells out to `git`
   (a live checkout can hang on fsmonitor).
2. **The frontend is built by the daemon.** `app.py` mounts `web/dist` at **import** time, so the task
   spawns first (API + pipe up in seconds), builds after, and bounces once when the build lands.
   `npm ci` + `npm run build` run as **async subprocesses**, not a worker thread: `asyncio.run` waits
   for its default executor at exit, so a threaded build would hold a graceful reload hostage for as
   long as npm ran. A shutdown kills the build instead. Attempts are capped per boot — npm is minutes,
   and a permanently failing build must not retry every 20 s.
3. **Auth: `cockpit.env` passthrough, `COCKPIT_DEV_NO_AUTH=1` only as a fallback.** Nothing in
   `cockpit/server` loads `cockpit.env` — it reads `os.environ` — so a naive spawn inherits no OIDC
   config, `auth_mode()` falls to `unconfigured`, and every gated route 503s. The spawner loads that
   file when it exists (real OIDC always wins) and injects the dev stub only when no client id ended up
   set. The exposure that buys is bounded by what the backend already enforces twice: it binds
   `127.0.0.1` and rejects non-loopback clients, so reaching it already means code execution on this
   desktop.

Auto-on whenever the `cockpit` extras are importable, and it never installs anything itself (item
3's separate-dependency-worlds line above holds). Without them: one log line, one nudge per boot, and stop.

**Behind a local reverse proxy.** A LAN hostname in front of `localhost:8760` works with a plain
reverse-proxy site — the WebSocket upgrade is forwarded transparently, original `Host` header
included, and that forwarded `Host` is what `dev` mode's `Origin` gate on `GET /api/ws` uses to accept
a same-host proxied origin (`cockpit/README.md` → "Auth modes").

**The extras must survive a deploy.** `uv sync` makes the venv *match* the requested set, so the
updater's `uv sync --frozen` doesn't merely skip fastapi/uvicorn — it **uninstalls** them. A durable
opt-in therefore needs the deploy path to append `--extra cockpit` when the owner has asked for it (a
zero-byte sentinel in `state/`). That deploy-side opt-in is not part of this repo yet; until it is, the
"Ops note" under Open items stands.

## Model dials & Fable delegation

- **`state/model-config.json`** (gitignored; seed `state/model-config.example.json`):
  `{"warm_model": "...", "max_routable_model": "...", "updated_at": ...}` — **two dials, set
  separately** in the cockpit. No tracked file changes to switch models. The daemon reads
  `warm_model` at warm-session spawn ("apply now" queues a graceful restart);
  `max_routable_model` is read live, per turn. Validation rejects an incoherent pair (warm model
  above the ceiling).
- **The picker's options are served, not guessed.** `GET /api/model-config` returns `known_models`
  (and `known_backends`), derived from `cockpit/server/model_config.py`'s `RANK`. Item 3 above sanctions
  duplicating that table between the daemon and the backend — both Python, reviewed together, and
  `test_parity.py` alarms on drift. It does **not** sanction a third copy in `cockpit/web`, and such a
  copy is exactly what breaks: a browser list that never gained a new model id meets a warm dial set
  to that id, and a `<select>` whose value matches no option **renders the first one** — so the panel
  displays the wrong model, and saving what looks right **silently downgrades the warm session**. Two
  fixes, because drift is only the trigger:
  1. the backend serves the list, so an id can be in the config and absent from the picker never again;
  2. the panel **never renders a value it can't represent** — an unrecognized value gets its own
     explicit option labelled as such, so a future drift is *visible* instead of being silently
     rewritten by pressing Save.
  Labels spell out versions (`Opus 4.8` vs `Opus 5`) deliberately — a bare "Opus" is what makes a
  downgrade look like a reasonable click. `RANK` runs haiku < sonnet < opus-4.8 < opus-5 < opus-5.5 <
  fable-5 < fable-5.1; `admits_fable()`'s threshold stays pinned to `claude-fable-5`, so it still reads
  "Fable-tier or above".
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

### A third dial: `backend`

- **`state/model-config.json` gains a `backend` field** (`"claude-cli"` | `"codex-cli"`) alongside the
  two existing dials. `warm_model`/`max_routable_model` are checked against WHICHEVER backend's own
  rank table — `seneschal/scripts/model_config.py` and `cockpit/server/model_config.py` both key
  `RANK`/`ALIASES`/`LABELS` by backend. Saving without naming a backend **preserves whatever is already
  stored** rather than resetting to `claude-cli` — the same silent-downgrade guard as the served model
  list, applied to this axis (a client built before the axis existed must never reset it).
- **The panel has a backend selector** (`cockpit/web/src/components/ModelDialsPanel.tsx`), fed by
  `known_backends` (every backend's id/label/own model list, so the warm/ceiling `<select>`s
  repopulate the moment the backend changes, with no second round-trip). Switching backend mid-edit
  snaps warm/ceiling to the new backend's weakest model — the prior ids almost certainly don't exist on
  the new table, and a value the panel can't represent is the failure mode above.
- **Fable is a claude-cli-only concept, by construction.** `admits_fable()` returns `False`
  unconditionally for any backend without a Fable tier — not because a rank comparison happens to
  fail, but because the concept doesn't exist off Claude.
- **The dial stores and validates; it does not yet switch anything.** The pluggable-backend runtime
  that would run a codex-cli warm session is a separate layer that is not in this repo yet — until it
  is, the daemon runs claude-cli whatever this field says (`model_config.py`'s module docstring).
  Switching is always the owner's own tap, never automatic, and the default (absent field or an
  unrecognized value) is always `claude-cli`.

### The Router panel's row format

**A log row's `arm` must never be readable as its `verdict`.** `arm` is *which classifier ran*;
`verdict` is *what it decided* — and they share a vocabulary, because the fable arm's two verdicts are
"standard" and "fable". A row rendered `channel · arm · category` reads `telegram · fable · ` with the
word *fable* exactly where a reader parses "what happened", next to a `STANDARD` badge — so a panel of
the fable arm *declining* to delegate reads as heavy Fable usage while the summary counts a handful.
Both figures were correct; the panel was the only thing wrong.

The fix is rendering-only; **nothing about what `router.py` logs changed.** Each column is made
self-describing (`cockpit/web/src/components/routerRow.ts`, pinned by `routerRow.test.ts`):

| | before | after |
|---|---|---|
| fable arm, declined | `telegram · fable · ` + `STANDARD` | `telegram · fable arm` + `→ STANDARD` |
| triage arm | `telegram · triage · other` + `ESCALATE` | `telegram · triage arm` + `→ ESCALATE (OTHER)` |
| no `category` | dangling `·` | segment dropped entirely |

The arm always carries the literal word "arm" and the verdict always carries the arrow. `category`
moved to the **verdict** side, where it belongs: it is part of what the triage arm decided. Absent
segments are dropped rather than emitted empty — the tolerant-reader rule aimed at the wording.

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
- **The billable basis.** A ledger row carries a **breakdown** (`components`) and a derived
  **`billable_tokens`** alongside the raw `tokens` scalar, and *every rail, rollup and alert decides on
  `billable_tokens`*, falling back per-row to `tokens` for a legacy row. `tokens` deliberately keeps
  its old meaning — redefining it in place would silently reinterpret every row already on disk.
  - **Why.** Summing every token field flat looks like "a cache-heavy turn still meters its real
    cost", and **that premise is wrong.** A cache read meters at 0.1× a fresh input token, and the warm
    session re-reads its whole conversation every turn — so a flat sum tracks conversation *length*,
    not spend, and gets louder the cheaper each turn becomes. On a cache-heavy day the flat sum can
    alert at several times a budget while the provider's own plan panel reads well inside its limits.
  - **One weight table**, `governor.TOKEN_WEIGHTS`, using the provider's published cache ratios (cache
    read 0.1×, 5m cache write 1.25×, 1h cache write 2×, input 1×) with output **at par**: an
    *input-token-equivalent* basis, not a dollar one, because these knobs are token budgets. The dollar
    figure is already tracked exactly by the CLI, in `metrics.jsonl`'s `cost_usd`.
  - **Basis is named, never averaged.** Rollups report which basis each model's figure is in
    (`billable` / `raw` / `mixed`); alerts, the Fable refusal reason and the Thresholds meters all
    state it. **An alert that can't resolve a basis doesn't fire.**
  - **Unmeasurable spend is marked, never zeroed.** A row that genuinely couldn't be metered carries
    `metered: "unavailable"` and *no* token count. A 0 is indistinguishable from "measured, and free"
    — and the fable_oneshot gate trusts what it reads. Rollups accrue tokens from **any** row that
    carries them, not only `kind: "tokens"` rows, so a `fable_oneshot` row that reports its own usage
    counts. `fable_delegate.py` runs `claude -p --output-format json` and records the result's `usage`
    block on its `fable_oneshot` row; when that block can't be read the row is marked
    `metered: "unavailable"` rather than zeroed.
  - `spend-levers-spec.md` adds *why* a turn cost what it cost (a `turn_id` and a `levers` block on
    the same row) — diagnosis only; this section remains the only place a rail lives.
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
| Model dials | `state/model-config.json` | warm + max-routable + **backend** selectors, apply-now; the picker's options are **served** (`GET /api/model-config` → `known_models`/`known_backends`) — the browser keeps no model list of its own (§ "Model dials") |
| Thresholds (Oikonomos) | `state/governor-config.json` | schema-driven budget knobs + spend meters |
| Plan usage (est.) | `state/metrics.jsonl` + stream usage | **best-effort** tokens/turns by model — no official quota API; labeled estimated |
| Context gauge | stream usage events | warm-session context fill, in the Status panel (age / turns / cost / context bar with Oikonomos's `context_fill_winddown_pct` marked); the figure is an **estimate** and is labelled `est.` |
| Jobs | `state/jobs/` | durable background jobs, read-only; alarms only on `awaiting_push` (§ "Jobs panel") |
| Trace | `metrics.jsonl` + `turns.jsonl` + `warm-transcript.jsonl` + `assertions.jsonl` + `session-starts.jsonl` | the per-warm-session debug log — a collapsible section under the chat pane, **not** a grid tile (`session-trace-spec.md`) |
| Open specs | `seneschal/docs/*.md` status headers | derived, read-only ledger (§ Status above) |
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
cards; empty state: the reserved nutrition desk). Dream-side staging: `seneschal/modes/dream.md`'s
mode searches the store for meal-plan/meal-idea pages by title/tag convention (no hardcoded
database ids — none exist yet) and overwrites `state/meals.json` (`{"staged_at", "plans":
[{"title","url","summary"?,"tags"?}, ...]}`); no matches → still writes a valid empty-`plans` file;
the store unreachable → skips the write, leaving the prior `staged_at` standing so the UI shows
staleness honestly. Seed: `seneschal/state/meals.example.json`.

## Jobs panel (durable background jobs) — shipped

The observability half of `background-jobs-spec.md`. `seneschal/scripts/jobs.py` makes "I'll tell you
when it's done" durable — detached spawns, a `state/jobs/` ledger, and a daemon-owned completion push
on every terminal state. This surfaces the same ledger, read-only.

- **`GET /api/jobs`** (`cockpit/server/jobs.py`) — everything still running, plus the most recent
  `limit` finished jobs with a short log tail each. Running jobs are **never** truncated by `limit`; it
  bounds only the finished tail, so a busy fleet can't push its own members out of view. Finished jobs
  sort by when they *ended*, not when they started — "recently finished" means what it says.
- **`GET /api/jobs/{job_id}`** — one job with a longer tail, for when something failed and you want the
  traceback. The id is a URL path segment, so it is validated against a strict alphabet before it can
  reach the filesystem; traversal returns `available: false`, never a read.
- **`awaiting_push`** is the number worth alarming on, and the only thing in the panel styled as an
  alarm: `jobs.py` stamps `notified_at` **only** on a completion push that actually landed, so a
  terminal job without one means the daemon is trying to reach the owner and failing — precisely the
  silence the jobs feature exists to eliminate. A *running* job having no `notified_at` is normal and
  is deliberately not counted.
- **`available: false` ≠ empty.** A missing `state/jobs/` dir ("I can't see jobs at all") and a
  present-but-empty one ("nothing has run lately") are different facts and get different empty states.
- **Read-only, by decision — there is no cancel button.** Cancelling is `jobs.py cancel`; a dashboard
  button would make killing in-flight work a one-click accident. The panel observes; the daemon and the
  CLI act.
- **`jobs_active` rides the pipe `status` frame** so the Status panel can say "no warm session · 2
  background jobs" — background jobs outlive the session by design, so "no session" must not read as
  "nothing running". Optional field — an older daemon omits it and the UI omits the clause rather than
  claiming zero.
- Deliberately **does not import** `seneschal/scripts/jobs.py` (item 3 above): the backend must keep reading
  a state dir written by a daemon on a *different commit*, so it reads the on-disk shape defensively and
  passes an unrecognized status straight through instead of coercing it.
- **"Recently finished" collapses — and the collapse can never swallow the alarm.** On a busy day the
  finished tail is a wall of history on top of the panel's live half, so the group folds behind a
  `show N older` toggle on its header, persisted in `localStorage` (anything absent, corrupt or written
  by a future version reads as no-preference). **Collapsed by default**, a one-line flip
  (`COLLAPSED_BY_DEFAULT` in `cockpit/web/src/components/jobsCollapse.ts`).

  The rule for *what may be hidden* lives in that module, apart from the component, so it can be
  tested: **running jobs and any finished job whose completion push hasn't landed are never
  collapsible**. The predicate is written as a positive proof of "safe to hide" — provably
  `is_running === false` **and** a non-empty string `notified_at` — rather than as a search for
  "unsafe", so a renamed field, a missing one, or an unexpected type fails toward *showing too much*,
  never toward quietly hiding an undelivered ping. It re-checks `is_running` itself instead of trusting
  the backend's `active`/`recent` split. Pinned by `jobsCollapse.test.ts` (stdlib `node --test`, no
  test dependency added), including a test that a collapsed group still renders every job the banner's
  `awaiting_push` counts. The divider is a group *header*, rendered whenever `recent` is non-empty —
  otherwise a collapsed group would have no way back on an idle daemon.

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
| **Jobs / Trace / Open specs** ✅ shipped | The Jobs panel; the Trace panel (`session-trace-spec.md` phase 1); the derived Open-specs ledger | v1 |
| **Supervision + archive** — module shipped, wiring pending | `cockpit_site.py` (the daemon-supervised backend); `transcript_archive.py` + `transcript_size_watch.py` (the durable transcript and its sensor) | v2 |
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
