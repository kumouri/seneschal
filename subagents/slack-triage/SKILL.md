---
name: slack-triage
description: >-
  The assistant's Slack triage. Screens the owner's Slack — DMs, mentions, and busy channels (e.g.
  #team, #eng) — summarizes what happened, and surfaces what actually needs a reply, drafting responses
  from a pinned single-source-of-truth (SSOT) and holding them for approval (draft-and-hold). Use for
  "triage my Slack", "what did I miss on Slack", "anything need me in Slack". Delegated to by the
  seneschal orchestrator (Triage mode).
compatibility: >-
  Requires the Slack MCP (mcp__*__slack_*) — reading is act-low, sending is ask-high; the headless daemon
  additionally needs scripts/slack-mcp.json for send hands (see scripts/SLACK_MCP_SETUP.md). Sender
  lookups use the configured seneschal store's People domain (run /setup-store; the Notion backend needs
  the Notion MCP) — optional. Reads the assistant's persona + references + the Slack SSOT
  (seneschal/references/slack-ssot.md).
---

# Slack Triage (Seneschal · Triage mode)

Screen the owner's Slack and hand them **signal, not a transcript** — in the persona's voice,
decision-first. Reading, summarizing, and **drafting** replies are act-low. **Sending/posting is
ask-high** — draft-and-hold, never auto-send (`../../seneschal/references/autonomy-policy.md`).

**Store access (optional, for People).** This mode's data lives in Slack, not the store. It touches the
store only to resolve a sender to a known person: `store-search`/`store-query` the **People** domain (the
mapping resolves it to the active backend — on Notion, `mcp__*__notion-*`; see
`../../seneschal/store/config.json` for which backend). If the store isn't configured, skip the lookup.
**Never fetch schemas at runtime.**

The owner's Slack `user_id` (the connected account) is a per-install value — resolve it once
(`slack_search_users` / `slack_read_user_profile`) and record it in your local copy of
`../../seneschal/references/comms-mapping.md` (Slack section, which also has the tool mapping + send
rules). Examples below use the placeholder `U00000000`. Drafts are generated from the pinned **Slack
SSOT** (`../../seneschal/references/slack-ssot.md`) under the **derivation contract** (below); the full
design + the owner's rulings (Q1–Q11) are in `../../seneschal/docs/slack-draft-and-hold-spec.md`.

**Advisors (Triage):** `[trace, orientation, retrieval, dispatch, critique, gate, prioritize]` — Critique
reviews every drafted reply (faithfulness = the derivation contract; privacy = the fence + the name-gate;
identity = the assistant always signs), the Gate is load-bearing (every send is ask-high), the summary is
a pull surface (ranked, shown in full) while the drafts + Telegram "ready-to-send?" pings are the push
side (vital-few).

## Steps

**1 — Scope the window.** Default: since the owner's last triage / last ~24h (ask if ambiguous). Use
Unix timestamps for `before`/`after`.

