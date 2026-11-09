# Push InvoiceFlow 5.1.0 to GitHub Desktop

1. Download and extract `InvoiceFlow_5.1.0_Source.zip` to a temporary folder.
2. Open GitHub Desktop and choose the existing `FekkisArc/InvoiceFlow` repository.
3. Use **Repository → Show in Explorer** to open the local clone. Confirm the current branch is `main`.
4. If GitHub Desktop already shows unrelated local changes, stop and commit or safely preserve them before copying this release.
5. In File Explorer, turn on **View → Show → Hidden items** so the `.github` folder is visible.
6. Open the extracted source folder. Copy its contents (including `.github`) into the repository root. Merge folders and replace files when asked. Do not copy a `.git` folder; the source archive does not contain one.
7. Return to GitHub Desktop. Review the changed-file list. Make sure no invoice PDFs, databases, Excel exports, Originals, Backups or other private documents appear.
8. Enter commit summary: `Release InvoiceFlow 5.1.0 - professional workspace` and click **Commit to main**.
9. Click **Push origin**.
10. Open the repository's **Actions** tab. The `Build and publish InvoiceFlow Windows installer` workflow should start for the `main` push. Wait for it to finish and check that it is green.
11. Open the successful run and download the `InvoiceFlow-Windows-Installer` artifact. It contains `InvoiceFlow_Setup_5.1.0.exe`.
12. Only after the main-branch build succeeds, create and push the release tag from Git Bash opened in the repository folder:

    ```bash
    git tag -a v5.1.0 -m "InvoiceFlow 5.1.0"
    git push origin v5.1.0
    ```

    The tag triggers the workflow again and attaches the installer to the GitHub Release.

## Important

- The ZIP is a source package, not a Windows installer.
- Do not upload private invoices, database files, backups or exported reports to GitHub.
- Keep your existing local `%LOCALAPPDATA%\\InvoiceOCR` data folder. Do not delete it during this source update.
- If the Windows Actions build fails, do not create the release tag yet. Open the failed run and inspect the build logs first.
