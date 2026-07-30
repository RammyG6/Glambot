@echo off
rem Internal helper for Glambot.bat: polls the app URL until it responds, then
rem opens it in the default browser. Not meant to be run directly.
set HOST=%1
set PORT=%2

for /l %%n in (1,1,60) do (
    curl.exe -s -o NUL "http://%HOST%:%PORT%/"
    if not errorlevel 1 (
        start "" "http://%HOST%:%PORT%/"
        exit /b 0
    )
    timeout /t 1 /nobreak >nul
)
exit /b 1
