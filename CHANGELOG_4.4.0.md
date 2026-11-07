# InvoiceFlow 4.4.0 — Review workspace and OCR refinement

## Review workspace
- Reworked invoice review into a source-first side-by-side layout: original document preview on the left, editable extracted fields on the right.
- Shows each source file associated with a merged invoice independently, including multiple PDF/image source files.
- PDF previews display up to three pages per source file; open all originals launches every source file.
- Added expandable raw OCR text for comparison and audit.
- Clarified that Tesseract confidence is text-recognition confidence, not field accuracy.

## OCR refinements
- Expanded selective retry combinations using contrast, autocontrast, sharpening and Tesseract page segmentation modes 3/4/6/11.
- Added rare 90°/270° retry for extremely low-confidence pages.
- Increased PDF rasterization resolution for small text.
- Added 300 DPI hint to Tesseract OCR invocation.
- Expanded Romanian invoice vendor, invoice-number and payable-total label patterns.

## Important
OCR remains probabilistic. Always compare vendor, invoice number, date, total and currency against the source before marking an invoice Confirmed. These changes have passed local Python syntax checks and the existing unit tests, but OCR accuracy and desktop UI behavior still need validation against representative Romanian invoices on Windows.
