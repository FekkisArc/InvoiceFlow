import importlib.util, tempfile, unittest, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("invoice_app",Path(__file__).parents[1]/"main.py")
m=importlib.util.module_from_spec(spec); sys.modules[spec.name]=m; spec.loader.exec_module(m)

class CoreTests(unittest.TestCase):
    def test_date_formats(self):
        self.assertEqual(m.fmt_date("01.09.2026"),"01/09/2026")
        self.assertEqual(m.fmt_date("2026-09-01"),"01/09/2026")
        self.assertEqual(m.fmt_date("99.99.2026"),"")
    def test_money_european(self):
        self.assertEqual(m.parse_money("1.234,56 Lei"),1234.56)
        self.assertEqual(m.parse_money("1234.56"),1234.56)
    def test_sample_invoice(self):
        text="""INDUSTRY TRANSILVAN SRL
FACTURA PR/19496
Data facturii: 01.09.2026
Valoare: 716,53 Lei
TVA: 150,47 Lei
TOTAL DE PLATA: 867,00 Lei"""
        x=m.extract_fields(text,"sample.pdf")
        self.assertEqual(x.vendor,"INDUSTRY TRANSILVAN SRL")
        self.assertEqual(x.invoice_date,"01/09/2026")
        self.assertEqual(x.amount,"867,00")
        self.assertEqual(x.currency,"RON")
    def test_no_total_does_not_guess(self):
        x=m.extract_fields("SHOP SRL\\nNet 100,00 Lei\\nTVA 19,00 Lei","x.pdf")
        self.assertEqual(x.amount,"")
        self.assertIn("Payable total",x.notes)
    def test_vendor_and_date_but_no_payable_total(self):
        x=m.extract_fields("ABC RETAIL SRL\\nFactura nr 1234\\nData facturii: 04.10.2026\\nSubtotal 50,00 Lei","invoice.pdf")
        self.assertEqual(x.invoice_date,"04/10/2026")
        self.assertEqual(x.amount,"")
    def test_field_confidence_is_field_specific(self):
        text="""ALtex SRL
Factura nr 12345
Data facturii: 06.11.2026
TOTAL DE PLATA: 250,00 Lei"""
        x=m.extract_fields(text,"altex.pdf")
        self.assertIn("vendor",x.field_confidence)
        self.assertGreaterEqual(x.field_confidence["amount"],0.8)
        self.assertGreaterEqual(x.field_confidence["invoice_date"],0.8)
        self.assertLessEqual(x.field_confidence["invoice_number"],1.0)

    def test_vendor_alias_match(self):
        with tempfile.TemporaryDirectory() as td:
            old_base, old_db = m.BASE, m.DB
            m.BASE=Path(td); m.DB=Path(td)/"test.sqlite"
            try:
                m.init_db()
                m.save_vendor_alias("Altex Romania SRL","ALTX","logo text")
                x=m.extract_fields("FACTURA\nALTX\nData facturii: 06.11.2026\nTOTAL DE PLATA 250,00 Lei","scan.pdf")
                self.assertEqual(x.vendor,"Altex Romania SRL")
            finally:
                m.BASE, m.DB = old_base, old_db

    def test_split_part_filename_detection(self):
        self.assertEqual(m.split_part_info("Receipt 1 out of 2.pdf"), ("receipt",1,2))
        self.assertEqual(m.split_part_info("Receipt 2 of 2.pdf"), ("receipt",2,2))
        self.assertIsNone(m.split_part_info("ordinary_invoice.pdf"))
    def test_vendor_heuristic_skips_invoice_header(self):
        x=m.extract_fields("FACTURA\nALtex\nCUI 123456\nData facturii: 06.11.2026\nTOTAL DE PLATA 250,00 Lei","altex.pdf")
        self.assertEqual(x.vendor,"ALtex")
        self.assertEqual(x.amount,"250,00")

    def test_split_names(self):
        self.assertEqual(m.split_part_info("altex 1 out of 2.pdf"),("altex",1,2))
        self.assertEqual(m.split_part_info("altex 2 out of 2.pdf"),("altex",2,2))

    def test_zip_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            z=Path(td)/"x.zip"
            import zipfile
            with zipfile.ZipFile(z,"w") as f: f.writestr("../escape.txt","bad")
            out=Path(td)/"out"
            self.assertEqual(m.safe_extract_zip(z,out),[])
            self.assertFalse((Path(td)/"escape.txt").exists())
if __name__=="__main__": unittest.main()
