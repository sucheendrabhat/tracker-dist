@echo off
echo ==========================================
echo  GATE CSE Tracker — Automated Windows Setup
echo ==========================================

set "APP_DIR=%~dp0tracker_app"

echo [1/3] Checking Python installation...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed or not added to PATH.
    echo Please install Python 3.8+ from https://www.python.org/
    pause
    exit /b 1
)

echo [2/3] Creating virtual environment...
python -m venv "%APP_DIR%\.venv"
call "%APP_DIR%\.venv\Scripts\activate.bat"

echo [3/3] Installing dependencies...
python -m pip install --upgrade pip
pip install -r "%APP_DIR%\requirements.txt"

echo ==========================================
echo Setup complete! Run run.bat to launch.
echo ==========================================
pause
