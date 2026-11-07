"""
Invoice OCR — local-first Windows desktop application.
All OCR and data storage remain on the user's computer.
"""
from __future__ import annotations
import csv, difflib, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, threading, zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Optional
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "Invoice OCR"
APP_VERSION = "4.5.0"
SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
BASE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InvoiceOCR"
DB = BASE / "invoices.sqlite3"
ORIGINALS = BASE / "Originals"
EXPORTS = BASE / "Exports"
LOG = BASE / "app.log"

def log_error(message: str):
    BASE.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def find_tesseract() -> Optional[str]:
    candidates = [
        Path(os.environ.get("TESSERACT_CMD", "")) if os.environ.get("TESSERACT_CMD") else None,
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe" if os.environ.get("LOCALAPPDATA") else None,
    ]
    for p in candidates:
        if p and p.is_file():
            return str(p)
    return shutil.which("tesseract")

def db_connect():
    BASE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init_db():
    BASE.mkdir(parents=True, exist_ok=True)
    ORIGINALS.mkdir(parents=True, exist_ok=True)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    with db_connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS invoices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor TEXT NOT NULL DEFAULT '',
            invoice_number TEXT NOT NULL DEFAULT '',
            invoice_date TEXT NOT NULL DEFAULT '',
            amount TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL DEFAULT 'RON',
            status TEXT NOT NULL DEFAULT 'Needs review',
            review_notes TEXT NOT NULL DEFAULT '',
            source_name TEXT NOT NULL DEFAULT '',
            source_path TEXT NOT NULL DEFAULT '',
            file_hash TEXT NOT NULL DEFAULT '',
            duplicate_of INTEGER,
            raw_text TEXT NOT NULL DEFAULT '',
            ocr_confidence REAL,
            split_group TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_invoice_hash ON invoices(file_hash)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_invoice_date ON invoices(invoice_date)")
        # Safe schema evolution for databases created by earlier builds.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(invoices)")}
        additions = {
            "invoice_number": "TEXT NOT NULL DEFAULT ''", "file_hash": "TEXT NOT NULL DEFAULT ''",
            "duplicate_of": "INTEGER", "ocr_confidence": "REAL", "updated_at": "TEXT NOT NULL DEFAULT ''",
            "source_name": "TEXT NOT NULL DEFAULT ''", "review_notes": "TEXT NOT NULL DEFAULT ''"
        }
        for name, typ in additions.items():
            if name not in cols:
                con.execute(f"ALTER TABLE invoices ADD COLUMN {name} {typ}")
        # Migrate the earlier MVP column names without discarding existing records.
        if "filename" in cols:
            con.execute("UPDATE invoices SET source_name=filename WHERE source_name=''")
        if "notes" in cols:
            con.execute("UPDATE invoices SET review_notes=notes WHERE review_notes=''")
        if "created_at" in cols:
            con.execute("UPDATE invoices SET updated_at=created_at WHERE updated_at=''")
        if "field_confidence_json" not in cols:
            con.execute("ALTER TABLE invoices ADD COLUMN field_confidence_json TEXT NOT NULL DEFAULT '{}' ")
        if "verified_fields_json" not in cols:
            con.execute("ALTER TABLE invoices ADD COLUMN verified_fields_json TEXT NOT NULL DEFAULT '{}' ")
        con.execute("""CREATE TABLE IF NOT EXISTS vendor_aliases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL,
            alias TEXT NOT NULL COLLATE NOCASE UNIQUE,
            alias_type TEXT NOT NULL DEFAULT 'abbreviation',
            created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        con.execute("INSERT OR IGNORE INTO app_meta(key,value) VALUES('schema_version','2')")
        con.execute("""CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER, action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""")

def records():
    with db_connect() as con:
        rows = con.execute("""SELECT * FROM invoices
          ORDER BY CASE WHEN length(invoice_date)=10 THEN 0 ELSE 1 END,
          substr(invoice_date,7,4), substr(invoice_date,4,2), substr(invoice_date,1,2), id""").fetchall()
        return [dict(x) for x in rows]

def parse_money(raw: str) -> Optional[float]:
    if not raw: return None
    s = re.sub(r"[^\d,.\-]", "", str(raw)).replace("'", "")
    if not s: return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."): s = s.replace(".", "").replace(",", ".")
        else: s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try: return float(s)
    except ValueError: return None

def fmt_date(raw: str) -> str:
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try: return datetime.strptime(raw, fmt).strftime("%d/%m/%Y")
        except ValueError: pass
    return ""

DATE_RE = re.compile(r"\b(?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:20\d{2})\b|\b20\d{2}-\d{2}-\d{2}\b")
MONEY_RE = re.compile(r"(?<!\w)(?:\d{1,3}(?:[ .]\d{3})+|\d+)[,.]\d{2}(?!\d)")
TOTAL_LABELS = [
    r"\btotal\s*(?:de\s*)?(?:pl[aă]t[aă]|payable|due)\b",
    r"\btotal\s*factur[aă]\b", r"\bgrand\s*total\b", r"\bamount\s*due\b", r"\btotal\s*to\s*pay\b",
    r"\btotal\s*incl(?:usiv)?\s*(?:tva|vat)?\b", r"\btotal\b"
]

@dataclass
class Extraction:
    vendor: str = ""
    invoice_number: str = ""
    invoice_date: str = ""
    amount: str = ""
    currency: str = "RON"
    confidence: float = 0.0
    notes: str = ""
    field_confidence: dict = None

def _clean_ocr_line(line: str) -> str:
    line = re.sub(r"\s+", " ", line).strip(" |:_-")
    # Common OCR substitutions at line edges.
    return line[:180]

def _vendor_score(line: str, idx: int) -> float:
    """Rank likely issuer names near the top of an invoice; conservative."""
    s = line.strip()
    if len(s) < 3 or len(s) > 150: return -100
    upper=s.upper()
    if re.search(r"\b(FACTURA|INVOICE|BON FISCAL|RECEIPT|CUI|CIF|VAT|TVA|NR\.?|SERIA|TEL|TELEFON|WWW|HTTP|EMAIL|ADRESA|CLIENT|CUMPARATOR|FURNIZOR|DATA|TOTAL|SUBTOTAL|PAGINA|PAGE)\b", upper):
        return -20
    if DATE_RE.search(s) or MONEY_RE.search(s): return -20
    if re.fullmatch(r"[\d\s./,:-]+",s): return -20
    score=8.0-idx*0.65
    if any(ch.isalpha() for ch in s): score+=2
    if s.isupper(): score+=1.5
    if re.search(r"\b(SRL|SA|S\.A\.|S\.R\.L\.|LTD|GMBH|INC)\b",upper): score+=2
    if len(s.split())<=5: score+=1
    if re.search(r"(RO\d{2,}|[A-Z]{2,}\d{3,})",upper): score-=1
    return score

def _vendor_alias_rows():
    try:
        with db_connect() as con:
            return [dict(r) for r in con.execute("SELECT canonical_name,alias,alias_type FROM vendor_aliases")]
    except Exception:
        return []

def save_vendor_alias(canonical_name: str, alias: str, alias_type: str = "abbreviation"):
    canonical_name=(canonical_name or "").strip()
    alias=(alias or "").strip()
    if len(canonical_name)<2 or len(alias)<2:
        return False
    with db_connect() as con:
        con.execute("""INSERT INTO vendor_aliases(canonical_name,alias,alias_type,created_at)
            VALUES(?,?,?,?) ON CONFLICT(alias) DO UPDATE SET canonical_name=excluded.canonical_name,
            alias_type=excluded.alias_type""",
            (canonical_name,alias,alias_type,datetime.now().isoformat(timespec="seconds")))
    return True

def _norm_vendor(s):
    s=(s or "").upper()
    s=re.sub(r"[^A-Z0-9ĂÂÎȘȚ]", "", s)
    return s

def _known_vendor_alias_match(text):
    # Match learned abbreviations/brand text conservatively. Exact normalized
    # aliases are preferred; fuzzy matching is intentionally limited to OCR
    # strings of at least 4 characters to avoid false vendor assignments.
    lines=[_clean_ocr_line(x) for x in text.splitlines()[:60]]
    aliases=_vendor_alias_rows()
    matches=[]
    for row in aliases:
        alias=row.get("alias","")
        norm=_norm_vendor(alias)
        if len(norm)<3: continue
        for i,line in enumerate(lines):
            line_norm=_norm_vendor(line)
            if not line_norm: continue
            ratio=difflib.SequenceMatcher(None,norm,line_norm).ratio() if len(line_norm)>=3 else 0
            if norm in line_norm or line_norm in norm or (len(norm)>=4 and ratio>=0.84):
                matches.append((100-i+(ratio*3), row["canonical_name"], alias))
                break
    return max(matches,key=lambda x:x[0]) if matches else None

def extract_fields(text: str, filename: str = "") -> Extraction:
    out = Extraction()
    lines=[_clean_ocr_line(x) for x in text.splitlines()]
    lines=[x for x in lines if x]
    # Prefer explicitly labeled issuer/supplier names when present. Invoice
    # headers often include buyer and seller blocks, so don't simply choose the
    # first company-like line or the first occurrence of "company".
    alias_match=_known_vendor_alias_match(text)
    vendor_labels = r"(?:furnizor|emitent|prestator|v[aâ]nz[aă]tor|supplier|seller|vendor|issued\s+by)"
    vendor_hits=[]
    for i,line in enumerate(lines[:45]):
        match=re.search(vendor_labels+r"\s*[:\-]?\s*(.+)$",line,re.I)
        if match:
            value=re.sub(r"^(?:SC|S\.C\.|SRL|S\.R\.L\.)\s*[:\-]?\s*","",match.group(1),flags=re.I).strip(" .,:;-")
            if len(value)>=3 and not re.search(r"^(?:client|cump[aă]r[aă]tor|buyer|customer)\b",value,re.I):
                vendor_hits.append((12-i*0.08,value))
            elif i+1<len(lines):
                nxt=lines[i+1].strip()
                if len(nxt)>=3 and not re.search(r"^(?:client|cump[aă]r[aă]tor|buyer|customer)\b",nxt,re.I):
                    vendor_hits.append((10-i*0.08,nxt))
    if vendor_hits:
        out.vendor=max(vendor_hits,key=lambda x:x[0])[1][:150]
    elif alias_match:
        out.vendor=alias_match[1][:150]
    else:
        candidates=[(_vendor_score(line,i),line) for i,line in enumerate(lines[:30])]
        candidates=[x for x in candidates if x[0]>0]
        if candidates:
            out.vendor=max(candidates,key=lambda x:x[0])[1]
    # Invoice number: support label/value on same or adjacent line, Romanian
    # serial/invoice terminology and common OCR punctuation variation.
    patterns=[
        r"(?:factur[aă]|invoice|document)\s*(?:nr\.?|no\.?|number|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9./_-]{2,})",
        r"(?:num[aă]r(?:ul)?\s+factur[aă])\s*[:\-]?\s*([A-Z0-9][A-Z0-9./_-]{2,})",
        r"(?:serie\s*(?:[șs]i\s*)?nr\.?|invoice\s*(?:id|#)|num[aă]r\s*(?:document|factur[aă]))\s*[:\-]?\s*([A-Z0-9][A-Z0-9./_-]{2,})"
    ]
    for pat in patterns:
        m=re.search(pat,text,re.I)
        if m:
            out.invoice_number=m.group(1)[:60].strip(" .,:")
            break
    # Date candidates with label proximity and common Romanian label variants.
    candidates=[]
    for m in DATE_RE.finditer(text):
        d=fmt_date(m.group(0))
        if d:
            before=text[max(0,m.start()-100):m.start()].lower()
            after=text[m.end():m.end()+35].lower()
            label_terms=("data fact","data emiter","data emit","invoice date","date of issue","emis","data document","factura din","issue date","data eliber")
            score=5 if any(k in before for k in label_terms) else 1
            if "scadent" in before or "due date" in before: score-=2
            if any(k in after for k in ("data fact","invoice date")): score+=1
            candidates.append((score,m.start(),d))
    if candidates: out.invoice_date=sorted(candidates,key=lambda x:(-x[0],x[1]))[0][2]
    # Payable total: prioritize specific labels and later totals, never subtotal-only lines.
    total_candidates=[]
    strong_labels=[
        r"total\s+de\s+plat[aă]",r"total\s+factur[aă]",r"total\s+de\s+achitat",
        r"total\s+payable",r"amount\s+due",r"total\s+to\s+pay",r"grand\s+total",
        r"total\s+incl(?:usiv)?\s*(?:tva|vat)?",r"total\s+pl[aă]t[aă]",
        r"suma\s+de\s+pl[aă]t[aă]",r"valoare\s+total[aă]",r"total\s+brut",
        r"total\s+cu\s+tva",r"total\s+de\s+achitat"
    ]
    for i,line in enumerate(lines):
        low=line.casefold()
        if re.search(r"subtotal|sub\s*total|f[aă]r[aă]\s*tva|without\s*vat|net\s*total",low): continue
        score=0
        if any(re.search(p,low,re.I) for p in strong_labels): score=5
        elif re.search(r"\btotal\b",low,re.I): score=2
        if score:
            nums=MONEY_RE.findall(line)
            # Also inspect the immediately following line when the label is isolated.
            if not nums and i+1<len(lines): nums=MONEY_RE.findall(lines[i+1])
            if nums: total_candidates.append((score,i,nums[-1]))
    if total_candidates:
        out.amount=sorted(total_candidates,key=lambda x:(-x[0],-x[1]))[0][2]
    else:
        out.notes="Payable total not confidently identified; enter it manually."
    # Currency, prefer explicit currency codes; infer RON only from Romanian lei markers.
    if re.search(r"\b(EUR|EURO)\b|€",text,re.I): out.currency="EUR"
    elif re.search(r"\bUSD\b|US\s*\$",text,re.I): out.currency="USD"
    elif re.search(r"\bHUF\b|\bFt\b",text,re.I): out.currency="HUF"
    elif re.search(r"\bRON\b|\bLEI\b|\bLei\b",text,re.I): out.currency="RON"
    else: out.currency=""
    # Per-field confidence is evidence-based (label match, alias match, candidate
    # ambiguity, OCR confidence are not conflated). These are heuristics, not
    # calibrated probabilities; they are used as review cues, never auto-approval.
    field_conf={}
    field_conf["vendor"]=0.90 if vendor_hits else (0.82 if alias_match else (0.52 if out.vendor else 0.0))
    field_conf["invoice_number"]=0.88 if any(re.search(p,text,re.I) for p in patterns) and out.invoice_number else (0.45 if out.invoice_number else 0.0)
    field_conf["invoice_date"]=0.88 if candidates and max(x[0] for x in candidates)>=5 else (0.65 if out.invoice_date else 0.0)
    field_conf["amount"]=0.92 if total_candidates and max(x[0] for x in total_candidates)>=5 else (0.58 if out.amount else 0.0)
    field_conf["currency"]=0.92 if re.search(r"\b(EUR|EURO|RON|LEI|USD|HUF)\b|€|US\s*\$",text,re.I) else (0.50 if out.currency else 0.0)
    out.field_confidence=field_conf
    out.confidence=sum(bool(x) for x in [out.vendor,out.invoice_number,out.invoice_date,out.amount])/4
    notes=[]
    if not out.vendor: notes.append("Vendor not detected.")
    if not out.invoice_date: notes.append("Invoice date not detected.")
    if not out.amount: notes.append("Payable total not confidently identified.")
    if not out.currency: notes.append("Currency not detected; verify it.")
    if out.amount and re.search(r"\b(subtotal|f[aă]r[aă]\s*TVA|without VAT|net)\b",text,re.I):
        notes.append("Check that the extracted amount includes VAT and is payable.")
    if split_part_info(filename): notes.append("Filename suggests a multi-part scan; verify grouping.")
    out.notes=" ".join(notes)
    return out

def image_pages(path: Path):
    """Yield page images without needlessly upscaling already-rasterized PDFs."""
    import fitz
    from PIL import Image, ImageSequence
    import io
    if path.suffix.lower() == ".pdf":
        doc = fitz.open(path)
        try:
            for page in doc:
                # Scanned invoices are often a single image embedded in the PDF.
                # Extract that original image directly to avoid a costly render
                # (and avoid degrading it through render-then-resize).
                if page.rotation == 0:
                    try:
                        images = page.get_images(full=True)
                        page_area = max(1.0, page.rect.width * page.rect.height)
                        for info in images:
                            xref = info[0]
                            rects = page.get_image_rects(xref)
                            if any((r.width * r.height / page_area) >= 0.82 for r in rects):
                                payload = doc.extract_image(xref)
                                if payload.get("width", 0) >= 1000 and payload.get("height", 0) >= 1000:
                                    with Image.open(io.BytesIO(payload["image"])) as im:
                                        yield im.convert("RGB")
                                    break
                        else:
                            # A text/vector PDF or a page with multiple small images.
                            scale = min(4.0, 3200 / max(page.rect.width, page.rect.height))
                            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                            yield Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    except Exception as e:
                        log_error(f"PDF image extraction fallback for {path.name}: {e}")
                        scale = min(4.0, 3200 / max(page.rect.width, page.rect.height))
                        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                        yield Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                else:
                    scale = min(4.0, 3200 / max(page.rect.width, page.rect.height))
                    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    yield Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()
    else:
        im = Image.open(path)
        try:
            if getattr(im, "n_frames", 1) > 1:
                for frame in ImageSequence.Iterator(im):
                    yield frame.convert("RGB")
            else:
                yield im.convert("RGB")
        finally:
            im.close()


def _text_from_tesseract_data(data):
    """Rebuild OCR text in reading order from one Tesseract data result."""
    lines = {}
    confidences = []
    for i, raw in enumerate(data.get("text", [])):
        word = str(raw or "").strip()
        if not word:
            continue
        key = (
            data.get("page_num", [1] * len(data.get("text", [])))[i],
            data.get("block_num", [0] * len(data.get("text", [])))[i],
            data.get("par_num", [0] * len(data.get("text", [])))[i],
            data.get("line_num", [0] * len(data.get("text", [])))[i],
        )
        lines.setdefault(key, []).append(word)
        try:
            c = float(data.get("conf", ["-1"])[i])
            if c >= 0:
                confidences.append(c)
        except (ValueError, TypeError, IndexError):
            pass
    text = "\n".join(" ".join(words) for words in lines.values())
    confidence = (sum(confidences) / len(confidences) / 100) if confidences else 0.0
    return text, confidence


def _ocr_pass(image, languages, psm, pytesseract):
    config = f"--oem 3 --psm {psm} -c user_defined_dpi=300"
    data = pytesseract.image_to_data(
        image, lang=languages, config=config,
        output_type=pytesseract.Output.DICT
    )
    text, confidence = _text_from_tesseract_data(data)
    useful = len(re.findall(r"[A-Za-zĂÂÎȘȚăâîșț0-9]", text))
    # This score is only used to decide whether another OCR pass is warranted.
    score = confidence + min(useful, 300) / 3000
    if useful < 25:
        score -= 0.25
    return score, confidence, text, useful


def run_ocr(path: Path, tesseract: str, progress=None):
    """Quality-adaptive OCR: one strong pass first, targeted retries only when needed."""
    import pytesseract
    from PIL import ImageOps, ImageEnhance, Image, ImageFilter
    pytesseract.pytesseract.tesseract_cmd = tesseract

    languages = "eng"
    try:
        installed = set(pytesseract.get_languages(config=""))
        if "ron" in installed and "eng" in installed:
            languages = "ron+eng"
        elif "ron" in installed:
            languages = "ron"
        else:
            log_error(f"Romanian Tesseract language pack unavailable for {path.name}; using eng only")
    except Exception:
        languages = "ron+eng"

    # Embedded PDF text avoids rasterization and OCR entirely when it is substantive.
    if path.suffix.lower() == ".pdf":
        try:
            import fitz
            with fitz.open(path) as doc:
                native = [page.get_text("text").strip() for page in doc]
            native_text = "\n\n--- PAGE BREAK ---\n\n".join(
                f"--- PAGE {i+1} ---\n{x}" for i, x in enumerate(native) if x
            )
            useful = sum(len(re.findall(r"[A-Za-zĂÂÎȘȚăâîșț0-9]", x)) for x in native)
            if useful >= 60:
                return native_text, 0.99
        except Exception as e:
            log_error(f"Native PDF text extraction fallback for {path.name}: {e}")

    texts, page_confs = [], []
    pages = image_pages(path)
    for page_no, original in enumerate(pages, 1):
        if progress:
            progress(page_no, None)
        # Rendered scans are already 2x for PDF. Keep a practical pixel ceiling
        # so giant scans don't consume excessive memory or slow Tesseract.
        base = original.convert("RGB")
        w, h = base.size
        max_edge = max(w, h)
        if max_edge > 3400:
            scale = 3400 / max_edge
            base = base.resize((max(1, int(w*scale)), max(1, int(h*scale))), Image.Resampling.LANCZOS)
        elif max_edge < 2500:
            scale = min(1.65, 2500 / max_edge)
            base = base.resize((max(1, int(w*scale)), max(1, int(h*scale))), Image.Resampling.LANCZOS)

        gray = ImageOps.grayscale(base)
        contrast = ImageEnhance.Contrast(gray).enhance(1.45)

        # Crop large blank margins around small receipts/invoices. Use row/column
        # ink density so isolated scanner dust does not define the crop bounds.
        crop_ratio = 1.0
        try:
            # Median filtering removes isolated scanner dust; threshold + bbox
            # then finds the main printed region without adding dependencies.
            ink_mask = gray.filter(ImageFilter.MedianFilter(size=9)).point(
                lambda value: 255 if value < 175 else 0
            )
            bbox = ink_mask.getbbox()
            if bbox:
                x0, y0, x1, y1 = bbox
                pad_x = max(35, int(gray.width * 0.035))
                pad_y = max(35, int(gray.height * 0.035))
                x0=max(0,x0-pad_x); x1=min(gray.width,x1+pad_x)
                y0=max(0,y0-pad_y); y1=min(gray.height,y1+pad_y)
                candidate_crop=gray.crop((x0,y0,x1,y1))
                crop_ratio=(candidate_crop.width*candidate_crop.height)/(gray.width*gray.height)
                # Avoid cropping normal full-page documents or dense tables.
                if crop_ratio < 0.82 and candidate_crop.width > 250 and candidate_crop.height > 150:
                    gray=candidate_crop
                    contrast=ImageEnhance.Contrast(gray).enhance(1.65)
        except Exception as e:
            log_error(f"Content crop skipped ({path.name}, page {page_no}): {e}")

        # Pass 1: normal invoice layout. image_to_data also supplies text and
        # confidence, avoiding the old duplicate image_to_string call per pass.
        candidates = []
        try:
            candidates.append((*_ocr_pass(contrast, languages, 6, pytesseract),
                               "cropped" if crop_ratio < 0.82 else "contrast", 6))
        except Exception as e:
            log_error(f"Primary OCR pass failed ({path.name}, page {page_no}): {e}")

        best = max(candidates, key=lambda x: x[0]) if candidates else None
        # Sparse page layouts are sometimes scanned upside down. Try 180° only
        # when the ink crop is substantially smaller than the page.
        if crop_ratio < 0.82 and (best is None or best[1] < 0.78):
            try:
                rotated = contrast.rotate(180, expand=True)
                result = _ocr_pass(rotated, languages, 6, pytesseract)
                candidates.append((*result, "crop-rot180", 6))
                best = max(candidates, key=lambda x: x[0])
            except Exception as e:
                log_error(f"Rotation OCR check failed ({path.name}, page {page_no}): {e}")

        # Retry only pages where the initial result is sparse or low-confidence.
        # Use alternate layout segmentation and thresholding; don't blanket-run
        # every variant on every page.
        if best is None or best[1] < 0.66 or best[3] < 38:
            # Invoice pages vary between dense tables, single-column text and sparse
            # layouts. Tesseract's own guidance recommends trying page segmentation
            # modes and avoiding aggressive thresholding on clean scans.
            try:
                from PIL import ImageFilter
                sharp = ImageEnhance.Sharpness(contrast).enhance(1.35)
                auto = ImageOps.autocontrast(gray, cutoff=1)
            except Exception:
                sharp, auto = contrast, gray
            retry_images = [
                ("contrast-psm4", contrast, 4),
                ("autocontrast-psm6", auto, 6),
            ]
            # Add a sparse-text segmentation pass only when page layout is likely
            # irregular (e.g. receipts, logo/header text, multi-column blocks).
            if best is None or best[1] < 0.52:
                retry_images.append(("contrast-psm11", contrast, 11))
                retry_images.append(("sharpen-psm6", sharp, 6))
            if best is None or best[1] < 0.48 or best[3] < 35:
                retry_images.extend([
                    ("autocontrast-psm3", auto, 3),
                    ("sharpen-psm11", sharp, 11),
                    ("contrast-psm3", contrast, 3),
                ])
            if best is None or best[1] < 0.34 or best[3] < 20:
                # Keep thresholding as a last-resort candidate; it can harm fine
                # print and light gray text on otherwise readable invoices.
                threshold = contrast.point(lambda x: 255 if x > 175 else 0)
                retry_images.append(("threshold-psm6", threshold, 6))
            for variant_name, image, psm in retry_images:
                try:
                    result = _ocr_pass(image, languages, psm, pytesseract)
                    candidates.append((*result, variant_name, psm))
                except Exception as e:
                    log_error(f"OCR retry failed ({path.name}, page {page_no}, {variant_name}, psm {psm}): {e}")
            if candidates:
                best = max(candidates, key=lambda x: x[0])

        # For exceptionally weak text, test quarter-turn orientation variants.
        # This is deliberately rare because it is much slower than normal OCR.
        if best is None or (best[1] < 0.28 and best[3] < 30):
            for angle in (90, 270):
                try:
                    rotated = contrast.rotate(angle, expand=True)
                    result = _ocr_pass(rotated, languages, 6, pytesseract)
                    candidates.append((*result, f"rotate-{angle}", 6))
                except Exception as e:
                    log_error(f"Orientation OCR retry failed ({path.name}, page {page_no}, angle {angle}): {e}")
            if candidates:
                best = max(candidates, key=lambda x: x[0])

        if best is None:
            raise RuntimeError("Tesseract could not produce text from this page. Check OCR language packs.")

        # Header/footer OCR is expensive, so reserve it for low-confidence pages.
        extra = ""
        if best[1] < 0.62 or best[3] < 55:
            try:
                cw, ch = contrast.size
                regions = [
                    ("header", contrast.crop((0, 0, cw, int(ch*0.34)))),
                    ("footer", contrast.crop((0, int(ch*0.62), cw, ch))),
                ]
                crop_results = []
                for region_name, crop in regions:
                    if crop.width < 20 or crop.height < 20:
                        continue
                    try:
                        crop_text = pytesseract.image_to_string(
                            crop, lang=languages, config="--oem 3 --psm 6"
                        ).strip()
                        if len(re.sub(r"\s", "", crop_text)) > 8:
                            crop_results.append(f"--- OCR {region_name.upper()} REGION ---\n{crop_text}")
                    except Exception as e:
                        log_error(f"OCR crop failed ({path.name}, page {page_no}, {region_name}): {e}")
                if crop_results:
                    extra = "\n\n" + "\n\n".join(crop_results)
            except Exception as e:
                log_error(f"OCR crop processing failed ({path.name}, page {page_no}): {e}")

        texts.append(f"--- PAGE {page_no} ---\n{best[2]}{extra}")
        page_confs.append(best[1])
        log_error(
            f"OCR selected {path.name} page {page_no}: variant={best[4]}, "
            f"psm={best[5]}, text_conf={best[1]:.2f}, retries={len(candidates)-1}"
        )
    return "\n\n--- PAGE BREAK ---\n\n".join(texts), (
        sum(page_confs) / len(page_confs) if page_confs else 0.0
    )

def safe_extract_zip(zip_path: Path, dest: Path):
    dest.mkdir(parents=True,exist_ok=True)
    extracted=[]
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir(): continue
            # Reject absolute paths, parent traversal, symlinks and absurdly large expansion.
            pp=PurePosixPath(info.filename.replace("\\","/"))
            if pp.is_absolute() or ".." in pp.parts: continue
            mode=(info.external_attr >> 16) & 0o170000
            if mode == 0o120000: continue
            if info.file_size > 150*1024*1024: continue
            target=(dest / Path(*pp.parts)).resolve()
            if dest.resolve() not in target.parents: continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with z.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src,dst)
            if target.suffix.lower() in SUPPORTED: extracted.append(target)
    return extracted

def collect_inputs(paths, batch_dir):
    files=[]
    for raw in paths:
        p=Path(raw)
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and f.suffix.lower() in SUPPORTED: files.append(f)
        elif p.suffix.lower()==".zip":
            files.extend(safe_extract_zip(p,batch_dir/(p.stem+"_contents")))
        elif p.suffix.lower() in SUPPORTED: files.append(p)
    return sorted(set(files), key=lambda x:x.name.casefold())

def preserved_copy(source: Path, batch: Path) -> Path:
    # Keep source originals available even when input came from a temporary folder.
    target_dir=batch/"files"
    target_dir.mkdir(parents=True,exist_ok=True)
    target=target_dir/source.name
    if target.exists():
        target=target_dir/(source.stem+"_"+hashlib.md5(str(source).encode()).hexdigest()[:8]+source.suffix)
    shutil.copy2(source,target)
    return target


def split_part_info(filename: str):
    """Return (normalized grouping key, part, total) for common split naming conventions."""
    stem=Path(filename).stem
    patterns=[
        r"(?i)(.*?)[ _-]*(?:part|pagina|page|scan)?[ _-]*(\d+)[ _-]*(?:of|out of|din|de|_)[ _-]*(\d+)$",
        r"(?i)(.*?)[ _-]*(\d+)[ _-]*din[ _-]*(\d+)$",
        r"(?i)(.*?)[ _-]*(\d+)[ _-]*of[ _-]*(\d+)$",
    ]
    for pat in patterns:
        m=re.match(pat,stem)
        if m:
            base=re.sub(r"[^a-z0-9]+","_",m.group(1).casefold()).strip("_")
            part,total=int(m.group(2)),int(m.group(3))
            if total>1 and 1<=part<=total:
                return (base or "invoice",part,total)
    # Common “1 of 2” phrase can appear in file title.
    m=re.search(r"(?i)(.*?)(\d+)\s*(?:out\s*of|of|din)\s*(\d+)",stem)
    if m:
        base=re.sub(r"[^a-z0-9]+","_",m.group(1).casefold()).strip("_")
        part,total=int(m.group(2)),int(m.group(3))
        if total>1 and 1<=part<=total:return (base or "invoice",part,total)
    return None

def duplicate_candidate(data):
    """Return a likely duplicate record based on vendor+invoice number or vendor/date/amount."""
    vendor=(data.get("vendor") or "").strip().casefold()
    number=(data.get("invoice_number") or "").strip().casefold()
    date=(data.get("invoice_date") or "").strip()
    amount=parse_money(data.get("amount",""))
    currency=(data.get("currency") or "RON").strip().upper()
    if not vendor:return None
    with db_connect() as con:
        rows=con.execute("SELECT id,vendor,invoice_number,invoice_date,amount,currency FROM invoices WHERE vendor<>''").fetchall()
    for r in rows:
        rv=(r["vendor"] or "").strip().casefold()
        rn=(r["invoice_number"] or "").strip().casefold()
        ra=parse_money(r["amount"])
        if number and rn and number==rn and vendor==rv:
            return (r["id"],"same vendor and invoice number")
        if date and date==r["invoice_date"] and amount is not None and ra is not None and abs(amount-ra)<0.005 and currency==str(r["currency"]).upper() and vendor==rv:
            return (r["id"],"same vendor, date, amount and currency")
    return None

def audit(invoice_id, action, details=""):
    with db_connect() as con:
        con.execute("INSERT INTO audit_log(invoice_id,action,details,created_at) VALUES(?,?,?,?)",
                    (invoice_id,action,details,datetime.now().isoformat(timespec="seconds")))

def insert_invoice(data: dict) -> tuple[int,bool]:
    now=datetime.now().isoformat(timespec="seconds")
    with db_connect() as con:
        dup=con.execute("SELECT id FROM invoices WHERE file_hash=? AND file_hash<>''",(data["file_hash"],)).fetchone()
        status=data.get("status","Needs review")
        notes=data.get("review_notes","")
        duplicate_of=None
        possible=duplicate_candidate(data) if not dup else None
        if dup:
            duplicate_of=dup["id"]
            status="Needs review"
            notes=(notes+" " if notes else "")+f"Exact duplicate file of invoice record #{duplicate_of}."
        elif possible:
            duplicate_of=possible[0]
            status="Needs review"
            notes=(notes+" " if notes else "")+f"Possible duplicate of record #{duplicate_of} ({possible[1]}). Verify manually."
        cur=con.execute("""INSERT INTO invoices
          (vendor,invoice_number,invoice_date,amount,currency,status,review_notes,source_name,source_path,
           file_hash,duplicate_of,raw_text,ocr_confidence,split_group,field_confidence_json,verified_fields_json,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (data.get("vendor",""),data.get("invoice_number",""),data.get("invoice_date",""),
           data.get("amount",""),data.get("currency","RON"),status,notes,data["source_name"],
           data["source_path"],data["file_hash"],duplicate_of,data.get("raw_text",""),
           data.get("ocr_confidence",0),data.get("split_group",""),
           json.dumps(data.get("field_confidence",{}),ensure_ascii=False),
           json.dumps(data.get("verified_fields",{}),ensure_ascii=False),now,now))
        return cur.lastrowid, bool(dup)

def update_invoice(i, values, verified_fields=None):
    with db_connect() as con:
        if verified_fields is None:
            con.execute("""UPDATE invoices SET vendor=?,invoice_number=?,invoice_date=?,amount=?,currency=?,
              status=?,review_notes=?,updated_at=? WHERE id=?""",
              (*values,datetime.now().isoformat(timespec="seconds"),i))
        else:
            con.execute("""UPDATE invoices SET vendor=?,invoice_number=?,invoice_date=?,amount=?,currency=?,
              status=?,review_notes=?,verified_fields_json=?,updated_at=? WHERE id=?""",
              (*values,json.dumps(verified_fields,ensure_ascii=False),datetime.now().isoformat(timespec="seconds"),i))
    audit(i,"reviewed",f"status={values[5]}")

def delete_invoice(i):
    audit(i,"deleted","record deleted by user")
    with db_connect() as con:
        con.execute("DELETE FROM invoices WHERE id=?",(i,))

def export_xlsx(path: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    rows=records()
    wb=Workbook()
    ws=wb.active; ws.title="General Tracker"
    headers=["Invoice date","Vendor / name","Invoice number","Total payable","Currency","Status","Source file","Review notes"]
    def fill_sheet(sh, data):
        sh.append(headers)
        for r in data:
            sh.append([r["invoice_date"],r["vendor"],r["invoice_number"],parse_money(r["amount"]) if parse_money(r["amount"]) is not None else r["amount"],
                       r["currency"],r["status"],r["source_name"],r["review_notes"]])
        sh.freeze_panes="A2"; sh.auto_filter.ref=sh.dimensions
        for c in sh[1]:
            c.font=Font(bold=True,color="FFFFFF")
            c.fill=PatternFill("solid",fgColor="24476B")
        widths=[16,32,20,18,12,22,38,58]
        for n,w in enumerate(widths,1): sh.column_dimensions[get_column_letter(n)].width=w
        for row in sh.iter_rows():
            for c in row: c.alignment=Alignment(vertical="top",wrap_text=True)
    fill_sheet(ws,rows)
    months={}
    for r in rows:
        try:
            d=datetime.strptime(r["invoice_date"],"%d/%m/%Y")
            months.setdefault(d.strftime("%Y-%m"),[]).append(r)
        except ValueError: pass
    for key,vals in sorted(months.items()):
        sh=wb.create_sheet(datetime.strptime(key,"%Y-%m").strftime("%B %Y"))
        fill_sheet(sh,vals)
        sums={}
        for r in vals:
            if r["status"]!="Confirmed": continue
            n=parse_money(r["amount"])
            if n is not None: sums[r["currency"]]=sums.get(r["currency"],0)+n
        sh.append([]); sh.append(["Monthly totals by currency"])
        for cur,total in sorted(sums.items()): sh.append([cur,round(total,2)])
    review=wb.create_sheet("Needs Review")
    review.append(["Record ID","Source file","Vendor","Date","Amount","Currency","Issue / notes","Duplicate of"])
    for r in rows:
        if r["status"]!="Confirmed" or r["review_notes"] or r["duplicate_of"]:
            review.append([r["id"],r["source_name"],r["vendor"],r["invoice_date"],r["amount"],r["currency"],r["review_notes"],r["duplicate_of"]])
    fill_headers=review[1]
    for c in fill_headers:
        c.font=Font(bold=True,color="FFFFFF"); c.fill=PatternFill("solid",fgColor="24476B")
    for n,w in enumerate([12,38,30,16,16,12,60,16],1): review.column_dimensions[get_column_letter(n)].width=w
    review.freeze_panes="A2"; review.auto_filter.ref=review.dimensions
    # Summary includes only confirmed rows; unconfirmed amounts are intentionally excluded.
    sums={}
    dates=[]
    for r in rows:
        if r["status"]!="Confirmed": continue
        n=parse_money(r["amount"])
        if n is not None: sums[r["currency"]]=sums.get(r["currency"],0)+n
        try: dates.append(datetime.strptime(r["invoice_date"],"%d/%m/%Y"))
        except ValueError: pass
    ws.append([]); ws.append(["Confirmed purchase totals by currency"])
    for cur,total in sorted(sums.items()): ws.append([cur,round(total,2)])
    ws.append(["Note", "Only Confirmed records are included. Duplicate flags are advisory; verify and confirm only one copy if appropriate."])
    ws.append([])
    ws.append(["Purchase period"])
    if dates:
        ws.append(["Earliest invoice",min(dates).strftime("%d/%m/%Y")])
        ws.append(["Latest invoice",max(dates).strftime("%d/%m/%Y")])
    wb.save(path)

class ReviewDialog(tk.Toplevel):
    def __init__(self, parent, row):
        super().__init__(parent)
        self.title(f"Review invoice #{row['id']}")
        self.geometry("1000x760")
        self.configure(background="#111318")
        self.transient(parent); self.grab_set()
        self.result=None
        left=ttk.Frame(self,padding=10); left.pack(side="left",fill="y")
        right=ttk.Frame(self,padding=10); right.pack(side="right",fill="both",expand=True)
        self.vars={}
        fields=[("Vendor","vendor"),("Invoice number","invoice_number"),("Invoice date (DD/MM/YYYY)","invoice_date"),
                ("Total payable","amount"),("Currency","currency"),("Status","status")]
        for label,key in fields:
            ttk.Label(left,text=label).pack(anchor="w",pady=(8,2))
            v=tk.StringVar(value=row.get(key,"")); self.vars[key]=v
            if key=="status":
                w=ttk.Combobox(left,textvariable=v,values=["Needs review","Confirmed"],state="readonly",width=34)
            else: w=ttk.Entry(left,textvariable=v,width=38)
            w.pack(fill="x")
        ttk.Label(left,text="Review notes").pack(anchor="w",pady=(8,2))
        self.notes=tk.Text(left,width=40,height=8,bg="#1B1F27",fg="#E5E9F0",insertbackground="#FFFFFF",relief="flat",highlightthickness=1,highlightbackground="#343B47"); self.notes.pack(fill="x")
        self.notes.insert("1.0",row.get("review_notes",""))
        ttk.Label(left,text=f"OCR confidence indicator: {row.get('ocr_confidence',0):.0%} (rough text confidence, not field accuracy)",
                  wraplength=280).pack(anchor="w",pady=8)
        ttk.Button(left,text="Save review",command=self.save).pack(anchor="e",pady=12)
        ttk.Label(right,text="Original OCR text",font=("Segoe UI",12,"bold")).pack(anchor="w")
        txt=tk.Text(right,wrap="word",bg="#191D24",fg="#DDE3EC",insertbackground="#FFFFFF",relief="flat",highlightthickness=1,highlightbackground="#343B47",font=("Consolas",10)); txt.pack(fill="both",expand=True)
        txt.insert("1.0",row.get("raw_text","")); txt.config(state="disabled")
    def save(self):
        date=self.vars["invoice_date"].get().strip()
        if date and not fmt_date(date):
            messagebox.showerror("Invalid date","Use DD/MM/YYYY, for example 07/11/2026.",parent=self); return
        amount=self.vars["amount"].get().strip()
        if amount and parse_money(amount) is None:
            messagebox.showerror("Invalid amount","Enter a valid numeric amount.",parent=self); return
        if self.vars["status"].get()=="Confirmed":
            missing=[k for k in ("vendor","invoice_date","amount","currency") if not self.vars[k].get().strip()]
            if missing:
                messagebox.showerror("Cannot confirm","Fill in vendor, date, amount, and currency first.",parent=self); return
        self.result=([self.vars[k].get().strip() for k in
            ("vendor","invoice_number","invoice_date","amount","currency","status")]+[self.notes.get("1.0","end").strip()])
        self.destroy()

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1540x900"); self.minsize(1180,700)
        init_db()
        self.busy=False
        self.build_ui(); self.refresh()
        self.protocol("WM_DELETE_WINDOW",self.on_close)

    def build_ui(self):
        style=ttk.Style(self)
        try: style.theme_use("clam")
        except tk.TclError: pass
        self.configure(background="#0B0D12")
        style.configure(".",font=("Segoe UI",10),background="#0B0D12",foreground="#E6EAF2")
        style.configure("TFrame",background="#0B0D12")
        style.configure("Panel.TFrame",background="#141821")
        style.configure("TLabel",background="#0B0D12",foreground="#E6EAF2")
        style.configure("Title.TLabel",font=("Segoe UI",25,"bold"),foreground="#F5F7FB")
        style.configure("Sub.TLabel",font=("Segoe UI",9),foreground="#8E98AA")
        style.configure("Section.TLabel",font=("Segoe UI",11,"bold"),foreground="#DCE3EF")
        style.configure("TButton",padding=(12,8),font=("Segoe UI",10),background="#202633",foreground="#E6EAF2",borderwidth=0)
        style.map("TButton",background=[("active","#30394A"),("disabled","#171B23")],foreground=[("disabled","#687386")])
        style.configure("Accent.TButton",background="#647EF5",foreground="#FFFFFF",padding=(16,10),font=("Segoe UI",10,"bold"))
        style.map("Accent.TButton",background=[("active","#7890FF")])
        style.configure("TEntry",fieldbackground="#1A202B",foreground="#F0F3F8",insertcolor="#FFFFFF",padding=8,borderwidth=1)
        style.configure("TCombobox",fieldbackground="#1A202B",background="#202633",foreground="#F0F3F8",arrowcolor="#B8C3D5",padding=7)
        style.map("TCombobox",fieldbackground=[("readonly","#1A202B")],foreground=[("readonly","#F0F3F8")])
        style.configure("Treeview",background="#151A23",fieldbackground="#151A23",foreground="#DDE4EF",
                        rowheight=38,borderwidth=0,font=("Segoe UI",10))
        style.configure("Treeview.Heading",background="#222A38",foreground="#BFCBE0",
                        font=("Segoe UI",9,"bold"),padding=(10,12),relief="flat")
        style.map("Treeview",background=[("selected","#344B7B")],foreground=[("selected","#FFFFFF")])
        style.configure("Vertical.TScrollbar",background="#252E3D",troughcolor="#0B0D12",arrowcolor="#AAB6CA")
        style.configure("Horizontal.TScrollbar",background="#252E3D",troughcolor="#0B0D12",arrowcolor="#AAB6CA")

        # App shell: compact title area, clear primary actions, spreadsheet workspace.
        shell=ttk.Frame(self,padding=(24,20,24,16)); shell.pack(fill="both",expand=True)
        top=ttk.Frame(shell); top.pack(fill="x")
        brand=ttk.Frame(top); brand.pack(side="left")
        ttk.Label(brand,text="INVOICE WORKSPACE",style="Sub.TLabel").pack(anchor="w")
        ttk.Label(brand,text="Invoice OCR",style="Title.TLabel").pack(anchor="w",pady=(2,0))
        ttk.Label(brand,text="Local-first invoice processing  ·  Review, organize, export",style="Sub.TLabel").pack(anchor="w",pady=(2,0))
        actions=ttk.Frame(top); actions.pack(side="right",anchor="center")
        ttk.Button(actions,text="＋  Import invoices",style="Accent.TButton",command=self.import_files).pack(side="left",padx=(0,8))
        ttk.Button(actions,text="Import folder",command=self.import_folder).pack(side="left",padx=4)
        ttk.Button(actions,text="Export Excel",command=self.export).pack(side="left",padx=4)
        ttk.Button(actions,text="Backup",command=self.backup).pack(side="left",padx=4)

        # Small summary cards provide status at a glance.
        cards=ttk.Frame(shell); cards.pack(fill="x",pady=(18,14))
        self.card_total=self._summary_card(cards,"INVOICES","0","All imported records")
        self.card_review=self._summary_card(cards,"NEEDS REVIEW","0","Fields need checking")
        self.card_confirmed=self._summary_card(cards,"CONFIRMED","0","Ready for Excel totals")
        self.card_selection=self._summary_card(cards,"SELECTED","0","Rows in current selection")

        # Table controls.
        controls=ttk.Frame(shell); controls.pack(fill="x",pady=(2,10))
        ttk.Label(controls,text="INVOICE REGISTER",style="Section.TLabel").pack(side="left")
        ttk.Label(controls,text="  ·  Spreadsheet view",style="Sub.TLabel").pack(side="left",padx=(6,12))
        self.search=tk.StringVar()
        ttk.Label(controls,text="Search",style="Sub.TLabel").pack(side="left",padx=(12,5))
        ent=ttk.Entry(controls,textvariable=self.search,width=28)
        ent.pack(side="left",padx=(2,8))
        ent.bind("<KeyRelease>",lambda e:self.refresh())
        self.filter=tk.StringVar(value="All records")
        cb=ttk.Combobox(controls,textvariable=self.filter,values=["All records","Needs review","Confirmed","Possible duplicate"],state="readonly",width=18)
        cb.pack(side="left",padx=4); cb.bind("<<ComboboxSelected>>",lambda e:self.refresh())
        ttk.Button(controls,text="Clear filters",command=self.clear_filters).pack(side="left",padx=4)
        ttk.Label(controls,text="Click a header to sort  ·  Double-click a cell to edit",style="Sub.TLabel").pack(side="right")

        table_panel=ttk.Frame(shell,style="Panel.TFrame",padding=1); table_panel.pack(fill="both",expand=True)
        table_wrap=ttk.Frame(table_panel,style="Panel.TFrame"); table_wrap.pack(fill="both",expand=True)
        cols=("date","vendor","number","amount","currency","status","confidence","file","notes")
        self.tree=ttk.Treeview(table_wrap,columns=cols,show="headings",selectmode="extended")
        labels=("Invoice date","Vendor / merchant","Invoice #","Payable total","Currency","Review status","OCR signal","Source file","Review notes")
        widths=(125,230,145,135,85,145,105,260,320)
        self._sort_state={}
        for c,l,w in zip(cols,labels,widths):
            self.tree.heading(c,text=l,command=lambda col=c:self.sort_by_column(col))
            self.tree.column(c,width=w,minwidth=70,anchor="w",stretch=True)
        ybar=ttk.Scrollbar(table_wrap,orient="vertical",command=self.tree.yview)
        xbar=ttk.Scrollbar(table_wrap,orient="horizontal",command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set,xscrollcommand=xbar.set)
        self.tree.grid(row=0,column=0,sticky="nsew")
        ybar.grid(row=0,column=1,sticky="ns")
        xbar.grid(row=1,column=0,sticky="ew")
        table_wrap.rowconfigure(0,weight=1); table_wrap.columnconfigure(0,weight=1)
        self.tree.tag_configure("even",background="#151A23",foreground="#DDE4EF")
        self.tree.tag_configure("odd",background="#191F2A",foreground="#DDE4EF")
        self.tree.tag_configure("review",foreground="#F0C879")
        self.tree.tag_configure("confirmed",foreground="#91D7B0")
        self.tree.tag_configure("duplicate",foreground="#FF938E")
        self.tree.bind("<Button-3>",self.show_context_menu)
        self.tree.bind("<Double-1>",self.edit_cell)
        self.tree.bind("<Return>",lambda e:self.review())
        self.tree.bind("<Delete>",lambda e:self.delete())
        self.tree.bind("<Control-a>",lambda e:self.select_all())
        self.tree.bind("<Control-A>",lambda e:self.select_all())
        self.tree.bind("<Control-c>",lambda e:self.copy_selected())
        self.tree.bind("<<TreeviewSelect>>",lambda e:self.update_selection_status())

        # Bottom action dock.
        dock=ttk.Frame(shell); dock.pack(fill="x",pady=(12,0))
        left=ttk.Frame(dock); left.pack(side="left")
        for label,cmd in [("Review",self.review),("Re-scan",self.rescan_selected),("Merge parts",self.merge_selected),("Open originals",self.open_original),("Copy rows",self.copy_selected),("Delete",self.delete),("Select all",self.select_all)]:
            ttk.Button(left,text=label,command=cmd).pack(side="left",padx=(0,5))
        self.status=ttk.Label(dock,text="Ready",style="Sub.TLabel"); self.status.pack(side="left",padx=12)
        self.import_progress=ttk.Progressbar(dock,mode="determinate",length=180,maximum=100)
        self.import_progress.pack(side="left",padx=(10,4))
        self.progress_label=ttk.Label(dock,text="",style="Sub.TLabel"); self.progress_label.pack(side="left",padx=4)
        self.selection_status=ttk.Label(dock,text="0 selected",style="Sub.TLabel"); self.selection_status.pack(side="left",padx=8)
        self.totals=ttk.Label(dock,text="",style="Sub.TLabel"); self.totals.pack(side="right")
        self.bind("<Escape>",lambda e:self.clear_filters())

    def _summary_card(self,parent,label,value,subtitle):
        card=ttk.Frame(parent,style="Panel.TFrame",padding=(16,12))
        card.pack(side="left",fill="x",expand=True,padx=(0,9))
        ttk.Label(card,text=label,style="Sub.TLabel").pack(anchor="w")
        value_label=ttk.Label(card,text=value,font=("Segoe UI",19,"bold"),foreground="#F4F6FA",background="#141821")
        value_label.pack(anchor="w",pady=(3,0))
        ttk.Label(card,text=subtitle,style="Sub.TLabel").pack(anchor="w",pady=(2,0))
        return value_label

    def clear_filters(self):
        self.search.set("")
        self.filter.set("All records")
        self.refresh()

    def sort_by_column(self,col):
        rows=[(self.tree.set(i,col),i) for i in self.tree.get_children("")]
        reverse=self._sort_state.get(col,False)
        def key(item):
            val=item[0].strip()
            if col=="amount":
                n=parse_money(val)
                return (n is None,n if n is not None else 0)
            if col=="date":
                d=fmt_date(val)
                try: return (0,datetime.strptime(d,"%d/%m/%Y")) if d else (1,val)
                except ValueError: return (1,val)
            if col=="confidence":
                try:return float(val.rstrip("%"))
                except:return -1
            return val.casefold()
        rows.sort(key=key,reverse=reverse)
        for pos,(_,iid) in enumerate(rows): self.tree.move(iid,"",pos)
        self._sort_state[col]=not reverse
    def refresh(self):
        for x in self.tree.get_children(): self.tree.delete(x)
        q=self.search.get().casefold()
        filt=self.filter.get()
        rows=records()
        for r in rows:
            if q and not q in " ".join(str(r.get(k,"")) for k in ("vendor","invoice_number","invoice_date","source_name","amount","review_notes")).casefold(): continue
            if filt=="Needs review" and r["status"]=="Confirmed": continue
            if filt=="Confirmed" and r["status"]!="Confirmed": continue
            if filt=="Possible duplicate" and not r["duplicate_of"]: continue
            tags=("confirmed",) if r["status"]=="Confirmed" else ("review",)
            tags=(("even" if len(self.tree.get_children())%2==0 else "odd"),)+tags
            if r["duplicate_of"]: tags=("duplicate",)+tags
            self.tree.insert("","end",iid=str(r["id"]),tags=tags,values=(r["invoice_date"],r["vendor"],r["invoice_number"],r["amount"],r["currency"],r["status"],f"{(r['ocr_confidence'] or 0):.0%}",r["source_name"],r["review_notes"]))
        sums={}
        for r in rows:
            if r["status"]=="Confirmed":
                n=parse_money(r["amount"])
                if n is not None: sums[r["currency"]]=sums.get(r["currency"],0)+n
        if hasattr(self,"card_total"):
            self.card_total.config(text=str(len(rows)))
            self.card_review.config(text=str(sum(r["status"]!="Confirmed" for r in rows)))
            self.card_confirmed.config(text=str(sum(r["status"]=="Confirmed" for r in rows)))
        self.totals.config(text="Confirmed totals: "+" | ".join(f"{k} {v:,.2f}" for k,v in sorted(sums.items())))
        self.update_selection_status()
    def show_context_menu(self,event):
        row=self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        menu=tk.Menu(self,tearoff=0,bg="#252932",fg="#E7EAF0",activebackground="#344A76",activeforeground="#FFFFFF")
        menu.add_command(label="Review selected",command=self.review)
        menu.add_command(label="Re-scan selected with OCR",command=self.rescan_selected)
        menu.add_command(label="Open source files",command=self.open_original)
        menu.add_command(label="Copy selected rows",command=self.copy_selected)
        menu.add_separator()
        menu.add_command(label="Delete selected",command=self.delete)
        try: menu.tk_popup(event.x_root,event.y_root)
        finally: menu.grab_release()

    def edit_cell(self,event):
        """Excel-like inline editing for common invoice fields."""
        row_id=self.tree.identify_row(event.y)
        col_id=self.tree.identify_column(event.x)
        if not row_id or not col_id: return
        col_index=int(col_id[1:])-1
        columns=list(self.tree["columns"])
        col=columns[col_index]
        editable={"date":"invoice_date","vendor":"vendor","number":"invoice_number",
                  "amount":"amount","currency":"currency","status":"status","notes":"review_notes"}
        field=editable.get(col)
        if not field: 
            self.review(); return
        x,y,w,h=self.tree.bbox(row_id,col_id)
        if not w: return
        old=self.tree.set(row_id,col)
        editor=ttk.Entry(self.tree)
        editor.insert(0,old); editor.select_range(0,"end")
        editor.place(x=x,y=y,width=w,height=h)
        editor.focus_set()
        def finish(save=True):
            value=editor.get().strip()
            editor.destroy()
            if not save or value==old: return
            invoice_id=int(row_id)
            if field=="invoice_date" and value and not fmt_date(value):
                messagebox.showerror("Invalid date","Use DD/MM/YYYY.",parent=self); return
            if field=="amount" and value and parse_money(value) is None:
                messagebox.showerror("Invalid amount","Enter a valid amount.",parent=self); return
            if field=="status" and value not in ("Needs review","Confirmed"):
                messagebox.showerror("Invalid status","Choose Needs review or Confirmed.",parent=self); return
            with db_connect() as con:
                con.execute(f"UPDATE invoices SET {field}=?,updated_at=? WHERE id=?",
                            (value,datetime.now().isoformat(timespec="seconds"),invoice_id))
            audit(invoice_id,"inline_edit",f"{field} updated in spreadsheet view")
            self.refresh()
            self.status.config(text=f"Updated {field.replace('_',' ')}")
        editor.bind("<Return>",lambda e:finish(True))
        editor.bind("<Escape>",lambda e:finish(False))
        editor.bind("<FocusOut>",lambda e:finish(True))

    def selected_ids(self):
        """Return all selected invoice IDs in the visible table."""
        return [int(x) for x in self.tree.selection()]
    def selected(self):
        ids=self.selected_ids()
        return ids[0] if ids else None
    def update_selection_status(self):
        if hasattr(self,"selection_status"):
            n=len(self.tree.selection())
            self.selection_status.config(text=f"{n} selected")
            if hasattr(self,"card_selection"): self.card_selection.config(text=str(n))
    def select_all(self):
        children=self.tree.get_children()
        if children:
            self.tree.selection_set(children)
        self.update_selection_status()
        return "break"
    def copy_selected(self):
        ids=self.selected_ids()
        if not ids:
            messagebox.showinfo("Copy rows","Select one or more invoice rows first."); return
        wanted=set(ids)
        rows=[r for r in records() if r["id"] in wanted]
        headers=["Date","Vendor","Invoice #","Total payable","Currency","Status","Source file","Review notes"]
        lines=["\t".join(headers)]
        for r in rows:
            vals=[r.get("invoice_date",""),r.get("vendor",""),r.get("invoice_number",""),r.get("amount",""),r.get("currency",""),r.get("status",""),r.get("source_name",""),r.get("review_notes","")]
            lines.append("\t".join(str(v or "").replace("\t"," ").replace("\r"," ").replace("\n"," ") for v in vals))
        self.clipboard_clear(); self.clipboard_append("\n".join(lines))
        self.status.config(text=f"Copied {len(rows)} row(s) to clipboard")
    def review(self):
        ids=self.selected_ids()
        if not ids: return
        row_map={r["id"]:r for r in records()}
        changed=0
        # Review every selected record one at a time; keep going even if a dialog is cancelled.
        for i in ids:
            row=row_map.get(i)
            if not row: continue
            dlg=ReviewDialog(self,row); self.wait_window(dlg)
            if dlg.result:
                update_invoice(i,dlg.result); changed+=1
        self.refresh()
        self.status.config(text=f"Reviewed {changed} of {len(ids)} selected record(s)")
    def rescan_selected(self):
        ids=self.selected_ids()
        if not ids:
            messagebox.showinfo("Re-scan OCR","Select one or more invoice records first."); return
        tesseract=find_tesseract()
        if not tesseract:
            messagebox.showerror("OCR engine missing","Tesseract is not installed or could not be found."); return
        row_map={r["id"]:r for r in records()}
        chosen=[row_map[i] for i in ids if i in row_map]
        if not messagebox.askyesno("Re-scan selected invoices",
            f"Run the enhanced OCR passes on {len(chosen)} selected record(s)?\n\nExisting non-empty fields will be preserved; only missing fields will be filled."):
            return
        self.busy=True
        self.status.config(text=f"Re-scanning {len(chosen)} selected invoice(s)…")
        threading.Thread(target=self.rescan_worker,args=(chosen,tesseract),daemon=True).start()

    def rescan_worker(self,chosen,tesseract):
        updated=failed=0
        for n,row in enumerate(chosen,1):
            try:
                paths=[Path(x) for x in (row.get("source_path") or "").splitlines() if x.strip()]
                paths=[p for p in paths if p.exists()]
                if not paths: raise FileNotFoundError("No saved source file could be found")
                texts=[]; confidences=[]
                for p in paths:
                    raw,conf=run_ocr(p,tesseract); texts.append(raw); confidences.append(conf)
                raw="\n\n===== RESCANNED SOURCE =====\n\n".join(texts)
                fields=extract_fields(raw,row.get("source_name",""))
                # Never overwrite user-entered values. Only backfill blank fields.
                vals={k:(row.get(k) or getattr(fields,k)) for k in ("vendor","invoice_number","invoice_date","amount")}
                vals["currency"]=row.get("currency") or fields.currency or ""
                notes=(row.get("review_notes") or "").strip()
                notes=re.sub(r"(?:Enhanced OCR rescan completed[^.]*\.?)", "", notes).strip()
                if fields.notes:
                    notes=(notes+" " if notes else "")+fields.notes
                conf=sum(confidences)/len(confidences) if confidences else 0
                with db_connect() as con:
                    con.execute("""UPDATE invoices SET vendor=?,invoice_number=?,invoice_date=?,amount=?,currency=?,raw_text=?,ocr_confidence=?,review_notes=?,updated_at=? WHERE id=?""",
                        (vals["vendor"],vals["invoice_number"],vals["invoice_date"],vals["amount"],vals["currency"],raw,conf,notes,datetime.now().isoformat(timespec="seconds"),row["id"]))
                audit(row["id"],"ocr_rescan",f"sources={len(paths)}; text_conf={conf:.2f}")
                updated+=1
            except Exception as e:
                failed+=1; log_error(f"OCR rescan failed for invoice #{row['id']}: {type(e).__name__}: {e}")
            self.after(0,lambda n=n,total=len(chosen):self.status.config(text=f"Re-scanning invoice {n}/{total}…"))
        self.after(0,lambda:self.rescan_finished(updated,failed))

    def rescan_finished(self,updated,failed):
        self.busy=False; self.refresh()
        self.status.config(text=f"OCR re-scan finished • {updated} updated • {failed} errors")
        messagebox.showinfo("OCR re-scan complete",f"Records re-scanned: {updated}\nErrors: {failed}\n\nExisting non-empty fields were preserved. Check the original invoice before confirming.")

    def merge_selected(self):
        ids=self.selected_ids()
        if len(ids)<2:
            messagebox.showinfo("Merge invoices","Select at least two records that belong to the same invoice."); return
        row_map={r["id"]:r for r in records()}
        chosen=[row_map[i] for i in ids if i in row_map]
        names=[r.get("source_name","") for r in chosen]
        if not messagebox.askyesno("Merge invoice parts",
            "Merge these selected records into one invoice record?\n\n"+
            "\n".join(names[:8])+
            "\n\nThe first selected row will be kept. Other selected rows will be removed from the invoice list, but their source files will be preserved."):
            return
        primary=chosen[0]
        all_paths=[]
        all_names=[]
        all_text=[]
        for r in chosen:
            all_names.extend([x for x in (r.get("source_name") or "").split(" + ") if x])
            all_paths.extend([x for x in (r.get("source_path") or "").splitlines() if x])
            if r.get("raw_text"): all_text.append(r["raw_text"])
        merged_text="\n\n===== MERGED RECORD =====\n\n".join(all_text)
        fields=extract_fields(merged_text," + ".join(all_names))
        # Keep already manually-entered values from the primary record; fill blanks from combined OCR.
        vendor=primary.get("vendor") or fields.vendor
        number=primary.get("invoice_number") or fields.invoice_number
        date=primary.get("invoice_date") or fields.invoice_date
        amount=primary.get("amount") or fields.amount
        currency=primary.get("currency") or fields.currency or "RON"
        notes=(primary.get("review_notes") or "").strip()
        merge_note=f"Manually merged {len(chosen)} selected records. Verify invoice pages and extracted fields."
        notes=(notes+" " if notes else "")+merge_note
        with db_connect() as con:
            con.execute("""UPDATE invoices SET vendor=?,invoice_number=?,invoice_date=?,amount=?,currency=?,
                source_name=?,source_path=?,raw_text=?,review_notes=?,split_group=?,status='Needs review',
                updated_at=? WHERE id=?""",
                (vendor,number,date,amount,currency," + ".join(all_names),"\n".join(dict.fromkeys(all_paths)),
                 merged_text,notes,"manual_merge",datetime.now().isoformat(timespec="seconds"),primary["id"]))
            for r in chosen[1:]:
                con.execute("DELETE FROM invoices WHERE id=?",(r["id"],))
        audit(primary["id"],"manual_merge",f"merged records={ids}")
        self.refresh()
        self.status.config(text=f"Merged {len(chosen)} records into invoice #{primary['id']}")
        messagebox.showinfo("Merge complete","Selected records are now one invoice entry. Check the merged OCR text and source pages before confirming.")

    def open_original(self):
        ids=self.selected_ids()
        if not ids:return
        if len(ids)>1 and not messagebox.askyesno("Open originals",f"Open source files for {len(ids)} selected invoices?"):
            return
        row_map={r["id"]:r for r in records()}
        missing=[]; opened=0
        for i in ids:
            row=row_map.get(i)
            if not row: continue
            paths=[Path(x) for x in (row.get("source_path") or "").splitlines() if x.strip()]
            for p in paths:
                if not p.exists():
                    missing.append(row.get("source_name") or str(p)); continue
                try: os.startfile(str(p)); opened+=1
                except Exception as e: messagebox.showerror("Open failed",f"{p}\n{e}"); break
        if missing:
            messagebox.showwarning("Original missing",f"Could not find {len(missing)} original file(s):\n\n"+"\n".join(missing[:8]))
        self.status.config(text=f"Opened {opened} source file(s)")
    def delete(self):
        ids=self.selected_ids()
        if not ids:return
        n=len(ids)
        msg=(f"Delete {n} selected invoice records?" if n>1 else "Delete this invoice record?")
        msg+=" The saved original file(s) will not be deleted."
        if messagebox.askyesno("Delete records",msg):
            for i in ids: delete_invoice(i)
            self.refresh()
            self.status.config(text=f"Deleted {n} record(s)")
    def import_files(self):
        paths=filedialog.askopenfilenames(title="Select invoices and/or ZIP archives",
          filetypes=[("Invoice files","*.pdf *.zip *.png *.jpg *.jpeg *.tif *.tiff *.bmp"),("All files","*.*")])
        if paths:self.start_import(list(paths))
    def import_folder(self):
        p=filedialog.askdirectory(title="Select folder containing invoices")
        if p:self.start_import([p])
    def start_import(self,paths):
        if self.busy:return
        tesseract=find_tesseract()
        if not tesseract:
            messagebox.showerror("OCR engine not installed","Install Tesseract OCR with Romanian and English language packs. See the included setup guide."); return
        batch=ORIGINALS/datetime.now().strftime("Batch_%Y%m%d_%H%M%S_%f")
        batch.mkdir(parents=True,exist_ok=True)
        try: files=collect_inputs(paths,batch/"zip_extracted")
        except Exception as e:
            log_error(f"Import collection failed: {e}"); messagebox.showerror("Import failed",str(e)); return
        if not files:
            messagebox.showinfo("No supported files","No PDF or supported image files were found."); return
        self.busy=True
        self.import_progress.configure(maximum=max(1,len(files)),value=0)
        self.progress_label.config(text=f"0 / {len(files)} invoices")
        self.status.config(text="Preparing OCR…")
        threading.Thread(target=self.import_worker,args=(files,batch,tesseract),daemon=True).start()
    def import_worker(self,files,batch,tesseract):
        added=duplicates=failed=0
        # Group split pages before OCR so "part 1 of 2" + "part 2 of 2"
        # becomes ONE invoice record, not two records.
        split_groups={}
        standalone=[]
        for f in files:
            info=split_part_info(f.name)
            if info:
                base,part,total=info
                # Parent folder prevents unrelated same-named invoices in separate folders from merging.
                key=(str(f.parent).casefold(),base,total)
                split_groups.setdefault(key,[]).append((part,f))
            else:
                standalone.append([f])
        work_items=list(standalone)
        incomplete=[]
        for key,items in split_groups.items():
            by_part={}
            for part,f in items: by_part.setdefault(part,f)
            total=key[2]
            if len(by_part)==total and all(i in by_part for i in range(1,total+1)):
                work_items.append([by_part[i] for i in range(1,total+1)])
            else:
                # Do not silently merge incomplete sets. Process separately and clearly flag.
                for part,f in sorted(items):
                    incomplete.append((f,part,total))
                    work_items.append([f])
        total_items=len(work_items)
        for n,parts in enumerate(work_items,1):
            names = " + ".join(f.name for f in parts)
            self.after(0, lambda n=n, total=total_items, names=names:
                       self.status.config(text=f"Invoice {n}/{total} • {names[:70]} • starting…"))
            try:
                saved_parts=[]
                for f in parts:
                    saved=f if ORIGINALS in f.parents else preserved_copy(f,batch)
                    saved_parts.append(saved)
                raw_parts=[]; conf_parts=[]
                for saved in saved_parts:
                    def page_progress(page_no, _unused, n=n, total=total_items, name=saved.name):
                        self.after(0, lambda: self.status.config(
                            text=f"Invoice {n}/{total} • {name[:55]} • page {page_no}"))
                    raw,conf=run_ocr(saved,tesseract,progress=page_progress)
                    raw_parts.append(raw); conf_parts.append(conf)
                raw="\n\n===== MERGED SCAN PART =====\n\n".join(raw_parts)
                conf=sum(conf_parts)/len(conf_parts) if conf_parts else 0
                display_name=" + ".join(p.name for p in saved_parts)
                fields=extract_fields(raw,display_name)
                notes=fields.notes
                if conf < 0.48:
                    notes=(notes+" " if notes else "")+"Low OCR text confidence. Verify vendor, invoice date, amount, and currency against the original; consider a sharper scan."
                elif conf < 0.68:
                    notes=(notes+" " if notes else "")+"OCR text confidence is moderate. Check extracted fields against the original."
                split=split_part_info(saved_parts[0].name) if len(saved_parts)==1 else None
                if len(saved_parts)>1:
                    notes=(notes+" " if notes else "")+f"Automatically merged {len(saved_parts)} scan parts in filename order. Verify original pages and invoice completeness."
                elif split:
                    notes=(notes+" " if notes else "")+f"Incomplete split invoice: found part {split[1]} of {split[2]}. Other part(s) were not found in this import."
                digest=hashlib.sha256("|".join(sha256_file(p) for p in saved_parts).encode()).hexdigest()
                group_key=""
                if len(saved_parts)>1:
                    group_key=f"{saved_parts[0].stem.rsplit(' ',1)[0]}__merged_{len(saved_parts)}"
                _,dup=insert_invoice({
                    "vendor":fields.vendor,"invoice_number":fields.invoice_number,"invoice_date":fields.invoice_date,
                    "amount":fields.amount,"currency":fields.currency or "","status":"Needs review","review_notes":notes,
                    "source_name":display_name,"source_path":"\n".join(str(p) for p in saved_parts),
                    "file_hash":digest,"raw_text":raw,"ocr_confidence":conf,"split_group":group_key
                })
                added+=1
                if dup:duplicates+=1
                audit(_, "imported", f"sources={display_name}; merged_parts={len(saved_parts)}; duplicate={dup}")
            except Exception as e:
                failed+=1; log_error(f"OCR failed for {[str(x) for x in parts]}: {type(e).__name__}: {e}")
                for f in parts:
                    try:
                        saved=f if ORIGINALS in f.parents else preserved_copy(f,batch)
                        insert_invoice({"source_name":saved.name,"source_path":str(saved),"file_hash":sha256_file(saved),
                          "status":"Needs review","review_notes":f"OCR failed: {type(e).__name__}: {e}"})
                    except Exception as e2: log_error(f"Could not save failed import record: {e2}")
            self.after(0, lambda n=n, total=total_items:
                       (self.import_progress.configure(value=n),
                        self.progress_label.config(text=f"{n} / {total} invoices"),
                        self.status.config(text=f"Completed invoice {n}/{total}")))
        self.after(0,lambda:self.import_finished(added,duplicates,failed))
    def import_finished(self,added,duplicates,failed):
        self.busy=False; self.refresh()
        self.import_progress.configure(value=self.import_progress["maximum"])
        self.progress_label.config(text="Complete")
        self.status.config(text=f"Import finished • {added} imported • {duplicates} exact duplicates • {failed} errors")
        messagebox.showinfo("Import complete",
          f"Files processed: {added}\nExact duplicate files: {duplicates}\nOCR/import errors: {failed}\n\nAll new records remain Needs review. OCR confidence measures text recognition, not invoice-field correctness. Verify original pages before relying on totals.")
    def export(self):
        p=filedialog.asksaveasfilename(title="Export Excel tracker",defaultextension=".xlsx",
           initialfile="Invoice_Tracker.xlsx",filetypes=[("Excel workbook","*.xlsx")])
        if not p:return
        try:
            export_xlsx(Path(p)); messagebox.showinfo("Export complete",f"Workbook saved:\n{p}\n\nOnly Confirmed, non-duplicate records are included in summary totals.")
        except Exception as e:
            log_error(f"Excel export failed: {e}"); messagebox.showerror("Export failed",str(e))
    def backup(self):
        dest=filedialog.askdirectory(title="Choose a folder for the backup")
        if not dest:return
        target=Path(dest)/f"InvoiceOCR_Backup_{datetime.now():%Y%m%d_%H%M%S}"
        try:
            shutil.copytree(BASE,target)
            messagebox.showinfo("Backup complete",f"Database, saved originals, and logs copied to:\n{target}")
        except Exception as e: messagebox.showerror("Backup failed",str(e))
    def on_close(self):
        if self.busy and not messagebox.askyesno("Import in progress","An import is still running. Close anyway? The current file may finish but remaining files will not be processed."):
            return
        self.destroy()

if __name__=="__main__":
    try:
        init_db()
        App().mainloop()
    except Exception as exc:
        log_error(f"Fatal application error: {type(exc).__name__}: {exc}")
        try: messagebox.showerror("Invoice OCR error",f"The app encountered an error:\n{exc}\n\nLog: {LOG}")
        except Exception: pass
        raise
