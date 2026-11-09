"""Invoice OCR web-style desktop shell (local Edge/WebView)."""
import os, sys, threading, shutil, hashlib, traceback, re, json, base64, urllib.request, urllib.error, subprocess, tempfile, time, zipfile
from pathlib import Path
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import filedialog, messagebox

# Import application modules before WebView so a missing/broken WebView runtime
# can still be logged and reported to the user in a windowed executable.
import main as core
import workspace_features as features
try:
    import webview
except Exception as _webview_import_error:
    _webview_details = traceback.format_exc()
    _webview_log = core.BASE / "app.log"
    try:
        core.log_error("FATAL WEBVIEW IMPORT ERROR\n" + _webview_details)
    except Exception:
        pass
    try:
        messagebox.showerror(
            "InvoiceFlow could not start",
            "The embedded desktop browser could not be loaded.\n\n"
            f"{type(_webview_import_error).__name__}: {_webview_import_error}\n\n"
            f"Diagnostic log: {_webview_log}"
        )
    except Exception:
        pass
    raise

# Support source runs and PyInstaller 6+ onedir packages.
# PyInstaller 6+ places bundled data files in sys._MEIPASS (usually _internal),
# while the executable itself remains one directory above.
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
BUNDLED_TESSDATA = ROOT / "tessdata"
if BUNDLED_TESSDATA.is_dir():
    # Keep language data next to the app so users do not need to configure PATH
    # or TESSDATA_PREFIX. Tesseract's executable is installed by the setup wizard.
    os.environ["TESSDATA_PREFIX"] = str(BUNDLED_TESSDATA)

state={"busy":False,"done":True,"percent":0,"message":"Ready","detail":"Local processing · OCR runs on your PC"}
lock=threading.Lock()

UPDATE_CONFIG_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InvoiceOCR" / "update_config.json"
APP_VERSION = "5.1.1"

def _read_update_config():
    """Prefer the user's config; fall back to the bundled public repo config."""
    candidates = [UPDATE_CONFIG_PATH, RESOURCE_ROOT / "update_config.json", ROOT / "update_config.json"]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("github_owner", "").strip() and data.get("github_repo", "").strip():
                return data
        except Exception:
            continue
    return {"github_owner": "FekkisArc", "github_repo": "InvoiceFlow"}

def _version_tuple(value):
    nums = re.findall(r"\d+", str(value))
    return tuple(int(x) for x in nums[:4]) if nums else (0,)

def _latest_release():
    cfg = _read_update_config()
    owner, repo = cfg.get("github_owner", "").strip(), cfg.get("github_repo", "").strip()
    if not owner or not repo:
        raise RuntimeError("Updates are not configured. Check update_config.json.")
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    req = urllib.request.Request(api_url, headers={"Accept":"application/vnd.github+json","User-Agent":"InvoiceFlow-Updater"})
    with urllib.request.urlopen(req, timeout=20) as response:
        release = json.loads(response.read().decode("utf-8"))
    if release.get("draft") or release.get("prerelease"):
        raise RuntimeError("The latest GitHub release is a draft or prerelease.")
    tag = str(release.get("tag_name", "")).lstrip("v")
    expected_name = f"InvoiceFlow_Setup_{tag}.exe"
    assets = release.get("assets", [])
    asset = next((a for a in assets if a.get("name") == expected_name), None)
    if not asset:
        raise RuntimeError(f"The release does not contain the expected installer {expected_name}.")
    url = str(asset.get("browser_download_url", ""))
    parsed = urllib.parse.urlparse(url)
    expected_path = f"/{owner}/{repo}/releases/download/"
    if parsed.scheme != "https" or parsed.hostname != "github.com" or not parsed.path.startswith(expected_path):
        raise RuntimeError("The release installer URL is not from the configured official GitHub repository.")
    return {"version":tag, "url":url, "asset_name":asset["name"],
            "release_url":release.get("html_url", ""), "digest":asset.get("digest", ""),
            "size":int(asset.get("size") or 0)}

def _report_fatal_startup_error(exc_type, exc, tb):
    """Persist startup failures and show a useful dialog in windowed builds."""
    details = "".join(traceback.format_exception(exc_type, exc, tb))
    log_path = core.BASE / "app.log"
    try:
        core.log_error("FATAL STARTUP ERROR\n" + details)
    except Exception:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("\nFATAL STARTUP ERROR\n" + details + "\n")
        except Exception:
            pass
    try:
        from tkinter import messagebox
        messagebox.showerror(
            "InvoiceFlow could not start",
            "InvoiceFlow encountered an error while starting.\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"The full diagnostic information was saved to:\n{log_path}\n\n"
            "Please send that log to support so the problem can be fixed."
        )
    except Exception:
        pass


