# Chapter: verify

The closing chapter — one end-to-end doctor pass over everything the walk configured, red
rows walked to a resolution, and an honest send-off. This chapter **configures nothing**
itself: it runs the doctor, folds in the in-session MCP probes, and offers jumps back to
whichever chapter owns each problem. Re-runnable forever; it's also just `/doctor` with the
wizard's closing ceremony around it.

## 1 — The doctor board

```
python seneschal/scripts/setup_doctor.py
```

Show the board as-is (it's already owner-readable: `[+]`/`[!]`/`[x]`/`[ ]` per surface, a fix
pointer per non-green row, exit code = red count). Then offer the two deepenings, each its
own question:

- **Model probes** — "Want me to live-probe the model dials? (~2 min worst case; one
  `claude -p` per configured model, subscription-billed.)" Yes → re-run with `--probe`.
- **Telegram send test** — only when the telegram row is green: "Send a real test message to
  your Telegram now?" **Only an explicit yes** → re-run with `--send-test`, and ask whether
  the message actually arrived (the script can't see the phone).

## 2 — In-session MCP probes

The script can only see files; MCP tool **auth is per-session**. Same probe table as
`/doctor` — one cheap call per *configured* server, skip the rest, never register or OAuth
anything here (that's the `mcp` chapter's):

| When | Probe |
|---|---|
| the store row says notion backend | `notion-get-teams` (or any trivial `notion-*` call) |
| ledger `mcp:calendar` is done / awaiting-auth-restart | `list_calendars` |
| `seneschal/scripts/slack-mcp.json` present, or ledger `mcp:slack` done | one `slack_search_channels` (any term) |

Probe answers → GREEN. Tools absent or auth error → YELLOW, with the fix (`/mcp` → the
server → finish the OAuth, or `-> /setup mcp:<server>`). Fold these verdicts into the board
(replace the store row's "verify in-session" caveat; add one row per probed server) and show
the **combined board** with its `N green, N yellow, N red` summary. Never report a probe you
didn't run.

## 3 — Walk the reds (and the loud yellows)

For each RED row, in board order: one line on what's broken, then offer the jump —
"`/setup env:telegram` now?" A yes goes straight to that chapter (this chapter stays
`in-progress` with `--step "walking reds"`; the wizard resumes here after). A no is fine —
the row stays red on the board and that's an honest answer. YELLOW rows get one line each
only where the owner can act (a declined feature's SKIP needs no speech); never nag about
yellows that are by-design (default persona, no daemon yet).

## 4 — Stamp + mark

Record the pass in the ledger (statuses and counts only — the `doctor_last` block, same as
`/doctor`), then mark the chapter:

```
python -c "import sys; sys.path.insert(0, 'seneschal/scripts'); import setup_state; s = setup_state.load(); s['doctor_last'] = {'at': setup_state._now(), 'green': G, 'yellow': Y, 'red': R, 'skip': K}; setup_state.save(s)"
python seneschal/scripts/setup_state.py mark verify done --summary "doctor: G green / Y yellow / R red"
```

(Fill G/Y/R/K from the combined board.) Reds the owner chose not to fix don't block `done` —
the board, not this chapter's status, is the ground truth for health.

## 5 — The send-off

Close the wizard in a short human paragraph, from the board — not a template, but it covers:

- **What works now** — name the green surfaces in plain words ("Telegram is wired and
  verified, your store is Notion and answering, the persona is built…").
- **What's deliberately off** — declined/skipped features in one honest line, with the
  degradation ("no email — triage falls back to Gmail drafts").
- **The three doors:** `/assistant` starts a chat with the assistant right now; `/doctor`
  re-runs this health check any time; `/setup <chapter>` reopens any chapter to change an
  answer.
- **The daemon**, until its chapter lands: starting the always-on loop is a by-hand step —
  point at `seneschal/scripts/SCHEDULING.md`.
