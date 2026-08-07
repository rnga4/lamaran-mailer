import csv
import datetime
import io
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path("/app/data/mailer.db")
_initialized = False


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    global _initialized
    if _initialized:
        return
    conn = _conn()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            to_addr TEXT NOT NULL,
            company TEXT NOT NULL,
            position TEXT NOT NULL,
            extra TEXT,
            cv_file TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sent',
            error TEXT,
            smtp_account TEXT DEFAULT '',
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rate_limit (
            account_key TEXT NOT NULL PRIMARY KEY,
            window_start REAL NOT NULL,
            count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS batch_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total INTEGER NOT NULL DEFAULT 0,
            sent INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            rate_limited INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'running',
            last_error TEXT DEFAULT '',
            filename TEXT DEFAULT '',
            scheduled_at REAL DEFAULT 0,
            resume_at REAL DEFAULT 0,
            payload TEXT DEFAULT '',
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            body TEXT NOT NULL,
            html_body TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_created ON emails(created_at DESC)")
    _migrate_db(conn)
    conn.commit()
    conn.close()
    _initialized = True


def _migrate_db(conn: sqlite3.Connection) -> None:
    # Add smtp_account column if it doesn't exist (legacy DBs)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(emails)").fetchall()]
    if "smtp_account" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN smtp_account TEXT DEFAULT ''")

    # Add filename/scheduled_at/payload columns to batch_jobs if missing (legacy DBs)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(batch_jobs)").fetchall()]
    if "filename" not in cols:
        conn.execute("ALTER TABLE batch_jobs ADD COLUMN filename TEXT DEFAULT ''")
    if "scheduled_at" not in cols:
        conn.execute("ALTER TABLE batch_jobs ADD COLUMN scheduled_at REAL DEFAULT 0")
    if "resume_at" not in cols:
        conn.execute("ALTER TABLE batch_jobs ADD COLUMN resume_at REAL DEFAULT 0")
    if "payload" not in cols:
        conn.execute("ALTER TABLE batch_jobs ADD COLUMN payload TEXT DEFAULT ''")

    # Migrate old rate_limit (with id PK) to new per-account format (account_key PK)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(rate_limit)").fetchall()]
    if "account_key" not in cols:
        # Save old data if exists
        old_data = None
        if "id" in cols:
            old = conn.execute("SELECT * FROM rate_limit WHERE id=1").fetchone()
            if old:
                old_data = {"window_start": old["window_start"], "count": old["count"]}
        conn.execute("DROP TABLE rate_limit")
        conn.execute("""
            CREATE TABLE rate_limit (
                account_key TEXT NOT NULL PRIMARY KEY,
                window_start REAL NOT NULL,
                count INTEGER NOT NULL DEFAULT 0
            )
        """)
        if old_data:
            conn.execute(
                "INSERT INTO rate_limit (account_key, window_start, count) VALUES (?, ?, ?)",
                ("default", old_data["window_start"], old_data["count"]),
            )

    # Migrate old settings to templates table
    try:
        existing = conn.execute("SELECT COUNT(*) AS c FROM templates").fetchone()["c"]
        if existing == 0:
            body = conn.execute("SELECT value FROM settings WHERE key='body_template'").fetchone()
            html = conn.execute("SELECT value FROM settings WHERE key='html_template'").fetchone()
            if body and html:
                conn.execute(
                    "INSERT INTO templates (name, body, html_body, created_at) VALUES (?, ?, ?, ?)",
                    ("default", body["value"], html["value"], time.time()),
                )
    except sqlite3.OperationalError:
        pass


def _ensure_db() -> None:
    if not _initialized:
        init_db()


def log_email(
    to_addr: str,
    company: str,
    position: str,
    extra: str,
    cv_file: str,
    status: str = "sent",
    error: Optional[str] = None,
    smtp_account: str = "",
) -> None:
    _ensure_db()
    conn = _conn()
    conn.execute(
        "INSERT INTO emails (to_addr, company, position, extra, cv_file, status, error, smtp_account, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (to_addr, company, position, extra, cv_file, status, error, smtp_account, time.time()),
    )
    conn.commit()
    conn.close()


def get_emails(
    limit: int = 25,
    offset: int = 0,
    search: str = "",
    status_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    account_filter: str = "",
) -> list[dict[str, Any]]:
    _ensure_db()
    conn = _conn()
    query = "SELECT *, datetime(created_at, 'unixepoch', 'localtime') as created_at_fmt FROM emails WHERE 1=1"
    params: list[Any] = []

    if search:
        query += " AND (to_addr LIKE ? OR company LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)

    if account_filter:
        query += " AND smtp_account = ?"
        params.append(account_filter)

    if date_from:
        query += " AND datetime(created_at, 'unixepoch', 'localtime') >= ?"
        params.append(date_from)

    if date_to:
        query += " AND datetime(created_at, 'unixepoch', 'localtime') <= ?"
        params.append(date_to + " 23:59:59")

    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_email_by_id(email_id: int) -> Optional[dict[str, Any]]:
    _ensure_db()
    conn = _conn()
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_email_count(
    search: str = "",
    status_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    account_filter: str = "",
) -> int:
    _ensure_db()
    conn = _conn()
    query = "SELECT COUNT(*) as total FROM emails WHERE 1=1"
    params: list[Any] = []

    if search:
        query += " AND (to_addr LIKE ? OR company LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])

    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)

    if account_filter:
        query += " AND smtp_account = ?"
        params.append(account_filter)

    if date_from:
        query += " AND datetime(created_at, 'unixepoch', 'localtime') >= ?"
        params.append(date_from)

    if date_to:
        query += " AND datetime(created_at, 'unixepoch', 'localtime') <= ?"
        params.append(date_to + " 23:59:59")

    row = conn.execute(query, params).fetchone()
    conn.close()
    return row["total"]


