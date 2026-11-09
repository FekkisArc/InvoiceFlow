"""Backend tests for multi-source and incremental original-document previews."""
import importlib
import importlib.util
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The desktop shell dependency is not needed to test its API methods. Supply a
# minimal module only on environments where PyWebView is not installed (for
# example, headless Linux test runners).
if importlib.util.find_spec("webview") is None:
    stub = types.ModuleType("webview")
    stub.SAVE_DIALOG = 1
    stub.FOLDER_DIALOG = 2
    sys.modules.setdefault("webview", stub)

web_app = importlib.import_module("web_app")
core = web_app.core


class PreviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = (core.BASE, core.DB, core.ORIGINALS, core.EXPORTS, core.LOG)
        core.BASE = self.root / "appdata"
        core.DB = core.BASE / "invoices.sqlite3"
        core.ORIGINALS = core.BASE / "Originals"
        core.EXPORTS = core.BASE / "Exports"
        core.LOG = core.BASE / "app.log"
        core.init_db()
        self.api = web_app.API()

    def tearDown(self):
        core.BASE, core.DB, core.ORIGINALS, core.EXPORTS, core.LOG = self.old
        self.temp.cleanup()

    def make_pdf(self, name, count):
        import fitz
        path = self.root / name
        doc = fitz.open()
        for index in range(count):
            page = doc.new_page()
            page.insert_text((72, 72), f"Invoice source {name} page {index + 1}")
        doc.save(path)
        doc.close()
        return path

    def add_invoice(self, paths):
        return core.insert_invoice({
            "vendor": "Preview Supplier", "invoice_number": "PV-001",
            "invoice_date": "06/11/2026", "amount": "100,00", "currency": "RON",
            "status": "Needs review", "source_name": ", ".join(p.name for p in paths),
            "source_path": "\n".join(str(p) for p in paths),
            "file_hash": "preview-test-" + str(len(core.records())),
        })[0]

    def test_preview_includes_every_source_and_loads_remaining_pages(self):
        first = self.make_pdf("part-a.pdf", 5)
        second = self.make_pdf("part-b.pdf", 2)
        invoice_id = self.add_invoice([first, second])

        result = self.api.get_original_preview(invoice_id)
        self.assertEqual(len(result["sources"]), 2)
        self.assertEqual([p["page"] for p in result["sources"][0]["pages"]], [1, 2, 3])
        self.assertEqual(result["sources"][0]["next_page"], 3)
        self.assertEqual([p["page"] for p in result["sources"][1]["pages"]], [1, 2])
        self.assertIsNone(result["sources"][1]["next_page"])

        more = self.api.get_original_preview_more(invoice_id, 0, 3)
        self.assertEqual([p["page"] for p in more["pages"]], [4, 5])
        self.assertIsNone(more["next_page"])
        self.assertEqual(more["page_count"], 5)

    def test_missing_original_is_reported_instead_of_silently_hidden(self):
        missing = self.root / "missing.pdf"
        invoice_id = self.add_invoice([missing])
        result = self.api.get_original_preview(invoice_id)
        self.assertEqual(len(result["sources"]), 1)
        self.assertFalse(result["sources"][0]["exists"])
        self.assertIn("missing", result["sources"][0]["error"].lower())

    def test_web_import_tracks_the_user_selected_original_file_for_deletion(self):
        original = self.make_pdf("direct-import.pdf", 1)
        fake_ocr = "ACME STORE SRL\nInvoice number: AC-2026-123\nInvoice date: 06.11.2026\nTotal payable: 123,45 RON"
        with patch.object(core, "find_tesseract", return_value="/fake/tesseract.exe"), \
             patch.object(core, "run_ocr", return_value=(fake_ocr, 0.92)):
            self.api._worker([str(original)])
        rows = core.records()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source_input_path"], str(original.absolute()))
        preserved = Path(row["source_path"])
        self.assertTrue(preserved.exists())
        result = self.api.delete_invoices([row["id"]], True)
        self.assertEqual(result["deleted_original_files"], 1)
        self.assertEqual(result["deleted_managed_copies"], 1)
        self.assertFalse(original.exists())
        self.assertFalse(preserved.exists())

    def test_web_import_tracks_zip_archive_and_deletes_it_only_after_last_linked_invoice(self):
        import zipfile
        archive = self.root / "supplier-batch.zip"
        import fitz
        with zipfile.ZipFile(archive, "w") as bundle:
            for name in ("invoice-a.pdf", "invoice-b.pdf"):
                doc = fitz.open()
                page = doc.new_page()
                page.insert_text((72, 72), f"{name} source")
                bundle.writestr(name, doc.tobytes())
                doc.close()

        def fake_ocr(path, _tesseract, progress=None):
            number = Path(path).stem.upper()
            return (f"ACME STORE SRL\nInvoice number: {number}\nInvoice date: 06.11.2026\nTotal payable: 123,45 RON", 0.91)

        with patch.object(core, "find_tesseract", return_value="/fake/tesseract.exe"), \
             patch.object(core, "run_ocr", side_effect=fake_ocr):
            self.api._worker([str(archive)])

        rows = core.records()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["source_input_path"] == str(archive.absolute()) for row in rows))
        first, second = rows[0]["id"], rows[1]["id"]
        deleted = self.api.delete_invoices([first], True)
        self.assertTrue(archive.exists(), "ZIP archive is shared with the surviving invoice")
        self.assertEqual(deleted["retained_shared_files"], 1)
        self.api.delete_invoices([second], True)
        self.assertFalse(archive.exists(), "ZIP archive is removed when its final linked invoice is deleted")

    def test_preview_api_rejects_invalid_source_index(self):
        path = self.make_pdf("single.pdf", 1)
        invoice_id = self.add_invoice([path])
        with self.assertRaises(ValueError):
            self.api.get_original_preview_more(invoice_id, 4, 0)


if __name__ == "__main__":
    unittest.main()
