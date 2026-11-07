# Fix for PyInstaller unable to find webui.html

Your build log shows PyInstaller looking for `webui.html` inside `.build\\webui.html`, even though the HTML file is in the project root. This happens because the build command places the generated spec file in `.build` and the data-file path was relative.

The updated build script passes absolute paths for the app's data files (`webui.html` and `update_config.json`) so PyInstaller can find them regardless of the spec file location.

## Steps
1. Extract this ZIP into a fresh folder.
2. Run `build_windows_installer.bat`.
3. If it fails, send the next error screenshot. This patch is based on the error log and has not yet been tested on your Windows PC.
