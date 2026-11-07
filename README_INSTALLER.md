# InvoiceFlow 4.2.0 — Windows installer build

This folder contains the source and build recipe for a Windows 11 x64 setup wizard.

## What the setup is designed to install
- InvoiceFlow desktop app, packaged with PyInstaller (no Python installation required on the target PC)
- Tesseract OCR engine from UB Mannheim (installed silently by the setup)
- Romanian (`ron`) and English (`eng`) fast language models bundled with InvoiceFlow
- Start Menu shortcut, optional desktop shortcut, and Windows uninstaller
- Invoice data remains in `%LOCALAPPDATA%\\InvoiceOCR` and is intentionally not deleted by uninstall

## Build requirements (only on the developer/build PC)
1. Windows 11 x64
2. Python 3.15 x64 and the `py` launcher
3. Inno Setup 6 (ISCC.exe on PATH)
4. Internet access for downloads

## Build
Double-click `build_windows_installer.bat`. It downloads the Tesseract setup executable and OCR language data, creates a build-only virtual environment, packages the app, and compiles `installer\\InvoiceFlow.iss`.

Output:
`installer-output\\InvoiceFlow_Setup_4.2.0.exe`

## Installation
Run the resulting setup executable. Administrator permission is needed because the Tesseract engine is installed under Program Files. The setup wizard can create a desktop shortcut. It also installs Tesseract OCR using the UB Mannheim Windows distribution.

## Important testing note
The installer has not been executed on Windows in this environment. Test the generated setup on a clean Windows 11 x64 machine before distributing it. Verify first launch, import a real invoice, OCR language detection (`ron` and `eng`), Excel export, and uninstall behavior.

## OCR engine / data sources
- Tesseract Windows build: UB Mannheim (Tesseract Windows installer)
- Language models: `tessdata_fast` project (Romanian and English)
- PyInstaller bundles Python and the app's Python dependencies into the Windows app folder.

The setup uses the official project/community distribution sources and does not alter the app's invoice database during upgrade or uninstall.


## In-app updates (4.5.0)
InvoiceFlow now has a **Check for updates** action. It checks the latest GitHub Release, downloads its Windows setup `.exe`, closes InvoiceFlow, and launches the setup silently. The setup updates the app while preserving `%LOCALAPPDATA%\\InvoiceOCR`.

Configure the public release repository in:
`%LOCALAPPDATA%\\InvoiceOCR\\update_config.json`

Example:
```json
{
  "github_owner": "YOUR_GITHUB_USERNAME_OR_ORG",
  "github_repo": "YOUR_PUBLIC_REPOSITORY"
}
```

Publish each update as a GitHub Release and attach an asset named exactly:
`InvoiceFlow_Setup_<version>.exe`
For example: `InvoiceFlow_Setup_4.5.0.exe`

**The updater is not active until the project has a real public GitHub release repository and the release assets are published.** Do not use a repository you do not control. Installer updates should be signed before wide distribution.
