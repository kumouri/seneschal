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
- **The host's own context counts as a source too.** Claude Code may already know the
  owner's name or email from their user-level `~/.claude/CLAUDE.md` or the account it's
  signed into — nothing in Seneschal reads those, but the model sees them. Treat any such
  value exactly like a sheet candidate: show it with its source ("from your global
  CLAUDE.md", "from your Claude account") and confirm / edit / reject.
- Read `persona/identity.json` if present: any `owner.*` field already set (usually by the
  persona wizard's owner-basics step) is confirmed in passing, not re-asked.

## The interview

**One question per turn, asked with the picker** (the SKILL's ground rules): a concrete
default or suggestions as options, the tool's free-text "Other", and a **Skip** option — never
two questions in one message, and never a tool call or file write after the question. Every
section is skippable.

**Resume cursor.** On entering the chapter, `get owner-interview` — a recorded `step` means a
crashed walk: confirm the earlier sections' answers in passing (from `identity.json` /
the draft) and resume at the recorded section rather than question 1. As **each** section
starts, re-mark the cursor first (before the question, so the turn still ends on it):

```
python seneschal/scripts/setup_state.py mark owner-interview in-progress --step "section <n>: <name>"
```

(e.g. `--step "section 5: projects"`). Sections, in order:

1. **Name + pronunciation.** The owner's name; ask whether it's pronounced the way it's
   spelled and capture a phonetic spelling only if not (`owner.nameSpoken`).
2. **Pronouns.** (`owner.pronouns`.) Then, as its own question, **their email addresses** — the
   primary (`owner.email`) and any others a message to them might go to, e.g. a work address
   (`owner.emails`, a list). The outbound send gate treats exactly these as "the owner": a send
   only to them passes untouched, anything else needs an approval (`seneschal/scripts/SEND_GATE_SETUP.md`).
3. **Timezone + day boundary.** IANA zone, machine's current zone offered as the default
   (hint it with `python -c "from datetime import datetime; print(datetime.now().astimezone().tzinfo)"`
   — that prints an offset name on some platforms, so have the owner confirm the proper IANA
   name, e.g. `Europe/Berlin`). Then the
   **after-midnight rule**: activity before their usual sleep hour counts as the *prior* day
   — confirm the boundary hour (default 05:00; a whole hour, 0–12). The answer is
   `owner.dayBoundaryHour` (an int, e.g. `4` for 04:00). Date logic everywhere gates on this
   zone and this cut, never UTC.
4. **Work.** What they do, where, and roughly what shape their week has — two or three lines
   the briefings can lean on. Nothing sensitive is required; whatever they offer.
5. **Top ~3 projects.** What's actually live right now, one line each (the store's In-Progress
   list is a good prefill source when present).
6. **Key people.** The handful of names the assistant should recognize without asking —
   family, close collaborators, the manager. Name + one clause of context each.

   **Sections 4–6 are free-form, and still one question per turn through the picker.** They
   are the ones most tempting to batch ("tell me about your work, projects, and people") —
   don't. Each gets its own turn and its own `--step` mark. Offer what's known as options,
   each **with its source** — for §4 a work line drafted from the candidate-facts sheet; for
   §5 up to three candidate projects from the store's In-Progress list or the sheet (a
   multi-select picker fits: pick the live ones, "Other" to add); for §6 names the sheet
   surfaced — plus "Other" for their own words and **Skip**. No candidates → a single
   Skip-plus-Other picker is still the question form.
7. **Habits worth tracking.** Recurring things they want nudged about (meds, exercise,
   a daily walk, journaling). These are **seed candidates for Reminders** — collect them
   here; the store flow's close step offers to seed actual reminder rows from them
   (one `store-create` per habit, act-low, shown as a list first).
8. **Escalation preferences.** Quiet hours (when nudges hold), nag tolerance (once and done
   vs. persistent re-nudges), and call-me appetite (should anything ever *ring the phone*,
   and if so what tier — maps to the `Call Me` reminder channel). These calibrate
   `references/reminders-policy.md` for this owner.

## Write

1. Mark `--step "write"`, then assemble `persona/owner-profile.md` from the answers — short
   and current beats exhaustive; the template's section headings, one owner. Show it. One
   approval → write (in the turn *after* the approval, so no question ever has a file card
   under it).
2. Merge `owner.*` (name / nameSpoken / pronouns / email / emails / timezone / dayBoundaryHour) into
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
