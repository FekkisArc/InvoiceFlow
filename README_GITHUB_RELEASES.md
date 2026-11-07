# InvoiceFlow — source and automatic Windows releases

Repository: https://github.com/FekkisArc/InvoiceFlow

## Repository contents
The repository contains the application source and build configuration. `.gitignore` excludes local build outputs and private invoice/database/export files. Never commit real invoices, local SQLite databases, backups, customer details, passwords, or API keys.

## Build automation
Open **Actions → Build and publish InvoiceFlow Windows installer → Run workflow** to build a Windows installer without publishing a release. The generated `.exe` is available under the workflow run's Artifacts section for validation.

When a `v*` tag is pushed, the same workflow builds the installer, stores a workflow artifact, and attaches the `.exe` to a GitHub Release automatically. The release asset name must be exactly `InvoiceFlow_Setup_<version>.exe`, matching the app's updater.

## First publication / safety
Do not push a release tag until a manual Actions build succeeds and the generated installer has been tested on Windows. The current application still needs a working startup/UI check on a clean machine; do not distribute a release until that check passes.

## In-app updates
The updater checks the latest public release in `FekkisArc/InvoiceFlow`. It downloads and runs the setup asset; end users do not need to download the source ZIP. The updater is still a prototype and must be tested end-to-end before relying on it for production updates.
