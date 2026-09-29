# Before the tunnel — a pre-exposure threat model

**Status:** `MEMO`

A threat model and GO/NO-GO checklist for the moment an install puts any seneschal surface on the
public internet. **It ships nothing and changes no behaviour.** It is a list of conditions an installer
(or a contributor hardening the framework) should verify, each written so that someone else's PR can
turn it into a test.

*Finder's vocabulary, so a future search reaches this file:* threat model · red team · security review ·
prompt injection · attack surface · exposure · public · tunnel · cloudflared · Cloudflare Access ·
authentication · authorization · OIDC · Zitadel · CSRF · websocket · Origin · secrets · credentials ·
exfiltration · supply chain · branch protection · blast radius · go/no-go · hardening.

**The question this document answers**, and the only one: *what must be true before a tunnel opens?*
"Live" here means the deferred public tier in `cockpit-spec.md` (ruling 1 — build every phase, expose
nothing; a tunnel + DNS in front of the cockpit is the deferred last step), plus anything else that puts
a seneschal surface on the public internet. The deliverable is **§6, the GO/NO-GO checklist**.
Everything before it is the argument for those conditions.

**The structural result, stated first:** most of what a reader would expect to be *only-if-exposed* is
reachable **without any tunnel at all**. The public phone Worker, the inbound-mail path, the LAN health
listener and the deploy branch are all doors that exist on a default install. §4.9 is the part that
only a tunnel opens.

---

## 1. Method, and what this document is not

The findings were derived by reading the framework's code — `phone/src/`, `seneschal/scripts/`,
`cockpit/` — and, where cheap, by running pure functions offline (no socket, no request, no model call).
**Nothing was attacked, sent, or connected to.** Every "an attacker could" below is a reachability
argument from code, marked as such.

**Three exposure classes, used rigorously:**

| Class | Means |
|---|---|
| **REACHABLE WITHOUT A TUNNEL** | Exploitable on a default install with the documented setup, no tunnel involved. |
| **ONLY-IF-EXPOSED** | Becomes exploitable the moment a tunnel opens (or an already-public surface widens). |
| **THEORETICAL** | Needs a precondition that a default install does not meet. |

Whether a given finding is live on *your* machine depends on what you enabled (the phone Worker, the
health listener, the Watch peek, the cockpit). Treat each as a condition to check on your install, not
as a statement about it.

---

## 2. The threat model

### 2.1 Assets, ranked by what a compromise costs the owner

Ranked by harm to the owner, not by exploit elegance — the ordering §4 follows.

| # | Asset | Where it lives | Worst realistic loss |
|---|---|---|---|
| 1 | **Health and reminder adherence** | `seneschal/state/health.db`, `presence.db`, the reminder queue, the store's ⏰ rows | A missed or falsely-deferred reminder. A forged presence context can hold a place-gated nudge. |
| 2 | **Private conversation** | `state/turns.jsonl`, the warm-session transcript, the RAG index, `state/session-distillations.jsonl` | Read-out or exfiltration — a *reader*, not a sender. |
| 3 | **The daemon's grant** — store write, Telegram/email/phone send, shell on the workstation | the warm session and every `claude -p` child | Arbitrary action in the owner's name. The daemon's default `--permission-mode` is `bypassPermissions` (`presence.py`). |
| 4 | **The owner's identity across channels** | `proton.env`, `google.env`, `telegram.env`, `push-call.env`, the Worker's secrets | Mail, calls and messages that are genuinely from the owner. |
| 5 | **The machine** | the workstation itself | Persistence, lateral movement, other repos and clients. |
| 6 | **Money** | the Claude subscription, the Worker's **metered** API key, Twilio, any other API credential in the user environment | Metered spend driven by an anonymous client (§4.3); spend-capable credentials inherited by every child process (§4.8). |
| 7 | **The home** | Home Assistant, if configured (`ha.env`) | Inert until configured — but a forged presence feed is already the input side of it. |
| 8 | **The repo's integrity** | the deploy branch on the remote | It *is* the deploy artifact (§4.1). |

### 2.2 Trust boundaries — drawn, and the one that does not exist

```
        ┌─ PUBLIC INTERNET ────────────────────────────────────────────────┐
        │  phone screener Worker (workers.dev)  ── PUBLIC IF DEPLOYED ──►  §4.3
        │  [a tunnel — deferred; one cloudflared command away]  ────────►  §4.9
        └──────────────────────────────────────────────────────────────────┘
                     │  inbound text: email, Telegram, Discord, Slack, web, store pages
                     ▼
        ┌─ LAN / TAILNET ──────────────────────────────────────────────────┐
        │  health_listener.py   :8765   static bearer  ──────────────────►  §4.5
        └──────────────────────────────────────────────────────────────────┘
                     ▼
        ┌─ THE WORKSTATION, as the owner's user ───────────────────────────┐
        │  cockpit :8760 (127.0.0.1)   decoy :8490   break-glass :8499     │
        │  daemon pipe :8471 (127.0.0.1)   ollama :11434   archon UIs      │
        │                                                                  │
        │   ╔══════════════════════════════════════════════════════════╗   │
        │   ║  NO TRUST BOUNDARY EXISTS INSIDE THIS BOX.               ║   │
        │   ║  Every agent, job, archon, MCP server, browser-driven    ║   │
        │   ║  backend and scheduled task runs as the SAME user, on    ║   │
        │   ║  the same disk, with the same reach.                     ║   │
        │   ╚══════════════════════════════════════════════════════════╝   │
        └──────────────────────────────────────────────────────────────────┘
                     ▲  git pull --ff-only on the deploy branch, every ~10 min
        ┌─ the remote repository ──────────────────────────────────────────┐
        │  main = the deploy branch.  Protected?  ──────────────────────►  §4.1
        │  No signature check anywhere in the deploy path.                 │
        └──────────────────────────────────────────────────────────────────┘
```

**The boxed sentence is the thesis.** `seneschal/state/` is a control plane any process running as the
owner can write (§4.7); an installed guard hook is a file a merge overwrites (§4.7); and
`bypassPermissions` is the default for every daemon-spawned model (§4.4). The boundary that *does* hold
is the filesystem ACL against *other local accounts*; the boundary that does not exist is between the
things running as the owner.

**Consequence for the tunnel decision:** exposure does not add a new interior boundary. It adds a new
*door into a room that has none.* Most of Gate B (§6) is about that door; Gate A is about the doors that
are already open.

### 2.3 Adversaries

