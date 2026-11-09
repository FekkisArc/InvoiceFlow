import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("invoiceflow_core_v5", ROOT / "main.py")
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)


class IsolatedDatabase(unittest.TestCase):
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

    def tearDown(self):
        core.BASE, core.DB, core.ORIGINALS, core.EXPORTS, core.LOG = self.old
        self.temp.cleanup()

    def add(self, **changes):
        data = {
            "vendor": "Altex Romania SRL", "invoice_number": "ALT-2026-001",
            "invoice_date": "06.11.2026", "amount": "250,00", "currency": "RON",
            "status": "Needs review", "review_notes": "", "source_name": "a.pdf",
            "source_path": str(self.root / "a.pdf"), "file_hash": "hash-a", "raw_text": "",
            "field_confidence": {"amount": 0.9}, "verified_fields": {}
        }
        data.update(changes)
        return core.insert_invoice(data)

    def test_legacy_database_migrates_before_indexes(self):
        # Simulate an early MVP schema with filename/notes and no file_hash.
        with sqlite3.connect(core.DB) as con:
            con.execute("DROP TABLE invoices")
            con.execute("CREATE TABLE invoices(id INTEGER PRIMARY KEY, vendor TEXT, invoice_date TEXT, amount TEXT, currency TEXT, status TEXT, filename TEXT, notes TEXT, created_at TEXT)")
            con.execute("INSERT INTO invoices VALUES(1,'Old Vendor','2026-09-01','12,00','RON','Needs review','old.pdf','legacy note','2026-09-01T10:00:00')")
        core.init_db()
        row = next(r for r in core.records() if r["id"] == 1)
        self.assertEqual(row["source_name"], "old.pdf")
        self.assertEqual(row["review_notes"], "legacy note")
        self.assertEqual(row["invoice_date"], "01/09/2026")
        with core.db_connect() as con:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(invoices)")}
            self.assertIn("file_hash", cols)
            self.assertIn("verified_fields_json", cols)
            version = con.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0]
            self.assertEqual(version, "6")

    def test_date_ordering_normalizes_iso_and_european_values(self):
        self.add(invoice_date="01/10/2026", file_hash="hash-1")
        self.add(invoice_date="2026-09-30", file_hash="hash-2", invoice_number="ALT-002")
        dates = [r["invoice_date"] for r in core.records()]
        self.assertEqual(dates, ["30/09/2026", "01/10/2026"])

    def test_duplicate_is_flagged_and_confirmation_is_blocked(self):
        first, duplicate = self.add(file_hash="same-hash")
        second, flagged = self.add(file_hash="same-hash", source_name="copy.pdf", invoice_number="ALT-2026-001")
        self.assertFalse(duplicate)
        self.assertTrue(flagged)
        row = next(r for r in core.records() if r["id"] == second)
        self.assertEqual(row["duplicate_of"], first)
        checks = {k: True for k in ("vendor", "invoice_number", "invoice_date", "amount", "currency")}
        result = core.update_invoice(second, [row["vendor"], row["invoice_number"], row["invoice_date"], row["amount"], row["currency"], "Confirmed", row["review_notes"]], checks)
        self.assertEqual(result["status"], "Needs review")
        self.assertIn("Possible duplicate", result["warning"])

    def test_manual_duplicate_clear_requires_reconfirmation(self):
        first, _ = self.add(file_hash="same-hash")
        second, flagged = self.add(file_hash="same-hash", source_name="copy.pdf")
        self.assertTrue(flagged)
        self.assertTrue(core.resolve_duplicate(second))
        row = next(r for r in core.records() if r["id"] == second)
        self.assertIsNone(row["duplicate_of"])
        checks = {k: True for k in ("vendor", "invoice_number", "invoice_date", "amount", "currency")}
        result = core.update_invoice(second, [row["vendor"], row["invoice_number"], row["invoice_date"], row["amount"], row["currency"], "Confirmed", row["review_notes"]], checks)
        self.assertEqual(result["status"], "Confirmed")

    def test_changed_amount_invalidates_verification_and_downgrades(self):
        invoice_id, _ = self.add(file_hash="unique-hash")
        row = next(r for r in core.records() if r["id"] == invoice_id)
        all_checked = {k: True for k in ("vendor", "invoice_number", "invoice_date", "amount", "currency")}
        result = core.update_invoice(invoice_id, [row["vendor"], row["invoice_number"], row["invoice_date"], "900,00", row["currency"], "Confirmed", row["review_notes"]], all_checked)
        self.assertEqual(result["status"], "Needs review")
        self.assertIn("amount", result["changed_fields"])
        self.assertFalse(result["verified_fields"]["amount"])
        updated = next(r for r in core.records() if r["id"] == invoice_id)
        self.assertEqual(updated["amount"], "900,00")
        self.assertEqual(updated["status"], "Needs review")

    def test_unknown_currency_stays_unknown(self):
        x = core.extract_fields("Example Supplier\nInvoice no AB-1200\nInvoice date: 06.11.2026\nTotal payable 250.00", "x.pdf")
        self.assertEqual(x.currency, "")
        invoice_id, _ = self.add(currency="", file_hash="unknown-currency")
        row = next(r for r in core.records() if r["id"] == invoice_id)
        self.assertEqual(row["currency"], "")

    def test_alias_rejects_suffix_and_matches_complete_token_only(self):
        self.assertFalse(core.save_vendor_alias("Altex Romania SRL", "SRL"))
        self.assertTrue(core.save_vendor_alias("Altex Romania SRL", "ALTX"))
        good = core.extract_fields("FACTURA\nALTX SRL\nInvoice date: 06.11.2026\nTotal payable: 250,00 Lei", "x.pdf")
        bad = core.extract_fields("MALTXSTORE SRL\nInvoice date: 06.11.2026\nTotal payable: 250,00 Lei", "x.pdf")
        self.assertEqual(good.vendor, "Altex Romania SRL")
        self.assertNotEqual(bad.vendor, "Altex Romania SRL")

    def test_due_date_is_not_invoice_date(self):
        x = core.extract_fields("ABC STORE SRL\nInvoice no ABC-123\nDue date: 14.11.2026\nTotal payable: 100,00 Lei", "x.pdf")
        self.assertEqual(x.invoice_date, "")

    def test_invoice_number_does_not_capture_bill_to(self):
        x = core.extract_fields("FACTURA\nBILL TO\nCustomer Name\nInvoice number: AB/2026/123\nInvoice date: 06.11.2026\nTotal payable: 250,00 Lei", "x.pdf")
        self.assertEqual(x.invoice_number, "AB/2026/123")
        y = core.extract_fields("FACTURA\nALtex SRL\nCUI 123456\nData facturii: 06.11.2026\nTOTAL DE PLATA 250,00 Lei", "x.pdf")
        self.assertNotEqual(y.invoice_number, "ALtex")

    def test_total_payable_prefers_payable_value_over_vat(self):
        x = core.extract_fields("SHOP SRL\nTOTAL DE PLATA: 463,10 RON TVA 41,72 RON", "x.pdf")
        self.assertEqual(x.amount, "463,10")
        self.assertEqual(x.currency, "RON")

    def test_folder_import_finds_zip_inside_directory(self):
        selected = self.root / "selected"
        selected.mkdir()
        archive = selected / "nested.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("invoice.pdf", b"%PDF-FAKE")
            z.writestr("readme.txt", "ignore")
        files = core.collect_inputs([selected], self.root / "batch")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "invoice.pdf")
        self.assertTrue(files[0].exists())

    def test_zip_extraction_maps_each_document_back_to_the_original_archive(self):
        archive = self.root / "root-archive.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("invoice-a.pdf", b"%PDF-FAKE-A")
            z.writestr("invoice-b.pdf", b"%PDF-FAKE-B")
        origins = {}
        files = core.collect_inputs([archive], self.root / "batch", origin_map=origins)
        self.assertEqual(len(files), 2)
        for extracted in files:
            self.assertEqual(origins[str(extracted.absolute())], str(archive.absolute()))


    def test_confirmation_requires_invoice_number_and_currency(self):
        invoice_id, _ = self.add(invoice_number="", currency="")
        row = next(r for r in core.records() if r["id"] == invoice_id)
        checks = {k: True for k in ("vendor", "invoice_number", "invoice_date", "amount", "currency")}
        result = core.update_invoice(invoice_id, [row["vendor"], "", row["invoice_date"], row["amount"], "", "Confirmed", ""], checks)
        self.assertEqual(result["status"], "Needs review")
        self.assertIn("invoice_number", result["warning"])
        self.assertIn("currency", result["warning"])

    def test_invalid_date_and_currency_are_rejected_by_core(self):
        invoice_id, _ = self.add(file_hash="validation-hash")
        row = next(r for r in core.records() if r["id"] == invoice_id)
        values = [row["vendor"], row["invoice_number"], "31/02/2026", row["amount"], row["currency"], "Needs review", ""]
        with self.assertRaises(ValueError):
            core.update_invoice(invoice_id, values, {})
        values[2] = row["invoice_date"]
        values[4] = "Romanian Lei"
        with self.assertRaises(ValueError):
            core.update_invoice(invoice_id, values, {})

    def test_restore_backup_preserves_pre_restore_snapshot(self):
        original_id, _ = self.add(file_hash="before-restore")
        core.ORIGINALS.mkdir(parents=True, exist_ok=True)
        (core.ORIGINALS / "kept.pdf").write_bytes(b"invoice source")
        backup = core.create_backup(self.root / "known-good-backup")
        self.add(file_hash="after-backup", invoice_number="ALT-2026-002")
        safety = core.restore_backup(backup)
        ids = [r["id"] for r in core.records()]
        self.assertEqual(ids, [original_id])
        self.assertTrue((safety / "invoices.sqlite3").exists())
        self.assertTrue((core.ORIGINALS / "kept.pdf").exists())

    def test_corrupt_backup_is_rejected_without_changing_database(self):
        before, _ = self.add(file_hash="pre-corrupt")
        corrupt = self.root / "corrupt-backup"
        corrupt.mkdir()
        (corrupt / "invoices.sqlite3").write_bytes(b"not sqlite")
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            core.restore_backup(corrupt)
        self.assertEqual([r["id"] for r in core.records()], [before])

    def test_export_totals_ignore_legacy_confirmed_duplicate(self):
        first, _ = self.add(file_hash="same-export", status="Needs review")
        second, _ = self.add(file_hash="same-export", source_name="duplicate.pdf", invoice_number="ALT-2026-001")
        row = next(r for r in core.records() if r["id"] == first)
        checks = {k: True for k in ("vendor", "invoice_number", "invoice_date", "amount", "currency")}
        core.update_invoice(first, [row["vendor"], row["invoice_number"], row["invoice_date"], row["amount"], row["currency"], "Confirmed", ""], checks)
        # Simulate a legacy bad state where an older release had confirmed a duplicate.
        with core.db_connect() as con:
            con.execute("UPDATE invoices SET status='Confirmed' WHERE id=?", (second,))
        path = self.root / "report.xlsx"
        core.export_xlsx(path)
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True)
        monthly = wb["November 2026"]
        summary_rows = [[c.value for c in row] for row in monthly.iter_rows()]
        self.assertTrue(any(len(row) >= 2 and row[0] == "RON" and row[1] == 250 for row in summary_rows))
        self.assertFalse(any(len(row) >= 2 and row[0] == "RON" and row[1] == 500 for row in summary_rows))

    def test_backup_uses_consistent_database_and_includes_originals(self):
        invoice_id, _ = self.add(file_hash="backup-hash")
        core.ORIGINALS.mkdir(parents=True, exist_ok=True)
        (core.ORIGINALS / "invoice.pdf").write_bytes(b"original")
        backup = core.create_backup(self.root / "backup")
        with sqlite3.connect(backup / "invoices.sqlite3") as con:
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM invoices WHERE id=?", (invoice_id,)).fetchone()[0], 1)
        self.assertTrue((backup / "Originals" / "invoice.pdf").exists())


    def test_delete_removes_record_and_app_managed_source_copy(self):
        stored = core.ORIGINALS / "Batch_test" / "files" / "invoice.pdf"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b"invoice source")
        invoice_id, _ = self.add(source_path=str(stored), file_hash="delete-source")
        result = core.delete_invoices([invoice_id])
        self.assertEqual(result["deleted_records"], 1)
        self.assertEqual(result["deleted_files"], 1)
        self.assertFalse(stored.exists())
        self.assertFalse((core.ORIGINALS / "Batch_test" / "files").exists())
        self.assertEqual(core.records(), [])

    def test_legacy_source_path_outside_originals_is_preserved_without_input_tracking(self):
        external = self.root / "user-picked-invoice.pdf"
        external.write_bytes(b"user original")
        invoice_id, _ = self.add(source_path=str(external), file_hash="legacy-external")
        result = core.delete_invoices([invoice_id])
        self.assertEqual(result["deleted_records"], 1)
        self.assertEqual(result["deleted_files"], 0)
        self.assertTrue(external.exists())

    def test_delete_removes_direct_import_original_and_preserved_copy(self):
        external = self.root / "user-picked-invoice.pdf"
        external.write_bytes(b"user original")
        stored = core.ORIGINALS / "Batch_direct" / "files" / external.name
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b"preserved copy")
        invoice_id, _ = self.add(source_path=str(stored), source_input_path=str(external),
                                 file_hash="direct-original-delete")
        result = core.delete_invoices([invoice_id])
        self.assertEqual(result["deleted_records"], 1)
        self.assertEqual(result["deleted_files"], 2)
        self.assertEqual(result["deleted_managed_copies"], 1)
        self.assertEqual(result["deleted_original_files"], 1)
        self.assertFalse(external.exists())
        self.assertFalse(stored.exists())

    def test_delete_can_keep_original_file_when_user_unchecks_option(self):
        external = self.root / "keep-original.pdf"
        external.write_bytes(b"original should remain")
        stored = core.ORIGINALS / "Batch_keep" / "copy.pdf"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b"managed copy")
        invoice_id, _ = self.add(source_path=str(stored), source_input_path=str(external),
                                 file_hash="keep-original-choice")
        result = core.delete_invoices([invoice_id], delete_originals=False)
        self.assertFalse(stored.exists())
        self.assertTrue(external.exists())
        self.assertEqual(result["deleted_managed_copies"], 1)
        self.assertEqual(result["deleted_original_files"], 0)
        self.assertEqual(result["preserved_original_files"], 1)

    def test_delete_keeps_external_original_if_shared_by_surviving_invoice(self):
        external = self.root / "shared-original.pdf"
        external.write_bytes(b"shared")
        stored_a = core.ORIGINALS / "Batch_a" / "copy.pdf"
        stored_b = core.ORIGINALS / "Batch_b" / "copy.pdf"
        stored_a.parent.mkdir(parents=True); stored_b.parent.mkdir(parents=True)
        stored_a.write_bytes(b"copy a"); stored_b.write_bytes(b"copy b")
        first, _ = self.add(source_path=str(stored_a), source_input_path=str(external), file_hash="shared-input-a")
        second, _ = self.add(source_path=str(stored_b), source_input_path=str(external), file_hash="shared-input-b", invoice_number="ALT-003")
        result = core.delete_invoices([first])
        self.assertTrue(external.exists())
        self.assertFalse(stored_a.exists())
        self.assertTrue(stored_b.exists())
        self.assertEqual(result["retained_shared_files"], 1)
        self.assertEqual([row["id"] for row in core.records()], [second])

    def test_delete_does_not_follow_symlinked_original(self):
        external = self.root / "important.pdf"
        external.write_bytes(b"keep")
        link = self.root / "imported-link.pdf"
        try:
            link.symlink_to(external)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are not available in this environment")
        invoice_id, _ = self.add(source_path=str(core.ORIGINALS / "copy.pdf"),
                                 source_input_path=str(link), file_hash="symlink-input")
        (core.ORIGINALS / "copy.pdf").write_bytes(b"copy")
        result = core.delete_invoices([invoice_id])
        self.assertTrue(external.exists())
        self.assertTrue(link.is_symlink())
        self.assertEqual(result["skipped_symlinks"], 1)

    def test_delete_keeps_managed_file_still_referenced_by_another_record(self):
        stored = core.ORIGINALS / "Batch_shared" / "invoice.pdf"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b"shared source")
        first, _ = self.add(source_path=str(stored), file_hash="shared-one")
        second, _ = self.add(source_path=str(stored), file_hash="shared-two", invoice_number="ALT-002")
        result = core.delete_invoices([first])
        self.assertEqual(result["deleted_records"], 1)
        self.assertEqual(result["deleted_files"], 0)
        self.assertEqual(result["retained_shared_files"], 1)
        self.assertTrue(stored.exists())
        self.assertEqual([row["id"] for row in core.records()], [second])

    def test_delete_many_records_deletes_each_unshared_file_once(self):
        paths = []
        ids = []
        for index in range(3):
            path = core.ORIGINALS / f"Batch_{index}" / f"{index}.pdf"
            path.parent.mkdir(parents=True)
            path.write_bytes(f"source {index}".encode())
            paths.append(path)
            ids.append(self.add(source_path=str(path), file_hash=f"bulk-{index}")[0])
        result = core.delete_invoices(ids + ids[:1])
        self.assertEqual(result["deleted_records"], 3)
        self.assertEqual(result["deleted_files"], 3)
        self.assertTrue(all(not path.exists() for path in paths))


