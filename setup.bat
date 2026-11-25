@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python Launcher was not found. Install 64-bit Python 3.10 first.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  py -3.10 -m venv .venv
  if errorlevel 1 (
    echo Could not create the Python 3.10 virtual environment.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo Setup completed. Double-click run.bat to start the local camera console.
pause
exit /b 0

:failed
echo Dependency installation failed. Read the error above and check Python 3.10/network access.
pause
exit /b 1