`cockpit-spec.md` → "Threat model" lists five adversaries for the public tier. This model keeps them,
ranks them by present reachability, and adds the ones that need no tunnel.

| Adversary | Reachable without a tunnel? | Best current path |
|---|---|---|
| **A stranger who knows an address** — the assistant's inbox, or the Worker URL | **YES** | §4.3, §4.4 |
| **A prompt-injector writing content the assistant will read** — an email, a job posting, a README, a store page | **YES** | §4.4 |
| **Someone on the LAN or tailnet** | **YES**, if the health listener runs | §4.5 |
| **Anyone who can land a commit on the deploy branch** — including every collaborator account | **YES** | §4.1 |
| **A page in a browser the owner uses** (ad frame, link, compromised site) | **YES** — a WebSocket handshake is exempt from the same-origin policy | §4.9 |
| **Internet-random scanners** (spec's list) | only the Worker, until a tunnel | §4.3 |
| **A targeted attacker with the domain** (spec's list) | not until the tunnel | §4.9 |
| **A browser-session thief** (spec's list) | only once OIDC is on | §5 |
| **A compromised archon UI** (spec's list) | whenever an archon serves a UI | §4.9, §8 |

**Explicitly out of scope as an adversary: the owner, and Anthropic.** This model does not defend
against the operator, and it treats the Claude Code harness as trusted infrastructure.

---

## 3. Testing the threat model that already exists

`cockpit-spec.md` → "Threat model" carries a mitigation list written at design time. Scoring it against
the code is the cheapest orientation available.

| # | Mitigation as written | Verdict | Evidence |
|---|---|---|---|
| 1 | *localhost-only binds + a middleware rejecting non-loopback clients* | **HOLDS for HTTP; not for `/api/ws`** | `app.py`'s `localhost_only` is `@app.middleware("http")`; its own docstring notes it does not run for websocket connections. The uvicorn `--host 127.0.0.1` bind is the whole control for the socket (§4.9b). |
| 2 | *authed surface behind OIDC + PKCE + TOTP* | **NOT IN FORCE BY DEFAULT** | `auth.auth_mode()` is `oidc` only when `COCKPIT_OIDC_CLIENT_ID` is set; the setup chapter recommends dev-no-auth as the default. TOTP is an IdP-side enrolment the code cannot confirm. |
| 3 | *decoy fully isolated* | **HOLDS** | Own process and port, no tools, no state access (`cockpit/decoy/README.md`). Its rate limiter is the residual (§4.9e). |
| 4 | *mutations CSRF-protected + audit-logged* | **HOLDS in `oidc`; CSRF is a no-op otherwise** | `require_csrf` returns immediately outside `oidc` mode (by design — there is no session to ride). Every mutating route writes a `cockpit-audit.jsonl` line. |
| 5 | *break-glass = 3-factor ladder incl. an out-of-band channel* | **HOLDS, if the supervisor is running** | The ladder fails closed. Its CORS is deliberately permissive, justified by a loopback bind — which a tunnel must not route to (B7). |
| 6 | *daemon pipe localhost-only + token* | **HOLDS** | `presence.py` binds the pipe to a hardcoded `127.0.0.1`; the token is `secrets.token_hex(32)`. |
| 7 | *archons never internet-facing (proxied under the session)* | **HOLDS at the method layer; the origin is the residual** | The proxy is GET-only (writes are 405'd); the port comes from the registry, never the request. But the proxy relays archon HTML verbatim on the cockpit's own origin (§8). |
| 8 | *all state gitignored/local* | **HOLDS** | `.gitignore` covers `seneschal/state/*` except README and `*.example.*`. |
| 9 | *secrets never in the repo* | **HOLDS in the tree** | Only `*.example` templates are tracked; CI's placeholder guard backs it for UUIDs. |
| 10 | *the break-glass supervisor is the only process that can hard-reset* | **DOES NOT HOLD** | `request_control.py` appends `restart`/`shutdown` to `state/control-queue.json` with no token and no author, and `presence.pop_control` pops the entry when it applies it. `/api/ws` reaches the same queue via a `control.restart` frame. The supervisor is *one* way, not the only one. |

**Two further mitigations the spec relies on for the public tier — Cloudflare WAF and rate limiting —
live entirely outside the repo.** `cockpit/server/` has no rate limiting at all, so the mitigation and
the exposure ship in the same change and neither can be tested against the other first (B5).

---

## 4. Findings, ranked by blast radius

Class in the header. Every entry names its evidence and its precondition.

### 4.1 — F1 · Deploying is merging, and the deploy path verifies nothing · **REACHABLE WITHOUT A TUNNEL**

**The path.** Land a commit on the deploy branch — by PR merge, or by a direct push. Within about ten
minutes `seneschald-control.ps1 -Action Update` fetches it, fast-forwards, syncs the lockfile, and asks
the daemon to restart onto it. The code then runs as the owner, with the daemon's full environment and
`bypassPermissions`.

**Evidence.**
- `seneschald-control.ps1` — `$DeployBranch = 'main'`, `git fetch origin $DeployBranch`,
  `git pull --ff-only`, then a restart enqueue.
- **No signature verification anywhere in the deploy path**: a search for `verify-signatures`,
  `verify-commit`, `verify-tag` or `allowed-signers` across `*.py`, `*.ps1` and `*.yml` returns zero
  hits. The single predicate between "a commit exists on the remote deploy branch" and "it is running as
  the owner" is *is it a fast-forward*.
- **The merge guard does not see a direct push.** `git push origin HEAD:main` is not a merge command, so
  a `PreToolUse` merge hook never evaluates it. Branch protection on the deploy branch is the only real
  fix, and it is a remote-side setting no PR can make.

**Check on your install:** is the deploy branch protected (no direct push, no force-push, required
status checks)? On some hosting plans branch protection and rulesets are unavailable for private
repositories — the API answers 403 — in which case the remedy is a plan or visibility decision, not a
config change. Until one of those happens, every other control in this document sits downstream of an
unguarded write to the deploy branch.

**Blast radius:** everything in §2.1. **Precondition:** push access, or a token or session holding it.

### 4.2 — F2 · "Docs-only" must not mean *ends in `.md`*, and the guard must read CI · **REACHABLE WITHOUT A TUNNEL**

Two defects that compose into a self-lowering gate. `autonomy-policy.md` grants merge-on-green to a PR
whose diff touches only prose **"and changes no executable behavior"**, and states that **green is an
absolute precondition**. A merge guard can implement both clauses, or only the first.

**(a) Markdown in this repo is behaviour.** `seneschal/modes/**` is read imperatively on dispatch — an
edit there edits a live run. `seneschal/SKILL.md`, every `subagents/**/SKILL.md`, `persona/**`,
`seneschal/references/**` (including `autonomy-policy.md`, *the file that is the gate*) and every
per-directory `CLAUDE.md` are prompt. A classifier that treats "every changed path ends in `.md`" as
docs-only lets a PR rewrite the gate itself with no approval tap. The ruled answer, and what §A3 checks,
is **"everything that runs, except the docs router"** — see A3 for why the router exemption is
load-bearing.

**(b) A guard that never reads the check rollup does not enforce "green".** If the guard's PR query
does not fetch status checks, the one code-enforced gate does not enforce the half the policy calls
absolute — green is then enforced only by prose, in files that are themselves `.md`. Two shapes to
watch for: a docs-only PR with a **red** check, and a PR with an **empty** rollup (checks skipped or
delayed) that the host reports as mergeable.

**One direction is right, and should be said:** the *asking* path should treat an empty rollup as
not-green, and `seneschal/modes/dream.md` says a pending PR is never merged. Nothing should auto-merge on
"no checks". The risk is the *allowing* path, if it never asks.

**Blast radius:** the assistant's instructions, and transitively every asset in §2.1.

### 4.3 — F3 · The phone Worker is public by construction, and most of its routes have no auth · **REACHABLE WITHOUT A TUNNEL**

This is not a tunnel question: once `phone/` is deployed it is on `workers.dev`. **A search for
`X-Twilio-Signature`, `validateRequest` or `hmac` in `phone/src/` returns nothing** — no Twilio webhook
is signature-validated. The route table in `phone/src/index.ts` bearer-checks `/blocklist`,
`/sync-contacts` and `/push-call`, and nothing else: `/voice`, `/gate`, `/ws`, `/status`,
`/push-call/ack`, `/after-bridge` and `/voicemail` are open.

**F3a — `GET /ws` is unauthenticated and drives Claude on a METERED key.** `handleWs` checks only for an
`Upgrade: websocket` header, then hands the socket to the `RelaySession` Durable Object, which builds an
Anthropic client from `env.ANTHROPIC_API_KEY`. `MAX_TURNS` bounds the tool loop *within one turn*;
nothing bounds prompt frames per socket, or sockets. **The daily budget is enforced only on the
bearer-authed talk-mode path** (`handlePushCallTalk` calls `overBudget`); the anonymous screener socket
never consults it, and `/status` publicly reports the configured cap.

Worse than the spend: in screener mode the system prompt is
`buildSystemPrompt(owner, env.OWNER_PROFILE, persona, env.OWNER_NAME_SPOKEN, env.OWNER_PASSWORD)` — **an
anonymous client converses with a model whose system prompt contains the owner profile and the screener
password**, extractable by ordinary prompt injection over that socket.

**F3b — `POST /gate` blocklists a number on the first forged request.** `handleGate` calls
`recordGateFail(env.DB, from, to, digits === "")`, and `recordGateFail` blocklists immediately when
`silent` is true. That is correct *for a real Twilio webhook* (a silent robodialer). The defect is that
the endpoint is unauthenticated and `From` is attacker-supplied, so a forged POST with an empty `Digits`
permanently blocks any number not already in the contacts — a clinic, a pharmacy — and the block syncs
to the on-device blocker.

**F3c — an unauthenticated notification to the owner.** On the anonymous `/ws` socket, steering the model
to a `connect` terminal calls `alertConnecting` (which notifies the owner) **before** `redirectToDial`
(`phone/src/relay/session.ts`), so the notification lands even though the attacker-supplied `callSid`
makes the subsequent Twilio call fail. The `message`/`spam` terminals call `redirectToHangup` first and
are incidentally protected by ordering; `connect` is not.

**F3d — `POST /push-call` will dial any number.** The only validation on `to` is that it starts with
`+`, and `escalate: true` hands it to a retrying Durable Object. Gated by `PUSH_CALL_SECRET` — compared
with plain `!==` — so this is a blast-radius note on that secret, not an open door.

**And nothing is recorded.** `phone/wrangler.toml` configures no `[observability]`, tail consumer or
logpush.

**Blast radius:** the owner's phone and attention, inbound-call reachability, metered spend, and the
screener password. **Precondition for F3a–F3c: knowing the Worker URL.**

### 4.4 — F4 · Third-party text reaches a `bypassPermissions` model unattended, and the persistence guard now refuses it at the index · **REACHABLE WITHOUT A TUNNEL**

**The grant that sets every blast radius here.** `presence.py` —
`--permission-mode`, default `bypassPermissions`, whose help string reads *"the assistant's
act-low/ask-high gate is the real safety"*. **That concedes the point: the only gate is prose inside the
same prompt the attacker is writing into.** No launcher passes `--allowedTools` or `--disallowedTools`.
So a model reading mail holds shell, file write, subagent spawn (which inherits the bypass), and the
store's MCP if one is configured.

**The path.** `run-presence.cmd` launches the daemon with `--peek-interval-min 5` and a Watch prompt
(*"glance at email/Slack/calendar and escalate only if something is genuinely hot"*) on a cheap model.
Mail to the assistant's address → the mail bridge → a peek every five minutes with no human present →
the model reads the body (`proton_read.py`) and decides for itself whether to act.

**The persistent variant is worse, and it is the finding.** `session_stamp.py`'s machine-wide
`SessionEnd` hook spawns `mini_dream.py`, whose LLM arm (at `LLM_USER_TURNS` or more user turns)
concatenates up to `MAX_EXCERPT_CHARS` (16,000) of **verbatim transcript, both speakers**, under a bare
label — `"Transcript excerpt:\n"` is the entire delimiter — and hands it to a tool-less `claude -p`
summarizer. The output is stored unreviewed in `state/session-distillations.jsonl`, Dream ingests it
into the RAG index, and retrieval injects it into future turns automatically. `comms-mapping.md` names
the index as the destination where **nobody is in the loop at all**.

**The asymmetry is the exploit:** the *writer* is a tool-less summarizer; the *reader* is the warm
session at `bypassPermissions`. A low-privilege model launders attacker text into a store a
high-privilege model treats as its own recall.

**What exists, and what does not yet.** `provenance_guard.py` is adopted: `rag_index.index_records`
runs it on every record before reading the text, `mini_dream.py` writes the producer stamp (`llm-excerpt`
is refused), and the LLM distill fences the transcript as untrusted data. Refusals land in the index's
`provenance_refusals` ledger, which `rag_index.py --stats` shows. Every other destination in
`comms-mapping.md` (job briefs, subagent prompts, carry-over, the run log, memory files) stays prose-only
by that reference's own account.
`rag_projects.py` also indexes README excerpts and commit subjects from configured roots — third-party
text if any configured root holds a cloned dependency.

**The one sanitizer in the scripts tree covers filenames.** `telegram_poll.safe_filename` exists because
an attachment's name is the sender's string about to be read by a model. Message bodies, email bodies,
store page bodies and READMEs get nothing structural.

**Blast radius:** everything in §2.1, unattended. **Precondition:** knowing the assistant's address, or
writing anything it will read.

### 4.5 — F5 · `health_listener.py`'s public-bind guard cannot see `0.0.0.0` · **REACHABLE WITHOUT A TUNNEL**

**The module's documented refusal does not do what it says.** The docstring claims it *"refuses to start
on a public interface without a token."* The guard is `_is_local_or_tailnet`:

```python
return ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10") or ip.is_private
```

and CPython classifies `0.0.0.0` as `is_private == True` (as it does every RFC1918 address). So
`python health_listener.py --host 0.0.0.0` **with no token starts an unauthenticated listener on every
interface**, and the guard never fires. `is_private` also means **every RFC1918 address needs no
token** — that is not "the tailnet", it is the whole LAN, guests and IoT included. The startup banner
computes its `scope` through the same predicate, so a `0.0.0.0` bind is logged as `(localhost)`.

**What holds on a default install:** `run-health-listener.cmd` passes `--host 0.0.0.0` but refuses to
launch when `HEALTH_INGEST_TOKEN` is empty — four lines of `cmd.exe`, not the module. Beyond that, off-host
reach depends on the host firewall's default, not on code. The token check is a plain string `!=`, with
no rate limit or lockout; it is the same static secret configured in the Android app, which POSTs over
plain HTTP (`usesCleartextTraffic="true"` in `phone/android/app/src/main/AndroidManifest.xml`).

**What a forged POST buys.** `/presence-ingest` imports into `presence.db`, the context
`presence_rules.presence_gate` consults. `require_place` is checked **before** the `pierces` branch —
deliberately, "you tagged it place-locked, so honor that" — so a forged `at_place` holds a place-gated
item, even a Critical one, for as long as the forged context stays fresh. `/health-ingest` writes
`health.db`, which briefs and panels read as fact.

**Blast radius:** asset #1. **Precondition:** LAN/tailnet adjacency plus one observed cleartext POST,
or anyone following the module's own `--host 0.0.0.0` usage without a token.

### 4.6 — F6 · A merge guard's detection perimeter must match its documentation · **REACHABLE WITHOUT A TUNNEL**

`autonomy-policy.md` makes `merge_guard.py` (a `PreToolUse` hook, documented in `MERGE_GUARD_SETUP.md`)
the code enforcement of the approval gate, and says it is **inert until installed**. The *approval* half
of such a guard — head-SHA-bound, single-use, repo-keyed approvals, untrusted PR text fenced below the
guard's own facts — can be very strong. The *detection* half decides whether the approval logic runs at
all, and it is where false negatives hide. **Before relying on an installed guard, run its own command
parser over these shapes and confirm each is detected (or denied):**

| Shape | Why it matters |
|---|---|
| `gh.cmd` / `gh.bat` / `gh.ps1` | Windows CLI shims are the ordinary install shape; stripping only `.exe` misses them *by accident*, which is precisely the class the guard exists for. |
| `gh api …/pulls/$N/merge` (shell variable, not literal digits) | A regex needing digits can fall through to "not a merge" — failing **open** where `gh pr merge <branch>` correctly denies. |
| `command gh`, `env gh`, `nohup gh`, `xargs … gh`, `uv run gh`, `npx gh` | Wrapper prefixes. |
| `GH=gh; $GH pr merge 1`, `$(echo gh) pr merge 1` | Indirection. |
| `python -c "subprocess.run(['gh','pr','merge',…])"`, `Start-Process gh -ArgumentList …` | A second interpreter. |
| `git push origin HEAD:main` | Not a merge at all — §4.1. |

**F6b — the approval picker must show every changed path**, or the count plus the highest-risk ones. A
picker that shows the first four paths and "(+3 more)" lets a PR that edits the guard alongside four
alphabetically-earlier files present the owner with four innocuous names. That is social engineering of
the owner, not a parser bypass.

The correct standard for judging a guard like this: it does not buy impossibility — it makes **routing
around it a deliberate act rather than a judgment call.** By that standard a `.cmd` shim is a real defect
(it is not deliberate), and an undocumented gap is a documentation defect.

### 4.7 — F7 · The guards are deployed by the thing they guard, and `seneschal/state/` is an unauthenticated control plane · **REACHABLE WITHOUT A TUNNEL**

**(a) The gate is its own payload.** Session hooks (`session_stamp.py`, and any installed guard) are
registered by absolute path into the daemon's checkout — the one `seneschald-update` overwrites every ten
minutes. "Inert until installed" is true of the *registration* and false of the *code*: once registered,
a merged change to the hook file is a change to the `PreToolUse` gate of every Claude Code session on the
machine, with no reinstallation. Combined with §4.1's direct push, the largest hole also removes the
guard.

**(b) A file write is an RPC.** Any process running as the owner can drive the daemon by writing a file,
with no author recorded:

| File (under `seneschal/state/`, or `SENESCHAL_STATE_DIR`) | What writing it does |
|---|---|
| `control-queue.json` | restarts or shuts down the daemon (`request_control.py`); the entry is popped when applied, so no history survives |
| `cockpit-inbox.jsonl` | becomes an inbound turn to the warm session |
| `model-config.json` | sets the warm model and the delegation ceiling |
| `presence.db` / `health.db` | as §4.5, without the network hop |

**(c) Untracked code must not run on a schedule.** `state/` is gitignored, so a `.py` placed there is
invisible to CI's byte-compile, to the unit suite, and to review. **Check on your install** that every
scheduled task points at a tracked script (or its documented `.cmd`/`.sh` wrapper — some wrappers exist
precisely to scrub the environment), and that the break-glass supervisor is actually registered if you
rely on it.

### 4.8 — F8 · The environment scrub guards one name · **REACHABLE WITHOUT A TUNNEL**

**The `ANTHROPIC_API_KEY` scrub is real and it works** — `presence.child_env()` pops it before every
child `claude`, so a stray key never switches the assistant to metered billing. **The defect is that it
is a BILLING control easily read as a SECRETS control**, and it is name-matched to one literal string.

- **Every other credential in the user environment is inherited by every child** — every delegated
  subagent, every archon, every Watch peek. On Windows, anything set with `setx` lands in
  `HKCU\Environment` and is broadcast to every new process. **Check on your install:** list the
  credential-shaped names in the user environment (names only, never values). Any cloud DNS / Worker
  deploy token is a Gate A item by itself (A7); the rest belong in per-tool env files the shell does not
  broadcast (C1).
- **`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` are not scrubbed.** They do not change billing; they
  change **where every model call goes**. A user-level environment change needs no elevation and is
  inherited by the daemon and every child. *(That the CLI honours these variables is documented
  behaviour, not something measured here.)*
- **`sentinel.py`'s `--launch-cmd`** runs through `subprocess.run(shell=True)` with no `env=` at all,
  while its sibling in `presence.py` passes `child_env()`. Dormant unless configured, not safe.
- **Plaintext secret files outside this repo** (for example the phone Worker's local `.dev.vars`) are
  readable by any process running as the owner. `child_env()` has no opinion about files.

**Rollback and notice.** A deploy that pulls and syncs cleanly stamps `state/seneschald-health.json` as
healthy by construction — **the health file measures liveness, not legitimacy.** There is no per-deploy
"here is what changed" notice, and a tested rollback-to-a-pinned-SHA procedure is worth having before
exposure, not after (C3, C4).

### 4.9 — F9 · What a tunnel actually changes · **ONLY-IF-EXPOSED**

Everything above needs no tunnel. This is the part that does — and a quick tunnel is one command
(`cloudflared tunnel --url http://localhost:8760`) away.

**(a) Dev mode is an explicit opt-in, and the setup chapter recommends it.** `auth.auth_mode()` is `oidc`
when `COCKPIT_OIDC_CLIENT_ID` is set, `dev` when `COCKPIT_DEV_NO_AUTH=1`, else `unconfigured` (every gated
route 503s — fails **closed**). `subagents/setup/chapters/cockpit.md` presents dev-no-auth as the default
and launches uvicorn with `COCKPIT_DEV_NO_AUTH=1`, justified by the `127.0.0.1` bind — reaching it already
means code execution on the desktop. **A tunnel is exactly the negation of that justification.** An
exposed process with an empty or misspelled client id and the dev flag still set is fully open.

**(b) `/api/ws` is not covered by the middleware that guards every other route.** Its docstring says so,
and says *"the uvicorn 127.0.0.1 bind still covers exposure regardless of route type."* **That clause is
the entire compensating control, and a tunnel is its negation.** No code in `cockpit/server/` checks an
`Origin` header. Behind `cloudflared` every request arrives *from loopback*, so `localhost_only` becomes
a no-op even for HTTP routes; the safe `X-Forwarded-For` behaviour holds only by a uvicorn library
default nothing here names or tests. **`FORWARDED_ALLOW_IPS=*`** would be a one-variable total bypass —
and it is the first thing anyone tries when a tunnelled app "sees 127.0.0.1".

**(c) The read routes are the data breach, not the mutators.** In dev mode every gated route is open:
`GET /api/transcript` is the verbatim conversation, `GET /api/presence` is live location and activity,
`GET /api/health/*` is sleep, workouts and nutrition. **`GET /api/health` and `GET /api/auth/status` are
public in every mode** — together they announce the auth mode to an unauthenticated caller.

**(d) Exfiltration in real time.** Every frame the daemon sends is fanned out to every connected browser
tab (`browser_hub.broadcast`). **An attacker's socket is a browser tab** — the live conversation streams
to it, and it can send `chat.send` and `control.restart` back.

**(e) Smaller, but each must ship with the tunnel.** `secure=False` is hardcoded on both cookies
(`auth.py`), with a comment naming the exact remediation and its trigger. There is no rate limiting in
`cockpit/server/`; the planned mitigation is Cloudflare-side and does not exist until the tunnel is
configured. The cockpit backend sets no CSP or `X-Frame-Options`. The decoy's per-IP rate limiter keys on
`request.client.host` (`cockpit/decoy/server.py`), which collapses to `127.0.0.1` for every visitor behind
a tunnel — one shared global bucket, a self-DoS of the page *designed* to be public. And tunnelling the
cockpit also tunnels every archon UI its SSO proxy reaches.

**And the cheap answer appears nowhere.** A search for "Cloudflare Access", "zero trust" or `Cf-Access`
across the repo returns nothing. An Access policy in front of the hostname would neutralise most of this
section at a stroke, and it has never been written down.

### 4.10 — F10 · Lower-consequence, listed for completeness

- **The Telegram allowlist is per-CHAT, not per-USER** (`telegram_poll.py` checks `chat.id`, for messages
  and reactions alike). Benign while the allowlisted chat is a DM. **If it is ever a group, any member's
  message is an inbound turn and any member's 👍 on a nudge auto-acks it.**
- **Fail-open allowlists.** Telegram (`TELEGRAM_ALLOWED_CHAT_IDS`) and Discord
  (`DISCORD_ALLOWED_USER_IDS`) both use `if allowed and id not in allowed` — an absent, empty or
  misspelled key disables the check with no startup validation and no log line.
- **"Absent" is a fact about today's filesystem, not a property.** Each unconfigured `*.env` (Discord,
  push-SMS, Home Assistant, …) is one file write away from arming a sender, with no provisioning gate and
  nothing that would notice one appearing.
- **Non-constant-time secret comparisons** in `health_listener.py`, the three bearer checks in
  `phone/src/index.ts`, and the break-glass phrase check in `cockpit/breakglass/supervisor.py`. Each is
  practically unexploitable in its own setting; each is a one-word fix.
- **Gradle wrapper:** `gradle-wrapper.jar` is committed, `distributionSha256Sum` is absent from
  `gradle-wrapper.properties`, and `.github/workflows/android.yml` has no wrapper-validation step.
- **npm:** `npm ci` everywhere is tight, but it still runs install scripts; nothing passes
  `--ignore-scripts`.
- **`cockpit/server/archons.py`** builds `f"http://127.0.0.1:{port}/{path.lstrip('/')}"` with no path
  normalization. Bounded — fixed host, port from the registry, GET-only — but unguarded.

---

## 5. Zitadel, sized

Finishing the OIDC login is a prerequisite for most of Gate B — but **it does not do what the
sequencing implies**, which is the single most important thing in this section.

### 5.1 What is built

The cockpit **deliberately never parses a token**: authorization code + PKCE, then a userinfo call to
establish identity (`cockpit/server/auth.py` and `oidc.py` docstrings). So every "ID-token validation"
row below is *not applicable by design*, not an oversight — with a genuine benefit: **zero JWKS and
key-rotation surface.**

| Step | State | Where |
|---|---|---|
| Discovery (`.well-known`) | **BUILT** | `oidc.py` — but **no self-check that the document's `issuer` matches the configured issuer** |
| Authorization redirect, `state`, PKCE S256 | **BUILT** | `oidc.py`, `app.py` `_start_oidc_round_trip` |
| OIDC `nonce` | **N/A by design** | no ID token is read |
| Callback failure ladder, each failure audited | **BUILT** | `app.py` `/auth/callback` |
| Code exchange (public client, PKCE, no secret) | **BUILT** | `oidc.py` |
| ID-token signature / `iss` / `aud` / `exp` · JWKS | **N/A by design** | only the access token is used |
| userinfo → identity | **BUILT** | `preferred_username` or `sub` |
| **Authorization allowlist** | **BUILT** | `COCKPIT_ALLOWED_USER`; **a valid IdP user who is not the owner gets a 403.** Residual: the `@`-local-part of `preferred_username` is also accepted, so `owner@anything` matches `owner`. |
| Session cookie: HMAC-signed, `compare_digest`, `HttpOnly`, `SameSite=Strict` | **BUILT** | `session.py` |
| Signing key: random, file-backed (`state/cockpit-session-secret`), never an env var | **BUILT** | `session.get_or_create_secret` |
| `secure=True` | **MISSING** | hardcoded `False` in `auth.py`, remediation in a comment |
| Absolute session lifetime | **MISSING** | 6 h *sliding*, reissued on every authenticated call; a polling tab re-arms it indefinitely. No absolute cap, no server-side revocation; logout clears only the local cookie. |
| RP-initiated / back-channel logout | **MISSING** | |
| CSRF in `oidc` mode | **BUILT** | `require_csrf` custom header, paired with `require_auth` on mutating routes |
| Archon SSO proxy auth | **BUILT** | the same `require_auth` dependency; writes 405'd |

### 5.2 Fixed: a pending-login cookie was accepted as a session cookie

Both cookie tokens are signed with the **same secret**, so a valid signature alone never said which
kind a token was — and `read_session_token` checked only the signature and `iat` age, never `sub`.
`GET /auth/login` has no auth dependency, so one anonymous request yielded a pending-login cookie that
replayed as the session cookie; the websocket gate in `oidc` mode (`current_session_from_cookie(...) is
None` → 4401) then accepted it. REST failed closed (a `KeyError` on `sess["sub"]`, a 500); the websocket
did not. Turning on `oidc` mode therefore left the websocket open while making it look closed.

**Fixed** in `cockpit/server/session.py`: every payload carries a `typ` (`session` / `login`), each
reader refuses the other kind, and `read_session_token` requires a non-empty string `sub`. An untagged
payload is accepted only as a legacy session (it must carry `sub` and none of the login fields), so
existing sessions survive the upgrade without a forced logout. `test_session.TokenKindConfusionTests`
pins every direction of the confusion. The model to keep following is the break-glass assertion
(`cockpit/breakglass/assertion.py`): a separate secret and type-checked claims.

### 5.3 Sizing

**Code (all Small unless marked).** `secure=True`, config-driven · an absolute
session cap carried through the sliding reissue · a discovery `issuer` self-check (one `if` — cheap
defence-in-depth for a design with no signature check) · tighten the allowlist's `@`-local-part match ·
a WebSocket `Origin` **and** client-host check · RP-initiated logout *(Small–Medium)* ·
`X-Forwarded-For` trust and a rework of `localhost_only` *(Medium)* · rate limiting *(Medium —
`cockpit/decoy/ratelimit.py` is a working stdlib implementation to port)* · tests for all of the above
*(Medium)*.

**No new dependency.** The OIDC path is hand-rolled stdlib `urllib` + `hmac`, and there is no hand-rolled
JWT validation to audit because no token is parsed. That is the design working.

**Host setup — the owner's own hands; no PR can do any of these.** The canonical walkthrough is
`cockpit/zitadel/ZITADEL_SETUP.md`; in outline:

1. Start the Zitadel compose stack (`cockpit/zitadel/docker-compose.yml`). Nothing in the framework
   supervises it.
2. First login and password change at the local console.
3. **Enrol TOTP** — break-glass rung 2 is meaningless without it.
4. Create the project and the **OIDC application** — Web, PKCE, no client secret, exact redirect URI,
   HTTP allowed only for local dev.
5. Put the client id and issuer into the cockpit's environment (`cockpit/server/cockpit.env.example`
   lists the keys) — and **unset `COCKPIT_DEV_NO_AUTH`**.
6. Do nothing about the session secret — it self-generates. Called out because it looks like a missing
   step and is not.
7. Restart the cockpit backend so the environment is read.
8. *(Only at tunnel time)* `cloudflared tunnel create` + `cloudflared tunnel route dns` — **not
   wrangler** — plus flipping the IdP's external domain / secure settings and turning the app's HTTP
   allowance **off** in the same change.

### 5.4 Does Zitadel alone make it safe to expose?

**No.** Even with the token confusion fixed (§5.2), Zitadel touches none of: `secure=False`, unbounded sliding
sessions with no revocation, the vacuous `localhost_only` behind a proxy, the missing WebSocket `Origin`
check, the absent rate limiting, the break-glass port (which never reads the cockpit cookie and has
deliberately permissive CORS justified by a loopback bind), the archon UI ports, or the tunnel's
ingress routing.

**Sequence it as two changes, not one:** finish the login and **exercise it locally** — expect an
afternoon of redirect-URI and issuer-URL mismatches (the defaults mix `localhost` and `127.0.0.1`) —
*then* treat exposure as its own change carrying the rest.

---

## 6. THE GO / NO-GO CHECKLIST

**The deliverable.** Each condition is written as something **checkable** — a command whose output
decides it, a test that would fail, or an observable state — because a checklist of dispositions ("be
careful", "verify first") is not a checklist. **NO-GO means: do not open the tunnel.** "Framework state"
records what the code in this repo does as of this memo; "your install" items can only be answered on
the machine.

### Gate A — must hold before ANY public exposure. These are prerequisites, not hardening.

| # | Condition | How to check | Framework state |
|---|---|---|---|
| **A1** | **The deploy branch is protected**: no direct push, no force-push, required status checks. | `gh api repos/<owner>/<repo>/branches/main --jq .protected` → `true` | **Your install.** If the API returns 403 for protection or rulesets, it is a plan/visibility decision, not an engineering task. §4.1 |
| **A2** | **Every account with push is intended and 2FA-enabled.** | `gh api repos/<owner>/<repo>/collaborators --jq '.[].login'` — reconcile by hand | **Your install.** Each push-capable account is a key to the daemon. §4.1 |
| **A3** | **"Docs-only" no longer means "ends in `.md`."** A prompt-path denylist — `seneschal/modes/**`, `seneschal/SKILL.md`, `subagents/**/SKILL.md`, `persona/**`, `seneschal/references/**`, every `**/CLAUDE.md`, plus other harnesses' instruction files — forces the approval picker regardless of extension (`merge_guard.PROMPT_PATHS` / `AGENT_INSTRUCTION_GLOBS`). **One exemption: `seneschal/docs/CLAUDE.md`**, the docs router. | a test asserting a PR touching only `persona/persona.default.md` is NOT docs-only, and one asserting a PR touching only `seneschal/docs/CLAUDE.md` still is | **Ruled: "everything that runs, except the docs router."** Why the exemption: the router is an index with a one-line status per document, not an instruction, and every docs PR touches it to add an entry. Measured on the private install this was distilled from, the denylist *without* the exemption reclassified **every** docs-only PR — the large majority of them on `**/CLAUDE.md` alone — which would have repealed the docs-only grant rather than narrowed it; *with* the exemption roughly a third flipped. **Still on the docs side, named rather than widened:** the router itself; the one file in `merge_guard.DOCS_ONLY_ALLOWLIST` (named in `autonomy-policy.md`); a subagent's own reference files beside its `SKILL.md`; archon charter/profile prose. The per-path list lives in `MERGE_GUARD_SETUP.md` §5. §4.2 |
| **A4** | **The merge guard reads CI.** Pending / empty / red is a DENY on the docs-only path too. | `grep -c statusCheckRollup` over the installed `merge_guard.py` → non-zero; a test with a red rollup on a docs-only PR | **Check the installed guard.** Reuse the PR-sweep's existing green classifier rather than restating it. §4.2 |
| **A5** | **The public Worker authenticates every route.** `X-Twilio-Signature` validation on `/voice`, `/gate`, `/after-bridge`, `/voicemail`, `/push-call/ack`; auth on `/ws`; the daily budget enforced on the screener path; `to` allowlisted on `/push-call`. | `grep -rc "X-Twilio-Signature" phone/src/` → non-zero; `overBudget` called on the `/ws` screener path | **NO-GO.** No signature validation exists; `overBudget` is called only on the talk-mode path. **Independent of the tunnel — live wherever the Worker is deployed.** §4.3 |
| **A6** | **`health_listener.py`'s public-bind guard refuses `0.0.0.0` and RFC1918 without a token**, and the banner stops calling `0.0.0.0` "localhost". | a test asserting `_is_local_or_tailnet("0.0.0.0")` is `False` | **NO-GO.** It returns `True`. §4.5 |
| **A7** | **No credential that controls the exposed hostname's DNS or Worker deploys sits in the ambient user environment.** Every delegated agent inherits it. | list the user environment's variable names (never values) — no DNS/Worker-capable token present | **Your install.** Exposing a surface while the credential that controls its DNS is readable by every subagent inverts the order of operations. §4.8 |

### Gate B — must hold before the tunnel specifically.

| # | Condition | How to check | Framework state |
|---|---|---|---|
| **B1** | **The exposed cockpit cannot run in dev mode.** `COCKPIT_DEV_NO_AUTH` is unset in the exposed process, and with a tunnel configured, an empty/misspelled OIDC config yields `unconfigured` (503), never `dev`. | `GET /api/auth/status` reports `oidc`; a test pinning that dev mode refuses when a tunnel/public flag is set | **NO-GO.** Nothing ties dev mode to the bind; the setup chapter recommends it. §4.9(a) |
| **B2** | **`/api/ws` has its own `Origin` AND client-host check, independent of `auth_mode()`, and `dev` is a hard refusal there.** | `grep -c "Origin" cockpit/server/app.py` → a code hit, not a comment | **NO-GO.** No `Origin` check exists. §4.9(b) |
| **B3** | **The pending-login token cannot be replayed as a session token.** | a test asserting `read_session_token(secret, create_pending_login_token(...)) is None` | **GO.** Fixed; `test_session.TokenKindConfusionTests`. §5.2 |
| **B4** | **`secure=True` on both cookies**, plus an absolute session cap. | `grep -c "secure=False" cockpit/server/auth.py` → 0 | **NO-GO.** Two occurrences, each with a comment naming this exact moment. §5.3 |
| **B5** | **Rate limiting exists** on `/auth/login` and `/api/*` — in the app, not only at the edge. | `grep -ric "ratelimit\|throttle" cockpit/server/` → non-zero | **NO-GO.** None. §3 |
| **B6** | **`X-Forwarded-For` handling is decided explicitly** — `--proxy-headers --forwarded-allow-ips=127.0.0.1`, and `localhost_only` means something once loopback stops meaning "local human". **`FORWARDED_ALLOW_IPS=*` is never set.** | read the uvicorn argv; `FORWARDED_ALLOW_IPS` empty in the process environment | **NO-GO** (undecided). Today the safe behaviour holds by a library default nothing names or tests. §4.9(b) |
| **B7** | **The tunnel routes ONLY what is intended.** A `config.yml` with explicit per-hostname ingress; the break-glass port (8499), the daemon pipe (8471), the health listener (8765), Ollama, and every archon UI port are **not** routed; the decoy's hostname is separate from the cockpit's. | `cloudflared tunnel ingress validate` + read `config.yml` | **Your install.** A quick tunnel (`--url`) publishes a whole origin with **no authentication of any kind** — never use one here. §4.9 |
| **B8** | **Cloudflare Access (or an equivalent identity-aware proxy) sits in front of the hostname.** | an Access policy exists for the hostname | **NO-GO, and unwritten.** Not mentioned anywhere in the repo. **The cheapest item on the list; it neutralises most of Gate B by itself.** §4.9 |
| **B9** | **The decoy's rate limiter survives the proxy** (keys on the forwarded client, not `request.client.host`). | a test with `X-Forwarded-For` set | **NO-GO.** Collapses to one shared bucket. §4.9(e) |

### Gate C — should hold. Not blockers; each reduces blast radius or time-to-notice.

| # | Condition | Ref |
|---|---|---|
| **C1** | Third-party API credentials move out of the ambient user environment into per-tool env files the shell does not broadcast. Host action; no PR can do it. | §4.8 |
| **C2** | The scrub covers the family, not one name — at minimum `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` are refused or asserted-absent — and `presence.child_env` has a test asserting it; `sentinel.py --launch-cmd` passes `env=child_env()`. | §4.8 |
| **C3** | **A real rollback:** a tagged release point and a documented, *tested* "deploy this pinned SHA" procedure. A current `main` is not a rollback capability. | §4.8 |
| **C4** | **One owner notice per successful deploy naming what changed.** Today a hostile deploy and a healthy one produce the identical healthy stamp. | §4.8 |
| **C5** | No untracked code in `seneschal/state/` runs on a schedule; every scheduled task points at a tracked script or its documented wrapper; the break-glass supervisor is registered if relied on. | §4.7 |
| **C6** | The installed merge guard's detection perimeter matches its documentation (the §4.6 table), and any shape it does not detect is listed as unguarded. | §4.6 |
| **C7** | The approval picker shows **all** changed paths, or the count plus the highest-risk ones — not the first few. | §4.6 |
| **C8** | The allowlisted Telegram chat is a **DM, not a group**; if a group is ever needed, the allowlist moves from chat id to user id first. Both chat allowlists refuse to start empty rather than fail open. | §4.10 |
| **C9** | **Met.** `provenance_guard.py` is adopted — called from `rag_index.index_records`, with `mini_dream.py` writing its producer stamp — and the LLM distill's prompt fences the transcript as data. | §4.4 |
| **C10** | Gradle `distributionSha256Sum` + a wrapper-validation step; `--ignore-scripts` where the build allows. | §4.10 |

### The honest summary of the checklist

- **A1 may not be an engineering task.** If branch protection is unavailable on the hosting plan, it is a
  purchase or a visibility decision — and everything else sits downstream of it.
- **A3 is ruled, and the ruling needed its exemption.** The denylist as first written would have flipped
  every docs-only PR; exempting the one router is what makes it a narrowing instead of a repeal.
- **B8 — an Access policy — is the cheapest item on the whole list** and would neutralise most of Gate B
  by itself.
- **A5 and A7 are not about the tunnel at all.** A5 is live wherever the Worker is deployed; A7 is the
  credential that would control the tunnel's own DNS. Both are in Gate A because exposure is the wrong
  thing to do *next* while either holds.
- **Most items are verifiable by a single command or a single test** — the property that lets someone
  else turn this list into gates.

---

## 7. Non-goals

- **Building any of §6.** This document changes no behaviour, adds no rule to any prompt-side file, and
  ships no code.
- **Re-arguing the deferral of public exposure** (`cockpit-spec.md` ruling 1). This is the input to
  reversing it, not an argument for reversing it.
- **Widening or narrowing the approval gate.** A3 makes the *classifier* match the *policy already
  written* in `autonomy-policy.md`; it proposes no new policy.
- **Any exploitation.** Nothing was connected to, sent, or run against a live surface.
- **Defending against the owner, or against Anthropic.** §2.3.
- **Reading private content or secret values.** Counts, names, shapes and field names only.

---

## 8. What this document could NOT determine

- **Whether hostile text is already sitting in an install's memory corpora.** This is the check that
  would convert §4.4 from *a live mechanism* into *a demonstrated compromise*, and it requires reading
  the owner's content — so only the owner, or someone with their explicit permission, can do it. It is
  the single highest-value follow-up on any real install.
- **Whether a deployed Worker matches `phone/src`.** Deployment is manual, with no version stamp or build
  id on `/status`. Every §4.3 finding is read from the tree.
- **Whether a specific injected instruction actually wins** — survives the distiller's framing, or takes
  the Watch peek's attention. Measuring that means running a model against crafted input. The mechanism
  and the absence of a fence are established; the success rate is not.
- **Whether any LAN host has ever reached the health listener.** It records route and byte count, never a
  peer address.
- **Whether an archon's own runtime sets its own Claude config directory.** If it does, archon sessions
  run with **no hooks at all**.
- **Whether every test is safe against a state-defaulting flag.** A test whose state directory defaults
  to the live `seneschal/state/` can mint or spend real state — which, given §4.7(b), is the same thing as
  acting one drain-tick later. The `SENESCHAL_STATE_DIR` override exists; whether every suite honours it
  deserves its own pass.
- **The merge guard's behaviour.** `merge_guard.py` and `MERGE_GUARD_SETUP.md` are named by
  `autonomy-policy.md` but are not in this tree, so §4.6 and A4 are conditions to check against whatever
  guard is installed, not findings about shipped code.

**One suspicion, unsubstantiated, recorded rather than asserted:** an archon UI that puts scraped values
(e.g. a job posting's apply URL) into `href="…"` behind `html.escape` does not block a `javascript:`
scheme, and any unescaped `innerHTML` interpolation is worse. If an archon UI ever renders raw scraped
text, it composes with §4.9 into a same-site path to the authenticated cockpit **even after OIDC is
stood up** — because SameSite is per-*site* and a port is not part of a site, so `127.0.0.1:<archon
port>` is same-site with `127.0.0.1:8760`. Worth twenty minutes from someone who can check the data
provenance of each archon UI.

---

## 9. How this document avoids being a disposition

A security memo fails when its recommendations reduce to "be more careful". The test applied here:

1. **It adds no rule to any prompt-side file.** No sentence is proposed for `persona/`,
   `seneschal/SKILL.md`, `seneschal/modes/`, or any `CLAUDE.md`. **Every checklist item in §6 is a
   command whose output decides it, a test that would fail, or a host action** — none is an instruction
   to a model. A PR citing this document as authority for adding a rule to a prompt file is misreading
   it.
2. **It scored the threat model that already existed** (§3) rather than writing a second one, and
   reports the mitigation that was never true (*the supervisor is the only process that can
   hard-reset*).
3. **It reports what holds**, specifically — §3, §5.1 — so that "not in the findings list" reads as
   *checked*, not *missed*.
4. **Its highest-value follow-up is one it could not perform** (§8, first item), and it says so instead
   of substituting something it could.

**Where it is at risk, stated rather than hidden.** This is prose. **Nothing mechanical ships with it** —
§6 is a checklist, not a check. Its one structural defence is that every item is written so that
someone else's PR can turn it into a test. **Judge it on whether §6 becomes gates. If a tunnel opens
with §6 unbuilt, this document was decoration.**
