@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo App environment missing. Run install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,15) else 1)" >nul 2>nul
if errorlevel 1 (
  echo This app environment is not using Python 3.15.
  echo Run install.bat to recreate it for Python 3.15.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import pytesseract, fitz, PIL, openpyxl, webview" >nul 2>nul
if errorlevel 1 (
  echo Required Python packages are missing or damaged.
  echo Please run install.bat to repair the app environment.
  pause
  exit /b 1
)
".venv\Scripts\pythonw.exe" web_app.py
if errorlevel 1 (
  echo App exited with an error. See %LOCALAPPDATA%\InvoiceOCR\app.log
  pause
)
