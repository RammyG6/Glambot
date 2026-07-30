@echo off
setlocal
title Glambot
cd /d "%~dp0"

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

pip install -q -r requirements.txt
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
