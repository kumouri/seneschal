---
name: store-setup
description: >-
  The store onboarding wizard. Picks the owner's data backend (Notion / Obsidian vault / plain
  Markdown folder), provisions it, reads any existing content to learn about the owner, and
  hands the owner interview its candidate-facts sheet to build persona/owner-profile.md. Use
  for "/setup-store", "set up my data store", "connect Notion / an Obsidian vault", "switch
  backends", or as the store chapter of the unified /setup flow. Re-runnable: detects existing
  config and offers reconfigure / switch.
compatibility: >-
  Writes seneschal/store/config.json + (for Notion) store/notion/schema.md + store/notion/mcp.json
  (all gitignored); persona/owner-profile.md lands via the owner-interview hand-off (Phase 5).
  Notion provisioning needs the Notion MCP; the filesystem backends need only disk access.
---

# Store onboarding (`/setup-store`)

Give the assistant a system of record. This wizard chooses a backend, stands it up, learns what
it can from what's already there, and — with the owner's consent, fact by fact — assembles the
candidate-facts sheet the **owner interview** turns into a profile. Callable standalone or as
the "where does your data live" chapter of `/setup` (where it runs after the persona wizard).
The interview itself lives in the unified wizard
(`subagents/setup/chapters/owner-interview.md`, which **owns `owner.*` in identity.json and
owner-profile.md** and confirms rather than re-asks anything the persona wizard already set);
Phase 5 below hands off to it.

Read `seneschal/store/README.md` first — it defines the three backends and the indirection every
skill uses.

## Phase 0 — Detect

If `seneschal/store/config.json` exists, don't overwrite silently. Show the active backend and
offer: **reconfigure** (re-run provisioning for the same backend) / **switch backend** (warn
plainly: v1 does **not** migrate data — the old store stays where it is, untouched) / **just the
interview** (skip to Phase 3) / **abort**. Only proceed on an explicit choice.

## Phase 1 — Pick a backend

Present the three with honest trade-offs, then let the owner choose. Write nothing yet.

| Backend | For the owner who… | Cost |
|---|---|---|
| **Notion** | already lives in Notion, wants the mobile app + one-tap reminder acks | needs the Notion MCP connected; subject to Notion's rate limits |
| **Obsidian vault** | wants local-first, offline, plaintext they own; likes the graph | none — filesystem; put it in an existing vault or a new one |
| **Plain Markdown** | wants zero dependencies and no app at all | none — just a folder of Markdown |

## Phase 2 — Provision

**Notion:**
1. Verify the MCP is live with one cheap call (`notion-get-teams` / `notion-get-users`). If it's
   not connected, walk the owner through connecting it, then retry.
2. **Discover-or-create** the core databases (Tasks, Projects, Important Flags, People, Goals,
   Reminders, Run Log — plus the journal set if they want the journal). For each: `notion-search`
   by name; on a hit, do a **setup-time schema fetch** (explicitly allowed — the
   no-runtime-schema-fetch rule governs routine runs, not setup) and reconcile its properties/
   options against `store/notion/schema.template.md`, listing any gaps for the owner to approve
   filling; on a miss, `notion-create-database` from the template so the option strings match the
   registry exactly. Existing DBs whose option strings differ (no emoji, different statuses):
   **adapt-and-record** — write their real strings into the translation table of the filled
   schema.md rather than forcing a migration.
3. Render `store/notion/schema.template.md` → the gitignored `store/notion/schema.md` with the
   real ids and any adapted option strings. Copy `store/notion/mcp.example.json` →
   `store/notion/mcp.json`. This is what the daemon forwards to headless sessions.

**Obsidian:** ask for the vault path (offer to detect: `Glob` for `**/.obsidian` under the home
/ Documents), confirm the subfolder name (default `Seneschal`), create the skeleton folders +
`Carry-Over.md` + a small `README.md` inside the subfolder pointing back at seneschal.

**Plain Markdown:** ask for / create `root_path`, create the skeleton folders.

Then write `store/config.json` (`active` + the chosen backend's block).

## Phase 3 — Read existing info (opt-in — this reads personal data; get explicit consent)

Only with a clear yes. The goal is a **candidate-facts sheet**, not silent writes.
- **Notion:** query pre-existing **dated/active** Tasks, **In-Progress** Projects, People, and
  Goal categories; `notion-search` a handful of top pages.
- **Obsidian / Markdown:** `Glob` the owner's *existing* notes (their whole vault, not just the
  seneschal subfolder), sample ≤30 by recent-modified, skim titles + frontmatter.

Summarize into candidate facts — name/timezone hints, active projects, recurring people,
habits/recurring themes, stale-backlog warnings — **each with a source citation**. Write nothing.

## Phase 4 — Read the owner's Claude self-description (opt-in)

If present, `Read` `~/.claude/CLAUDE.md` (the global Claude Code memory — it's auto-loaded anyway;
the explicit read is for provenance) and the project `CLAUDE.md`. Pull only owner-relevant lines
(name, role, preferences, pronouns) into the candidate sheet, tagged `source: your global
CLAUDE.md`. Never lift anything you wouldn't show the owner.

## Phase 5 — Owner interview (hand-off)

Run the owner-interview chapter — `subagents/setup/chapters/owner-interview.md` — with the
Phase 3–4 candidate-facts sheet. It owns the interview, `persona/owner-profile.md`, and the
`owner.*` identity fields (prefills confirmed / edited / rejected — never a silent write), and
marks its own ledger chapter. Standalone `/setup-store` runs it inline here; the unified
`/setup` runs it as its own chapter right after this one.

## Phase 6 — Seed + hand off

Offer to seed starter Reminders from the habits confirmed in the owner interview (one
`store-create` per habit, act-low). Copy any relevant `state/*.example.*` seeds. Then close the
setup ledger for this chapter:

```
python seneschal/scripts/setup_state.py mark store done --artifacts seneschal/store/config.json --hash-artifacts --summary "<backend> backend provisioned"
```

Finally print a plain summary: what was written (config.json, the filled registry / vault
skeleton, owner-profile.md, identity.json owner fields), and next steps — start the daemon, run
`/brief`, and that re-running `/setup-store` changes any of it.

## Guardrails

- Act-low throughout **except** creating Notion databases and seeding reminders, which you do only
  after the owner approves the specific set (show the list first).
- Phase 3's ingestion and Phase 4's CLAUDE.md read are **each** gated on explicit consent.
- Every owner fact is confirmed, never inferred-and-written. No fact leaves the machine.
- Switching backends never deletes the old store; say so before switching.
- Coordinate with the persona wizard (`assistant.*` + persona.md) and the owner interview
  (`owner.*` + owner-profile.md — the chapter Phase 5 hands off to). Confirm shared fields,
  don't re-ask.
