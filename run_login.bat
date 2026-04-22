@echo off
REM Kotak Neo auto-login wrapper - called by Task Scheduler at 9:00 AM IST
REM Logs every run to login_history.log so you can see what happened

cd /d "%~dp0"

echo. >> login_history.log
echo ================================================================ >> login_history.log
echo Run at: %date% %time% >> login_history.log
echo ================================================================ >> login_history.log

python auto_login.py >> login_history.log 2>&1

echo. >> login_history.log
echo Finished at: %date% %time% >> login_history.log
