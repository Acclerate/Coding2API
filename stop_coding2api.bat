@echo off
:: Coding2API stop script: find the process listening on port 8321 and kill it
:: (graceful taskkill first, force with /F if that fails).
::
:: ASCII-only on purpose -- same lesson as start_coding2api.bat: cmd.exe
:: misparses batch files that mix multi-byte (UTF-8) text with chcp switches,
:: which once garbled commands into "'xxx' is not recognized" and skipped
:: real lines. chcp 65001 is safe here because this file stays ASCII.
chcp 65001 >nul

echo ========================================
echo   Stopping Coding2API ...
echo ========================================

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8321.*LISTENING"') do (
    echo Found PID: %%a
    echo Sending close signal...
    taskkill /PID %%a /T >nul 2>&1
    if errorlevel 1 (
        echo Graceful close failed, forcing...
        taskkill /PID %%a /T /F >nul 2>&1
        if errorlevel 1 (
            echo [FAILED] Cannot kill PID %%a, close it manually in Task Manager.
            pause
            exit /b 1
        )
    )
    echo [OK] PID %%a terminated.
    echo.
    echo Coding2API stopped.
    echo.
    timeout /t 2 /nobreak >nul
    exit /b 0
)

echo [HINT] No process listening on port 8321 -- service may not be running.
echo.
pause
