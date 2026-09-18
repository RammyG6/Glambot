@echo off
setlocal enabledelayedexpansion
rem Builds GlambotSetup.exe: PyInstaller (bundles Python + deps + ffmpeg/
rem ffprobe into a standalone .exe) then Inno Setup (wraps that into a
rem proper installer with its own data-folder picker and shortcuts).
rem
rem One-time setup on THIS build machine only:
rem   - the .venv already created by Glambot.bat / run.bat
rem   - Inno Setup: winget install -e --id JRSoftware.InnoSetup
rem
rem ⚠ The resulting windows_app\GlambotSetup.exe embeds this machine's real
rem .env / credentials.json / token.json (see installer.iss). Treat it like
rem .env itself - never commit it, upload it, or share it casually.

cd /d "%~dp0\.."
set "REPO_ROOT=%CD%"

rem Single version source shared with mac_app - see VERSION's own comment
rem and windows_app\installer.iss's header.
set /p APP_VERSION=<"%REPO_ROOT%\VERSION"

if not exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
    echo Run Glambot.bat once first to create the virtualenv.
    pause
    exit /b 1
)

call "%REPO_ROOT%\.venv\Scripts\activate.bat"

echo Installing build-time dependencies...
pip install -q --disable-pip-version-check -r "%REPO_ROOT%\windows_app\launcher-requirements.txt" pyinstaller
if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
)

if not exist "%REPO_ROOT%\windows_app\vendor\ffprobe.exe" (
    echo Looking for an installed ffprobe.exe to vendor...
    set "FFPROBE_SRC="
    for /f "delims=" %%F in ('where ffprobe 2^>nul') do (
        if not defined FFPROBE_SRC set "FFPROBE_SRC=%%F"
    )
    if not defined FFPROBE_SRC (
        echo.
        echo Could not find ffprobe.exe on PATH.
        echo Install it first: winget install -e --id Gyan.FFmpeg
        echo ^(then reopen this terminal^), or manually copy a static
        echo ffprobe.exe to windows_app\vendor\ffprobe.exe.
        pause
        exit /b 1
    )
    copy /y "!FFPROBE_SRC!" "%REPO_ROOT%\windows_app\vendor\ffprobe.exe" >nul
    echo Vendored ffprobe.exe from !FFPROBE_SRC!
)

echo Regenerating the .exe/window icon from logo\glambotlogo.png...
python "%REPO_ROOT%\windows_app\make_icon.py"
if errorlevel 1 (
    echo Failed to build the icon.
    pause
    exit /b 1
)

echo Running PyInstaller...
pyinstaller "%REPO_ROOT%\windows_app\glambot.spec" ^
    --distpath "%REPO_ROOT%\windows_app\dist" ^
    --workpath "%REPO_ROOT%\windows_app\build" ^
    --noconfirm
if errorlevel 1 (
    echo PyInstaller build failed.
    pause
    exit /b 1
)

where iscc >nul 2>nul
if errorlevel 1 (
    echo.
    echo Inno Setup ^(iscc^) not found on PATH.
    echo Install it: winget install -e --id JRSoftware.InnoSetup
    echo ^(then reopen this terminal^)
    pause
    exit /b 1
)

echo Running Inno Setup...
iscc "%REPO_ROOT%\windows_app\installer.iss" "/DSourceDataDir=%REPO_ROOT%" "/DMyAppVersion=%APP_VERSION%"
if errorlevel 1 (
    echo Inno Setup build failed.
    pause
    exit /b 1
)

echo.
echo Done: windows_app\GlambotSetup.exe
pause
