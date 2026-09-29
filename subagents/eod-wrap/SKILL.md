---
name: eod-wrap
description: >-
  The assistant's end-of-day wrap. A short evening recap for the owner — what got done today, what
  slipped, what's waiting on them, and a preview of tomorrow — from their calendar and Notion.
  Write-enabled under the act-low / ask-high gate. Use for "end of day", "wrap up", "what did I do
  today", or the scheduled evening run. Delegated to by the seneschal orchestrator (Wrap mode).
compatibility: >-
  Requires a configured seneschal store (run /setup-store); the Notion backend additionally requires the
  Notion MCP (mcp__*__notion-*). Calendar comes from a Calendar MCP if one is connected, otherwise from
  the gcal_api.py REST bridge (see ../../seneschal/references/calendar-mapping.md). Reads the
  assistant's persona + references.
---

# End-of-Day Wrap (Seneschal · Wrap mode)

Close out the day in the persona's voice: brief, honest, and forward-looking. **Write-enabled, governed
by the act-low / ask-high gate** (`../../seneschal/references/autonomy-policy.md`): the Wrap makes
**act-low** writes directly (reconcile its own Reminders tracker, apply acks it surfaces, seed carry-over,
write the Run Log) and **drafts-and-holds ask-high** ones (flip a linked Task/Goal to done, merge/archive
Task rows, any send) unless the owner has approved them. The **scheduled** run is the daemon's
`eod-wrap` slot, **21:07 owner-local** (`SLOTS_TEMPLATE` in `../../seneschal/scripts/presence.py`; the
standalone `seneschal-eod-wrap` scheduled task is retired); it also appends the wrap to the
"🗒️ Daily Brief" page and writes a Run Log row (Mode=Wrap).

**That slot's prompt orders two Reminders jobs as well** (`../reminders/SKILL.md`): the **journal-presence
gate**, nudge-or-satisfy, and a **linked-task refresh for active Deadline Watches** so a `nag_until_done`
row stops re-firing once its linked record is done.

## Read first

**Store access.** Read `../../seneschal/store/config.json` for the active backend, then that backend's
`../../seneschal/store/<backend>/schema.md` + `mapping.md`. Speak the six **store verbs** (`store-query`
/ `store-get` / `store-create` / `store-update` / `store-append` / `store-search`) plus domain nouns and
canonical, **emoji-free** option values; the mapping resolves them to the backend's tools. **Never fetch
schemas at runtime.**

- `../../persona/persona.md` (else `../../persona/persona.default.md`) — voice.
- `../../seneschal/references/briefing.md` — the EOD Wrap shape.
- `../../seneschal/references/databases.md` — the Tasks/Projects/Flags **+ Reminders** domain/ID map;
  the Notion query projection gotcha lives in `../../seneschal/store/notion/mapping.md`.
- `../../seneschal/references/reminders-policy.md` — what counts as acked/done/skipped in the Reminders
  tracker.
- `../../seneschal/references/calendar-mapping.md` — the **two calendar doors** (MCP → `gcal_api.py`
  bridge) and the confirmed include-set.

## Steps

**1 — Window.** "Today" in the owner's configured timezone (after-midnight still counts as today).

**2 — Gather, read-only, in parallel (`store-query`):**
- **Done today (Tasks):** Tasks where `status: done` and `completed` is today (fall back to the
  last-edited time for older rows with no `completed` date); plus achievements if surfaced, and wins
  from the day's journal entry if present.
- **Done today (Reminders):** rows with `last_acknowledged` = today and status in (done, finished) — the
  habits/todos the owner acked today, whether by the one-tap flag or by telling the assistant over
  Telegram/chat. (`done` = done-for-today, any type; `finished` = a retired item acked on its way out;
  **query both**, both count as done today.)
  **Never use the one-tap `ack` flag as the signal** — it's a one-shot input the reminder runs consume
  and reset (`databases.md`); all-cleared flags in the evening is normal, not "nothing finished."
  `status: skipped` with today's `last_acknowledged` = an honest "not today" — neither done nor slipped.
  A done **Today Todo** whose `related_task` isn't flipped yet → surface it under "Waiting on you" as a
  proposed flip (ask-high) and flip it once the owner okays it.
