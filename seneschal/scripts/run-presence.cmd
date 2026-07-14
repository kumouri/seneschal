@echo off
REM ============================================================================
REM Seneschal presence daemon launcher (for Windows Task Scheduler).
REM
REM Point the scheduled task's "Program/script" at THIS file (no Arguments needed):
REM   Program/script:  %USERPROFILE%\workspace\repos\seneschal\seneschal\scripts\run-presence.cmd
REM   Trigger:         At log on   (single instance; restart on failure)
REM
REM This wrapper exists so you don't fight Task Scheduler quoting — edit the knobs
REM here, in a normal file, instead of in the Arguments field. It also guarantees
REM SUBSCRIPTION billing (not metered API):
REM   * It clears ANTHROPIC_API_KEY for this process tree.
REM   * It relies on CLAUDE_CODE_OAUTH_TOKEN being set in your user environment
REM     (one-time:  claude setup-token   then   setx CLAUDE_CODE_OAUTH_TOKEN <token>).
REM ============================================================================
setlocal

REM --- billing safety: never let a stray API key force metered API billing ---
set "ANTHROPIC_API_KEY="

REM --- force UTF-8 so emoji / em-dashes in replies don't mojibake on Windows ---
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

REM --- run from the repo root so the daemon's relative paths resolve ---
cd /d "%~dp0..\.."

REM --- OPTIONAL add-ons: uncomment the matching continuation line inside the command below ---
REM  * Notion for HEADLESS runs (gives the Telegram assistant read/write Notion so acks/edits persist — fixes
REM    "Notion's not reachable" AND the ack-never-lands re-nudge loop). See NOTION_MCP_SETUP.md.
REM    EASIEST: `cp notion-mcp.json.example notion-mcp.json` — presence.py AUTO-DETECTS it, no flag needed.
REM    To point elsewhere:  --notion-mcp "C:\path\to\notion-mcp.json" ^   ·  to force OFF:  --no-notion ^
REM    (stdio internal-integration variant only: also  set "NOTION_TOKEN=ntn_your_token"  above.)
REM  * Discord two-way channel (DISCORD_SETUP.md): AUTO-DETECTED — just create scripts\discord.env
REM    (gateway push when the venv is live, REST fallback otherwise). To force OFF:  --no-discord ^
REM  * Phone-call reminders (Call Me):  --call-env "%~dp0push-call.env" ^
REM  * Internal scheduled runs (brief/wrap/dream/journal + the 4 reminder slots) are ON by default;
REM    add  --no-slots  to disable, or  --slot-model <id>  to run them on a specific model.

REM --- interpreter: prefer the uv-managed venv (has the daemon's one dependency, websockets),
REM     fall back to system python (the daemon degrades gracefully without the venv — the Discord
REM     gateway falls back to REST polling). `uv sync --frozen` (or seneschald-update) creates .venv.
set "PYEXE=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"

REM --- knobs (edit these freely) ---
"%PYEXE%" "%~dp0presence.py" ^
  --model claude-opus-4-8 ^
  --idle-min 20 ^
  --poll-timeout 25 ^
  --peek-interval-min 5 ^
  --call-env "%~dp0push-call.env" ^
  --log-file "%~dp0..\state\presence.log" ^
  --watch-model claude-haiku-4-5-20251001 ^
  --watch-prompt "Run Watch mode (seneschal/SKILL.md): glance at email/Slack/calendar and escalate only if something is genuinely hot; otherwise exit."

endlocal