def delete_email(email_id: int) -> None:
    _ensure_db()
    conn = _conn()
    conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))
    conn.commit()
    conn.close()


def clear_all_emails() -> None:
    _ensure_db()
    conn = _conn()
    conn.execute("DELETE FROM emails")
    conn.commit()
    conn.close()


def get_distinct_smtp_accounts() -> list[str]:
    _ensure_db()
    conn = _conn()
    rows = conn.execute("SELECT DISTINCT smtp_account FROM emails WHERE smtp_account IS NOT NULL AND smtp_account != '' ORDER BY smtp_account").fetchall()
    conn.close()
    return [r["smtp_account"] for r in rows]


def delete_emails(ids: list[int]) -> None:
    if not ids:
        return
    _ensure_db()
    conn = _conn()
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM emails WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()


def export_emails_csv(
    search: str = "",
    status_filter: str = "",
    date_from: str = "",
    date_to: str = "",
    account_filter: str = "",
) -> str:
    _ensure_db()
    conn = _conn()
    query = "SELECT *, datetime(created_at, 'unixepoch', 'localtime') as created_at_fmt FROM emails WHERE 1=1"
    params: list[Any] = []

    if search:
        query += " AND (to_addr LIKE ? OR company LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)
    if account_filter:
        query += " AND smtp_account = ?"
        params.append(account_filter)
    if date_from:
        query += " AND datetime(created_at, 'unixepoch', 'localtime') >= ?"
        params.append(date_from)
    if date_to:
        query += " AND datetime(created_at, 'unixepoch', 'localtime') <= ?"
        params.append(date_to + " 23:59:59")

    query += " ORDER BY created_at DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "waktu", "email", "perusahaan", "posisi", "cv", "status", "error", "akun"])
    for e in rows:
        writer.writerow([
            e["id"], e["created_at_fmt"], e["to_addr"], e["company"],
            e["position"], e["cv_file"], e["status"],
            e["error"] or "",
            e["smtp_account"] or "",
        ])
    return output.getvalue()


def get_unique_companies() -> list[str]:
    _ensure_db()
    conn = _conn()
    rows = conn.execute("SELECT DISTINCT company FROM emails WHERE status='sent' ORDER BY company").fetchall()
    conn.close()
    return [r["company"] for r in rows]


def check_duplicate_email(to_addr: str, company: str) -> bool:
    _ensure_db()
    conn = _conn()
    row = conn.execute(
        "SELECT id FROM emails WHERE to_addr = ? AND company = ? AND status = 'sent' LIMIT 1",
        (to_addr, company),
    ).fetchone()
    conn.close()
    return row is not None


