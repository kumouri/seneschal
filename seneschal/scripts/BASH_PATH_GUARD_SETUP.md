# Bash path guard — install

A `PreToolUse` hook that refuses a **Bash** command containing a double-quoted Windows path ending in
a backslash, because the trailing `\` escapes the closing `"` and the command dies at the parser —
usually with an error reported far from the real mistake. Script: `bash_path_guard.py` (its module
docstring carries the full rationale and the hook contract).

**This is a user-level install, and until you do it the guard does nothing.** The hook goes in
**your** `~/.claude/settings.json` — *not* this repo's `.claude/settings.json*` (a repo-shipped hook
would impose it on every install; personal hook config never ships). Pulling the script into your
checkout changes nothing until you add the block below.

**The automated path.** `settings_merge.py` writes exactly the block in step 2 for you — diff-first,
append-only, backup-first:

```bash
python seneschal/scripts/settings_merge.py --guard bash-path           # dry run: prints the diff
python seneschal/scripts/settings_merge.py --guard bash-path --apply   # writes it
```

`--guard all` installs every shell/PR guard at once (all but the Notion-only `query-shape`). The
steps below are the manual equivalent, and step 3 applies either way.

---

## 1. Check the script is there

Point the hook at the **daemon's** checkout — the one `seneschald-update` keeps current (it runs off
`main`) — so it picks up fixes on the next pull without you touching settings again. Below, `$REPO`
stands for that checkout's absolute path (e.g. `C:/Users/you/workspace/seneschal` or
`/home/you/seneschal`); for the shell commands, set it first (`REPO=/home/you/seneschal` in bash,
`$REPO = "C:/Users/you/workspace/seneschal"` in PowerShell).

```bash
# POSIX
test -f "$REPO/seneschal/scripts/bash_path_guard.py" && echo present
```

```powershell
# Windows
Test-Path "$REPO/seneschal/scripts/bash_path_guard.py"
```

## 2. Add it to `~/.claude/settings.json`

Add a **`"PreToolUse"`** entry inside the top-level `"hooks"` object. `"PreToolUse"` is a *list*, so
if other `PreToolUse` hooks are already installed (the merge guard, the script-file guard, …), add
this as a separate sibling object. Every matching hook runs and any one exiting 2 blocks the call, so
they compose without knowing about each other. With no `PreToolUse` hooks yet:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "python $REPO/seneschal/scripts/bash_path_guard.py", "timeout": 10 }
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

Settings are re-read for **new** sessions. Start a fresh `claude` session (or `/hooks` in a running
one) to pick it up.

`"matcher": "Bash"` means the hook process only spawns for Bash calls — no `python` start-up on every
Read or Edit. The script checks the tool name **again** internally, so the matcher is an optimisation
and not the safety mechanism.

## 3. Verify it fired

Drive the hook directly, without a session. The payload is JSON, so a Windows path inside it has its
backslashes doubled:

```bash
# POSIX
echo '{"tool_name":"Bash","tool_input":{"command":"ls \"C:\\Users\\me\\state\\\""}}' |
  python3 "$REPO/seneschal/scripts/bash_path_guard.py"; echo "exit=$?"
```

```powershell
# Windows
'{"tool_name":"Bash","tool_input":{"command":"ls \"C:\\Users\\me\\state\\\""}}' |
  python "$REPO/seneschal/scripts/bash_path_guard.py"; "exit=$LASTEXITCODE"
```

`exit=2` with the message *"Blocked: this Bash command contains a double-quoted Windows path ending
in a backslash."* on stderr is a working guard. `exit=0` and silence is a guard that is not matching —
a bug in the script, not in your settings. The same payload with `"tool_name":"PowerShell"` must exit
`0`: that is the guard correctly staying out of PowerShell.

In a session, ask for a Bash command such as `ls -la "C:\Users\you\some\folder\"` and watch it be
refused. Two things that would mean it is **not** working:

- The command runs and reports `` unexpected EOF while looking for matching `"' `` — the hook never
  fired. Re-check the path in step 1 and that `python` resolves on `PATH`.
- The command runs and succeeds — you were in **PowerShell**, not Bash. That is correct behaviour;
  see below.

## 4. What it deliberately does **not** do

- **PowerShell is untouched, on purpose.** In PowerShell the backslash is not an escape character, so
  `"C:\path\"` closes its quote and runs correctly. Every PowerShell match would be a false positive
  by construction.
- **Every other tool is untouched** — Edit, Write, Read, MCP tools, all of them.
- **It never blocks on its own failure.** Unreadable input, a broken payload, a crash in the script:
  all of those allow the command. There is no failure mode of this hook that blocks your work.
- **It writes nothing.** No state file, no log, no network. Stdlib only, no venv — it runs under a
  bare `python` in any session on the machine, including ones with nothing to do with this repo.
- **It is one narrow rule, not a style policy.** Broader candidates (`;`-chained, multi-line,
  heredoc) are properties of correct shell and would refuse many working commands for each broken
  one; `script_file_guard.py` covers the other mechanically-broken shapes.

## 5. Known false positives

The expression has no notion of escaping at its left edge, so an *escaped* quote can act as its
opening quote and a doubled backslash can be an escape rather than a path separator:

```bash
python3 -c "print(f\"\\n=== header ===\")"                  # the \\n reads as a path separator
powershell -Command "... -Filter \\\"Name='python.exe'\\\""  # a quote escaped into a nested shell
```

Both are correct Bash and both get refused. They are rare next to the real catches, and the
rejection message names this case and points at the fix that is right for it anyway — put the
command in a script file, which is what the escaping was fighting. If the guard starts firing on
something you write **often**, that is a reason to revisit the rule; `test_bash_path_guard.py`'s
`KnownFalsePositiveTest` is where those shapes are pinned.

## 6. Remove it

Delete the `"PreToolUse"` object you added from `~/.claude/settings.json` (mind the comma again) and
start a new session. Nothing else to undo: the hook keeps no state and touches no file.
