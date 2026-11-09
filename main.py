"""
Invoice OCR — local-first Windows desktop application.
All OCR and data storage remain on the user's computer.
"""
from __future__ import annotations
import csv, difflib, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, threading, zipfile
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Optional
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "Invoice OCR"
APP_VERSION = "5.1.0"
SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".xml"}
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
    """Locate Tesseract from explicit config, standard folders, registry or PATH."""
    candidates = [
        Path(os.environ.get("TESSERACT_CMD", "")) if os.environ.get("TESSERACT_CMD") else None,
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe" if os.environ.get("LOCALAPPDATA") else None,
    ]
    if os.name == "nt":
        try:
            import winreg
            registry_keys = (
                (r"SOFTWARE\Tesseract-OCR", "InstallDir"),
                (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Tesseract-OCR_is1", "InstallLocation"),
            )
            views = [getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)]
            hives = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]
            for hive in hives:
                for key_name, value_name in registry_keys:
                    for view in views:
                        try:
                            with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                                install_dir, _ = winreg.QueryValueEx(key, value_name)
                            if install_dir:
                                candidates.append(Path(install_dir) / "tesseract.exe")
                        except OSError:
                            continue
        except (ImportError, AttributeError, OSError):
            pass
    for candidate in candidates:
        if candidate and candidate.is_file():
            return str(candidate)
    return shutil.which("tesseract")

@contextmanager
def _managed_sqlite_connection(database_path: Path, *, row_factory=None, timeout: int = 30):
    """Yield a SQLite connection and always close its file handle.

    sqlite3.Connection's built-in context manager commits/rolls back but does
    not close the connection. Explicit closure is important on Windows, where
    open database handles prevent database backups and temporary test folders
    from being removed.
    """
    connection = sqlite3.connect(database_path, timeout=timeout)
    if row_factory is not None:
        connection.row_factory = row_factory
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def db_connect():
    """Context-managed connection to the active database; always closes it."""
    BASE.mkdir(parents=True, exist_ok=True)
    @contextmanager
    def _connection():
        with _managed_sqlite_connection(DB, row_factory=sqlite3.Row, timeout=30) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA journal_mode=WAL")
            yield con
    return _connection()

def init_db():
    """Create and safely migrate the local database before creating indexes."""
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
            currency TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Needs review',
            review_notes TEXT NOT NULL DEFAULT '',
            source_name TEXT NOT NULL DEFAULT '',
            source_path TEXT NOT NULL DEFAULT '',
            source_input_path TEXT NOT NULL DEFAULT '',
            file_hash TEXT NOT NULL DEFAULT '',
            duplicate_of INTEGER,
            raw_text TEXT NOT NULL DEFAULT '',
            ocr_confidence REAL,
            split_group TEXT NOT NULL DEFAULT '',
            field_confidence_json TEXT NOT NULL DEFAULT '{}',
            verified_fields_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )""")

        # Migrate first. Older releases did not necessarily contain file_hash,
        # so creating its index before this block made upgrades fail at startup.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(invoices)")}
        additions = {
            "vendor": "TEXT NOT NULL DEFAULT ''",
            "invoice_number": "TEXT NOT NULL DEFAULT ''",
            "invoice_date": "TEXT NOT NULL DEFAULT ''",
            "amount": "TEXT NOT NULL DEFAULT ''",
            "currency": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'Needs review'",
            "review_notes": "TEXT NOT NULL DEFAULT ''",
            "source_name": "TEXT NOT NULL DEFAULT ''",
            "source_path": "TEXT NOT NULL DEFAULT ''",
            "source_input_path": "TEXT NOT NULL DEFAULT ''",
            "file_hash": "TEXT NOT NULL DEFAULT ''",
            "duplicate_of": "INTEGER",
            "raw_text": "TEXT NOT NULL DEFAULT ''",
            "ocr_confidence": "REAL",
            "split_group": "TEXT NOT NULL DEFAULT ''",
            "field_confidence_json": "TEXT NOT NULL DEFAULT '{}'",
            "verified_fields_json": "TEXT NOT NULL DEFAULT '{}'",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "updated_at": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in cols:
                con.execute(f"ALTER TABLE invoices ADD COLUMN {name} {definition}")

        # Migrate earlier MVP column names without discarding any records.
        if "filename" in cols:
            con.execute("UPDATE invoices SET source_name=filename WHERE source_name=''")
        if "notes" in cols:
            con.execute("UPDATE invoices SET review_notes=notes WHERE review_notes=''")
        if "created_at" in cols:
            con.execute("UPDATE invoices SET updated_at=created_at WHERE updated_at=''")
        con.execute("UPDATE invoices SET currency='' WHERE currency IS NULL")
        con.execute("UPDATE invoices SET status='Needs review' WHERE status IS NULL OR status=''")

        con.execute("""CREATE TABLE IF NOT EXISTS vendor_aliases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT NOT NULL,
            alias TEXT NOT NULL COLLATE NOCASE UNIQUE,
            alias_type TEXT NOT NULL DEFAULT 'abbreviation',
            created_at TEXT NOT NULL
        )""")
        con.execute("CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("""CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER, action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_invoice_hash ON invoices(file_hash)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_invoice_date ON invoices(invoice_date)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_invoice_vendor_number ON invoices(vendor, invoice_number)")
        con.execute("INSERT INTO app_meta(key,value) VALUES('schema_version','6') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value")

def records():
    """Return records in true chronological order, including legacy ISO dates."""
    with db_connect() as con:
        rows = [dict(x) for x in con.execute("SELECT * FROM invoices").fetchall()]
    for row in rows:
        row["invoice_date"] = fmt_date(str(row.get("invoice_date") or "")) or str(row.get("invoice_date") or "")
    def key(row):
        try:
            d = datetime.strptime(row["invoice_date"], "%d/%m/%Y")
            return (0, d, int(row.get("id") or 0))
        except (ValueError, TypeError):
            return (1, datetime.max, int(row.get("id") or 0))
    return sorted(rows, key=key)


def parse_money(raw: str) -> Optional[float]:
    """Parse common European and US monetary formats without losing sign."""
    if raw is None:
        return None
    original = str(raw).strip()
    if not original:
        return None
    negative = original.startswith("(") and original.endswith(")")
    if negative:
        original = original[1:-1]
    s = re.sub(r"[^0-9,.+\-]", "", original).replace("'", "")
    if not s or s in {"+", "-", ".", ","}:
        return None
    if "+" in s:
        s = s.replace("+", "")
    if s.count("-") > 1 or ("-" in s and not s.startswith("-")):
        return None
    if "," in s and "." in s:
        # The right-most separator is decimal; all preceding separators group.
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        grouping = "." if decimal == "," else ","
        s = s.replace(grouping, "").replace(decimal, ".")
    elif "," in s:
        tail = len(s.rsplit(",", 1)[1])
        s = s.replace(",", ".") if tail in (1, 2) else s.replace(",", "")
    elif "." in s:
        tail = len(s.rsplit(".", 1)[1])
        if s.count(".") > 1 or tail == 3:
            s = s.replace(".", "")
    try:
        value = float(s)
    except ValueError:
        return None
    if negative:
        value = -abs(value)
    if not (float("-1e15") < value < float("1e15")):
        return None
    return value


def fmt_date(raw: str) -> str:
    value = str(raw or "").strip()
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%d/%m/%Y")
        except ValueError:
            pass
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
    currency: str = ""
    confidence: float = 0.0
    notes: str = ""
    field_confidence: dict = None
    vat_amount: str = ""

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

