# Script-file guard — install

A `PreToolUse` hook that **refuses the three shell one-liners that are mechanically broken, and
names the script file that fixes each.** Script: `script_file_guard.py` (its module docstring carries
the full rationale, the tokenizer design and the hook contract).

**This is a user-level install, and until you do it the guard does nothing.** The hook goes in
**your** `~/.claude/settings.json` — *not* this repo's `.claude/settings.json*` (a repo-shipped hook
would impose it on every install; personal hook config never ships).

**The automated path.** `settings_merge.py` writes exactly the block in step 2 for you — diff-first,
append-only, backup-first:

```bash
python seneschal/scripts/settings_merge.py --guard script-file           # dry run: prints the diff
python seneschal/scripts/settings_merge.py --guard script-file --apply   # writes it
```

`--guard all` installs every shell/PR guard at once (all but the Notion-only `query-shape`). The
steps below are the manual equivalent, and step 3 applies either way.

---

## Why it exists

*"Shell work goes in a script file, not a one-liner"* is a convention that is easy to agree with and
does not bind: under time pressure an agent reaches for the one-liner anyway, and a prose rule has no
way to refuse it. This hook is the enforcement half — but only the part that can be enforced without
refusing correct work:

| Rule | What it refuses | The mechanism |
|---|---|---|
| `oversize` | a command of 7,500 characters or more | it does not reach the shell intact: the text is truncated in transport and the shell reports an unterminated quote at the cut |
| `cross_shell_expansion` | `powershell -Command "… $_ …"` from Bash, `bash -lc "… $d …"` from PowerShell | this shell substitutes before the other shell reads — and when the result is still valid, it **runs silently wrong** |
| `heredoc_in_powershell` | `<<` in a PowerShell command | PowerShell has no heredoc; `<` is a reserved operator |

Each one refuses commands that were already broken. **It does not enforce the whole convention as
written** — §5 says which part is left to prose, and why.

## 1. Check the script is there

Point the hook at the **daemon's** checkout — the one `seneschald-update` keeps current (it runs off
`main`) — so it picks up fixes on the next pull without you touching settings again. Below, `$REPO`
stands for that checkout's absolute path (e.g. `C:/Users/you/workspace/seneschal` or
`/home/you/seneschal`); for the shell commands, set it first (`REPO=/home/you/seneschal` in bash,
`$REPO = "C:/Users/you/workspace/seneschal"` in PowerShell).

```bash
# POSIX
test -f "$REPO/seneschal/scripts/script_file_guard.py" && echo present
```

```powershell
# Windows
Test-Path "$REPO/seneschal/scripts/script_file_guard.py"
```

## 2. Add it to `~/.claude/settings.json`

Add a **`"PreToolUse"`** entry inside the top-level `"hooks"` object. `"PreToolUse"` is a *list*: if
other `PreToolUse` hooks are already installed (`bash_path_guard.py`, `merge_guard.py`,
`branch_delete_guard.py`), add this as another sibling object. Every matching hook runs and any one
exiting 2 blocks the call, so they compose without knowing about each other. With no `PreToolUse`
hooks yet:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|PowerShell",
        "hooks": [
          { "type": "command", "command": "python $REPO/seneschal/scripts/script_file_guard.py", "timeout": 10 }
        ]
      }
    ]
  }
}
```

Replace `$REPO` with the literal path (forward slashes work on Windows too, and avoid JSON
backslash-escaping). Use `python3` instead of `python` if that is how Python is invoked on your
machine. Mind JSON's no-trailing-comma rule when adding the entry beside existing ones.

**`"timeout": 10` is generous, not tight.** This hook makes **no network call, opens no file and
spawns no process** — it is string work, and the whole process (Python start-up included) finishes in
well under a second. `merge_guard` needs 90 because it calls `gh`; there is nothing here that can be
slow, and **a hook timeout is an allow**, so a short budget on a hook that cannot hang fails in the
harmless direction.

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

## 3. Verify it fired

The read-only way, needing no session, no network and no repository — a refusal, then the control:

```bash
# POSIX
python3 "$REPO/seneschal/scripts/script_file_guard.py" check --tool PowerShell --command "git commit -F - <<'EOF'"; echo "exit=$?"
python3 "$REPO/seneschal/scripts/script_file_guard.py" check --command "git status"; echo "exit=$?"
```

```powershell
# Windows
python "$REPO/seneschal/scripts/script_file_guard.py" check --tool PowerShell --command "git commit -F - <<'EOF'"; "exit=$LASTEXITCODE"
python "$REPO/seneschal/scripts/script_file_guard.py" check --command "git status"; "exit=$LASTEXITCODE"
```

`BLOCKED by heredoc_in_powershell` with `exit=2`, then `allowed: no rule matches this Bash command`
with `exit=0`. **Run both.** A guard that refuses everything and a guard that refuses nothing look
identical from one check.

You can also drive the hook itself, exactly as the harness does:

```bash
# POSIX
echo '{"tool_name":"Bash","tool_input":{"command":"powershell -Command \"Write-Output $_.Name\""}}' |
  python3 "$REPO/seneschal/scripts/script_file_guard.py"; echo "exit=$?"
