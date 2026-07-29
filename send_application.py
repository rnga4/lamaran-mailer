import argparse
import os
import sys

from email_service import build_email, send_email


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Kirim email lamaran kerja otomatis dari template (subject fixed, body diisi nama PT)."
    )
    parser.add_argument("--to", required=True, help="Email HRD tujuan")
    parser.add_argument("--company", required=True, help="Nama perusahaan/instansi, contoh: 'PT Contoh Sejahtera'")
    parser.add_argument("--position", default="IT Support / DevOps", help="Posisi yang dilamar (default: IT Support / DevOps)")
    parser.add_argument("--extra", default="", help="Kalimat tambahan opsional, ditempel di paragraf pengalaman")
    parser.add_argument(
        "--cv",
        default=os.environ.get("CV_PATH", "/app/cv/your_cv.pdf"),
        help="Path file CV (PDF). Default ambil dari env CV_PATH atau /app/cv/your_cv.pdf",
    )
    parser.add_argument("--dry-run", action="store_true", help="Cuma preview subject/body/attachment, tidak dikirim")
    args = parser.parse_args()

    from_addr = os.environ.get("SMTP_FROM")
    from_name = os.environ.get("SMTP_FROM_NAME", "Nama Anda")
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")

    if not args.dry_run and not all([from_addr, host, user, password]):
        sys.exit(
            "Env var SMTP_FROM, SMTP_HOST, SMTP_USER, SMTP_PASSWORD wajib diisi "
            "(kecuali pakai --dry-run). Lihat .env.example."
        )

    msg, body, _ = build_email(
        args.to, args.company, args.position, args.extra,
        from_addr or "preview@local", from_name, args.cv,
    )

    print("=== SUBJECT ===")
    print(msg["Subject"])
    print("\n=== BODY ===")
    print(body)
    print(f"\n=== ATTACHMENT === {args.cv}")

    if args.dry_run:
        print("\n[DRY RUN] Email tidak dikirim.")
        return

    send_email(msg, host, port, user, password)
    print(f"\nEmail terkirim ke {args.to}")


if __name__ == "__main__":
    main()
