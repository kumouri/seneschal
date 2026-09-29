# Branch-delete guard — install

A `PreToolUse` hook that **refuses to delete a remote branch while an open pull request has it as
`base`** — because GitHub does not retarget the dependent, it **closes** it. Script:
`branch_delete_guard.py`. Why it exists, how to count past occurrences, and the open question about
GitHub's own documentation: `../docs/stacked-pr-branch-deletion-spec.md`.

**This is a user-level install, and until you do it the guard does nothing.** The hook goes in
**your** `~/.claude/settings.json` — *not* this repo's `.claude/settings.json*` (a repo-shipped hook
would impose it on every install; personal hook config never ships). Pulling the script into your
checkout changes nothing until you add the block below.

**The automated path.** `settings_merge.py` writes exactly the block in step 2 for you — diff-first,
append-only, backup-first:

```bash
python seneschal/scripts/settings_merge.py --guard branch-delete           # dry run: prints the diff
python seneschal/scripts/settings_merge.py --guard branch-delete --apply   # writes it
```

`--guard all` installs every shell/PR guard at once (all but the Notion-only `query-shape`). The
steps below are the manual equivalent, and step 3 applies either way.

---

## Why it exists

`gh pr merge <n> --delete-branch` deletes the head branch. If another open pull request is *stacked*
on that branch — has it as its `base` — GitHub closes the dependent in the same second. Recovery is
three steps by hand (recreate the ref, reopen the PR, retarget it), and a reopened PR does not show
up afterwards as a failure, so the damage is easy to miss entirely.

The predicate is one call and it is cheap:

```bash
gh pr list --state open --base <branch> --json number   # must be empty
```

A written rule ("check before deleting") has to be remembered at exactly the moment the author is
thinking about the merge instead. This is a stop, not a sentence.

## Which branches are protected

Beyond the predicate, some branches are refused outright, whatever is stacked on them: the fixed
floor `develop`, `master`, `main`, `trunk`, `HEAD`, plus any names you add under `protected_branches`
in `seneschal/references/pr-guard.json` (copy `pr-guard.example.json`; the file is gitignored). The
config can **add** names, never remove the floor. `repo_config.py` reads it; `python
seneschal/scripts/repo_config.py` prints the resolved set.

## 1. Check the script is there

Point the hook at the **daemon's** checkout — the one `seneschald-update` keeps current (it runs off
`main`) — so it picks up fixes on the next pull without you touching settings again. Below, `$REPO`
stands for that checkout's absolute path (e.g. `C:/Users/you/workspace/seneschal` or
`/home/you/seneschal`); for the shell commands, set it first (`REPO=/home/you/seneschal` in bash,
`$REPO = "C:/Users/you/workspace/seneschal"` in PowerShell).

```bash
# POSIX
test -f "$REPO/seneschal/scripts/branch_delete_guard.py" && echo present
```

```powershell
# Windows
Test-Path "$REPO/seneschal/scripts/branch_delete_guard.py"
```

It also needs the `gh` CLI on `PATH`, authenticated (`gh auth status`).

## 2. Add it to `~/.claude/settings.json`

