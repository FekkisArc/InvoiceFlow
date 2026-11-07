# Fix for installed app showing WebView 404 for webui.html

The screenshot shows the local WebView server returning 404 for `webui.html`. PyInstaller 6+ onedir builds commonly put bundled data files under `sys._MEIPASS` (often the `_internal` directory), while the app's old code looked for `webui.html` beside `InvoiceFlow.exe`.

This update adds `RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))` and loads the UI HTML from that resource directory. Source-mode runs still use the project folder. Tesseract data remains under the executable directory as currently packaged by the installer.

## Rebuild steps
1. Extract this ZIP into a fresh folder.
2. Run `build_windows_installer.bat`.
3. Install the newly generated `InvoiceFlow_Setup_4.3.0.exe` over the current installation.
4. Launch InvoiceFlow and confirm the UI loads.

The installer project has not been tested on your Windows machine; this fix is based on the 404 screenshot and PyInstaller 6+ onedir layout.
