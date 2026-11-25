@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat once before starting the camera console.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" webcam_server.py --host 127.0.0.1 --port 8000 --autostart
if errorlevel 1 (
  echo Camera console exited with an error. Read the message above.
  pause
)
