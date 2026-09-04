# Lamaran Mailer

Automated job application email sender with Web UI and CLI.

> 💡 **Kalau mau lanjut kerja besok**: baca bagian **«Catatan Sesi Terakhir»** di paling bawah — berisi status terbaru, apa yang sudah dikerjakan, dan daftar fitur yang belum dikerjakan.

## Quick Start

```bash
cp .env.example .env   # isi SMTP + kontak pengirim
docker compose up --build -d
# buka http://localhost:8086
```

## Architecture

- **`app.py`** (FastAPI) — web UI, API endpoints, single/batch sending, batch worker thread, auth, backup worker
- **`email_service.py`** — inti email: 4 desain HTML + plain text, variabel acak (greeting/opening/closing), render via `str.format()`, kirim via SMTP
- **`database.py`** — SQLite (`/app/data/mailer.db`), logging, rate limit, templates, batch jobs + payload (bertahan restart), backup
- **`send_application.py`** — CLI wrapper single-send (pakai desain `html` default)
- **Docker** — host port **8086** → container 8000, `TZ=Asia/Jakarta` (limit harian & jam kerja ikut WIB), volume `./cv` dan `./data`

## Features

### SMTP Multi-Account Failover
- Sampai 9 akun: `SMTP_` + `SMTP2_` … `SMTP9_` env vars
- Auto-failover: akun kena rate limit/gagal → coba akun berikutnya; round-robin antar akun
- `_try_send_with_failover()` di app.py menegakkan rate limit **dan** `GMAIL_DAILY_LIMIT` per akun, plus mengunci (`_send_lock`) agar aman antar-thread (batch vs kirim tunggal)

### Email Designs (dropdown "Desain Email")
4 desain bawaan (registri `EMAIL_DESIGNS` di `email_service.py`) + template kustom dari DB ikut muncul di dropdown:

| id | Nama | Karakter |
|---|---|---|
| `html` | Premium Klasik (default) | gradien indigo→violet, kartu putih, tombol pil kontak |
| `minimal` | Minimal Modern | latar abu muda, tipografi tipis (font-weight 300), link kontak teks |
| `dark` | Elegant Dark | navy pekat + aksen emas; **punya meta `color-scheme: dark`** biar Gmail/Apple Mail tidak auto-invert |
| `serif` | Editorial Serif | kertas krem + Georgia serif + burgundy |

