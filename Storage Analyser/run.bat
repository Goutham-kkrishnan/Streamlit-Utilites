@echo off
rem Start Storage Analyser on http://localhost:8517 (reachable from this computer only). Windows;
rem on Linux use run.sh.
rem
rem Double-click this file in Explorer, or run it from a Command Prompt in any folder. launch.py
rem does the rest: it installs missing Python packages into .venv\ in this folder on the first run
rem (needs internet once) and starts the server. Close this window to stop the app.
setlocal
title Storage Analyser
cd /d "%~dp0"

rem Find Python 3.10+ ("py" is the python.org launcher; the Microsoft Store "python" stub fails this test).
set "PY="
for %%P in ("py -3" "python" "python3") do (
    if not defined PY (
        %%~P -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=%%~P"
    )
)
if not defined PY (
    echo Storage Analyser: Python 3.10 or newer was not found.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this again.
    pause
    exit /b 1
)

%PY% launch.py %*
set "RC=%ERRORLEVEL%"
rem Keep the window open after an error so it can be read.
if not "%RC%"=="0" if not "%RC%"=="130" pause
exit /b %RC%
