# Session prompt — Notion write-behind outbox (durability)

> Paste this whole file as the opening prompt of a fresh Claude Code session in the **seneschal** repo.
> It's a *planning* prompt: produce a spec for the owner's sign-off **before** writing any code.

---

You are working in the **seneschal** repo (the owner's chief-of-staff assistant). Your job this session is to
**design, then (on my approval) build** a **Notion write-behind outbox** — framed as **durability, not
rate-limit-dodging**. Lead with durability; rate-limit relief is a side benefit, not the pitch.

## Read first
- `CLAUDE.md` — repo overview, act-low/ask-high, conventions, CI.
- `seneschal/references/notion-rate-limits.md` — the read/write limits and the throttle we already hit.
- `seneschal/references/memory.md` — the run-log / carry-over / acks durability patterns this generalizes.
- `seneschal/docs/asyncio-daemon-design.md` — the reactive core + supervised tasks (where a drainer task would
  live), and the invariants a new task must respect.
- `seneschal/state/README.md` — existing state-file schemas (`reminders.json`, `acks.json`, etc.).
- `seneschal/state/carry-over.md` — note the **"Chat→Notion ack write-through gap"** ops-watch entry (chat acks
  that reached only local `acks.json`, never Notion). That's the real-world failure this fixes.

## The problem
The assistant's tracker writes to Notion — reminder **acks** (⏰ row `Status` / `Last Acknowledged` /
`Consecutive Misses`), **med-intake** rows, **run-log** entries — can be **lost** when Notion is
unreachable, returns 429, or the daemon restarts mid-write. Design a durable local outbox: a write is
**journaled to a durable local store first** (survives a reboot), **then flushed to Notion**; idempotent,
ordered, retried until it lands.

## Scope the spec to cover
1. **Outbox schema + location** (in `state/…`): what each entry holds — target page/db, operation, payload,
   **idempotency key**, `created_at`, `attempts`, `status`.
2. **Enqueue points** — which writes route through the outbox (acks, med logs, run-log, reminder status) and
   which stay direct, and how the ack path (`reminders_dequeue.py` / chat write-through) plugs in.
3. **Idempotency** (a replay after restart must not double-write), **ordering** guarantees, **retry/backoff**,
   and **conflict/failure** handling.
4. **Relationship to the existing `acks.json` / `reminders.json` ledgers** — this generalizes them; say how,
   and whether they fold into the outbox or stay separate.
5. **Observability** — how I'd see a stuck or failed entry.
6. **THE FORK — leave this as my call, with honest tradeoffs for each:**
   - **(a) opportunistic-flush** — drain the outbox inline on the next daemon activity / next warm-session
     turn (no new task).
   - **(b) dedicated REST-drainer task** — a supervised asyncio background task that drains on a cadence,
     independent of chat activity.
7. **Open questions** — everything you need me to decide.

## How to work
- **Spec first.** Write `seneschal/docs/notion-write-behind-outbox-spec.md` (Markdown is canonical), tone like
  `seneschal/docs/asyncio-daemon-design.md`. **Name the (a)/(b) fork as the open decision. Stop for my sign-off
  before implementation.**
- Conventional Commits (`docs(notion):`, `feat(notion):`). Branch off `develop` (Git Flow; `develop` integrates, `main` releases — and the daemon deploys from `main`).
- CI green before any merge; **never merge red/pending**; **merge commit** only.
- `git -c core.fsmonitor=false …` on every git command.
- Stdlib-first (sqlite3 + urllib are already the house style); don't add deps without asking.

Start by reading the files above, then draft the spec and walk me through the fork + open questions.
