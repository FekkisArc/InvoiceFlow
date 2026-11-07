# InvoiceFlow 4.5.0 — OCR verification, vendor aliases, and adaptive scan processing

## What's new

### Field-specific confidence
- Extracted invoice fields now have separate evidence scores for vendor, invoice number, date, payable amount, and currency.
- These are heuristic review indicators, not calibrated probabilities and not proof that a field is correct.
- The review UI labels this clearly and shows a confidence bar per field.

### Manual verification
- Added a per-field “Verified against original” checklist in invoice review.
- A record cannot be marked Confirmed unless all five checks have been ticked and required values are present.
- Checked verification states are saved to the local invoice database.

### Vendor abbreviations and OCR-visible logo text
- Reviewers can add comma-separated vendor aliases, abbreviations, or OCR-visible logo text (for example `ALTX` for a canonical supplier name).
- Aliases are saved locally and used for future imports.
- Matching uses normalized exact/substring matching and cautious fuzzy matching for OCR character errors.
- This is not image-based logo recognition: the logo must contain text that Tesseract can read, or the user must enter a visible abbreviation as an alias.

### OCR / rendering efficiency changes
- PDF pages are rendered at a higher effective resolution, with a pixel ceiling to control memory and processing time.
- Adaptive retries are reduced on readable pages; deeper retries are reserved for weaker OCR pages.
- Added a limited segmentation retry and cautious fuzzy alias matching.
- The OCR pipeline remains offline and uses Tesseract with Romanian and English language packs.

## Important limitations
- OCR confidence is a recognition signal, not an accuracy percentage.
- No claim is made that the new settings achieve a specific accuracy rate. Performance must be measured on your own invoice scans.
- Complex logos, tables, faint text, skew, low-resolution scans, and merged documents can still require manual correction.
- Please test on a copy of your invoice dataset first. Do not delete your existing database or originals.

## Build and install
Extract this project to a new folder and run `build_windows_installer.bat`. The build script is designed for Windows x64 with Python 3.15 and Inno Setup 6. The generated installer has not been built or runtime-tested on this Windows environment by the assistant.
