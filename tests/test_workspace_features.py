"""Integration tests for InvoiceFlow's local professional workspace modules."""
from __future__ import annotations
import csv
import sys
import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

import main as core
import workspace_features as features


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = (core.BASE, core.DB, core.ORIGINALS, core.EXPORTS, core.LOG)
        core.BASE = self.root / "InvoiceFlow"
        core.DB = core.BASE / "invoices.sqlite3"
        core.ORIGINALS = core.BASE / "Originals"
        core.EXPORTS = core.BASE / "Exports"
        core.LOG = core.BASE / "app.log"
        core.init_db()
        features.init_db()

    def tearDown(self):
        core.BASE, core.DB, core.ORIGINALS, core.EXPORTS, core.LOG = self.old
        self.tmp.cleanup()

    def add_invoice(self, **changes):
        serial = len(list(core.records())) + 1
        managed = core.ORIGINALS / "Batch_Test" / f"source-{serial}.pdf"
        managed.parent.mkdir(parents=True, exist_ok=True)
        managed.write_bytes(b"managed pdf placeholder")
        original = self.root / "Imports" / f"source-{serial}.pdf"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"original pdf placeholder")
        data = {
            "vendor": "Altex Romania SRL", "invoice_number": "ALT-2026-001",
            "invoice_date": "06/11/2026", "amount": "250,00", "currency": "RON",
            "status": "Needs review", "review_notes": "", "source_name": "source.pdf",
            "source_path": str(managed), "source_input_path": str(original),
            "file_hash": "hash-" + str(len(list(core.records())) + 1), "raw_text": "",
            "ocr_confidence": 0.8, "field_confidence": {"amount": 0.9}, "verified_fields": {},
        }
        data.update(changes)
        invoice_id, _duplicate = core.insert_invoice(data)
        features.update_invoice_extra(
            invoice_id,
            category=data.get("category", ""),
            project=data.get("project", ""),
            due_date=data.get("due_date", ""),
            vat_amount=data.get("vat_amount", ""),
            gross_amount=data.get("gross_amount", ""),
            payment_status=data.get("payment_status", "Unknown"),
        )
        return invoice_id, managed, original

    def sample_ubl(self, path: Path, root_name="Invoice"):
        text = f'''<?xml version="1.0" encoding="UTF-8"?>
<{root_name} xmlns="urn:oasis:names:specification:ubl:schema:xsd:{root_name}-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cbc:ID>EF-2026-123</cbc:ID><cbc:IssueDate>2026-11-06</cbc:IssueDate><cbc:DueDate>2026-11-20</cbc:DueDate><cbc:DocumentCurrencyCode>RON</cbc:DocumentCurrencyCode>
 <cac:AccountingSupplierParty><cac:Party><cac:PartyLegalEntity><cbc:RegistrationName>Furnizor Test SRL</cbc:RegistrationName></cac:PartyLegalEntity></cac:Party></cac:AccountingSupplierParty>
 <cac:TaxTotal><cbc:TaxAmount currencyID="RON">19.00</cbc:TaxAmount></cac:TaxTotal>
 <cac:LegalMonetaryTotal><cbc:TaxInclusiveAmount currencyID="RON">119.00</cbc:TaxInclusiveAmount><cbc:PayableAmount currencyID="RON">119.00</cbc:PayableAmount></cac:LegalMonetaryTotal>
</{root_name}>'''
        path.write_text(text, encoding="utf-8")
        return path

    def test_workspace_migrations_are_idempotent(self):
        features.init_db(); features.init_db()
        with core.db_connect() as con:
            tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"suppliers", "projects", "saved_views", "recycle_bin", "invoice_attachments"}.issubset(tables))

    def test_ubl_invoice_extracts_payable_date_supplier_and_vat(self):
        path = self.sample_ubl(self.root / "ubl.xml")
        result = features.parse_ubl_invoice(path)
        self.assertEqual(result["vendor"], "Furnizor Test SRL")
        self.assertEqual(result["invoice_number"], "EF-2026-123")
        self.assertEqual(result["invoice_date"], "06/11/2026")
        self.assertEqual(result["due_date"], "20/11/2026")
        self.assertEqual(result["amount"], "119.00")
        self.assertEqual(result["currency"], "RON")
        self.assertEqual(result["vat_amount"], "19.00")
        self.assertIn("not validated", result["review_notes"])

    def test_ubl_credit_note_returns_negative_payable(self):
        path = self.sample_ubl(self.root / "credit.xml", "CreditNote")
        self.assertEqual(features.parse_ubl_invoice(path)["amount"], "-119.0")

    def test_xml_parser_rejects_non_ubl_root_and_oversized_file(self):
        p = self.root / "not-invoice.xml"; p.write_text("<html/>")
        with self.assertRaisesRegex(ValueError, "Unsupported XML root"):
            features.parse_ubl_invoice(p)
        p.write_bytes(b"0" * (50 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "50 MB"):
            features.parse_ubl_invoice(p)

    def test_supplier_profile_aliases_and_spending(self):
        invoice_id, _, _ = self.add_invoice(status="Confirmed")
        sid = features.update_supplier({"canonical_name": "Altex Romania SRL", "vat_number": "RO123", "email": "accounts@example.ro", "default_category": "Office & administration"})
        features.save_supplier_ocr_rules(sid, {"invoice_number": r"Invoice\s+No\s*([A-Z0-9-]+)"})
        features.set_tags(invoice_id, ["Urgent"])
        data = features.supplier_list()[0]
        self.assertEqual(data["vat_number"], "RO123")
        self.assertEqual(data["spend_by_currency"]["RON"], 250.0)
        self.assertIn("Urgent", features.invoice_metadata(invoice_id)["tags"])

    def test_supplier_specific_ocr_rules_override_field_candidate(self):
        sid = features.upsert_supplier("Altex Romania SRL")
        features.save_supplier_ocr_rules(sid, {"invoice_number": r"Reference\s*:\s*([A-Z0-9/-]+)"})
        fields = core.extract_fields("Altex Romania SRL\nInvoice no: WRONG-1\nReference: RIGHT-22", "a.pdf")
        self.assertEqual(features.apply_supplier_ocr_rules(fields, "Reference: RIGHT-22", "Altex Romania SRL").invoice_number, "RIGHT-22")

    def test_category_rules_and_auto_categorization_setting(self):
        invoice_id, _, _ = self.add_invoice(vendor="Fuel Supplier SRL")
        category = next(x for x in features.categories() if x["name"] == "Travel & transport")
        features.add_category_rule("diesel", category["id"])
        self.assertEqual(features.auto_categorize(invoice_id, "Fuel Supplier SRL", "Diesel purchase"), "Travel & transport")
        features.save_settings({"auto_categorize": "false"})
        invoice2, _, _ = self.add_invoice(vendor="Fuel Two SRL")
        self.assertEqual(features.auto_categorize(invoice2, "Fuel Two SRL", "Diesel purchase"), "")

    def test_projects_saved_views_and_bulk_updates(self):
        a, _, _ = self.add_invoice(file_hash="p1")
        b, _, _ = self.add_invoice(file_hash="p2", invoice_number="ALT-002")
        features.save_project("Office move", "Relocation work", "Operations")
        self.assertEqual(features.project_list()[0]["name"], "Office move")
        self.assertEqual(features.bulk_update_invoices([a, b], category="Equipment", project="Office move", add_tag="Q4" )["updated"], 2)
        for row in core.records():
            self.assertEqual(row["category"], "Equipment")
            self.assertEqual(row["project"], "Office move")
        features.save_saved_view("Q4 equipment", {"status": "all", "category": "Equipment", "query": ""})
        self.assertEqual(features.saved_views()[0]["config"]["category"], "Equipment")
        features.delete_saved_view(features.saved_views()[0]["id"])
        self.assertEqual(features.saved_views(), [])

    def test_bulk_update_does_not_confirm_or_bypass_verification(self):
        invoice_id, _, _ = self.add_invoice()
        features.bulk_update_invoices([invoice_id], payment_status="Paid")
        row = next(r for r in core.records() if r["id"] == invoice_id)
        self.assertEqual(row["status"], "Needs review")
        self.assertEqual(row["payment_status"], "Paid")

    def test_budget_and_multi_sheet_workbook(self):
        invoice_id, _, _ = self.add_invoice(status="Confirmed", category="Equipment")
        category = next(x for x in features.categories() if x["name"] == "Equipment")
        features.save_budget("2026-11", "RON", 500, category["id"], "Monthly cap")
        output = self.root / "report.xlsx"
        features.export_workspace_xlsx(output)
        from openpyxl import load_workbook
        wb = load_workbook(output, read_only=True)
        try:
            self.assertTrue({"Executive Summary", "General Tracker", "Supplier Analysis", "Category Analysis", "Monthly Trends", "Budget vs Actual", "Needs Review"}.issubset(set(wb.sheetnames)))
            self.assertEqual(wb["Budget vs Actual"].cell(2, 5).value, 250.0)
        finally:
            wb.close()

    def test_csv_export_uses_semicolon_and_includes_metadata(self):
        self.add_invoice(category="Office & administration", project="HQ", due_date="20/11/2026", vat_amount="47.50")
        out = self.root / "export.csv"
        self.assertEqual(features.export_csv(out), 1)
        data = out.read_text(encoding="utf-8-sig")
        self.assertIn("Due date", data); self.assertIn("Office & administration", data); self.assertIn("HQ", data)

    def test_invoice_attachment_is_copied_under_managed_originals(self):
        invoice_id, _, _ = self.add_invoice()
        source = self.root / "contract.pdf"; source.write_bytes(b"contract")
        added = features.add_attachment(invoice_id, str(source), "supporting contract")
        self.assertTrue(Path(added["path"]).is_relative_to(core.ORIGINALS))
        self.assertTrue(Path(added["path"]).is_file())
        self.assertEqual(features.invoice_metadata(invoice_id)["attachments"][0]["note"], "supporting contract")

    def test_recycle_and_restore_moves_originals_and_managed_copies(self):
        invoice_id, managed, original = self.add_invoice()
        result = features.recycle_invoices([invoice_id], True)
        self.assertEqual(result["count"], 1)
        self.assertFalse(managed.exists()); self.assertFalse(original.exists())
        self.assertEqual(len(core.records()), 0)
        item = features.recycle_bin_list()[0]
        restored = features.restore_recycled(item["id"])
        self.assertTrue(Path(next(r for r in core.records() if r["id"] == restored["invoice_id"])["source_path"]).exists())
        self.assertTrue(original.exists())

    def test_recycle_shared_external_source_is_moved_once_and_survives_one_purge(self):
        a, _, original = self.add_invoice(file_hash="shared-1")
        b, managed_b, _ = self.add_invoice(file_hash="shared-2", invoice_number="ALT-002", source_input_path=str(original))
        result = features.recycle_invoices([a, b], True)
        self.assertFalse(original.exists())
        self.assertEqual(result["moved_files"], 3)  # two managed copies + one shared external original
        items = features.recycle_bin_list()
        features.permanently_delete_recycled([items[0]["id"]])
        with core.db_connect() as con:
            moved_json = con.execute("SELECT moved_files_json FROM recycle_bin LIMIT 1").fetchone()[0]
        self.assertTrue(any(Path(v["recycled"]).exists() for v in __import__('json').loads(moved_json).values()))
        remaining = features.recycle_bin_list()[0]
        features.restore_recycled(remaining["id"])
        self.assertTrue(original.exists())

    def test_recycle_bin_shared_files_with_active_invoice_are_retained(self):
        a, _, original = self.add_invoice(file_hash="shared-a")
        b, _, _ = self.add_invoice(file_hash="shared-b", invoice_number="ALT-002", source_input_path=str(original))
        features.recycle_invoices([a], True)
        self.assertTrue(original.exists())

    def test_passcode_uses_hash_and_verification(self):
        features.set_passcode("123456")
        settings = features.get_settings()
        self.assertNotEqual(settings["pin_hash"], "123456")
        self.assertTrue(features.verify_passcode("123456"))
        self.assertFalse(features.verify_passcode("wrong"))

    def test_watched_folder_skips_recently_modified_file_until_copy_finishes(self):
        folder = self.root / "watched"; folder.mkdir()
        document = folder / "invoice.pdf"; document.write_bytes(b"pdf data")
        features.add_watched_folder(str(folder))
        self.assertEqual(features.scan_watched_folders(), [])
        import os, time
        old = time.time() - 10; os.utime(document, (old, old))
        discovered = features.scan_watched_folders()
        self.assertEqual(len(discovered), 1)
        self.assertTrue(os.path.samefile(discovered[0], document))

    def test_due_date_dashboard_counts_unpaid_past_due_invoices(self):
        self.add_invoice(due_date="01/01/2020", payment_status="Unpaid")
        self.assertGreaterEqual(features.dashboard_data()["counts"]["overdue"], 1)


    def test_analytics_filters_by_period_status_currency_and_category(self):
        self.add_invoice(status="Confirmed", invoice_date="06/11/2026", amount="250,00", currency="RON", category="Equipment", file_hash="analytics-current-1")
        self.add_invoice(status="Confirmed", invoice_date="30/10/2026", amount="100,00", currency="RON", category="Equipment", file_hash="analytics-previous-1", invoice_number="ALT-PREV")
        self.add_invoice(status="Confirmed", invoice_date="06/11/2026", amount="90,00", currency="EUR", category="Equipment", file_hash="analytics-current-2", invoice_number="ALT-EUR")
        self.add_invoice(status="Needs review", invoice_date="06/11/2026", amount="500,00", currency="RON", category="Equipment", file_hash="analytics-review", invoice_number="ALT-REV")
        result = features.analytics_data("2026-11-01", "2026-11-10", "Confirmed", "RON", "Equipment")
        self.assertEqual(result["period"]["records"], 1)
        self.assertEqual(result["totals_by_currency"], {"RON": 250.0})
        self.assertEqual(result["comparison"]["RON"]["previous"], 100.0)
        self.assertEqual(result["counts"]["confirmed"], 1)

    def test_analytics_all_currency_filter_keeps_currencies_separate(self):
        self.add_invoice(status="Confirmed", amount="250,00", currency="RON", file_hash="analytics-ron")
        self.add_invoice(status="Confirmed", amount="90,00", currency="EUR", file_hash="analytics-eur", invoice_number="ALT-EUR")
        result = features.analytics_data()
        self.assertEqual(result["totals_by_currency"], {"RON": 250.0, "EUR": 90.0})

    def test_tag_management_rename_and_delete(self):
        invoice_id, _, _ = self.add_invoice()
        features.set_tags(invoice_id, ["Urgent"])
        tag = next(t for t in features.list_tags() if t["name"] == "Urgent")
        renamed_id = features.rename_tag(tag["id"], "Priority", "#cc3344")
        meta = features.invoice_metadata(invoice_id)
        self.assertEqual(meta["tags"], ["Priority"])
        self.assertTrue(features.delete_tag(renamed_id))
        self.assertEqual(features.invoice_metadata(invoice_id)["tags"], [])

    def test_report_profile_can_be_saved_listed_and_deleted(self):
        features.save_report_profile("November confirmed", {"start_date": "2026-11-01", "end_date": "2026-11-30", "status": "Confirmed", "currency": "RON", "category": "all"})
        profile = features.report_profiles()[0]
        self.assertEqual(profile["name"], "November confirmed")
        self.assertEqual(profile["config"]["status"], "Confirmed")
        self.assertTrue(features.delete_report_profile(profile["id"]))
        self.assertEqual(features.report_profiles(), [])

    def test_omitting_tags_during_extra_update_does_not_erase_existing_tags(self):
        invoice_id, _, _ = self.add_invoice()
        features.set_tags(invoice_id, ["Keep me"])
        features.update_invoice_extra(invoice_id, category="Equipment", tags=None)
        self.assertEqual(features.invoice_metadata(invoice_id)["tags"], ["Keep me"])

    def test_custom_export_template_persists_and_controls_tracker_columns(self):
        self.add_invoice(status="Confirmed", category="Office", file_hash="template-1")
        template_id = features.save_export_template("Compact bookkeeping", {"columns": ["invoice_date", "vendor", "amount", "currency"]})
        template = next(t for t in features.export_templates() if t["id"] == template_id)
        self.assertEqual(template["config"]["columns"], ["invoice_date", "vendor", "amount", "currency"])
        output = self.root / "custom-template.xlsx"
        features.export_workspace_xlsx(output, template["config"])
        from openpyxl import load_workbook
        wb = load_workbook(output, read_only=True)
        try:
            self.assertEqual([c.value for c in wb["General Tracker"][1]], ["Invoice date", "Supplier", "Total payable", "Currency"])
        finally:
            wb.close()
        self.assertTrue(features.delete_export_template(template_id))
        self.assertEqual(features.export_templates(), [])

    def test_export_template_rejects_empty_and_unknown_column_sets(self):
        with self.assertRaises(ValueError):
            features.save_export_template("Empty", {"columns": []})
        with self.assertRaises(ValueError):
            features.save_export_template("Unknown", {"columns": ["password"]})


if __name__ == "__main__":
    unittest.main()