def _norm_vendor(s):
    value = str(s or "").upper().strip()
    translate = str.maketrans({"Ă":"A", "Â":"A", "Î":"I", "Ș":"S", "Ş":"S", "Ț":"T", "Ţ":"T"})
    value = value.translate(translate)
    return re.sub(r"[^A-Z0-9]", "", value)


_GENERIC_VENDOR_ALIASES = {
    "SRL", "SA", "SAS", "LTD", "LLC", "INC", "GMBH", "CUI", "CIF",
    "VAT", "TVA", "FACTURA", "INVOICE", "SUPPLIER", "VENDOR", "COMPANY"
}


def save_vendor_alias(canonical_name: str, alias: str, alias_type: str = "abbreviation"):
    canonical_name = (canonical_name or "").strip()
    alias = (alias or "").strip()
    normalized = _norm_vendor(alias)
    if len(canonical_name) < 2 or len(normalized) < 4 or normalized in _GENERIC_VENDOR_ALIASES:
        return False
    # Do not allow a company suffix or a common OCR word to become a global alias.
    if len(normalized) <= 5 and normalized in {"ROMAN", "TOTAL", "GROUP", "TRADE", "STORE"}:
        return False
    with db_connect() as con:
        con.execute("""INSERT INTO vendor_aliases(canonical_name,alias,alias_type,created_at)
            VALUES(?,?,?,?) ON CONFLICT(alias) DO UPDATE SET canonical_name=excluded.canonical_name,
            alias_type=excluded.alias_type""",
            (canonical_name, alias, alias_type, datetime.now().isoformat(timespec="seconds")))
    return True


