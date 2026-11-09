# InvoiceFlow 5.1.0 — Professional local invoice workspace

InvoiceFlow is a Windows 11 x64 desktop application for importing invoices and receipts, extracting their key fields with OCR, checking every result against the original documents, and exporting a local Excel tracker. It is designed for Romanian and English documents and keeps invoice content on the user's computer.

## What's new in 5.1.0

### Reliable data and accounting safeguards
- Database migrations run before indexes are created, with legacy-column migration and a schema version.
- Invoice dates are normalized and sorted as dates rather than compared as text.
- Unknown currency stays unknown; it is never silently defaulted to RON.
- Confirmation requires all five key values (supplier, invoice number, invoice date, payable amount and currency) and explicit verification of each field.
- Editing a verified value invalidates that field's verification.
- Exact and likely business duplicates are flagged and cannot silently inflate confirmed totals. A user can explicitly resolve a duplicate after review.
- Excel totals exclude duplicate-flagged records and records with unknown currency. Totals are grouped by currency rather than mixing currencies.

### OCR and document intake
- PDF text is evaluated page by page, so searchable pages and scanned pages can coexist in one PDF.
- Scanned PDF pages are rendered with their page transforms applied before OCR, avoiding inverted or incorrectly oriented embedded images.
- OCR uses a bounded, selective retry strategy and density-based crop to reduce repeated work on blank margins and scanner dust.
- Romanian invoice labels such as `Serie/Numar`, receipt fiscal-number patterns, and common payable-total labels receive more specific extraction handling.
- Due dates are not treated as invoice issue dates; VAT/subtotals are less likely to be mistaken for payable totals.
- ZIP archives inside selected folders are collected. ZIP extraction checks path traversal, symlinks, file count, individual/aggregate size and suspicious compression ratios.
- Split-document grouping leaves ambiguous sets unmerged rather than silently dropping repeated part numbers.

### Review, previews and recovery
- Multi-source invoices retain their original source paths.
- Original-document preview can load additional pages in batches, rather than stopping permanently after the first three pages.
- Duplicate warnings are visible in the review workflow and require an explicit user decision to clear.
- Backup uses SQLite's online backup API and performs an integrity check.
- Restore validates the selected backup before touching the live database and first creates a safety backup of the current database.
- Restoring merges preserved originals without deleting files that already exist locally.

### Interface and release workflow
- Professional 4.6 workspace redesign retained and refined.
- Working invoice-table sorting, including European decimal and date formats.
- Select-all state follows visible selections; clipboard fallback is provided.
- Improved currency-unknown and duplicate states, review notices, and backup/restore actions.
- GitHub Actions validates Python, tests, inline JavaScript, and builds the Windows installer on `main` pushes and version tags.
- Installer and updater use the versioned `InvoiceFlow_Setup_<version>.exe` release asset and require the published SHA-256 digest for update installation.

## Install the Windows application

The intended end-user distribution is the Windows installer (`InvoiceFlow_Setup_5.1.0.exe`). It packages Python and application dependencies, installs the Tesseract OCR engine and Romanian/English language data, and creates the Windows shortcuts. End users do not need to install Python separately.

The installer is built by GitHub Actions on Windows. A source archive alone is not the installer. Before distributing a new build, test the generated setup on a clean Windows 11 x64 machine.

## Run from source (developer setup)

Requirements: Windows 11 x64, Python 3.15 x64, Tesseract OCR with `ron` and `eng` language data, and Microsoft Edge WebView2.

1. Create and activate a virtual environment.
2. Install dependencies: `python -m pip install -r requirements.txt`.
3. Run `python main.py` only for the legacy Tk interface, or `python web_app.py` for the packaged WebView interface used by the installer.
4. On Windows, `install.bat` and `run_app.bat` provide convenience wrappers.

To build the Windows installer, install Inno Setup 6 and use `build_windows_installer.bat` on a Windows x64 build machine. The script downloads the Tesseract installer and `ron`/`eng` language data, builds an isolated environment, packages the WebView app, and compiles the setup wizard.

## Local data and privacy

The active database and local application data are stored under:

`%LOCALAPPDATA%\InvoiceOCR`

This includes `invoices.sqlite3`, `Originals`, `Exports`, logs, update configuration and backups. OCR, extraction, review and export run locally. Invoice originals, the local database, exported financial reports and test documents must never be committed to the public source repository or included in a source archive.

Backups may contain confidential financial data. Store them securely and share them only with authorized people. Uninstall intentionally preserves the local invoice data folder.

## Accounting and verification notes

OCR confidence is a signal about the recognition result, not proof that a supplier, invoice number, date or total is correct. InvoiceFlow deliberately leaves uncertain currency blank and requires manual field verification before confirmation. Check the extracted payable amount and currency against the original invoice before relying on exported totals. Different currencies are kept separate; the workbook does not convert them.

Duplicate detection is a warning system, not an accounting judgment. A user must inspect a flagged pair and explicitly clear a duplicate warning when the documents are genuinely distinct. Confirmation and workbook totals exclude unresolved duplicates.

## Tests

Run the automated regression suite from the project root:

```bash
python -m pytest -q
python -m py_compile main.py web_app.py
```

To validate the inline JavaScript, extract the contents of the `<script>` block in `webui.html` and run `node --check` on the extracted file. The GitHub Actions workflow performs these checks before the installer build.

The project test suite includes database migration, date/currency parsing, duplicate protection, verification invalidation, ZIP collection, split-file grouping, mixed-PDF page handling, OCR extraction regressions, workbook totals, backup/restore, and multi-source preview pagination cases.

## Release validation still required on Windows

Automated source tests do not replace a desktop acceptance test. Each release should be installed on a clean Windows 11 x64 machine and checked for: first launch, Tesseract language availability, a searchable PDF, a scanned receipt, a mixed searchable/scanned PDF, a multi-part invoice, duplicate review, manual field verification, Excel export, backup, restore, update installation and uninstall data preservation.

## Deleting invoices and their source files

The register's Delete action removes the selected invoice records and their unshared InvoiceFlow-preserved copies. A confirmation dialog also offers **Also delete original source files**, checked by default. When checked, original PDFs, images and ZIP archives from their source locations are permanently deleted too. Uncheck it to keep those original files while removing the app records and preserved copies. Files still referenced by invoices that were not selected are retained. Symlinks are skipped. Records created before original-input tracking was introduced may not have a separately tracked original file. New ZIP imports track the source archive; if several invoices came from one ZIP, the archive is kept until the last linked invoice is deleted. Deletion is irreversible, so create a backup first when in doubt.

## Tesseract installer behavior

The Windows installer checks standard per-machine and per-user locations, `TESSERACT_CMD`, directories in `PATH`, and Tesseract registry install paths. If it finds an existing `tesseract.exe`, it neither extracts nor runs the bundled Tesseract setup. If no executable is found, the bundled setup runs with Inno Setup's `/VERYSILENT`, `/NORESTART` and `/SP-` switches. This prevents an unnecessary Tesseract reinstall during ordinary InvoiceFlow upgrades.
