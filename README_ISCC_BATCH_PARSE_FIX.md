This version fixes the batch parser error `Inno was unexpected at this time`.

The prior script printed paths containing parentheses (such as `Program Files (x86)`) inside a parenthesized IF block. Windows CMD can misparse that. This version uses simple IF/GOTO checks without wrapping the path echoes in a block.

Extract this ZIP into a new folder, then run `build_windows_installer.bat` again. If another error occurs, open Command Prompt in the folder and run the script there so the output remains visible.
