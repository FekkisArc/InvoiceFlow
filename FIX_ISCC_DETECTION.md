# Fix for “ISCC.exe not on PATH”

The build script in this project has been updated to search for Inno Setup in these locations automatically:
- PATH
- `C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe`
- `C:\\Program Files\\Inno Setup 6\\ISCC.exe`
- `%LOCALAPPDATA%\\Programs\\Inno Setup 6\\ISCC.exe`

## Use this updated project
1. Extract this ZIP into a new folder (do not merge over your old project).
2. Run `build_windows_installer.bat` again.
3. The script should print `Found Inno Setup compiler: ...` if it finds it.

If it still fails:
1. Press Windows + E.
2. Paste `%ProgramFiles(x86)%\\Inno Setup 6` into the address bar and press Enter.
3. If that folder doesn't exist, try `%LOCALAPPDATA%\\Programs\\Inno Setup 6`.
4. Find `ISCC.exe`. If absent in both, Inno Setup may not have installed correctly. Reinstall from https://jrsoftware.org/isdl.php.
5. If you do find it, tell me the full folder path (or send a screenshot).

This updated batch script has not been run on your Windows computer; it is a code-level fix intended to avoid requiring PATH configuration.
