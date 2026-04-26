@echo off
REM One-time setup: registers Windows Task Scheduler entry to run
REM auto_login.py every weekday at 9:00 AM IST.
REM
REM Run this ONCE by double-clicking or: setup_schedule.bat

echo.
echo ================================================================
echo Kotak Neo Auto-Login - Task Scheduler Setup
echo ================================================================
echo.
echo This will schedule auto_login.py to run:
echo   - Every Monday to Friday
echo   - At 9:00 AM IST
echo   - Even if you're not logged into Windows (requires password)
echo.
echo If you want to skip password prompt, we will use /RL LIMITED
echo which requires you to be logged in at 9 AM.
echo.
pause

cd /d "%~dp0"
set SCRIPT_PATH=%~dp0run_login.bat

REM Delete existing task if any (ignore error if not exists)
schtasks /delete /tn "KotakNeoAutoLogin" /f >nul 2>&1

REM Create new task - runs Mon-Fri at 9:00 AM, only when user logged in
schtasks /create ^
    /tn "KotakNeoAutoLogin" ^
    /tr "\"%SCRIPT_PATH%\"" ^
    /sc weekly ^
    /d MON,TUE,WED,THU,FRI ^
    /st 09:00 ^
    /rl LIMITED ^
    /f

if %errorlevel% equ 0 (
    echo.
    echo ================================================================
    echo SUCCESS! Task scheduled.
    echo ================================================================
    echo.
    echo Task name: KotakNeoAutoLogin
    echo Runs: Mon-Fri at 9:00 AM
    echo Script: %SCRIPT_PATH%
    echo.
    echo To test NOW without waiting for 9 AM:
    echo   schtasks /run /tn "KotakNeoAutoLogin"
    echo.
    echo To see next run time:
    echo   schtasks /query /tn "KotakNeoAutoLogin" /v /fo LIST
    echo.
    echo To delete this schedule later:
    echo   schtasks /delete /tn "KotakNeoAutoLogin" /f
    echo.
    echo Check login_history.log after each run to see results.
    echo ================================================================
) else (
    echo.
    echo ERROR: Task creation failed. See message above.
)

echo.
pause
