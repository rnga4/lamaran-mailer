# Lamaran Mailer

Automated job application email sender with Web UI and CLI.

## Architecture

- **FastAPI** web app (`app.py`) — serves web UI, API endpoints, handles single/batch sending
- **email_service.py** — core email logic: build emails, render templates, send via SMTP
- **database.py** — SQLite (stored in `/app/data/mailer.db`), handles logging, rate limiting, templates, batch jobs
- **send_application.py** — CLI wrapper for single sends
- **Docker** — runs on port 8086 (host) → 8000 (container)

## Key Concepts

### SMTP Multi-Account Failover
- Configure up to 9 accounts via `SMTP_` / `SMTP2_` ... `SMTP9_` env vars
- Auto-failover: if one account hits rate limit or fails, tries the next
- Round-robin load balancing across accounts

### Email Templates
- Default templates use random greeting/opening/closing variants (from `email_service.py`)
- Custom templates stored in DB `templates` table, editable via Web UI at `/templates-editor`
- Supports `{company}`, `{position}`, `{extra}` variables (plus `{greeting}`, `{opening}`, `{closing}` for default templates)

### Batch Sending
- Upload CSV with `email` and `company` columns
- Streaming progress via SSE at `/api/batch-progress/{job_id}/stream`
- Random delays to avoid detection patterns
- `SPREAD_HOURS` distributes emails evenly over N hours

### Rate Limiting
- Per-account sliding window (1 hour) tracked in SQLite
- Gmail daily limit configurable via `GMAIL_DAILY_LIMIT`
- Global `RATE_LIMIT_PER_HOUR` (999999 = practically unlimited)

## Database Tables

- `emails` — sent email history
- `rate_limit` — per-account rate limit windows
- `settings` — key-value store (legacy, replaced by templates table)
- `batch_jobs` — batch send progress tracking
- `templates` — custom email templates

## Routes

| Route | Description |
|---|---|
| `GET /` | Single send form |
| `POST /preview` | Preview email |
| `POST /send` | Send single email |
| `GET /batch` | Batch send page |
| `POST /batch/preview` | Upload & preview CSV |
| `POST /batch/send` | Start batch send |
| `GET /history` | Email history with search/filter |
| `GET /dashboard` | Stats dashboard |
| `GET /cv` | Upload/manage CV files |
| `GET /templates-editor` | Manage email templates |
| `GET /_health` | Health check |

## CV Files

- Stored in `cv/` directory (mounted volume)
- Must be PDF format
- Auto-detected and listed in UI

## Deployment

```bash
docker compose up --build
```

Runs on `http://localhost:8086`

## Important Notes

- `.env` is gitignored — contains SMTP passwords (use App Passwords for Gmail)
- `data/` is gitignored — SQLite DB with email history
- Default position: "IT Support / DevOps"
- Default sender name: set via `SMTP_FROM_NAME` env var (lihat `.env.example`)
