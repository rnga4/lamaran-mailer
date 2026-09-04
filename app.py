import asyncio
import csv
import datetime
import hashlib
import hmac
import html as html_mod
import io
import json
import os
import random
import re
import secrets
import smtplib
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import database as db
from email_service import (
    EMAIL_DESIGNS,
    build_email,
    build_variants,
    get_templates,
    is_valid_email,
    render_body,
    send_email,
)

# ────────────────────────── Config ──────────────────────────

CV_DIR = Path(os.environ.get("CV_DIR", "/app/cv"))
RATE_LIMIT_PER_HOUR: int = int(os.environ.get("RATE_LIMIT_PER_HOUR", "999999"))
# Gmail free tier: 500 email/hari. Bisa di-override di .env (GMAIL_DAILY_LIMIT).
GMAIL_DAILY_LIMIT: int = int(os.environ.get("GMAIL_DAILY_LIMIT", "500"))
SEND_DELAY_MIN: int = int(os.environ.get("SEND_DELAY_MIN", "30"))
SEND_DELAY_MAX: int = int(os.environ.get("SEND_DELAY_MAX", "90"))
SPREAD_HOURS: int = int(os.environ.get("SPREAD_HOURS", "6"))
MAX_UPLOAD_SIZE: int = 10 * 1024 * 1024  # 10MB
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/app/data/backups"))
BACKUP_RETENTION_DAYS: int = int(os.environ.get("BACKUP_RETENTION_DAYS", "7"))

CSV_CACHE_TTL = 3600

_batch_csv_cache: dict[str, dict[str, Any]] = {}
_batch_wake = threading.Event()
_smtp_index: int = 0
_smtp_lock = threading.Lock()
_send_lock = threading.Lock()


@dataclass
class SmtpAccount:
    key: str
    host: str
    port: int
    user: str
    password: str
    from_addr: str
    from_name: str
    use_ssl: bool = True


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
        security = os.environ.get(f"{prefix}SECURITY", "").strip().lower()
        if security == "starttls":
            use_ssl = False
        elif security == "ssl":
            use_ssl = True
        else:
            # Default: SSL untuk port 465, STARTTLS untuk port lain (587, 25, ...)
            use_ssl = port == 465
        key = f"{user}@{host}"
        accounts.append(SmtpAccount(key, host, port, user, password, from_addr, from_name, use_ssl))

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
    experience: Optional[str] = None,
    sender_name: Optional[str] = None,
) -> tuple[bool, str, str]:
    if not SMTP_ACCOUNTS:
        raise RuntimeError("No SMTP accounts configured")

    used_keys: set[str] = set()
    last_error = ""
    # Kunci ini juga membuat cek rate limit + kirim + pakai kuota jadi atomik
    # antar-thread (batch worker vs kirim tunggal via web UI).
    with _send_lock:
        while len(used_keys) < len(SMTP_ACCOUNTS):
            acct = _next_smtp_account()
            if acct.key in used_keys:
                continue
            used_keys.add(acct.key)

            ok, _ = db.peek_rate_limit(acct.key, RATE_LIMIT_PER_HOUR)
            if not ok:
                continue

            # Batas aman harian per akun (GMAIL_DAILY_LIMIT) — ditegakkan
            if GMAIL_DAILY_LIMIT > 0 and db.get_daily_sent_count(acct.key) >= GMAIL_DAILY_LIMIT:
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
                    experience=experience,
                    sender_name=sender_name,
                )
                send_email(msg, acct.host, acct.port, acct.user, acct.password, use_ssl=acct.use_ssl)
                db.use_rate_limit(acct.key, RATE_LIMIT_PER_HOUR)
                return True, acct.key, ""
            except (smtplib.SMTPException, ConnectionError, TimeoutError) as e:
                # Akun ini gagal — coba akun berikutnya
                last_error = str(e)
                continue
            except (FileNotFoundError, ValueError, KeyError):
                # CV hilang / error template — masalah nyata, jangan ditelan failover
                raise
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                continue

    # Semua akun sudah dicoba
    if GMAIL_DAILY_LIMIT > 0:
        all_daily_capped = all(
            db.get_daily_sent_count(a.key) >= GMAIL_DAILY_LIMIT for a in SMTP_ACCOUNTS
        )
        if all_daily_capped:
            err = (
                f"Limit harian tercapai di semua akun ({GMAIL_DAILY_LIMIT}/hari). "
                "Lanjut besok atau naikkan GMAIL_DAILY_LIMIT di .env."
            )
            db.log_email(to, company, position, extra, Path(cv_path).name, "failed", err)
            return False, "", err

    all_rate_limited = True
    min_remaining = RATE_LIMIT_PER_HOUR
    for acct in SMTP_ACCOUNTS:
        info = db.get_rate_limit_info(acct.key, RATE_LIMIT_PER_HOUR)
        if info["remaining"] <= 0 and info["resets_in"] < min_remaining:
            min_remaining = info["resets_in"]
        if info["remaining"] > 0:
            all_rate_limited = False
    if all_rate_limited:
        err = f"Semua akun kena rate limit, tunggu ~{min_remaining} detik"
    else:
        err = last_error or "Semua akun gagal mengirim (cek koneksi SMTP)"
    # Catat SATU baris gagal per penerima (bukan satu baris per akun yang dicoba)
    db.log_email(to, company, position, extra, Path(cv_path).name, "failed", err)
    return False, "", err


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.fail_stale_running_jobs()
    worker = threading.Thread(target=_batch_worker, daemon=True)
    worker.start()
    backup = threading.Thread(target=_backup_worker, daemon=True)
    backup.start()
    yield


