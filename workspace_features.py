"""InvoiceFlow professional workspace services (local-only).

This module keeps higher-level business workflows out of the OCR engine while
sharing its local SQLite database and protected Originals directory.
"""
from __future__ import annotations
import csv, hashlib, hmac, json, os, re, shutil, sqlite3, uuid, zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET
import main as core

DEFAULT_CATEGORIES = [
    ("Office & administration", "#6287df"), ("Travel & transport", "#38a18a"),
    ("Supplies & materials", "#d69b43"), ("Utilities", "#8a78c9"),
    ("Rent & facilities", "#6b91a8"), ("Professional services", "#4e9aa7"),
    ("Equipment", "#be7c58"), ("Marketing", "#bb79a5"), ("Other", "#8491a4"),
]


def _now():
    return datetime.now().isoformat(timespec="seconds")


def init_db():
    """Migrate InvoiceFlow's v5 database with additive, non-destructive tables."""
    with core.db_connect() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(invoices)")}
        additions = {
            "category": "TEXT NOT NULL DEFAULT ''", "project": "TEXT NOT NULL DEFAULT ''",
            "due_date": "TEXT NOT NULL DEFAULT ''", "vat_amount": "TEXT NOT NULL DEFAULT ''",
            "gross_amount": "TEXT NOT NULL DEFAULT ''", "payment_status": "TEXT NOT NULL DEFAULT 'Unknown'",
            "approved_at": "TEXT NOT NULL DEFAULT ''", "exported_at": "TEXT NOT NULL DEFAULT ''",
            "archived_at": "TEXT NOT NULL DEFAULT ''", "deleted_at": "TEXT NOT NULL DEFAULT ''", "workflow_status": "TEXT NOT NULL DEFAULT 'Needs review'",
        }
        for name, definition in additions.items():
            if name not in cols:
                con.execute(f"ALTER TABLE invoices ADD COLUMN {name} {definition}")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS suppliers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
          vat_number TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
          address TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', logo_path TEXT NOT NULL DEFAULT '',
          default_category TEXT NOT NULL DEFAULT '', ocr_rules_json TEXT NOT NULL DEFAULT '{}',
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS expense_categories(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
          color TEXT NOT NULL DEFAULT '#8491a4', active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS category_rules(
          id INTEGER PRIMARY KEY AUTOINCREMENT, keyword TEXT NOT NULL DEFAULT '', supplier_id INTEGER,
          category_id INTEGER NOT NULL, priority INTEGER NOT NULL DEFAULT 100,
          created_at TEXT NOT NULL, UNIQUE(keyword,supplier_id,category_id));
        CREATE TABLE IF NOT EXISTS budgets(
          id INTEGER PRIMARY KEY AUTOINCREMENT, period TEXT NOT NULL, category_id INTEGER,
          currency TEXT NOT NULL, amount REAL NOT NULL, notes TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(period,category_id,currency));
        CREATE TABLE IF NOT EXISTS tags(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
          color TEXT NOT NULL DEFAULT '#6f86bd', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS invoice_tags(
          invoice_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, PRIMARY KEY(invoice_id,tag_id));
        CREATE TABLE IF NOT EXISTS invoice_attachments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL, path TEXT NOT NULL,
          name TEXT NOT NULL, file_hash TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recycle_bin(
          id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id_original INTEGER NOT NULL,
          vendor TEXT NOT NULL DEFAULT '', source_name TEXT NOT NULL DEFAULT '', record_json TEXT NOT NULL,
          attachments_json TEXT NOT NULL DEFAULT '[]', moved_files_json TEXT NOT NULL DEFAULT '{}',
          deleted_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS watched_folders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE, recursive INTEGER NOT NULL DEFAULT 1,
          enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_scan TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS watched_files(
          path TEXT PRIMARY KEY, file_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'processed',
          seen_at TEXT NOT NULL, processed_at TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS app_settings(
          key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS report_profiles(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
          config_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS export_templates(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
          config_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS scheduled_jobs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, job_type TEXT NOT NULL,
          config_json TEXT NOT NULL, interval_minutes INTEGER NOT NULL DEFAULT 1440,
          enabled INTEGER NOT NULL DEFAULT 1, last_run TEXT NOT NULL DEFAULT '', next_run TEXT NOT NULL,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS projects(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
          description TEXT NOT NULL DEFAULT '', client TEXT NOT NULL DEFAULT '',
          active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS saved_views(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
          config_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_invoice_category ON invoices(category);
        CREATE INDEX IF NOT EXISTS idx_recycle_deleted_at ON recycle_bin(deleted_at);
        CREATE INDEX IF NOT EXISTS idx_attachments_invoice ON invoice_attachments(invoice_id);
        """)
        supplier_cols={r["name"] for r in con.execute("PRAGMA table_info(suppliers)")}
        if "logo_path" not in supplier_cols: con.execute("ALTER TABLE suppliers ADD COLUMN logo_path TEXT NOT NULL DEFAULT ''")
        now = _now()
        for name, color in DEFAULT_CATEGORIES:
            con.execute("INSERT OR IGNORE INTO expense_categories(name,color,created_at,updated_at) VALUES(?,?,?,?)",
                        (name, color, now, now))
        # Seed tags once; users can rename or add their own.
        for name, color in [("Urgent", "#c75c65"), ("Recurring", "#4e91b4"), ("Capital expense", "#8a78c9")]:
            con.execute("INSERT OR IGNORE INTO tags(name,color,created_at) VALUES(?,?,?)", (name, color, now))
        con.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('default_currency','RON',?) ON CONFLICT(key) DO NOTHING", (now,))
        con.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('auto_backup_enabled','false',?) ON CONFLICT(key) DO NOTHING", (now,))
        con.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('backup_retention','10',?) ON CONFLICT(key) DO NOTHING", (now,))
        con.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('auto_categorize','true',?) ON CONFLICT(key) DO NOTHING", (now,))
        con.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('default_language','ron+eng',?) ON CONFLICT(key) DO NOTHING", (now,))
        con.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('theme','light',?) ON CONFLICT(key) DO NOTHING", (now,))
        con.execute("INSERT INTO app_settings(key,value,updated_at) VALUES('density','comfortable',?) ON CONFLICT(key) DO NOTHING", (now,))
        con.execute("INSERT INTO app_meta(key,value) VALUES('workspace_schema_version','2') ON CONFLICT(key) DO UPDATE SET value=excluded.value")


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def upsert_supplier(name: str, vat_number: str = "", email: str = "", phone: str = "", address: str = "") -> int | None:
    name = normalize_name(name)
    if not name:
        return None
    now = _now()
    with core.db_connect() as con:
        con.execute("INSERT INTO suppliers(canonical_name,vat_number,email,phone,address,created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(canonical_name) DO UPDATE SET "
                    "vat_number=CASE WHEN excluded.vat_number<>'' THEN excluded.vat_number ELSE suppliers.vat_number END, "
                    "email=CASE WHEN excluded.email<>'' THEN excluded.email ELSE suppliers.email END, "
                    "phone=CASE WHEN excluded.phone<>'' THEN excluded.phone ELSE suppliers.phone END, "
                    "address=CASE WHEN excluded.address<>'' THEN excluded.address ELSE suppliers.address END, updated_at=excluded.updated_at",
                    (name, vat_number, email, phone, address, now, now))
        row = con.execute("SELECT id FROM suppliers WHERE canonical_name=? COLLATE NOCASE", (name,)).fetchone()
        return int(row[0]) if row else None


def supplier_invoice_history(supplier_id: int, limit=100):
    with core.db_connect() as con:
        supplier=con.execute("SELECT canonical_name FROM suppliers WHERE id=?",(int(supplier_id),)).fetchone()
    if not supplier: raise ValueError("Supplier not found.")
    name=supplier[0]
    with core.db_connect() as con:
        rows=[dict(r) for r in con.execute("SELECT id,invoice_number,invoice_date,due_date,amount,currency,status,duplicate_of,payment_status,category,source_name FROM invoices WHERE vendor=? COLLATE NOCASE AND COALESCE(deleted_at,'')='' ORDER BY id DESC LIMIT ?",(name,max(1,min(int(limit),500))))]
    return {"supplier":name,"invoices":rows,"count":len(rows)}


def supplier_list():
    with core.db_connect() as con:
        rows = con.execute("""SELECT s.*, COUNT(i.id) invoice_count,
          SUM(CASE WHEN i.status='Confirmed' AND COALESCE(i.duplicate_of,0)=0 THEN 1 ELSE 0 END) confirmed_count,
          MAX(i.invoice_date) latest_invoice FROM suppliers s LEFT JOIN invoices i
          ON i.vendor=s.canonical_name AND COALESCE(i.deleted_at,'')=''
          WHERE s.active=1 GROUP BY s.id ORDER BY s.canonical_name COLLATE NOCASE""").fetchall()
    result=[]
    for r in rows:
        item=dict(r)
        item["spend_by_currency"] = supplier_spend(item["canonical_name"])
        with core.db_connect() as con:
            item["aliases"]=[x[0] for x in con.execute("SELECT alias FROM vendor_aliases WHERE canonical_name=? COLLATE NOCASE ORDER BY alias",(item["canonical_name"],))]
        result.append(item)
    return result


def supplier_spend(name: str):
    with core.db_connect() as con:
        rows=con.execute("SELECT currency,amount FROM invoices WHERE vendor=? COLLATE NOCASE AND status='Confirmed' AND COALESCE(duplicate_of,0)=0 AND COALESCE(deleted_at,'')=''", (name,)).fetchall()
    sums={}
    for row in rows:
        value=core.parse_money(row["amount"])
        if value is not None:
            currency=str(row["currency"] or "Unknown")
            sums[currency]=sums.get(currency,0)+value
    return {k:round(v,2) for k,v in sums.items()}


def update_supplier(data: dict):
    sid=int(data.get("id") or 0)
    fields={k: normalize_name(data.get(k,"")) for k in ("canonical_name","vat_number","email","phone","address","website","notes","default_category","logo_path")}
    if len(fields["canonical_name"])<2:
        raise ValueError("Supplier name must contain at least two characters.")
    if fields["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", fields["email"]):
        raise ValueError("Enter a valid supplier email address.")
    if fields["website"] and not fields["website"].startswith(("https://","http://")):
        fields["website"]="https://"+fields["website"]
    now=_now()
    with core.db_connect() as con:
        if sid:
            con.execute("UPDATE suppliers SET canonical_name=?,vat_number=?,email=?,phone=?,address=?,website=?,notes=?,default_category=?,logo_path=?,updated_at=? WHERE id=?",
                (*fields.values(),now,sid))
        else:
            con.execute("INSERT INTO suppliers(canonical_name,vat_number,email,phone,address,website,notes,default_category,logo_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (*fields.values(),now,now))
        row=con.execute("SELECT id FROM suppliers WHERE canonical_name=? COLLATE NOCASE",(fields["canonical_name"],)).fetchone()
    return int(row[0])


def save_supplier_ocr_rules(supplier_id: int, rules: dict):
    allowed={"invoice_number","invoice_date","amount","currency","vat_amount"}
    clean={}
    for key,value in (rules or {}).items():
        if key not in allowed: continue
        pattern=str(value or "").strip()
        if len(pattern)>180: raise ValueError("A supplier OCR pattern is limited to 180 characters.")
        if pattern:
            re.compile(pattern, re.I)
            clean[key]=pattern
    with core.db_connect() as con:
        con.execute("UPDATE suppliers SET ocr_rules_json=?,updated_at=? WHERE id=?",(json.dumps(clean,ensure_ascii=False),_now(),int(supplier_id)))
    return clean


def supplier_rules_for_vendor(vendor: str):
    with core.db_connect() as con:
        row=con.execute("SELECT ocr_rules_json FROM suppliers WHERE canonical_name=? COLLATE NOCASE AND active=1",(normalize_name(vendor),)).fetchone()
    try: return json.loads(row[0] or "{}") if row else {}
    except Exception: return {}


def apply_supplier_ocr_rules(fields, text: str, vendor: str):
    rules=supplier_rules_for_vendor(vendor)
    outputs={"invoice_number":"invoice_number","invoice_date":"invoice_date","amount":"amount","currency":"currency","vat_amount":"vat_amount"}
    for key,attr in outputs.items():
        pattern=rules.get(key)
        if not pattern: continue
        try:
            match=re.search(pattern,text,re.I|re.M)
            if match:
                candidate=match.group(1) if match.lastindex else match.group(0)
                if key=="invoice_date": candidate=core.fmt_date(candidate)
                if key in {"amount","vat_amount"} and core.parse_money(candidate) is None: continue
                if key=="currency": candidate=candidate.upper()
                if candidate: setattr(fields,attr,candidate)
        except (re.error,IndexError,ValueError): continue
    return fields


def categories():
    with core.db_connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM expense_categories WHERE active=1 ORDER BY name COLLATE NOCASE")]


def save_category(name: str, color="#8491a4", category_id=None):
    name=normalize_name(name)
    if not name or len(name)>60: raise ValueError("Category name must be 1–60 characters.")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}",color): color="#8491a4"
    now=_now()
    with core.db_connect() as con:
        if category_id:
            con.execute("UPDATE expense_categories SET name=?,color=?,updated_at=? WHERE id=?",(name,color,now,int(category_id)))
        else:
            con.execute("INSERT INTO expense_categories(name,color,created_at,updated_at) VALUES(?,?,?,?)",(name,color,now,now))
    return True


def category_rules():
    with core.db_connect() as con:
        return [dict(r) for r in con.execute("SELECT r.*,c.name category,s.canonical_name supplier FROM category_rules r JOIN expense_categories c ON c.id=r.category_id LEFT JOIN suppliers s ON s.id=r.supplier_id ORDER BY r.priority,r.id DESC")]


def add_category_rule(keyword: str, category_id: int, supplier_id=None, priority=100):
    keyword=normalize_name(keyword)
    if len(keyword)<3 and not supplier_id: raise ValueError("Use a keyword of at least 3 characters or choose a supplier.")
    with core.db_connect() as con:
        con.execute("INSERT OR REPLACE INTO category_rules(keyword,supplier_id,category_id,priority,created_at) VALUES(?,?,?,?,?)",
                    (keyword,int(supplier_id) if supplier_id else None,int(category_id),int(priority),_now()))
    return True


def auto_categorize(invoice_id: int, vendor: str, raw_text: str=""):
    settings=get_settings()
    if str(settings.get("auto_categorize", "true")).lower() == "false":
        return ""
    sid=upsert_supplier(vendor) if vendor else None
    text=(vendor+" "+raw_text).casefold()
    with core.db_connect() as con:
        if sid:
            s=con.execute("SELECT default_category FROM suppliers WHERE id=?",(sid,)).fetchone()
            if s and s[0]:
                con.execute("UPDATE invoices SET category=? WHERE id=? AND COALESCE(category,'')=''",(s[0],invoice_id)); return s[0]
        rules=con.execute("SELECT r.keyword,r.supplier_id,c.name FROM category_rules r JOIN expense_categories c ON c.id=r.category_id ORDER BY r.priority ASC,r.id DESC").fetchall()
        for rule in rules:
            if rule["supplier_id"] and int(rule["supplier_id"])!=int(sid or -1): continue
            if not rule["keyword"] or rule["keyword"].casefold() in text:
                con.execute("UPDATE invoices SET category=? WHERE id=? AND COALESCE(category,'')=''",(rule["name"],invoice_id)); return rule["name"]
    return ""


def save_budget(period: str, currency: str, amount: float, category_id=None, notes=""):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])",str(period)): raise ValueError("Budget period must be YYYY-MM.")
    currency=str(currency or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}",currency): raise ValueError("Enter a three-letter currency code.")
    amount=float(amount)
    if amount<=0 or amount>1e12: raise ValueError("Budget amount must be positive and within a sensible range.")
    now=_now()
    with core.db_connect() as con:
        con.execute("INSERT INTO budgets(period,category_id,currency,amount,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
          "ON CONFLICT(period,category_id,currency) DO UPDATE SET amount=excluded.amount,notes=excluded.notes,updated_at=excluded.updated_at",
          (period,int(category_id) if category_id else None,currency,amount,str(notes),now,now))
    return True


def budgets():
    with core.db_connect() as con:
        return [dict(r) for r in con.execute("SELECT b.*,c.name category FROM budgets b LEFT JOIN expense_categories c ON c.id=b.category_id ORDER BY period DESC,category")]


def set_tags(invoice_id: int, names: list[str]):
    cleaned=[]
    for name in names or []:
        value=normalize_name(name)
        if value and len(value)<=30 and value.casefold() not in [x.casefold() for x in cleaned]: cleaned.append(value)
    with core.db_connect() as con:
        con.execute("DELETE FROM invoice_tags WHERE invoice_id=?",(int(invoice_id),))
        for name in cleaned[:20]:
            con.execute("INSERT OR IGNORE INTO tags(name,created_at) VALUES(?,?)",(name,_now()))
            tid=con.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE",(name,)).fetchone()[0]
            con.execute("INSERT OR IGNORE INTO invoice_tags(invoice_id,tag_id) VALUES(?,?)",(int(invoice_id),int(tid)))
    return cleaned


def create_tag(name: str, color="#8190a6"):
    name=normalize_name(name)
    if not name or len(name)>30: raise ValueError("Tag names must contain 1–30 characters.")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}",str(color or "")): raise ValueError("Choose a valid six-digit tag color.")
    with core.db_connect() as con:
        con.execute("INSERT INTO tags(name,color,created_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET color=excluded.color",(name,color,_now()))
        row=con.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE",(name,)).fetchone()
    return int(row[0])


def list_tags():
    with core.db_connect() as con:
        rows=con.execute("""SELECT t.id,t.name,t.color,COUNT(it.invoice_id) usage_count
            FROM tags t LEFT JOIN invoice_tags it ON it.tag_id=t.id
            GROUP BY t.id ORDER BY t.name COLLATE NOCASE""").fetchall()
    return [dict(r) for r in rows]


def rename_tag(tag_id: int, name: str, color=None):
    name=normalize_name(name)
    if not name or len(name)>30: raise ValueError("Tag names must contain 1–30 characters.")
    with core.db_connect() as con:
        row=con.execute("SELECT id,name,color FROM tags WHERE id=?",(int(tag_id),)).fetchone()
        if not row: raise ValueError("Tag not found.")
        clash=con.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE AND id<>?",(name,int(tag_id))).fetchone()
        if clash:
            other=int(clash[0])
            con.execute("INSERT OR IGNORE INTO invoice_tags(invoice_id,tag_id) SELECT invoice_id,? FROM invoice_tags WHERE tag_id=?",(other,int(tag_id)))
            con.execute("DELETE FROM invoice_tags WHERE tag_id=?",(int(tag_id),))
            con.execute("DELETE FROM tags WHERE id=?",(int(tag_id),))
            if color: con.execute("UPDATE tags SET color=? WHERE id=?",(str(color),other))
            return other
        con.execute("UPDATE tags SET name=?,color=COALESCE(?,color) WHERE id=?",(name,color,int(tag_id)))
    return int(tag_id)


def delete_tag(tag_id: int):
    with core.db_connect() as con:
        con.execute("DELETE FROM invoice_tags WHERE tag_id=?",(int(tag_id),))
        cur=con.execute("DELETE FROM tags WHERE id=?",(int(tag_id),))
    return bool(cur.rowcount)


def activity_log(limit=150):
    limit=max(1,min(int(limit),1000))
    with core.db_connect() as con:
        rows=con.execute("SELECT id,invoice_id,action,details,created_at FROM audit_log ORDER BY id DESC LIMIT ?",(limit,)).fetchall()
    return [dict(r) for r in rows]


def recognition_candidates(invoice_id: int):
    with core.db_connect() as con:
        row=con.execute("SELECT id,vendor,raw_text,field_confidence_json FROM invoices WHERE id=?",(int(invoice_id),)).fetchone()
    if not row: raise ValueError("Invoice not found.")
    text=row["raw_text"] or ""
    dates=[]; amounts=[]; numbers=[]
    for m in core.DATE_RE.finditer(text):
        v=core.fmt_date(m.group(0))
        if v and v not in dates: dates.append(v)
    for m in core.MONEY_RE.finditer(text):
        v=m.group(0)
        if v not in amounts: amounts.append(v)
    for line in text.splitlines():
        line=line.strip()
        if re.search(r"(?:invoice|factur[aă]|bon fiscal|nr\.?|number)\\b",line,re.I):
            for v in re.findall(r"\\b[A-Z0-9][A-Z0-9./_-]{3,}\\b",line,re.I):
                if any(c.isdigit() for c in v) and v not in numbers: numbers.append(v)
    try: confidence=json.loads(row["field_confidence_json"] or "{}")
    except Exception: confidence={}
    lines=[line.strip() for line in text.splitlines() if line.strip()]
    evidence={
        "supplier":[line for line in lines if row["vendor"] and row["vendor"].casefold() in line.casefold()][:6],
        "invoice_number":[line for line in lines if re.search(r"invoice|factur[aă]|bon fiscal|serie|number|nr\.?",line,re.I)][:8],
        "invoice_date":[line for line in lines if core.DATE_RE.search(line)][:8],
        "amount":[line for line in lines if core.MONEY_RE.search(line) and re.search(r"total|payable|achitat|pl[aă]tit|sum[aă]|valoare|amount",line,re.I)][:10],
        "vat":[line for line in lines if core.MONEY_RE.search(line) and re.search(r"TVA|VAT|tax",line,re.I)][:8],
    }
    return {"invoice_id":int(row["id"]),"vendor":row["vendor"],"dates":dates[:12],"amounts":amounts[:20],"invoice_numbers":numbers[:12],"field_confidence":confidence,"evidence":evidence,
            "note":"Candidates and evidence lines come from stored OCR text. They are suggestions, not verified invoice values; source text may contain OCR errors."}


def invoice_metadata(invoice_id: int):
    with core.db_connect() as con:
        tags=[r[0] for r in con.execute("SELECT t.name FROM tags t JOIN invoice_tags x ON x.tag_id=t.id WHERE x.invoice_id=? ORDER BY t.name",(int(invoice_id),))]
        attachments=[dict(r) for r in con.execute("SELECT * FROM invoice_attachments WHERE invoice_id=? ORDER BY id",(int(invoice_id),))]
        activity=[dict(r) for r in con.execute("SELECT action,details,created_at FROM audit_log WHERE invoice_id=? ORDER BY id DESC LIMIT 40",(int(invoice_id),))]
    return {"tags":tags,"attachments":attachments,"activity":activity}


def add_attachment(invoice_id: int, source_path: str, note=""):
    source=Path(source_path).expanduser()
    if not source.is_file(): raise FileNotFoundError("Choose an existing attachment.")
    if source.stat().st_size>100*1024*1024: raise ValueError("Attachments are limited to 100 MB.")
    folder=core.ORIGINALS/"Attachments"/str(int(invoice_id)); folder.mkdir(parents=True,exist_ok=True)
    target=folder/(uuid.uuid4().hex[:8]+"_"+source.name)
    shutil.copy2(source,target)
    digest=core.sha256_file(target)
    with core.db_connect() as con:
        con.execute("INSERT INTO invoice_attachments(invoice_id,path,name,file_hash,note,created_at) VALUES(?,?,?,?,?,?)",
                    (int(invoice_id),str(target),source.name,digest,str(note),_now()))
    core.audit(invoice_id,"attachment_added",source.name)
    return {"name":source.name,"path":str(target)}


def save_supplier_logo(supplier_id: int, source_path: str):
    source=Path(source_path).expanduser()
    if not source.is_file(): raise FileNotFoundError("Choose an existing image file.")
    if source.suffix.lower() not in {".png",".jpg",".jpeg",".webp"}: raise ValueError("Supplier logos must be PNG, JPG or WebP images.")
    if source.stat().st_size>8*1024*1024: raise ValueError("Supplier logos are limited to 8 MB.")
    folder=core.BASE/"Suppliers"/str(int(supplier_id)); folder.mkdir(parents=True,exist_ok=True)
    target=folder/("logo"+source.suffix.lower()); shutil.copy2(source,target)
    with core.db_connect() as con: con.execute("UPDATE suppliers SET logo_path=?,updated_at=? WHERE id=?",(str(target),_now(),int(supplier_id)))
    return str(target)


def set_workflow_status(invoice_id: int, status: str, note=""):
    allowed={"Imported","Processing","Needs review","Verified","Approved","Exported","Archived"}
    if status not in allowed: raise ValueError("Choose a supported workflow status.")
    with core.db_connect() as con:
        row=con.execute("SELECT status,duplicate_of FROM invoices WHERE id=?",(int(invoice_id),)).fetchone()
        if not row: raise ValueError("Invoice not found.")
        if status in {"Verified","Approved","Exported","Archived"} and (row["status"]!="Confirmed" or row["duplicate_of"]):
            raise ValueError("This invoice must be confirmed and have no unresolved duplicate warning first.")
        now=_now()
        con.execute("UPDATE invoices SET workflow_status=?,approved_at=CASE WHEN ?='Approved' THEN ? ELSE approved_at END,exported_at=CASE WHEN ?='Exported' THEN ? ELSE exported_at END,archived_at=CASE WHEN ?='Archived' THEN ? ELSE archived_at END,updated_at=? WHERE id=?",
                    (status,status,now,status,now,status,now,now,int(invoice_id)))
    core.audit(invoice_id,"workflow_status",f"{status}: {note}")
    return {"status":status}


def update_invoice_extra(invoice_id: int, category="", project="", due_date="", vat_amount="", payment_status="Unknown", tags=None, gross_amount=None):
    if due_date:
        due_date=core.fmt_date(due_date)
        if not due_date: raise ValueError("Due date must be a valid date.")
    if vat_amount and core.parse_money(vat_amount) is None: raise ValueError("VAT amount is invalid.")
    if gross_amount not in (None, "") and core.parse_money(gross_amount) is None: raise ValueError("Gross amount is invalid.")
    if payment_status not in {"Unknown","Unpaid","Partially paid","Paid","Overdue"}: raise ValueError("Invalid payment status.")
    with core.db_connect() as con:
        row=con.execute("SELECT id FROM invoices WHERE id=?",(int(invoice_id),)).fetchone()
        if not row: raise ValueError("Invoice not found.")
        if gross_amount is None:
            con.execute("UPDATE invoices SET category=?,project=?,due_date=?,vat_amount=?,payment_status=?,updated_at=? WHERE id=?",
                        (normalize_name(category),normalize_name(project),due_date,str(vat_amount),payment_status,_now(),int(invoice_id)))
        else:
            con.execute("UPDATE invoices SET category=?,project=?,due_date=?,vat_amount=?,gross_amount=?,payment_status=?,updated_at=? WHERE id=?",
                        (normalize_name(category),normalize_name(project),due_date,str(vat_amount),str(gross_amount),payment_status,_now(),int(invoice_id)))
    if tags is not None: set_tags(invoice_id,tags)
    core.audit(invoice_id,"metadata_updated",f"category={category}; project={project}; payment_status={payment_status}")



def project_list():
    with core.db_connect() as con:
        rows=con.execute("SELECT p.*,COUNT(i.id) invoice_count FROM projects p LEFT JOIN invoices i ON i.project=p.name AND COALESCE(i.deleted_at,'')='' WHERE p.active=1 GROUP BY p.id ORDER BY p.name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def save_project(name: str, description="", client="", project_id=None):
    name=normalize_name(name)
    if not name or len(name)>100: raise ValueError("Project name must be 1–100 characters.")
    now=_now()
    with core.db_connect() as con:
        if project_id:
            con.execute("UPDATE projects SET name=?,description=?,client=?,updated_at=? WHERE id=?",(name,str(description or "").strip(),normalize_name(client),now,int(project_id)))
        else:
            con.execute("INSERT INTO projects(name,description,client,created_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET description=excluded.description,client=excluded.client,updated_at=excluded.updated_at",(name,str(description or "").strip(),normalize_name(client),now,now))
    core.audit(None,"project_saved",name)
    return True


def saved_views():
    with core.db_connect() as con:
        rows=con.execute("SELECT * FROM saved_views ORDER BY name COLLATE NOCASE").fetchall()
    return [{"id":r["id"],"name":r["name"],"config":json.loads(r["config_json"] or "{}"),"updated_at":r["updated_at"]} for r in rows]


def save_saved_view(name: str, config: dict):
    name=normalize_name(name)
    if not name: raise ValueError("Saved view needs a name.")
    cfg={k:config.get(k) for k in ("query","status","category","currency","project","payment_status","tag")}
    now=_now()
    with core.db_connect() as con:
        con.execute("INSERT INTO saved_views(name,config_json,created_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET config_json=excluded.config_json,updated_at=excluded.updated_at",(name,json.dumps(cfg,ensure_ascii=False),now,now))
    return True


def delete_saved_view(view_id: int):
    with core.db_connect() as con: con.execute("DELETE FROM saved_views WHERE id=?",(int(view_id),))
    return True


def bulk_update_invoices(invoice_ids, category=None, project=None, add_tag=None, payment_status=None):
    ids=sorted({int(x) for x in (invoice_ids or [])})
    if not ids: return {"updated":0}
    if payment_status not in (None,"","Unknown","Unpaid","Partially paid","Paid","Overdue"):
        raise ValueError("Invalid payment status.")
    placeholders=','.join('?' for _ in ids)
    with core.db_connect() as con:
        rows=con.execute(f"SELECT id FROM invoices WHERE id IN ({placeholders})",ids).fetchall()
        found=[int(r[0]) for r in rows]
        sets=[]; vals=[]
        if category is not None: sets.append("category=?"); vals.append(normalize_name(category))
        if project is not None: sets.append("project=?"); vals.append(normalize_name(project))
        if payment_status not in (None,""): sets.append("payment_status=?"); vals.append(payment_status)
        if sets:
            sets.append("updated_at=?"); vals.append(_now())
            con.execute(f"UPDATE invoices SET {','.join(sets)} WHERE id IN ({','.join('?' for _ in found)})",[*vals,*found])
    if add_tag:
        for invoice_id in found:
            meta=invoice_metadata(invoice_id); set_tags(invoice_id,meta["tags"]+[str(add_tag)])
    for invoice_id in found: core.audit(invoice_id,"bulk_updated","Bulk invoice metadata update")
    return {"updated":len(found)}


def dashboard_data():
    with core.db_connect() as con:
        active=[dict(r) for r in con.execute("SELECT * FROM invoices WHERE COALESCE(deleted_at,'')='' AND COALESCE(archived_at,'')='' ORDER BY id DESC")]
    today=date.today(); month_start=today.replace(day=1); last12=(month_start.replace(day=1)-timedelta(days=335)).replace(day=1)
    confirmed=[r for r in active if r.get("status")=="Confirmed" and not r.get("duplicate_of") and (r.get("currency") or "").strip()]
    sums={}; monthly={}; suppliers={}; cats={}
    for r in confirmed:
        value=core.parse_money(r.get("amount",""))
        if value is None: continue
        cur=r.get("currency") or "Unknown"; sums[cur]=sums.get(cur,0)+value
        try: dt=datetime.strptime(core.fmt_date(r.get("invoice_date","")),"%d/%m/%Y")
        except ValueError: continue
        key=dt.strftime("%Y-%m")
        if dt.date()>=last12:
            monthly.setdefault(key,{}); monthly[key][cur]=monthly[key].get(cur,0)+value
        suppliers.setdefault(r.get("vendor") or "Unknown",{}); suppliers[r.get("vendor") or "Unknown"][cur]=suppliers[r.get("vendor") or "Unknown"].get(cur,0)+value
        cat=r.get("category") or "Uncategorized"; cats.setdefault(cat,{}); cats[cat][cur]=cats[cat].get(cur,0)+value
    reviews=sum(1 for r in active if r.get("status")!="Confirmed" or r.get("duplicate_of"))
    today=date.today(); overdue=0
    for r in active:
        if r.get("payment_status")=="Overdue": overdue+=1; continue
        if r.get("payment_status") not in {"Unpaid","Partially paid"}: continue
        due=core.fmt_date(r.get("due_date","")) if r.get("due_date") else ""
        try:
            if due and datetime.strptime(due,"%d/%m/%Y").date()<today: overdue+=1
        except ValueError: pass
    budget_rows=[]
    for b in budgets():
        actual=0.0
        for r in confirmed:
            try: period=datetime.strptime(core.fmt_date(r.get("invoice_date")),"%d/%m/%Y").strftime("%Y-%m")
            except ValueError: continue
            if period==b["period"] and r.get("currency")==b["currency"] and (not b.get("category") or r.get("category")==b.get("category")):
                actual+=core.parse_money(r.get("amount")) or 0
        budget_rows.append({"period":b["period"],"category":b.get("category") or "All categories","currency":b["currency"],"budget":float(b["amount"]),"actual":round(actual,2),"remaining":round(float(b["amount"])-actual,2),"utilization":round(actual/float(b["amount"])*100,1) if b["amount"] else 0})
    return {"counts":{"total":len(active),"review":reviews,"confirmed":len(active)-reviews,"duplicates":sum(bool(r.get("duplicate_of")) for r in active),
        "overdue":overdue},"budget_vs_actual":budget_rows,
        "totals_by_currency":{k:round(v,2) for k,v in sums.items()},
        "monthly":[{"period":k,"totals":{c:round(v,2) for c,v in vals.items()}} for k,vals in sorted(monthly.items())],
        "suppliers":[{"name":k,"totals":{c:round(v,2) for c,v in vals.items()}} for k,vals in sorted(suppliers.items(),key=lambda x:sum(x[1].values()),reverse=True)[:12]],
        "categories":[{"name":k,"totals":{c:round(v,2) for c,v in vals.items()}} for k,vals in sorted(cats.items(),key=lambda x:sum(x[1].values()),reverse=True)],
        "pending_total":sum(1 for r in active if not r.get("vendor") or not r.get("invoice_number") or not r.get("invoice_date") or not r.get("amount") or not r.get("currency")),
    }


def analytics_data(start="", end="", status="all", currency="all", category="all"):
    sd=core.fmt_date(start) if start else ""; ed=core.fmt_date(end) if end else ""
    if start and not sd: raise ValueError("Analytics start date is invalid.")
    if end and not ed: raise ValueError("Analytics end date is invalid.")
    if sd and ed and datetime.strptime(sd,"%d/%m/%Y")>datetime.strptime(ed,"%d/%m/%Y"):
        raise ValueError("Analytics start date must be on or before the end date.")
    rows=[r for r in core.records() if not r.get("deleted_at") and not r.get("archived_at")]
    if status not in (None,"","all"): rows=[r for r in rows if r.get("status")==status]
    if currency not in (None,"","all"): rows=[r for r in rows if r.get("currency")==currency]
    if category not in (None,"","all"): rows=[r for r in rows if (r.get("category") or "Uncategorized")==category]
    if sd or ed:
        filtered=[]
        for r in rows:
            normalized=core.fmt_date(r.get("invoice_date", ""))
            if not normalized: continue
            current=datetime.strptime(normalized,"%d/%m/%Y")
            if sd and current<datetime.strptime(sd,"%d/%m/%Y"): continue
            if ed and current>datetime.strptime(ed,"%d/%m/%Y"): continue
            filtered.append(r)
        rows=filtered
    totals={}; counts={}; vat={}; monthly={}; suppliers={}; categories={}
    confirmed=[]
    for r in rows:
        if r.get("status")!="Confirmed" or r.get("duplicate_of") or not r.get("currency"): continue
        amount=core.parse_money(r.get("amount")); cur=r["currency"]
        if amount is None: continue
        confirmed.append(r); totals[cur]=totals.get(cur,0)+amount; counts[cur]=counts.get(cur,0)+1
        tax=core.parse_money(r.get("vat_amount"))
        if tax is not None: vat[cur]=vat.get(cur,0)+tax
        dt=core.fmt_date(r.get("invoice_date",""))
        if dt:
            period=datetime.strptime(dt,"%d/%m/%Y").strftime("%Y-%m")
            monthly.setdefault(period,{}); monthly[period][cur]=monthly[period].get(cur,0)+amount
        supplier=r.get("vendor") or "Unknown supplier"; suppliers.setdefault(supplier,{})
        suppliers[supplier][cur]=suppliers[supplier].get(cur,0)+amount
        category=r.get("category") or "Uncategorized"; categories.setdefault(category,{})
        categories[category][cur]=categories[category].get(cur,0)+amount
    comparison={}
    if sd and ed:
        current_start=datetime.strptime(sd,"%d/%m/%Y").date(); current_end=datetime.strptime(ed,"%d/%m/%Y").date()
        span=(current_end-current_start).days+1
        previous_start=current_start-timedelta(days=span); previous_end=current_start-timedelta(days=1)
        previous_totals={}
        for r in core.records():
            normalized=core.fmt_date(r.get("invoice_date",""))
            if not normalized or r.get("status")!="Confirmed" or r.get("duplicate_of") or not r.get("currency"): continue
            if currency not in (None,"","all") and r.get("currency")!=currency: continue
            if category not in (None,"","all") and (r.get("category") or "Uncategorized")!=category: continue
            dt=datetime.strptime(normalized,"%d/%m/%Y").date()
            if previous_start<=dt<=previous_end:
                amount=core.parse_money(r.get("amount"))
                if amount is not None: previous_totals[r["currency"]]=previous_totals.get(r["currency"],0)+amount
        comparison={cur:{"previous":round(previous_totals.get(cur,0),2),"current":round(value,2),
                         "change":round(value-previous_totals.get(cur,0),2),
                         "change_percent":round((value-previous_totals.get(cur,0))/previous_totals[cur]*100,1) if previous_totals.get(cur) else None}
                    for cur,value in totals.items()}
        for cur,value in previous_totals.items():
            comparison.setdefault(cur,{"previous":round(value,2),"current":0.0,"change":round(-value,2),"change_percent":-100.0})
    return {
        "period":{"start":sd,"end":ed,"records":len(rows)},
        "totals_by_currency":{k:round(v,2) for k,v in totals.items()},
        "invoice_counts":counts,"vat_by_currency":{k:round(v,2) for k,v in vat.items()},
        "monthly":[{"period":k,"totals":{c:round(v,2) for c,v in values.items()}} for k,values in sorted(monthly.items())],
        "suppliers":[{"name":k,"totals":{c:round(v,2) for c,v in values.items()}} for k,values in sorted(suppliers.items(),key=lambda x:sum(x[1].values()),reverse=True)],
        "categories":[{"name":k,"totals":{c:round(v,2) for c,v in values.items()}} for k,values in sorted(categories.items(),key=lambda x:sum(x[1].values()),reverse=True)],
        "comparison":comparison,
        "counts":{"records":len(rows),"confirmed":len(confirmed),"review":len(rows)-len(confirmed),"duplicates":sum(bool(r.get("duplicate_of")) for r in rows)},
    }


def add_watched_folder(path: str, recursive=True):
    folder=Path(path).expanduser().resolve()
    if not folder.is_dir(): raise ValueError("Select an existing folder to watch.")
    now=_now()
    with core.db_connect() as con:
        con.execute("INSERT OR IGNORE INTO watched_folders(path,recursive,enabled,created_at,last_scan) VALUES(?,?,1,?,?)",
                    (str(folder),1 if recursive else 0,now,now))
    return True


def watched_folders():
    with core.db_connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM watched_folders ORDER BY path")]


def remove_watched_folder(folder_id: int):
    with core.db_connect() as con: con.execute("DELETE FROM watched_folders WHERE id=?",(int(folder_id),))
    return True


def scan_watched_folders():
    new=[]; now=_now()
    with core.db_connect() as con:
        folders=[dict(r) for r in con.execute("SELECT * FROM watched_folders WHERE enabled=1")]
    for folder in folders:
        root=Path(folder["path"])
        if not root.is_dir(): continue
        iterator=root.rglob("*") if folder["recursive"] else root.glob("*")
        for path in iterator:
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in (core.SUPPORTED|{".zip",".xml"}): continue
            try:
                stat=path.stat()
                # Process stable files already present when a folder is first watched too.
                # The stability window, rather than folder creation time, prevents partial copies.
                if datetime.now().timestamp()-stat.st_mtime<3: continue # avoid files still being copied
                digest=core.sha256_file(path)
                with core.db_connect() as con:
                    prior=con.execute("SELECT file_hash,status FROM watched_files WHERE path=?",(str(path),)).fetchone()
                    if prior and prior["file_hash"]==digest and prior["status"] in {"processed","pending"}: continue
                    # Exact content already imported by any route does not need a new watch import.
                    exists=con.execute("SELECT 1 FROM invoices WHERE file_hash=? LIMIT 1",(digest,)).fetchone()
                    if exists:
                        con.execute("INSERT OR REPLACE INTO watched_files(path,file_hash,status,seen_at,processed_at) VALUES(?,?,'processed',?,?)",(str(path),digest,now,now)); continue
                    con.execute("INSERT OR REPLACE INTO watched_files(path,file_hash,status,seen_at) VALUES(?,?,'pending',?)",(str(path),digest,now))
                new.append(str(path))
            except (OSError, PermissionError): continue
        with core.db_connect() as con: con.execute("UPDATE watched_folders SET last_scan=? WHERE id=?",(now,folder["id"]))
    return new


def mark_watched_processed(paths):
    now=_now()
    for raw in paths or []:
        p=Path(raw)
        try: digest=core.sha256_file(p)
        except OSError: continue
        with core.db_connect() as con:
            con.execute("INSERT OR REPLACE INTO watched_files(path,file_hash,status,seen_at,processed_at) VALUES(?,?,'processed',?,?)",(str(p),digest,now,now))


def get_settings():
    with core.db_connect() as con: rows={r["key"]:r["value"] for r in con.execute("SELECT key,value FROM app_settings")}
    return rows


def save_settings(values: dict):
    allowed={"default_currency","auto_backup_enabled","backup_retention","backup_interval_hours","default_language","theme","density","watched_scan_seconds","auto_categorize","auto_export_folder","auto_backup_last","last_opened_view","dashboard_cards","saved_filter_default","pin_lock_on_start"}
    now=_now()
    with core.db_connect() as con:
        for key,value in (values or {}).items():
            if key not in allowed: continue
            if isinstance(value,(dict,list)): value=json.dumps(value,ensure_ascii=False)
            con.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,str(value),now))
    return get_settings()


def set_passcode(pin: str):
    pin=str(pin or "")
    if len(pin)<6 or len(pin)>128: raise ValueError("Passcode must contain at least 6 characters.")
    salt=os.urandom(16); digest=hashlib.pbkdf2_hmac("sha256",pin.encode(),salt,240000)
    with core.db_connect() as con:
        con.executemany("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            [("pin_salt",salt.hex(),_now()),("pin_hash",digest.hex(),_now())])
    return True


def verify_passcode(pin: str):
    settings=get_settings(); salt=settings.get("pin_salt",""); expected=settings.get("pin_hash","")
    if not salt or not expected: return True
    actual=hashlib.pbkdf2_hmac("sha256",str(pin or "").encode(),bytes.fromhex(salt),240000).hex()
    return hmac.compare_digest(actual,expected)


def save_report_profile(name: str, config: dict):
    name=normalize_name(name)
    if not name: raise ValueError("Report name is required.")
    with core.db_connect() as con:
        con.execute("INSERT INTO report_profiles(name,config_json,created_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET config_json=excluded.config_json,updated_at=excluded.updated_at",
                    (name,json.dumps(config,ensure_ascii=False),_now(),_now()))
    return True


def delete_report_profile(profile_id: int):
    with core.db_connect() as con: cur=con.execute("DELETE FROM report_profiles WHERE id=?",(int(profile_id),))
    return bool(cur.rowcount)


def report_profiles():
    with core.db_connect() as con:
        return [{"id":r["id"],"name":r["name"],"config":json.loads(r["config_json"] or "{}"),"updated_at":r["updated_at"]} for r in con.execute("SELECT * FROM report_profiles ORDER BY name")]


def parse_ubl_invoice(path: Path):
    """Extract common EN16931/UBL fields used by ANAF's RO e-Factura UBL files.

    This extracts business terms; it does NOT validate the XML against the full
    RO-CIUS schema or check an ANAF signature/seal.
    """
    path=Path(path)
    if path.stat().st_size>50*1024*1024: raise ValueError("XML invoice exceeds the 50 MB import limit.")
    root=ET.parse(path).getroot()
    def lname(el): return el.tag.rsplit("}",1)[-1].split(":")[-1]
    def child(parent,name):
        if parent is None: return None
        return next((x for x in list(parent) if lname(x)==name),None)
    def text(el): return (el.text or "").strip() if el is not None else ""
    def descendants(parent,name): return [x for x in parent.iter() if lname(x)==name] if parent is not None else []
    if lname(root) not in {"Invoice","CreditNote"}:
        raise ValueError("Unsupported XML root. Expected UBL Invoice or CreditNote.")
    number=text(child(root,"ID")); issue=text(child(root,"IssueDate")); due=text(child(root,"DueDate"))
    currency=text(child(root,"DocumentCurrencyCode")).upper()
    supplier_node=child(root,"AccountingSupplierParty")
    party=child(supplier_node,"Party")
    if party is None: party=supplier_node
    legal=child(party,"PartyLegalEntity"); p_name=child(party,"PartyName")
    supplier=text(child(legal,"RegistrationName")) or text(child(p_name,"Name")) or text(child(party,"Name"))
    if not supplier:
        vals=descendants(party,"RegistrationName") or descendants(party,"Name")
        supplier=text(vals[0]) if vals else ""
    tax_node=child(root,"TaxTotal"); tax_amount_node=child(tax_node,"TaxAmount")
    payable_node=child(child(root,"LegalMonetaryTotal"),"PayableAmount")
    payable=text(payable_node); payable_currency=(payable_node.attrib.get("currencyID","") if payable_node is not None else "").upper()
    if lname(root)=="CreditNote" and payable and core.parse_money(payable) is not None:
        payable=str(-abs(core.parse_money(payable)))
    if payable_currency: currency=payable_currency
    tax=text(tax_amount_node)
    due_fmt=core.fmt_date(due) if due else ""
    issue_fmt=core.fmt_date(issue) if issue else ""
    if issue and not issue_fmt: raise ValueError("UBL invoice issue date is invalid.")
    if not number or not issue_fmt or not supplier or not payable:
        raise ValueError("UBL invoice is missing one or more required fields (invoice number, supplier, issue date, payable total).")
    return {"vendor":supplier,"invoice_number":number,"invoice_date":issue_fmt,"due_date":due_fmt,
            "amount":payable,"currency":currency,"vat_amount":tax,"gross_amount":text(child(child(root,"LegalMonetaryTotal"),"TaxInclusiveAmount")),
            "raw_text":f"UBL XML invoice: {number}\nSupplier: {supplier}\nIssue date: {issue}\nPayable: {payable} {currency}\nVAT: {tax}",
            "field_confidence":{"vendor":1.0,"invoice_number":1.0,"invoice_date":1.0,"amount":1.0,"currency":1.0 if currency else 0.0},
            "ocr_confidence":1.0,"status":"Needs review","review_notes":"Parsed from UBL XML. Manual verification is still required; XML structure and ANAF signature were not validated."}


def recycle_invoices(invoice_ids, move_originals=True):
    """Move invoices and tracked local documents into a restorable recycle bin.

    File moves are compensated if the SQLite transaction fails. Files referenced
    by still-active invoices are retained. A shared file among the selected
    invoices is moved once and referenced by each corresponding recycle item.
    """
    ids=sorted({int(x) for x in invoice_ids or []})
    if not ids: return {"count":0,"moved_files":0,"retained_shared_files":0}
    bin_root=core.BASE/"RecycleBin"/"files"; bin_root.mkdir(parents=True,exist_ok=True)
    moved_total=0; retained_shared=0; moved_assets={}
    try:
        with core.db_connect() as con:
            placeholders=','.join('?' for _ in ids)
            rows=[dict(r) for r in con.execute(f"SELECT * FROM invoices WHERE id IN ({placeholders})",ids)]
            if not rows: raise ValueError("No selected invoices were found.")
            attachments_by_id={row["id"]:[dict(r) for r in con.execute("SELECT * FROM invoice_attachments WHERE invoice_id=?",(row["id"],))] for row in rows}
            all_live=[dict(r) for r in con.execute(f"SELECT id,source_path,source_input_path FROM invoices WHERE id NOT IN ({placeholders})",ids)]
            refs=set()
            for r in all_live:
                for value in (str(r.get("source_path") or "")+"\n"+str(r.get("source_input_path") or "")).splitlines():
                    if value.strip(): refs.add(str(Path(value.strip()).resolve(strict=False)))
                for a in con.execute("SELECT path FROM invoice_attachments WHERE invoice_id=?",(r["id"],)):
                    refs.add(str(Path(a[0]).resolve(strict=False)))
            for row in rows:
                record_json=json.dumps(row,ensure_ascii=False)
                attachments=attachments_by_id[row["id"]]
                moved={}
                paths=[]
                paths.extend((p,"managed") for p in str(row.get("source_path") or "").splitlines() if p.strip())
                if move_originals:
                    paths.extend((p,"original") for p in str(row.get("source_input_path") or "").splitlines() if p.strip())
                paths.extend((a["path"],"attachment") for a in attachments)
                for raw,kind in paths:
                    original=Path(raw).expanduser()
                    if original.is_symlink() or any(parent.is_symlink() for parent in original.parents): continue
                    resolved=original.resolve(strict=False); key=str(resolved)
                    if kind in {"managed","attachment"} and not resolved.is_relative_to(core.ORIGINALS.resolve()): continue
                    if kind=="original" and resolved.suffix.lower() not in (core.SUPPORTED|{".zip",".xml"}): continue
                    if key in refs:
                        retained_shared+=1; continue
                    if key in moved_assets:
                        # Each recycle row stores only the metadata object. The
                        # original path is already the dictionary key; storing
                        # the entire compensation tuple corrupts shared-file rows.
                        moved[key]=moved_assets[key][1]
                        continue
                    if not resolved.is_file(): continue
                    dest=bin_root/(uuid.uuid4().hex[:10]+"_"+resolved.name)
                    shutil.move(str(resolved),str(dest))
                    meta={"recycled":str(dest),"kind":kind}
                    moved_assets[key]=(str(resolved),meta)
                    moved[key]=meta; moved_total+=1
                con.execute("INSERT INTO recycle_bin(invoice_id_original,vendor,source_name,record_json,attachments_json,moved_files_json,deleted_at) VALUES(?,?,?,?,?,?,?)",
                    (row["id"],row.get("vendor", ""),row.get("source_name", ""),record_json,json.dumps(attachments,ensure_ascii=False),json.dumps(moved,ensure_ascii=False),_now()))
                con.execute("DELETE FROM invoice_tags WHERE invoice_id=?",(row["id"],))
                con.execute("DELETE FROM invoice_attachments WHERE invoice_id=?",(row["id"],))
                con.execute("DELETE FROM invoices WHERE id=?",(row["id"],))
                con.execute("INSERT INTO audit_log(invoice_id,action,details,created_at) VALUES(?,?,?,?)",(row["id"],"recycled",f"Moved to recycle bin; moved_originals={move_originals}",_now()))
    except Exception:
        # Restore any physical file already moved if the DB transaction rolled back.
        for original,meta in reversed(list(moved_assets.values())):
            try:
                source=Path(meta["recycled"]); destination=Path(original)
                if source.exists() and not destination.exists():
                    destination.parent.mkdir(parents=True,exist_ok=True); shutil.move(str(source),str(destination))
            except OSError as rollback_error:
                core.log_error(f"Recycle rollback could not restore {original}: {rollback_error}")
        raise
    return {"count":len(rows),"moved_files":moved_total,"retained_shared_files":retained_shared}


def recycle_bin_list():
    with core.db_connect() as con:
        rows=[dict(r) for r in con.execute("SELECT id,invoice_id_original,vendor,source_name,deleted_at FROM recycle_bin ORDER BY deleted_at DESC")]
    return rows


def restore_recycled(item_id: int):
    with core.db_connect() as con:
        item=con.execute("SELECT * FROM recycle_bin WHERE id=?",(int(item_id),)).fetchone()
        if not item: raise ValueError("Recycle-bin item not found.")
        row=json.loads(item["record_json"]); moved=json.loads(item["moved_files_json"] or "{}")
        attachments=json.loads(item["attachments_json"] or "[]")
        restored={}
        for original,meta in moved.items():
            source=Path(meta["recycled"])
            if not source.is_file(): continue
            destination=Path(original)
            if destination.exists():
                if core.sha256_file(destination)==core.sha256_file(source):
                    source.unlink(missing_ok=True); restored[original]=str(destination); continue
                destination=core.ORIGINALS/"Restored"/(uuid.uuid4().hex[:8]+"_"+destination.name)
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.move(str(source),str(destination)); restored[original]=str(destination)
        def replace_paths(value):
            pieces=[]
            for raw in str(value or "").splitlines():
                key=str(Path(raw).resolve(strict=False))
                pieces.append(restored.get(key,raw))
            return "\n".join(pieces)
        row["source_path"]=replace_paths(row.get("source_path"))
        row["source_input_path"]=replace_paths(row.get("source_input_path"))
        cols={r["name"] for r in con.execute("PRAGMA table_info(invoices)")}
        row={k:v for k,v in row.items() if k in cols}
        oldid=int(row.pop("id",0) or 0)
        exists=con.execute("SELECT 1 FROM invoices WHERE id=?",(oldid,)).fetchone() if oldid else None
        if exists: oldid=None
        keys=list(row)
        if oldid is not None: keys.insert(0,"id"); row["id"]=oldid
        sql=f"INSERT INTO invoices({','.join(keys)}) VALUES({','.join('?' for _ in keys)})"
        con.execute(sql,[row.get(k) for k in keys])
        new_id=int(oldid or con.execute("SELECT last_insert_rowid()").fetchone()[0])
        for a in attachments:
            old_path=str(a.get("path", "")); a["path"]=restored.get(str(Path(old_path).resolve(strict=False)),old_path)
            con.execute("INSERT INTO invoice_attachments(invoice_id,path,name,file_hash,note,created_at) VALUES(?,?,?,?,?,?)",
                        (new_id,a["path"],a.get("name",""),a.get("file_hash",""),a.get("note",""),a.get("created_at") or _now()))
        con.execute("DELETE FROM recycle_bin WHERE id=?",(int(item_id),))
        con.execute("INSERT INTO audit_log(invoice_id,action,details,created_at) VALUES(?,?,?,?)",(new_id,"restored","Restored from recycle bin",_now()))
    return {"restored":True,"invoice_id":new_id}


def permanently_delete_recycled(item_ids):
    ids=sorted({int(x) for x in item_ids or []})
    if not ids:return {"deleted":0,"files":0}
    removed=0; deleted=0
    with core.db_connect() as con:
        remaining=[dict(r) for r in con.execute("SELECT id,moved_files_json FROM recycle_bin")]
        refs={}
        for item in remaining:
            try:
                for meta in json.loads(item.get("moved_files_json") or "{}").values():
                    if isinstance(meta, list) and len(meta) >= 2 and isinstance(meta[-1], dict):
                        meta = meta[-1]  # tolerate early 5.1 development rows
                    if not isinstance(meta, dict): continue
                    path=str(Path(meta.get("recycled", "")).resolve(strict=False))
                    refs[path]=refs.get(path,0)+1
            except Exception: continue
        for item_id in ids:
            row=con.execute("SELECT moved_files_json FROM recycle_bin WHERE id=?",(item_id,)).fetchone()
            if not row: continue
            moved=json.loads(row[0] or "{}")
            for meta in moved.values():
                if isinstance(meta, list) and len(meta) >= 2 and isinstance(meta[-1], dict):
                    meta = meta[-1]
                if not isinstance(meta, dict): continue
                path=Path(meta.get("recycled","")); key=str(path.resolve(strict=False))
                # The same source can be attached to several selected invoices.
                # Delete only when no other recycle item still refers to it.
                refs[key]=refs.get(key,0)-1
                if refs[key]<=0:
                    try:
                        if path.is_file() and not path.is_symlink(): path.unlink(); removed+=1
                    except OSError as exc: core.log_error(f"Recycle-bin permanent delete failed: {exc}")
            con.execute("DELETE FROM recycle_bin WHERE id=?",(item_id,)); deleted+=1
    return {"deleted":deleted,"files":removed}


def export_csv(path: Path, start_date="", end_date="", status="all", currency="all", category="all"):
    rows=core.records(); sd=core.fmt_date(start_date) if start_date else ""; ed=core.fmt_date(end_date) if end_date else ""
    def include(r):
        d=core.fmt_date(r.get("invoice_date", ""))
        if sd and (not d or datetime.strptime(d,"%d/%m/%Y")<datetime.strptime(sd,"%d/%m/%Y")): return False
        if ed and (not d or datetime.strptime(d,"%d/%m/%Y")>datetime.strptime(ed,"%d/%m/%Y")): return False
        if status!="all" and r.get("status")!=status:return False
        if currency!="all" and r.get("currency")!=currency:return False
        if category!="all" and r.get("category")!=category:return False
        return True
    exported=0
    with Path(path).open("w",newline="",encoding="utf-8-sig") as f:
        writer=csv.writer(f,delimiter=";")
        writer.writerow(["Invoice date","Due date","Supplier","Invoice number","Total payable","Currency","VAT amount","Category","Project","Payment status","Review status","Source file","Notes"])
        for r in rows:
            if not include(r):continue
            writer.writerow([r.get("invoice_date"),r.get("due_date"),r.get("vendor"),r.get("invoice_number"),r.get("amount"),r.get("currency"),r.get("vat_amount"),r.get("category"),r.get("project"),r.get("payment_status"),r.get("status"),r.get("source_name"),r.get("review_notes")]); exported+=1
    return exported


EXPORT_COLUMN_OPTIONS = {
    "invoice_date": ("Invoice date", 15), "due_date": ("Due date", 15),
    "vendor": ("Supplier", 30), "invoice_number": ("Invoice number", 22),
    "amount": ("Total payable", 18), "currency": ("Currency", 12),
    "vat_amount": ("VAT amount", 15), "category": ("Category", 24),
    "project": ("Project", 20), "payment_status": ("Payment status", 18),
    "status": ("Review status", 20), "tags": ("Tags", 24),
    "source_name": ("Source file", 36), "review_notes": ("Review notes", 55),
}


def export_templates():
    with core.db_connect() as con:
        rows=con.execute("SELECT * FROM export_templates ORDER BY name COLLATE NOCASE").fetchall()
    return [{"id":r["id"],"name":r["name"],"config":json.loads(r["config_json"] or "{}"),"updated_at":r["updated_at"]} for r in rows]


def save_export_template(name: str, config: dict):
    name=normalize_name(name)
    if not name or len(name)>80: raise ValueError("Template name must contain 1–80 characters.")
    config=config or {}
    selected=[x for x in config.get("columns",[]) if x in EXPORT_COLUMN_OPTIONS]
    if not selected: raise ValueError("Choose at least one tracker column.")
    clean={"columns":selected,"start_date":str(config.get("start_date") or ""),"end_date":str(config.get("end_date") or ""),
           "status":str(config.get("status") or "all"),"currency":str(config.get("currency") or "all"),"category":str(config.get("category") or "all")}
    now=_now()
    with core.db_connect() as con:
        con.execute("INSERT INTO export_templates(name,config_json,created_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET config_json=excluded.config_json,updated_at=excluded.updated_at",
                    (name,json.dumps(clean,ensure_ascii=False),now,now))
        row=con.execute("SELECT id FROM export_templates WHERE name=? COLLATE NOCASE",(name,)).fetchone()
    core.audit(None,"export_template_saved",name)
    return int(row[0])


def delete_export_template(template_id: int):
    with core.db_connect() as con: cur=con.execute("DELETE FROM export_templates WHERE id=?",(int(template_id),))
    return bool(cur.rowcount)


def export_workspace_xlsx(path: Path, config: dict | None = None):
    """Rich multi-sheet workbook with filters, VAT/category/supplier summaries and budgets."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    config=config or {}
    rows=core.records()
    sd=core.fmt_date(config.get("start_date","")) if config.get("start_date") else ""
    ed=core.fmt_date(config.get("end_date","")) if config.get("end_date") else ""
    def include(r):
        if config.get("status","all")!="all" and r.get("status")!=config["status"]: return False
        if config.get("currency","all")!="all" and r.get("currency")!=config["currency"]: return False
        if config.get("category","all")!="all" and r.get("category")!=config["category"]: return False
        d=core.fmt_date(r.get("invoice_date",""))
        if sd and (not d or datetime.strptime(d,"%d/%m/%Y")<datetime.strptime(sd,"%d/%m/%Y")): return False
        if ed and (not d or datetime.strptime(d,"%d/%m/%Y")>datetime.strptime(ed,"%d/%m/%Y")): return False
        return True
    rows=[r for r in rows if include(r)]
    confirmed=[r for r in rows if r.get("status")=="Confirmed" and not r.get("duplicate_of") and r.get("currency")]
    wb=Workbook(); summary=wb.active; summary.title="Executive Summary"
    summary.append(["InvoiceFlow Financial Summary"]); summary.append(["Generated",_now()]); summary.append(["Records in report",len(rows)]); summary.append(["Confirmed non-duplicate records",len(confirmed)])
    summary.append([]); summary.append(["Confirmed totals by currency"])
    sums={}; vats={}; suppliers={}; categories_sum={}; months={}
    for r in confirmed:
        amount=core.parse_money(r.get("amount")); tax=core.parse_money(r.get("vat_amount")); cur=r["currency"]
        if amount is not None:
            sums[cur]=sums.get(cur,0)+amount
            supplier=r.get("vendor") or "Unknown"; suppliers.setdefault((supplier,cur),0); suppliers[(supplier,cur)]+=amount
            category=r.get("category") or "Uncategorized"; categories_sum.setdefault((category,cur),0); categories_sum[(category,cur)]+=amount
            try: month=datetime.strptime(core.fmt_date(r.get("invoice_date")),"%d/%m/%Y").strftime("%Y-%m")
            except ValueError: month="Undated"
            months.setdefault((month,cur),0); months[(month,cur)]+=amount
        if tax is not None: vats[cur]=vats.get(cur,0)+tax
    for cur,total in sorted(sums.items()): summary.append([cur,round(total,2)])
    summary.append([]); summary.append(["VAT totals by currency"])
    for cur,total in sorted(vats.items()): summary.append([cur,round(total,2)])
    tracker=wb.create_sheet("General Tracker")
    selected=config.get("columns") or list(EXPORT_COLUMN_OPTIONS)
    selected=[key for key in selected if key in EXPORT_COLUMN_OPTIONS]
    headers=[EXPORT_COLUMN_OPTIONS[key][0] for key in selected]
    tracker.append(headers)
    tag_map={}
    for r in rows:
        tag_map[int(r["id"])]=", ".join(invoice_metadata(r["id"])["tags"])
        values={"invoice_date":r.get("invoice_date"),"due_date":r.get("due_date"),"vendor":r.get("vendor"),"invoice_number":r.get("invoice_number"),
            "amount":core.parse_money(r.get("amount")) if core.parse_money(r.get("amount")) is not None else r.get("amount"),"currency":r.get("currency"),"vat_amount":r.get("vat_amount"),
            "category":r.get("category"),"project":r.get("project"),"payment_status":r.get("payment_status"),
            "status":"Possible duplicate" if r.get("duplicate_of") else r.get("status"),"tags":tag_map[int(r["id"])],"source_name":r.get("source_name"),"review_notes":r.get("review_notes")}
        tracker.append([values[key] for key in selected])
    def style(sh,widths=None):
        sh.freeze_panes="A2"; sh.auto_filter.ref=sh.dimensions
        for c in sh[1]: c.font=Font(bold=True,color="FFFFFF"); c.fill=PatternFill("solid",fgColor="203955")
        for row in sh.iter_rows():
            for cell in row: cell.alignment=Alignment(vertical="top",wrap_text=True)
        for i,width in enumerate(widths or [18]*sh.max_column,1): sh.column_dimensions[get_column_letter(i)].width=width
    style(tracker,[EXPORT_COLUMN_OPTIONS[key][1] for key in selected])
    suppliers_ws=wb.create_sheet("Supplier Analysis"); suppliers_ws.append(["Supplier","Currency","Confirmed spend","Invoice count","Latest invoice"])
    supplier_counts={}
    for r in confirmed:
        key=(r.get("vendor") or "Unknown",r.get("currency") or "Unknown")
        item=supplier_counts.setdefault(key,{"count":0,"latest":""})
        item["count"]+=1
        latest=str(r.get("invoice_date") or "")
        if latest>item["latest"]: item["latest"]=latest
    for (vendor,currency),meta in sorted(supplier_counts.items()):
        total=suppliers.get((vendor,currency),0)
        suppliers_ws.append([vendor,currency,round(total,2),meta["count"],meta["latest"]])
    style(suppliers_ws,[34,14,20,16,18])
    cat_ws=wb.create_sheet("Category Analysis"); cat_ws.append(["Category","Currency","Confirmed spend"])
    for (cat,cur),value in sorted(categories_sum.items()): cat_ws.append([cat,cur,round(value,2)])
    style(cat_ws,[32,14,20])
    monthly=wb.create_sheet("Monthly Trends"); monthly.append(["Month","Currency","Confirmed spend"])
    for (period,cur),value in sorted(months.items()): monthly.append([period,cur,round(value,2)])
    style(monthly,[18,14,20])
    budgets_ws=wb.create_sheet("Budget vs Actual"); budgets_ws.append(["Period","Category","Currency","Budget","Actual","Variance","Utilization"])
    for b in budgets():
        actual=0.0
        for r in confirmed:
            try: period=datetime.strptime(core.fmt_date(r.get("invoice_date")),"%d/%m/%Y").strftime("%Y-%m")
            except ValueError: continue
            if period==b["period"] and r.get("currency")==b["currency"] and (not b.get("category") or r.get("category")==b.get("category")):
                actual+=core.parse_money(r.get("amount")) or 0
        budgets_ws.append([b["period"],b.get("category") or "All categories",b["currency"],b["amount"],round(actual,2),round(float(b["amount"])-actual,2),round(actual/float(b["amount"])*100,1) if b["amount"] else 0])
    style(budgets_ws,[16,28,14,18,18,18,16])
    review=wb.create_sheet("Needs Review"); review.append(["Record ID","Supplier","Invoice number","Date","Amount","Currency","Issue","Duplicate of"])
    for r in rows:
        if r.get("status")!="Confirmed" or r.get("review_notes") or r.get("duplicate_of"):
            review.append([r["id"],r.get("vendor"),r.get("invoice_number"),r.get("invoice_date"),r.get("amount"),r.get("currency"),r.get("review_notes"),r.get("duplicate_of")])
    style(review,[12,30,22,15,16,12,60,16])
    wb.save(path); return len(rows)


def schedule_job(name, job_type, config, interval_minutes=1440):
    if job_type not in {"backup","export_excel","export_csv"}: raise ValueError("Unsupported scheduled task type.")
    interval_minutes=max(15,min(int(interval_minutes),525600))
    next_run=(datetime.now()+timedelta(minutes=interval_minutes)).isoformat(timespec="seconds")
    with core.db_connect() as con:
        con.execute("INSERT INTO scheduled_jobs(name,job_type,config_json,interval_minutes,enabled,next_run,created_at) VALUES(?,?,?,?,1,?,?)",
                    (normalize_name(name) or job_type,job_type,json.dumps(config or {},ensure_ascii=False),interval_minutes,next_run,_now()))
    return True


def scheduled_jobs():
    with core.db_connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM scheduled_jobs ORDER BY next_run")]


def due_jobs():
    now=_now()
    with core.db_connect() as con:
        rows=[dict(r) for r in con.execute("SELECT * FROM scheduled_jobs WHERE enabled=1 AND next_run<=?",(now,))]
    return rows


def mark_job_run(job_id: int):
    with core.db_connect() as con:
        row=con.execute("SELECT interval_minutes FROM scheduled_jobs WHERE id=?",(int(job_id),)).fetchone()
        if row:
            nxt=(datetime.now()+timedelta(minutes=int(row[0]))).isoformat(timespec="seconds")
            con.execute("UPDATE scheduled_jobs SET last_run=?,next_run=? WHERE id=?",(_now(),nxt,int(job_id)))


def set_job_enabled(job_id: int, enabled: bool):
    with core.db_connect() as con: con.execute("UPDATE scheduled_jobs SET enabled=? WHERE id=?",(1 if enabled else 0,int(job_id)))

