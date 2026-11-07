# 3.0.0 — InvoiceFlow web workspace

- Replaced the previous Tkinter-based screen with a website-inspired HTML/CSS workspace rendered locally in WebView2.
- Added responsive navigation, summary KPI cards, searchable/filterable invoice table, status pills, OCR signal bars, invoice review modal, and source-opening action.
- Added sortable date/vendor/invoice number/amount/status columns, multi-select, select-all, row copy, and bulk delete.
- Connected the workspace to the existing local SQLite/OCR core through a Python WebView API.
- Added WebView-native file/folder/save dialogs for import, export, and backup.
- Added progress polling for import status.
- Preserved existing invoice DB path and OCR core; the new UI uses the same local database.
