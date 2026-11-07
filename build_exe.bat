@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pyinstaller.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,15) else 1)" >nul 2>nul
if errorlevel 1 (
  echo The app environment must use Python 3.15. Run install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --name InvoiceFlow --collect-all fitz --collect-all PIL --collect-all pytesseract --collect-all webview --add-data "webui.html;." web_app.py
if errorlevel 1 (
  echo Build failed. Review the error above.
  pause
  exit /b 1
)
echo.
echo Build output: dist\InvoiceFlow\
echo Note: Tesseract OCR still needs to be installed separately on the target PC.
pause
