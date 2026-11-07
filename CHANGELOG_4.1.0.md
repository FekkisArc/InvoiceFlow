# InvoiceFlow 4.1.0

## Accounting safety and setup diagnostics
- Confirmed invoices are included in Excel totals based on the user's explicit status, even if a duplicate candidate flag remains. Duplicate detection is advisory and no longer silently suppresses a confirmed row from totals.
- Excel summary includes a reminder to review duplicate candidates and confirm only one copy when appropriate.
- Import now checks Tesseract language data before processing and logs a clear warning when Romanian (`ron`) or English (`eng`) data is missing.
- Improved the missing-Tesseract error message.

## Important
- OCR-extracted values still require verification against the source invoice before confirmation.
- This is a Python desktop package, not a self-contained EXE. Tesseract OCR and its language data must be installed separately.
- Windows visual/runtime validation has not been performed in this environment.
