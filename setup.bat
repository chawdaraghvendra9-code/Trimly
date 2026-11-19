@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Trimly Clip Studio - Setup

echo ============================================================
echo   Trimly Clip Studio - first-time setup
echo ============================================================
echo.

rem --- 1. Find Python 3.11, 3.12 or 3.13 through the py launcher ---
where py >nul 2>&1
if errorlevel 1 goto no_python

set "PY_CMD="
py -3.12 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.12"
if not defined PY_CMD py -3.13 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.13"
if not defined PY_CMD py -3.11 -c "import sys" >nul 2>&1 && set "PY_CMD=py -3.11"
if not defined PY_CMD goto wrong_python

echo Found Python:
%PY_CMD% --version

rem --- 2. Create the virtual environment and install packages ---
if not exist "venv\Scripts\python.exe" (
  echo Creating virtual environment in venv\ ...
  %PY_CMD% -m venv venv
  if errorlevel 1 goto failed
) else (
  echo Virtual environment already exists, reusing it.
)

set "PY=venv\Scripts\python.exe"

echo.
echo Installing Python packages (this can take a few minutes)...
"%PY%" -m pip install --upgrade pip
if errorlevel 1 goto failed
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto failed

rem --- 3. Check FFmpeg and offer to install it ---
echo.
where ffmpeg >nul 2>&1
if errorlevel 1 (
  where ffprobe >nul 2>&1
  if errorlevel 1 (
    echo FFmpeg was not found. Trimly needs ffmpeg and ffprobe to read videos.
    choice /C YN /M "Install FFmpeg now with winget"
    if errorlevel 2 (
      echo Skipped. Install it later with: winget install Gyan.FFmpeg
    ) else (
      winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
      echo.
      echo FFmpeg installed. Close this window and open a new one so PATH updates.
    )
  )
)

rem --- 4. Optional OpenRouter key -> .env ---
echo.
echo AI highlight suggestions are optional.
echo Paste an OpenRouter API key, or just press Enter to skip.
set "LLM_KEY="
set /p "LLM_KEY=OpenRouter API key: "
"%PY%" scripts\setup_env.py "%LLM_KEY%"
if errorlevel 1 goto failed

echo.
echo ============================================================
echo   Setup complete.
echo   Double-click run.bat to start Trimly Clip Studio.
echo ============================================================
echo.
pause
exit /b 0

:no_python
echo.
echo The Python launcher "py" was not found.
echo Install Python 3.12 from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during installation.
echo.
choice /C YN /M "Open the Python download page now"
if not errorlevel 2 start "" https://www.python.org/downloads/
echo After installing, close this window and run setup.bat again.
pause
exit /b 1

:wrong_python
echo.
echo Python 3.11, 3.12 or 3.13 was not found through the py launcher.
echo.
choice /C YN /M "Install Python 3.12 with winget now"
if not errorlevel 2 (
  winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
  echo Close this window, then run setup.bat again so the py launcher is available.
  pause
  exit /b 1
)
echo Install Python 3.11 or 3.12 from https://www.python.org/downloads/
pause
exit /b 1

:failed
echo.
echo Setup failed. Read the message above, then run setup.bat again.
pause
exit /b 1