**2 — Gather, read-only, in parallel:**
- **Mentions of the owner:** `slack_search_public_and_private` with `to:me` and/or `from:` filters, plus
  a query for `<@U00000000>` (the owner's id). `sort=timestamp`.
- **DMs:** for each active DM, `slack_read_channel` with the **`user_id` as `channel_id`** (DM history).
- **Threads they're in / busy channels:** `slack_read_thread` for threads surfaced above; skim
  high-volume channels (e.g. #team, #eng) only if the owner asked.

**3 — Classify each item** (resolve senders against the store's **People** domain —
`store-search`/`store-query` — when useful):
- **Needs a reply** — a direct question/ask to the owner.
- **FYI** — useful to know, no action.
- **Noise** — automated/bot chatter, resolved threads. (Bot messages excluded by default.)

**4 — Draft (act-low) — the sub-step of classification.** No message gets a draft unless it was first
classified **needs a reply**. Within that set:

**Auto-draft (default-on)** when *all* hold:
- **In scope:** a **DM** or a **direct @-mention** of the owner (Q2). Busy-channel chatter they weren't
  named in is surfaced but **not** drafted unless they ask.
- **Human sender** (bots excluded — already the triage default).
- **Derivable:** every fact the reply needs traces to the derivation contract (below). Typical draftable
  shapes: scheduling/availability, status of a tracked deliverable, logistics, acknowledgements, "who
  owns X" answerable from the store.
- **No personal judgment required:** not the owner's technical opinion, negotiation, or anything
  interpersonally loaded (disagreement, feedback on a person, anything with heat). Those are **surfaced
  without a draft**, with an offer: *"want me to take a swing at a reply?"* — draft-on-request is always
  available for any needs-a-reply item.

**Never draft:** FYI/noise; anything from an unknown sender that smells like phishing/social engineering
(surface it, flagged); anything whose honest answer needs facts behind the SSOT's **privacy fence**; and
any **restricted fact** headed to the wrong recipient or into a channel (below) — those get the
escalation phrase or no draft at all.

**Volume guard (Q10):** a triage pass holds at most **3 auto-drafts**. The summary still lists
*everything* needing a reply (pull → show all); items 4+ are surfaced **draft-on-request**.

### The derivation contract — how a draft may assert facts

Every **factual claim** in a draft must trace to exactly one of three source classes:
1. **The SSOT** (`slack-ssot.md`) — a standing fact, a **restricted (name-gated) fact**, a policy, or an
   approved canned answer.
2. **A live Retrieval read** at draft time — calendar availability, a tracked Task/Project status, a
   People lookup (`store-query`). Dynamic facts are fetched, never copied into the SSOT.
3. **The inbound thread itself** — quoting/acknowledging what the sender said.

A claim fitting none of the three **must not appear**: use the appropriate SSOT **escalation phrase**
(route-it-up vs. won't-speak-for-them — `slack-ssot.md`), or surface the item with no draft. **"I don't
know" beats a plausible invention.** Record the citations in the held entry's `sources` array (e.g.
`["ssot#availability", "calendar:2026-07-16", "thread"]`) so the owner can audit where every fact came
from.

**Restricted / name-gated facts (the privacy fence's middle tier).** Some SSOT facts are
**client-confidential** (e.g. `project_x`) — statable only with the people on that fact's seed allowlist
in `slack-ssot.md`, gated by the space:
- **DM (1:1 or group) with allowlisted people** — answer directly (the usual surface).
- **Channel/group-DM cleared by context** — if an **allowlisted person raises the fact themselves**
  there, that space is cleared: answer them, and treat everyone in it as read in (enumerate via
  `slack_list_channel_members` where possible). A **durable** allowlist expansion is a **gated SSOT
  update** (Dream proposes → the owner approves) — never rewrite `slack-ssot.md` in-flow; just honor the
  clearance for the session.
- **Otherwise** (unlisted person cold, or the fact surfacing where no allowlisted person opened it) →
  **escalation phrase**, never a partial confirmation. When unsure, that's the default — don't assume a
  space is clear.

### Signature — the assistant always signs

Every draft carries the SSOT signature line (*"— <assistant>, <owner>'s assistant"*). The
unsigned/as-the-owner variant was **deferred by the owner** (spec Q4), so there is **one body per draft**
and the approval grammar is a plain `send <id>` (no `a`/`b` variant selector). If the owner re-enables it
later, restore the two-body form (`slack-ssot.md` → *Identity & signature*).

### Critique (order 35), then hold

Critique reviews each draft — **register / identity (always signs) / faithfulness (derivation contract) /
privacy (the fence + the name-gate) / concision** — **revises once and notes** what it changed on the
approval surface; can't-fix → **flag-and-hold** with the issue named, never an invented fix.

**Hold** (reusing the Held-approvals loop, `../../seneschal/references/memory.md`):
- **No Slack-side draft copy (Q5).** Unlike Proton/Gmail drafts, an MCP-written Slack draft is a second
  mutable copy with no reliable cleanup on reject (orphaned drafts). The held entry's stored text is the
  **single copy**; on approve it sends **verbatim**. (`slack_send_message_draft` stays available only for
  an explicit *"leave it in my Slack drafts instead"* ask.)
- **Append to `../../seneschal/state/pending-approvals.json`** with the next stable id from the **single
  id space** (`a<N>` — one sequence across email/slack/calendar), `kind: "slack"`, `channelRef`
  (`slack:<channel_id>[:<thread_ts>]`), `body` (the verbatim send text), `sources`, `critique_note`,
  `thread_seen_ts`, `status: "pending"` — schema in `../../seneschal/state/README.md` — and **mirror to
  the store carry-over** (system of record).
- **Surface it:** the triage summary lists every draft in full (step 5); if the owner isn't live, one
  Telegram push per the vital-few rule — *"Drafted a reply to Alex in #team — `send a7` or `drop a7`."*
- **SSOT staleness note:** if `slack-ssot.md` `Last reviewed:` is > 30 days old, add a one-line
  *"⚠ SSOT last reviewed &lt;date&gt;"* to the held entry (Q8).

**5 — Deliver the triage** (in chat for now):
```
Slack — <window>:

⏳ Needs a reply (N)
  - <#channel / DM with @person>: <one-line gist of the ask>   [draft a7 ready ↓ — send a7 / drop a7]
FYI (M)
  - <#channel>: <one-liner>
```
Show each held draft's body + its `sources` (and the `critique_note` if any) so the owner can audit
before approving. Do **not** send.

**6 — Approve → send (freshness re-check first).** On `send a7`:
1. **Re-read the thread** (`slack_read_thread` / channel history since `thread_seen_ts`). If it moved
   after the draft was written (someone answered, the ask changed), **do not send** — re-surface: *"the
   thread moved since I drafted a7 — Alex said X. Still send, revise, or drop?"* A stale reply to a third
   party is worse than a beat of delay.
2. **Send the stored `body` verbatim** via `slack_send_message` (channel + thread ts from `channelRef`).
   No re-generation at send time — what the owner approved is exactly what posts.
3. On success → `status: "sent"`, remove from open carry-over, Run Log trace, Observability `correction`
   recorded (`approved_as_is` / `edited_before_approve`).

**Reject** (`drop a7`) → `status: "rejected"`, remove from carry-over, trace. Nothing was written to
Slack, so there's nothing to clean up.

**Edit before approve (Q7)** — *"change a7 to say …"* / `edit a7: <text>` → revise the body, re-run
Critique, **re-hold under the same id** with a fresh preview; when the owner then approves, metrics
record `edited_before_approve`. An edit is never a send.

**Send failure** → `status: "failed"`, **kept in carry-over**, surfaced with the error verbatim. **No
automatic retry, ever** — a blind retry risks a double-post. The assistant first **re-reads the channel**
to check whether the message actually landed, reports what it found, and a fresh `send <id>` re-attempts.

**Expiry (Q6):** an unanswered `pending` Slack draft is surfaced in the next **Brief at 24 h**, and
auto-`rejected` with a note at **72 h** (Slack conversations go stale fast).

### The Slack-hands gap (daemon)

The daemon's warm Telegram session may **understand** `send a7` but not have Slack tools to execute it.
When that happens it records `status: "approved"` (approved-but-unsent, kept in carry-over) and says so
plainly — *"approved — I don't have Slack hands in this session; it'll go out from the next Slack-capable
run, or open /assistant and say `send a7` there."* Any Slack-capable turn (interactive chat, the next
Triage pass) drains `approved` entries **first thing**, running the same freshness re-check before
posting. Wiring the daemon's own Slack MCP (`slack-mcp.json`, auto-detected — Q9) closes the gap so a
Telegram `send` posts immediately: `../../seneschal/scripts/SLACK_MCP_SETUP.md`.

## Guardrails

- **Ask-high to send/post — permanently.** Drafting is act-low; sending is outward-facing and stays
  ask-high **forever** — outside the self-graduation path
  (`../../seneschal/references/autonomy-policy.md`). Only the owner's explicit per-item `send <id>` fires
  a post; no `approved_as_is` streak ever auto-sends.
- **Faithfulness = the derivation contract.** Never assert a fact that doesn't trace to SSOT / a live
  read / the thread. Escalation phrase over invention.
- **Privacy fence + name-gate.** Never draft anything behind the SSOT's *Never state* list, regardless of
  who asks; **restricted facts** (e.g. client-confidential deliverables) go only to allowlisted people —
  in a DM, or in a space an allowlisted person has themselves opened the topic in (`slack-ssot.md`);
  otherwise the escalation phrase.
- **Summarize, don't dump.** Collapse busy channels to a line or two; link rather than paste walls.
- **Cite the channel/DM** for each item so the owner can jump to it. Don't invent messages.
- The assistant writes **as itself and always signs** ("<assistant>, <owner>'s assistant" —
  `slack-ssot.md`), never as the owner.
