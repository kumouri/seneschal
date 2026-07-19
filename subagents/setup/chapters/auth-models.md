# Chapter: auth-models

Billing safety and the two model dials. Four steps: verify the subscription token exists,
check `ANTHROPIC_API_KEY` hygiene, interview the owner's plan tier into a dials choice, then
probe and write the dials. Nothing here ever sees, echoes, or stores a token value —
**presence checks only**.

## 1 — The subscription token

Explain first, briefly: the daemon, the Forge, and every delegated one-shot run the `claude`
CLI **subscription-billed**. Unattended processes (a scheduled task, a systemd unit) don't
inherit an interactive login, so they need `CLAUDE_CODE_OAUTH_TOKEN` — minted once by the
owner, in their own terminal:

```
claude setup-token
```

**The owner runs that themselves and stores the result themselves** — the wizard never
handles the value:

- **Windows:** `setx CLAUDE_CODE_OAUTH_TOKEN <the token>` in their own terminal (user
  environment; new processes pick it up).
- **Linux / macOS:** put `CLAUDE_CODE_OAUTH_TOKEN=<the token>` in
  `~/.config/seneschal/daemon.env` (create the directory; `chmod 600` the file). That's the
  file the daemon chapter's launchers will source — keep it out of shell profiles so it
  doesn't leak into every process.

Then verify **presence only**:

- **Windows** — the wizard's own shell predates any `setx`, so check the user environment
  fresh (prints `True`/`False`, never the value):

  ```
  powershell -NoProfile -Command "[bool][Environment]::GetEnvironmentVariable('CLAUDE_CODE_OAUTH_TOKEN','User')"
  ```

- **Linux / macOS** — file exists and the var is non-empty (quiet grep; nothing echoed):

  ```
  test -f ~/.config/seneschal/daemon.env && grep -q '^CLAUDE_CODE_OAUTH_TOKEN=.' ~/.config/seneschal/daemon.env && echo present || echo absent
  ```

Absent + the owner skips → fine: interactive use works today; note that the *daemon* chapter
will need it and record that in the summary.

## 2 — `ANTHROPIC_API_KEY` hygiene

A persistently-set `ANTHROPIC_API_KEY` silently flips any bare `claude` invocation to
**metered API billing**. The launchers and the daemon scrub it defensively, but a stray key
still bites every hand-run `claude -p`. Check for a *persistent* setting (again, presence
only):

- **Windows:** the same `[Environment]::GetEnvironmentVariable(...)` pattern, for both
  `'User'` and `'Machine'` scopes.
- **Linux / macOS:** `grep -l 'ANTHROPIC_API_KEY' ~/.bashrc ~/.zshrc ~/.profile ~/.bash_profile 2>/dev/null` —
  filenames only.

If set: explain the risk plainly, then **offer removal instructions for the owner to run
themselves** (don't run them unasked — the key may serve other projects):

- Windows: `powershell -NoProfile -Command "[Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY', $null, 'User')"`
- POSIX: delete the export line from the named profile file(s), then open a fresh shell.

Declining is fine — record "API key present, owner keeping it" in the summary so the doctor
knows.

## 3 — Plan tier → the dials

Ask one question: **which Claude plan is this?** Then propose the matching row — these are
defaults to confirm, not mandates (the owner can pick any coherent pair):

| Plan | Warm model | Max-routable ceiling | Delegation |
|---|---|---|---|
| **Pro** | sonnet | sonnet | off (the ceiling never admits it) |
| **Max 5x** | sonnet — opus optional, with a plain warning that an always-warm opus eats a 5x budget fast | opus | off |
| **Max 20x** | opus | fable | **on** — quota-gated by the governor's defaults (`state/governor-config.json` absent = defaults; the cockpit's Thresholds panel tunes them later) |
| **Not sure** | the Max-5x row | | |

The dials' meaning, one line each: the **warm model** is what the resident chat session
runs; the **ceiling** is the hard cap on delegating a hard turn up — delegation exists at
all only when the ceiling admits the top tier. The rank and the short aliases
(`haiku`/`sonnet`/`opus`/`fable`) live in `seneschal/scripts/model_config.py` (`RANK` /
`ALIASES`) — that file is the authority; don't restate ids from memory.

Also ask (defaults fine for almost everyone): the **watch model** for the cheap comms-peek
and the **slot model** for scheduled runs — default is the launchers' shipped values (a
haiku-tier watch; slots on the warm model). Record the choices in the ledger for the daemon
chapter to render into launchers:

```
python -c "import sys; sys.path.insert(0, 'seneschal/scripts'); import setup_state; s = setup_state.load(); s['models'] = {'watch': '<id or null>', 'slots': '<id or null>', 'plan': '<pro|max5x|max20x|unsure>'}; setup_state.save(s)"
```

## 4 — Probe, then write

**Probe after ask, before write.** For each **unique** model id the owner settled on (warm,
ceiling, watch, slots — deduped), confirm the plan can actually run it:

```
claude -p "ok" --model <id> --max-turns 1
```

(~120 s timeout each.) On failure: show the error verbatim, step **down one rank** (the
`RANK` order in `model_config.py`), and re-probe — a Pro plan that asked for opus finds its
real level in one step. Never write a dial that didn't probe clean.

**Write the dials.** `model_config.py` has **no CLI write subcommand** (its CLI only prints
the current config) — the write is the module API, which validates the pair, normalizes
aliases, and writes atomically:

```
python -c "import sys; sys.path.insert(0, 'seneschal/scripts'); import model_config; print(model_config.save('seneschal/state', '<warm>', '<ceiling>'))"
```

A `ValueError` here (unrecognized id, warm outranking the ceiling) is a config bug — fix the
pair, don't force it. Read back with `python seneschal/scripts/model_config.py` and show the
owner the resulting JSON.

## Close

```
python seneschal/scripts/setup_state.py mark auth-models done --artifacts seneschal/state/model-config.json --summary "token <present|absent>; dials <warm>/<ceiling>; delegation <on|off>"
```

Name the marquee failure this chapter just prevented: a warm model the plan can't run means
the daemon's **warm spawn dies on arrival** — Telegram goes silent and the cockpit chat pane
can't load — with nothing obviously "wrong" anywhere. Probed dials make that impossible.
