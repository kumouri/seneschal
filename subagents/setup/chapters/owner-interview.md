# Chapter: owner-interview

Who the assistant works for. A short structured interview that fills
`persona/owner-profile.md` (the context every mode uses to judge relevance) and the `owner.*`
fields of `persona/identity.json`. Lifted from the store-onboarding flow (its Phase 5 now
hands off here) so one chapter owns the owner, whatever path reached it.

**Ownership contract:** this chapter owns **`owner.*` + `owner-profile.md`**. The persona
wizard owns `assistant.*` + `persona.md`. Whichever ran second **confirms** shared fields
(owner name, pronouns, timezone) rather than re-asking — and merges into `identity.json`,
never clobbering the other chapter's fields.

## 0 — Detect + prefill

- If `persona/owner-profile.md` exists, this is a re-run: summarize it in two lines and ask
  what to change — only walk the full interview on request.
- **Consume the candidate-facts sheet** when the store chapter produced one (its Phases 3–4:
  facts read from the owner's existing store content and their global `CLAUDE.md`, each with
  a source citation). Present each candidate as a **prefill to confirm / edit / reject —
  never silently write an inferred fact.** Accepted facts land; rejected ones vanish without
  trace. No sheet → just interview.
- Read `persona/identity.json` if present: any `owner.*` field already set (usually by the
  persona wizard's owner-basics step) is confirmed in passing, not re-asked.

## The interview

One question at a time, a concrete default shown, everything skippable. Sections, in order:

1. **Name + pronunciation.** The owner's name; ask whether it's pronounced the way it's
   spelled and capture a phonetic spelling only if not (`owner.nameSpoken`).
2. **Pronouns.** (`owner.pronouns`.)
3. **Timezone + day boundary.** IANA zone, machine's current zone offered as the default
   (hint it with `python -c "from datetime import datetime; print(datetime.now().astimezone().tzinfo)"`
   — that prints an offset name on some platforms, so have the owner confirm the proper IANA
   name, e.g. `Europe/Berlin`). Then the
   **after-midnight rule**: activity before their usual sleep hour counts as the *prior* day
   — confirm the boundary hour (default 03:00). Date logic everywhere gates on this zone,
   never UTC.
4. **Work.** What they do, where, and roughly what shape their week has — two or three lines
   the briefings can lean on. Nothing sensitive is required; whatever they offer.
5. **Top ~3 projects.** What's actually live right now, one line each (the store's In-Progress
   list is a good prefill source when present).
6. **Key people.** The handful of names the assistant should recognize without asking —
   family, close collaborators, the manager. Name + one clause of context each.
7. **Habits worth tracking.** Recurring things they want nudged about (meds, exercise,
   a daily walk, journaling). These are **seed candidates for Reminders** — collect them
   here; the store flow's close step offers to seed actual reminder rows from them
   (one `store-create` per habit, act-low, shown as a list first).
8. **Escalation preferences.** Quiet hours (when nudges hold), nag tolerance (once and done
   vs. persistent re-nudges), and call-me appetite (should anything ever *ring the phone*,
   and if so what tier — maps to the `Call Me` reminder channel). These calibrate
   `references/reminders-policy.md` for this owner.

## Write

1. Assemble `persona/owner-profile.md` from the answers — short and current beats
   exhaustive; the template's section headings, one owner. Show it. One approval → write.
2. Merge `owner.*` (name / nameSpoken / pronouns / email / timezone) into
   `persona/identity.json` — read-modify-write, preserving `assistant.*` untouched. Create
   the file from `persona/identity.example.json` if it doesn't exist yet.
3. Never write on a skipped confirmation; a fully-skipped interview marks the chapter
   `declined` and the assistant simply runs owner-agnostic.

## Close

```
python seneschal/scripts/setup_state.py mark owner-interview done --artifacts persona/owner-profile.md --summary "profile written; owner.* merged into identity.json"
```

(`identity.json` is deliberately not listed as this chapter's artifact — the `persona`
chapter's built-in artifact rule already covers it, and two chapters hashing one file would
cross-trip `infer`'s staleness check.)
