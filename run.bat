@echo off
setlocal
cd /d "%~dp0"
title Trimly Clip Studio

rem --- Make sure setup.bat was run first ---
if not exist "venv\Scripts\python.exe" (
  echo Trimly is not set up yet.
  echo Double-click setup.bat first, then run this file again.
  echo.
  pause
  exit /b 1
)

rem --- First run without setup: start from the example settings ---
if not exist ".env" (
  if exist ".env.example" copy /Y ".env.example" ".env" >nul
)

rem --- FFmpeg is required; warn early with a clear fix ---
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo WARNING: FFmpeg was not found on PATH.
  echo Trimly needs it to read and render videos.
  echo Fix: close this window, run setup.bat, or run: winget install Gyan.FFmpeg
  echo.
  pause
)

echo Starting Trimly Clip Studio at http://127.0.0.1:8000
echo Keep this window open while you work. Press Ctrl+C to stop.
echo.

rem --- Open the browser a few seconds after the server starts ---
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8000'"

"venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo.
echo Server stopped.
pause
