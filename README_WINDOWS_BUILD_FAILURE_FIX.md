# InvoiceFlow 5.1.1 — Windows CI and launch diagnostics

This stability revision addresses the latest Windows Actions failures and makes launch failures diagnosable.

## Windows CI fixes
- Use explicit close-on-exit SQLite connections for application database access and backup/restore handles.
- Explicitly close read-only openpyxl workbooks in tests so Windows can remove temporary workbooks.
- Avoid unclosed direct SQLite connections in tests.
- Compare watched-folder paths with `os.path.samefile`, which handles Windows long and 8.3 short path aliases.

## Launch diagnostics
- The windowed WebView application now logs fatal startup exceptions to `%LOCALAPPDATA%\InvoiceOCR\app.log`.
- It displays a startup error dialog with the exception type/message and log location rather than silently exiting.

## Validation performed here
- Python compile check passed.
- 88 automated tests passed after the stabilization changes.
- JavaScript syntax check passed.

The actual Windows installer still needs to be produced by the Windows GitHub Actions runner and tested on the target PC. This source archive is not itself an installer.
