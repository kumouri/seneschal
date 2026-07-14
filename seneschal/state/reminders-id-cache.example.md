# ⏰ Reminders — row-ID cache (live table) — EXAMPLE SEED

**Gitignored runtime cache.** This `*.example.md` is the tracked *seed* showing the shape of the live
`reminders-id-cache.md` (which is gitignored and machine-specific). Copy it to `reminders-id-cache.md` to
bootstrap a fresh checkout, or just let the first ack / nightly Dream populate the real file. The
**protocol/doc** (why it exists, how to ack by id, staleness/fallback) is
the ack-by-cached-id notes in `../references/databases.md`; the ⏰ Reminders DB
(`collection://00000000-0000-0000-0000-000000000007`) is the system of record.

## Rows (page id ← Reminder)

| Reminder | Page ID | Type | Window | Cadence | Importance |
|----------|---------|------|--------|---------|------------|
| Morning meds — required | `00000000-0000-0000-0000-000000000000` | Recurring Habit | Morning | Daily | 🚨 Critical |
| Eat lunch | `00000000-0000-0000-0000-000000000001` | Recurring Habit | Midday | Daily | ⭐ High |
| Submit timesheet | `00000000-0000-0000-0000-000000000002` | Recurring Habit | Evening | Weekly | 🚨 Critical |

> **Note.** Real ids are the ⏰ Reminders DB page ids. The nightly Dream re-pulls the DB and reconciles
> the live file when it notices drift; add a row when a reminder is created, remove it when one is retired.
