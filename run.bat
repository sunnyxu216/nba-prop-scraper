@echo off
REM Creates a local virtual environment on first run, then starts the app.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment in .venv ...
  python -m venv .venv || (echo Python 3.10+ is required. & pause & exit /b 1)
  .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
)
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe app.py %*