app = FastAPI(title="Lamaran Mailer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(CV_DIR)), name="static")
app.mount("/assets", StaticFiles(directory="static"), name="assets")
templates = Jinja2Templates(directory="templates")


# ────────────────────────── Auth (opsional) ──────────────────────────

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSION_COOKIE = "lm_session"
_SESSION_SECRET = os.environ.get("APP_SECRET") or secrets.token_hex(32)


def _session_token() -> str:
    data = f"lm|{int(time.time())}"
    sig = hmac.new(_SESSION_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{data}.{sig}"


def _valid_session(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    data, sig = cookie.rsplit(".", 1)
    expected = hmac.new(_SESSION_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Autentikasi hanya aktif kalau APP_PASSWORD diisi di .env
    if not APP_PASSWORD:
        return await call_next(request)
    path = request.url.path
    if path == "/login" or path == "/_health" or path.startswith("/assets"):
        return await call_next(request)
    if _valid_session(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)
    if request.method == "GET":
        return RedirectResponse(url=f"/login?next={quote(path)}", status_code=303)
    return Response(status_code=401)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, message: str = "", msg_type: str = "", next: str = ""):
    return templates.TemplateResponse(
        request, "login.html",
        _ctx(request, message=message, msg_type=msg_type, next=next),
    )


@app.post("/login")
def login(next: str = Form(""), password: str = Form(...)):
    if not APP_PASSWORD:
        return RedirectResponse(url="/", status_code=303)
    if hmac.compare_digest(password, APP_PASSWORD):
        target = next if (next.startswith("/") and not next.startswith("//")) else "/"
        resp = RedirectResponse(url=target, status_code=303)
        resp.set_cookie(
            SESSION_COOKIE, _session_token(),
            max_age=30 * 24 * 3600, httponly=True, samesite="lax",
        )
        return resp
    return RedirectResponse(url="/login?message=Password+salah&msg_type=error", status_code=303)


@app.post("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


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


def _email_designs() -> list[dict[str, str]]:
    """Desain bawaan (EMAIL_DESIGNS) + template kustom dari DB, untuk dropdown."""
    designs = [dict(d) for d in EMAIL_DESIGNS]
    for t in db.get_all_templates():
        if not any(d["id"] == t["name"] for d in designs):
            designs.append({"id": t["name"], "name": t["name"], "desc": "Template kustom (editor)"})
    return designs


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
        "auth_enabled": bool(APP_PASSWORD),
        "email_designs": _email_designs(),
        "work_hours": {
            "enabled": db.get_setting("work_hours_enabled", "0") == "1",
            "start": db.get_setting("work_start", "08:00") or "08:00",
            "end": db.get_setting("work_end", "17:00") or "17:00",
            "weekdays_only": db.get_setting("work_weekdays_only", "1") == "1",
        },
    }
    base.update(kw)
    return base


def _trim(val: str | None) -> str:
    return val.strip() if val else ""


def _next_midnight() -> float:
    """Detik epoch pergantian hari berikutnya (zona waktu lokal server)."""
    now = datetime.datetime.now()
    nxt = now + datetime.timedelta(days=1)
    return datetime.datetime(nxt.year, nxt.month, nxt.day, 0, 0).timestamp()


def _all_accounts_daily_capped() -> bool:
    """Semua akun SMTP sudah mencapai batas harian (GMAIL_DAILY_LIMIT)."""
    if GMAIL_DAILY_LIMIT <= 0 or not SMTP_ACCOUNTS:
        return False
    return all(db.get_daily_sent_count(a.key) >= GMAIL_DAILY_LIMIT for a in SMTP_ACCOUNTS)


# ────────────────────────── Jam kerja pengiriman batch ──────────────────────────
#
# Pengaturan disimpan di tabel `settings` (bisa diubah dari UI halaman Batch):
#   work_hours_enabled  = "1" / "0"   — aktif / nonaktif
#   work_start          = "08:00"      — jam mulai (24 jam)
#   work_end            = "17:00"      — jam selesai (24 jam)
#   work_weekdays_only  = "1" / "0"   — hanya Senin–Jumat


def _work_hours_enabled() -> bool:
    return db.get_setting("work_hours_enabled", "0") == "1"


def _work_hours_window() -> tuple[int, int]:
    """(menit_mulai, menit_selesai) sejak 00:00 — default 08:00–17:00."""
    def parse(v: Optional[str], default: int) -> int:
        if v:
            try:
                h, m = v.strip().split(":")
                return int(h) * 60 + int(m)
            except (ValueError, AttributeError):
                pass
        return default
    start = parse(db.get_setting("work_start"), 8 * 60)
    end = parse(db.get_setting("work_end"), 17 * 60)
    if end <= start:
        end = start + 9 * 60  # pengaman: selesai harus setelah mulai
    return start, end


def _work_weekdays_only() -> bool:
    return db.get_setting("work_weekdays_only", "1") == "1"


def _is_work_time(dt: Optional[datetime.datetime] = None) -> bool:
    """Apakah saat ini termasuk jam kerja (zona waktu lokal server)."""
    if not _work_hours_enabled():
        return True
    dt = dt or datetime.datetime.now()
    if _work_weekdays_only() and dt.weekday() >= 5:  # Sabtu & Minggu
        return False
    start, end = _work_hours_window()
    minutes = dt.hour * 60 + dt.minute
    return start <= minutes < end


def _next_work_window() -> float:
    """Epoch jam kerja berikutnya (dipakai saat sekarang di luar jam kerja)."""
    now = datetime.datetime.now()
    start, _ = _work_hours_window()
    for delta in range(0, 9):
        cand = now + datetime.timedelta(days=delta)
        if _work_weekdays_only() and cand.weekday() >= 5:
            continue
        s = cand.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
        if s > now:
            return s.timestamp()
    return (now + datetime.timedelta(days=7)).timestamp()  # fallback aman


def _work_hours_desc() -> str:
    start, end = _work_hours_window()
    days = "Senin–Jumat " if _work_weekdays_only() else ""
    return f"{days}{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}"


def _batch_blocked_reason() -> str:
    """Alasan batch tidak boleh kirim sekarang ('' = boleh kirim)."""
    capped = _all_accounts_daily_capped()
    outside = not _is_work_time()
    if capped and outside:
        return "Kuota harian habis dan di luar jam kerja - batch dijeda otomatis, lanjut saat jam kerja berikutnya"
    if capped:
        return "Kuota harian habis - batch dijeda otomatis, lanjut besok"
    if outside:
        return f"Di luar jam kerja ({_work_hours_desc()}) - batch dijeda otomatis, lanjut saat jam kerja berikutnya"
    return ""


def _next_resume_ts() -> float:
    """Kapan batch boleh lanjut — kombinasi kuota harian & jam kerja."""
    candidates: list[float] = []
    if _all_accounts_daily_capped():
        candidates.append(_next_midnight())
    if not _is_work_time():
        candidates.append(_next_work_window())
    return max(candidates) if candidates else 0.0


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


@app.post("/api/preview-html")
def api_preview_html(
    to: str = Form(...),
    company: str = Form(...),
    position: str = Form("IT Support / DevOps"),
    extra: str = Form(""),
    cv_file: str = Form(""),
    template_name: str = Form("html"),
    experience: str = Form(""),
    sender_name: str = Form(""),
):
    to = _trim(to)
    company = _trim(company)
    position = _trim(position) or "IT Support / DevOps"
    extra = _trim(extra)
    experience = _trim(experience)
    sender_name = _trim(sender_name)
    try:
        variants = build_variants(company, position, sender_name=sender_name)
        html_body = render_body(company, position, extra, template_name, variants=variants, experience=experience, sender_name=sender_name)
    except (ValueError, KeyError) as e:
        html_body = f"Error: {e}"
    return HTMLResponse(content=html_body)


@app.post("/preview", response_class=HTMLResponse)
def preview(
    request: Request,
    to: str = Form(...),
    company: str = Form(...),
    position: str = Form("IT Support / DevOps"),
    extra: str = Form(""),
    cv_file: str = Form(""),
    template_name: str = Form("html"),
    experience: str = Form(""),
    sender_name: str = Form(""),
):
    to = _trim(to)
    company = _trim(company)
    position = _trim(position) or "IT Support / DevOps"
    extra = _trim(extra)
    experience = _trim(experience)
    sender_name = _trim(sender_name)
    cv_file = _trim(cv_file)
    if not cv_file:
        return RedirectResponse(url="/?message=Pilih+file+CV+untuk+preview&msg_type=error", status_code=303)
    try:
        variants = build_variants(company, position, sender_name=sender_name)
        body = render_body(company, position, extra, template_name, variants=variants, plain=True, experience=experience, sender_name=sender_name)
        html_body = render_body(company, position, extra, template_name, variants=variants, experience=experience, sender_name=sender_name)
    except (ValueError, KeyError) as e:
        body = f"Error: {e}"
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
                "template_name": template_name,
                "experience": experience,
                "sender_name": sender_name,
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
    cv_file: str = Form(""),
    template_name: str = Form("html"),
    experience: str = Form(""),
    sender_name: str = Form(""),
):
    to = _trim(to)
    company = _trim(company)
    position = _trim(position) or "IT Support / DevOps"
    extra = _trim(extra)
    experience = _trim(experience)
    sender_name = _trim(sender_name)
    cv_file = _trim(cv_file)

    if not cv_file:
        return RedirectResponse(url="/?message=Pilih+file+CV+dulu&msg_type=error", status_code=303)

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
        success, key, err = _try_send_with_failover(
            to, company, position, extra, cv_path,
            template_name=template_name, experience=experience, sender_name=sender_name,
        )
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


@app.get("/batch/example-csv")
def batch_example_csv():
    content = (
        "email,company,sender_name,experience\n"
        "hrd@ptcontoh.co.id,PT Contoh Sejahtera,Rangga,IT Support dengan 4 tahun pengalaman jaringan\n"
        "career@cvcontoh.com,CV Maju Jaya,,\n"
    )
    return StreamingResponse(
        io.BytesIO(content.encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=contoh_batch.csv"},
    )


@app.post("/batch/preview", response_class=HTMLResponse)
async def batch_preview(
    request: Request,
    file: UploadFile = File(...),
    cv_file: str = Form(""),
    position: str = Form("IT Support / DevOps"),
    extra: str = Form(""),
    template_name: str = Form("html"),
):
    cv_file = _trim(cv_file)
    if not cv_file:
        return RedirectResponse(url="/batch?message=Pilih+file+CV+dulu&msg_type=error", status_code=303)
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
        rows.append({
            "email": email_val,
            "company": company_val,
            "sender_name": _row_field(row, header_map, "sender_name").strip() or None,
            "experience": _row_field(row, header_map, "experience").strip() or None,
            "row": i,
        })

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
        "template_name": template_name,
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
            position=position,
            cv_file=cv_file,
            template_name=template_name,
        ),
    )


@app.post("/batch/send")
def batch_send(
    cache_key: str = Form(...),
    scheduled_at: str = Form(""),
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
    template_name: str = data.get("template_name", "html")
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

    # Jadwal kirim: epoch (detik) dari datetime-local browser; 0 = langsung.
    sched_epoch = 0
    scheduled_at = _trim(scheduled_at)
    if scheduled_at:
        try:
            sched_epoch = int(float(scheduled_at))
        except ValueError:
            sched_epoch = 0

    total = len(rows)
    # Simpan seluruh data batch di DB (payload) agar bisa dilanjutkan walau
    # container restart — bukan hanya di memori.
    payload = json.dumps({
        "rows": rows,
        "position": position,
        "extra": extra,
        "safe_cv": safe_cv,
        "cv_path": cv_path,
        "template_name": template_name,
    })
    job_id = db.create_batch_job(total, filename=filename, scheduled_at=sched_epoch, payload=payload)
    _batch_wake.set()

    if sched_epoch > 0:
        return RedirectResponse(
            url=f"/batch?job_id={job_id}&message=Batch+%23{job_id}+dibuat,+dijadwalkan+sesuai+waktu+yang+dipilih&msg_type=success",
            status_code=303,
        )
    reason = _batch_blocked_reason()
    if reason:
        return RedirectResponse(
            url=f"/batch?job_id={job_id}&message=Batch+%23{job_id}+dibuat+tetapi+{quote(reason)}&msg_type=warning",
            status_code=303,
        )
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

    # Resume-aware: mulai dari counter yang tersimpan (jika batch pernah
    # dijeda/dibatalkan lalu dilanjutkan) — email yang sudah diproses tidak
    # akan dikirim ulang.
    current = db.get_batch_job(job_id) or {}
    results: dict[str, int] = {
        "success": int(current.get("sent") or 0),
        "failed": int(current.get("failed") or 0),
        "rate_limited": int(current.get("rate_limited") or 0),
    }
    last_error = ""
    processed = results["success"] + results["failed"] + results["rate_limited"]

    if processed >= total:
        db.update_batch_job(job_id, status="done",
            sent=results["success"],
            failed=results["failed"],
            rate_limited=results["rate_limited"],
            last_error="",
        )
        return

    # Tutup celah race: pause/cancel bisa masuk antara cek worker dan update
    # status 'running' di bawah — kalau status sudah berubah, jangan lanjut
    # (kalau tidak, jeda bisa 'hilang' dan batch terus berjalan satu siklus).
    current = db.get_batch_job(job_id)
    if current and current["status"] in ("cancelled", "paused"):
        db.update_batch_job(job_id,
            sent=results["success"],
            failed=results["failed"],
            rate_limited=results["rate_limited"],
            last_error=(
                "Dibatalkan pengguna" if current["status"] == "cancelled"
                else "Dijeda pengguna - klik Lanjutkan untuk meneruskan"
            ),
        )
        return

    db.update_batch_job(job_id, status="running", last_error="")
    tpl_name = data.get("template_name") or "html"
    try:
        for row in rows[processed:]:
            # Cek cancel / pause
            current = db.get_batch_job(job_id)
            if current and current["status"] in ("cancelled", "paused"):
                last_error = (
                    "Dibatalkan pengguna" if current["status"] == "cancelled"
                    else "Dijeda pengguna - klik Lanjutkan untuk meneruskan"
                )
                db.update_batch_job(job_id,
                    sent=results["success"],
                    failed=results["failed"],
                    rate_limited=results["rate_limited"],
                    last_error=last_error,
                )
                break

            # Kuota harian habis / di luar jam kerja di tengah jalan — jeda
            # otomatis sampai waktunya tiba. Baris ini belum diproses; nanti
            # dilanjutkan dari sini.
            reason = _batch_blocked_reason()
            if reason:
                last_error = reason
                db.pause_batch_job(job_id, resume_at=_next_resume_ts(), last_error=last_error)
                db.update_batch_job(job_id,
                    sent=results["success"],
                    failed=results["failed"],
                    rate_limited=results["rate_limited"],
                )
                break

            email_val = row["email"]
            company_val = row["company"]
            row_sender = row.get("sender_name") or None
            row_experience = row.get("experience") or None

            try:
                success, key, err = _try_send_with_failover(
                    email_val, company_val, position, extra, cv_path,
                    template_name=tpl_name,
                    experience=row_experience, sender_name=row_sender,
                )
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
                if "rate limit" in err.lower() or "limit harian" in err.lower():
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

        # Kalau berhenti karena cancel/pause, status sudah di-set oleh API —
        # jangan ditimpa jadi 'done'.
        final = db.get_batch_job(job_id)
        if final and final["status"] in ("cancelled", "paused"):
            db.update_batch_job(job_id,
                sent=results["success"],
                failed=results["failed"],
                rate_limited=results["rate_limited"],
                last_error=last_error,
            )
        else:
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


def _load_batch_payload(job: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Baca data batch dari kolom payload DB (bertahan melewati restart)."""
    raw = job.get("payload") or ""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return None
    return data


def _batch_worker() -> None:
    while True:
        # Auto-lanjutkan batch yang waktunya lanjut tiba (mis. jeda karena
        # batas harian — lanjut otomatis hari berikutnya).
        for job in db.get_auto_resumable_paused_jobs():
            db.resume_batch_job(job["id"])

        queued = db.get_queued_batch_jobs()
        if not queued:
            _batch_wake.wait(timeout=5)
            _batch_wake.clear()
            continue

        now = time.time()
        ready = None
        earliest_sched: Optional[float] = None
        for job in queued:
            sched = float(job.get("scheduled_at") or 0)
            if sched > now:
                if earliest_sched is None or sched < earliest_sched:
                    earliest_sched = sched
                continue
            ready = job
            break

        if ready is None:
            # Semua masih menunggu jadwal — tidur sebentar, cek lagi (juga
            # agar jeda/batal tetap responsif). Batch non-jadwal tetap bisa
            # jalan duluan kalau dibuat setelahnya.
            if earliest_sched is not None:
                _batch_wake.wait(timeout=min(earliest_sched - now, 5))
            else:
                _batch_wake.wait(timeout=5)
            _batch_wake.clear()
            continue

        job_id = ready["id"]

        # Ada batasan (kuota harian habis / di luar jam kerja) — jangan mulai;
        # jeda otomatis sampai waktunya tiba.
        reason = _batch_blocked_reason()
        if reason:
            db.pause_batch_job(
                job_id,
                resume_at=_next_resume_ts(),
                last_error=reason,
            )
            continue

        # Payload hanya diambil untuk job yang siap — antrian lain cukup
        # metadata (tanpa payload besar) selama menunggu jadwal.
        ready_full = db.get_batch_job(job_id, with_payload=True)
        data = _load_batch_payload(ready_full) if ready_full else None
        if data is None:
            db.update_batch_job(job_id, status="failed", last_error="Data batch tidak ditemukan (payload kosong)")
            continue

        current = db.get_batch_job(job_id)
        if current and current["status"] in ("cancelled", "paused"):
            continue

        _run_batch(job_id, data)


@dataclass
class JobBody:
    job_id: int


@dataclass
class StageBody:
    email_id: int
    stage: str


@app.post("/api/batch/cancel")
def batch_cancel(body: JobBody):
    db.cancel_batch_job(body.job_id)
    return {"ok": True}


@app.post("/api/batch/pause")
def batch_pause(body: JobBody):
    db.pause_batch_job(body.job_id)
    return {"ok": True}


@app.post("/api/batch/resume")
def batch_resume(body: JobBody):
    db.resume_batch_job(body.job_id)
    _batch_wake.set()
    return {"ok": True}


@app.post("/batch/cancel/{job_id}")
def batch_cancel_form(job_id: int):
    db.cancel_batch_job(job_id)
    return RedirectResponse(
        url=f"/batch?message=Batch+%23{job_id}+dibatalkan&msg_type=warning",
        status_code=303,
    )


@app.post("/batch/pause/{job_id}")
def batch_pause_form(job_id: int):
    db.pause_batch_job(job_id)
    return RedirectResponse(
        url=f"/batch?message=Batch+%23{job_id}+dijeda,+klik+Lanjutkan+untuk+meneruskan&msg_type=warning",
        status_code=303,
    )


@app.post("/batch/resume/{job_id}")
def batch_resume_form(job_id: int):
    db.resume_batch_job(job_id)
    _batch_wake.set()
    return RedirectResponse(
        url=f"/batch?job_id={job_id}&message=Batch+%23{job_id}+dilanjutkan&msg_type=success",
        status_code=303,
    )


@app.post("/api/batch/retry")
def batch_retry(body: JobBody):
    """Kirim ulang batch yang gagal/dibatalkan/selesai-ber-error (harus punya
    payload tersimpan). Email yang sudah terkirim akan dilewati otomatis."""
    job = db.get_batch_job(body.job_id, with_payload=True)
    if not job or not job.get("payload"):
        return {"ok": False, "error": "Data batch tidak tersedia untuk dikirim ulang (batch lama). Upload ulang CSV atau retry per-email dari History."}
    if job["status"] in ("running", "queued", "paused"):
        return {"ok": False, "error": f"Batch masih berstatus {job['status']} — tunggu atau jeda dulu."}
    if not db.retry_batch_job(body.job_id):
        return {"ok": False, "error": "Batch tidak lagi dalam status yang bisa dikirim ulang."}
    _batch_wake.set()
    return {"ok": True}


@app.post("/batch/retry/{job_id}")
def batch_retry_form(job_id: int):
    job = db.get_batch_job(job_id, with_payload=True)
    if not job or not job.get("payload"):
        return RedirectResponse(
            url="/batch?message=Data+batch+tidak+tersedia+untuk+dikirim+ulang+(batch+lama).+Upload+ulang+CSV+atau+retry+per-email+dari+History&msg_type=error",
            status_code=303,
        )
    if job["status"] in ("running", "queued", "paused"):
        return RedirectResponse(
            url=f"/batch?message=Batch+%23{job_id}+masih+berjalan+({job['status']})&msg_type=warning",
            status_code=303,
        )
    if not db.retry_batch_job(job_id):
        return RedirectResponse(
            url="/batch?message=Batch+tidak+lagi+dalam+status+yang+bisa+dikirim+ulang&msg_type=warning",
            status_code=303,
        )
    _batch_wake.set()
    return RedirectResponse(
        url=f"/batch?job_id={job_id}&message=Batch+%23{job_id}+dijadwalkan+ulang,+email+yang+sudah+terkirim+akan+dilewati&msg_type=success",
        status_code=303,
    )


# ────────────────────────── SETTINGS: JAM KERJA ──────────────────────────


@app.post("/settings/work-hours")
def save_work_hours(
    enabled: str = Form("0"),
    start: str = Form("08:00"),
    end: str = Form("17:00"),
    weekdays_only: str = Form("0"),
):
    """Simpan pengaturan jam kerja pengiriman batch (dari halaman /batch)."""
    start = _trim(start)
    end = _trim(end)
    time_re = re.compile(r"^\d{1,2}:\d{2}$")
    if not (time_re.match(start) and time_re.match(end)):
        return RedirectResponse(
            url="/batch?message=Format+jam+tidak+valid+(contoh:+08:00)&msg_type=error",
            status_code=303,
        )
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
        return RedirectResponse(
            url="/batch?message=Jam+di+luar+jangkauan+(00:00-23:59)&msg_type=error",
            status_code=303,
        )
    if sh * 60 + sm >= eh * 60 + em:
        return RedirectResponse(
            url="/batch?message=Jam+selesai+harus+setelah+jam+mulai&msg_type=error",
            status_code=303,
        )
    db.set_setting("work_hours_enabled", "1" if enabled == "on" else "0")
    db.set_setting("work_start", start)
    db.set_setting("work_end", end)
    db.set_setting("work_weekdays_only", "1" if weekdays_only == "on" else "0")
    # Kalau sekarang sudah boleh kirim, langsung lanjutkan batch yang dijeda
    # karena jam kerja (yang dijeda karena kuota harian tetap menunggu batasnya).
    if not _batch_blocked_reason():
        for j in db.get_active_batch_jobs():
            if j["status"] == "paused" and "jam kerja" in (j.get("last_error") or ""):
                db.resume_batch_job(j["id"])
    _batch_wake.set()
    return RedirectResponse(
        url="/batch?message=Pengaturan+jam+kerja+disimpan&msg_type=success",
        status_code=303,
    )


# ────────────────────────── SSE Batch Progress ──────────────────────────


@app.get("/api/batch-progress/{job_id}/stream")
async def batch_progress_stream(job_id: int):
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            job = await asyncio.to_thread(db.get_batch_job, job_id)
            if not job:
                yield f"event: error\ndata: {json.dumps({'error': 'not found'})}\n\n"
                break

            yield f"data: {json.dumps(job)}\n\n"

            if job["status"] in ("done", "cancelled", "failed"):
                yield f"event: {job['status']}\ndata: {json.dumps(job)}\n\n"
                break

            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/batch-progress/{job_id}")
def batch_progress(job_id: int):
    job = db.get_batch_job(job_id)
    if not job:
        return {"error": "not found"}
    return job


# ────────────────────────── TRACKER LAMARAN ──────────────────────────


@app.get("/lamaran", response_class=HTMLResponse)
def tracker_page(request: Request):
    """Kanban board: lamaran terkirim dikelompokkan per tahap (pipeline)."""
    applications = db.get_applications()
    stage_stats = db.get_stage_stats()
    total_apps = db.get_email_count(status_filter="sent")
    return templates.TemplateResponse(
        request, "tracker.html",
        _ctx(request, applications=applications, stage_stats=stage_stats, total_apps=total_apps),
    )


@app.post("/api/tracker/stage")
def tracker_set_stage(body: StageBody):
    """Ubah tahap sebuah lamaran (drag & drop / dropdown di board)."""
    ok = db.update_email_stage(body.email_id, body.stage)
    if not ok:
        return {"ok": False, "error": "Tahap tidak valid atau lamaran tidak ditemukan"}
    # Balikin funnel asli dari server supaya angka statistik tetap benar
    # (board hanya menampilkan 400 lamaran terbaru, tapi funnel hitung semua).
    return {"ok": True, "stage": body.stage, "stage_stats": db.get_stage_stats()}


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
    smtp_stats = db.get_smtp_account_stats()
    return templates.TemplateResponse(
        request, "dashboard.html",
        _ctx(request, stats=stats, smtp_stats=smtp_stats),
    )


# ────────────────────────── BACKUP ──────────────────────────


def _backup_worker() -> None:
    """Backup otomatis harian + bersihkan backup yang lebih lama dari N hari."""
    while True:
        try:
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            marker = BACKUP_DIR / f"auto-{today}.db"
            if not marker.exists():
                db.create_backup(marker)
            cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
            for f in BACKUP_DIR.glob("auto-*.db"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass
        except Exception:
            pass
        time.sleep(6 * 3600)


@app.get("/backup/download")
def backup_download():
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db.create_backup(tmp)
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return RedirectResponse(
            url=f"/dashboard?message=Backup+gagal:+{quote(str(e))}&msg_type=error",
            status_code=303,
        )
    fname = f"lamaran-mailer-backup-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.db"

    def _iter():
        try:
            with open(tmp, "rb") as f:
                chunk = f.read(65536)
                while chunk:
                    yield chunk
                    chunk = f.read(65536)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    return StreamingResponse(
        _iter(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
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
    if path.exists() and path.parent.resolve() == CV_DIR.resolve():
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


def _html_to_plain(html_text: str) -> str:
    """Strip HTML tags dan unescape entities → plain text sederhana."""
    text = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6]|td|th)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _plain_to_html(plain_text: str) -> str:
    """Wrap paragraf plain text (dipisah baris kosong) menjadi tag <p> HTML."""
    paragraphs = re.split(r"\n\s*\n", plain_text.strip())
    html_parts = []
    for para in paragraphs:
        line = re.sub(r"\s*\n\s*", " ", para).strip()
        if line:
            html_parts.append(f"<p>{line}</p>")
    return "\n".join(html_parts)


@app.post("/templates-editor/save")
def save_template(
    name: str = Form(...),
    body: str = Form(...),
    html_body: str = Form(""),
):
    name = _trim(name)
    body = _trim(body)
    html_body = _trim(html_body)
    if not name or not body:
        return RedirectResponse(
            url="/templates-editor?message=Nama+template+dan+isi+email+wajib+diisi&msg_type=error",
            status_code=303,
        )
    if not html_body:
        html_body = _plain_to_html(body)
    if len(name) > 50 or not re.fullmatch(r"[a-zA-Z0-9 ._\-]+", name):
        return RedirectResponse(
            url="/templates-editor?message=Nama+template+hanya+boleh+huruf,+angka,+spasi,+titik,+strip+dan+underscore+(maks+50)&msg_type=error",
            status_code=303,
        )
    # Template default memakai placeholder variant (greeting/opening/closing,
    # sender_*, wa_link, linkedin_url) — sediakan nilai dummy agar validasi
    # tidak menolak template yang memakainya.
    _dummy_variants = {
        "greeting": "Test", "opening": "Test", "closing": "Test",
        "experience": "Test",
        "sender_name": "Test", "sender_phone": "Test", "sender_email": "Test",
        "sender_linkedin": "Test", "sender_github": "Test",
        "wa_link": "Test", "linkedin_url": "Test", "github_url": "Test",
    }
    try:
        body.format(company="Test", position="Test", extra="", **_dummy_variants)
        html_body.format(company="Test", position="Test", extra="", **_dummy_variants)
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
