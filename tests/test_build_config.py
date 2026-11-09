"""Release metadata and build-pipeline regression checks."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_version_is_consistent_across_app_installer_and_metadata():
    expected = "5.1.0"
    assert f'APP_VERSION = "{expected}"' in (ROOT / "main.py").read_text(encoding="utf-8")
    assert f'APP_VERSION = "{expected}"' in (ROOT / "web_app.py").read_text(encoding="utf-8")
    assert f"InvoiceFlow {expected}" in (ROOT / "VERSION.txt").read_text(encoding="utf-8")
    assert f'#define AppVersion "{expected}"' in (ROOT / "installer" / "InvoiceFlow.iss").read_text(encoding="utf-8")
    assert f"InvoiceFlow {expected} - Windows installer build" in (ROOT / "build_windows_installer.bat").read_text(encoding="utf-8")


def test_windows_build_pins_and_verifies_tesseract_installer():
    script = (ROOT / "build_windows_installer.bat").read_text(encoding="utf-8")
    assert "tesseract-ocr-w64-setup-5.5.3.20260724.exe" in script
    assert "BEE9E3434BD94FD65387D9BE28CD467A41F61B1275383B55B0F59A1331270AE4" in script
    assert "Get-FileHash" in script
    assert script.index("Get-FileHash") < script.index("Compiling the Windows setup wizard")


def test_github_workflow_runs_validations_before_installer_build():
    workflow = (ROOT / ".github" / "workflows" / "windows-installer.yml").read_text(encoding="utf-8")
    assert "- main" in workflow
    assert "- 'v*'" in workflow
    assert "python -m pytest -q" in workflow
    assert "node --check" in workflow
    assert workflow.index("Run regression suite") < workflow.index("Build installer")


def test_gitignore_excludes_local_financial_data_and_build_outputs():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("*.sqlite3", "*.pdf", "*.xlsx", "Originals/", ".build/", "installer-output/"):
        # Originals are excluded by the application-level local-data policy;
        # include an explicit rule so copied local data cannot be committed.
        if pattern == "Originals/":
            assert pattern in ignore or "Originals" in ignore
        else:
            assert pattern in ignore


def test_installer_skips_tesseract_setup_when_existing_executable_is_detected():
    script = (ROOT / "installer" / "InvoiceFlow.iss").read_text(encoding="utf-8")
    assert "function IsTesseractInstalled(): Boolean;" in script
    assert "TESSERACT_CMD" in script
    assert "{autopf}\\Tesseract-OCR\\tesseract.exe" in script
    assert "Check: not IsTesseractInstalled" in script
    run_line = next(line for line in script.splitlines() if "tesseract-ocr-setup.exe" in line and "Filename:" in line)
    assert "Check: not IsTesseractInstalled" in run_line
    assert "/VERYSILENT" in run_line
    assert "/NORESTART" in run_line
    assert "/SUPPRESSMSGBOXES" not in run_line, "Do not auto-confirm a nested Tesseract uninstall if detection misses an existing install."
    files_line = next(line for line in script.splitlines() if "Source:" in line and "DestDir: \"{tmp}\"" in line)
    assert "Check: not IsTesseractInstalled" in files_line
    assert "PathValue := GetEnv('PATH')" in script
    assert "HKCU" in script


def test_delete_ui_explains_optional_original_file_cleanup():
    ui = (ROOT / "webui.html").read_text(encoding="utf-8")
    assert 'id="deleteOriginals" type="checkbox" checked' in ui
    assert "Also move original source files into the recycle bin" in ui
    assert "Files shared with invoices you did not select are kept" in ui
    assert "moved to Recycle bin" in ui
    assert "Permanently deletion cannot be undone" not in ui
    assert "metric-shortcut" in ui
    assert "verifiedMeter" in ui
    assert "deleteOriginalsHint" in ui
    assert "Keep original PDFs, images and ZIP archives in their current folders" in ui
    assert "Keep unknown values at the bottom" in ui


def test_runtime_tesseract_lookup_supports_registry_installs():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "import winreg" in source
    assert "TESSERACT_CMD" in source
    assert "Tesseract-OCR_is1" in source


def test_web_import_tracks_external_original_input_paths():
    source = (ROOT / "web_app.py").read_text(encoding="utf-8")
    assert "input_paths=[]" in source
    assert '"source_input_path":"\\n".join(dict.fromkeys(input_paths))' in source
