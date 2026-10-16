@echo off
:: Coding2API autostart script
:: Place in shell:startup to launch at user login.
:: NOTE: keep this file ASCII-only. cmd.exe misparses batch files that mix
::       multi-byte (UTF-8) text with chcp switches, which caused the garbled
::       "'xxx' is not recognized" error and could skip real commands.
:: chcp 65001 is safe here (file stays ASCII) and makes both the console and
:: cmd's ">>" redirection speak UTF-8 -- the venv python emits UTF-8 output.
chcp 65001 >nul

:: Log lives on D: next to the project; C: is off-limits for logs. Every line
:: goes to BOTH the console and the log file (tee via PowerShell below).
cd /d "D:\privategit\github\Coding2API"
if errorlevel 1 (
    echo [ERROR] Cannot cd to project dir: D:\privategit\github\Coding2API
    pause
    exit /b 1
)
set LOG_DIR=D:\privategit\github\Coding2API\logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set LOG_FILE=%LOG_DIR%\coding2api_startup.log
if not exist "%LOG_FILE%" type nul > "%LOG_FILE%"

set TIMESTAMP=[%date% %time%]
echo %TIMESTAMP% Starting Coding2API... (log: %LOG_FILE%)
echo === uvicorn session start at %date% %time% === >> "%LOG_FILE%"

:: Force python stdio to UTF-8. Without this, a service launched from
:: Startup/cmd has no PYTHONIOENCODING/PYTHONUTF8 in its environment and the
:: venv python falls back to the ANSI codepage (GBK here): log lines with
:: Chinese reach the pipe as GBK bytes and get mangled downstream.
set PYTHONIOENCODING=utf-8

:: Tee via PowerShell: decode child output as UTF-8, append each line to the
:: log with explicit UTF-8 no-BOM (Add-Content would mojibake non-ASCII and
:: add a BOM), then echo it back to the console. Do NOT pipe through findstr:
:: an empty search string exits findstr instantly and breaks the pipe -- every
:: later write fails with OSError [Errno 22] ("--- Logging error ---" spam).
:: `exit $LASTEXITCODE` relays uvicorn's exit code back to %errorlevel%.
powershell -NoProfile -Command "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); & 'C:\Users\Administrator\.local\bin\uv.exe' run -- uvicorn src.main:build_app --factory --host 127.0.0.1 --port 8321 2>&1 | ForEach-Object { $line = [string]$_; [IO.File]::AppendAllText('%LOG_FILE%', $line + [char]13 + [char]10, [Text.UTF8Encoding]::new($false)); $line }; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 }"

:: Service exited; EXIT_CODE is uvicorn's own exit code, relayed through
:: `exit $LASTEXITCODE` above (PowerShell itself always exits 0 otherwise).
set EXIT_CODE=%errorlevel%
set TIMESTAMP=[%date% %time%]
echo %TIMESTAMP% Service exited, code: %EXIT_CODE%
echo === uvicorn service exited, code: %EXIT_CODE% at %date% %time% === >> "%LOG_FILE%"