def get_stats() -> dict[str, Any]:
    _ensure_db()
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) as c FROM emails").fetchone()["c"]
    sent = conn.execute("SELECT COUNT(*) as c FROM emails WHERE status='sent'").fetchone()["c"]
    failed = conn.execute("SELECT COUNT(*) as c FROM emails WHERE status='failed'").fetchone()["c"]

    hour_ago = time.time() - 3600
    last_hour = conn.execute("SELECT COUNT(*) as c FROM emails WHERE created_at > ? AND status='sent'", (hour_ago,)).fetchone()["c"]

    today_start = _today_start()
    today_count = conn.execute("SELECT COUNT(*) as c FROM emails WHERE created_at > ? AND status='sent'", (today_start,)).fetchone()["c"]

    by_company = conn.execute(
        "SELECT company, COUNT(*) as count FROM emails WHERE status='sent' GROUP BY company ORDER BY count DESC LIMIT 10"
    ).fetchall()

    by_position = conn.execute(
        "SELECT position, COUNT(*) as count FROM emails WHERE status='sent' GROUP BY position ORDER BY count DESC"
    ).fetchall()

    daily = conn.execute(
        "SELECT date(created_at, 'unixepoch', 'localtime') as day, COUNT(*) as count FROM emails WHERE status='sent' GROUP BY day ORDER BY day DESC LIMIT 14"
    ).fetchall()

    unique_companies = conn.execute("SELECT COUNT(DISTINCT company) as c FROM emails WHERE status='sent'").fetchone()["c"]

    by_account = conn.execute(
        "SELECT smtp_account, COUNT(*) as count FROM emails WHERE status='sent' GROUP BY smtp_account ORDER BY count DESC"
    ).fetchall()

    conn.close()
    daily_max = max((r["count"] for r in daily), default=0)
    return {
        "total": total,
        "sent": sent,
        "failed": failed,
        "last_hour": last_hour,
        "today": today_count,
        "unique_companies": unique_companies,
        "by_company": [dict(r) for r in by_company],
        "by_position": [dict(r) for r in by_position],
        "daily": [dict(r) for r in daily],
        "daily_max": daily_max,
        "by_account": [dict(r) for r in by_account],
    }


def _today_start() -> float:
    now = datetime.datetime.now()
    return datetime.datetime(now.year, now.month, now.day, 0, 0).timestamp()


def peek_rate_limit(account_key: str, max_per_hour: int = 30) -> tuple[bool, int]:
    """Read-only check — does NOT increment count. Use before sending."""
    _ensure_db()
    conn = _conn()
    row = conn.execute("SELECT * FROM rate_limit WHERE account_key=?", (account_key,)).fetchone()
    conn.close()
    now = time.time()
    if not row:
        return True, 0
    if now - row["window_start"] > 3600:
        return True, 0
    if row["count"] >= max_per_hour:
        remaining = int(3600 - (now - row["window_start"]))
        return False, remaining
    return True, 0


def use_rate_limit(account_key: str, max_per_hour: int = 30) -> None:
    """Consume one slot. Call AFTER successful send."""
    _ensure_db()
    conn = _conn()
    now = time.time()
    row = conn.execute("SELECT * FROM rate_limit WHERE account_key=?", (account_key,)).fetchone()
    if not row:
        conn.execute("INSERT INTO rate_limit (account_key, window_start, count) VALUES (?, ?, 1)", (account_key, now))
    elif now - row["window_start"] > 3600:
        conn.execute("UPDATE rate_limit SET window_start=?, count=1 WHERE account_key=?", (now, account_key))
    else:
        conn.execute("UPDATE rate_limit SET count=count+1 WHERE account_key=?", (account_key,))
    conn.commit()
    conn.close()


def get_rate_limit_info(account_key: str = "default", max_per_hour: int = 30) -> dict[str, int]:
    _ensure_db()
    conn = _conn()
    row = conn.execute("SELECT * FROM rate_limit WHERE account_key=?", (account_key,)).fetchone()
    conn.close()
    now = time.time()

    if not row:
        return {"used": 0, "remaining": max_per_hour, "resets_in": 3600, "max": max_per_hour}

    window_start, count = row["window_start"], row["count"]

    if now - window_start > 3600:
        return {"used": 0, "remaining": max_per_hour, "resets_in": 3600, "max": max_per_hour}

    return {
        "used": count,
        "remaining": max(0, max_per_hour - count),
        "resets_in": max(0, int(3600 - (now - window_start))),
        "max": max_per_hour,
    }


