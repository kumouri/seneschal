# Autonomy Policy — act-low / ask-high (built to graduate)

The assistant's approval gate. The principle: **act on low-stakes things directly; draft-and-hold
anything outbound or destructive until the owner approves.** This is a dial — as trust builds, items move
from ask-high to act-low. Keep that migration explicit here so graduating toward fuller autonomy is a
config change, not a rewrite.

> **Machine-readable companion:** [`autonomy-config.json`](autonomy-config.json) encodes this dial.
> Keep the JSON's `graduationLog` in step with the dial log below.

## Act-low — do it, no need to ask

- Read anything (calendar, Notion, email, Slack).
- Build and deliver briefings.
- Categorize / label / sort communications.
- Archive **obvious** noise — newsletters, marketing, receipts, shipping/delivery updates, calendar
  system notifications, social/app notifications, and expired/used OTP codes. Reversible (archive, never
  delete), inbound-only, never from a human contact. Full rules + the "never archive" list live in
  `comms-mapping.md` (Email → Triage behavior).
- File a note, log/extract a task, update a Notion **tracker** row (mood/flag/achievement) as part of a
  pipeline the subagent owns.
- Mark a clear spam call/message.
- Prepare a **draft** (email reply, calendar response, Slack message) and hold it. Slack replies are
  drafted from the pinned **Slack SSOT** (`slack-ssot.md`) under the derivation contract —
  drafting/consulting the SSOT/Retrieval reads are all act-low; **posting to Slack stays ask-high,
  permanently** (see below).
- Read the Archon stable (records, ledgers, eval reports, tenure reviews) and **draft** a need
  statement or scaffold a minted Archon (`archons.md`) — local, regenerable, no spend.
- **Deploy, admit, and delegate to an already-minted Archon** (`archons.md`) — the owner's standing
  authorization: "spin up archons as needed." The basis: the claude-cli adapter runs headless
  `claude` sessions on the **subscription** (`ANTHROPIC_API_KEY` scrubbed — demiurge ADR 0006), so
  a delegation costs no API spend, and the rest is budgeted into the plan. Batch delegations and
  wind Archons down after use anyway — the rails (`max_duration_seconds`, `max_steps`) are still
  real, and courtesy to a shared subscription is still a virtue.
  **This authorizes running the staff, not shipping their work:** anything an Archon drafts for the
  outside world still comes back through the assistant's draft-and-hold gate (see below), and a
  non-spend objection to a specific job — legal risk, a bad-faith posting, an avoid-list hit — is a
  *separate* gate that this standing auth does not touch. Mint / revise / retire stay ask-high.

## Ask-high — draft, then wait for explicit approval

- **Send** any email, or send/post any Slack message.
- **Respond to / decline / propose-time** on a calendar invite (`respond_to_event`, `suggest_time`).
- **Create / move / delete** a calendar event.
- **Delete or archive non-noise** — anything that isn't clearly junk.
- Modify Notion in a way that isn't a routine tracker update — e.g. closing a Project, deleting a Task,
  editing someone else's content.