def _known_vendor_alias_match(text):
    """Match learned aliases as complete OCR tokens, not risky substrings."""
    lines = [_clean_ocr_line(x) for x in text.splitlines()[:60]]
    aliases = _vendor_alias_rows()
    matches = []
    for row in aliases:
        alias = row.get("alias", "")
        norm = _norm_vendor(alias)
        if len(norm) < 4 or norm in _GENERIC_VENDOR_ALIASES:
            continue
        for i, line in enumerate(lines):
            tokens = [_norm_vendor(t) for t in re.findall(r"[\wĂÂÎȘȚăâîșț]+", line, re.UNICODE)]
            tokens = [t for t in tokens if t]
            line_norm = _norm_vendor(line)
            exact_token = norm in tokens
            exact_line = norm == line_norm
            phrase = len(norm) >= 6 and any("".join(tokens[j:j+n]) == norm
                                            for n in range(2, min(5, len(tokens))+1)
                                            for j in range(0, len(tokens)-n+1))
            fuzzy = False
            if len(norm) >= 7 and line_norm:
                fuzzy = difflib.SequenceMatcher(None, norm, line_norm).ratio() >= 0.94
            if exact_line or exact_token or phrase or fuzzy:
                matches.append((120-i + (10 if exact_line else 5 if exact_token else 0), row["canonical_name"], alias))
                break
    return max(matches, key=lambda x: x[0]) if matches else None

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
    # Invoice number: parse one line at a time. A bare "Factura" or "Invoice"
    # header must never consume the following "BILL TO" / customer heading.
    number_patterns = [
        re.compile(r"(?:bon\s+fiscal)\s*(?:nr\.?|no\.?|number|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9./_-]{5,})", re.I),
        re.compile(r"(?:factur[aă]|invoice)\s*(?:nr\.?|no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9./_-]{2,})", re.I),
        re.compile(r"(?:serie\s*/\s*num[aă]r|num[aă]r(?:ul)?\s+factur[aă]|serie\s*(?:[șs]i\s*)?nr\.?)\s*[:\-]?\s*([A-Z0-9][A-Z0-9./_-]{2,})", re.I),
        re.compile(r"(?:factur[aă]|invoice)\s*[:\-]\s*([A-Z0-9][A-Z0-9./_-]{2,})", re.I),
        re.compile(r"(?:factur[aă]|invoice)\s+((?=[A-Z0-9./_-]*\d)[A-Z][A-Z0-9./_-]{2,})", re.I),
    ]
    forbidden_number_tokens = {"BILL", "BILLTO", "TO", "CUSTOMER", "CLIENT", "BUYER", "SUPPLIER", "DATE", "TOTAL", "PAGE"}
    for idx, line in enumerate(lines):
        candidate = ""
        if re.search(r"\b(accum(?:ulated)?|cumulat)\b", line, re.I):
            continue
        for pat in number_patterns:
            m = pat.search(line)
            if m:
                candidate = m.group(1).strip(" .,:;#")
                break
        # A genuinely split label/value can be accepted only when the value on
        # the next line is a plausible identifier and not a common heading.
        if not candidate and re.fullmatch(r"(?:factur[aă]|invoice)", line, re.I) and idx + 1 < len(lines):
            nxt = lines[idx + 1].strip(" .,:;#")
            if (re.fullmatch(r"[A-Z0-9][A-Z0-9./_-]{2,}", nxt, re.I)
                    and any(ch.isdigit() for ch in nxt)
                    and _norm_vendor(nxt) not in forbidden_number_tokens):
                candidate = nxt
        # Romanian fiscal receipts frequently print the long fiscal document
        # number on the line immediately below a standalone BON FISCAL label.
        if not candidate and re.fullmatch(r"BON\s+FISCAL", line, re.I) and idx + 1 < len(lines):
            nxt = lines[idx + 1].strip(" .,:;#")
            if re.fullmatch(r"[A-Z0-9][A-Z0-9./_-]{5,}", nxt, re.I) and sum(ch.isdigit() for ch in nxt) >= 5:
                candidate = nxt
        if candidate and _norm_vendor(candidate) not in forbidden_number_tokens:
            out.invoice_number = candidate[:60]
            break

    # Invoice date: use issue-date evidence. Due dates, payment dates and
    # delivery dates are not substitutes for the invoice issue date.
    date_candidates = []
    issue_terms = ("data facturii", "data emiterii", "data emit", "invoice date",
                   "date of issue", "issue date", "issued on", "data documentului",
                   "factura din", "data eliber")
    due_terms = ("scadent", "due date", "payment date", "data platii", "data plății",
                 "termen de plata", "termen de plată", "delivery date", "data livrarii",
                 "data expedierii", "data transportului")
    for match in DATE_RE.finditer(text):
        value = fmt_date(match.group(0))
        if not value:
            continue
        before = text[max(0, match.start()-110):match.start()].casefold()
        after = text[match.end():match.end()+35].casefold()
        if any(term in before for term in due_terms):
            continue
        score = 0
        if any(term in before for term in issue_terms):
            score = 6
        elif re.search(r"\bserie\s*/\s*num[aă]r\b", before, re.I):
            score = 5  # Romanian invoices often pair Serie/Numar with Data.
        elif any(term in after for term in ("data facturii", "invoice date")):
            score = 5
        elif re.search(r"\b(bon\s+fiscal|bon\s+v[aâ]nzare|receipt|cas[aă])\b", text, re.I):
            # A receipt's issue date is commonly printed near the top without a
            # textual label. Keep the score below an explicit invoice-date match.
            if match.start() < max(1, int(len(text) * 0.48)):
                score = 3
        elif len(DATE_RE.findall(text)) == 1:
            score = 1  # weak candidate; show as low confidence and require review
        if score:
            date_candidates.append((score, match.start(), value))
    if date_candidates:
        out.invoice_date = sorted(date_candidates, key=lambda x: (-x[0], x[1]))[0][2]

    # Payable total: prioritize explicit payment labels; reject VAT, tax,
    # subtotal, paid and balance lines. Use the first amount after the label,
    # not the last number on a line (which is often the VAT amount).
    total_candidates = []
    strong_labels = [
        r"total\s+de\s+plat[aă]", r"total\s+factur[aă]", r"total\s+de\s+achitat",
        r"total\s+payable", r"amount\s+due", r"total\s+to\s+pay", r"grand\s+total",
        r"total\s+incl(?:usiv)?\s*(?:tva|vat)?", r"total\s+pl[aă]t[aă]",
        r"suma\s+de\s+pl[aă]t[aă]", r"valoare\s+total[aă]", r"total\s+brut",
        r"total\s+cu\s+tva", r"net\s+payable"
    ]
    excluded_total_line = re.compile(
        r"subtotal|sub\s*total|f[aă]r[aă]\s*tva|without\s*vat|net\s+total|\btva\b|\bvat\b|\btax\b|\bpaid\b|\bachitat\b.*\bavans\b",
        re.I)
    for i, line in enumerate(lines):
        low = line.casefold()
        strong_match = next((re.search(p, line, re.I) for p in strong_labels if re.search(p, line, re.I)), None)
        generic = bool(re.search(r"\btotal\b", line, re.I))
        score = 5 if strong_match else (1 if generic else 0)
        if not score:
            continue
        if excluded_total_line.search(low) and not strong_match:
            continue
        number_source = line[strong_match.end():] if strong_match else line
        first_money = MONEY_RE.search(number_source)
        if excluded_total_line.search(number_source[:first_money.start()] if first_money else number_source):
            continue
        nums = MONEY_RE.findall(number_source)
        if not nums and i+1 < len(lines):
            next_line = lines[i+1]
            if not excluded_total_line.search(next_line.casefold()):
                nums = MONEY_RE.findall(next_line)
        if nums:
            total_candidates.append((score, i, nums[0]))
    if not total_candidates and re.search(r"\b(bon\s+fiscal|receipt)\b", text, re.I):
        # Receipts may label the amount by payment method rather than "total".
        # First use a repeated amount seen in multiple non-tax, non-unit-price
        # lines (e.g. item amount and subtotal); OCR can misread the card line.
        repeated = {}
        for i, line in enumerate(lines):
            if re.search(r"\b(tva|vat|tax|accum)\b|\b\d+\s*[x×]\s*\d", line, re.I):
                continue
            for raw_amount in MONEY_RE.findall(line):
                parsed = parse_money(raw_amount)
                if parsed is not None:
                    key = round(parsed, 2)
                    repeated.setdefault(key, []).append((i, raw_amount))
        repeated_candidates = [(3.5, max(v, key=lambda x: x[0])[0], v[0][1])
                               for _, v in repeated.items() if len(v) >= 2]
        total_candidates.extend(repeated_candidates)
        # Payment-method fallback is deliberately lower priority than a
        # repeated amount, and remains a manual-review candidate.
        for i, line in enumerate(lines):
            if not re.search(r"\b(card|cash|numerar|card\s+bank|pl[aă]tit)\b", line, re.I):
                continue
            if excluded_total_line.search(line):
                continue
            vals = MONEY_RE.findall(line)
            if vals:
                total_candidates.append((2.5, i, vals[0]))
    if total_candidates:
        out.amount = sorted(total_candidates, key=lambda x: (-x[0], -x[1]))[0][2]
    else:
        out.notes = "Payable total not confidently identified; enter it manually."

    # VAT extraction is separate from payable total. Prefer explicit tax-amount
    # labels and do not confuse a percentage (19%) with the monetary tax value.
    vat_candidates=[]
    vat_labels=(r"total\s+(?:tva|vat)",r"(?:tva|vat)\s+(?:de\s+plat[aă]|amount|value)",r"(?:valoare|sum[aă])\s+(?:tva|vat)",r"(?:tva|vat)\s*[:=]")
    for i,line in enumerate(lines):
        if not re.search(r"\b(?:TVA|VAT)\b",line,re.I):
            continue
        if re.search(r"\b(?:cod|cota|rate|procent)\b",line,re.I) and not re.search(r"\d[,.]\d{2}",line):
            continue
        match=next((re.search(p,line,re.I) for p in vat_labels if re.search(p,line,re.I)),None)
        if not match:
            # A common Romanian layout is "TVA 19% 47,50"; accept the amount
            # only when the same line explicitly contains TVA/VAT.
            if not re.search(r"\b(?:TVA|VAT)\s+\d{1,2}\s*%",line,re.I):
                continue
            match=re.search(r"\b(?:TVA|VAT)\b",line,re.I)
        source=line[match.end():]
        nums=MONEY_RE.findall(source)
        if not nums and i+1<len(lines) and not re.search(r"\b(?:total|invoice|factur[aă])\b",lines[i+1],re.I):
            nums=MONEY_RE.findall(lines[i+1])
        if nums:
            value=parse_money(nums[0])
            if value is not None and value>=0:
                vat_candidates.append((3 if "total" in line.casefold() else 2,i,nums[0]))
    if vat_candidates:
        out.vat_amount=sorted(vat_candidates,key=lambda x:(-x[0],-x[1]))[0][2]

    # Currency must be linked to a money line or an explicit currency label.
    # A stray OCR token such as HUF inside a damaged word must not set the code.
    currency_rules = [
        ("EUR", r"\bEUR\b|\bEURO\b|€"),
        ("USD", r"\bUSD\b|US\s*\$"),
        ("HUF", r"\bHUF\b|\bFt\b"),
        ("RON", r"\bRON\b|\bLEI\b|\bLei\b"),
    ]
    currency_evidence = []
    for code, pattern in currency_rules:
        for match in re.finditer(pattern, text, re.I):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            if line_end < 0: line_end = len(text)
            line = text[line_start:line_end]
            explicit_label = bool(re.search(r"(?:currency|moneda|valut[aă])\s*[:=]?\s*$", text[max(0, match.start()-24):match.start()], re.I))
            if MONEY_RE.search(line) or explicit_label:
                currency_evidence.append((match.start(), code))
                break
    if currency_evidence:
        # The currency nearest the payable amount wins; absent a payable amount,
        # preserve an explicit code without guessing from the country alone.
        if out.amount and total_candidates:
            try:
                chosen_amount = total_candidates[0][2]
                amount_pos = text.lower().find(chosen_amount.lower())
                _, out.currency = min(currency_evidence, key=lambda x: abs(x[0] - max(0, amount_pos)))
            except Exception:
                out.currency = currency_evidence[0][1]
        else:
            out.currency = currency_evidence[0][1]
    else:
        out.currency=""
    # Per-field confidence is evidence-based (label match, alias match, candidate
    # ambiguity, OCR confidence are not conflated). These are heuristics, not
    # calibrated probabilities; they are used as review cues, never auto-approval.
    field_conf={}
    field_conf["vendor"]=0.90 if vendor_hits else (0.82 if alias_match else (0.52 if out.vendor else 0.0))
    field_conf["invoice_number"]=0.88 if bool(out.invoice_number) and any(p.search(line) for line in lines for p in number_patterns) else (0.45 if out.invoice_number else 0.0)
    field_conf["invoice_date"]=0.90 if date_candidates and max(x[0] for x in date_candidates)>=5 else (0.62 if date_candidates and max(x[0] for x in date_candidates)>=3 else (0.35 if out.invoice_date else 0.0))
    field_conf["amount"]=0.90 if total_candidates and max(x[0] for x in total_candidates)>=5 else (0.62 if total_candidates and max(x[0] for x in total_candidates)>=2 else (0.30 if out.amount else 0.0))
    field_conf["currency"]=0.92 if out.currency else 0.0
    out.field_confidence=field_conf
    out.confidence=sum(bool(x) for x in [out.vendor,out.invoice_number,out.invoice_date,out.amount])/4
    notes=[]
    if not out.vendor: notes.append("Vendor not detected.")
    if not out.invoice_date: notes.append("Invoice date not detected.")
    if not out.amount: notes.append("Payable total not confidently identified.")
    if out.invoice_date and date_candidates and max(x[0] for x in date_candidates) < 5: notes.append("Date has no clear invoice-issue label; verify it against the original.")
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
                            scale = min(5.0, 4600 / max(page.rect.width, page.rect.height))
                            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                            yield Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    except Exception as e:
                        log_error(f"PDF image extraction fallback for {path.name}: {e}")
                        scale = min(5.0, 4600 / max(page.rect.width, page.rect.height))
                        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                        yield Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                else:
                    # Thermal receipts often contain tiny type; retain a larger
                    # source raster because density-cropping later makes OCR safe.
                    scale = min(5.0, 4600 / max(page.rect.width, page.rect.height))
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
    # Prefer readable invoice text over a marginally higher raw Tesseract score.
    # These are ranking heuristics only; they are not field probabilities.
    tokens = re.findall(r"[^\s]+", text)
    words = [t for t in tokens if re.search(r"[A-Za-zĂÂÎȘȚăâîșț]", t)]
    readable = sum(1 for t in words if len(re.sub(r"[^A-Za-zĂÂÎȘȚăâîșț]", "", t)) >= 2)
    readable_ratio = readable / max(1, len(words))
    anchors = len(re.findall(
        r"\b(?:invoice|factur[aă]|bon\s+fiscal|total|amount|payable|date|data|lei|ron|vat|tva|srl|cash|card|subtotal)\b",
        text, re.I))
    money_count = len(MONEY_RE.findall(text))
    date_count = len(DATE_RE.findall(text))
    score = (confidence * 0.58 + min(useful, 500) / 500 * 0.15
             + min(anchors, 8) * 0.018 + min(money_count, 4) * 0.012
             + min(date_count, 2) * 0.018 + readable_ratio * 0.08)
    if useful < 25:
        score -= 0.25
    return score, confidence, text, useful


