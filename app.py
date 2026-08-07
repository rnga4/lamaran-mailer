import asyncio
import csv
import io
import json
import os
import random
import smtplib
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import database as db
from email_service import (
    build_email,
    get_templates,
    is_valid_email,
    render_body,
    send_email,
    set_template,
)

# ────────────────────────── Config ──────────────────────────

CV_DIR = Path(os.environ.get("CV_DIR", "/app/cv"))
RATE_LIMIT_PER_HOUR: int = int(os.environ.get("RATE_LIMIT_PER_HOUR", "999999"))
GMAIL_DAILY_LIMIT: int = int(os.environ.get("GMAIL_DAILY_LIMIT", "100"))
SEND_DELAY_MIN: int = int(os.environ.get("SEND_DELAY_MIN", "30"))
SEND_DELAY_MAX: int = int(os.environ.get("SEND_DELAY_MAX", "90"))
SPREAD_HOURS: int = int(os.environ.get("SPREAD_HOURS", "6"))
MAX_UPLOAD_SIZE: int = 10 * 1024 * 1024  # 10MB

CSV_CACHE_TTL = 3600

_batch_csv_cache: dict[str, dict[str, Any]] = {}
_batch_job_data: dict[int, dict[str, Any]] = {}
_batch_wake = threading.Event()
_smtp_index: int = 0
_smtp_lock = threading.Lock()


@dataclass
class SmtpAccount:
    key: str
    host: str
    port: int
    user: str
    password: str
    from_addr: str
    from_name: str


def _parse_smtp_accounts() -> list[SmtpAccount]:
    accounts: list[SmtpAccount] = []

    def add(prefix: str) -> None:
        host = os.environ.get(f"{prefix}HOST")
        user = os.environ.get(f"{prefix}USER")
        password = os.environ.get(f"{prefix}PASSWORD")
        if not all([host, user, password]):
            return
        port_str = os.environ.get(f"{prefix}PORT", "465")
        try:
            port = int(port_str)
        except ValueError:
            port = 465
        from_addr = os.environ.get(f"{prefix}FROM", user)
        from_name = os.environ.get(f"{prefix}FROM_NAME", "Nama Anda")
        key = f"{user}@{host}"
        accounts.append(SmtpAccount(key, host, port, user, password, from_addr, from_name))

    add("SMTP_")  # backward compat: SMTP_HOST, SMTP_USER, etc.
    for i in range(1, 10):
        add(f"SMTP{i}_")

    return accounts


SMTP_ACCOUNTS: list[SmtpAccount] = _parse_smtp_accounts()
SMTP_OK: bool = len(SMTP_ACCOUNTS) > 0


def _next_smtp_account() -> SmtpAccount:
    global _smtp_index
    if not SMTP_ACCOUNTS:
        raise RuntimeError("No SMTP accounts configured")
    with _smtp_lock:
        acct = SMTP_ACCOUNTS[_smtp_index % len(SMTP_ACCOUNTS)]
        _smtp_index = (_smtp_index + 1) % len(SMTP_ACCOUNTS)
    return acct


