# seneschal/docs/ — the design record

Specs, how-it-works pages and research memos. **Each file is the authority on its own subject**;
this is a router, not a summary — **one line per doc**: status + one clause. The full finding lives
in the doc's own file (under a `## Router entry` section where it has one), one hop out.

**Status:** `REFERENCE` — vocabulary: *BUILT* / *PARTIAL(...)* (qualifier required) / *SPEC-ONLY* /
*MEMO* / *REFERENCE* (this file and the `how-*` pages). **A spec header on `develop` reads "unbuilt"
until its PR merges.** Verified by `check_doc_status.py --enforce`.

**Measurement phases ship whole, never a field at a time** — a half-shipped measurement reads as a
finished one and gets cited before its missing half exists.

`spec-prompts/` holds the historical planning prompts some specs were written from; they are not
specs and carry no status.

## Whole-repo designs

- `rulings.md` — REFERENCE — the dated design-decision ledger (starts empty); `check_rulings.py`
  requires a row whenever a change narrates a new decision.

- `pluggable-backend-spec.md` — **PARTIAL(Phase 2 BUILT — the `Backend` contract
  (`seneschal/scripts/backends/`), `CodexWarmSession` over `codex exec --json`, the cockpit's third
  `backend` dial, all behind a config flag defaulting to claude-cli; Phases 3-5 and §4's
  pre-classifier router path unbuilt)** — the `claude -p` seam, exhaustively inventoried
  (`WarmSession`, the headless spawn sites, hooks, `--mcp-config`, the model-dial rank table); the
  codex session structurally cannot mount an outbound MCP server at spawn time, so that safety
  question is sidestepped rather than resolved; §6 specs dual-active claude-cli + codex-cli with
  dynamic routing (explicit prefix, shadow-first router pick, A/B comparison, failover).

## The daemon and its lifecycle

- `asyncio-daemon-design.md` + `asyncio-daemon-plan.md` — **PARTIAL(phases 0-3 BUILT)** — the
  reactive core: supervised tasks under one `asyncio.gather`, each behind `_supervise`.
- `how-to-add-a-daemon-task.md` — REFERENCE — the explicit gather-and-`_supervise` mechanism, the
  steps, and what not to do.
- `hung-turn-deadline-spec.md` — **PARTIAL(P1+P2 BUILT; §4 decided)** — a hang is a gap between
  stream events, never a cap.
- `seneschald-revive-spec.md` — **PARTIAL(BUILT + §8/§9)** — revives a dead daemon; §9 catches it
  alive on the wrong account.
- `stacked-pr-branch-deletion-spec.md` — **BUILT** — deleting a branch used to close every PR stacked
  on it.
- `mid-turn-interleave-spec.md` — **PARTIAL(phases 0+1 BUILT; phase 2 BUILT behind
  `--interleave-mode live`, shipped OFF)** — no true in-turn injection exists on this CLI; the
  classifier isn't reliable enough yet, so `live` stays off.
- `state-durability-spec.md` — **PARTIAL(core BUILT)** — zero-byte `state/` losses; `write_text` now
  refuses an empty overwrite.
- `log-rotation-spec.md` — **BUILT** — rotates a log around Windows's held-open-file rename limit.
- `session-continuity-spec.md` — **PARTIAL(phase 0 BUILT)** — the resume gate refuses more often than
  it resumes.
- `dependency-freshness-spec.md` — **SPEC-ONLY** — watch-and-report on stale host tools, never
  auto-upgrade.
- `session-coupling-spec.md` — **PARTIAL(phase 0 BUILT)** — a backgrounded command has no owner or
  lifecycle.

## What the assistant says, and what was said to it

- `how-inbound-becomes-a-reply.md` — REFERENCE — the path from a Telegram/Discord arrival through the
  one action queue to a delivered reply.
- `mouth-spec.md` — **PARTIAL(phases 0-1 module BUILT; send-site recording + daemon drain pending)**
  — the record of what the assistant actually said.
- `telegram-inbound-spec.md` — **PARTIAL(§2-§5 BUILT; §6a-§6c poller + picker halves BUILT, daemon
  wiring pending)** — attachments, swipe-replies, reactions, edits, pickers, albums; an unnamed
  `allowed_updates` type is never delivered at all.
- `telegram-capability-map.md` — **MEMO(built items marked inline)** — the Bot API documented method
  by method; only a handful are called.
- `telegram-send-document-spec.md` — **BUILT(sendPhoto + `--document`/sendDocument, both narrow — no
  `--filename` override, no `--topic` resolution, no outbound trace)** — overturns the capability
  map's "deliberate" verdict on outbound media; stdlib multipart so the token never reaches argv.
- `picker-state-marking-spec.md` — **PARTIAL(`telegram_ask.py` primitives BUILT; the marking pass
  arrives with the PR-sweep port)** — a Telegram reaction marks picker state; 😴 is derived every
  pass, never latched.
