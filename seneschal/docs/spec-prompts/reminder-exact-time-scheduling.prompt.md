# Session prompt — Exact-time reminders (retire the 4-slot model)

> Paste this whole file as the opening prompt of a fresh Claude Code session in the **seneschal** repo.
> It's a *planning* prompt: produce a spec for the owner's sign-off **before** writing any code.
> This one is **entangled** with a lot of reminder machinery — read carefully before designing.

---

You are working in the **seneschal** repo (the owner's chief-of-staff assistant). Your job this session is to
**design, then (on my approval) build** a move from the current **4 fixed reminder slots** to
**arbitrary / exact-time reminder scheduling**.

## Why
Reminders currently fire only at **4 fixed daily slots** — Morning 08:00 / Midday 12:30 / Evening 18:30 /
Bedtime 21:30 (owner-local). That's an artifact of the **original** system, which used Claude scheduled
actions. The presence daemon is now an **asyncio reactive core with a ~5-second scheduler tick** and can
fire at **any exact time**. I want the 4-slot model **retired** in favor of real per-reminder times.

## Read first (this is the entangled part — read it all)
- `seneschal/references/reminders-policy.md` — **especially**: "The four daily slots", the **daily reset**
  (Morning-slot-only, once per owner-local day), "Catch-up stagger", "Quiet window", "Standing rolls", the
  **Cadence** rules, and the **Fire-time ack gate**. Understand what each slot does *besides* firing.
- `seneschal/docs/asyncio-daemon-design.md` — the scheduler tick, supervised tasks, invariants.
- `seneschal/references/databases.md` — the ⏰ Reminders schema (`Time Window`, `Cadence`, `Due / Target`,
  `Last Reminded` / `Last Acknowledged`).
- `seneschal/scripts/SCHEDULING.md` and `seneschal/scripts/reminders_enqueue.py` / `reminders_roll.py` /
  `sentinel.py` (`check_reminders`) — how nudges are enqueued and fired today.

## Scope the spec to cover
1. **The new model** — a per-reminder explicit time-of-day (and a small set of times for multiple/day),
   fired at the exact due minute via the existing scheduler tick + `reminders.json` queue +
   `reminders_enqueue.py`. Decide whether `Time Window` **survives as optional sugar** (coarse presets that
   map to default times) or is **fully replaced**.
2. **Migration** — map each existing `Time Window` (Morning/Midday/Evening/Bedtime/Anytime) to a concrete
   default time, and give a concrete plan for the existing ⏰ rows so nothing regresses.
3. **Decouple the daily reset** from "Morning slot only" — where does the once-per-local-day reset run now
   that there's no morning slot? Propose a clean trigger (first tick after local midnight, or a dedicated
   daily-reset scheduler entry).
4. **Everything that must still hold** under exact-time firing — explain how each survives the slot removal:
   **catch-up stagger** (drip, don't wall), **presence-gate defer** (asleep/driving/away — NO DROPS),
   **quiet window** (drop-not-defer + pierce set), **standing rolls** (every-N-hours), **fire-time ack gate**.
5. **Where the slots' other duties move** — re-fire of unacked important items, snooze re-surface, the daily
   reset — each needs a new home.
6. **Rollout + testing** — how to ship this merge-on-green without a flag-day; what `test_*.py` coverage is
   needed.
7. **Open questions** — default times per old window, whether to keep coarse presets, per-reminder overrides.

## How to work
- **Spec first.** Write `seneschal/docs/reminder-exact-time-scheduling-spec.md` (Markdown is canonical), tone
  like `seneschal/docs/asyncio-daemon-design.md`. **Stop for my sign-off before implementation** — this touches
  the nerve center, so I want to read the design before any code lands.
- Conventional Commits (`docs(reminders):`, `feat(reminders):`). Branch off `develop` (Git Flow; `develop` deploys, `master` is the release point).
- The **stdlib unittest suite** must stay green (`python -m unittest discover -s seneschal/scripts -p "test_*.py"`);
  add tests for the new scheduling. CI green before any merge; **never merge red/pending**; **merge commit** only.
- `git -c core.fsmonitor=false …` on every git command.
- Remember the daemon **runs off `develop` and reloads on merge** — design the cutover so a mid-flight reload
  doesn't drop or double-fire reminders.

Start by reading the files above (the reminder machinery especially), then draft the spec and walk me
through the migration + open questions.