def _try_send_with_failover(
    to: str,
    company: str,
    position: str,
    extra: str,
    cv_path: str,
    template_name: str = "html",
) -> tuple[bool, str, str]:
    used_keys: set[str] = set()
    while len(used_keys) < len(SMTP_ACCOUNTS):
        acct = _next_smtp_account()
        if acct.key in used_keys:
            continue
        used_keys.add(acct.key)

        ok, _ = db.peek_rate_limit(acct.key, RATE_LIMIT_PER_HOUR)
        if not ok:
            continue

        try:
            msg, _, _ = build_email(
                to_addr=to,
                company=company,
                position=position,
                extra=extra,
                from_addr=acct.from_addr,
                from_name=acct.from_name,
                cv_path=cv_path,
                template_name=template_name,
            )
            send_email(msg, acct.host, acct.port, acct.user, acct.password)
            db.use_rate_limit(acct.key, RATE_LIMIT_PER_HOUR)
            return True, acct.key, ""
        except smtplib.SMTPException as e:
            db.log_email(to, company, position, extra, Path(cv_path).name, "failed", str(e), acct.key)
            continue
        except (ConnectionError, TimeoutError) as e:
            db.log_email(to, company, position, extra, Path(cv_path).name, "failed", str(e), acct.key)
            continue
        except FileNotFoundError:
            raise
        except Exception as e:
            db.log_email(to, company, position, extra, Path(cv_path).name, "failed", str(e), acct.key)
            continue

    # All accounts exhausted
    all_rate_limited = True
    min_remaining = RATE_LIMIT_PER_HOUR
    for acct in SMTP_ACCOUNTS:
        info = db.get_rate_limit_info(acct.key, RATE_LIMIT_PER_HOUR)
        if info["remaining"] <= 0 and info["resets_in"] < min_remaining:
            min_remaining = info["resets_in"]
        if info["remaining"] > 0:
            all_rate_limited = False
    if all_rate_limited:
        return False, "", f"Semua akun kena rate limit, tunggu ~{min_remaining} detik"
    return False, "", "Semua akun gagal mengirim (cek koneksi SMTP)"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.fail_stale_running_jobs()
    worker = threading.Thread(target=_batch_worker, daemon=True)
    worker.start()
    yield


app = FastAPI(title="Lamaran Mailer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(CV_DIR)), name="static")
templates = Jinja2Templates(directory="templates")


# ────────────────────────── Helpers ──────────────────────────


def _sanitize_filename(name: str) -> str:
    clean = Path(name).name
    if not clean or ".." in clean:
        raise ValueError("Invalid filename")
    return clean


def _get_cv_files() -> list[str]:
    if not CV_DIR.exists():
        return []
    return sorted(f.name for f in CV_DIR.iterdir() if f.suffix.lower() == ".pdf")


def _get_cv_files_with_size() -> list[dict[str, str]]:
    if not CV_DIR.exists():
        return []
    files: list[dict[str, str]] = []
    for f in sorted(CV_DIR.iterdir()):
        if f.suffix.lower() == ".pdf":
            size = f.stat().st_size
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024 * 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size / (1024 * 1024):.1f} MB"
            files.append({"name": f.name, "size": size_str})
    return files


def _ctx(request: Request, **kw: Any) -> dict[str, Any]:
    all_limits = db.get_all_rate_limits(RATE_LIMIT_PER_HOUR)
    # Ensure all configured accounts appear even if not yet used
    for acct in SMTP_ACCOUNTS:
        if acct.key not in all_limits:
            all_limits[acct.key] = {"used": 0, "remaining": RATE_LIMIT_PER_HOUR, "resets_in": 3600, "max": RATE_LIMIT_PER_HOUR}
    base = {
        "request": request,
        "smtp_ok": SMTP_OK,
        "rate_limit": all_limits,
        "smtp_accounts": [{"key": a.key, "name": a.from_addr} for a in SMTP_ACCOUNTS],
        "daily_sent": db.get_daily_sent_count(),
        "gmail_daily_limit": GMAIL_DAILY_LIMIT,
        "gmail_per_second": 5,
    }
    base.update(kw)
    return base


def _trim(val: str | None) -> str:
    return val.strip() if val else ""


def _clean_csv_cache() -> None:
    now = time.time()
    expired = [k for k, v in _batch_csv_cache.items() if now - v["created_at"] > CSV_CACHE_TTL]
    for k in expired:
        _batch_csv_cache.pop(k, None)


def _decode_csv_bytes(content: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _detect_delimiter(text: str) -> str:
    first_line = text.split("\n", 1)[0]
    candidates = {d: first_line.count(d) for d in (";", ",", "\t", "|")}
    best = max(candidates, key=candidates.get)
    return best if candidates[best] > 0 else ","


def _row_field(row: dict[str, Any], header_map: dict[str, str], name: str) -> str:
    actual = header_map.get(name)
    if actual:
        return row.get(actual) or ""
    return row.get(name) or ""


# ────────────────────────── Health ──────────────────────────


@app.get("/_health")
async def health():
    return {"status": "ok", "smtp_ok": SMTP_OK}


# ────────────────────────── WEB UI ──────────────────────────


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    message: str = "",
    msg_type: str = "",
):
    return templates.TemplateResponse(
        request, "index.html",
        _ctx(request,
            cv_files=_get_cv_files(),
            default_position="IT Support / DevOps",
            message=message,
            msg_type=msg_type,
            companies=db.get_unique_companies(),
        ),
    )


