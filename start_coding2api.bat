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

:: 7-day retention: the tee below appends forever, so trim at session
:: start. Session markers ("=== uvicorn session start at ... ===" with
:: a YYYY/M/D date) are parsed; everything before the oldest marker
:: dated within 7 days is dropped. If every session is older than that
:: the file is truncated -- a fresh session starts right below. UTF-8
:: no-BOM rewrite matches the tee's encoding. Safe only here, before
:: the tee starts appending (it reopens the file per line).
powershell -NoProfile -Command "$f='%LOG_FILE%'; if (Test-Path $f) { $cut=(Get-Date).Date.AddDays(-7); $L=[IO.File]::ReadAllLines($f); $keep=-1; $found=$false; for ($i=$L.Count-1; $i -ge 0; $i--) { if ($L[$i] -match '=== uvicorn session start at .*(\d{4})/(\d{1,2})/(\d{1,2})') { $found=$true; $d=Get-Date -Year $Matches[1] -Month $Matches[2] -Day $Matches[3]; if ($d -ge $cut) { $keep=$i } else { break } } }; if ($found) { if ($keep -lt 0) { [IO.File]::WriteAllText($f, '', [Text.UTF8Encoding]::new($false)) } elseif ($keep -gt 0) { [IO.File]::WriteAllLines($f, $L[$keep..($L.Count-1)], [Text.UTF8Encoding]::new($false)) } } }"

set TIMESTAMP=[%date% %time%]
echo %TIMESTAMP% Starting Coding2API... (log: %LOG_FILE%)
echo === uvicorn session start at %date% %time% === >> "%LOG_FILE%"

:: Force python stdio to UTF-8. Without this, a service launched from
:: Startup/cmd has no PYTHONIOENCODING/PYTHONUTF8 in its environment and the
:: venv python falls back to the ANSI codepage (GBK here): log lines with
:: Chinese reach the pipe as GBK bytes and get mangled downstream.
set PYTHONIOENCODING=utf-8

:: Pin the bind address explicitly (Settings defaults are HOST=127.0.0.1,
:: PORT=8000; the .env file may or may not override PORT). These must match
:: what clients/gateway configs expect: 127.0.0.1:8321.
set HOST=127.0.0.1
set PORT=8321

:: Tee via PowerShell: decode child output as UTF-8, append each line to the
:: log with explicit UTF-8 no-BOM (Add-Content would mojibake non-ASCII and
:: add a BOM), then echo it back to the console. Do NOT pipe through findstr:
:: an empty search string exits findstr instantly and breaks the pipe -- every
:: later write fails with OSError [Errno 22] ("--- Logging error ---" spam).
:: `exit $LASTEXITCODE` relays uvicorn's exit code back to %errorlevel%.
::
:: Launch via `python -m src.main` (the console entry, src.main.run) instead
:: of `uvicorn src.main:build_app --factory`: run() acquires the single-
:: instance lock (the uvicorn CLI form bypassed it entirely, so the lock file
:: never existed on this deployment) and applies the hardened uvicorn flags
:: (proxy_headers=False against loopback XFF spoofing, graceful-shutdown
:: window, access_log off -- request logging is done by the app middleware).
powershell -NoProfile -Command "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); & 'C:\Users\Administrator\.local\bin\uv.exe' run -- python -m src.main 2>&1 | ForEach-Object { $line = [string]$_; [IO.File]::AppendAllText('%LOG_FILE%', $line + [char]13 + [char]10, [Text.UTF8Encoding]::new($false)); $line }; if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 }"

:: Service exited; EXIT_CODE is uvicorn's own exit code, relayed through
:: `exit $LASTEXITCODE` above (PowerShell itself always exits 0 otherwise).
set EXIT_CODE=%errorlevel%
set TIMESTAMP=[%date% %time%]
echo %TIMESTAMP% Service exited, code: %EXIT_CODE%
echo === uvicorn service exited, code: %EXIT_CODE% at %date% %time% === >> "%LOG_FILE%"