def get_all_rate_limits(max_per_hour: int = 30) -> dict[str, dict[str, int]]:
    _ensure_db()
    conn = _conn()
    rows = conn.execute("SELECT * FROM rate_limit").fetchall()
    conn.close()
    now = time.time()
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        key: str = row["account_key"]
        if now - row["window_start"] > 3600:
            result[key] = {"used": 0, "remaining": max_per_hour, "resets_in": 3600, "max": max_per_hour}
        else:
            result[key] = {
                "used": row["count"],
                "remaining": max(0, max_per_hour - row["count"]),
                "resets_in": max(0, int(3600 - (now - row["window_start"]))),
                "max": max_per_hour,
            }
    return result


def get_daily_sent_count(account_key: str = "") -> int:
    """Jumlah terkirim hari ini. Tanpa argumen = semua akun; dengan account_key = per akun."""
    _ensure_db()
    conn = _conn()
    today_midnight = int(datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp())
    if account_key:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM emails WHERE created_at >= ? AND status='sent' AND smtp_account = ?",
            (today_midnight, account_key),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM emails WHERE created_at >= ? AND status='sent'",
            (today_midnight,),
        ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    _ensure_db()
    conn = _conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    _ensure_db()
    conn = _conn()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def get_all_templates() -> list[dict[str, Any]]:
    _ensure_db()
    conn = _conn()
    rows = conn.execute("SELECT name, body, html_body FROM templates ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_db_path() -> str:
    return str(DB_PATH)


def create_backup(dest_path: str | Path) -> None:
    """Salin DB ke path tujuan memakai API backup sqlite3 — konsisten walau
    sedang ada penulisan lain (sending/rate limit)."""
    _ensure_db()
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(DB_PATH))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def get_smtp_account_stats() -> dict[str, dict[str, int]]:
    """Per akun SMTP: jumlah terkirim vs gagal (dari riwayat email)."""
    _ensure_db()
    conn = _conn()
    rows = conn.execute(
        "SELECT smtp_account, status, COUNT(*) as c FROM emails "
        "WHERE smtp_account IS NOT NULL AND smtp_account != '' "
        "GROUP BY smtp_account, status"
    ).fetchall()
    conn.close()
    result: dict[str, dict[str, int]] = {}
    for r in rows:
        acc = result.setdefault(r["smtp_account"], {"sent": 0, "failed": 0})
        if r["status"] == "sent":
            acc["sent"] = r["c"]
        else:
            acc["failed"] += r["c"]
    return result


def get_template_by_name(name: str) -> Optional[dict[str, str]]:
    _ensure_db()
    conn = _conn()
    row = conn.execute("SELECT body, html_body FROM templates WHERE name=?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_template(name: str, body: str, html_body: str) -> None:
    _ensure_db()
    conn = _conn()
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO templates (name, body, html_body, created_at) VALUES (?, ?, ?, ?)",
        (name, body, html_body, now),
    )
    conn.commit()
    conn.close()


def delete_template(name: str) -> None:
    _ensure_db()
    conn = _conn()
    conn.execute("DELETE FROM templates WHERE name=?", (name,))
    conn.commit()
    conn.close()


def create_batch_job(
    total: int,
    filename: str = "",
    scheduled_at: float = 0,
    payload: str = "",
) -> int:
    _ensure_db()
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO batch_jobs (total, sent, failed, rate_limited, status, filename, scheduled_at, payload, created_at) VALUES (?, 0, 0, 0, 'queued', ?, ?, ?, ?)",
        (total, filename, scheduled_at, payload, time.time()),
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()
    return job_id


