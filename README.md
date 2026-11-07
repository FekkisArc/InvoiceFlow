# InvoiceFlow 4.0 — Professional invoice workspace

A local Windows invoice OCR and review tool with a website-inspired desktop interface. The web frontend runs in a local WebView window; OCR, database, source files, and Excel export remain local.

## Interface
- Website-like navigation rail, responsive workspace, polished KPI cards, invoice register, status indicators, OCR signal bars, review modal, and local privacy indicator.
- Search, filters, sortable fields, multi-selection, inline review modal, copy selected rows, and bulk delete.
- Customizable columns; show/hide settings persist.
- Dark mode and compact table density; preferences persist locally.
- Command palette (`Ctrl+K`) for common actions.
- Keyboard shortcuts: `Ctrl+F` search, `Ctrl+I` import, `Ctrl+E` export, `Esc` close dialogs.
- Import files/folders, Excel export, and backup.
- Processing progress/status updates.

## Install
1. Install Python 3.15 x64 and Tesseract OCR with Romanian (`ron`) and English (`eng`) language packs.
2. Run `install.bat`.
3. Run `run_app.bat`. Microsoft Edge WebView2 is normally available on Windows 11.
4. Optional executable build: `build_exe.bat`.

## Data
Local database, invoice originals, logs and exports are stored under `%LOCALAPPDATA%\\InvoiceOCR`. Existing invoice records are retained.

## Accounting caution
OCR confidence reflects text recognition quality—not correctness of vendor/date/amount fields. Verify values against the original invoice before confirming or relying on accounting totals.

## Validation status
Python and JavaScript syntax checks pass; the existing 9 core tests pass. Windows-specific WebView layout/runtime validation has not yet been performed.


### Duplicate handling in exports
Duplicate matches are advisory. Only records marked **Confirmed** are included in totals; a confirmed record is not automatically excluded just because it has a duplicate candidate flag. Review duplicate candidates and confirm only the correct invoice copy when appropriate.


## 4.4.0 — Invoice review and OCR improvements
The review dialog is now designed for side-by-side verification: source previews and merged source files on the left, editable fields on the right, plus raw OCR text. The OCR pipeline adds selective image variants and PSM retries, higher PDF render resolution and expanded Romanian label extraction. OCR is still probabilistic; verify values before confirming.
