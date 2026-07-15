@echo off
REM ===================================================================================================
REM Durable launcher for the seneschal health + presence feed listener (health_listener.py).
REM
REM Receives the phone's POSTs to /health-ingest and /presence-ingest over the LAN/tailnet and imports
REM them into state/health.db and state/presence.db. Runs as its OWN scheduled task ("seneschal-health-
REM listener"), independent of the presence daemon, so a seneschald reload never drops the listener.
REM
REM Token: this binds all interfaces (--host 0.0.0.0), so it REQUIRES a bearer token, read from the
REM HEALTH_INGEST_TOKEN user env var. Set it to the SAME value configured in the companion
REM phone app (its PRESENCE_INGEST_TOKEN / HEALTH_INGEST_TOKEN):
REM     setx HEALTH_INGEST_TOKEN <token>
REM
REM Register the task (see SCHEDULING.md), or smoke-test by just double-clicking this file.
REM ===================================================================================================
cd /d %~dp0

if "%HEALTH_INGEST_TOKEN%"=="" (
  echo [health-listener] HEALTH_INGEST_TOKEN is not set - refusing to bind an unguarded listener.
  echo   Set it once with:  setx HEALTH_INGEST_TOKEN ^<token^>   ^(must match the phone app's token^)
  exit /b 2
)

:loop
python health_listener.py --host 0.0.0.0
echo [health-listener] process exited; restarting in 5s...
timeout /t 5 /nobreak >nul
goto loop
