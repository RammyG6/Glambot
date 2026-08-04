@echo off
setlocal
title Glambot
cd /d "%~dp0"

rem This script only works from inside the Glambot folder - every path below
rem is relative to its own location. Copying it somewhere else (the Desktop,
rem say) would otherwise silently build a stray virtualenv there and then
rem fail on the missing requirements.txt. Checked before anything is created.
set "GLAMBOT_OK=1"
if not exist "requirements.txt" set "GLAMBOT_OK="
if not exist "glambot\pipeline.py" set "GLAMBOT_OK="
if not defined GLAMBOT_OK (
    echo.
    echo This file has to stay in the Glambot folder - it could not find
    echo requirements.txt and glambot\pipeline.py next to it.
    echo.
    echo Looked in: "%CD%"
    echo.
    echo For a Desktop launcher, use a shortcut or a small .bat that calls
    echo this one, rather than a copy of it.
    echo.
    pause
    exit /b 1
)

if not exist .venv (
    echo Creating virtualenv - first run may take a minute...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Could not create the virtual environment. Is Python installed and on PATH?
        echo See WINDOWS_SETUP.md for help.
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

rem --disable-pip-version-check: a "new release of pip is available" notice on
rem every launch is noise, and it misleads - it looks like a Glambot problem.
pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo Failed to install dependencies. Check your internet connection and try again.
    pause
    exit /b 1
)

if not exist .env (
    echo.
    echo .env not found - copy .env.example to .env and fill in your credentials first.
    echo See WINDOWS_SETUP.md for details.
    pause
    exit /b 1
)

set HOST=127.0.0.1
set PORT=5000
for /f "usebackq tokens=2 delims==" %%V in (`findstr /b "HOST=" .env`) do set HOST=%%V
for /f "usebackq tokens=2 delims==" %%V in (`findstr /b "PORT=" .env`) do set PORT=%%V

echo.
echo Starting Glambot - your browser will open automatically at http://%HOST%:%PORT%/ once ready.
echo.

start "" /b "%~dp0_open_browser.bat" %HOST% %PORT%

python -m glambot.pipeline

echo.
echo Glambot has stopped.
pause
