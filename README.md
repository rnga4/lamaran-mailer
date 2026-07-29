# Lamaran Mailer

Tool kirim email lamaran kerja otomatis — ada **Web UI** dan **CLI**.

## Setup

1. Copy `.env.example` jadi `.env`, isi kredensial email pengirim:
   ```bash
   cp .env.example .env
   ```
   Untuk Gmail, `SMTP_PASSWORD` **wajib App Password** — generate di https://myaccount.google.com/apppasswords

2. Taruh file CV (PDF) di folder `cv/`

3. Build & jalankan:
   ```bash
   docker compose up --build
   ```
   Buka **http://localhost:8000** di browser.

## Web UI

1. Isi form: Email HRD, Nama Perusahaan, Posisi, Kalimat Tambahan (opsional), pilih CV
2. Klik **Preview Email** — cek body sudah benar
3. Klik **Kirim Sekarang**

## CLI (opsional)

```bash
# Preview
docker compose run --rm mailer python send_application.py \
  --to hrd@contoh.com --company "PT Contoh Sejahtera" --dry-run

# Kirim
docker compose run --rm mailer python send_application.py \
  --to hrd@contoh.com --company "PT Contoh Sejahtera"
```

### Opsi CLI

- `--position "Nama Posisi"` — default "IT Support / DevOps"
- `--extra "kalimat tambahan"` — nempel di paragraf pengalaman
- `--cv /app/cv/NamaFile.pdf` — pakai CV lain
- `--dry-run` — preview tanpa kirim

## Struktur

```
├── app.py              # FastAPI web app
├── email_service.py    # Core email logic
├── send_application.py # CLI wrapper
├── templates/
│   └── index.html      # Web UI template
├── cv/                 # File CV (PDF)
├── .env                # Config (jangan commit!)
└── docker-compose.yml
```

## Catatan

- Jangan commit `.env` ke git — berisi App Password.
- Subject email selalu: `Lamaran Kerja – Nama Anda`
