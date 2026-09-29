# Query-shape annotation — install

A `PostToolUse` hook that, when a Notion query carried **`LIMIT` with no `ORDER BY`**, puts one true
sentence next to the tool result:

> *this result was truncated without an order; absence from it is not evidence of absence.*

Script: `query_shape_hook.py` (its module docstring carries the full rationale and the hook contract).

**Notion backend only.** The query it reads is the Notion MCP's `notion-query-data-sources` SQL mode;
the Obsidian and Markdown backends have no such call. On any other active store backend
(`seneschal/store/config.json`'s `active`) the hook is a strict no-op — exit 0, nothing printed,
nothing logged — so installing it on a filesystem-backed install is harmless but pointless. That is
also why `settings_merge.py --guard all` leaves it out.

**It annotates and it cannot block.** `PostToolUse` runs after the tool has already returned, so
there is nothing left to refuse; and the script never emits `decision: "block"`, never exits
non-zero, and never writes to stderr. The worst thing it can do is fail to annotate.

**This is a user-level install, and until you do it the hook does nothing.** It goes in **your**
`~/.claude/settings.json` — *not* this repo's `.claude/settings.json*` (personal hook config never
ships).

**The automated path.** `settings_merge.py` writes exactly the block in step 3 for you — diff-first,
append-only, backup-first:

```bash
python seneschal/scripts/settings_merge.py --guard query-shape           # dry run: prints the diff
python seneschal/scripts/settings_merge.py --guard query-shape --apply   # writes it
```

It must be named explicitly (`--guard all` does not include it). Check the matcher afterwards against
your Notion MCP server's name — see step 3.

---

## Why it exists

A claim like *"that row never got written"* can be wrong even when the turn behind it looks thorough:
live queries against the right data source, and the scope sentence *"I queried your database"* is
**true** — which is exactly what makes it convincing. The defect is one query argument:
`SELECT … WHERE … LIMIT 4` with no `ORDER BY` returns an arbitrary four of however many rows matched,
and reading those as *the* rows leads straight to a duplicate write. This is a syntactic property of
the call, decidable without judging the answer, so it can be stated next to the result rather than
added as one more written rule the assistant is supposed to remember.

## 1. Check the script is there

Point the hook at the **daemon's** checkout — the one `seneschald-update` keeps current (it runs off
`main`) — so it picks up fixes on the next pull without you touching settings again. Below, `$REPO`
stands for that checkout's absolute path (e.g. `C:/Users/you/workspace/seneschal` or
`/home/you/seneschal`); for the shell commands, set it first (`REPO=/home/you/seneschal` in bash,
`$REPO = "C:/Users/you/workspace/seneschal"` in PowerShell).

```bash
# POSIX
test -f "$REPO/seneschal/scripts/query_shape_hook.py" && echo present
```

```powershell
# Windows
Test-Path "$REPO/seneschal/scripts/query_shape_hook.py"
```

## 2. Verify the mechanism BEFORE wiring it up

One command, no session, no hook event needed:

```bash
# POSIX
python3 "$REPO/seneschal/scripts/query_shape_hook.py" --explain "SELECT * FROM \"collection://x\" WHERE Name LIKE '%x%' LIMIT 4"
```

```powershell
# Windows
python "$REPO/seneschal/scripts/query_shape_hook.py" --explain "SELECT * FROM \`"collection://x\`" WHERE Name LIKE '%x%' LIMIT 4"
```

You should get `{"fires": true, "line": "[query shape] This result was truncated…", "notion_backend": …}`.
The same command with `ORDER BY Name` inserted before `LIMIT` must print `"fires": false`. If those two
disagree, stop — that is a bug in the script and not in your settings. `notion_backend` reports whether
this install's active store is Notion; if it is `false`, the hook will stay silent in real sessions
no matter what the SQL says.

## 3. Add it to `~/.claude/settings.json`

