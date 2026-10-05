# A second Claude login for jobs — host setup

`jobs.py start --account <name>` runs a job on a named Claude login (a config dir). Script:
`claude_accounts.py`. Why it exists and how it carries through retries and resumes:
`../docs/background-jobs-spec.md` §3.15.

**This part is host-side.** The login is an interactive browser flow, and the registry and the
linked files are host-side state, so no PR can do any of it. The flag ships with the code; nothing
uses it until the steps below are done. Paths below use `C:\Users\<you>` and the daemon's checkout
as the working directory — substitute your own.

---

## 1. Log the second account in, into its own config dir

PowerShell. Pick the directory once; `.claude-2` is the name the rest of this doc uses.

```powershell
$env:CLAUDE_CONFIG_DIR = "C:\Users\<you>\.claude-2"
claude /login
```

Sign in with the **second** account in the browser that opens. Then close that shell, or run
`Remove-Item Env:CLAUDE_CONFIG_DIR`, so nothing else started from it lands on account 2 by accident.

Check it took, from a fresh shell:

```powershell
Test-Path "C:\Users\<you>\.claude-2\.credentials.json"   # must be True
$env:CLAUDE_CONFIG_DIR = "C:\Users\<you>\.claude-2"; claude auth status; Remove-Item Env:CLAUDE_CONFIG_DIR
```

`claude auth status` should name the second account's email. The normal login (`~\.claude`) is
untouched: a separate config dir keeps its own `.credentials.json` and `.claude.json`.

On Linux/macOS the same steps are `CLAUDE_CONFIG_DIR=~/.claude-2 claude /login` and
`test -f ~/.claude-2/.credentials.json`.

## 2. Link the rails into it — REQUIRED, and enforced

Claude Code reads its **hooks** from `<config dir>\settings.json` and the **global instructions**
from `<config dir>\CLAUDE.md`. A fresh `.claude-2` has neither. A job there would run with no
`merge_guard.py`, no `send_gate_hook.py`, no `job_background_guard.py`, and none of the standing
rules in your global `CLAUDE.md`. So `jobs.py` **refuses** `--account 2` until both files match
`~\.claude`'s. The check runs on every start, so a link that later breaks is caught the next time a
job asks for the account.

Symlinks (Windows may need an elevated shell, or Developer Mode, for `New-Item -ItemType
SymbolicLink`):

```powershell
New-Item -ItemType SymbolicLink -Path "C:\Users\<you>\.claude-2\settings.json" -Target "C:\Users\<you>\.claude\settings.json"
New-Item -ItemType SymbolicLink -Path "C:\Users\<you>\.claude-2\CLAUDE.md" -Target "C:\Users\<you>\.claude\CLAUDE.md"
```

(`ln -s ~/.claude/settings.json ~/.claude-2/settings.json`, and the same for `CLAUDE.md`, elsewhere.)

A symlink follows every future edit to the original. A plain copy passes today and is refused the
first time either file changes. That refusal is the check doing its job, not a bug.

**Optional, not checked:** the `commands`, `agents` and `skills` directories (link them the same
way if a job needs `/assistant` or a skill), and MCP servers. Those live in `.claude-2\.claude.json`,
so a job on account 2 has no store/Slack/Gmail MCP unless you `claude mcp add` them under that
config dir. Most delegated jobs only need git, `gh` and Python, so leaving MCP off account 2 also
keeps its reach smaller.

## 3. Register the account

Create `seneschal\state\claude-accounts.json` (gitignored; the tracked seed is
`seneschal\state\claude-accounts.example.json`):

```json
{
  "default": null,
  "2": "C:\\Users\\<you>\\.claude-2"
}
```

`null` means the normal `~\.claude` login. `~` and `%VARS%`/`$VARS` are expanded. Keys starting with
`_` are ignored, so a `"_note"` can live beside the entries.

## 4. Try it

```powershell
python seneschal\scripts\jobs.py start --title "account 2 smoke" --account 2 --no-agent -- python -c "import os; print(os.environ.get('CLAUDE_CONFIG_DIR'))"
python seneschal\scripts\jobs.py list
```

The job's log should print the `.claude-2` path, and the completion push carries an `Account: 2`
line. A refusal is exit 2 with one line naming what to fix: unknown name, missing dir, never logged
in, or rails not linked.

## What this does NOT do

- **It never picks an account for you.** Choosing the one with headroom is a follow-up
  (`../docs/background-jobs-spec.md` §8). Today a job runs where the owner or the assistant points it.
- **It never bills an API key.** `ANTHROPIC_API_KEY` is scrubbed from every job, on every account.
- **The caps stay one global pair.** They protect the owner's concurrency, and that is the same desk
  whichever subscription pays (`jobs.py caps`).
