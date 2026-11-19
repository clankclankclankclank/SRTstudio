@echo off
setlocal
cd /d "%~dp0"
title SRT Studio - Prepare offline model
if not exist ".venv\Scripts\python.exe" (
  echo Run Install-SRT-Studio.cmd first.
  pause
  exit /b 1
)
set "SRT_STUDIO_MODEL_DIR=%CD%\models"
echo.
echo This downloads and validates one model, then sets it as the desktop default.
echo Turbo is recommended for NVIDIA GPU use. Small is lighter for CPU use.
choice /C TS /N /M "[T] Turbo (about 1.6 GB)  [S] Small (about 0.5 GB): "
if errorlevel 2 goto small
set "MODEL=turbo"
goto get_model

:small
set "MODEL=small"

:get_model
".venv\Scripts\python.exe" prepare_offline.py "%MODEL%" --select
if errorlevel 1 (
  pause
  exit /b 1
)
echo.
echo The cached model is ready. You can now transcribe with that model offline.
pause
exit /b 0
