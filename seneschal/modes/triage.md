# Triage mode — screen comms

This file is read **only when the router in `../SKILL.md` dispatches to this mode** — `SKILL.md`
renders into every invocation whole, so each mode's rules live in their own leaf and cost nothing on
the runs that don't need them (`docs/grounding-restructure-spec.md`).

**Read this whole file before acting, then work its steps in order.**

---

**Advisors:** full chain **+ Critique + Prioritize (pull → rank, show all)** — `[trace, orientation,
oikonomos, retrieval, dispatch, critique, gate, prioritize]`; Critique reviews every drafted reply, the
Gate is load-bearing (every send is ask-high), and the surfaced summary is ranked but shown in full.

A sweep across channels, surfacing only what needs the owner, each item drafted-and-held (ask-high to
send):
- **Slack** → delegate to `../../subagents/slack-triage/SKILL.md` (DMs, mentions, busy channels).
  Replies are **drafted from the pinned Slack SSOT** (`references/slack-ssot.md`) under the derivation
  contract and **held** on the standard approval loop (`send a7` / `drop a7`), freshness-re-checked
  before a verbatim send; the assistant always signs. Design: `docs/slack-draft-and-hold-spec.md`.
- **Email** (Proton + Gmail) → `../../subagents/email-triage/SKILL.md`. Triage **both** inboxes when
  both are configured, replying from the assistant's own address (`assistant.email` in
  `../../persona/identity.json`; `references/comms-mapping.md`).
- **Calendar invites** → delegate to `../../subagents/calendar-steward/SKILL.md` (unanswered invites,
  conflicts, focus-time; accept/decline/propose-time is ask-high).

When asked to "triage everything," run all three channels (Slack, email, calendar) and combine.

**Every approved send meets the send gate.** An outbound send to anyone but the owner is refused in
code (`scripts/send_gate.py`, exit 3) unless an approval row stands behind it — record the approval
first (`scripts/pending_approvals.py resolve a<N> --status approved`), then send. A refusal is never
worked around with another script (`references/autonomy-policy.md`).
