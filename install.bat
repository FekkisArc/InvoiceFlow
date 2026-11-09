@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "LOG=%~dp0install_log.txt"
echo ============================================================ > "%LOG%"
echo Invoice OCR setup log - %DATE% %TIME% >> "%LOG%"
echo ============================================================ >> "%LOG%"
echo.
echo InvoiceFlow 5.1.1 setup - Python 3.15
echo Log file: "%LOG%"
echo.
echo [1/6] Checking Python Launcher...
where py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: Windows Python Launcher 'py' was not found.
  echo Install Python 3.15 x64 and select "Install launcher for all users".
  echo ERROR: py launcher missing >> "%LOG%"
  goto :end_error
)
echo [2/6] Checking Python 3.15...
py -3.15 --version >> "%LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: Python 3.15 was not found by the launcher.
  echo Run: py -0p
  echo Python 3.15 missing >> "%LOG%"
  goto :end_error
)
echo [3/6] Checking or creating virtual environment...
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,15) else 1)" >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo Removing old app environment...
    rmdir /s /q ".venv" >> "%LOG%" 2>&1
    if errorlevel 1 (
      echo ERROR: Could not remove old .venv. Close app windows and try again.
      goto :end_error
    )
  )
)
if not exist ".venv\Scripts\python.exe" (
  py -3.15 -m venv .venv >> "%LOG%" 2>&1
  if errorlevel 1 (
    echo ERROR: Could not create the virtual environment. See install_log.txt.
    goto :end_error
  )
)
echo [4/6] Updating pip...
".venv\Scripts\python.exe" -m ensurepip --upgrade >> "%LOG%" 2>&1
if errorlevel 1 goto :pip_error
".venv\Scripts\python.exe" -m pip install --upgrade pip >> "%LOG%" 2>&1
if errorlevel 1 goto :pip_error
echo [5/6] Installing app packages (this may take several minutes)...
".venv\Scripts\python.exe" -m pip install --upgrade -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 goto :pip_error
echo [6/6] Verifying packages...
".venv\Scripts\python.exe" -c "import sys, fitz, PIL, pytesseract, openpyxl, webview; print('Python:', sys.version); print('Pillow:', PIL.__version__)" >> "%LOG%" 2>&1
if errorlevel 1 goto :pip_error
echo.
echo SUCCESS: Python 3.15 and required packages are installed.
echo Install Tesseract OCR with Romanian and English language packs if not already installed.
echo Then run run_app.bat to open the new web-style InvoiceFlow workspace.
echo SUCCESS >> "%LOG%"
goto :end_ok

:pip_error
echo.
echo ERROR: A Python package installation or verification step failed.
echo Please send me install_log.txt from this folder.
echo ERROR during pip/install verification >> "%LOG%"
goto :end_error

:end_error
echo.
echo SETUP FAILED. Please send me this file:
echo "%LOG%"
echo.
echo Press any key to close this window...
pause >nul
exit /b 1

:end_ok
echo.
echo Setup complete. Press any key to close this window...
pause >nul
exit /b 0