@app.post("/preview", response_class=HTMLResponse)
def preview(
    request: Request,
    to: str = Form(...),
    company: str = Form(...),
    position: str = Form("IT Support / DevOps"),
    extra: str = Form(""),
    cv_file: str = Form(...),
):
    to = _trim(to)
    company = _trim(company)
    position = _trim(position) or "IT Support / DevOps"
    extra = _trim(extra)
    cv_file = _trim(cv_file)
    try:
        body = render_body(company, position, extra, "plain")
    except (ValueError, KeyError) as e:
        body = f"Error: {e}"
    try:
        html_body = render_body(company, position, extra, "html")
    except (ValueError, KeyError) as e:
        html_body = f"Error: {e}"
    return templates.TemplateResponse(
        request, "index.html",
        _ctx(request,
            cv_files=_get_cv_files(),
            default_position=position,
            preview_mode=True,
            companies=db.get_unique_companies(),
            preview={
                "to": to,
                "company": company,
                "position": position,
                "extra": extra,
                "cv_file": cv_file,
                "body": body,
                "html_body": html_body,
            },
        ),
    )


@app.post("/send")
def send_single(
    to: str = Form(...),
    company: str = Form(...),
    position: str = Form("IT Support / DevOps"),
    extra: str = Form(""),
    cv_file: str = Form(...),
    template_name: str = Form("html"),
):
    to = _trim(to)
    company = _trim(company)
    position = _trim(position) or "IT Support / DevOps"
    extra = _trim(extra)
    cv_file = _trim(cv_file)

    if db.check_duplicate_email(to, company):
        return RedirectResponse(
            url=f"/?message=Email+kepada+{quote(to)}+untuk+{quote(company)}+sudah+pernah+dikirim&msg_type=error",
            status_code=303,
        )

    if not is_valid_email(to):
        return RedirectResponse(
            url="/?message=Format+email+tidak+valid&msg_type=error",
            status_code=303,
        )

    try:
        safe_cv = _sanitize_filename(cv_file)
    except ValueError:
        return RedirectResponse(
            url="/?message=Nama+file+CV+tidak+valid&msg_type=error",
            status_code=303,
        )
    cv_path = str(CV_DIR / safe_cv)

    if not SMTP_OK:
        db.log_email(to, company, position, extra, safe_cv, "failed", "SMTP not configured")
        return RedirectResponse(
            url="/?message=SMTP+belum+diatur+di+.env&msg_type=error",
            status_code=303,
        )

    try:
        success, key, err = _try_send_with_failover(to, company, position, extra, cv_path, template_name=template_name)
        if success:
            db.log_email(to, company, position, extra, safe_cv, "sent", smtp_account=key)
            return RedirectResponse(
                url=f"/?message=Email+berhasil+dikirim+ke+{to}&msg_type=success",
                status_code=303,
            )
        else:
            return RedirectResponse(
                url=f"/?message={quote(err)}&msg_type=error",
                status_code=303,
            )
    except FileNotFoundError as e:
        db.log_email(to, company, position, extra, safe_cv, "failed", str(e), smtp_account="system")
        return RedirectResponse(
            url=f"/?message={quote(str(e))}&msg_type=error",
            status_code=303,
        )
    except Exception as e:
        db.log_email(to, company, position, extra, safe_cv, "failed", str(e))
        return RedirectResponse(
            url=f"/?message=Error+tidak+terduga:+{quote(str(e))}&msg_type=error",
            status_code=303,
        )


# ────────────────────────── API: COMPANY SUGGEST ──────────────────────────


@app.get("/api/companies")
def api_companies(q: str = ""):
    companies = db.get_unique_companies()
    if q:
        companies = [c for c in companies if q.lower() in c.lower()]
    return companies


# ────────────────────────── BATCH CSV ──────────────────────────