- `ask-provenance-spec.md` — **PARTIAL(phases 1-3 BUILT; 4 open)** — a picker's `origin` is now
  auto-stamped.
- `promises-ledger-spec.md` — **PARTIAL(phase 0 BUILT)** — a commitment record owned outside the turn
  that made it; phases 1-3 designed, not built.
- `substance-or-silence-spec.md` — **PARTIAL(rule + `turn_suppression.py` BUILT; landed predicate +
  daemon wiring pending; phase 3 is a counting exercise)** — a
  turn restating what the owner already has is a buzz with no fact in it (`../modes/chat.md`).

## Background work

- `background-jobs-spec.md` — **PARTIAL(BUILT + §3.7-§3.14)** — exactly-once completion push;
  concurrency capped 3 soft / 5 hard, agent jobs only; §3.13 detect/resume/rescue for a job that
  exits 0 unfinished; §3.14 a `PreToolUse` hook (`job_background_guard.py`) refusing the
  backgrounded-Bash call that causes it, before it runs.
- `job-origin-routing-spec.md` — **PARTIAL(phases 0-3,5 BUILT)** — a wrong attribution is worse than
  none, so nothing is inferred.
- `session-job-watcher-spec.md` — **SPEC-ONLY** — the daemon cannot wake a desktop session.
- `jobs-panel-depth-spec.md` — **SPEC-ONLY(§7 superseded)** — recommends against an emit protocol.
- `job-fanout-spec.md` — **SPEC-ONLY(closed)** — multi-agent fan-out already works with zero code
  changes; the one cost reading is in doubt, so re-measure before citing it. The structural gap
  stands: `jobs.py` counts jobs, not agents inside one, so nothing gates the width.
- `cancel-attribution-spec.md` — **BUILT** — a purposeful cancel still buzzes; fixed the tone, not
  the buzz; §12: a self-cancel from the assistant's own surface also skips the wake, so it isn't
  re-told why.
- `delegated-work-isolation-spec.md` — **PARTIAL(phases 1+3 BUILT)** — the collision unit is the
  checkout, not the branch.
- `notion-write-behind-outbox-spec.md` — **PARTIAL(BUILT; veto→delay; dead-letter
  classification)** — Notion backend only: journal then flush; a live session delays, never vetoes;
  a 4xx dead-letter is no longer assumed permanent.
- `ack-system-ownership-spec.md` — **SPEC-ONLY(redesign declined)** — measured zero lost, zero
  dead-lettered entries.
- `jobs-ended-unknown-spec.md` — **MEMO** — a finished job gets mislabeled `ended-unknown`; the
  reconciler's once-per-pass stale snapshot can clobber a shim's completed write; the narrow re-read
  fix (§9) shipped and measured zero recurrence (§10); the systemic decision stays open.

## Cost, context, and the root prompt

- `spend-levers-spec.md` — **PARTIAL(phases 0-1 BUILT, tee wiring pending)** — twelve levers on
  turn cost; diagnosis only.
- `usage-telemetry-spec.md` — **PARTIAL(phase 1 BUILT, loop wiring pending)** — reads the real
  plan meters; never speaks except on failure.
- `grounding-restructure-spec.md` — **PARTIAL(mode router + modes/ + sub-routers BUILT)** — the root split into a
  router + sub-routers.