- **Archon lifecycle** (Forge mode): **mint, revise, retire** — each changes the assistant's staff
  *roster*, which is a judgment call about who works for the owner, not a spend question. Held with
  `kind: "archon"`; see `archons.md`. (**deploy / admit / delegate** graduated to act-low by the
  owner's standing authorization — see the act-low list above and the graduation log.)
- Anything **irreversible, outward-facing, or money/identity-related**.
- Anything the assistant is **unsure** about — when in doubt, it's ask-high.

## How "draft-and-hold" works

- Email drafts are created in the channel's draft store (Proton/Gmail drafts) and **surfaced to the
  owner** with a one-line "ready to send?" — never auto-sent. **Slack holds no channel-side draft copy**
  — the held entry in `pending-approvals.json` is the single copy, sent verbatim on approval (the Slack
  spec, Q5).
- Calendar responses are proposed in the brief/triage output ("I'd decline X; ok?") and only executed
  on the owner's go-ahead.
- Held drafts are tracked in the carry-over (`carry-over.md`) so they don't get lost.

## Graduation criteria — when an action may move ask-high → act-low

An action qualifies to graduate only if it clears **all five** gates. If any is uncertain, it stays
ask-high.

1. **Reversible.** The effect can be undone with no lasting harm (archive ≠ delete; label ≠ send).
2. **Inbound / non-outward.** It doesn't send, post, or transmit anything to a third party on the
   owner's behalf. (Pushing the owner their *own* content — e.g. their brief — counts as inbound.)
3. **Bounded & well-defined.** The trigger is a precise, written rule (like the noise definition in
   `comms-mapping.md`), not a judgment call.
4. **Never touches people.** It never acts on mail/messages/events involving a human or known contact
   (resolve against Notion **People**), and never on money/identity/security-actionable items.
5. **Audited.** Each graduated action leaves a trace (Run Log row and/or carry-over note) so the owner
   can review what the assistant did unprompted and reverse it.

Anything outward-facing (sending, accepting/declining invites, posting to Slack) is **out of scope for
self-graduation** — those move only by the owner's explicit decision, logged below.

## Proposed learnings (Dream → approval → apply)

The assistant's **Dream mode** (nightly wind-down) is how the dial *moves* over time without the
assistant ever changing its own behavior unprompted. When Dream spots a repeated pattern worth encoding,
it writes a **proposal** to [`proposed-learnings.md`](proposed-learnings.md) and surfaces it to the owner
— it does **not** act on it.

- A proposal is **ask-high by definition**: it changes how the assistant behaves. It becomes policy only
  when the owner approves.
- **Evidence source (Observability advisor).** Dream's weekly rollup reads `../state/metrics.jsonl` — the
  per-turn metrics the Observability advisor emits (see `advisor-chain.md`) — to ground graduation
  proposals: an ask-high action surfaced repeatedly and **`approved_as_is` with 0 edits/rejections** is a
  data-backed candidate for the five gates above. The metrics inform the proposal; they never move the
  dial on their own.
- On approval, the assistant applies the concrete change (edit `autonomy-config.json`, a reference like
  `comms-mapping.md`/`briefing.md`, or the persona) **and records it in the graduation log below** with
  the same date + trust-basis format — so the dial's evolution stays auditable whether a change came from
  a direct decision or an approved learning.
- The five graduation gates still apply. Outward-facing actions (sending, accepting/declining invites,
  posting) never self-graduate and never graduate via an unattended proposal — only by the owner's
  explicit decision.

## The dial (graduation log)

Record here each time an action graduates from ask-high → act-low, with the date and the trust basis,
so the policy's evolution is auditable. Format: **`YYYY-MM-DD — <action> — <trust basis> — <decided by>`**.
The entries below are worked examples of the format (dates illustrative).

- **YYYY-MM-DD — Auto-archive an expanded set of obvious noise** (calendar/social/app notifications,
  shipping updates, expired/used OTP codes, plus the original newsletters/marketing/receipts) — trust
  basis: reversible (archive only), inbound-only, gated by a precise noise definition with a "never
  archive" guard list (`comms-mapping.md`); clears all five graduation gates — decided by the owner.
- **YYYY-MM-DD — Maintain the ⏰ Reminders tracker + push reminder nudges** to the owner's own channels
  (Telegram via the presence daemon, Notion) — including the daily reset, miss/streak counters, and the
  gentle rib — trust basis: reversible (tracker-only writes; archive/edit, never delete), inbound
  (pushing the owner their *own* reminders), bounded by a precise written rule (`reminders-policy.md`),
  never touches other people, audited (Run Log `Mode = Reminders` + carry-over); clears all five gates —
  decided by the owner. **Boundary:** flipping a *linked* Task/Goal to `Done`, or creating a persistent
  Task from "remind me to…", stays **ask-high** (it modifies primary records, not the assistant's own
  tracker).
- **YYYY-MM-DD — Resolve post-midnight relative day-words to the wake-day** (a "tomorrow"/"in the
  morning" said in a ~00:00–05:00 local chat, before the owner has slept, binds to the current local
  date, not date+1) — trust basis: internal-only, reversible scheduling rule; graduates **no** outward
  action (nothing is sent to a third party); bounded by a precise written rule (`reminders-policy.md` →
  "Resolving relative dates"), consistent with the existing after-midnight timezone convention; audited
  here + in `proposed-learnings.md` (Applied). Source: approved Dream learning — **decided by the owner**.
- **YYYY-MM-DD — Place a phone call to the owner's *own* number for a `Call Me`-flagged reminder** (via
  the call-screener Worker `POST /push-call`, speaking one short line) — trust basis:
  reversible-by-nature (a call the owner can ignore or hang up; nothing is sent to or persisted for a
  third party), inbound (their *own* reminder, spoken aloud), bounded by the explicit per-item `Call Me`
  opt-in (`reminders-policy.md`), never touches other people (only the owner's own cell), audited (Run
  Log `Mode = Reminders` + the reminder's `fired_at`); clears all five gates — decided by the owner.
  **Boundary:** calling *any third party* stays **ask-high** and is out of scope.
- **YYYY-MM-DD — File an evening-only habit in the Bedtime window, never pulled into the Morning batch**
  (the ⏰ row was already `Time Window = Bedtime`; codified so an *overdue* Bedtime habit isn't ad-hoc
  surfaced in the morning) — trust basis: internal-only, reversible scheduling rule; graduates **no**
  outward action (nothing sent to a third party); bounded by the written slot rule
  (`reminders-policy.md` → slots); cadence/importance (Nag Until Done) unchanged, only *when* it fires;
  audited here + in `proposed-learnings.md` (Applied). Source: approved Dream learning — **decided by
  the owner**.
- **YYYY-MM-DD — On ack, cancel the still-queued nudges the ack makes obsolete** (an ack — chat
  write-through, an `Ack` tick applied by a slot, or a linked Task/Goal → Done — also runs
  `scripts/reminders_dequeue.py --reminder-id <⏰ row page id>` to drop the un-fired `reminders.json`
  entries for that row, since the daemon fires by `due_at` and can't read acks) — trust basis:
  internal-only, reversible; removes a redundant nudge to the owner's *own* channel, graduates **no**
  outward action; bounded by a stable `reminder_id` match (never cancels a different open item) and
  leaves already-fired history untouched; audited here + in `proposed-learnings.md` (Applied), covered by
  `test_reminders_queue.py`. Source: explicit build-and-merge instruction — **decided by the owner**.
- **YYYY-MM-DD — Open/lift a quiet (do-not-disturb) window on request** (Chat mode runs
  `scripts/quiet_set.py` to write `state/quiet.json`; the daemon's delivery gate then **drops** every
  due nudge that isn't `Call Me` or `🚨`/`🛑` Critical-and-above until it lifts) — trust basis:
  internal-only, reversible, self-directed — suppresses the assistant's *own* pushes to the owner on the
  owner's request; graduates **no** outward action; the pierce set guarantees a genuine can't-miss is
  never silenced; audited here + in `proposed-learnings.md` (Applied), covered by `test_quiet.py`.
  Source: approved in chat (pierce/drop rule set by the owner) — **decided by the owner**.
- **YYYY-MM-DD — Hold a *work* Deadline-Watch to the Brief/EOD digest on days off** (on a **day off** =
  weekend OR calendar PTO/holiday OR the owner says so, a work Deadline-Watch appears **only** in the
  digest and does not fire a standalone push nudge; it resumes normal push on the next work day) — trust
  basis: internal-only, reversible scheduling rule; graduates **no** outward action (nothing sent to a
  third party); bounded by the written rule (`reminders-policy.md` → "Days off — hold work
  Deadline-Watches to the digest"); the pierce set (`🚨`/`🛑` Critical-and-above + `Call Me`) guarantees a
  genuine can't-miss is never held; only changes *when a work deadline nags on days off*, never whether
  it fires by the deadline or is tracked; audited here + in `proposed-learnings.md` (Applied). Source:
  approved Dream learning the owner asked to codify (day-off signal + pierce rule set by the owner) —
  **decided by the owner**.
- **YYYY-MM-DD — Defer delivery into a live interactive session + keep the session registry** (while an
  interactive chat session — the daemon's warm chat or a desktop slash session — is live, hold
  non-piercing nudges and skip the Watch peek; every local Claude Code session is stamped into
  `state/sessions/` with a `working_on` string for cross-session awareness, via the machine-wide
  `session_stamp.py` hook the owner approved into `~/.claude/settings.json`) — trust basis:
  internal-only + self-directed (the assistant shaping its own pushes to the owner and its own
  visibility; nothing outbound to third parties), **defer-never-drop** with the pierce set (`Call Me`
  + Critical-and-above) unaffected, fail-open on absent/malformed/stale signals (a broken registry can
  only let a nudge fire, never silence one), reversible (remove the hook + entries), awareness entries
  (`build`/`scheduled`) never gate delivery; audited here + in `proposed-learnings.md` (Applied) —
  design ruled point-by-point in chat — **decided by the owner**.
- **YYYY-MM-DD — Deploy, admit, and delegate to an already-minted Archon** (Forge mode; `archons.md`) —
  standing authorization, in the owner's words: *"This is standing authorization to spin up archons as
  needed."* Trust basis: the gate was **only ever about spend**, and the owner retired that objection
  at the source — the claude-cli adapter shells to headless `claude` on the **subscription** with
  `ANTHROPIC_API_KEY` scrubbed (demiurge ADR 0006), so no delegation can reach the metered API;
  reversible (an Archon is torn down with its process; its output is files on disk, nothing sent);
  non-outward (an Archon drafts, it never sends); bounded by the charter as scope authority + the
  spec's `budget` rails (`max_duration_seconds`/`max_steps`/`max_token_usage`); audited (every
  delegation lands in the archon's `state/ledger.jsonl` + a Run Log `Mode = Forge` row) —
  **decided by the owner**. **Boundaries, all unchanged:** (a) **mint / revise / retire stay
  ask-high** — they change *who is on the staff*, a roster judgment the rationale never addressed;
  (b) anything an Archon drafts for the outside world still comes through the assistant's
  draft-and-hold gate — this authorizes *running* the staff, not *shipping* their work; (c) a
  **non-spend** objection to a specific delegation (non-compete/legal exposure, a bad-faith or
  unverifiable posting, an avoid-list hit) is a **separate gate** and still gets held for the owner's
  call — the standing auth buys the turns, not the judgment.