@app.get("/batch", response_class=HTMLResponse)
def batch_page(
    request: Request,
    message: str = "",
    msg_type: str = "",
    job_id: int = 0,
):
    job = None
    active_jobs: list[dict[str, Any]] = []
    if job_id:
        job = db.get_batch_job(job_id)
    else:
        active_jobs = db.get_active_batch_jobs()
        if active_jobs:
            job_id = active_jobs[0]["id"]
            job = active_jobs[0]
    queued_jobs = db.get_queued_batch_jobs()
    batch_history = db.get_batch_jobs(limit=10)
    return templates.TemplateResponse(
        request, "batch.html",
        _ctx(request,
            cv_files=_get_cv_files(),
            message=message,
            msg_type=msg_type,
            job=job,
            job_id=job_id,
            active_jobs=active_jobs,
            queued_jobs=queued_jobs,
            batch_history=batch_history,
            now=time.time(),
        ),
    )


@app.get("/batch/preview", response_class=RedirectResponse)
async def batch_preview_redirect():
    return RedirectResponse(url="/batch", status_code=302)


@app.post("/batch/preview", response_class=HTMLResponse)
async def batch_preview(
    request: Request,
    file: UploadFile = File(...),
    cv_file: str = Form(...),
    position: str = Form("IT Support / DevOps"),
    extra: str = Form(""),
):
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        return RedirectResponse(
            url="/batch?message=File+terlalu+besar+(maks+10MB)&msg_type=error",
            status_code=303,
        )

    text = _decode_csv_bytes(content)
    reader = csv.DictReader(io.StringIO(text), delimiter=_detect_delimiter(text))
    header_map: dict[str, str] = {}
    if reader.fieldnames:
        header_map = {
            fn.strip().lower(): fn
            for fn in reader.fieldnames
            if fn and fn.strip()
        }

    rows: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    duplicate_rows: list[dict[str, Any]] = []
    prev_sent_rows: list[dict[str, Any]] = []
    seen_companies: set[str] = set()
    for i, row in enumerate(reader, 1):
        email_val = _row_field(row, header_map, "email").strip()
        company_val = (_row_field(row, header_map, "company") or _row_field(row, header_map, "nama_pt")).strip()
        if not email_val or not company_val:
            continue
        if not is_valid_email(email_val):
            invalid_rows.append({"email": email_val, "company": company_val, "row": i})
            continue
        company_lower = company_val.lower()
        if company_lower in seen_companies:
            duplicate_rows.append({"email": email_val, "company": company_val, "row": i})
            continue
        seen_companies.add(company_lower)
        if db.check_duplicate_email(email_val, company_val):
            prev_sent_rows.append({"email": email_val, "company": company_val, "row": i})
            continue
        rows.append({"email": email_val, "company": company_val, "row": i})

    if not rows:
        if prev_sent_rows:
            msg = "Semua+email+sudah+pernah+dikirim+ke+perusahaan+tersebut,+tidak+ada+baris+baru."
        elif invalid_rows:
            msg = "Tidak+ada+baris+email+valid.+Periksa+format+email+di+CSV."
        else:
            msg = "Tidak+ada+baris+data+ditemukan.+Pastikan+kolom+%27email%27+dan+%27company%27+(atau+%27nama_pt%27)+ada+di+baris+header."
        return RedirectResponse(
            url=f"/batch?message={msg}&msg_type=error",
            status_code=303,
        )

    # store rows server-side, pass cache_key instead of raw data
    cache_key = str(uuid.uuid4())
    _batch_csv_cache[cache_key] = {
        "rows": rows,
        "cv_file": cv_file,
        "position": position,
        "extra": extra,
        "filename": file.filename or "",
        "created_at": time.time(),
    }
    _clean_csv_cache()

    return templates.TemplateResponse(
        request, "batch.html",
        _ctx(request,
            cv_files=_get_cv_files(),
            preview_mode=True,
            rows=rows,
            invalid_rows=invalid_rows,
            duplicate_rows=duplicate_rows,
            prev_sent_rows=prev_sent_rows,
            cache_key=cache_key,
            filename=file.filename,
        ),
    )


