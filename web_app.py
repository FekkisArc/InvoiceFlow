"""Invoice OCR web-style desktop shell (local Edge/WebView)."""
import os, sys, threading, shutil, hashlib, traceback, re, json, base64, urllib.request, urllib.error, subprocess, tempfile, time
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox
import webview

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

import main as core
state={"busy":False,"done":True,"percent":0,"message":"Ready","detail":"Local processing · OCR runs on your PC"}
lock=threading.Lock()

UPDATE_CONFIG_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "InvoiceOCR" / "update_config.json"
APP_VERSION = "4.5.0"

def _read_update_config():
    try:
        return json.loads(UPDATE_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"github_owner": "", "github_repo": ""}

def _version_tuple(value):
    nums = re.findall(r"\d+", str(value))
    return tuple(int(x) for x in nums[:4]) if nums else (0,)

def _latest_release():
    cfg = _read_update_config()
    owner, repo = cfg.get("github_owner", "").strip(), cfg.get("github_repo", "").strip()
    if not owner or not repo:
        raise RuntimeError("Updates are not configured yet. Set github_owner and github_repo in update_config.json to the project's public GitHub repository.")
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    req = urllib.request.Request(api_url, headers={"Accept":"application/vnd.github+json","User-Agent":"InvoiceFlow-Updater"})
    with urllib.request.urlopen(req, timeout=20) as response:
        release = json.loads(response.read().decode("utf-8"))
    if release.get("draft") or release.get("prerelease"):
        raise RuntimeError("The latest GitHub release is a draft or prerelease.")
    assets = release.get("assets", [])
    asset = next((a for a in assets if re.fullmatch(r"InvoiceFlow_Setup_[0-9][0-9A-Za-z._-]*\.exe", a.get("name",""))), None)
    if not asset:
        raise RuntimeError("The release does not contain an asset named InvoiceFlow_Setup_<version>.exe.")
    tag = release.get("tag_name", "").lstrip("v")
    return {"version":tag, "url":asset["browser_download_url"], "asset_name":asset["name"], "release_url":release.get("html_url","")}

