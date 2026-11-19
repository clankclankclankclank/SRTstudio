@echo off
setlocal
cd /d "%~dp0"
title SRT Studio
if not exist ".venv\Scripts\python.exe" (
  echo Run Install-SRT-Studio.cmd once before starting the app.
  pause
  exit /b 1
)
set "SRT_STUDIO_MODEL_DIR=%CD%\models"
".venv\Scripts\python.exe" srt_studio.py
if errorlevel 1 (
  echo.
  echo SRT Studio closed with an error.
  pause
  exit /b 1
)
