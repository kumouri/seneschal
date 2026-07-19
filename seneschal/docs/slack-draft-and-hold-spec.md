# Slack draft-and-hold — reply drafting design

**Status:** BUILT — Phase 1 shipped (owner sign-off; SSOT seeded + Q4 revised during sign-off). ·
**Owner:** the assistant · **Scope:** `subagents/slack-triage/SKILL.md`, `seneschal/references/` (a new
pinned SSOT reference + small edits to `comms-mapping.md` / `memory.md` / `advisor-chain.md` /
`autonomy-policy.md`), additive fields on `seneschal/state/pending-approvals.json`, and the daemon
`slack-mcp.json` auto-detect (Q9). Markdown-first; one small daemon code change (Q9), no new runtime
dependencies, no new poller, no new approval system.

> **Revision — build-time (the owner, during SSOT sign-off):**
> - **Q4 revised — variant B deferred.** The unsigned/as-the-owner variant is **dropped for now**; drafts
>   are **A-only / always signed**, and the approval grammar is a plain `send <id>` (no `a`/`b` selector).
>   `pending-approvals` carries a single `body`, not `bodyA`/`bodyB`. This also removes the persona tension
>   B raised — the assistant now always signs. B can be re-enabled later.
> - **New: a name-gated *restricted facts* tier** in the SSOT — a middle tier between "work-public standing
>   facts" and the "never state" fence. A **client-confidential** fact (e.g. `project_x`) is statable only
>   with an allowlist of named people — in a DM, or in a channel/group-DM an allowlisted person has
>   themselves cleared by raising it there (which also expands the cleared audience to that space's members;
>   a durable allowlist write stays a gated SSOT update); otherwise the escalation phrase. Enforced by
>   Critique's privacy check alongside the fence.
>
> The sections below describe the original design (including the A/B mechanic) **for the record**; the
> shipped implementation reflects the revision above. The 11 rulings block near the end is unchanged
> history.

## Why

Slack Triage already classifies inbound Slack into **needs a reply / FYI / noise** and drafts replies
in-flow — but the drafting half is under-specified prose: drafts live only in the triage transcript,
they aren't reliably registered in the held-approval loop, and nothing constrains *what facts a draft
may assert*. Two gaps this design closes:

1. **Drafts must ride the real held-approval loop** — `pending-approvals.json` + the store carry-over,
   stable short ids, `send <id>` / `drop <id>` across chat / Telegram / store comment — exactly like held
   email drafts, so a Slack draft survives the session that wrote it and can be approved from the owner's
   phone.
