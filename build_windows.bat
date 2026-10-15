@echo off
setlocal
cd /d "%~dp0"

py -3.12 -m venv .venv-windows
if errorlevel 1 goto :failed

".venv-windows\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed

".venv-windows\Scripts\python.exe" -m pip install ".[desktop,windows-build]"
if errorlevel 1 goto :failed

".venv-windows\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --windowed --name PredictMMBot --paths . --add-data "predict_mm\web_static;predict_mm\web_static" --collect-submodules uvicorn --collect-submodules predict_sdk desktop_entry.py
if errorlevel 1 goto :failed

echo.
echo Build complete: dist\PredictMMBot\PredictMMBot.exe
echo Keep the entire dist\PredictMMBot folder together when copying it.
exit /b 0

:failed
echo.
echo Build failed. Check that Python 3.12 for Windows is installed and available as py -3.12.
exit /b 1
