# InvoiceFlow 4.2.0

- Added Windows installer build pipeline using PyInstaller + Inno Setup.
- Installer is designed to bundle the Python runtime/dependencies, install Tesseract OCR, and create shortcuts/uninstaller.
- Added bundled Romanian and English Tesseract fast language data support through `TESSDATA_PREFIX`.
- Kept invoice database and originals in Local AppData so app updates/uninstalls don't remove invoice records.
- Added build checks and installer documentation.
- Windows setup and runtime still require real Windows validation.
