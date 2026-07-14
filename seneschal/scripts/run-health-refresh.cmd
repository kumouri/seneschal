@echo off
REM ============================================================================
REM Seneschal health-cache refresh (for Windows Task Scheduler).
REM
REM Imports any Samsung Health export it hasn't seen yet, then re-renders the
REM dashboard. Both steps are idempotent and exit quietly when there's nothing
REM new, so this is safe to run on a daily timer.
REM
REM   schtasks /Create /TN "seneschal-health-refresh" /SC DAILY /ST 22:30 ^
REM     /TR "%USERPROFILE%\workspace\repos\seneschal\seneschal\scripts\run-health-refresh.cmd"
REM
REM Samsung has no export API — the zip still comes from a manual tap in the
REM phone app (Settings -> Personal data -> Download personal data). This wrapper
REM automates everything downstream of that tap. See HEALTH_SETUP.md.
REM ============================================================================
setlocal

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

REM --- where Samsung's zip lands. Edit if you save exports elsewhere. ---
set "WATCH_DIR=%USERPROFILE%\Downloads"

python "%~dp0health_import.py" --watch-dir "%WATCH_DIR%" || exit /b 1
python "%~dp0health_dashboard.py" || exit /b 1

endlocal