2. **Drafts must be generated from a pinned single-source-of-truth (SSOT)** — a canonical, owner-curated
   fact sheet the assistant drafts from — so replies to coworkers stay consistent across weeks and
   **never invent facts**. This is the heart of the ask (the owner's requirement).

## Non-goals

- **No new Slack poller or daemon task.** Drafting rides the existing Slack Triage pass (scheduled,
  on-demand, or a Watch escalation into Triage). The daemon's job stays reminders + chat + Watch cadence.
- **No parallel approval system.** The loop in `references/memory.md` → *Held approvals* is reused
  verbatim; this spec only adds Slack-specific fields and rules, all additive.
- **No auto-send, ever, and no self-graduation path.** Posting to Slack is outward-facing; per
  `autonomy-policy.md` it is permanently outside self-graduation — it moves only by the owner's explicit,
  logged decision (and this spec does not propose moving it).
- **No workspace bot identity.** Sends go through the connected Slack MCP (the owner's account —
  `U00000000` in the examples; a per-install value); standing a separate assistant Slack app/bot user is
  out of scope (noted in open questions for the persona implications).
- **No new dependencies.** Everything here is Markdown + the existing MCP tools; if Phase 2's optional
  code rail is built it is stdlib-only.

## How it rides the existing machinery

```
inbound Slack ──▶ Slack Triage (existing pass; also reachable via Watch escalation)
                    │  classify: needs-a-reply / FYI / noise
                    │  draftable? ──▶ draft from SSOT + Retrieval (act-low)
                    ▼
              Critique (order 35): register / identity / faithfulness / privacy / concision
                    ▼
              Gate (order 40): ask-high ──▶ HOLD
                    ├─ append pending-approvals.json  (kind: "slack", stable id)
                    ├─ mirror to the store carry-over (system of record)
                    └─ surface: triage summary (pull) + Telegram "send a7 / drop a7" (push, vital-few)
                    ▼
              the owner decides (chat / Telegram / store comment — existing detection path)
                    ├─ send <id>  ──▶ freshness re-check ──▶ slack_send_message ──▶ status: sent
                    ├─ drop <id>  ──▶ status: rejected
                    ├─ edit <id>… ──▶ revise, re-hold (same id; metrics: edited_before_approve)
                    └─ send fails ──▶ status: failed — kept in carry-over, surfaced, manual re-approve
```

## 1 · Trigger conditions — what gets a draft

Drafting is a **sub-step of the existing triage classification** (Slack Triage step 3). No message gets
a draft unless it was first classified **needs a reply** (a direct question/ask to the owner in a DM, an
@-mention, or a thread they're in). Within that set:

**Auto-draft (default-on, pending open question 3)** when *all* hold:

- The message is in scope: a **DM** or a **direct @-mention** of the owner's `user_id`. Busy-channel
  chatter they weren't named in is surfaced but not drafted unless they ask.
- The sender is a human (bots excluded — already the triage default).
- The reply is **derivable**: every fact the reply would need traces to the SSOT, a live Retrieval read
  (calendar / the store), or the thread itself (the derivation contract, §2). Typical draftable shapes:
  scheduling/availability ("can you meet Thursday?"), status of a tracked deliverable ("where's
  `project_x`?"), logistics, acknowledgements, "who owns X" answerable from the store.
- The reply does **not** require the owner's personal judgment, technical opinion, negotiation, or
  anything interpersonally loaded (disagreement, feedback on a person, anything with heat). Those are
  **surfaced without a draft**, with an offer: *"want me to take a swing at a reply?"* — draft-on-request
  is always available for any needs-a-reply item.

**Never draft:** FYI/noise items; anything from an unknown sender that smells like phishing/social
engineering (surface it, flagged); anything whose honest answer would require facts behind the SSOT's
privacy fence (§2) — those get the escalation phrase or no draft at all.

**Volume guard.** A triage pass holds at most **3 auto-drafts** (Prioritization: the triage summary is a
pull surface and shows *everything* needing a reply; the drafts — and especially the Telegram
"ready-to-send?" pings — are the push side and stay vital-few). Items 4+ are surfaced draft-on-request.

**Watch.** Watch itself never drafts (it's a gate, not a doer). A hot Slack item escalates to Triage as
today, and the *escalated Triage run* may draft under the same rules above.

## 2 · The pinned single-source-of-truth (SSOT)

### What it is

A single, owner-owned Markdown fact sheet — **`seneschal/references/slack-ssot.md`** — that is the *only*
standing-facts source a Slack draft may assert from. It is "pinned" in both senses: pinned in place (one
canonical file, versioned) and pinned in time (an explicit `Last reviewed:` date, so staleness is
visible, never silent).

### Where it lives — recommendation: a tracked repo reference

**Recommended: `seneschal/references/slack-ssot.md`, tracked in git.** Rationale:

- **Markdown is canon** (global rule) and a tracked reference gives an audit trail — every change to
  what the assistant is allowed to tell coworkers rides a diff/PR, same as `comms-mapping.md`.
- **Zero runtime reads.** It's a baked-in reference (the repo's #1 doctrine: no runtime schema/context
  fetching) — available to any session, including ones without store hands, and it costs nothing
  against the backend's rate limits.
- It composes with **Dream's gated-proposal machinery**: Dream can propose SSOT updates as a PR, never
  apply them silently (§ *kept current*, below).

**Alternative (open question 1):** a store page (e.g. Notion — phone-editable, no git required to update
a fact) — at the cost of a runtime read per draft, no diff trail, and a second place for truth to drift.
A hybrid (store edit-surface mirrored into the repo file by Dream) buys phone editing but imports
sync-drift complexity; not recommended for v1.

### Skeleton (the file the owner signs off and then owns)

```markdown
# Slack SSOT — pinned facts the assistant may draft from
Last reviewed: YYYY-MM-DD (by the owner)

## Identity & signature
- The assistant signs every Slack draft as itself; the exact signature line lives here (see open
  question 4).

## Standing facts (safe to state to coworkers)
- Role/title, team, manager, working hours + the owner's timezone.
- Current work-public commitments (e.g. `project_x` — release and code-freeze dates) —
  ONLY facts already public inside the team.

## Availability & scheduling policy
- How the assistant answers "can <owner> meet at X?" — check the calendar live; what it may/may not
  commit to (e.g. propose only, never accept a meeting — that's the calendar-steward's ask-high lane).

## Approved canned answers
- Verbatim-approved responses for recurring asks (standup status shape, "where do I file X", etc.).

## Never state (the privacy fence)
- Health, therapy, family/caregiving, personal identity, finances, identity/security details, anything
  from the personal-not-outward-facing set — categorically, regardless of who asks.
- Anything not in this file and not derivable per the contract below.

## Escalation phrase
- The default honest deflection: "I'll check with <owner> and get back to you." (exact wording theirs)
```

### The derivation contract — how a draft cites the SSOT

Every **factual claim** in a Slack draft must trace to exactly one of three source classes:

1. **The SSOT** — a standing fact, policy, or canned answer from `slack-ssot.md`.
2. **A live Retrieval read** resolved *at draft time* through the Retrieval advisor's normal pipeline
   (calendar availability, a tracked Task/Project status, a People lookup — `store-query`) — dynamic
   facts are never copied into the SSOT; the SSOT instead says *how* to fetch them (e.g. the
   availability policy).
3. **The inbound thread itself** — quoting or acknowledging what the sender said.

A claim that fits none of the three **must not appear**: the draft uses the SSOT's escalation phrase
instead ("I'll check with <owner>…"), or the item is surfaced without a draft. **"I don't know" always
beats a plausible invention.**

Each held approval records its citations in a `sources` array (schema in §3) — e.g.
`["ssot#availability", "calendar:2026-07-16", "store:project_x", "thread"]` — so the owner can audit
*where every fact came from* before saying `send`. **Critique's faithfulness check (order 35) is the
enforcement point:** a claim it can't trace to one of the three classes is a fail → it revises the claim
out (revise-once-and-note) or flags and holds; it never invents a fix. The privacy fence maps to
Critique's privacy check the same way.

### How it's kept current

- **The owner edits it** — it's their fact sheet; the assistant never changes what it's allowed to say on
  its own.
- **Dream proposes, never applies.** When Dream notices drift (carry-over says a deliverable shipped but
  the SSOT still says "in flight"; a canned answer contradicted twice in edits), it drafts a gated SSOT
  update via the existing proposed-learnings → PR path. Merging records the proposal; the change is
  policy only on the owner's approval — identical to every other behavior-shaping edit.
- **Staleness is loud, not blocking.** If `Last reviewed:` is older than the staleness threshold
  (proposed: 30 days — open question 8), the Brief flags it once and every held Slack approval carries a
  one-line "⚠ SSOT last reviewed <date>" note. Drafting continues (standing facts rot slowly); the note
  keeps the pin honest.
- **`edited_before_approve` is the feedback signal.** When the owner repeatedly edits drafts the same way
  before approving, that's Observability evidence (`metrics.jsonl` → `correction`) that the SSOT or a
  canned answer is wrong — Dream's weekly rollup turns it into an SSOT-update proposal.

## 3 · The draft-and-hold flow, end to end

Reuses the **Held approvals** loop (`references/memory.md`) step for step; Slack-specific behavior noted
inline.

**Hold (when Triage drafts a Slack reply):**

1. **Draft** from the SSOT + Retrieval under the derivation contract (§2) — act-low.
2. **Critique** reviews (register / identity / faithfulness / privacy / concision), revises once and
   notes what changed; can't-fix → flag-and-hold with the issue named.
3. **Channel draft store — none for Slack (proposed).** The loop's step 1 ("write the draft to its
   channel store *where applicable*") is a no-op here: unlike Proton/Gmail drafts, an MCP-written Slack
   draft is a second mutable copy with no reliable programmatic cleanup on reject — orphaned drafts in
   the owner's Slack client. The held entry's stored text is the single copy; on approve it is sent
   **verbatim** from the store. (`slack_send_message_draft` remains available for an explicit "leave it
   in my Slack drafts instead" — open question 5.)
4. **Append to `pending-approvals.json`** with the next stable short id from the *existing single id
   space* (`a<N>` — one sequence across email/slack/calendar, so "send a7" is never ambiguous), and
   **mirror to the store carry-over** (system of record), as today.
5. **Surface it:** the triage summary lists every draft in full (pull → show all); if the owner isn't
   live, a Telegram push per the vital-few rule — *"Drafted a reply to Alex in #team — reply `send a7`
   or `drop a7`."*

**Schema — additive fields on the existing `pending-approvals.json` entry** (existing fields unchanged;
`state/README.md` gets the update when built):

```json
{
  "id": "a7",
  "kind": "slack",
  "channelRef": "slack:C00000000:1720900000.123400",
  "to": "Alex (#team, thread)",
  "summary": "Reply to Alex re: review timing",
  "bodyPreview": "Hi Alex — <assistant> here (<owner>'s assistant). They're free after…",
  "body": "<the full verbatim text that will be sent>",
  "sources": ["ssot#availability", "calendar:2026-07-16", "thread"],
  "critique_note": "tightened the tone; removed an unverifiable date",
  "thread_seen_ts": "1720900000.123400",
  "created_at": "2026-07-13T22:40:00Z",
  "status": "pending"
}
```

- `channelRef` = `slack:<channel_id>[:<thread_ts>]` — everything the send call needs (a DM uses the DM
  channel id; a threaded reply carries the thread ts).
- `body` is the **verbatim send text** — what the owner approves is exactly what posts, no re-generation
  at send time (re-drafting after approval would un-approve it).
- `sources` + `critique_note` make the approval auditable at a glance (§2).
- `thread_seen_ts` = the newest message in the thread at draft time — powers the freshness re-check (§5).
- `status` gains one additive value: `approved` (approved-but-not-yet-sent; §5, the Slack-hands gap).

## 4 · Approval grammar & detection

**Unchanged from the existing loop** — same words, same surfaces, same detection path:

- **`send <id>`** (also `yes` / `approve` / `ship it`, + id or bare when exactly one is pending) → approve.
- **`drop <id>`** (also `no` / `reject` / `don't send`) → reject.
- **Edit before approve** (additive, and consistent with the Observability `correction` taxonomy):
  *"change a7 to say …"* / `edit a7: <text>` → revise the `body`, re-run Critique, **re-hold under the
  same id** with a fresh preview; when the owner then approves, the turn's metrics record
  `edited_before_approve`. An edit is never a send.
- **Ambiguous → ask, don't guess** (multiple pending + bare "send"; a reply that could be chat or a
  decision).

**Detection surfaces (existing paths, no new machinery):**

- **Chat / Telegram / Discord** — the daemon delivers the owner's reply to the warm Chat session, which
  reads intent against the open `pending-approvals.json` entries.
- **Store comment (Notion backend)** — Watch's peek checks for new comments on the carry-over /
  pending-approvals surface and reads the same intent (LLM-tier read, as today).

## 5 · Send + failure paths

**On approve — freshness re-check first, then send:**

1. **Re-read the thread** (`slack_read_thread` / channel history since `thread_seen_ts`). If the
   conversation moved after the draft was written (someone answered already, the ask changed), **do not
   send** — re-surface: *"the thread moved since I drafted a7 — Alex said X. Still send, revise, or
   drop?"* A stale reply posted to a third party is worse than a beat of delay.
2. **Send verbatim** via `slack_send_message` (channel + thread ts from `channelRef`, text = `body`).
3. On success → `status: "sent"`, remove from open carry-over, Run Log trace, metrics `correction`
   recorded.

**On reject** → `status: "rejected"`, remove from open carry-over, trace. Nothing was ever written to
Slack, so there is nothing to clean up (a consequence of §3's no-channel-draft choice).

**On send failure** → `status: "failed"`, **kept in carry-over** so it isn't lost, surfaced with the
error verbatim. **No automatic retry, ever:** a Slack post is outbound to a third party and a blind
retry risks a double-post (was the failure before or after the message landed?). Recovery is manual and
explicit — the assistant first re-reads the channel to check whether the message actually landed,
reports what it found, and a fresh `send <id>` from the owner re-attempts. This is the "never claim a
write you didn't make" rule applied outbound: report honestly, hold state, let the owner re-approve.

**The Slack-hands gap (real constraint, named honestly).** The Slack MCP is wired into the interactive
Claude Code app; the **daemon's warm session is not guaranteed to have it** (it gets the store's MCP
config, nothing Slack). So a `send a7` arriving over Telegram may be *understood* by a session that
cannot *execute* it. Behavior: the session records `status: "approved"` (approved-but-unsent, kept in
carry-over) and says so plainly — *"approved — I don't have Slack hands in this session; it'll go out
from the next Slack-capable run, or open /assistant and say `send a7` there."* Any Slack-capable turn
(interactive chat, the next Triage pass) drains `approved` entries **first thing**, running the same
freshness re-check before posting. Closing the gap properly — a `slack-mcp.json` auto-detected by the
daemon — is open question 9.

## 6 · The gate & persona

- **Drafting is act-low** (already enumerated in `autonomy-policy.md`: "Prepare a draft … and hold it").
  Reading Slack, classifying, consulting the SSOT, Retrieval reads — all act-low.
- **Sending/posting to Slack is ask-high, permanently.** It is outward-facing and therefore **outside
  the self-graduation path** by policy — no Dream proposal, no `approved_as_is` streak, can ever
  auto-send to Slack. Only the owner's explicit per-item `send <id>` fires a post. (Observability still
  records the corrections — that evidence tunes the *drafting*, never the gate.)
- **Persona: the assistant, never the owner.** Every draft is written in the assistant's voice and
  identifies it — *"<assistant> here (<owner>'s assistant)"* or the SSOT's signature line — even though
  the connected Slack account is the owner's own, which is exactly why the identification matters:
  without it, a send *would* read as the owner typing. Critique's identity check enforces this on every
  draft. Whether work-Slack optics want a different convention (the owner approves verbatim — does that
  make it their message?) is open question 4; **the default per the standing persona rule is: the
  assistant always signs.**
- **Privacy fence** (§2's "Never state" list) is enforced twice: at drafting (the derivation contract)
  and at Critique (privacy check). A message whose honest answer sits behind the fence gets the
  escalation phrase, not a partial truth.

## Docs updated in the same change (when built)

Stale docs are a bug. The build PR touches, minimum: `subagents/slack-triage/SKILL.md` (the drafting
sub-step, triggers, volume guard), `seneschal/references/comms-mapping.md` (Slack section → SSOT + hold
loop pointers), `seneschal/references/memory.md` + `seneschal/state/README.md` (additive schema fields +
`approved` status), `seneschal/references/autonomy-policy.md` (only if wording needs the SSOT pointer),
`seneschal/SKILL.md` (Triage mode one-liner), and the new `seneschal/references/slack-ssot.md` itself
(seeded from the skeleton, filled by the owner).

## Phasing

1. **Phase 0 — this spec.** The owner signs off / answers the open questions.
2. **Phase 1 — Markdown-native (the whole feature).** Seed `slack-ssot.md`; wire the drafting sub-step,
   derivation contract, schema additions, and approval-grammar edits into the docs above. Zero new
   runtime code — enforcement is the orchestrator following the spec, exactly like the advisor chain
   (Option A). One PR, merge on green.
3. **Phase 2 — optional code rail (deferred).** If/when the advisor chain's Option B rails land
   (`seneschal/scripts/advisors.py`), the pre-send Gate assertion covers `slack_send_message` like every
   other outbound path, and a small stdlib helper could own the `pending-approvals.json`
   read/append/flip (with `test_*.py` coverage). Not needed for correctness; earns itself only with the
   broader rails work.

## Open questions for the owner

> **The owner's rulings (via Telegram):**
> - **Q2 — Scope: DECIDED.** DMs + direct @-mentions only.
> - **Q3 — Auto-draft: DECIDED.** Auto-draft by default.
> - **Q4 — Signature: DECIDED.** Draft **both** variants (assistant-signed + unsigned/as-the-owner) and
>   the owner picks per send — it depends on the person. Context: some coworkers shouldn't learn a
>   personal AI assistant is hooked to the owner's work Slack. So every held draft carries an A (signed)
>   and B (unsigned) body; the approval grammar needs `send a7a` / `send a7b` or equivalent.
>   *(Later revised — see the revision block at the top: B deferred, drafts are A-only.)*
> - **Q9 — Daemon Slack hands: DECIDED.** Wire up Slack MCP for the daemon (`slack-mcp.json`
>   auto-detect) so a Telegram `send` posts immediately.
> - **Q1 — SSOT location: DECIDED.** Git — the tracked repo file `seneschal/references/slack-ssot.md`
>   (the spec's recommendation).
> - **Q5–Q8, Q10, Q11 — ALL DECIDED: go with the spec's recommendation on each.**
>   - **Q5** — hold only in `pending-approvals.json`, no Slack-side draft copy (Slack-drafts path kept
>     for an explicit "leave it in my drafts" ask).
>   - **Q6** — pending drafts expire: surface in the Brief at 24 h, auto-`rejected` with a note at 72 h.
>   - **Q7** — `edit <id>` re-holds under the same id, counts as `edited_before_approve`.
>   - **Q8** — SSOT staleness threshold: 30 days.
>   - **Q10** — volume guard: ≤ 3 auto-drafts per triage pass.
>   - **Q11** — scheduled sends out of scope for v1.
>
> **All 11 questions are ruled on — the spec is fully signed off.** Q4's both-variants ruling meant
> §Hold format needed an A/B body per draft and a variant selector (`send a7a` / `send a7b`) — later
> revised to A-only (see top); Q9's ruling adds daemon `slack-mcp.json` auto-detect to v1 scope.

1. **SSOT location.** Tracked repo file `seneschal/references/slack-ssot.md` (recommended: diff trail,
   zero runtime reads) — or a store page (phone-editable), or the hybrid (store edit-surface,
   Dream-mirrored to the repo file)?
2. **Scope of inbound.** DMs + direct @-mentions only (proposed)? Any channels to watch even unmentioned
   — or explicitly exclude (e.g. shared/external channels, anything with clients)?
3. **Auto-draft vs draft-on-request.** Default auto-draft for derivable DM/mention asks (proposed), or
   start draft-on-request-only and graduate auto-draft later on the evidence?
4. **Signature convention.** The assistant always signs ("<assistant> here, <owner>'s assistant" — the
   standing persona rule, proposed default) — or, since every send is verbatim-approved by the owner
   from their own account, should approved sends read as *the owner's* words (no signature)? This is the
   one place the persona rule and work-Slack optics can pull apart; the owner's call.
5. **Slack-side draft store.** Confirm §3's choice: hold only in `pending-approvals.json`, no
   `slack_send_message_draft` copy (avoids orphaned drafts on reject) — with the Slack-drafts path kept
   for an explicit "leave it in my drafts" request?
6. **Pending-draft expiry.** Should an unanswered `pending` Slack draft auto-expire (proposed: surface
   in the next Brief at 24 h, auto-`rejected` with a note at 72 h — Slack conversations go stale fast),
   or hold forever like email drafts?
7. **Edit grammar.** Confirm `edit <id>: <text>` / "change a7 to say …" re-holds under the same id (and
   counts as `edited_before_approve`).
8. **SSOT staleness threshold.** 30 days before the Brief flags `Last reviewed:` (proposed) — or
   tighter/looser?
9. **Slack hands for the daemon.** Live with the `approved`-then-drain gap (§5) for v1, or wire a
   `slack-mcp.json` auto-detect into the daemon so a Telegram `send a7` can post immediately?
10. **Volume guard.** ≤ 3 auto-drafts per triage pass (proposed) — right number?
11. **Scheduled sends.** Is `slack_schedule_message` in scope ("send this at 9am") — same hold loop,
    send-time freshness re-check at the *scheduled* moment is impossible, so propose: out of scope v1?
