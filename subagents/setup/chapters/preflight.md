# Chapter: preflight

Look before anything is touched. This chapter detects the platform, inventories the tools the
later chapters lean on, sanity-checks the checkout, initializes the ledger, and shows the owner
the road ahead. **It writes nothing but the ledger** — no env files, no installs, no persona.

## 1 — Platform

Detect once, from the interpreter the wizard will actually use:

```
python -c "import sys; print(sys.platform)"
```

`win32` / `linux` / `darwin`. The ledger's top-level `platform` field records it automatically
on the first write (`setup_state.py` stamps `platform.system().lower()` — `windows` / `linux` /
`darwin` — when it creates the file); confirm the two agree and tell the owner what was
detected. Every later chapter reads the ledger's value instead of re-sniffing.

## 2 — Tool inventory

Probe each tool with its `--version` (cheap; capture the version string where it prints one).
Batch the probes; a missing tool is a *finding*, not an error — nothing installs here.

| Tool | Probe | Needed for | If absent |
|---|---|---|---|
| Python 3.11+ | `python --version` (or `python3` on POSIX) | everything | hard stop — the wizard itself runs on it; point at python.org / the platform package manager and stop politely |
| git | `git --version` | the checkout, the daemon's self-update | hard stop for updates; the wizard can continue but say what breaks |
| `claude` CLI | `claude --version` | the brain — daemon, delegation, MCP | hard stop for anything beyond file edits; point at the Claude Code install docs |
| uv | `uv --version` | the optional venv (`websockets`, `tzdata`) + the cockpit extra | note it; Discord gateway degrades to REST polling, cockpit chapter will re-ask |
| node + npm | `node --version` / `npm --version` | the cockpit web build, the phone Worker | note it; the cockpit chapter re-checks before offering the build |

Present the inventory as one compact table with found-version or "not found" per row. Do not
offer installs here — the chapter that *needs* a missing tool owns that conversation
(per-step confirmed, per the SKILL's ground rules).

## 3 — Repo sanity

- **A real checkout:** `git rev-parse --show-toplevel` from the repo root succeeds and points
  at this directory. If not (a tarball download), say plainly that self-update and the
  merge-is-deploy loop won't work, and continue — nothing else here needs git history.
- **`seneschal/state/` writable:** the ledger write in step 4 is itself the probe. If it
  fails, surface the OS error and stop — every later chapter depends on writing state.

## 4 — Ledger init

Respect pre-wizard work **first**:

```
python seneschal/scripts/setup_state.py infer
python seneschal/scripts/setup_state.py board
```

`infer` promotes chapters whose artifacts already exist (a hand-copied `telegram.env`, a
pre-built persona) to `done (inferred)` — a machine that was set up before the wizard existed
self-heals instead of being re-interviewed. Show the board and, if anything was inferred, name
it: "you've already got X — I won't re-ask."

## 5 — The road ahead

Close with a short human paragraph, not a lecture: the chapter list from the SKILL's table
(persona → store → owner interview → auth + models → channels → MCP → cockpit → daemon →
verify), that everything is skippable, that it resumes if interrupted (`/setup` picks up
where it left off), and that `/setup <chapter>` jumps anywhere.

Then mark and move on:

```
python seneschal/scripts/setup_state.py mark preflight done --summary "<platform>; python <ver>, git <ver>, claude <ver>; uv/node <found|missing>"
```
