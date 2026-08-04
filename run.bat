@echo off
setlocal
rem Starts the Glambot footage pipeline: inbox watcher + local review/approve app.
cd /d "%~dp0"

rem Paths below are relative to this script's own folder, so a copy left
rem elsewhere would build a stray virtualenv there. Fail before that happens.
set "GLAMBOT_OK=1"
if not exist "requirements.txt" set "GLAMBOT_OK="
if not exist "glambot\pipeline.py" set "GLAMBOT_OK="
if not defined GLAMBOT_OK (
    echo This file has to stay in the Glambot folder - could not find
    echo requirements.txt and glambot\pipeline.py in "%CD%".
    exit /b 1
)

if not exist .venv (
    echo Creating virtualenv...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
pip install -q --disable-pip-version-check -r requirements.txt

if not exist .env (
    echo .env not found - copy .env.example to .env and fill in your credentials first.
    exit /b 1
)

python -m glambot.pipeline