- Dropdown di form **Kirim tunggal** dan **Batch**; pilihan disimpan lewat hidden field `template_name` dan masuk ke payload batch (retry/auto-lanjut tetap pakai desain yang sama)
- Semua desain: layout tabel + inline style (kompatibel Gmail/Outlook), tombol WhatsApp/Email/LinkedIn/**GitHub**; **responsif di HP**: tombol kontak memakai inline-block fluid dalam satu `<td>` (auto-wrap di 375px, tetap rapi walau Gmail membuang `<style>`) + `<style>` `@media (max-width:480px)` (stack tombol + padding kartu mengecil)
- `render_body()`: variants (greeting/opening/closing/sender_*) **selalu disuntik**; variants parsial di-merge dengan default; kunci bentrok (`company`/`position`/`extra`) dibuang; error format → `ValueError` berisi daftar variabel yang tersedia. Param `plain=True` → ambil `template_data["body"]` (plain text) dari template kustom; kalau template kustom tidak ada → fallback `BODY_TEMPLATE`. `build_email()` dan rute preview plain text pakai `render_body(..., template_name, plain=True)` — bukan lagi hardcode `"plain"`

### Email Templates & Variabel
- Template kustom: tabel `templates`, edit via UI `/templates-editor` (validasi nama: `[a-zA-Z0-9 ._\-]`, max 50)
- **Simpla UI**: cuma 1 kotak utama "Isi Email" (**plain text**, wajib) — tulis seperti pesan biasa, kosong baris = paragraf. Field "HTML" opsional & disembunyikan di dalam `<details>` "Mode HTML". Kalau HTML dikosongkan saat save → **auto-generate** dari plain text via `_plain_to_html()` di `app.py`. Jadi cukup isi kotak plain aja.
- **Tombol variabel klik-sisip** di `/templates-editor`: tombol berlabel manusiawi (mis. "+ Nama perusahaan") dengan atribut `data-insert="{company}"` dalam `<template>`; JS menyisipkan token ke textarea yang sedang fokus (fallback: textarea plain pertama)
- Variabel: `{company}` `{position}` `{extra}` `{experience}` `{greeting}` `{opening}` `{closing}` `{sender_name}` `{sender_phone}` `{sender_email}` `{sender_linkedin}` `{sender_github}` `{wa_link}` `{linkedin_url}` `{github_url}`
- **`{experience}` (paragraf "Pengalaman & Keahlian")**: punya nilai default (`EXPERIENCE_DEFAULT` di `email_service.py`); dipakai ulang di `BODY_TEMPLATE` + 4 desain HTML; kosong di form → pakai default. Template kustom yang tidak punya `{experience}` tetap aman (kwarg ekstra diabaikan `str.format`)
- Kontak pengirim dari env: `SENDER_PHONE` (**`wa.me` otomatis normalisasi 0→62**, mis. `0822…` → `wa.me/62822…`), `SENDER_LINKEDIN`, `SENDER_GITHUB`

### Kirim Tunggal — Override Nama & Pengalaman (per-kirim)
- Form **Kirim tunggal** punya 2 field opsional: **Nama Perkenalan / Pengirim** (`sender_name`) & **Pengalaman & Keahlian** (`experience`)
- `sender_name` menggantikan nama di badan email (paragraf pembuka `{opening}` **dan** tanda tangan `{sender_name}`); kosong → pakai `SMTP_FROM_NAME` dari env
- `experience` menggantikan paragraf pengalaman (variable `{experience}`); kosong → pakai `EXPERIENCE_DEFAULT`
- Mengalir lewat `/preview`, `/api/preview-html`, `/send`, `_try_send_with_failover`, `build_email`; hidden field diteruskan di form "Kirim Sekarang"
- Versi HTML di-`html.escape()` (anti-injeksi); tetap tampil konsisten di plain + HTML karena `build_email` render keduanya dari `variants` yang sama

### Batch Sending
- CSV kolom `email` + `company` (atau `nama_pt`); upload preview → validasi (duplikat, sudah pernah dikirim, email invalid) → `POST /batch/send`
- **Payload tersimpan di DB** (`batch_jobs.payload`) — batch bisa dilanjutkan setelah container restart; retry 1-klik (email yang sudah terkirim dilewati otomatis)
- **Resume-aware**: `_run_batch` mulai dari counter tersimpan, cek cancel/pause tiap iterasi
- Progress via SSE `/api/batch-progress/{job_id}/stream`; status: `queued → running → paused/done/cancelled/failed`
- Delay acak `SEND_DELAY_MIN/MAX`; `SPREAD_HOURS` meratakan kiriman selama N jam
- **Auto-pause otomatis** (dua sumber, digabung):
  1. **Kuota harian habis** (`GMAIL_DAILY_LIMIT` tercapai semua akun) → pause, `resume_at = tengah malam WIB`
  2. **Di luar jam kerja** → pause, `resume_at = jam kerja berikutnya`
  - Resume = `max(tengah malam, jam kerja berikutnya)`. Worker thread auto-resume saat `resume_at` tiba. **Kirim manual (halaman Kirim) tidak terpengaruh.**
- **Jam Kerja**: diatur dari kartu "Jam Kerja Pengiriman" di `/batch` → tersimpan di tabel `settings`: `work_hours_enabled`, `work_start`, `work_end`, `work_weekdays_only`. Default nonaktif, 08:00–17:00, Senin–Jumat. Saat pengaturan berubah & sekarang sudah boleh kirim, batch yang ter-pause karena jam kerja langsung di-resume otomatis.

### Rate Limiting
- Sliding window per akun (1 jam) di SQLite
- `GMAIL_DAILY_LIMIT` **ditegakkan** (bukan hanya tampil di UI): kirim berhenti saat akun mencapai limit harian, pindah akun berikutnya
- `RATE_LIMIT_PER_HOUR` global (999999 ≈ tanpa limit)

### Tracker Lamaran (Kanban)
- Halaman `/lamaran`: board 5 kolom **Applied → Follow-up → Interview → Offer → Ditolak**
- Setiap lamaran terkirim (`status='sent'`) otomatis masuk sebagai `Applied` (kolom `stage` di tabel `emails`, default `applied`, + `stage_updated_at`)
- **Drag & drop** antar kolom atau dropdown di tiap kartu (fallback mobile) → `POST /api/tracker/stage` (validasi stage di `VALID_STAGES`, hanya untuk email `sent`)
- Board menampilkan **400 lamaran terbaru** (biar ringan); funnel statistik di atas menghitung **semua** lamaran (server: `get_stage_stats()`)
- **Compact**: kolom tinggi terbatas (scroll internal, halaman tidak scroll vertikal — di-scope lewat class `page-tracker` + `body:has()` biar tidak bocor ke halaman lain) + **search bar per kolom** (filter live: perusahaan/posisi/email)
- Kolom `stage` ikut diekspor di CSV riwayat (`/history/export`)

### Auth (opsional)
- `APP_PASSWORD` di `.env` → seluruh UI + unduhan CV butuh login (`/login`); cookie session HMAC `APP_SECRET` (acak per boot kalau kosong)
- `SMTP_SECURITY=ssl|starttls` per akun (default: SSL port 465, STARTTLS lainnya)

### Themes & UI
- **8 tema**: Light, Dark (default), Sakura, Bamboo, Cyberpunk, Mingyu, Ocean, Retro — dipilih via dropdown di sidebar (membuka **ke atas**)
- Pilihan tersimpan di `localStorage['lm-theme']`; script inline langsung setelah `<body>` mencegah flash; tema **tidak reset** saat pindah menu/path
- Toast kompak **fixed pojok kanan atas** (`#toast-container`), auto-dismiss 4 detik
- Sidebar mobile + hamburger animasi (Uiverse-style, scoped ke `.menu-toggle`)

## Database

### Tables
- `emails` — riwayat kirim (to_addr, company, position, extra, cv_file, status, error, smtp_account, created_at) + index status & created_at
- `rate_limit` — jendela rate limit per akun (`account_key` PK)
- `settings` — key-value; **dipakai**: `work_hours_enabled/work_start/work_end/work_weekdays_only`
- `batch_jobs` — status, counter (sent/failed/rate_limited), `filename`, `scheduled_at`, `resume_at`, `payload` (JSON, tahan restart)
- `templates` — template kustom (name PK, body, html_body, created_at)
- Migrasi otomatis kolom tambahan untuk DB lama (`filename`, `scheduled_at`, `resume_at`, `payload`)

## Routes

| Route | Description |
|---|---|
| `GET /` | Form kirim tunggal |
| `POST /preview` · `POST /send` | Preview / kirim tunggal (termasuk `template_name`) |
| `GET /api/companies` | Autocomplete nama perusahaan |
| `GET /batch` · `GET /batch/example-csv` | Halaman batch + contoh CSV |
| `POST /batch/preview` · `POST /batch/send` | Upload preview / mulai batch (form: `cache_key`, `scheduled_at`) |
| `POST /batch/{cancel,pause,resume,retry}/{id}` | Form aksi batch |
| `POST /api/batch/{cancel,pause,resume,retry}` | API aksi batch (JSON `{job_id}`) |
| `GET /api/batch-progress/{id}/stream` · `GET /api/batch-progress/{id}` | SSE live progress / snapshot |
| `GET /lamaran` · `POST /api/tracker/stage` | Kanban tracker lamaran + ubah tahap |
| `GET /history` · `GET /history/export` | Riwayat + **Export CSV** (tombol sudah ada di UI) |
| `POST /history/{delete}/{id}` · `/history/bulk-delete` · `/history/clear` · `/history/retry/{id}` | Aksi riwayat |
| `GET /dashboard` | Statistik |
| `GET /backup/download` | Download backup DB |
| `GET /cv` · `POST /cv/upload` · `POST /cv/delete` | Kelola CV (PDF) |
| `GET /templates-editor` · `POST /templates-editor/{save,delete,reset}` | Editor template |
| `POST /settings/work-hours` | Simpan pengaturan jam kerja |
| `GET /login` · `POST /login` · `POST /logout` | Auth (jika `APP_PASSWORD`) |
| `GET /_health` | Health check |

## Environment Variables (`.env`)

| Var | Default | Keterangan |
|---|---|---|
| `SMTP_HOST/PORT/USER/PASSWORD/FROM/FROM_NAME` | — | Akun utama (Gmail: App Password wajib) |
| `SMTP2_…` … `SMTP9_` | — | Akun failover tambahan |
| `SMTP_SECURITY` | auto | `ssl` / `starttls` per akun |
| `GMAIL_DAILY_LIMIT` | 500 | Limit harian per akun — **ditegakkan** |
| `RATE_LIMIT_PER_HOUR` | 999999 | Limit per jam per akun |
| `SEND_DELAY_MIN/MAX` | 30/90 | Delay acak antar email batch (detik) |
| `SPREAD_HOURS` | 6 | Sebar kiriman merata selama N jam (0 = pakai SEND_DELAY) |
| `CV_DIR` | `/app/cv` | Folder CV |
| `CV_PATH` | — | Path CV untuk CLI saja |
| `BACKUP_DIR` / `BACKUP_RETENTION_DAYS` | `/app/data/backups` / 7 | Backup DB otomatis harian |
| `SENDER_PHONE/LINKEDIN/GITHUB` | — | Kontak di tanda tangan email (WA otomatis 0→62) |
| `SMTP_FROM_NAME` | Nama Anda | Nama pengirim |
| `APP_PASSWORD` / `APP_SECRET` | kosong / acak | Proteksi login UI |
| `TZ` (docker-compose) | Asia/Jakarta | Basis "hari" limit & jam kerja |

## CV Files

- Folder `cv/` (mounted volume), wajib PDF; auto-detect & daftar di UI
- Anti path-traversal: `_sanitize_filename()` + cek `path.parent.resolve() == CV_DIR`

## Dev Workflow

```bash
python3 -m py_compile app.py email_service.py database.py   # cek sintaks
docker compose up --build -d                                # rebuild + jalan
curl -s http://localhost:8086/_health                       # health check

# tes cepat render desain di dalam container:
docker exec lamaran-mailer-mailer-1 python -c "
import sys; sys.path.insert(0,'/app'); import email_service as es
v = es.build_variants('PT X','IT Support / DevOps')
print(es.render_body('PT X','IT Support / DevOps','', 'dark', variants=v)[:200])"
```

## Gotchas (penting, jangan diulang)

- **`render_body` butuh 0 brace `{`/`}` di template** — semua desain HTML memakai inline style (bukan `<style>`), karena dirender via `str.format()`. Satu-satunya pengecualian: blok `<style>` media query di desain bawaan **wajib kurung kurawal ganda** (`{{`/`}}` di source, jadi `{`/`}` di hasil akhir) — jangan pernah tulis brace polos
- **Desain `dark` wajib meta `color-scheme: dark`** — tanpa itu Gmail/Apple Mail auto-invert dan teks putih jadi tak terbaca
- **Tombol kontak = inline-block fluid satu `<td>`** (bukan `<td>` per tombol) — itu yang bikin wrap rapi di HP; kalau diubah balik ke `<td>` terpisah, baris tombol bakal overflow di 375px
- **Tema**: jangan tulis `localStorage.getItem('lm-theme') || 'dark'` — string kosong itu tema Light yang VALID; dan jangan masukkan `''` ke array THEMES (`classList.remove('')` melempar SyntaxError)
- **`wa.me` harus format internasional** — nomor diawali `0` otomatis diganti `62`
- **Jangan timpa status batch** — setelah `pause/cancel`, jangan `update_batch_job(status='done')` (bug lama: cancel malah jadi done)
- CSS tema: hanya `static/pl-komatsu-ui-template.css` yang dipakai (diload via `/assets/` di `base.html`)
- `.env`, `data/`, `cv/*.pdf` di-gitignore; jangan commit App Password

---

## Catatan Sesi Terakhir

> Diperbarui: **4 September 2026**. Ini "memory" agar kerja bisa dilanjutkan besok tanpa kehilangan konteks.

### Status saat ini
- Container **berjalan** di `http://localhost:8086` (sudah rebuilt)
- `SMTP_` 1 akun Gmail pribadi (App Password, FROM_NAME = nama asli)
- Kontak terpasang di `.env` (nilai asli TIDAK disimpan di repo — lihat `.env`): WA otomatis format internasional, LinkedIn, GitHub
- CV: file PDF di folder `cv/` (di-gitignore, tidak ikut ke repo); file baru `Rangga_CV_ID_Revisi-2.pdf` & `Rangga_CV_EN_Revisi-2.pdf`, `CV_PATH` di `.env` → `Rangga_CV_ID_Revisi-2.pdf`
- **Jam kerja OFF** (default) — pengaturan tersimpan di DB, `08:00–17:00`, Senin–Jumat
- Tema default: Dark (Light = `''` di localStorage)
- Git: sudah push ke `origin/main` (branch main sudah track upstream)

### Yang sudah dikerjakan di sesi terakhir
0. **Override Nama Perkenalan & Pengalaman di Kirim Tunggal** — form Kirim tunggal kini punya 2 field opsional `sender_name` & `experience`. Variabel `{experience}` baru (default `EXPERIENCE_DEFAULT`) menggantikan paragraf pengalaman hardcoded di `BODY_TEMPLATE` + 4 desain HTML. `sender_name` ikut mengganti paragraf pembuka `{opening}` (fix: variants dibangun dengan `sender_name` biar opening gak stale). Mengalir lewat `/preview`, `/api/preview-html`, `/send`, `_try_send_with_failover`, `build_email`; HTML di-`html.escape()`. Backward compatible: kosong → default; template kustom tanpa `{experience}` aman.
1. **Fix bug: plain text template kustom tidak terpakai** — `render_body`/`build_email`/rute `/preview` sebelumnya hardcode `"plain"` → cuma nyari template bernama `"plain"` (tak pernah ada) → selalu fallback ke `BODY_TEMPLATE`. Solusi: tambah param `plain: bool` di `render_body()`; `build_email()` & `/preview` kini render plain text via `render_body(..., template_name, plain=True)` → pakai `template_data["body"]` dari template kustom yang dipilih; fallback `BODY_TEMPLATE` kalau template kustom tak ada (backward compatible)
2. **Plain text auto-generate** — `app.py` tambah `_plain_to_html()` (baris baru → `<br>`/`</p>`, escape entity); `save_template()` jadikan field `html_body` opsional, auto-generate dari plain text kalau dikosongkan
3. **Simpla UI editor template** — `/templates-editor` jadi 1 kotak utama "Isi Email" (**plain text**), HTML di-<details> "Mode HTML" opsional, panduan 4 langkah, panel tombol variabel klik-sisip (label manusiawi + `data-insert` dalam `<template>`), form template baru sudah ada contoh isi
4. **Fix attachment PDF korup (base64)** — (sesi sebelumnya) pastikan nggak hilang dari konteks
5. (sebelumnya, sesi lalu) **Tracker Lamaran (Kanban)**, **4 desain email**, kontak asli `.env` + fix `wa.me/0822…` → `wa.me/62822…`, fitur Jam Kerja, perbaikan bug (cancel batch, preview batch, render_body variants, dll)

### Ide fitur yang belum dikerjakan (kandidat berikutnya)
- **Follow-up otomatis** — kirim susulan ke email yang belum dibalas setelah N hari (hook-nya sudah ada: kolom `stage` + `stage_updated_at`)
- **Kolom CSV opsional per baris** — `position` / `cv_file` / `extra` per baris (sekarang satu untuk semua)
- **Desain acak per email di batch** — anti-pola deteksi spam
- **Notifikasi Telegram** saat batch selesai/dijeda (perlu bot token + chat id)
- **Test SMTP dari UI** — cek koneksi & kuota semua akun
- **AI personalisasi** isi email per perusahaan (perlu API key)
- Sudah ada, jangan dibikin ulang: Export CSV riwayat (`/history/export`), proteksi login (`APP_PASSWORD`), anti-duplikat email, backup DB otomatis

### TODO kecil / cleanup
- (done) Duplikat `pl-komatsu-ui-template.css` di root proyek sudah dihapus
- (done) `.env.example` sudah sinkron (SENDER_GITHUB ditambahkan)