- **Slipped (Tasks):** Tasks with due ≤ today still not done/archived (the morning brief's "due today"
  that didn't close) → these carry to tomorrow.
- **Slipped (Reminders):** rows that fired today (`reminded_today: true` or `last_reminded` = today)
  still `pending`/`reminded`/`snoozed` — nudged, never answered. Fold the ones that matter
  (super-critical/critical/high **or** `nag_until_done`) into Slipped; leave the low-stakes ones to their
  miss counters. (On the Notion backend, filter the date fields via their `date:<Prop>:start` projection
  — see `../../seneschal/store/notion/mapping.md`.)
- **Suppressed silently (staleness):** run `python seneschal/scripts/reminder_suppressions.py
  count-today` — every row it counts is a reminder the 2 h staleness cutoff consumed today WITHOUT ever
  delivering it (`../../seneschal/references/reminders-policy.md` → "Night curfew + staleness cutoff";
  the ledger exists because a high-importance Today Todo can otherwise be silently killed this way day
  after day, with nothing reaching this Wrap or the owner). A nonzero count is its own Slipped line —
  *"N reminder(s) went stale unfired today"* — even if the store shows nothing pending for them, since a
  suppressed row is consumed (`suppressed_at`), not left `pending`, and the Tasks/Reminders read above
  would otherwise miss it entirely. Zero is a quiet win, not a skipped check — say nothing extra, don't
  manufacture a line.
- **Waiting on the owner:** open held drafts / unanswered calendar invites (from the day's Triage, if any).
- **Tomorrow:** tomorrow's events across the include-set; first event + count. Calendar MCP
  `list_events` if present, **otherwise the bridge** — `python seneschal/scripts/gcal_api.py events
  --account personal --env-file seneschal/scripts/google.env --calendar <id> --start
  <tomorrow>T00:00:00<±HH:MM> --end <tomorrow>T23:59:59<±HH:MM>` (the owner's UTC offset for that date
  is mandatory; a bare date is read as UTC and lands on the wrong day). **Zero events = "tomorrow's
  clear"**, not a missing calendar; say the calendar is unreachable only if both doors failed, and name
  which.
- **`tomorrow` flag reconciliation (`../../seneschal/docs/tomorrow-marker-spec.md` §7):** same shape as
  the Brief's own (`../morning-briefing/SKILL.md`) — `store-query` Tasks + Reminders for rows with the
  `tomorrow` flag set (on the Notion backend, the `Tomorrow` checkbox), `python
  ../../seneschal/scripts/tomorrow_marker.py reconcile task '<JSON rows>'` (or `reminder`), then
  `store-update` each returned row id to `tomorrow: false`. Fails open on a missing/erroring field (a
  backend or schema without the flag) — skip silently.
- **Tomorrow's Lead candidates:** while you already have today's due Tasks + near-due Deadline Watches
  in hand from the reads above, note which ones are worth offering on the "what's tomorrow's lead?"
  multi-select in step 5 — this is a promotion of what you already gathered, never a fresh query
  (`../../seneschal/docs/tomorrow-marker-spec.md` §2.3).

**3 — Deliver** per `briefing.md` → *End-of-Day Wrap*:
```
Evening recap — <Weekday, Month D>.

✔ Done today: <completed tasks / reminders acked / wins>      (or "quiet day" if nothing closed)
↪ Slipped: <due-today not done / important reminders never answered> → carried to tomorrow
🗓 Tomorrow: <N> events; first at <HH:MM> <event>
📨 Waiting on you: <held drafts / unanswered invites>
```

**4 — Seed carry-over (act-low — live now, not deferred work).** The Wrap writes tonight's snapshot
into the bounded `WRAP_SEED` region of `../../seneschal/state/carry-over.md` — **never the whole file**.
Build the current-state block (Tomorrow / Done / Slipped / Waiting on the owner / Standing holds / Do
NOT re-raise) as a fresh draft, write it to a temp file, run
`python ../../seneschal/scripts/check_carryover_resolved.py <temp-file>` against it — it flags a PR
still cited as open in the draft after the file's own resolved-items table already marks it
`~~struck-through~~ RESOLVED` elsewhere (report-only for now: a finding means reconcile the draft, drop
the stale clause, before the next step, not a blocked run) — then pipe the reconciled draft into
`python ../../seneschal/scripts/carryover_region.py write-region --name WRAP_SEED < <temp-file>`.
**Never compose "tonight's block + the entire existing file" and hand that to `memory_write.py
write`** — that shape is indistinguishable at the byte level from a prepend, and repeated nightly it
grows the file by a full copy each run with zero prunes. `carryover_region.py` replaces only the
bounded region and refuses to touch anything else. Full design:
`../../seneschal/docs/carry-over-region-spec.md`; protocol: `../../seneschal/references/memory.md`.

Before composing that block, read today's and yesterday's ad-hoc notes (read-only) — `python
../../seneschal/scripts/notes.py list --days 2` — for anything worth folding into a decision-shaped
line. **Read only; never copy them into `WRAP_SEED` wholesale** — a witness-only note has no closing
event by design (`carry-over-region-spec.md` §2.4), so pasting the raw file into a nightly-rebuilt
region would just relocate the same never-revisited accumulation the region was built to stop.

**5 — Tomorrow's Lead: two pickers, never a prose list** (`../../seneschal/docs/tomorrow-marker-spec.md`
§4.2/§2.3). Two SEPARATE act-low sends, both dispatched by `presence.py`'s callback path — this run
never writes `../../seneschal/state/tomorrow.json` directly:
- **Close/Roll/Drop, per marked item:** `python ../../seneschal/scripts/tomorrow_marker.py wrap-ask` —
  one grid row per item still marked for today; no send at all when nothing is marked. A linked
  item's Close comes back through the callback as an **ask-high proposal** to flip the linked
  Task/Reminder to done — relay it in your next turn, never flip it yourself.
- **"What's tomorrow's lead?"** — a multi-select over the candidates step 2 already gathered (today's
  due Tasks / near-due Deadline Watches), never a fresh query and never freeform text (§6.6):
  `python ../../seneschal/scripts/tomorrow_marker.py lead-ask '<JSON candidates>'`, each candidate
  `{"id": <row id or null>, "text": ..., "why": <why it's worth leading tomorrow>, "linked_kind":
  "task"|"reminder"|null}` — `why` is required (Telegram's own "every option needs a description"
  rule).

## Guardrails

- **Write within the gate** (the split is in the header). **Only claim a write that landed.**
- **Honest and short**, times in the owner's configured timezone. If little happened, say so plainly;
  don't inflate. Cite sources; don't invent.
