@echo off
REM Kotak Neo auto-login wrapper - called by Task Scheduler at 9:00 AM IST.
REM Logs every run to data\login_history.log so you can see what happened.

REM cd to repo root (one level up from scripts\)
cd /d "%~dp0\.."

if not exist data mkdir data

echo. >> data\login_history.log
echo ================================================================ >> data\login_history.log
echo Run at: %date% %time% >> data\login_history.log
echo ================================================================ >> data\login_history.log

python scripts\auto_login.py >> data\login_history.log 2>&1

echo. >> data\login_history.log
echo Finished at: %date% %time% >> data\login_history.log