def _density_crop(gray, ImageFilter):
    """Find the main printed block while ignoring isolated scanner dust.

    The projection is computed on a small thumbnail to keep processing bounded
    even for very large scans. Returns (image, crop_ratio).
    """
    width, height = gray.size
    thumb_w = min(640, width)
    thumb_h = min(900, height)
    thumb = gray.resize((thumb_w, thumb_h)) if (thumb_w, thumb_h) != gray.size else gray
    mask = thumb.filter(ImageFilter.MedianFilter(size=5)).point(
        lambda value: 255 if value < 190 else 0
    )
    raw = mask.tobytes()
    rows = [0] * thumb_h
    cols = [0] * thumb_w
    for y in range(thumb_h):
        row = raw[y * thumb_w:(y + 1) * thumb_w]
        rows[y] = row.count(255)
        for x, value in enumerate(row):
            if value:
                cols[x] += 1
    row_threshold = max(5, int(thumb_w * 0.008))
    col_threshold = max(3, int(thumb_h * 0.003))
    active_y = [i for i, count in enumerate(rows) if count >= row_threshold]
    active_x = [i for i, count in enumerate(cols) if count >= col_threshold]
    if not active_x or not active_y:
        return gray, 1.0
    sx, sy = width / thumb_w, height / thumb_h
    pad_x, pad_y = max(24, int(width * 0.012)), max(24, int(height * 0.012))
    x0 = max(0, int(min(active_x) * sx) - pad_x)
    x1 = min(width, int((max(active_x) + 1) * sx) + pad_x)
    y0 = max(0, int(min(active_y) * sy) - pad_y)
    y1 = min(height, int((max(active_y) + 1) * sy) + pad_y)
    if x1 <= x0 or y1 <= y0:
        return gray, 1.0
    candidate = gray.crop((x0, y0, x1, y1))
    ratio = (candidate.width * candidate.height) / max(1, width * height)
    if ratio < 0.90 and candidate.width > 220 and candidate.height > 120:
        return candidate, ratio
    return gray, 1.0


