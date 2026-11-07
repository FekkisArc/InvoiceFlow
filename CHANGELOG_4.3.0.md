# InvoiceFlow 4.3.0

- Added an in-app **Check for updates** action.
- Update check uses GitHub Releases and downloads the published Windows setup directly from within InvoiceFlow.
- The updater runs the setup wizard in silent mode and closes the app first; invoice database and originals remain in Local AppData.
- Added update-channel configuration in `%LOCALAPPDATA%\\InvoiceOCR\\update_config.json`.
- Update channel must be configured with the public GitHub repository before the feature can work.
- Update installer builds are still Windows-only and must be tested on Windows before distribution.