@app.post("/batch/send")
def batch_send(
    cache_key: str = Form(...),
):
    data = _batch_csv_cache.pop(cache_key, None)
    if not data:
        return RedirectResponse(
            url="/batch?message=Data+CSV+tidak+ditemukan+(mungkin+kedaluwarsa),+upload+ulang&msg_type=error",
            status_code=303,
        )

    rows: list[dict[str, Any]] = data["rows"]
    cv_file: str = data["cv_file"]
    position: str = data["position"]
    extra: str = data["extra"]
    filename: str = data.get("filename", "")

    try:
        safe_cv = _sanitize_filename(cv_file)
    except ValueError:
        return RedirectResponse(
            url="/batch?message=Nama+file+CV+tidak+valid&msg_type=error",
            status_code=303,
        )
    cv_path = str(CV_DIR / safe_cv)

    if not SMTP_OK:
        return RedirectResponse(
            url="/batch?message=SMTP+belum+diatur+di+.env&msg_type=error",
            status_code=303,
        )

    total = len(rows)
    job_id = db.create_batch_job(total, filename=filename)
    _batch_job_data[job_id] = {
        "rows": rows,
        "position": position,
        "extra": extra,
        "safe_cv": safe_cv,
        "cv_path": cv_path,
    }
    _batch_wake.set()

    return RedirectResponse(
        url=f"/batch?job_id={job_id}",
        status_code=303,
    )


def _run_batch(job_id: int, data: dict[str, Any]) -> None:
    rows: list[dict[str, Any]] = data["rows"]
    position: str = data["position"]
    extra: str = data["extra"]
    safe_cv: str = data["safe_cv"]
    cv_path: str = data["cv_path"]
    total = len(rows)

    results: dict[str, int] = {"success": 0, "failed": 0, "rate_limited": 0}
    last_error = ""
    template_names = db.get_template_names()
    db.update_batch_job(job_id, status="running", last_error="")
    try:
        for row in rows:
            # Cek cancel
            current = db.get_batch_job(job_id)
            if current and current["status"] == "cancelled":
                last_error = "Dibatalkan pengguna"
                db.update_batch_job(job_id,
                    sent=results["success"],
                    failed=results["failed"],
                    rate_limited=results["rate_limited"],
                    last_error=last_error,
                )
                break

            email_val = row["email"]
            company_val = row["company"]

            tpl_name = random.choice(template_names) if template_names else "html"

            try:
                success, key, err = _try_send_with_failover(email_val, company_val, position, extra, cv_path, template_name=tpl_name)
            except FileNotFoundError as e:
                results["failed"] += 1
                last_error = str(e)
                db.update_batch_job(job_id,
                    sent=results["success"],
                    failed=results["failed"],
                    rate_limited=results["rate_limited"],
                    last_error=last_error,
                )
                continue

            if success:
                db.log_email(email_val, company_val, position, extra, safe_cv, "sent", smtp_account=key)
                results["success"] += 1
            else:
                if "rate limit" in err.lower():
                    results["rate_limited"] += 1
                else:
                    results["failed"] += 1
                last_error = err

            db.update_batch_job(job_id,
                sent=results["success"],
                failed=results["failed"],
                rate_limited=results["rate_limited"],
                last_error=last_error,
            )
            if SPREAD_HOURS > 0 and total > 1:
                spread_sec = SPREAD_HOURS * 3600
                avg_delay = spread_sec / total
                delay = avg_delay * random.uniform(0.7, 1.3)
            else:
                delay = random.uniform(SEND_DELAY_MIN, SEND_DELAY_MAX)
            time.sleep(delay)

        db.update_batch_job(job_id, status="done",
            sent=results["success"],
            failed=results["failed"],
            rate_limited=results["rate_limited"],
            last_error=last_error,
        )
    except Exception as e:
        last_error = f"Kesalahan sistem: {e}"
        db.update_batch_job(job_id, status="failed",
            sent=results["success"],
            failed=results["failed"],
            rate_limited=results["rate_limited"],
            last_error=last_error,
        )


def _batch_worker() -> None:
    while True:
        queued = db.get_queued_batch_jobs()
        if not queued:
            _batch_wake.wait(timeout=5)
            _batch_wake.clear()
            continue
        job_id = queued[0]["id"]
        data = _batch_job_data.pop(job_id, None)
        if data is None:
            for _ in range(20):
                time.sleep(0.05)
                data = _batch_job_data.pop(job_id, None)
                if data is not None:
                    break
        if data is None:
            db.update_batch_job(job_id, status="failed", last_error="Data batch tidak ditemukan")
            continue
        current = db.get_batch_job(job_id)
        if current and current["status"] == "cancelled":
            continue
        _run_batch(job_id, data)