def _iter_page_sources(path: Path):
    """Yield (page_number, image, native_text) so mixed PDFs are handled per page."""
    if path.suffix.lower() == ".pdf":
        import fitz
        from PIL import Image
        import io
        doc = fitz.open(path)
        try:
            for page_index, page in enumerate(doc):
                native_text = page.get_text("text").strip()
                useful = len(re.findall(r"[A-Za-zĂÂÎȘȚăâîșț0-9]", native_text))
                native_tokens = re.findall(r"\b[A-Za-zĂÂÎȘȚăâîșț0-9]{2,}\b", native_text)
                readable_ratio = sum(1 for token in native_tokens
                                     if re.search(r"[A-Za-zĂÂÎȘȚăâîșț]", token)) / max(1, len(native_tokens))
                # A hidden OCR layer can contain enough characters to pass a
                # length-only check while still being mostly scanner noise.
                if useful >= 60 and len(native_tokens) >= 8 and readable_ratio >= 0.38:
                    yield page_index + 1, None, native_text
                    continue
                # Render the page through PyMuPDF rather than extracting the
                # embedded scan image. Embedded image pixels may have a PDF
                # placement transform (including vertical flips/rotations); the
                # page renderer applies those transforms correctly.
                scale = min(5.0, 4600 / max(page.rect.width, page.rect.height))
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                yield page_index + 1, image, ""
        finally:
            doc.close()
    else:
        for page_no, image in enumerate(image_pages(path), 1):
            yield page_no, image, ""


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

    texts, page_confs = [], []
    pages = _iter_page_sources(path)
    for page_no, original, native_text in pages:
        if progress:
            progress(page_no, None)
        if native_text:
            texts.append(f"--- PAGE {page_no} [SEARCHABLE PDF TEXT] ---\n{native_text}")
            page_confs.append(0.99)
            log_error(f"PDF native text selected {path.name} page {page_no}: chars={len(native_text)}")
            continue
        # Rendered scans are already 2x for PDF. Keep a practical pixel ceiling
        # so giant scans don't consume excessive memory or slow Tesseract.
        base = original.convert("RGB")
        # Rendered scans are already 2x for PDF. Keep a practical pixel ceiling
        # so giant scans don't consume excessive memory or slow Tesseract.
        base = original.convert("RGB")
        w, h = base.size
        max_edge = max(w, h)
        if max_edge > 4600:
            scale = 4600 / max_edge
            base = base.resize((max(1, int(w*scale)), max(1, int(h*scale))), Image.Resampling.LANCZOS)
        elif max_edge < 3800:
            # Upsample moderate embedded receipt scans before isolating the
            # printed block; Tesseract otherwise struggles with thermal text.
            scale = min(1.8, 4600 / max_edge)
            base = base.resize((max(1, int(w*scale)), max(1, int(h*scale))), Image.Resampling.LANCZOS)

        gray = ImageOps.grayscale(base)
        contrast = ImageEnhance.Contrast(gray).enhance(1.45)

        # Crop blank margins by printed-pixel density rather than a raw bounding
        # box: isolated scanner dust must not enlarge the crop to the whole page.
        crop_ratio = 1.0
        try:
            gray, crop_ratio = _density_crop(gray, ImageFilter)
            contrast = ImageEnhance.Contrast(gray).enhance(1.65 if crop_ratio < 0.90 else 1.45)
        except Exception as e:
            log_error(f"Content crop skipped ({path.name}, page {page_no}): {e}")
            contrast = ImageEnhance.Contrast(gray).enhance(1.45)

        # Pass 1: normal invoice layout. image_to_data also supplies text and
        # confidence, avoiding the old duplicate image_to_string call per pass.
        candidates = []
        try:
            candidates.append((*_ocr_pass(contrast, languages, 6, pytesseract),
                               "cropped" if crop_ratio < 0.82 else "contrast", 6))
        except Exception as e:
            log_error(f"Primary OCR pass failed ({path.name}, page {page_no}): {e}")

        best = max(candidates, key=lambda x: x[0]) if candidates else None

        # Bound OCR retries. Earlier builds could run up to nine full-page passes
        # plus header/footer crops even when the first result was already useful.
        # The second pass is selected by page shape; a third is reserved for a
        # genuinely poor result. This caps routine scans at three Tesseract calls.
        aspect = max(contrast.size) / max(1, min(contrast.size))
        if best is None or best[1] < 0.70 or best[3] < 45:
            try:
                if aspect > 3.2:
                    retry_image, retry_psm, retry_name = contrast, 11, "sparse-layout-psm11"
                else:
                    retry_image, retry_psm, retry_name = ImageOps.autocontrast(gray, cutoff=1), 4, "autocontrast-psm4"
                result = _ocr_pass(retry_image, languages, retry_psm, pytesseract)
                candidates.append((*result, retry_name, retry_psm))
                best = max(candidates, key=lambda x: x[0])
            except Exception as exc:
                log_error(f"Targeted OCR retry failed ({path.name}, page {page_no}): {exc}")

        if best is None or best[1] < 0.40 or best[3] < 18:
            try:
                sharp = ImageEnhance.Sharpness(contrast).enhance(1.4)
                result = _ocr_pass(sharp, languages, 6, pytesseract)
                candidates.append((*result, "sharpened-psm6", 6))
                best = max(candidates, key=lambda x: x[0])
            except Exception as exc:
                log_error(f"Final OCR retry failed ({path.name}, page {page_no}): {exc}")

        # Only try a 180-degree orientation when the page is almost unreadable;
        # low text confidence alone is common on thermal receipts and is not a
        # reason to spend another full OCR pass.
        if (best is None or (best[1] < 0.18 and best[3] < 10)) and crop_ratio < 0.82:
            try:
                rotated = contrast.rotate(180, expand=True)
                result = _ocr_pass(rotated, languages, 6, pytesseract)
                candidates.append((*result, "crop-rot180", 6))
                best = max(candidates, key=lambda x: x[0])
            except Exception as exc:
                log_error(f"Orientation OCR retry failed ({path.name}, page {page_no}): {exc}")

        if best is None:
            raise RuntimeError("Tesseract could not produce text from this page. Check OCR language packs.")

        # Keep the selected full-page text intact. Regional OCR used to add two
        # more expensive passes on nearly every weak receipt without reliably
        # improving extraction; low-quality pages are clearly flagged for review.
        extra = ""
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
    """Extract supported invoice files with traversal, size and zip-bomb guards."""
    dest.mkdir(parents=True, exist_ok=True)
    extracted = []
    max_files = 2000
    max_file_bytes = 150 * 1024 * 1024
    max_total_bytes = 500 * 1024 * 1024
    total_bytes = 0
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if len(members) > max_files:
            raise ValueError(f"ZIP contains too many entries ({len(members)}; maximum {max_files}).")
        for info in members:
            if info.is_dir():
                continue
            raw_name = info.filename.replace("\\", "/")
            pp = PurePosixPath(raw_name)
            if pp.is_absolute() or not pp.parts or ".." in pp.parts or "\x00" in raw_name:
                continue
            if any(part in {"", "."} for part in pp.parts):
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                continue
            if info.file_size < 0 or info.file_size > max_file_bytes:
                continue
            if info.compress_size and info.file_size / info.compress_size > 500:
                continue
            if total_bytes + info.file_size > max_total_bytes:
                raise ValueError("ZIP expands beyond the 500 MB safety limit.")
            target = (root / Path(*pp.parts)).resolve()
            if target != root and root not in target.parents:
                continue
            if target.exists():
                # Avoid silently overwriting a previous member with the same name.
                target = target.with_name(f"{target.stem}_{hashlib.sha256(raw_name.encode()).hexdigest()[:8]}{target.suffix}")
            target.parent.mkdir(parents=True, exist_ok=True)
            copied = 0
            with archive.open(info) as src, target.open("wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    total_bytes += len(chunk)
                    if copied > max_file_bytes or total_bytes > max_total_bytes:
                        dst.close()
                        target.unlink(missing_ok=True)
                        raise ValueError("ZIP expansion exceeded the safety limit while extracting.")
                    dst.write(chunk)
            if target.suffix.lower() in SUPPORTED:
                extracted.append(target)
    return extracted


def collect_inputs(paths, batch_dir, origin_map=None):
    """Collect PDFs/images and optionally map ZIP-extracted files to their source archive."""
    files = []
    zip_files = []
    seen = set()
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            try:
                candidates = p.rglob("*")
                root_resolved = p.resolve()
                for f in candidates:
                    if not f.is_file():
                        continue
                    try:
                        f.resolve().relative_to(root_resolved)
                    except (OSError, ValueError):
                        continue  # Do not follow symlinks outside the selected folder.
                    suffix = f.suffix.lower()
                    if suffix == ".zip":
                        zip_files.append(f)
                    elif suffix in SUPPORTED:
                        key = str(f.absolute()).casefold()
                        if key not in seen:
                            files.append(f); seen.add(key)
            except OSError as exc:
                log_error(f"Folder scan warning for {p}: {exc}")
        elif p.suffix.lower() == ".zip":
            zip_files.append(p)
        elif p.suffix.lower() in SUPPORTED:
            key = str(p.resolve()).casefold()
            if key not in seen:
                files.append(p); seen.add(key)

    extracted_root = Path(batch_dir)
    extracted_root.mkdir(parents=True, exist_ok=True)
    for index, archive in enumerate(sorted(set(zip_files), key=lambda x: str(x).casefold()), 1):
        try:
            extracted = safe_extract_zip(archive, extracted_root / f"{archive.stem}_{index}")
            for f in extracted:
                key = str(f.absolute()).casefold()
                if origin_map is not None:
                    origin_map[str(f.absolute())] = str(archive.absolute())
                if key not in seen:
                    files.append(f); seen.add(key)
        except Exception as exc:
            log_error(f"Could not import ZIP {archive}: {type(exc).__name__}: {exc}")
    return sorted(files, key=lambda x: (x.name.casefold(), str(x.parent).casefold()))

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

def group_split_files(files):
    """Group only complete, unambiguous split sets; never drop repeated parts."""
    groups = {}
    items = []
    for file_path in files:
        info = split_part_info(Path(file_path).name)
        if not info:
            items.append([Path(file_path)])
            continue
        base, part, total = info
        key = (str(Path(file_path).parent).casefold(), base, total)
        groups.setdefault(key, []).append((part, Path(file_path)))
    for key, parts in groups.items():
        by_part = {}
        duplicate_part = False
        for part, file_path in parts:
            if part in by_part:
                duplicate_part = True
            else:
                by_part[part] = file_path
        total = key[2]
        complete = (not duplicate_part and len(by_part) == total
                    and all(i in by_part for i in range(1, total + 1)))
        if complete:
            items.append([by_part[i] for i in range(1, total + 1)])
        else:
            # Preserve every physical file as its own review item. The import
            # worker adds an incomplete-set warning to each record.
            items.extend([[file_path] for _, file_path in sorted(parts, key=lambda x: (x[0], x[1].name.casefold()))])
    return items


def _normal_match_text(value):
    value = str(value or "").casefold().strip()
    value = re.sub(r"\b(s\.?r\.?l\.?|s\.?a\.?|ltd|llc|inc|gmbh)\b", "", value)
    return re.sub(r"[^a-z0-9ăâîșț]+", "", value)


def duplicate_candidate(data):
    """Return a likely duplicate based on stable invoice identity signals."""
    vendor = _normal_match_text(data.get("vendor"))
    number = _normal_match_text(data.get("invoice_number"))
    date = fmt_date(data.get("invoice_date", ""))
    amount = parse_money(data.get("amount", ""))
    currency = str(data.get("currency") or "").strip().upper()
    if not vendor:
        return None
    with db_connect() as con:
        rows = con.execute("SELECT id,vendor,invoice_number,invoice_date,amount,currency FROM invoices WHERE vendor<>''").fetchall()
    for row in rows:
        rv = _normal_match_text(row["vendor"])
        rn = _normal_match_text(row["invoice_number"])
        ra = parse_money(row["amount"])
        if number and rn and number == rn and vendor == rv:
            return (row["id"], "same normalized supplier and invoice number")
        row_date = fmt_date(row["invoice_date"] or "")
        row_currency = str(row["currency"] or "").strip().upper()
        if (date and date == row_date and amount is not None and ra is not None
                and abs(amount-ra) < 0.005 and currency and currency == row_currency and vendor == rv):
            return (row["id"], "same supplier, date, amount and currency")
    return None


def audit(invoice_id, action, details=""):
    with db_connect() as con:
        con.execute("INSERT INTO audit_log(invoice_id,action,details,created_at) VALUES(?,?,?,?)",
                    (invoice_id, action, details, datetime.now().isoformat(timespec="seconds")))


def insert_invoice(data: dict) -> tuple[int, bool]:
    now = datetime.now().isoformat(timespec="seconds")
    payload = dict(data)
    payload["invoice_date"] = fmt_date(payload.get("invoice_date", ""))
    payload["currency"] = str(payload.get("currency") or "").strip().upper()
    file_hash = str(payload.get("file_hash") or "")
    with db_connect() as con:
        dup = con.execute("SELECT id FROM invoices WHERE file_hash=? AND file_hash<>''", (file_hash,)).fetchone() if file_hash else None
        status = payload.get("status", "Needs review")
        notes = payload.get("review_notes", "")
        duplicate_of = None
        possible = duplicate_candidate(payload) if not dup else None
        if dup:
            duplicate_of = dup["id"]
            status = "Needs review"
            notes = (notes + " " if notes else "") + f"Exact duplicate file of invoice record #{duplicate_of}."
        elif possible:
            duplicate_of = possible[0]
            status = "Needs review"
            notes = (notes + " " if notes else "") + f"Possible duplicate of record #{duplicate_of} ({possible[1]}). Verify manually."
        cur = con.execute("""INSERT INTO invoices
          (vendor,invoice_number,invoice_date,amount,currency,status,review_notes,source_name,source_path,source_input_path,
           file_hash,duplicate_of,raw_text,ocr_confidence,split_group,field_confidence_json,verified_fields_json,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (payload.get("vendor", ""), payload.get("invoice_number", ""), payload.get("invoice_date", ""),
           payload.get("amount", ""), payload.get("currency", ""), status, notes, payload.get("source_name", ""),
           payload.get("source_path", ""), payload.get("source_input_path", ""), file_hash, duplicate_of, payload.get("raw_text", ""),
           payload.get("ocr_confidence", 0), payload.get("split_group", ""),
           json.dumps(payload.get("field_confidence", {}), ensure_ascii=False),
           json.dumps(payload.get("verified_fields", {}), ensure_ascii=False), now, now))
        return cur.lastrowid, bool(duplicate_of)


def update_invoice(i, values, verified_fields=None):
    """Persist review values and invalidate checks for every changed field."""
    keys = ["vendor", "invoice_number", "invoice_date", "amount", "currency", "status", "review_notes"]
    values = list(values)
    if len(values) < len(keys):
        values += [""] * (len(keys) - len(values))
    raw_date = str(values[2] or "").strip()
    values[2] = fmt_date(raw_date) if raw_date else ""
    if raw_date and not values[2]:
        raise ValueError("Enter a valid invoice date in DD/MM/YYYY format or a supported ISO date.")
    raw_amount = str(values[3] or "").strip()
    if raw_amount and parse_money(raw_amount) is None:
        raise ValueError("Enter a valid numeric invoice amount.")
    values[4] = str(values[4] or "").strip().upper()
    if values[4] and not re.fullmatch(r"[A-Z]{3,5}", values[4]):
        raise ValueError("Currency must be a three-to-five-letter code, such as RON or EUR.")
    if values[5] not in {"Needs review", "Confirmed"}:
        raise ValueError("Invoice status must be Needs review or Confirmed.")
    now = datetime.now().isoformat(timespec="seconds")
    warning_reasons = []
    changed_fields = []
    with db_connect() as con:
        old = con.execute("SELECT * FROM invoices WHERE id=?", (int(i),)).fetchone()
        if old is None:
            raise ValueError("This invoice record no longer exists. Refresh the register and try again.")
        checks = dict(verified_fields or {})
        for field, value in zip(keys[:5], values[:5]):
            old_value = str(old[field] or "").strip()
            new_value = str(value or "").strip()
            if field == "invoice_date":
                old_value = fmt_date(old_value) or old_value
                new_value = fmt_date(new_value) or new_value
            if field in {"vendor", "invoice_number", "currency"}:
                old_value = old_value.casefold()
                new_value = new_value.casefold()
            if old_value != new_value:
                checks[field] = False
                changed_fields.append(field)

        if values[5] == "Confirmed":
            if old["duplicate_of"]:
                warning_reasons.append(f"Possible duplicate of invoice #{old['duplicate_of']}; clear the warning first.")
            missing_values = [field for field, value in zip(keys[:5], values[:5])
                              if not str(value or "").strip()]
            if missing_values:
                warning_reasons.append("Missing required values: " + ", ".join(missing_values))
            required_checks = [field for field in keys[:5] if not bool(checks.get(field))]
            if required_checks:
                warning_reasons.append("Fields not currently verified: " + ", ".join(required_checks))
            if warning_reasons:
                values[5] = "Needs review"
                warning_note = "Confirmation blocked. " + " ".join(warning_reasons)
                values[6] = ((str(values[6]).strip() + " ") if str(values[6]).strip() else "") + warning_note
        con.execute("""UPDATE invoices SET vendor=?,invoice_number=?,invoice_date=?,amount=?,currency=?,
          status=?,review_notes=?,verified_fields_json=?,updated_at=? WHERE id=?""",
          (*values, json.dumps(checks, ensure_ascii=False), now, int(i)))
    audit(i, "reviewed", f"status={values[5]}; changed_fields={','.join(changed_fields)}")
    return {"verified_fields": checks, "status": values[5], "warning": " ".join(warning_reasons),
            "changed_fields": changed_fields}

def resolve_duplicate(i):
    """Clear a duplicate flag only after an explicit human decision."""
    with db_connect() as con:
        row = con.execute("SELECT duplicate_of,review_notes FROM invoices WHERE id=?", (int(i),)).fetchone()
        if row is None:
            raise ValueError("Invoice record not found.")
        if not row["duplicate_of"]:
            return False
        notes = re.sub(r"(?:Exact duplicate file|Possible duplicate) of invoice record #\d+\.|Possible duplicate of record #\d+ \([^)]*\)\. Verify manually\.?", "", row["review_notes"] or "", flags=re.I)
        notes = (notes + " Manually reviewed: duplicate warning cleared.").strip()
        con.execute("UPDATE invoices SET duplicate_of=NULL,status='Needs review',review_notes=?,verified_fields_json='{}',updated_at=? WHERE id=?",
                    (notes, datetime.now().isoformat(timespec="seconds"), int(i)))
    audit(i, "duplicate_cleared", "User explicitly marked this record as not a duplicate")
    return True


def delete_invoices(invoice_ids, delete_originals=True):
    """Delete records and unshared file copies, optionally including input originals.

    ``source_path`` is the preserved app copy and is eligible only inside
    InvoiceFlow's Originals directory. ``source_input_path`` tracks direct
    source PDFs/images chosen by the user. Those originals are removed only
    when ``delete_originals`` is True. Shared files and symlink targets are
    always protected. Older records and ZIP imports may not track an original.
    """
    ids = sorted({int(value) for value in (invoice_ids or [])})
    empty_result = {
        "deleted_records": 0, "deleted_files": 0, "deleted_managed_copies": 0,
        "deleted_original_files": 0, "preserved_original_files": 0,
        "retained_shared_files": 0, "missing_files": 0,
        "untracked_originals": 0, "skipped_symlinks": 0,
        "cleanup_errors": [],
    }
    if not ids:
        return empty_result

    originals_root = ORIGINALS.resolve()
    selected_candidates = set()
    remaining_references = set()
    found_rows = {}
    skipped_symlinks = set()
    now = datetime.now().isoformat(timespec="seconds")

    def normalized_reference(raw_path):
        raw_path = str(raw_path or "").strip()
        if not raw_path:
            return None, False
        raw_candidate = Path(raw_path).expanduser()
        try:
            # Never delete through a symlink in the file or any parent path.
            # It may point to a user file outside the imported folder.
            if any(part.is_symlink() for part in (raw_candidate, *raw_candidate.parents)):
                return None, True
            return raw_candidate.resolve(strict=False), False
        except (OSError, RuntimeError, ValueError):
            return None, False

    with db_connect() as con:
        rows = con.execute("SELECT id,source_path,source_input_path FROM invoices").fetchall()
        target_ids = set(ids)
        for row in rows:
            invoice_id = int(row["id"])
            found_rows[invoice_id] = row
            is_selected = invoice_id in target_ids
            for raw in str(row["source_path"] or "").splitlines():
                candidate, is_link = normalized_reference(raw)
                if is_link:
                    skipped_symlinks.add(str(raw).strip())
                if candidate is not None:
                    if is_selected and candidate.is_relative_to(originals_root):
                        selected_candidates.add((candidate, "managed"))
                    elif not is_selected:
                        remaining_references.add(candidate)
            for raw in str(row["source_input_path"] or "").splitlines():
                candidate, is_link = normalized_reference(raw)
                if is_link:
                    skipped_symlinks.add(str(raw).strip())
                if candidate is not None:
                    if is_selected:
                        if delete_originals:
                            selected_candidates.add((candidate, "original"))
                    else:
                        remaining_references.add(candidate)

        found_ids = set(found_rows).intersection(target_ids)
        if not found_ids:
            raise ValueError("No selected invoice records were found. Refresh the register and try again.")

        for invoice_id in sorted(found_ids):
            con.execute(
                "INSERT INTO audit_log(invoice_id,action,details,created_at) VALUES(?,?,?,?)",
                (invoice_id, "deleted", f"record deleted; original_inputs_removed={str(bool(delete_originals)).lower()}; unshared linked files cleaned", now),
            )
        placeholders = ",".join("?" for _ in found_ids)
        con.execute(f"DELETE FROM invoices WHERE id IN ({placeholders})", tuple(sorted(found_ids)))

    candidates_by_path = {}
    retained_shared = 0
    for path, kind in selected_candidates:
        if path in remaining_references:
            retained_shared += 1
            continue
        allowed_suffixes = SUPPORTED | ({".zip"} if kind == "original" else set())
        if path.suffix.lower() not in allowed_suffixes:
            continue
        # Keep the more informative kind if source_path and source_input_path
        # happen to point to the same underlying file.
        previous = candidates_by_path.get(path)
        if previous != "original":
            candidates_by_path[path] = kind

    untracked_originals = (sum(
        1 for invoice_id in found_ids
        if not str(found_rows[invoice_id]["source_input_path"] or "").strip()
    ) if delete_originals else 0)
    preserved_original_files = (0 if delete_originals else len({
        candidate for invoice_id in found_ids
        for raw in str(found_rows[invoice_id]["source_input_path"] or "").splitlines()
        for candidate, is_link in [normalized_reference(raw)]
        if candidate is not None and not is_link
    }))
    deleted_files = deleted_managed = deleted_originals = missing_files = 0
    cleanup_errors = []
    for path, kind in sorted(candidates_by_path.items(), key=lambda item: len(item[0].parts), reverse=True):
        try:
            if path.is_symlink():
                skipped_symlinks.add(str(path))
                continue
            if path.is_file():
                path.unlink()
                deleted_files += 1
                if kind == "managed":
                    deleted_managed += 1
                else:
                    deleted_originals += 1
            else:
                missing_files += 1
        except OSError as exc:
            cleanup_errors.append(f"{path.name}: {exc}")
            log_error(f"Could not remove deleted invoice source {path}: {exc}")

    # Remove empty app-managed import directories, never Originals root.
    parents = {parent for path in candidates_by_path for parent in path.parents
               if path.is_relative_to(originals_root)}
    for folder in sorted(parents, key=lambda item: len(item.parts), reverse=True):
        if folder == originals_root:
            continue
        try:
            folder.relative_to(originals_root)
            folder.rmdir()
        except (ValueError, OSError):
            pass

    return {
        "deleted_records": len(found_ids), "deleted_files": deleted_files,
        "deleted_managed_copies": deleted_managed, "deleted_original_files": deleted_originals,
        "preserved_original_files": preserved_original_files,
        "retained_shared_files": retained_shared, "missing_files": missing_files,
        "untracked_originals": untracked_originals, "skipped_symlinks": len(skipped_symlinks),
        "cleanup_errors": cleanup_errors,
    }


def delete_invoice(i):
    """Backward-compatible single-record deletion."""
    return delete_invoices([i])


def create_backup(destination: Path) -> Path:
    """Create a consistent SQLite backup plus originals and app diagnostics."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    backup_db = destination / "invoices.sqlite3"
    with db_connect() as source, _managed_sqlite_connection(backup_db) as target:
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Backup integrity check failed: {integrity}")
    if ORIGINALS.exists():
        shutil.copytree(ORIGINALS, destination / "Originals", dirs_exist_ok=True)
    if LOG.exists():
        shutil.copy2(LOG, destination / LOG.name)
    readme = destination / "BACKUP_INFO.txt"
    readme.write_text(
        "InvoiceFlow local backup\n"
        f"Created: {datetime.now().isoformat(timespec='seconds')}\n"
        "Contains a consistent SQLite database snapshot and preserved original documents.\n"
        "Keep this folder private; it may contain confidential financial information.\n",
        encoding="utf-8")
    return destination

def restore_backup(backup_directory: Path) -> Path:
    """Restore a validated InvoiceFlow backup after preserving current data."""
    backup_directory = Path(backup_directory).expanduser().resolve()
    backup_db = backup_directory / "invoices.sqlite3"
    if not backup_directory.is_dir() or not backup_db.is_file():
        raise ValueError("Select an InvoiceFlow backup folder containing invoices.sqlite3.")
    # Validate completely before touching the active database.
    with _managed_sqlite_connection(backup_db) as check:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if integrity != "ok" or "invoices" not in tables:
            raise ValueError("The selected backup is damaged or is not an InvoiceFlow database.")
    backup_root = BASE / "Backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    safety_snapshot = backup_root / f"BeforeRestore_{datetime.now():%Y%m%d_%H%M%S_%f}"
    create_backup(safety_snapshot)
    # Use SQLite's online backup API; do not replace a live WAL database file.
    with _managed_sqlite_connection(backup_db) as source, db_connect() as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Restored database integrity check failed.")
    # Merge preserved originals without deleting newer local files. Extra files
    # are harmless; missing files remain visible as a review warning.
    source_originals = backup_directory / "Originals"
    if source_originals.is_dir():
        shutil.copytree(source_originals, ORIGINALS, dirs_exist_ok=True)
    init_db()
    audit(None, "backup_restored", f"source={backup_directory}; safety_snapshot={safety_snapshot}")
    return safety_snapshot


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
            display_status = "Possible duplicate" if r.get("duplicate_of") else r["status"]
            sh.append([r["invoice_date"],r["vendor"],r["invoice_number"],parse_money(r["amount"]) if parse_money(r["amount"]) is not None else r["amount"],
                       r["currency"],display_status,r["source_name"],r["review_notes"]])
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
            d=datetime.strptime(fmt_date(r["invoice_date"]),"%d/%m/%Y")
            months.setdefault(d.strftime("%Y-%m"),[]).append(r)
        except ValueError: pass
    for key,vals in sorted(months.items()):
        sh=wb.create_sheet(datetime.strptime(key,"%Y-%m").strftime("%B %Y"))
        fill_sheet(sh,vals)
        sums={}
        for r in vals:
            if r["status"]!="Confirmed" or r.get("duplicate_of") or not str(r.get("currency") or "").strip(): continue
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
        if r["status"]!="Confirmed" or r.get("duplicate_of") or not str(r.get("currency") or "").strip(): continue
        n=parse_money(r["amount"])
        if n is not None: sums[r["currency"]]=sums.get(r["currency"],0)+n
        try: dates.append(datetime.strptime(r["invoice_date"],"%d/%m/%Y"))
        except ValueError: pass
    ws.append([]); ws.append(["Confirmed purchase totals by currency"])
    for cur,total in sorted(sums.items()): ws.append([cur,round(total,2)])
    ws.append(["Note", "Only Confirmed, non-duplicate records with a known currency are included in totals. Resolve duplicate warnings before confirmation; unknown-currency amounts are excluded from sums."])
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
            if r["status"]=="Confirmed" and not r.get("duplicate_of"):
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
                    con.execute("""UPDATE invoices SET vendor=?,invoice_number=?,invoice_date=?,amount=?,currency=?,raw_text=?,ocr_confidence=?,review_notes=?,status='Needs review',verified_fields_json='{}',updated_at=? WHERE id=?""",
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
        all_input_paths=[]
        all_names=[]
        all_text=[]
        for r in chosen:
            all_names.extend([x for x in (r.get("source_name") or "").split(" + ") if x])
            all_paths.extend([x for x in (r.get("source_path") or "").splitlines() if x])
            all_input_paths.extend([x for x in (r.get("source_input_path") or "").splitlines() if x])
            if r.get("raw_text"): all_text.append(r["raw_text"])
        merged_text="\n\n===== MERGED RECORD =====\n\n".join(all_text)
        fields=extract_fields(merged_text," + ".join(all_names))
        # Keep already manually-entered values from the primary record; fill blanks from combined OCR.
        vendor=primary.get("vendor") or fields.vendor
        number=primary.get("invoice_number") or fields.invoice_number
        date=primary.get("invoice_date") or fields.invoice_date
        amount=primary.get("amount") or fields.amount
        currency=primary.get("currency") or fields.currency or ""
        notes=(primary.get("review_notes") or "").strip()
        merge_note=f"Manually merged {len(chosen)} selected records. Verify invoice pages and extracted fields."
        notes=(notes+" " if notes else "")+merge_note
        with db_connect() as con:
            con.execute("""UPDATE invoices SET vendor=?,invoice_number=?,invoice_date=?,amount=?,currency=?,
                source_name=?,source_path=?,source_input_path=?,raw_text=?,review_notes=?,split_group=?,status='Needs review',
                verified_fields_json='{}',updated_at=? WHERE id=?""",
                (vendor,number,date,amount,currency," + ".join(all_names),"\n".join(dict.fromkeys(all_paths)),
                 "\n".join(dict.fromkeys(all_input_paths)),merged_text,notes,"manual_merge",datetime.now().isoformat(timespec="seconds"),primary["id"]))
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
        msg=(f"Permanently delete {n} selected invoice records and their linked PDF/image/ZIP source files?" if n>1 else "Permanently delete this invoice record and its linked PDF/image/ZIP source files?")
        msg+=" This includes stored InvoiceFlow copies and original import files recorded by this version. Files still used by another invoice are kept. Files imported by older versions may not have a separately tracked original path. A ZIP archive shared by other invoices is kept until its last linked invoice is deleted."
        if messagebox.askyesno("Delete records and source files",msg):
            result=delete_invoices(ids)
            self.refresh()
            self.status.config(text=f"Deleted {result['deleted_records']} record(s) and {result['deleted_files']} linked file(s)")
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
        zip_sources={}
        try: files=collect_inputs(paths,batch/"zip_extracted",origin_map=zip_sources)
        except Exception as e:
            log_error(f"Import collection failed: {e}"); messagebox.showerror("Import failed",str(e)); return
        if not files:
            messagebox.showinfo("No supported files","No PDF or supported image files were found."); return
        self.busy=True
        self.import_progress.configure(maximum=max(1,len(files)),value=0)
        self.progress_label.config(text=f"0 / {len(files)} invoices")
        self.status.config(text="Preparing OCR…")
        threading.Thread(target=self.import_worker,args=(files,batch,tesseract,zip_sources),daemon=True).start()
    def import_worker(self,files,batch,tesseract,zip_sources=None):
        zip_sources=zip_sources or {}
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
                input_paths=[]
                for f in parts:
                    is_managed = ORIGINALS.resolve() in f.resolve().parents
                    archive_source=zip_sources.get(str(f.absolute()))
                    if archive_source:
                        input_paths.append(archive_source)
                    elif not is_managed:
                        input_paths.append(str(f.absolute()))
                    saved=f if is_managed else preserved_copy(f,batch)
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
                    "source_input_path":"\n".join(dict.fromkeys(input_paths)),
                    "file_hash":digest,"raw_text":raw,"ocr_confidence":conf,"split_group":group_key
                })
                added+=1
                if dup:duplicates+=1
                audit(_, "imported", f"sources={display_name}; merged_parts={len(saved_parts)}; duplicate={dup}")
            except Exception as e:
                failed+=1; log_error(f"OCR failed for {[str(x) for x in parts]}: {type(e).__name__}: {e}")
                for f in parts:
                    try:
                        is_managed = ORIGINALS.resolve() in f.resolve().parents
                        saved=f if is_managed else preserved_copy(f,batch)
                        archive_source=zip_sources.get(str(f.absolute()))
                        input_source=archive_source or ("" if is_managed else str(f.absolute()))
                        insert_invoice({"source_name":saved.name,"source_path":str(saved),
                          "source_input_path":input_source,"file_hash":sha256_file(saved),
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
