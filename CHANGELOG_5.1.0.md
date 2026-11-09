# InvoiceFlow 5.1.0 — Professional workspace expansion

This is the current consolidated source snapshot. It builds on the 5.0.0 OCR, data-integrity, import, review, backup and recovery work.

## Workspace and dashboard
- Added a dashboard with date-period metrics and financial analytics, including comparison against the preceding period.
- Added clickable supplier/category/month breakdowns that navigate to filtered invoice results.
- Added dashboard card customization and saved report profiles.
- Added a full-screen invoice review mode and a more complete review dialog.
- Refined the invoice register with sortable optional columns for due date, currency, category, project and payment status.
- Added saved views, advanced filters and bulk updates.

## Supplier and accounting management
- Added supplier profiles, supplier invoice history and supplier-specific OCR rules.
- Added supplier alias management and logo references.
- Added expense categories and keyword/supplier categorization rules.
- Added project tracking, tags, invoice notes and supplementary attachments.
- Added budgets by period, currency and optional category.
- Added payment status and invoice workflow statuses.

## Automation and document lifecycle
- Added watched-folder configuration and scanning for new local documents.
- Added scheduled local backup and Excel export jobs, with automatic backup retention settings.
- Added automatic backup settings and a recycle bin with restore and permanent-delete operations.
- Added an activity log for invoice operations.
- Added a Recognition Studio view for reviewing extracted candidates.
- Added structured UBL XML invoice parsing alongside PDF/image OCR.

## Reporting and exports
- Added filtered analytics by date range, status, currency and category.
- Added CSV exports and expanded Excel workbooks with summary and analysis sheets.
- Added reusable named export templates and saved report profiles.
- Kept currencies separate rather than combining amounts across currencies.

## Settings and safeguards
- Added application settings for appearance, security/passcode and local automation preferences.
- Added database support for the workspace features and regression coverage for the new data paths.
- Continued to preserve local-only processing for invoice documents, database and exports.

## Validation performed for this source snapshot
- 84 automated tests pass in the current environment.
- Python compilation passes for `main.py`, `web_app.py` and `workspace_features.py`.
- The inline JavaScript in `webui.html` passes `node --check`.
- Chromium smoke tests exercised the major workspace navigation, supplier history, dashboard editor and invoice review UI.

## Still required before calling this a finished Windows release
- Build the 5.1.0 installer on Windows through GitHub Actions.
- Install and run the packaged app on Windows 11, including the Tesseract detection/skip path.
- Acceptance-test import, review, recycle/restore, watched folders, schedules, Excel/CSV export, update and upgrade workflows on Windows.
- Verify the UI at the user's actual Windows display scaling and screen size.

## Scope notes
- The source includes local UBL XML parsing; it is not a direct authenticated ANAF/e-Factura portal integration.
- Watched folders and scheduled jobs operate locally while InvoiceFlow is running; they are not a Windows service.
- There is no cloud sync, bank feed, payment processing or accounting-platform integration in this release.
- OCR confidence remains a review aid, not a guarantee of accounting correctness. Verify supplier, invoice number, invoice date, payable amount and currency against the source document.
