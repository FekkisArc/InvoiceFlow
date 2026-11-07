# Fix for ISCC.exe not recognized during final installer step

The screenshot shows PyInstaller completed successfully and the failure occurred only at Inno Setup compilation. The prior script stored `ISCC.exe` as a bare name and then invoked it with quotes. This version captures the full path from Windows `where ISCC.exe` first, so the quoted invocation points to a real file path.

## Steps
1. Extract this ZIP into a new folder.
2. Run `build_windows_installer.bat`.
3. The script should show the detected compiler path and continue to the final installer compile step.

If it fails, send the new error. This patch is based on the provided build log; it has not been run on your Windows PC.
