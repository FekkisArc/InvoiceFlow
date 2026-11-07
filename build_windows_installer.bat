@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Build InvoiceFlow Windows Installer
echo ============================================================
echo InvoiceFlow 4.5.0 - Windows installer build
echo ============================================================
echo This build must run on Windows x64.
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python Launcher 'py' is missing.
  echo Install Python 3.15 x64 from python.org, then retry.
  pause
  exit /b 1
)

py -3.15 --version
if errorlevel 1 (
  echo ERROR: Python 3.15 x64 is required for this build.
  pause
  exit /b 1
)

REM Locate Inno Setup compiler. Avoid parenthesized IF blocks here:
REM paths such as Program Files (x86) can break CMD block parsing.
set "ISCC="

REM Capture the full path returned by WHERE instead of storing bare "ISCC.exe".
REM Quoting a bare executable name in this script was causing CMD to fail.
for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do if not defined ISCC set "ISCC=%%I"

if defined ISCC goto :ISCC_FOUND
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if defined ISCC goto :ISCC_FOUND
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if defined ISCC goto :ISCC_FOUND
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if defined ISCC goto :ISCC_FOUND

echo ERROR: Could not locate ISCC.exe.
echo Checked PATH and common Inno Setup 6 installation locations.
echo Please check for ISCC.exe in the usual Inno Setup folders.
echo If installed elsewhere, tell me the exact folder.
pause
exit /b 1

:ISCC_FOUND
echo Found Inno Setup compiler:
echo "%ISCC%"

set "BUILD=.build"
set "TESSDIR=%BUILD%\tessdata"
if not exist "%BUILD%" mkdir "%BUILD%"
if not exist "%TESSDIR%" mkdir "%TESSDIR%"

echo [1/6] Downloading Tesseract Windows x64 installer from GitHub Releases...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/tesseract-ocr-w64-setup-5.5.3.20260724.exe' -OutFile '.build\tesseract-ocr-setup.exe'"
if errorlevel 1 goto :failed

echo [2/6] Downloading Romanian and English OCR language data...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/ron.traineddata' -OutFile '.build\tessdata\ron.traineddata'; Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/eng.traineddata' -OutFile '.build\tessdata\eng.traineddata'"
if errorlevel 1 goto :failed
if not exist "%TESSDIR%\ron.traineddata" goto :failed
if not exist "%TESSDIR%\eng.traineddata" goto :failed

echo [3/6] Creating isolated Python build environment...
if not exist ".build\venv\Scripts\python.exe" py -3.15 -m venv ".build\venv"
if errorlevel 1 goto :failed
".build\venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".build\venv\Scripts\python.exe" -m pip install -r requirements_installer.txt
if errorlevel 1 goto :failed

echo [4/6] Packaging app with PyInstaller...
".build\venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --onedir --name InvoiceFlow --distpath ".build\dist" --workpath ".build\pyi-work" --specpath ".build" --collect-all fitz --collect-all PIL --collect-all pytesseract --collect-all webview --add-data "%CD%\\webui.html;." --add-data "%CD%\\update_config.json;." web_app.py
if errorlevel 1 goto :failed

echo [5/6] Copying bundled language data and OCR installer...
if not exist ".build\dist\InvoiceFlow\tessdata" mkdir ".build\dist\InvoiceFlow\tessdata"
copy /y "%TESSDIR%\ron.traineddata" ".build\dist\InvoiceFlow\tessdata\ron.traineddata" >nul
copy /y "%TESSDIR%\eng.traineddata" ".build\dist\InvoiceFlow\tessdata\eng.traineddata" >nul
copy /y ".build\tesseract-ocr-setup.exe" ".build\dist\InvoiceFlow\tesseract-ocr-setup.exe" >nul
if errorlevel 1 goto :failed

echo [6/6] Compiling the Windows setup wizard...
"%ISCC%" "installer\InvoiceFlow.iss"
if errorlevel 1 goto :failed

echo.
echo SUCCESS. Installer output is in installer-output\InvoiceFlow_Setup_4.5.0.exe
echo You can now test it on a Windows 11 x64 PC.
pause
exit /b 0

:failed
echo.
echo BUILD FAILED. The error above should identify the failed step.
echo Check internet access, Python 3.15 x64, and Inno Setup 6.
pause
exit /b 1