class API:
    window=None
    def _dialog(self, kind, **kwargs):
        if self.window is None: return None
        return self.window.create_file_dialog(kind, **kwargs)

    def check_for_updates(self):
        rel = _latest_release()
        return {"current_version": APP_VERSION, "latest_version": rel["version"],
                "available": _version_tuple(rel["version"]) > _version_tuple(APP_VERSION),
                "download_url": rel["url"], "release_url": rel["release_url"]}

    def download_and_install_update(self, download_url, version):
        # Download the setup EXE to a temporary location, then launch it only after
        # the current app window closes. Inno Setup upgrades the installed files;
        # invoice DB and originals remain in Local AppData.
        if not str(download_url).startswith("https://"):
            raise RuntimeError("Refusing an update URL that is not HTTPS.")
        tempdir = Path(tempfile.mkdtemp(prefix="InvoiceFlowUpdate_"))
        installer = tempdir / f"InvoiceFlow_Setup_{version}.exe"
        req = urllib.request.Request(download_url, headers={"User-Agent":"InvoiceFlow-Updater"})
        with urllib.request.urlopen(req, timeout=60) as response, installer.open("wb") as out:
            shutil.copyfileobj(response, out)
        if installer.stat().st_size < 1_000_000:
            raise RuntimeError("Downloaded update installer is unexpectedly small; update cancelled.")
        def close_then_install():
            time.sleep(1.0)
            try:
                if self.window:
                    self.window.destroy()
            except Exception:
                pass
            time.sleep(1.0)
            # Inno Setup may request elevation; user sees standard Windows UAC.
            subprocess.Popen([str(installer), "/SILENT", "/NORESTART"],
                             cwd=str(tempdir), close_fds=True)
        threading.Thread(target=close_then_install, daemon=True).start()
        return {"message": f"Starting InvoiceFlow {version} update. The app will close; reopen it after setup completes."}

    def get_invoices(self):
        return core.records()

    def choose_files(self):
        paths=self._dialog(webview.OPEN_DIALOG,allow_multiple=True,
            file_types=("Invoice files (*.pdf;*.zip;*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.bmp)",
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
            if not tess:
                raise RuntimeError("Tesseract OCR was not found. Install Tesseract OCR, then restart InvoiceFlow. The app also needs the Romanian (ron) and English (eng) language data for best results.")
            # Check language data before processing a large batch so the user gets
            # one clear warning instead of discovering this from individual logs.
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
            files=core.collect_inputs(paths,batch/"zip_extracted")
            if not files: raise RuntimeError("No supported invoice PDFs or images were found.")
            # Group known filename split sets without combining incomplete sets.
            groups={}; standalone=[]
            for f in files:
                info=core.split_part_info(f.name)
                if info:
                    base,part,total=info; groups.setdefault((str(f.parent).casefold(),base,total),[]).append((part,f))
                else: standalone.append([f])
            items=list(standalone)
            for key,parts in groups.items():
                by_part={}
                for part,f in parts: by_part.setdefault(part,f)
                total=key[2]
                if len(by_part)==total and all(i in by_part for i in range(1,total+1)):
                    items.append([by_part[i] for i in range(1,total+1)])
                else: items.extend([[f] for _,f in sorted(parts)])
            totaln=len(items)
            for idx,parts in enumerate(items,1):
                self._set(percent=int((idx-1)/max(totaln,1)*100),message=f"Invoice {idx}/{totaln}",detail="Preparing "+", ".join(f.name for f in parts)[:100])
                saved=[]
                raw_parts=[]; confs=[]
                try:
                    for f in parts:
                        dest=f if core.ORIGINALS in f.parents else core.preserved_copy(f,batch)
                        saved.append(dest)
                        def page_progress(page,_none,idx=idx,totaln=totaln,name=dest.name):
                            self._set(message=f"Invoice {idx}/{totaln} · page {page}",detail=name)
                        txt,conf=core.run_ocr(dest,tess,progress=page_progress)
                        raw_parts.append(txt);confs.append(conf)
                    raw="\n\n===== MERGED SCAN PART =====\n\n".join(raw_parts)
                    conf=sum(confs)/len(confs) if confs else 0
                    source=" + ".join(x.name for x in saved)
                    fields=core.extract_fields(raw,source)
                    notes=fields.notes
                    if conf<.48: notes=(notes+" " if notes else "")+"Low OCR text confidence. Verify against original."
                    elif conf<.68: notes=(notes+" " if notes else "")+"Moderate OCR confidence. Verify extracted fields."
                    if len(saved)>1: notes=(notes+" " if notes else "")+f"Auto-grouped {len(saved)} filename-matched scan parts. Verify completeness."
                    split=core.split_part_info(saved[0].name) if len(saved)==1 else None
                    if split: notes=(notes+" " if notes else "")+f"Incomplete split set: part {split[1]} of {split[2]}."
                    digest=hashlib.sha256("|".join(core.sha256_file(x) for x in saved).encode()).hexdigest()
                    groupkey=f"{saved[0].stem}__merged_{len(saved)}" if len(saved)>1 else ""
                    rid,dup=core.insert_invoice({"vendor":fields.vendor,"invoice_number":fields.invoice_number,
                        "invoice_date":fields.invoice_date,"amount":fields.amount,"currency":fields.currency or "",
                        "status":"Needs review","review_notes":notes,"source_name":source,
                        "source_path":"\n".join(map(str,saved)),"file_hash":digest,"raw_text":raw,
                        "ocr_confidence":conf,"split_group":groupkey,
                        "field_confidence":fields.field_confidence or {}})
                    core.audit(rid,"imported",f"sources={source}; merged_parts={len(saved)}; duplicate={dup}")
                    added+=1;dups+=int(bool(dup))
                except Exception as e:
                    failed+=1;core.log_error(f"Web UI OCR failed for {parts}: {type(e).__name__}: {e}")
                    for f in parts:
                        try:
                            dest=f if core.ORIGINALS in f.parents else core.preserved_copy(f,batch)
                            core.insert_invoice({"source_name":dest.name,"source_path":str(dest),"file_hash":core.sha256_file(dest),
                                "status":"Needs review","review_notes":f"OCR failed: {type(e).__name__}: {e}"})
                        except Exception as e2: core.log_error(f"Could not save failed record: {e2}")
                self._set(percent=int(idx/max(totaln,1)*100),message=f"Processed {idx}/{totaln}",detail=f"{added} imported · {failed} errors")
            self._set(busy=False,done=True,percent=100,message=f"Import finished · {added} imported · {dups} duplicates · {failed} errors",
                      detail="Verify OCR fields against originals before confirming.")
        except Exception as e:
            core.log_error(f"Web UI import failure: {traceback.format_exc()}")
            self._set(busy=False,done=True,message="Import failed: "+str(e),detail="See app.log for diagnostic details.")

    def update_invoice(self,data):
        rid=int(data.get("id"))
        fields={k:str(data.get(k,"")).strip() for k in ["vendor","invoice_number","invoice_date","amount","currency","status","review_notes"]}
        if fields["invoice_date"] and not core.fmt_date(fields["invoice_date"]):
            raise ValueError("Date must be DD/MM/YYYY.")
        if fields["amount"] and core.parse_money(fields["amount"]) is None:
            raise ValueError("Enter a valid amount.")
        if fields["status"]=="Confirmed" and any(not fields[k] for k in ["vendor","invoice_date","amount","currency"]):
            raise ValueError("To confirm, fill vendor, date, amount, and currency.")
        if fields["status"]=="Confirmed":
            verified=data.get("verified_fields",{})
            required=["vendor","invoice_number","invoice_date","amount","currency"]
            missing=[key for key in required if not bool(verified.get(key))]
            if missing:
                raise ValueError("Before confirming, check the 'Verified against original' box for each field: "+", ".join(missing))
        core.update_invoice(rid,[fields[k] for k in ["vendor","invoice_number","invoice_date","amount","currency","status","review_notes"]],
                            data.get("verified_fields",{}))
        aliases=str(data.get("vendor_aliases","") or "")
        for alias in [x.strip() for x in re.split(r"[,;\n]",aliases) if x.strip()]:
            core.save_vendor_alias(fields["vendor"],alias,"abbreviation/logo text")
        core.audit(rid,"edited","Web workspace inline review")
        return True

    def get_vendor_aliases(self, canonical_name):
        with core.db_connect() as con:
            rows=con.execute("SELECT alias FROM vendor_aliases WHERE canonical_name=? COLLATE NOCASE ORDER BY alias",
                             (str(canonical_name or "").strip(),)).fetchall()
        return [r["alias"] for r in rows]


    def delete_invoices(self,ids):
        for rid in ids:
            core.delete_invoice(int(rid))
        return True

    def get_original_preview(self, invoice_id):
        """Return preview thumbnails for every source file and a few pages per PDF."""
        row=next((r for r in core.records() if int(r["id"])==int(invoice_id)),None)
        if not row:
            return {"sources": [], "error": "Invoice record not found."}
        sources=[]
        for raw_path in str(row.get("source_path","")).splitlines():
            raw_path=raw_path.strip()
            if not raw_path:
                continue
            path=Path(raw_path)
            source={"name":path.name,"path":str(path),"exists":path.exists(),"pages":[],"error":""}
            if not path.exists():
                source["error"]="Original file is missing"
                sources.append(source)
                continue
            try:
                from PIL import Image
                import io
                if path.suffix.lower()==".pdf":
                    import fitz
                    with fitz.open(path) as doc:
                        source["page_count"]=len(doc)
                        for page_no in range(min(len(doc),3)):
                            page=doc[page_no]
                            pix=page.get_pixmap(matrix=fitz.Matrix(1.25,1.25),alpha=False)
                            im=Image.frombytes("RGB",(pix.width,pix.height),pix.samples)
                            im.thumbnail((1000,1400),Image.Resampling.LANCZOS)
                            buf=io.BytesIO(); im.save(buf,format="JPEG",quality=78,optimize=True)
                            source["pages"].append({"page":page_no+1,"data":"data:image/jpeg;base64,"+base64.b64encode(buf.getvalue()).decode("ascii")})
                else:
                    with Image.open(path) as im:
                        source["page_count"]=getattr(im,"n_frames",1)
                        im.seek(0); im=im.convert("RGB")
                        im.thumbnail((1000,1400),Image.Resampling.LANCZOS)
                        buf=io.BytesIO(); im.save(buf,format="JPEG",quality=78,optimize=True)
                        source["pages"].append({"page":1,"data":"data:image/jpeg;base64,"+base64.b64encode(buf.getvalue()).decode("ascii")})
                if source.get("page_count",1)>len(source["pages"]):
                    source["more_pages"]=source["page_count"]-len(source["pages"])
            except Exception as e:
                source["error"]=f"Preview unavailable: {type(e).__name__}: {e}"
            sources.append(source)
        return {"sources":sources,"raw_text":str(row.get("raw_text","") or "")}

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
        folder=self._dialog(webview.FOLDER_DIALOG)
        if isinstance(folder,(list,tuple)): folder=folder[0] if folder else None
        if not folder:return ""
        target=Path(folder)/f"InvoiceOCR_Backup_{datetime.now():%Y%m%d_%H%M%S}"
        shutil.copytree(core.BASE,target)
        return "Backup created: "+str(target)

def main():
    core.init_db()
    api=API()
    window=webview.create_window("InvoiceFlow · Invoice OCR",url=str(RESOURCE_ROOT/"webui.html"),
        js_api=api,width=1500,height=950,min_size=(1024,680),background_color="#f5f7fb")
    api.window=window
    webview.start(debug=False)

if __name__=="__main__":
    main()
