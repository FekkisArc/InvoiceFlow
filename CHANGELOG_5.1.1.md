# InvoiceFlow 5.1.1 — Windows stability and launch diagnostics

- Fixed Windows test failures caused by open SQLite and read-only workbook handles.
- Ensured application, backup and restore SQLite connections close explicitly.
- Corrected watched-folder test path comparisons for Windows long/short path aliases.
- Added fatal startup logging and a visible error dialog for packaged WebView launch failures.
- Kept the 5.1.0 workspace, OCR, review, recycle-bin and reporting features unchanged.

- Added a regression test confirming active application database connections close when their transaction context exits.
- Windows regression suite expanded to 88 passing tests in the local validation environment.