@dataclass
class CancelBody:
    job_id: int


@app.post("/api/batch/cancel")
def batch_cancel(body: CancelBody):
    db.cancel_batch_job(body.job_id)
    return {"ok": True}


@app.post("/batch/cancel/{job_id}")
def batch_cancel_form(job_id: int):
    db.cancel_batch_job(job_id)
    return RedirectResponse(
        url=f"/batch?message=Batch+%23{job_id}+dibatalkan&msg_type=warning",
        status_code=303,
    )


# ────────────────────────── SSE Batch Progress ──────────────────────────


@app.get("/api/batch-progress/{job_id}/stream")
async def batch_progress_stream(job_id: int):
    async def event_generator():
        while True:
            job = await asyncio.to_thread(db.get_batch_job, job_id)
            if not job:
                yield f"event: error\ndata: {json.dumps({'error': 'not found'})}\n\n"
                break

            yield f"data: {json.dumps(job)}\n\n"

            if job["status"] in ("done", "cancelled", "failed"):
                yield f"event: {job['status']}\ndata: {json.dumps(job)}\n\n"
                break

            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/batch-progress/{job_id}")
def batch_progress(job_id: int):
    job = db.get_batch_job(job_id)
    if not job:
        return {"error": "not found"}
    return job


# ────────────────────────── HISTORY ──────────────────────────


