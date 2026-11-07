# Fix for Tesseract download failure (HTTP 403)

The previous build attempted to download the Tesseract installer from the UB Mannheim mirror, which returned HTTP 403 (Forbidden) on the user's computer.

This version changes the installer download to the Tesseract project's GitHub Release asset URL for the same Windows x64 installer version. Tesseract's documentation identifies UB Mannheim as the Windows installer source; the installer asset is also mirrored as a GitHub release download.

## Steps
1. Extract this ZIP to a new folder.
2. Run `build_windows_installer.bat`.
3. The first step should now download from GitHub instead of the Mannheim host.

If it fails again, send the error screenshot. This code change has not been run on the user's Windows machine, so we still need to confirm the download works from their network.