- `read-first-retirement-spec.md` — **PARTIAL(the store + CLI BUILT — `standing_safety.py` /
  `state/standing-safety.json`; the digest itself RETIRED; the §4 classification/compression process
  and §6's completeness check remain unbuilt design)** — the READ FIRST digest section only ever
  grew; its jobs now have their own homes and the digest is gone.
- `session-trace-spec.md` — **PARTIAL(readers + panel BUILT, join keys pending)** — joins the
  scattered logs into one session.
- `context-budget-spec.md` — **PARTIAL(phases 0-1 + §14 BUILT)** — the byte ratchet + the blocking
  pointer check.
- `carry-over-region-spec.md` — **PARTIAL(`carryover_region.py` + `check_carryover_prose.py`
  BUILT; §4 decided — head ceiling stays report-only, a witness-only ad-hoc note goes to
  `scripts/notes.py`'s dated file, never carry-over; the Brief stays read-only on carry-over.md)** —
  two bounded regions (`WRAP_SEED`, `GENERATED`) plus a measured-not-enforced hand-written head,
  replacing Wrap's prepend-and-keep growth.
- `coverage-manifest-spec.md` — **SPEC-ONLY** — a digest/doc names what it covers as `path` + line
  range + a CRLF-normalized `sha256` in an HTML-comment JSON block; an (unbuilt) checker reports
  FRESH/MOVED/STALE/GONE per entry, reusing `check_context_pointers.py`'s path resolution,
  report-only until an allowlist exists.
- `budget-headroom-spec.md` — **BUILT(phase 0; further phases archived)** — headroom is generated in
  code and validated, independent of the byte cap.
- `context-budget-collisions-spec.md` — **PARTIAL(phases 1-3 BUILT; `--enforce-chain` unwired)** —
  chain-continuity + a rechain helper.
- `concurrent-pr-collisions-spec.md` — **PARTIAL(phase 1, 2R, 2b-as-a-job, 2a's detection + §5B BUILT as modules; daemon wiring pending)** — a
  picker names which other open PR shares its files; the daemon rebases BEHIND PRs and repairs
  ledger and Markdown conflicts, keeping both sides (§5A.9); a PR a still-running job opened is a
  draft until that job ends (§5B, `job_pr_draft.py`).
- `scripts-subdirectories-spec.md` — **SPEC-ONLY(rules axis CLOSED)** — `.claude/rules/` splitting
  measured net-negative twice.
- `register-notion-projection-spec.md` — **PARTIAL(the forward + `project-status` BUILT; the
  in-progress question decided and BUILT — `loops.py start` / `in_progress`; inert until the
  outbox's task-status op lands)** — Notion backend only: a register status change
  (`resolve`/`drop`/`hold`/owner-abandon) forwards to its Tasks row's own status through the
  existing outbox, no approval prompt.
- `observation-gate-spec.md` — **PARTIAL(phase 1 BUILT — the `observation`/`observation-complete`
  statuses, the gate schema, `loops.py`'s `observe`/`mark_observation_complete`,
  `observation_gate.py`'s scanner, Dream step 2i, the Brief's lead-in, `min_elapsed`'s `since`
  (§2.2); §8's seed table is proposed)** — a work item gated on evidence gets a scanner instead of
  going quiet for weeks after its bar is met.
- `proposed-learnings-lifecycle-spec.md` — **BUILT** — `learnings.py retire` moves a closed, stamped,
  30-day-old row into a sibling archive file.

## The cockpit

- `cockpit-spec.md` — **PARTIAL(v1-v5 + Jobs/Trace/Open-specs BUILT; supervision wiring pending)**
  — pipe, model dials + Fable delegation, Oikonomos, health panels, auth.
- `pre-exposure-threat-model.md` — **MEMO** — the pre-tunnel conditions; most of the top ones need
  no tunnel at all.

## Comms and reminders

- `how-the-approval-gate-works.md` — REFERENCE — how the act-low / ask-high gate actually works, end
  to end.
- `message-routing-spec.md` — **PARTIAL(`channel_declare.py` BUILT; phase 1-2 daemon wiring pending)**
  — a declared, resolved channel purpose
  decides a Telegram turn's destination, winning over the inbound thread unconditionally.
- `dynamic-topics-spec.md` — **PARTIAL(§0-§2 core minting BUILT, via a simpler mechanism than
  designed)** — a topic needs no code change at all; promotion into the owner's table, renames of a
  promoted purpose, sprawl controls and the act-low line remain open.
- `topic-mirroring-spec.md` — **SPEC-ONLY** — copying into a topic works but
  loses/misattributes attribution.
- `reply-marker-forcing-function-spec.md` — **PARTIAL(marker check + retry prompt BUILT; daemon
  wiring and §9's empty-reply guard pending)** — every reply names which message it answers,
  unconditionally.
- `slack-draft-and-hold-spec.md` — **BUILT(phase 1 whole feature)** — drafts from a pinned SSOT; the
  send is always ask-high.
- `voice-call-spec.md` — **PARTIAL(P1 BUILT — outbound talk mode: the Worker's `/push-call` gains
  `mode: "talk"`, dialing the owner's own number only and handing the call to `RelaySession` in an
  owner register with no screener tools; `push_call.py --talk` builds the context snapshot; P2
  inbound owner-call auth decided (caller ID + spoken passphrase) but unbuilt; P3-P4 unbuilt)** — a
  live voice conversation with the assistant, Worker-brained, not a relay to the home daemon.
- `reminder-cadence-mechanism-spec.md` — **PARTIAL(phase 1 BUILT)** — a cadence is an expression, not
  an enumerable list.
- `reminder-exact-time-scheduling-spec.md` — **BUILT** — reminders fire at arbitrary per-reminder
  times.
- `reminder-premise-spec.md` — **PARTIAL(Phase 0 BUILT, pure; Phase 0.5 BUILT — wired into the live
  seed path, report-only, decides nothing new; Phase 1 gated on §10.2/§10.3)** — a reminder has no
  concept of *why* it exists.
- `tomorrow-marker-spec.md` — **PARTIAL(§6 decided; §7 phases 0-2 BUILT; §3 retention prune wired
  into Dream; the daemon's morning-slot injection + picker callback pending)** — a first-class "prioritize tomorrow" marker: three act-low capture doors (a
  `<tomorrow>` chat tag, a `Tomorrow` flag on tasks/reminders, a Wrap-time Telegram picker) into one
  durable `state/` file (`scripts/tomorrow_marker.py`) the Brief leads with and the Wrap resolves
  (close/roll/drop) per item — one grid picker, never a prose list.