@app.get("/history", response_class=HTMLResponse)
def history_page(
    request: Request,
    page: int = 1,
    search: str = "",
    status: str = "",
    account: str = "",
    date_from: str = "",
    date_to: str = "",
):
    per_page = 25
    offset = (page - 1) * per_page
    emails = db.get_emails(limit=per_page, offset=offset, search=search, status_filter=status, account_filter=account, date_from=date_from, date_to=date_to)
    total = db.get_email_count(search=search, status_filter=status, account_filter=account, date_from=date_from, date_to=date_to)
    total_pages = max(1, (total + per_page - 1) // per_page)
    smtp_accounts_list = db.get_distinct_smtp_accounts()

    return templates.TemplateResponse(
        request, "history.html",
        _ctx(request,
            emails=emails,
            page=page,
            total_pages=total_pages,
            total=total,
            search=search,
            status_filter=status,
            account_filter=account,
            date_from=date_from,
            date_to=date_to,
            smtp_accounts_list=smtp_accounts_list,
        ),
    )


@app.post("/history/delete/{email_id}")
def history_delete(email_id: int):
    db.delete_email(email_id)
    return RedirectResponse(url="/history?message=Email+dihapus&msg_type=success", status_code=303)


@app.post("/history/bulk-delete")
def history_bulk_delete(ids: str = Form(...)):
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    if id_list:
        db.delete_emails(id_list)
    return RedirectResponse(url="/history?message={}+entry+dihapus&msg_type=success".format(len(id_list)), status_code=303)


@app.post("/history/clear")
def history_clear():
    db.clear_all_emails()
    return RedirectResponse(url="/history?message=Semua+history+dihapus&msg_type=success", status_code=303)


@app.post("/history/retry/{email_id}")
def history_retry(email_id: int):
    email = db.get_email_by_id(email_id)
    if not email:
        return RedirectResponse(url="/history?message=Email+tidak+ditemukan&msg_type=error", status_code=303)

    return RedirectResponse(
        url=(
            f"/?retry=1"
            f"&to={quote(email['to_addr'])}"
            f"&company={quote(email['company'])}"
            f"&position={quote(email['position'])}"
            f"&extra={quote(email.get('extra') or '')}"
            f"&cv_file={quote(email['cv_file'])}"
        ),
        status_code=303,
    )


@app.get("/history/export")
def history_export(
    search: str = "",
    status: str = "",
    account: str = "",
    date_from: str = "",
    date_to: str = "",
):
    csv_data = db.export_emails_csv(search=search, status_filter=status, account_filter=account, date_from=date_from, date_to=date_to)
    return StreamingResponse(
        io.BytesIO(csv_data.encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=history_lamaran.csv"},
    )


# ────────────────────────── DASHBOARD ──────────────────────────


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    stats = db.get_stats()
    return templates.TemplateResponse(
        request, "dashboard.html",
        _ctx(request, stats=stats),
    )


# ────────────────────────── CV MANAGEMENT ──────────────────────────


@app.get("/cv", response_class=HTMLResponse)
def cv_page(request: Request, message: str = "", msg_type: str = ""):
    return templates.TemplateResponse(
        request, "cv_manage.html",
        _ctx(request,
            cv_files=_get_cv_files_with_size(),
            message=message,
            msg_type=msg_type,
        ),
    )


@app.post("/cv/upload")
async def cv_upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        return RedirectResponse(url="/cv?message=File+harus+PDF&msg_type=error", status_code=303)

    try:
        safe_name = _sanitize_filename(file.filename)
    except ValueError:
        return RedirectResponse(url="/cv?message=Nama+file+tidak+valid&msg_type=error", status_code=303)

    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        return RedirectResponse(url="/cv?message=File+terlalu+besar+(maks+10MB)&msg_type=error", status_code=303)

    dest = CV_DIR / safe_name
    with open(dest, "wb") as f:
        f.write(content)

    return RedirectResponse(url=f"/cv?message=CV+{quote(safe_name)}+berhasil+diupload&msg_type=success", status_code=303)


@app.post("/cv/delete")
def cv_delete(filename: str = Form(...)):
    try:
        safe_name = _sanitize_filename(filename)
    except ValueError:
        return RedirectResponse(url="/cv?message=Nama+file+tidak+valid&msg_type=error", status_code=303)
    path = CV_DIR / safe_name
    if path.exists() and path.parent == CV_DIR.resolve():
        path.unlink()
    return RedirectResponse(url="/cv?message=CV+dihapus&msg_type=success", status_code=303)


# ────────────────────────── TEMPLATE EDITOR ──────────────────────────


@app.get("/templates-editor", response_class=HTMLResponse)
def templates_editor_page(request: Request, message: str = "", msg_type: str = ""):
    tpl_list = db.get_all_templates()
    default_plain = get_templates()["plain"]
    default_html = get_templates()["html"]
    if not tpl_list:
        tpl_list = [{"name": "default", "body": default_plain, "html_body": default_html}]

    return templates.TemplateResponse(
        request, "templates_editor.html",
        _ctx(request,
            templates=tpl_list,
            message=message,
            msg_type=msg_type,
        ),
    )


@app.post("/templates-editor/save")
def save_template(
    name: str = Form(...),
    body: str = Form(...),
    html_body: str = Form(...),
):
    name = _trim(name)
    body = _trim(body)
    html_body = _trim(html_body)
    if not name or not body or not html_body:
        return RedirectResponse(
            url="/templates-editor?message=Semua+field+wajib+diisi&msg_type=error",
            status_code=303,
        )
    try:
        body.format(company="Test", position="Test", extra="")
        html_body.format(company="Test", position="Test", extra="")
    except (KeyError, ValueError, IndexError) as e:
        return RedirectResponse(
            url=f"/templates-editor?message=Error+di+template:+{quote(str(e))}&msg_type=error",
            status_code=303,
        )
    db.save_template(name, body, html_body)
    return RedirectResponse(
        url="/templates-editor?message=Template+berhasil+disimpan&msg_type=success",
        status_code=303,
    )


@app.post("/templates-editor/delete")
def delete_template(name: str = Form(...)):
    db.delete_template(name)
    return RedirectResponse(
        url="/templates-editor?message=Template+dihapus&msg_type=success",
        status_code=303,
    )


@app.post("/templates-editor/reset")
def reset_template():
    for t in db.get_all_templates():
        db.delete_template(t["name"])
    default_plain = get_templates()["plain"]
    default_html = get_templates()["html"]
    db.save_template("default", default_plain, default_html)
    return RedirectResponse(
        url="/templates-editor?message=Template+direset+ke+default&msg_type=success",
        status_code=303,
    )
