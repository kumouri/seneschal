@echo off
rem Proteus daily job digest — rolls up the hourly hunt cycles, emails the owner, pings Telegram.
rem Stdlib-only, no LLM spend. Scheduled daily (e.g. ~17:30 local) as \seneschal-proteus-digest (see SCHEDULING.md).
set ANTHROPIC_API_KEY=
cd /d %~dp0..\..
if not exist archons\proteus\out\digests mkdir archons\proteus\out\digests
python archons\proteus\tools\daily_digest.py >> archons\proteus\out\digests\task.log 2>&1
