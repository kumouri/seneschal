@echo off
rem Proteus hourly hunt cycle — fetch + score + diff + Telegram-notify new hot matches.
rem Stdlib-only, no LLM spend. Scheduled hourly as \seneschal-proteus-hunt (see SCHEDULING.md).
rem Pause switch: create archons\proteus\out\hourly\paused to skip cycles without unscheduling.
set ANTHROPIC_API_KEY=
cd /d %~dp0..\..
if not exist archons\proteus\out\hourly mkdir archons\proteus\out\hourly
python archons\proteus\tools\hunt_cycle.py >> archons\proteus\out\hourly\task.log 2>&1
