# InvoiceFlow 5.1.0 — Windows installer build

This project builds a Windows 11 x64 setup wizard named `InvoiceFlow_Setup_5.1.0.exe`.

## What the installer includes
- InvoiceFlow desktop app packaged with PyInstaller (the target PC does not need Python installed).
- Tesseract OCR engine, installed silently by the setup wizard.
- Romanian (`ron`) and English (`eng`) OCR language data.
- Start Menu shortcut, optional desktop shortcut, and Windows uninstaller.
- Local application data under `%LOCALAPPDATA%\\InvoiceOCR` is intentionally preserved by uninstall and upgrades.

## Build requirements
1. Windows 11 x64 build machine.
2. Python 3.15 x64 and the `py` launcher.
3. Inno Setup 6 (`ISCC.exe` on PATH or in a standard installation directory).
4. Internet access to retrieve Tesseract and language-data build assets.

## Build
Run `build_windows_installer.bat`. The script checks Python 3.15, downloads the pinned Tesseract 5.5.3 Windows installer and verifies its SHA-256 before embedding it, downloads the `ron`/`eng` language files, creates `.build\\venv`, installs `requirements_installer.txt`, packages `web_app.py` and `webui.html`, and compiles `installer\\InvoiceFlow.iss`.

Expected output:

`installer-output\\InvoiceFlow_Setup_5.1.0.exe`

The same build recipe runs in `.github/workflows/windows-installer.yml`. Workflow artifacts are uploaded for both `main` builds and version-tag builds. A tag push (for example `v5.1.0`) also publishes the installer to the GitHub Release.

## Installation and data safety
Administrator permission is required because the Tesseract engine is installed under Program Files. Invoice database, original documents, exports and settings remain under `%LOCALAPPDATA%\\InvoiceOCR`; the setup does not intentionally delete them during upgrades or uninstall.

## Updater
The updater checks the configured public GitHub repository's latest non-draft, non-prerelease release. It requires an asset named exactly `InvoiceFlow_Setup_<version>.exe`, checks that the URL belongs to the configured repository, checks file size, and verifies the GitHub-published SHA-256 digest before starting the installer. Releases without a SHA-256 digest are rejected.

Default repository configuration is in `update_config.json`; a user-specific `%LOCALAPPDATA%\\InvoiceOCR\\update_config.json` takes precedence. Do not configure a repository you do not control.

## Pre-release acceptance test
The installer cannot be launched in a non-Windows environment. Test each build on a clean Windows 11 x64 machine. Verify first launch, `ron` and `eng` Tesseract availability, import of searchable and scanned PDFs, mixed PDFs, multi-part records, duplicate review, edit/re-verification, Excel export, backup/restore, updater behavior and uninstall preservation. Do not distribute solely because the source tests pass.


## OCR binary pin

The Windows package currently pins the Tesseract 5.5.3 x64 installer asset and verifies its published SHA-256 (`BEE9E3434BD94FD65387D9BE28CD467A41F61B1275383B55B0F59A1331270AE4`) during build. This avoids silently embedding a replaced or corrupted binary. The upstream Tesseract source has a newer 5.5.4 release, but this project has not substituted an unverified Windows installer; update the pinned binary only when a suitable Windows distribution and checksum can be independently verified.

### Existing Tesseract installations

InvoiceFlow setup detects an existing Tesseract executable in the standard Program Files locations, the per-user Programs location, any directory in `PATH`, the `TESSERACT_CMD` override, and per-user/per-machine Tesseract registry install directories. When found, the bundled Tesseract setup is not even extracted to the temporary directory and is not run. If Tesseract is not detected, setup installs the bundled pinned version with Inno Setup's `/VERYSILENT /NORESTART /SP-` switches. Message-box suppression is deliberately omitted so a detection miss cannot silently auto-approve the nested installer's uninstall confirmation.
