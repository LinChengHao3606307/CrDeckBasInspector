@echo off
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" app.py
if errorlevel 1 (
    echo.
    echo Failed to start. Run this first: python -m pip install -r requirements.txt
    pause
)
