@echo off
setlocal
cd /d "%~dp0"
title SRT Studio - Setup
echo.
echo SRT Studio for Windows
echo The first setup needs internet. Run Start-SRT-Studio.cmd after setup.
echo.

if exist ".venv\Scripts\python.exe" goto choose_device
where py >nul 2>&1
if errorlevel 1 goto try_python
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3.12 -m venv .venv
  goto check_env
)
py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3.11 -m venv .venv
  goto check_env
)

:try_python
python -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) else 1)" >nul 2>&1
if errorlevel 1 (
  echo Install 64-bit Python 3.11 or 3.12 from https://www.python.org/downloads/windows/
  echo Enable the Python launcher or Add Python to PATH, then run this setup again.
  goto fail
)
python -m venv .venv

:check_env
if not exist ".venv\Scripts\python.exe" goto fail

:choose_device
set "VENV_PY=%CD%\.venv\Scripts\python.exe"
choice /C GC /N /M "[G] NVIDIA GPU (CUDA 12.8)  [C] CPU only: "
if errorlevel 2 goto cpu

:gpu
"%VENV_PY%" -m pip install "torch==2.11.0+cu128" --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 goto fail
goto install_studio

:cpu
"%VENV_PY%" -m pip install "torch==2.11.0+cpu" --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 goto fail

:install_studio
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo.
"%VENV_PY%" -c "import torch; print('CUDA available:', torch.cuda.is_available())"
if errorlevel 1 goto fail
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo.
  echo FFmpeg was not found on PATH. Get a Windows build from https://ffmpeg.org/download.html
  echo Or run in a new Windows Terminal: winget install --id Gyan.FFmpeg -e
  echo Open a new Explorer or terminal session after adding FFmpeg to PATH.
)
echo.
choice /C YN /N /M "Prepare a Whisper model for offline use now? [Y/N]: "
if errorlevel 2 goto done
call "%~dp0Prepare-Offline.cmd"
if errorlevel 1 goto fail

:done
echo.
echo Setup finished. Open Start-SRT-Studio.cmd to launch the workbench.
pause
exit /b 0

:fail
echo.
echo Setup did not complete. Fix the error shown above and rerun this file.
pause
exit /b 1