def update_batch_job(job_id: int, **kwargs: Any) -> None:
    _ensure_db()
    conn = _conn()
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in kwargs.items():
        if k in ("sent", "failed", "rate_limited", "status", "last_error", "scheduled_at", "resume_at"):
            sets.append(f"{k}=?")
            vals.append(v)
    if sets:
        vals.append(job_id)
        conn.execute(f"UPDATE batch_jobs SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()
    conn.close()


def get_batch_job(job_id: int, with_payload: bool = False) -> Optional[dict[str, Any]]:
    _ensure_db()
    conn = _conn()
    row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return None
    job = dict(row)
    if not with_payload:
        job["has_payload"] = bool(job.pop("payload", ""))
    return job


def cancel_batch_job(job_id: int) -> None:
    _ensure_db()
    conn = _conn()
    conn.execute("UPDATE batch_jobs SET status='cancelled' WHERE id=? AND status IN ('running', 'queued', 'paused')", (job_id,))
    conn.commit()
    conn.close()


def pause_batch_job(job_id: int, resume_at: float = 0, last_error: str = "") -> None:
    """Jeda sementara batch yang sedang berjalan / menunggu. Kalau resume_at
    diisi, worker akan melanjutkannya otomatis saat waktunya tiba (mis. besok
    setelah batas harian Gmail reset)."""
    _ensure_db()
    conn = _conn()
    conn.execute(
        "UPDATE batch_jobs SET status='paused', resume_at=?, last_error=? WHERE id=? AND status IN ('running', 'queued')",
        (resume_at, last_error, job_id),
    )
    conn.commit()
    conn.close()


def resume_batch_job(job_id: int) -> None:
    """Lanjutkan batch yang dijeda (kembali ke antrian) dan hapus jadwal
    auto-lanjut bila ada."""
    _ensure_db()
    conn = _conn()
    conn.execute("UPDATE batch_jobs SET status='queued', resume_at=0 WHERE id=? AND status='paused'", (job_id,))
    conn.commit()
    conn.close()


def get_auto_resumable_paused_jobs() -> list[dict[str, Any]]:
    """Batch berstatus 'paused' yang waktunya lanjut otomatis sudah tiba
    (resume_at di masa lalu) — dipakai worker untuk melanjutkannya."""
    _ensure_db()
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM batch_jobs WHERE status='paused' AND resume_at > 0 AND resume_at <= ? ORDER BY resume_at ASC",
        (time.time(),),
    ).fetchall()
    conn.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        j = dict(row)
        j["has_payload"] = bool(j.pop("payload", ""))
        result.append(j)
    return result


def retry_batch_job(job_id: int) -> int:
    """Kirim ulang batch gagal/dibatalkan/selesai-ber-error. Worker akan
    melewati email yang sudah terkirim (dari counter tersimpan) dan mencoba
    ulang sisanya yang gagal/rate-limited/belum sempat dicoba.
    Mengembalikan jumlah baris yang ter-update (0 = status sudah berubah
    sejak dicek, mis. jadi running)."""
    _ensure_db()
    conn = _conn()
    cur = conn.execute(
        "UPDATE batch_jobs SET status='queued', last_error='' WHERE id=? AND status IN ('failed', 'done', 'cancelled')",
        (job_id,),
    )
    conn.commit()
    conn.close()
    return cur.rowcount


def get_batch_jobs(limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    _ensure_db()
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM batch_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    conn.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        j = dict(row)
        j["has_payload"] = bool(j.pop("payload", ""))
        result.append(j)
    return result


def get_active_batch_jobs() -> list[dict[str, Any]]:
    _ensure_db()
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM batch_jobs WHERE status IN ('running', 'queued', 'paused') "
        "ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, created_at ASC"
    ).fetchall()
    conn.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        j = dict(row)
        j["has_payload"] = bool(j.pop("payload", ""))
        result.append(j)
    return result


def get_queued_batch_jobs(with_payload: bool = False) -> list[dict[str, Any]]:
    _ensure_db()
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM batch_jobs WHERE status='queued' ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    result: list[dict[str, Any]] = []
    for row in rows:
        j = dict(row)
        if not with_payload:
            j["has_payload"] = bool(j.pop("payload", ""))
        result.append(j)
    return result


def fail_stale_running_jobs() -> None:
    """Job yang 'running' saat server mati/restart diubah jadi 'paused' — bukan
    gagal — karena payload tersimpan di DB, jadi bisa dilanjutkan."""
    _ensure_db()
    conn = _conn()
    conn.execute(
        "UPDATE batch_jobs SET status='paused', last_error='Server restart, batch dijeda - klik Lanjutkan untuk meneruskan' WHERE status='running'"
    )
    conn.commit()
    conn.close()
