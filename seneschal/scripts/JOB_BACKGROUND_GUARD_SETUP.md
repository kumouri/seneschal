# Job background guard — install

A `PreToolUse` hook that refuses a **Bash** command asking to `run_in_background` while running
inside a `jobs.py`-launched job — the process is a single-prompt `claude -p` run with no next turn to
receive a background result on, so backgrounding a step there strands the job's work every time.
Script: `job_background_guard.py`. Why it exists and what it complements:
`../docs/background-jobs-spec.md` §3.14 (the preventive half of §3.13's DETECT/RESUME/RESCUE).

**This is a user-level install.** The hook goes in **your** `~/.claude/settings.json` — *not* this
repo's `.claude/settings.json*` (a repo-shipped hook would impose it on every install; personal hook
config never ships). Pulling the script into your checkout changes nothing until you add the block
below.

---

## 1. Check the script is there

Point the hook at the **daemon's** checkout — the one `seneschald-update` keeps current — so it picks
up fixes on the next pull without you touching settings again. Below, `$REPO` stands for that
checkout's absolute path (e.g. `C:/Users/you/workspace/seneschal` or `/home/you/seneschal`); for the
shell commands below, set it first (`REPO=/home/you/seneschal` in bash, `$REPO = "C:/Users/you/workspace/seneschal"`
in PowerShell).

```bash
# POSIX
test -f "$REPO/seneschal/scripts/job_background_guard.py" && echo present
```

```powershell
# Windows
Test-Path "$REPO/seneschal/scripts/job_background_guard.py"
```

## 2. Add it to `~/.claude/settings.json`

Add a **`"PreToolUse"`** entry inside the top-level `"hooks"` object. `"PreToolUse"` is a *list*, so
if another Bash `PreToolUse` hook is already installed, add this as a separate sibling entry, each
with its own `"matcher": "Bash"` block. Order does not matter. With no `PreToolUse` hooks yet:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "python $REPO/seneschal/scripts/job_background_guard.py", "timeout": 10 }
        ]
      }
    ]
  }
}
```

Replace `$REPO` with the literal path (forward slashes work on Windows too, and avoid JSON
backslash-escaping). Use `python3` instead of `python` if that is how Python is invoked on your
machine. Mind JSON's no-trailing-comma rule when adding the entry beside existing ones.

Check the file still parses:

```bash
# POSIX
python3 -c "import json, os; json.load(open(os.path.expanduser('~/.claude/settings.json'))); print('settings.json parses')"
```

```powershell
# Windows
Get-Content "$env:USERPROFILE\.claude\settings.json" -Raw | ConvertFrom-Json | Out-Null; "settings.json parses"
```

Settings are re-read for **new** sessions. A running delegated job was already launched, so this
takes effect on the *next* job, not a job already in flight.

`"matcher": "Bash"` means the hook process only spawns for Bash calls. The script checks the tool
name **and** the `SENESCHAL_JOB_ID` env var again internally, so the matcher is an optimisation and
not the safety mechanism.

## 3. Verify it fired

Start a job and ask it to background something:

```bash
python seneschal/scripts/jobs.py start --title "guard smoke test" --worktree -- \
  claude --dangerously-skip-permissions -p "Run 'python -c \"import time; time.sleep(5)\"' with run_in_background true, then say what happened."
```

The job's log should show the Bash tool call refused, with the reason beginning *"Blocked: this Bash
command asks to run in the background..."* — and the agent continuing without ever getting a
background result to wait on.

You can also check the hook directly, without a job, by simulating the env var it looks for:

```bash
# POSIX
echo '{"tool_name":"Bash","tool_input":{"command":"sleep 5","run_in_background":true}}' |
  SENESCHAL_JOB_ID=smoke-test python3 "$REPO/seneschal/scripts/job_background_guard.py"; echo "exit=$?"
```

```powershell
# Windows
$env:SENESCHAL_JOB_ID = "smoke-test"
'{"tool_name":"Bash","tool_input":{"command":"sleep 5","run_in_background":true}}' |
  python "$REPO/seneschal/scripts/job_background_guard.py"; "exit=$LASTEXITCODE"
Remove-Item Env:\SENESCHAL_JOB_ID
```

`exit=2` and the message on stderr is a working guard. `exit=0` and silence with `SENESCHAL_JOB_ID`
set is a guard that is not matching — that would be a bug in the script, not in your settings. With
`SENESCHAL_JOB_ID` **unset**, the same payload must exit `0` — that is the guard correctly doing
nothing outside a job.

## 4. What it deliberately does **not** do

- **An ordinary interactive session is completely untouched**, even one requesting
  `run_in_background` — that usage is safe there (there is a next turn to receive the notification on)
  and blocking it would just be noise. The `SENESCHAL_JOB_ID` env var is the entire scope: absent,
  this hook is a no-op on every path.
- **Every other tool is untouched** — Edit, Write, Read, MCP tools, Agent, all of them.
- **It never blocks on its own failure.** Unreadable input, a broken payload, a crash in the script:
  all of those allow the command.
- **It writes nothing.** No state file, no log, no network. Stdlib only, no venv.
- **It does not replace §3.13's DETECT/RESUME/RESCUE** (`job_completion.py`) — that mechanism still
  runs for a job launched without this hook installed, or one that strands work some other way. This
  hook only closes the one specific tool call that causes the common case.

## 5. Remove it

Delete the `"PreToolUse"` entry you added from `~/.claude/settings.json` (mind the comma again) and
start a new session (or wait for the next job). Nothing else to undo: the hook keeps no state and
touches no file.
