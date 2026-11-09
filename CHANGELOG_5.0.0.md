# InvoiceFlow 5.0.0 — Major reliability and OCR release

## Data integrity and verification
- Run legacy database migrations before creating indexes; migrate older source, notes and timestamp columns and record schema version 6.
- Normalize invoice dates and sort records chronologically.
- Preserve an unknown currency as blank rather than assuming RON.
- Require supplier, invoice number, invoice date, payable amount and currency plus explicit verification before confirmation.
- Invalidate verification for a field whenever its saved value changes.
- Detect exact-file and likely business duplicates; block confirmation until the user explicitly resolves the warning.
- Exclude duplicate-flagged and unknown-currency records from financial totals, and keep totals separated by currency.

## OCR and document handling
- Evaluate selectable text on each PDF page independently; use native text on readable pages and OCR on scanned or garbled pages.
- Render PDF pages with their page transforms before OCR rather than extracting raw embedded image bytes.
- Add density-based receipt cropping, bounded OCR retries, candidate scoring and optional rotation retry for poor pages.
- Improve Romanian `Serie/Numar`, invoice-date and fiscal-receipt identifier recognition.
- Refine payable-total parsing to avoid VAT and subtotal confusion; leave currency blank without nearby evidence.
- Include nested ZIP files during folder imports and validate archive paths, symlinks, file sizes, compression ratios and entry counts.
- Group split invoices only when part numbering is complete and unambiguous; do not silently drop duplicate part numbers.

## Review and recovery
- Load additional original-document preview pages in batches for every source in a merged invoice.
- Expose duplicate warnings and explicit resolution in the review UI.
- Add consistent SQLite backup, database integrity validation and safe restore with a pre-restore safety snapshot.
- Preserve existing originals during restore.

## Interface and release engineering
- Retain the professional 4.6 workspace redesign and improve date/amount sorting, select-all behavior, currency-unknown states, duplicate status, clipboard fallback and restore controls.
- Repair updater configuration fallback and validate the official release URL, asset name, version, size and SHA-256 digest before installation.
- Verify the pinned Tesseract Windows installer SHA-256 during the build.
- Run Python compile checks, regression tests and JavaScript syntax checks in GitHub Actions before producing the Windows installer.
- Trigger Windows builds on `main` pushes and version tags; publish the installer from version tags.

## Validation
- Expanded automated suite to 59 passing tests after UI, delete-source and ZIP-provenance regressions were added.
- Python compilation and inline JavaScript syntax checks passed.
- Real sample checks included a searchable Romanian invoice and a scanned thermal receipt. The scanned receipt extraction remains uncertain for its fiscal identifier and currency and requires user verification.
- The actual Windows WebView shell and installer must still be tested on Windows 11 x64 before broad distribution.


### UI, delete and installer follow-up

- Added a dedicated invoice-deletion dialog with a checked-by-default option to permanently remove original PDFs, images and ZIP archives as well as InvoiceFlow-preserved copies. Unchecking it keeps the original source files. Shared files remain protected, symlinks are skipped, and records created before source tracking are clearly reported.
- Persist original source paths for direct imports and imports from folders. ZIP archive provenance is retained on every extracted invoice; a shared archive is kept until its last linked invoice is deleted.
- Added an audit-log detail identifying whether original inputs were included in each delete action, and a live helper message explaining the effect of the delete checkbox.
- Expanded Tesseract detection to search `PATH` and per-user/per-machine registry keys. When an executable is already present, the installer skips both extraction and execution of the bundled setup. It uses `/VERYSILENT /NORESTART /SP-` only when Tesseract is not detected, avoiding silent confirmation of a nested uninstall prompt.
- Refined desktop table column sizing so the Review action remains visible at common desktop widths, improved dark-mode controls and search surfaces, and retained responsive mobile scrolling.
- Refined the register's empty state and review-screen verification progress, including accessible field labels and live verified-field counts in light and dark themes.
- Tested the web UI in Chromium at desktop and mobile sizes, including review verification and both delete-original choices; no JavaScript runtime errors or page-level mobile horizontal overflow were detected.
