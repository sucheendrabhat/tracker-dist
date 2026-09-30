@echo off
set "APP_DIR=%~dp0tracker_app"

if exist "%APP_DIR%\.venv\Scripts\activate.bat" (
    call "%APP_DIR%\.venv\Scripts\activate.bat"
)

python "%APP_DIR%\app.py" %*