Add a **`"PreToolUse"`** entry inside the top-level `"hooks"` object. `"PreToolUse"` is a *list*: if
other `PreToolUse` hooks are already installed (`bash_path_guard.py`, `merge_guard.py`,
`script_file_guard.py`), add this as another sibling object. Every matching hook runs and any one
exiting 2 blocks the call, so they compose without knowing about each other. With no `PreToolUse`
hooks yet:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|PowerShell",
        "hooks": [
          { "type": "command", "command": "python $REPO/seneschal/scripts/branch_delete_guard.py", "timeout": 60 }
        ]
      }
    ]
  }
}
```

Replace `$REPO` with the literal path (forward slashes work on Windows too, and avoid JSON
backslash-escaping). Use `python3` instead of `python` if that is how Python is invoked on your
machine. Mind JSON's no-trailing-comma rule when adding the entry beside existing ones.

**`"timeout": 60`, not 90 and not 10.** This hook makes at most two `gh` calls (`pr view`, then
`pr list`), each capped at 30 s inside the script. It is deliberately shorter than `merge_guard`'s 90:
this one can fire on an ordinary `git push`, and a hook that hangs is a hook that stops your work. A
harness timeout is an **allow** — the same direction this guard already takes when GitHub is
unreachable, so a timeout is not a hole in it, just a quieter version of the outage path.

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

The read-only way, needing no session and touching nothing. Run it from inside a checkout whose
`origin` is on GitHub (or pass `--repo owner/name`):

```bash
# POSIX
python3 "$REPO/seneschal/scripts/branch_delete_guard.py" check --branch develop; echo "exit=$?"
python3 "$REPO/seneschal/scripts/branch_delete_guard.py" check --branch some/merged-branch; echo "exit=$?"
```

```powershell
# Windows
python "$REPO/seneschal/scripts/branch_delete_guard.py" check --branch develop; "exit=$LASTEXITCODE"
python "$REPO/seneschal/scripts/branch_delete_guard.py" check --branch some/merged-branch; "exit=$LASTEXITCODE"
```

`develop` is protected, so the first prints a refusal and exits 2 — which proves the script runs and
can reach GitHub. For the second, name a branch nothing is stacked on: `safe: no open pull request
has ... as its base` and `exit=0` is a working predicate.

You can also drive the hook itself, exactly as the harness does:

```bash
# POSIX
echo '{"tool_name":"Bash","tool_input":{"command":"git push origin --delete develop"}}' |
  python3 "$REPO/seneschal/scripts/branch_delete_guard.py"; echo "exit=$?"
```

```powershell
# Windows
'{"tool_name":"Bash","tool_input":{"command":"git push origin --delete develop"}}' |
  python "$REPO/seneschal/scripts/branch_delete_guard.py"; "exit=$LASTEXITCODE"
```

`exit=2` with the refusal on stderr is a working guard.

Three things that would mean it is **not** doing what you expect:

- A deletion runs and a pull request closes — the hook never fired. Re-check the path in step 1 and
  that `python` resolves on `PATH`.
- You see `branch-delete guard: ALLOWED WITHOUT CHECKING` — the hook fired, but GitHub was
  unreachable and it allowed on purpose. That message is the guard telling you its answer is not a
  verdict. Re-run when you're back online.
- Nothing happens at all on a `git push origin --delete <something>` — check you are in a repository
  whose `origin` is on github.com. Anywhere else, the guard correctly says nothing.

## 4. What it deliberately does **not** do

- **It does not stop a deletion from the GitHub web UI, a browser, or an MCP tool.** It only sees
  Bash and PowerShell commands. This is also why the repository setting *"Automatically delete head
  branches"* is left off and **undecided** — the guard cannot see a server-side deletion, and GitHub's
  own documentation does not say whether that path behaves differently. See the spec.
- **It does not touch `git branch -d` / `-D`.** A local branch deletion cannot close a pull request.
- **It never merges, approves, or records anything.** No state file, no log, no message. Its entire
  output is an exit code and a message on stderr.
- **It does not replace `merge_guard.py`.** That one asks *"may this PR merge?"*; an **approved**
  `gh pr merge <n> --delete-branch` is a yes there, and it is exactly the command that closes a
  stacked PR. Both hooks should be installed; they refuse different acts.

## 5. Sweeping up afterwards

Because you now merge **without** `--delete-branch`, branches accumulate. `branch_sweep.py` clears
them, applying the same predicate per branch immediately before each deletion, and never touching a
protected branch or the base branch (`repo_config.base_branch()`, or `--base-branch`):

```bash
python seneschal/scripts/branch_sweep.py                      # dry run
python seneschal/scripts/branch_sweep.py --merged-within 7
python seneschal/scripts/branch_sweep.py --apply --limit 10
python seneschal/scripts/branch_sweep.py --repo example/repo --base-branch main
```

**It is a dry run unless you pass `--apply`,** and it deletes at most `--limit` branches per run
(default 25). Both bounds exist because a repository that has merged without `--delete-branch` for a
while can carry hundreds of stale heads, and an uncapped first run would remove all of them at once.

The sweep is **not wired into the daemon** — it is a hand-run tool until you have watched a few
`--apply` runs and are happy with what it picks. The predicate does not change if it is scheduled
later.

## 6. Remove it

Delete the `"PreToolUse"` object you added from `~/.claude/settings.json` (mind the comma again) and
start a new session. Nothing else to undo: the hook keeps no state and touches no file.