Add a **`"PostToolUse"`** entry inside the top-level `"hooks"` object. `"PostToolUse"` is a *list*,
so if another `PostToolUse` hook is already installed, add this as a separate sibling object. With no
`PostToolUse` hooks yet:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "mcp__notion__notion-query-data-sources",
        "hooks": [
          { "type": "command", "command": "python $REPO/seneschal/scripts/query_shape_hook.py", "timeout": 10 }
        ]
      }
    ]
  }
}
```

Replace `$REPO` with the literal path (forward slashes work on Windows too, and avoid JSON
backslash-escaping). Use `python3` instead of `python` if that is how Python is invoked on your
machine. Mind JSON's no-trailing-comma rule when adding the entry beside existing ones.

**Adjust the matcher to your Notion MCP server's name.** Tool names are `mcp__<server>__<tool>`, and
`notion-query-data-sources` is the standard Notion MCP tool; `notion` is only the most common server
name. If yours is registered under another name (`mcp__notion-work__…`, a connector id, …), change
the `mcp__notion__` prefix to match, or widen it to a regex such as
`"mcp__.*__notion-query-data-sources"`. The script checks the tool name again internally — it matches
the tool-name **suffix** — so the matcher is an optimisation, not the safety mechanism.

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

## 4. Verify it fired

Drive the hook directly, exactly as the harness does:

```bash
# POSIX
echo '{"hook_event_name":"PostToolUse","session_id":"t","tool_name":"mcp__notion__notion-query-data-sources","tool_input":{"data":{"query":"SELECT * FROM \"c://x\" LIMIT 4"}}}' |
  python3 "$REPO/seneschal/scripts/query_shape_hook.py"; echo "exit=$?"
```

```powershell
# Windows
'{"hook_event_name":"PostToolUse","session_id":"t","tool_name":"mcp__notion__notion-query-data-sources","tool_input":{"data":{"query":"SELECT * FROM \"c://x\" LIMIT 4"}}}' |
  python "$REPO/seneschal/scripts/query_shape_hook.py"; "exit=$LASTEXITCODE"
```

On a Notion install a working hook prints one JSON object containing `additionalContext` and
`exit=0`. Silence with `exit=0` means either the active backend is not Notion (step 2's
`notion_backend`) or it is not matching — the latter is a bug in the script, not in your settings.

**If the annotation never appears in a real session but the command above works**, the harness is not
surfacing `hookSpecificOutput.additionalContext` on `PostToolUse`, or the matcher does not match your
server name (step 3). Either degrades to *no annotation* and costs nothing else — the designed
fail-open direction, not a broken install. Do not switch it to `decision: "block"` to force the point:
that would turn an annotation into a correction.

## 5. The fire log

Every fire appends one row to `seneschal/state/query-shape.jsonl` — when, which detector, which tool,
which session, and whether the line was injected. **Not the query**: a Notion SQL string carries your
column values, and a fire log is not a place for them. Expected volume is a handful of rows a day at
most; the log exists so any later decision to widen the hook rests on a measured firing rate.

Adding `--report-only` to the `"command"` above logs fires without injecting anything, if you want a
measurement period with literally zero behaviour change first.

## 6. What it deliberately does **not** do

- **It never blocks and never fails a query.** There is no code path that emits a block, and the test
  suite asserts that across every branch including malformed input.
- **It does nothing off-Notion.** Another backend, no store configured, an unreadable
  `store/config.json`: exit 0, nothing printed, nothing logged.
- **It does not fire on an ordered query**, even one with a `LIMIT`. An annotation next to a visible
  `ORDER BY` reads as a broken checker, and a gate dies of its false-positive rate.
- **It misses the subquery case**, knowingly: an `ORDER BY` anywhere in the statement suppresses the
  finding, including one that orders only a subquery while the outer `LIMIT` stays unordered. That is
  the conservative direction.
- **`head -N` on a grep and Notion's default page size are the same bug and are NOT built.** They are
  declared and disabled in `DETECTORS`. Several unmeasured firing rates behind one gate is how a gate
  gets switched off in a week.
- **It adds no rule anywhere.** The line states what the result is; it does not tell the assistant to
  write `ORDER BY`.
- **Stdlib only, no venv.** It runs under a bare `python` in any session on the machine.

## 7. Remove it

Delete the `"PostToolUse"` object from `~/.claude/settings.json` and start a new session. Nothing else
holds state; the fire log is append-only and safe to leave or delete.
