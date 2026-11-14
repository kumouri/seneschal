@echo off
REM seneschald - the service verbs (install / uninstall / start / stop / restart / status / logs).
REM A thin shim over service.py; see seneschal\docs\service-supervision-spec.md.
setlocal
set "PYEXE=%~dp0..\..\.venv\Scripts\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" "%~dp0service.py" %*
endlocal & exit /b %ERRORLEVEL%
