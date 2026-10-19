@echo off
REM Builds "NBA Prop Model.exe" in this folder using PyInstaller inside .venv.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt pyinstaller
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --name "NBA Prop Model" ^
  --icon "%~dp0static\icon.ico" --add-data "%~dp0static;static" ^
  --collect-binaries lightgbm --exclude-module sklearn --exclude-module pandas ^
  --distpath "%~dp0." --workpath "%~dp0build\pyinstaller" --specpath "%~dp0build" app.py || (echo. & echo Build FAILED. Close any running NBA Prop Model.exe and try again. & exit /b 1)
echo.
echo Built: "%~dp0NBA Prop Model.exe"
