# Clear the Journal + Build Tomorrow's Carry-Over (Critical Path #2)

After the day is captured (the Agent Run Log row exists with the verbatim entries in its page body —
Phase 1), reset the Interstitial Journal (`00000000-0000-0000-0000-000000000010`) for the next journal
day and build the carry-over callout at the top. **Never clear a day whose capture doesn't exist yet.**

## Journal-day boundary

The owner may journal after midnight, so **treat late-night-after-midnight entries as part of the prior
day.** Group entries under a top-level **date toggle** whose title is the journal day they belong to. If
multiple date toggles exist, **process only the most recent/active one** during the run (unless asked
otherwise).

> **Preserve the date-toggle shape.** Other skills read "has the owner journaled today" as *a top-level
> date toggle for today with ≥ 1 entry* (see `databases.md` §1) — the reset below must leave the page in
> exactly that structure.

## Clearing steps

1. The processed day's entries are now preserved verbatim in the run-log capture — they can be removed
   from the Interstitial Journal.
2. Keep only **carry-over notes** relevant to the next journal day.
3. Add a fresh **top-level date toggle** for the next journal day; new entries (including after-midnight
   ones) go under it.

## The carry-over callout

A single callout at the very top of the Interstitial Journal: `<callout icon="📌">`. Inside, group items
into collapsible `<details>` toggles by category so it isn't overwhelming.

- Each toggle's summary line: **category emoji + bolded name + item count**, e.g. `💼 **Work** (2)`.
- Standard groups (use whichever apply; **skip empty ones**):
  - `💼 Work`
  - `💜 Relationships`
  - `🏠 Home & Errands`
  - `🩺 Health & Self-Care`
  - `💵 Finances`
  - `🔧 Hobbies & Tech Projects`
  - `🎮 Gaming`
- Items that don't fit go under a catch-all or an ad-hoc group.
- Standalone one-off notes (closing notes, single reminders) can sit **outside** the toggles at the
  bottom of the callout.
- A `🎯 Goals` group is added when a goal review is due / deadline within 7 days / status changed / new
  goal today (see `goals.md`).

### Shape (matches the live journal)

```markdown
<callout icon="📌">
**Carry-over — <Weekday Month D, YYYY>:**
<details>
<summary>🏠 **Home & Errands** (1)</summary>
	- 💡 water the plants before the weekend
</details>
<details>
<summary>💼 **Work** (2)</summary>
	- ⚠️ item…
	- 💡 item…
</details>
</callout>
```

## Important Flags in the carry-over (REQUIRED)

Every **Active** or **Carrying Over** flag MUST appear in the next day's callout until it's `Resolved` or
`Archived`. (Full lifecycle in `important-flags.md`.) Visual treatment:

- Group flag lines by **Type**; lead each line with the **type emoji + importance level**, then the
  description, then a `[flag](url)` link.
- Order within the callout: **Critical → High → Notable → Reference.**
- If several flags share a Type, sub-group them under a Type header.
- If a flag has been carrying **3+ days** unresolved, add a gentle nudge, e.g. `— carrying since Apr 27`.

Examples:
```markdown
- 🚨 💼 CRITICAL Work — short flag description — [flag](flag-url)
- ⭐ 👨‍👩‍👧 High Family — short flag description — [flag](flag-url)
- ✨ 🧠 Notable Internal/Feelings — short flag description — [flag](flag-url)
- 📌 🏠 Reference Home/Life — short flag description — [flag](flag-url)
```

These flag lines can live in a `⭐ Flags` group or be folded into the matching category toggle — but they
must be present and ordered by importance.
