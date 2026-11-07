# InvoiceFlow 4.0.0 — UI refinement release

## Professional UI refinements
- Refined spacing, typography, surfaces, shadows, color system, card hierarchy, sticky sidebar and sticky table header.
- Added dark mode and compact/comfortable density toggles.
- Added command palette with Ctrl+K and quick access to import, export, backup, refresh, review queue, duplicates, theme, density and columns.
- Added keyboard shortcuts: Ctrl+F search, Ctrl+I import, Ctrl+E Excel export, Escape close overlays.
- Added column customization; column visibility persists locally.
- Dark mode and table density preferences persist locally.
- Improved selection feedback, row focus, sticky table footer, confidence/status pills and responsive layouts.
- Improved selection count and empty state presentation.
- Corrected sidebar initial active state and view-specific table title/filter behavior.
- Retained local WebView/Python architecture and existing database path.

## Validation
- Python syntax checks pass.
- Frontend JavaScript syntax check passes.
- Existing test suite: 9/9 passed.
- Windows WebView visual verification is still required on the target PC.
