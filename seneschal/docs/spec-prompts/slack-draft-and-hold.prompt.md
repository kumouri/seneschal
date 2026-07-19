# Session prompt — Slack draft-and-hold reply drafting

> Paste this whole file as the opening prompt of a fresh Claude Code session in the **seneschal** repo.
> It's a *planning* prompt: produce a spec for the owner's sign-off **before** writing any code.

---

You are working in the **seneschal** repo (the owner's chief-of-staff assistant — a Claude Skill suite,
stdlib-first Python + Markdown skills). Your job this session is to **design, then (on my approval) build**
a **Slack draft-and-hold reply-drafting** capability for the assistant.

## Read first (don't skip — match existing conventions and terminology)
- `CLAUDE.md` — repo overview, the act-low / ask-high gate, conventions, CI, repo-state.
- `seneschal/references/autonomy-policy.md` — the graduation model + what's act-low vs ask-high.
- `seneschal/references/memory.md` → the section **"Held approvals — the local loop (chat / Telegram /
  Notion)"** and `seneschal/state/README.md` for the `pending-approvals.json` schema. **Reuse this loop —
  do not invent a parallel approval system.**
- `subagents/slack-triage/SKILL.md` (and `seneschal/references/comms-mapping.md`) — how Slack is read today.
- `seneschal/references/advisor-chain.md` — Dispatch / Critique / SafeGuard, so the drafting fits the pipeline.

## The goal
When an inbound Slack message warrants a reply, the assistant should **draft** the reply and **hold it** for
my approval (this is **ask-high / draft-and-hold** — the assistant never sends to Slack unprompted), then
**send on my approval**. My explicit requirement: **drafts are generated from a pinned
single-source-of-truth** — the canonical facts/context the assistant drafts from — so replies stay
consistent and never invent facts.

## Scope the spec to cover
1. **Trigger conditions** — which inbound Slack messages get a drafted reply vs. none (and how this rides on
   the existing Slack Triage pass, not a new poller).
2. **The pinned single-source-of-truth** — what it is, where it lives (a repo file? a store page? a
   `state/` doc?), how it's kept current, and exactly how a draft cites/derives from it. This is the heart
   of the ask — be concrete.
3. **The draft-and-hold flow end to end** — draft → write to the Slack draft store (if any) + append to
   `pending-approvals.json` and the store carry-over with a **stable short approval id**.
4. **Approval grammar** — `send <id>` / `drop <id>`, and how the decision is detected across chat / Telegram /
   store comment (reuse the existing detection path).
5. **Send + failure paths** — send on approve; discard on reject; on send-failure → `status: failed`, kept
   in carry-over so nothing is lost.
6. **The gate** — drafting = act-low; **sending to a third party (Slack) = ask-high**. Persona: always
   "the assistant, the owner's assistant," **never** impersonating me.
7. **Open questions** — list every decision you need from me (SSOT location, which channels/DMs are in scope,
   auto-draft vs draft-on-request, etc.).

## How to work
- **Spec first.** Write `seneschal/docs/slack-draft-and-hold-spec.md` (Markdown is canonical). Structure/tone
  like `seneschal/docs/asyncio-daemon-design.md`. **Stop and let me sign off before writing implementation code.**
- Conventional Commits with a scope (`docs(slack):`, `feat(slack):`). Branch off `develop` (Git Flow; `develop` integrates, `main` releases — and the daemon deploys from `main`).
- CI must be green before any merge; **never merge red or pending CI**. Merge via **merge commit** only.
- Use `git -c core.fsmonitor=false …` for every git command (fsmonitor hangs in this repo).
- Keep it **stdlib-first**; the sanctioned runtime deps are listed in `CLAUDE.md`. Don't add deps without
  asking.

Start by reading the files above, then draft the spec and walk me through the open questions.