class API:
    window=None
    def __init__(self):
        features.init_db()
        settings=features.get_settings()
        self.session_unlocked=not bool(settings.get("pin_hash"))
        self._automation_thread=None
        self._automation_stop=threading.Event()
    def start_automation_services(self):
        if self._automation_thread and self._automation_thread.is_alive(): return
        self._automation_stop.clear()
        def loop():
            while not self._automation_stop.wait(15):
                try:
                    if not state.get("busy"):
                        paths=features.scan_watched_folders()
                        if paths and not self.start_import(paths):
                            # Release pending marks so a later cycle can retry.
                            with core.db_connect() as con:
                                for raw in paths: con.execute("UPDATE watched_files SET status='retry' WHERE path=?",(raw,))
                    self._run_due_jobs()
                    settings=features.get_settings()
                    if str(settings.get("auto_backup_enabled","false")).lower()=="true" and not state.get("busy"):
                        hours=max(1,min(168,int(settings.get("backup_interval_hours","24") or 24)))
                        last=settings.get("auto_backup_last","")
                        due=True
                        if last:
                            try: due=datetime.fromisoformat(last) <= datetime.now()-timedelta(hours=hours)
                            except ValueError: due=True
                        if due:
                            root=core.BASE/"Backups"; root.mkdir(parents=True,exist_ok=True)
                            target=root/f"Automatic_{datetime.now():%Y%m%d_%H%M%S}"
                            core.create_backup(target); features.save_settings({"auto_backup_last":datetime.now().isoformat(timespec="seconds")})
                            retention=max(1,min(100,int(settings.get("backup_retention","10") or 10)))
                            backups=sorted([x for x in root.glob("Automatic_*") if x.is_dir()],key=lambda x:x.stat().st_mtime,reverse=True)
                            for stale in backups[retention:]: shutil.rmtree(stale,ignore_errors=True)
                except Exception as exc: core.log_error(f"Automation service: {type(exc).__name__}: {exc}")
        self._automation_thread=threading.Thread(target=loop,name="InvoiceFlowAutomation",daemon=True); self._automation_thread.start()

    def _run_due_jobs(self):
        for job in features.due_jobs():
            try:
                config=json.loads(job.get("config_json") or "{}")
                out=core.BASE/"Exports"; out.mkdir(parents=True,exist_ok=True)
                stem=re.sub(r"[^A-Za-z0-9_-]+","_",job["name"]).strip("_") or "Scheduled"
                if job["job_type"]=="backup": core.create_backup(core.BASE/"Backups"/f"{stem}_{datetime.now():%Y%m%d_%H%M%S}")
                elif job["job_type"]=="export_csv": features.export_csv(out/f"{stem}_{datetime.now():%Y%m%d_%H%M%S}.csv",**config)
                elif job["job_type"]=="export_excel": features.export_workspace_xlsx(out/f"{stem}_{datetime.now():%Y%m%d_%H%M%S}.xlsx",config)
                features.mark_job_run(job["id"])
            except Exception as exc: core.log_error(f"Scheduled job {job.get('id')} failed: {exc}")

    def _dialog(self, kind, **kwargs):
        if self.window is None: return None
        return self.window.create_file_dialog(kind, **kwargs)

    def check_for_updates(self):
        rel = _latest_release()
        return {"current_version": APP_VERSION, "latest_version": rel["version"],
                "available": _version_tuple(rel["version"]) > _version_tuple(APP_VERSION),
                "download_url": rel["url"], "release_url": rel["release_url"]}

    def download_and_install_update(self, download_url, version):
        """Download only the latest configured release and verify its SHA-256."""
        rel = _latest_release()
        if str(version).lstrip("v") != rel["version"] or str(download_url) != rel["url"]:
            raise RuntimeError("The update details changed or are not from the latest official release. Check for updates again.")
        tempdir = Path(tempfile.mkdtemp(prefix="InvoiceFlowUpdate_"))
        installer = tempdir / rel["asset_name"]
        req = urllib.request.Request(rel["url"], headers={"User-Agent":"InvoiceFlow-Updater"})
        with urllib.request.urlopen(req, timeout=90) as response, installer.open("wb") as out:
            shutil.copyfileobj(response, out)
        size = installer.stat().st_size
        if size < 1_000_000 or (rel["size"] and size != rel["size"]):
            shutil.rmtree(tempdir, ignore_errors=True)
            raise RuntimeError("Downloaded installer size does not match the published release. Update cancelled.")
        digest = rel.get("digest", "")
        if digest.startswith("sha256:"):
            hasher = hashlib.sha256()
            with installer.open("rb") as downloaded:
                for chunk in iter(lambda: downloaded.read(1024 * 1024), b""):
                    hasher.update(chunk)
            actual = hasher.hexdigest()
            if actual.casefold() != digest.split(":", 1)[1].casefold():
                shutil.rmtree(tempdir, ignore_errors=True)
                raise RuntimeError("Installer SHA-256 verification failed. Update cancelled.")
        else:
            shutil.rmtree(tempdir, ignore_errors=True)
            raise RuntimeError("The GitHub release does not publish a SHA-256 digest. Refusing an unverified update.")
        def close_then_install():
            time.sleep(1.0)
            try:
                if self.window:
                    self.window.destroy()
            except Exception:
                pass
            time.sleep(1.0)
            subprocess.Popen([str(installer), "/SILENT", "/NORESTART"],
                             cwd=str(tempdir), close_fds=True)
        threading.Thread(target=close_then_install, daemon=True).start()
        return {"message": f"Starting verified InvoiceFlow {version} update. The app will close; reopen it after setup completes."}

    def get_invoices(self):
        rows=core.records()
        for row in rows:
            try:
                meta=features.invoice_metadata(row["id"])
                row["tags"]=meta["tags"]
                row["category"]=row.get("category", "") or ""
                row["project"]=row.get("project", "") or ""
                row["workflow_status"]=row.get("workflow_status", "Needs review") or "Needs review"
            except Exception:
                row.setdefault("tags", [])
        return rows

    def choose_files(self):
        paths=self._dialog(webview.OPEN_DIALOG,allow_multiple=True,
            file_types=("Invoice files (*.pdf;*.zip;*.xml;*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.bmp)",
                        "All files (*.*)"))
        return list(paths or [])

    def choose_folder(self):
        p=self._dialog(webview.FOLDER_DIALOG)
        if isinstance(p,(list,tuple)): p=p[0] if p else None
        return [p] if p else []

    def start_import(self, paths):
        with lock:
            if state["busy"]: return False
            state.update(busy=True,done=False,percent=0,message="Collecting files…",detail="")
        threading.Thread(target=self._worker,args=(paths,),daemon=True).start()
        return True

    def get_progress(self):
        with lock: return dict(state)

    def _set(self,**kwargs):
        with lock: state.update(kwargs)

    def _worker(self,paths):
        added=dups=failed=0
        try:
            tess=core.find_tesseract()
            # Only raster/PDF members require OCR. XML-only imports, including
            # XML-only ZIP archives and folders, must work without Tesseract.
            needs_ocr = False
            for raw in paths:
                candidate=Path(raw)
                try:
                    if candidate.is_dir():
                        candidates = candidate.rglob("*")
                        if any(f.is_file() and f.suffix.lower() in (core.SUPPORTED-{".xml"}) for f in candidates):
                            needs_ocr=True; break
                    elif candidate.is_file() and candidate.suffix.lower() in (core.SUPPORTED-{".xml"}):
                        needs_ocr=True; break
                    elif candidate.is_file() and candidate.suffix.lower()==".zip":
                        with zipfile.ZipFile(candidate) as archive:
                            if any(Path(n).suffix.lower() in (core.SUPPORTED-{".xml"}) for n in archive.namelist() if not n.endswith("/")):
                                needs_ocr=True; break
                except (OSError, zipfile.BadZipFile, PermissionError):
                    # collect_inputs reports corrupt/unreadable archives later.
                    if candidate.suffix.lower() in (core.SUPPORTED-{".xml"}):
                        needs_ocr=True; break
            if needs_ocr and not tess:
                raise RuntimeError("Tesseract OCR was not found. Install it or import structured UBL XML invoices, which do not require OCR.")
            if needs_ocr and tess:
                try:
                    import pytesseract
                    pytesseract.pytesseract.tesseract_cmd=tess
                    installed=set(pytesseract.get_languages(config=""))
                    missing=[lang for lang in ("ron","eng") if lang not in installed]
                    if missing:
                        self._set(message="OCR language pack warning",detail="Missing: "+", ".join(missing)+". OCR may be less accurate; install language data and retry.")
                        core.log_error("Tesseract language data missing: "+", ".join(missing))
                except Exception as lang_error:
                    core.log_error(f"Could not verify Tesseract language data: {lang_error}")
            batch=core.ORIGINALS/datetime.now().strftime("Batch_%Y%m%d_%H%M%S_%f")
            batch.mkdir(parents=True,exist_ok=True)
            zip_sources={}
            files=core.collect_inputs(paths,batch/"zip_extracted",origin_map=zip_sources)
            if not files: raise RuntimeError("No supported invoice PDFs or images were found.")
            # Group only complete, unambiguous filename split sets. Repeated
            # part numbers remain separate review records instead of being lost.
            items=core.group_split_files(files)
            totaln=len(items)
            for idx,parts in enumerate(items,1):
                self._set(percent=int((idx-1)/max(totaln,1)*100),message=f"Invoice {idx}/{totaln}",detail="Preparing "+", ".join(f.name for f in parts)[:100])
                saved=[]
                input_paths=[]
                raw_parts=[]; confs=[]
                try:
                    xml_data=None
                    for f in parts:
                        is_managed = core.ORIGINALS.resolve() in f.resolve().parents
                        archive_source=zip_sources.get(str(f.absolute()))
                        if archive_source:
                            input_paths.append(archive_source)
                        elif not is_managed:
                            input_paths.append(str(f.absolute()))
                        dest=f if is_managed else core.preserved_copy(f,batch)
                        saved.append(dest)
                        if dest.suffix.lower()==".xml":
                            if len(parts)!=1: raise ValueError("UBL XML files cannot be auto-merged with other split documents; import them separately.")
                            xml_data=features.parse_ubl_invoice(dest)
                            raw_parts.append(xml_data["raw_text"]); confs.append(1.0)
                        else:
                            def page_progress(page,_none,idx=idx,totaln=totaln,name=dest.name):
                                self._set(message=f"Invoice {idx}/{totaln} · page {page}",detail=name)
                            txt,conf=core.run_ocr(dest,tess,progress=page_progress)
                            raw_parts.append(txt);confs.append(conf)
                    raw="\n\n===== MERGED SCAN PART =====\n\n".join(raw_parts)
                    conf=sum(confs)/len(confs) if confs else 0
                    source=" + ".join(x.name for x in saved)
                    fields=core.extract_fields(raw,source) if xml_data is None else None
                    if fields is not None:
                        fields=features.apply_supplier_ocr_rules(fields,raw,fields.vendor)
                    if xml_data is not None:
                        extracted={k:xml_data.get(k,"") for k in ("vendor","invoice_number","invoice_date","amount","currency")}
                        field_conf=xml_data.get("field_confidence",{})
                        notes=xml_data.get("review_notes", "Parsed structured UBL XML; verify before confirmation.")
                    else:
                        extracted={k:getattr(fields,k) for k in ("vendor","invoice_number","invoice_date","amount","currency")}
                        ocr_vat_amount=getattr(fields,"vat_amount","")
                        field_conf=fields.field_confidence or {}
                        notes=fields.notes
                    if conf<.48: notes=(notes+" " if notes else "")+"Low OCR text confidence. Verify against original."
                    elif conf<.68: notes=(notes+" " if notes else "")+"Moderate OCR confidence. Verify extracted fields."
                    if len(saved)>1: notes=(notes+" " if notes else "")+f"Auto-grouped {len(saved)} filename-matched scan parts. Verify completeness."
                    split=core.split_part_info(saved[0].name) if len(saved)==1 else None
                    if split: notes=(notes+" " if notes else "")+f"Incomplete split set: part {split[1]} of {split[2]}."
                    digest=hashlib.sha256("|".join(core.sha256_file(x) for x in saved).encode()).hexdigest()
                    groupkey=f"{saved[0].stem}__merged_{len(saved)}" if len(saved)>1 else ""
                    rid,dup=core.insert_invoice({"vendor":extracted["vendor"],"invoice_number":extracted["invoice_number"],
                        "invoice_date":extracted["invoice_date"],"amount":extracted["amount"],"currency":extracted["currency"] or "",
                        "status":"Needs review","review_notes":notes,"source_name":source,
                        "source_path":"\n".join(map(str,saved)),
                        "source_input_path":"\n".join(dict.fromkeys(input_paths)),
                        "file_hash":digest,"raw_text":raw,
                        "ocr_confidence":conf,"split_group":groupkey,
                        "field_confidence":field_conf})
                    features.upsert_supplier(extracted["vendor"])
                    features.auto_categorize(rid,extracted["vendor"],raw)
                    if xml_data is not None:
                        features.update_invoice_extra(rid,due_date=xml_data.get("due_date",""),vat_amount=xml_data.get("vat_amount",""),gross_amount=xml_data.get("gross_amount",""))
                    elif ocr_vat_amount:
                        features.update_invoice_extra(rid,vat_amount=ocr_vat_amount)
                    core.audit(rid,"imported",f"sources={source}; merged_parts={len(saved)}; duplicate={dup}; xml={bool(xml_data)}")
                    added+=1;dups+=int(bool(dup))
                except Exception as e:
                    failed+=1;core.log_error(f"Web UI OCR failed for {parts}: {type(e).__name__}: {e}")
                    for f in parts:
                        try:
                            dest=f if core.ORIGINALS in f.parents else core.preserved_copy(f,batch)
                            is_managed = core.ORIGINALS.resolve() in f.resolve().parents
                            archive_source=zip_sources.get(str(f.absolute()))
                            input_source=archive_source or ("" if is_managed else str(f.absolute()))
                            core.insert_invoice({"source_name":dest.name,"source_path":str(dest),
                                "source_input_path":input_source,"file_hash":core.sha256_file(dest),
                                "status":"Needs review","review_notes":f"OCR failed: {type(e).__name__}: {e}"})
                        except Exception as e2: core.log_error(f"Could not save failed record: {e2}")
                self._set(percent=int(idx/max(totaln,1)*100),message=f"Processed {idx}/{totaln}",detail=f"{added} imported · {failed} errors")
            features.mark_watched_processed(paths)
            self._set(busy=False,done=True,percent=100,message=f"Import finished · {added} imported · {dups} duplicates · {failed} errors",
                      detail="Verify OCR fields against originals before confirming.")
        except Exception as e:
            core.log_error(f"Web UI import failure: {traceback.format_exc()}")
            features.mark_watched_processed(paths)
            self._set(busy=False,done=True,message="Import failed: "+str(e),detail="See app.log for diagnostic details.")

    def update_invoice(self, data):
        rid = int(data.get("id"))
        fields = {k: str(data.get(k, "")).strip() for k in
                  ["vendor", "invoice_number", "invoice_date", "amount", "currency", "status", "review_notes"]}
        if fields["invoice_date"]:
            normalized_date = core.fmt_date(fields["invoice_date"])
            if not normalized_date:
                raise ValueError("Enter a valid date in DD/MM/YYYY format (or a supported ISO date).")
            fields["invoice_date"] = normalized_date
        if fields["amount"] and core.parse_money(fields["amount"]) is None:
            raise ValueError("Enter a valid amount.")
        if fields["currency"]:
            fields["currency"] = fields["currency"].upper()
            if not re.fullmatch(r"[A-Z]{3,5}", fields["currency"]):
                raise ValueError("Currency must be a three-to-five-letter code, such as RON or EUR.")
        result = core.update_invoice(rid, [fields[k] for k in
            ["vendor", "invoice_number", "invoice_date", "amount", "currency", "status", "review_notes"]],
            data.get("verified_fields", {}))
        aliases = str(data.get("vendor_aliases", "") or "")
        rejected = []
        for alias in [x.strip() for x in re.split(r"[,;\n]", aliases) if x.strip()]:
            if not core.save_vendor_alias(fields["vendor"], alias, "abbreviation/logo text"):
                rejected.append(alias)
        features.update_invoice_extra(rid, category=data.get("category",""), project=data.get("project",""),
            due_date=data.get("due_date",""), vat_amount=data.get("vat_amount",""),
            payment_status=data.get("payment_status","Unknown"), tags=data.get("tags",[]))
        if fields["vendor"]:
            features.upsert_supplier(fields["vendor"])
        core.audit(rid, "edited", "Web workspace inline review")
        return {"saved": True, "status": result["status"], "warning": result["warning"],
                "changed_fields": result["changed_fields"], "rejected_aliases": rejected}

    # --- Professional workspace APIs ---
    def get_dashboard_data(self): return features.dashboard_data()
    def get_analytics(self, start="", end="", status="all", currency="all", category="all"):
        return features.analytics_data(start,end,status,currency,category)
    def get_suppliers(self):
        rows=features.supplier_list()
        for row in rows:
            path=Path(row.get("logo_path") or "")
            try:
                resolved=path.resolve(strict=True)
                if resolved.is_file() and resolved.is_relative_to((core.BASE/"Suppliers").resolve()):
                    import mimetypes
                    mime=mimetypes.guess_type(resolved.name)[0] or "image/png"
                    row["logo_data_url"]="data:"+mime+";base64,"+base64.b64encode(resolved.read_bytes()).decode("ascii")
            except (OSError,ValueError): pass
        return rows
    def get_supplier_invoice_history(self, supplier_id, limit=100): return features.supplier_invoice_history(supplier_id,limit)
    def save_supplier(self, data): return features.update_supplier(data or {})
    def save_supplier_rules(self, supplier_id, rules): return features.save_supplier_ocr_rules(supplier_id,rules or {})
    def add_supplier_logo(self, supplier_id):
        paths=self._dialog(webview.OPEN_DIALOG,allow_multiple=False,file_types=("Image files (*.png;*.jpg;*.jpeg;*.webp)",))
        if isinstance(paths,(list,tuple)): paths=paths[0] if paths else None
        return features.save_supplier_logo(supplier_id,paths) if paths else None
    def set_invoice_workflow_status(self, invoice_id, status, note=""): return features.set_workflow_status(invoice_id,status,note)
    def get_categories(self): return features.categories()
    def get_tags(self): return features.list_tags()
    def create_tag(self, name, color="#8190a6"): return features.create_tag(name,color)
    def rename_tag(self, tag_id, name, color=None): return features.rename_tag(tag_id,name,color)
    def delete_tag(self, tag_id): return features.delete_tag(tag_id)
    def get_category_rules(self): return features.category_rules()
    def save_supplier_aliases(self, supplier_name, aliases):
        rejected=[]
        for alias in [x.strip() for x in re.split(r"[,;\n]",str(aliases or "")) if x.strip()]:
            if not core.save_vendor_alias(supplier_name,alias,"supplier profile"): rejected.append(alias)
        return {"rejected":rejected}
    def save_category(self, name, color="#8491a4", category_id=None): return features.save_category(name,color,category_id)
    def create_category_rule(self, keyword, category_id, supplier_id=None, priority=100): return features.add_category_rule(keyword,category_id,supplier_id,priority)
    def get_budgets(self): return features.budgets()
    def save_budget(self, period, currency, amount, category_id=None, notes=""): return features.save_budget(period,currency,amount,category_id,notes)
    def get_projects(self): return features.project_list()
    def save_project(self, name, description="", client="", project_id=None): return features.save_project(name,description,client,project_id)
    def bulk_update_invoices(self, ids, category=None, project=None, add_tag=None, payment_status=None):
        return features.bulk_update_invoices(ids,category,project,add_tag,payment_status)
    def get_saved_views(self): return features.saved_views()
    def save_saved_view(self, name, config): return features.save_saved_view(name,config or {})
    def delete_saved_view(self, view_id): return features.delete_saved_view(view_id)
    def open_attachment(self, path):
        target=Path(str(path or "")).expanduser().resolve()
        allowed=core.ORIGINALS.resolve()
        if not target.is_relative_to(allowed) or not target.is_file(): raise ValueError("Attachment is missing or outside InvoiceFlow's local document folder.")
        if os.name=="nt": os.startfile(str(target))
        elif sys.platform=="darwin": subprocess.Popen(["open",str(target)])
        else: subprocess.Popen(["xdg-open",str(target)])
        return {"opened":True,"name":target.name}
    def get_invoice_metadata(self, invoice_id): return features.invoice_metadata(invoice_id)
    def get_recognition_candidates(self, invoice_id): return features.recognition_candidates(invoice_id)
    def get_activity_log(self, limit=150): return features.activity_log(limit)
    def add_invoice_attachment(self, invoice_id, note=""):
        paths=self._dialog(webview.OPEN_DIALOG,allow_multiple=False,file_types=("Documents (*.pdf;*.xml;*.png;*.jpg;*.jpeg)","All files (*.*)"))
        if isinstance(paths,(list,tuple)): paths=paths[0] if paths else None
        return features.add_attachment(invoice_id,paths,note) if paths else None
    def get_recycle_bin(self): return features.recycle_bin_list()
    def recycle_invoices(self, ids, move_originals=True): return features.recycle_invoices(ids,move_originals)
    def restore_recycled(self, item_id): return features.restore_recycled(item_id)
    def permanently_delete_recycled(self, ids): return features.permanently_delete_recycled(ids)
    def get_watch_folders(self): return features.watched_folders()
    def add_watch_folder(self):
        path=self._dialog(webview.FOLDER_DIALOG)
        if isinstance(path,(list,tuple)): path=path[0] if path else None
        return features.add_watched_folder(path) if path else False
    def remove_watch_folder(self, folder_id): return features.remove_watched_folder(folder_id)
    def scan_watch_folders(self):
        paths=features.scan_watched_folders()
        if paths and not state.get("busy"):
            if self.start_import(paths): return {"queued":len(paths),"paths":paths}
        return {"queued":0,"paths":paths}
    def get_settings(self): return features.get_settings()
    def save_settings(self, values): return features.save_settings(values or {})
    def set_passcode(self, pin):
        result=features.set_passcode(pin); self.session_unlocked=True; return result
    def get_lock_status(self):
        return {"configured":bool(features.get_settings().get("pin_hash")),"locked":not self.session_unlocked}
    def unlock_app(self, pin):
        ok=features.verify_passcode(pin)
        if ok: self.session_unlocked=True
        return {"unlocked":ok}
    def lock_app(self):
        self.session_unlocked=False
        return True
    def remove_passcode(self, pin):
        if not features.verify_passcode(pin): raise ValueError("Current passcode is incorrect.")
        with core.db_connect() as con: con.execute("DELETE FROM app_settings WHERE key IN ('pin_hash','pin_salt')")
        self.session_unlocked=True
        return True
    def save_report_profile(self, name, config): return features.save_report_profile(name,config)
    def get_report_profiles(self): return features.report_profiles()
    def delete_report_profile(self, profile_id): return features.delete_report_profile(profile_id)
    def get_export_templates(self): return features.export_templates()
    def save_export_template(self, name, config): return features.save_export_template(name,config)
    def delete_export_template(self, template_id): return features.delete_export_template(template_id)
    def export_excel_template(self, template_id):
        template=next((x for x in features.export_templates() if int(x["id"])==int(template_id)),None)
        if not template: raise ValueError("Export template no longer exists.")
        name=re.sub(r"[^A-Za-z0-9_-]+","_",template["name"]).strip("_") or "InvoiceFlow"
        p=self._dialog(webview.SAVE_DIALOG,save_filename=f"{name}.xlsx",file_types=("Excel workbook (*.xlsx)",))
        if isinstance(p,(list,tuple)): p=p[0] if p else None
        if not p:return ""
        count=features.export_workspace_xlsx(Path(p),template["config"])
        return f"{template['name']} saved ({count} invoices): {p}"
    def create_scheduled_job(self, name, job_type, config, interval_minutes=1440): return features.schedule_job(name,job_type,config,interval_minutes)
    def get_scheduled_jobs(self): return features.scheduled_jobs()
    def set_scheduled_job(self, job_id, enabled): return features.set_job_enabled(job_id,enabled)
    def export_workspace_excel(self, config=None):
        p=self._dialog(webview.SAVE_DIALOG,save_filename="InvoiceFlow_Financial_Report.xlsx",file_types=("Excel workbook (*.xlsx)",))
        if isinstance(p,(list,tuple)): p=p[0] if p else None
        if not p:return ""
        count=features.export_workspace_xlsx(Path(p),config or {})
        return f"Financial workbook saved ({count} invoices): {p}"
    def export_csv(self):
        p=self._dialog(webview.SAVE_DIALOG,save_filename="InvoiceFlow_Report.csv",file_types=("CSV file (*.csv)",))
        if isinstance(p,(list,tuple)): p=p[0] if p else None
        if not p:return ""
        count=features.export_csv(Path(p))
        return f"CSV export saved ({count} records): {p}"
    def diagnostics(self):
        result={"version":APP_VERSION,"python":sys.version.split()[0],"database":str(core.DB),"db_integrity":"unknown","invoice_count":0,"tesseract":core.find_tesseract() or "Not found","webview":"available"}
        try:
            with core.db_connect() as con:
                result["db_integrity"]=con.execute("PRAGMA integrity_check").fetchone()[0]
                result["invoice_count"]=con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
                result["recycle_count"]=con.execute("SELECT COUNT(*) FROM recycle_bin").fetchone()[0]
                result["supplier_count"]=con.execute("SELECT COUNT(*) FROM suppliers WHERE active=1").fetchone()[0]
                result["watched_folder_count"]=con.execute("SELECT COUNT(*) FROM watched_folders WHERE enabled=1").fetchone()[0]
        except Exception as exc: result["database_error"]=str(exc)
        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd=result["tesseract"]
            result["tesseract_languages"]=pytesseract.get_languages(config="")
        except Exception: result["tesseract_languages"]=[]
        return result

    def resolve_duplicate(self, invoice_id):
        return core.resolve_duplicate(int(invoice_id))

    def get_vendor_aliases(self, canonical_name):
        with core.db_connect() as con:
            rows=con.execute("SELECT alias FROM vendor_aliases WHERE canonical_name=? COLLATE NOCASE ORDER BY alias",
                             (str(canonical_name or "").strip(),)).fetchall()
        return [r["alias"] for r in rows]


    def delete_invoices(self, ids, delete_originals=True):
        """Delete selected records and linked files, optionally their input originals."""
        return core.delete_invoices(ids, delete_originals=bool(delete_originals))

    def _preview_source_page(self, path, start_page=0, page_limit=3):
        """Render a bounded page window, returning 1-based page labels."""
        pages = []
        if path.suffix.lower() == ".pdf":
            import fitz
            from PIL import Image
            import io
            with fitz.open(path) as doc:
                total = len(doc)
                start_page = max(0, min(int(start_page), total))
                end_page = min(total, start_page + max(1, min(int(page_limit), 6)))
                for page_no in range(start_page, end_page):
                    page = doc[page_no]
                    preview_scale = min(1.25, 1800 / max(page.rect.width, page.rect.height))
                    pix = page.get_pixmap(matrix=fitz.Matrix(preview_scale, preview_scale), alpha=False)
                    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    im.thumbnail((1000, 1400), Image.Resampling.LANCZOS)
                    buf = io.BytesIO(); im.save(buf, format="JPEG", quality=78, optimize=True)
                    pages.append({"page":page_no+1, "data":"data:image/jpeg;base64,"+base64.b64encode(buf.getvalue()).decode("ascii")})
                return total, pages
        from PIL import Image
        import io
        with Image.open(path) as im:
            total = getattr(im, "n_frames", 1)
            start_page = max(0, min(int(start_page), total))
            end_page = min(total, start_page + max(1, min(int(page_limit), 6)))
            for page_no in range(start_page, end_page):
                im.seek(page_no)
                page = im.convert("RGB")
                page.thumbnail((1000, 1400), Image.Resampling.LANCZOS)
                buf = io.BytesIO(); page.save(buf, format="JPEG", quality=78, optimize=True)
                pages.append({"page":page_no+1, "data":"data:image/jpeg;base64,"+base64.b64encode(buf.getvalue()).decode("ascii")})
        return total, pages

    def get_original_preview(self, invoice_id):
        """Return first pages for every source; long PDFs can be loaded incrementally."""
        row = next((r for r in core.records() if int(r["id"]) == int(invoice_id)), None)
        if not row:
            return {"sources": [], "error": "Invoice record not found."}
        sources = []
        for source_index, raw_path in enumerate(str(row.get("source_path", "")).splitlines()):
            raw_path = raw_path.strip()
            if not raw_path:
                continue
            path = Path(raw_path)
            source = {"index":source_index, "name":path.name, "path":str(path),
                      "exists":path.exists(), "pages":[], "error":""}
            if not path.exists():
                source["error"] = "Original file is missing"
            else:
                try:
                    total, pages = self._preview_source_page(path, 0, 3)
                    source["page_count"] = total
                    source["pages"] = pages
                    source["next_page"] = len(pages) if len(pages) < total else None
                except Exception as exc:
                    source["error"] = f"Preview unavailable: {type(exc).__name__}: {exc}"
            sources.append(source)
        return {"sources":sources, "raw_text":str(row.get("raw_text", "") or "")}

    def get_original_preview_more(self, invoice_id, source_index, start_page):
        row = next((r for r in core.records() if int(r["id"]) == int(invoice_id)), None)
        if not row:
            raise ValueError("Invoice record not found.")
        source_paths = [x.strip() for x in str(row.get("source_path", "")).splitlines() if x.strip()]
        index = int(source_index)
        if index < 0 or index >= len(source_paths):
            raise ValueError("Original source index is invalid.")
        path = Path(source_paths[index])
        if not path.exists():
            raise FileNotFoundError(f"Original file is missing: {path.name}")
        total, pages = self._preview_source_page(path, int(start_page), 3)
        next_page = int(start_page) + len(pages)
        return {"pages":pages, "next_page":next_page if next_page < total else None, "page_count":total}

    def open_original(self, invoice_id):
        """Open every original source file belonging to a merged invoice."""
        row=next((r for r in core.records() if int(r["id"])==int(invoice_id)),None)
        if not row: return {"opened":0,"missing":0}
        opened=missing=0
        for raw_path in str(row.get("source_path","")).splitlines():
            path=Path(raw_path.strip())
            if path.exists():
                try:
                    os.startfile(str(path)); opened+=1
                except Exception as e:
                    core.log_error(f"Could not open original {path}: {e}")
            else:
                missing+=1
        return {"opened":opened,"missing":missing}

    def export_excel(self):
        p=self._dialog(webview.SAVE_DIALOG,save_filename="Invoice_Tracker.xlsx",
            file_types=("Excel workbook (*.xlsx)",))
        if isinstance(p,(list,tuple)): p=p[0] if p else None
        if not p:return ""
        core.export_xlsx(Path(p))
        return "Excel export saved: "+p

    def backup(self):
        if state.get("busy"):
            raise RuntimeError("Wait for the current import to finish before creating a backup.")
        folder=self._dialog(webview.FOLDER_DIALOG)
        if isinstance(folder,(list,tuple)): folder=folder[0] if folder else None
        if not folder:return ""
        target=Path(folder)/f"InvoiceFlow_Backup_{datetime.now():%Y%m%d_%H%M%S}"
        core.create_backup(target)
        return "Verified local backup created: "+str(target)

    def restore_backup(self):
        if state.get("busy"):
            raise RuntimeError("Wait for the current import to finish before restoring a backup.")
        folder=self._dialog(webview.FOLDER_DIALOG)
        if isinstance(folder,(list,tuple)): folder=folder[0] if folder else None
        if not folder:return ""
        safety=core.restore_backup(Path(folder))
        return "Backup restored. Your previous database was preserved at: "+str(safety)

def main():
    core.init_db()
    api=API()
    api.start_automation_services()
    window=webview.create_window("InvoiceFlow · Invoice OCR",url=str(RESOURCE_ROOT/"webui.html"),
        js_api=api,width=1500,height=950,min_size=(1024,680),background_color="#f5f7fb")
    api.window=window
    webview.start(debug=False)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        _report_fatal_startup_error(*sys.exc_info())
        raise SystemExit(1)
