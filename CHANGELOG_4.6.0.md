# InvoiceFlow 4.6.0 — Professional workspace redesign

## Visual refresh
- Rebuilt the workspace navigation as a premium finance-operations rail.
- Added an editorial workspace hero with a clear workflow narrative.
- Reworked metric cards, register toolbar, table headers, row hierarchy and action area.
- Added supplier monograms and clearer source-document metadata to register rows.
- Refined side-by-side review, source preview, field confidence and verification surfaces.
- Updated dark mode, responsive behavior and reduced-motion support.

## Workflow preservation
Existing import, folder import, search, filtering, sorting, column visibility, row selection, review, copy, delete, Excel export, backup, theme, compact mode, original previews, OCR field confidence, manual verification, vendor aliases and update controls are retained.

## Build automation
The Windows installer workflow now runs on pushes to `main` for test artifacts. Version-tag pushes still build and attach the installer to a GitHub Release.

## Validation note
The Windows installer must be built by GitHub Actions and the actual PyWebView desktop shell visually checked at common Windows display scales before broad distribution.
