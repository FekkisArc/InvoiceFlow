# Windows CI rebuild fix — InvoiceFlow 5.1.0

The first GitHub Actions run reached the Windows test stage but did not reach Inno Setup. The failures were caused by SQLite connections not being closed by `with sqlite3.Connection`: Python's built-in SQLite context manager commits or rolls back but leaves the handle open. Windows then refused to remove temporary test folders (`PermissionError: [WinError 32]`).

This update changes `main.db_connect()` to a real close-on-exit context manager and uses the same helper for backup/restore SQLite handles. That is also a production fix: it prevents long-lived database file handles and makes backup/restore and application shutdown more predictable.

A watched-folder test also compared path strings with case-sensitive equality. Windows paths are case-insensitive, so the test now compares case-folded paths.

Validation in the available environment:
- `python -m py_compile main.py web_app.py workspace_features.py`
- `python -m pytest -q` — 85 passed
- inline JavaScript `node --check` — passed

The Windows GitHub Actions runner must still execute the regression suite and build the actual `.exe`. This package is source code, not a Windows installer.