class PureFunctionTests(unittest.TestCase):

    def test_romanian_serie_numar_and_data_labels(self):
        x = core.extract_fields("Industry Transilvan SRL\nSerie/Numar: PR/19496 Data: 01.09.2026\nScadenta: 01.09.2026\nTotal de plata 867,00 Lei", "invoice.pdf")
        self.assertEqual(x.invoice_number, "PR/19496")
        self.assertEqual(x.invoice_date, "01/09/2026")
        self.assertEqual(x.amount, "867,00")
        self.assertEqual(x.currency, "RON")

    def test_fiscal_receipt_number_and_amount_fallback(self):
        text = """_JYSK ROMANIA SRL
STR. MIHAIL SEBASTIAN NR.88B
C.C.VULCAN VALUE CENTER
VI 6/11/2026 0061
4 BUC x 135,00
CUIER FELST 540,00
SUBTOTAL 540,00
CARD 240,00
BON FISCAL: 0040 BUCATI: 1
SUMA TVA 21% 93,72
ACCUM. BON FISCAL: 0099247 101
BON FISCAL
A6000884782
"""
        x = core.extract_fields(text, "receipt.pdf")
        self.assertEqual(x.vendor, "JYSK ROMANIA SRL")
        self.assertEqual(x.invoice_number, "A6000884782")
        self.assertEqual(x.invoice_date, "06/11/2026")
        self.assertEqual(x.amount, "540,00")
        # Incomplete currency evidence must remain blank, never assume RON.
        self.assertEqual(x.currency, "")

    def test_fiscal_series_word_is_not_misread_as_invoice_number(self):
        x = core.extract_fields("Industry Transilvan SRL\nFACTURA FISCALA\nSerie/Numar: PR/19496 Data: 01.09.2026\nTotal de plata 867,00 Lei")
        self.assertEqual(x.invoice_number, "PR/19496")

    def test_money_parser_european_us_and_negative(self):
        self.assertEqual(core.parse_money("1.234,56 Lei"), 1234.56)
        self.assertEqual(core.parse_money("1,234.56 USD"), 1234.56)
        self.assertEqual(core.parse_money("(125,50)"), -125.50)
        self.assertEqual(core.parse_money("not money"), None)

    def test_date_validation_rejects_impossible_dates(self):
        self.assertEqual(core.fmt_date("2026-11-08"), "08/11/2026")
        self.assertEqual(core.fmt_date("31/02/2026"), "")


    def test_split_grouping_keeps_duplicate_part_numbers_separate(self):
        folder = Path(tempfile.gettempdir()) / "InvoiceFlowSplitTest"
        # Same normalized base is needed to exercise duplicate part numbers.
        files = [folder / "invoice 1 of 2.pdf", folder / "invoice_1_of_2.pdf", folder / "invoice 2 of 2.pdf"]
        grouped = core.group_split_files(files)
        self.assertEqual(len(grouped), 3)
        self.assertTrue(all(len(item) == 1 for item in grouped))
        self.assertEqual({p.name for item in grouped for p in item}, {p.name for p in files})

    def test_complete_split_group_keeps_all_parts_in_order(self):
        folder = Path(tempfile.gettempdir()) / "InvoiceFlowCompleteSplitTest"
        files = [folder / "invoice 2 of 2.pdf", folder / "invoice 1 of 2.pdf"]
        grouped = core.group_split_files(files)
        self.assertEqual(len(grouped), 1)
        self.assertEqual([p.name for p in grouped[0]], ["invoice 1 of 2.pdf", "invoice 2 of 2.pdf"])

    def test_native_pdf_text_rejects_garbled_text_layer(self):
        try:
            import fitz
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("PyMuPDF and Pillow are needed for PDF text-layer test")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "garbled_layer.pdf"
            img = Image.new("RGB", (900, 1200), "white")
            draw = ImageDraw.Draw(img)
            draw.text((80, 100), "VISIBLE SCANNED INVOICE", fill="black")
            png = Path(td) / "scan.png"
            img.save(png)
            doc = fitz.open()
            page = doc.new_page()
            page.insert_image(page.rect, filename=str(png))
            # Plenty of characters, but this hidden OCR-like layer is noise.
            page.insert_text((20, 30), "xqz qxv 9921 zzz qvzx 1134 pqq 7788 xzqv 5544")
            doc.save(path)
            doc.close()
            sources = list(core._iter_page_sources(path))
            self.assertEqual(len(sources), 1)
            self.assertFalse(sources[0][2])
            self.assertIsNotNone(sources[0][1])

    def test_split_pdf_sources_are_handled_per_page(self):
        try:
            import fitz
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("PyMuPDF and Pillow are needed for the mixed PDF test")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mixed.pdf"
            doc = fitz.open()
            page1 = doc.new_page()
            page1.insert_text((50, 70), "INVOICE ISSUER: TEST COMPANY SRL\nINVOICE NUMBER: INV-2026-0001\nDATE: 08.11.2026\nTOTAL PAYABLE: 120.00 RON")
            image = Image.new("RGB", (900, 1200), "white")
            draw = ImageDraw.Draw(image)
            draw.text((80, 100), "SCANNED PAGE TWO", fill="black")
            png = Path(td) / "scan.png"
            image.save(png)
            page2 = doc.new_page()
            page2.insert_image(page2.rect, filename=str(png))
            doc.save(path)
            doc.close()
            sources = list(core._iter_page_sources(path))
            self.assertEqual(len(sources), 2)
            self.assertTrue(sources[0][2])
            self.assertIsNone(sources[0][1])
            self.assertEqual(sources[1][2], "")
            self.assertIsNotNone(sources[1][1])


if __name__ == "__main__":
    unittest.main()
