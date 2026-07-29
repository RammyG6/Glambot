@echo off
setlocal
rem Starts the Glambot footage pipeline: inbox watcher + local review/approve app.
cd /d "%~dp0"

if not exist .venv (
    echo Creating virtualenv...
    python -m venv .venv
)

call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

if not exist .env (
    echo .env not found - copy .env.example to .env and fill in your credentials first.
    exit /b 1
)

python -m glambot.pipeline