```

```powershell
# Windows
'{"tool_name":"Bash","tool_input":{"command":"powershell -Command \"Write-Output $_.Name\""}}' |
  python "$REPO/seneschal/scripts/script_file_guard.py"; "exit=$LASTEXITCODE"
```

`exit=2` with the refusal on stderr is a working guard. `exit=0` and silence would be a bug in the
script, not in your settings.

**In a session**, ask for a Bash command such as
`powershell -Command "Get-Process | Where-Object { $_.CPU -gt 30 }"` and watch it be refused. If it
runs and prints a plausible-looking empty table, the hook never fired — that is precisely the silent
failure this rule exists for (Bash ate `$_` and PowerShell filtered on nothing).

## 4. The escape it is required to leave open

Refusing heredocs would refuse `cat > script.sh <<'EOF'`, which is the obvious way to *create* the
script a refusal asks for. **A guard that blocks the only route to compliance is a wall**, so there
are two routes and both are verified by tests rather than asserted here:

1. **Create the file with the Write tool**, then run it with a short command —
   `bash x.sh`, `pwsh -NoProfile -File x.ps1`, `python x.py`. Every rejection message says this in
   those words, and `TheEscapeRouteTest` drives that exact pair through the guard in both tools.
2. **A heredoc in the Bash tool is not refused at all.** Only PowerShell lacks a heredoc, so
   `cat > /tmp/x.sh <<'EOF'` still works — right up to the 7,500-character size limit, which is the
   point at which it stops working anyway.

Keep the script until the task is done, so a fix is an edit rather than a retype, and **keep it out
of a repo working tree** — a stray untracked file at a path an incoming PR touches aborts
`pull --ff-only` and silently stalls the daemon's auto-reload.

## 5. What it deliberately does **not** do

- **It does not refuse `;`-chained, multi-line, or heredoc-bearing commands as such.** Those are
  properties of *correct* shell scripting; a rule on them refuses a large share of every shell call
  for each broken one it catches, and a guard like that gets uninstalled the same day. They stay
  prose.
- **It does not check `python -c "…"`.** It is arguably a nested shell and it is common; it is left
  out rather than guessed at.
- **It applies the same 7,500 limit to PowerShell.** PowerShell commands that size are rare, so the
  cost is negligible, and it means being refused in one shell is not an invitation to retype the same
  thing into the other.
- **It never blocks on its own failure.** Unreadable input, a broken payload, a missing sibling
  module, a crash in a predicate: all of those **allow** the command. `merge_guard` is the opposite
  by design; the two assert opposite exit codes on the same class of input, and if they ever agree
  one of them is wrong.
- **It writes nothing.** No state file, no log, no network. Stdlib only, no venv — it runs under a
  bare `python` in any session on the machine, including ones with nothing to do with this repo.

## 6. If it starts firing on something you write often

Check the exact command first:

```bash
python seneschal/scripts/script_file_guard.py check --command-file /path/to/the-command.txt
```

The likeliest candidate is `oversize` at 7,500. It sits below the transport failure band on purpose;
raising it toward 8,000 removes the occasional unnecessary block and gives up some real catches. It is
a one-constant change (`MAX_COMMAND_CHARS`) with a test that pins the band, so the test tells you what
you are giving up at the moment you change it.

## 7. Remove it

Delete the `"PreToolUse"` object you added from `~/.claude/settings.json` (mind the comma again) and
start a new session. Nothing else to undo: the hook keeps no state and touches no file.
