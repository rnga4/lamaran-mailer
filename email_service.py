import os
import random
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

SUBJECT_TEMPLATES = [
    "Lamaran Kerja {position} – {company}",
    "Pengajuan Lamaran {position} di {company}",
    "Permohonan {position} – {company}",
    "Lamaran Pekerjaan: {position} – {company}",
    "{position} – Lamaran di {company}",
    "Peluang Karier – {position} ({company})",
    "Surat Lamaran {position} – {company}",
    "Pengajuan Dir.– {position} ({company})",
]

GREETING_VARIANTS = [
    "Yang terhormat Tim HR/Bagian Kepegawaian",
    "Kepada Yth. Bapak/Ibu HRD",
    "Yth. Bagian Rekrutmen",
    "Kepada Yth. Tim Rekrutmen",
]

OPENING_VARIANTS = [
    "Perkenalkan, saya {sender_name}. Melalui email ini saya bermaksud melamar untuk posisi {position} di {company}.",
    "Dengan hormat, saya {sender_name} mengajukan lamaran untuk posisi {position} yang saat ini tersedia di {company}.",
    "Saya {sender_name}, bermaksud mengajukan diri untuk mengisi posisi {position} di {company}.",
    "Melalui surat elektronik ini, saya {sender_name} menyampaikan minat dan lamaran saya untuk posisi {position} di {company}.",
]

CLOSING_VARIANTS = [
    "Demikian permohonan ini saya sampaikan, atas perhatian dan pertimbangannya saya ucapkan terima kasih.",
    "Besar harapan saya untuk dapat bergabung dan berkontribusi di {company}. Atas perhatian Bapak/Ibu, saya ucapkan terima kasih.",
    "Terima kasih atas waktu dan pertimbangan Bapak/Ibu. Saya sangat menantikan kesempatan untuk dapat bergabung dengan {company}.",
    "Demikian surat lamaran ini saya buat, semoga Bapak/Ibu berkenan mempertimbangkannya. Terima kasih.",
]

BODY_TEMPLATE = """{greeting}
{company},

{opening}

Saya berpengalaman sebagai IT Support / System Administrator dengan keahlian mengelola infrastruktur server Linux (Docker, migrasi ke Kubernetes/k3s), administrasi jaringan (MikroTik, VLAN, FreeRADIUS), reverse proxy multi-domain, serta pengembangan sistem/aplikasi internal (FastAPI, Node.js, Python).{extra}

Bersama email ini saya lampirkan CV dan dokumen pendukung lainnya untuk pertimbangan lebih lanjut. Saya sangat terbuka untuk dihubungi guna proses seleksi selanjutnya.

{closing}

Hormat saya,
{sender_name}
{sender_phone}
{sender_email}
{sender_linkedin}
"""

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px; }}
  p {{ margin: 0 0 12px 0; }}
  .signature {{ margin-top: 20px; color: #555; }}
  .divider {{ border-top: 2px solid #e0e0e0; margin: 20px 0; }}
  .contact-buttons {{ margin-top: 12px; }}
  .contact-buttons a {{
    display: inline-block;
    padding: 8px 18px;
    margin: 4px 6px 4px 0;
    border-radius: 8px;
    text-decoration: none;
    font-size: 13px;
    font-weight: 500;
    color: #fff;
  }}
  .btn-wa {{ background-color: #25D366; }}
  .btn-wa:hover {{ background-color: #1ebe57; }}
  .btn-email {{ background-color: #1a73e8; }}
  .btn-email:hover {{ background-color: #1557b0; }}
  .btn-linkedin {{ background-color: #0077b5; }}
  .btn-linkedin:hover {{ background-color: #005e93; }}
</style>
</head>
<body>
<p>{greeting}<br>{company},</p>

<p>{opening}</p>

<p>Saya berpengalaman sebagai IT Support / System Administrator dengan keahlian mengelola infrastruktur server Linux (Docker, migrasi ke Kubernetes/k3s), administrasi jaringan (MikroTik, VLAN, FreeRADIUS), reverse proxy multi-domain, serta pengembangan sistem/aplikasi internal (FastAPI, Node.js, Python).{extra}</p>

<p>Bersama email ini saya lampirkan CV dan dokumen pendukung lainnya untuk pertimbangan lebih lanjut. Saya sangat terbuka untuk dihubungi guna proses seleksi selanjutnya.</p>

<p>{closing}</p>

<div class="divider"></div>

<div class="signature">
  <p><strong>Hormat saya,</strong><br>{sender_name}</p>
  <div class="contact-buttons">
    <a href="{wa_link}" class="btn-wa">WhatsApp</a>
    <a href="mailto:{sender_email}" class="btn-email">Email</a>
    <a href="{linkedin_url}" class="btn-linkedin">LinkedIn</a>
  </div>
</div>
</body>
</html>"""

EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')


def render_subject(position: str, company: str) -> str:
    return random.choice(SUBJECT_TEMPLATES).format(position=position, company=company)


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))


def get_templates() -> dict[str, str]:
    return {
        "plain": BODY_TEMPLATE,
        "html": HTML_TEMPLATE,
    }


def get_template(name: str = "plain") -> str:
    templates = get_templates()
    return templates.get(name, BODY_TEMPLATE)


def set_template(name: str, content: str) -> None:
    from database import set_setting
    set_setting(f"template_{name}", content)


def render_body(
    company: str,
    position: str,
    extra: str = "",
    template_name: str = "plain",
) -> str:
    from database import get_template_by_name

    # Try templates table by name
    template_data = get_template_by_name(template_name)
    if template_data:
        tpl = template_data["html_body"] if template_name != "plain" else template_data["body"]
    else:
        from database import get_setting
        custom = get_setting(f"template_{template_name}")
        tpl = custom if custom else get_template(template_name)
    extra_text = f" {extra}" if extra else ""

    # Inject random greeting/opening/closing for default templates only
    is_default = tpl in (get_template("plain"), get_template("html"))
    extra_vars = {}
    if is_default:
        sender_name = os.environ.get("SMTP_FROM_NAME", "Nama Anda")
        sender_phone = os.environ.get("SENDER_PHONE", "08XX-XXXX-XXXX")
        sender_email = os.environ.get("SMTP_FROM", "email.anda@gmail.com")
        sender_linkedin = os.environ.get("SENDER_LINKEDIN", "linkedin.com/in/username")
        wa_link = "https://wa.me/" + re.sub(r"\D", "", sender_phone)
        linkedin_url = "https://" + sender_linkedin.lstrip("https://")
        extra_vars = {
            "greeting": random.choice(GREETING_VARIANTS),
            "opening": random.choice(OPENING_VARIANTS).format(
                sender_name=sender_name, position=position, company=company
            ),
            "closing": random.choice(CLOSING_VARIANTS).format(company=company),
            "sender_name": sender_name,
            "sender_phone": sender_phone,
            "sender_email": sender_email,
            "sender_linkedin": sender_linkedin,
            "wa_link": wa_link,
            "linkedin_url": linkedin_url,
        }
    try:
        return tpl.format(company=company, position=position, extra=extra_text, **extra_vars)
    except KeyError as e:
        if extra_vars:
            try:
                return tpl.format(company=company, position=position, extra=extra_text)
            except KeyError as e2:
                raise ValueError(
                    f"Template menggunakan variabel {e2} yang tidak dikenal. "
                    "Variabel yang tersedia: company, position, extra"
                )
        raise ValueError(
            f"Template menggunakan variabel {e} yang tidak dikenal. "
            "Variabel yang tersedia: company, position, extra" +
            (", greeting, opening, closing, sender_name, sender_phone, sender_email, sender_linkedin, wa_link, linkedin_url" if is_default else "")
        )


def build_email(
    to_addr: str,
    company: str,
    position: str,
    extra: str,
    from_addr: str,
    from_name: str,
    cv_path: str,
    template_name: str = "html",
    additional_attachments: Optional[list[str]] = None,
) -> tuple[EmailMessage, str, str]:
    plain_body = render_body(company, position, extra, "plain")
    html_body = render_body(company, position, extra, template_name)

    cv_file = Path(cv_path)
    if not cv_file.exists():
        raise FileNotFoundError(
            f"CV tidak ditemukan di {cv_file}. Taruh file PDF CV kamu di folder ./cv "
            "(di-mount ke /app/cv di container) atau set --cv / env CV_PATH ke path yang benar."
        )

    outer = EmailMessage()
    outer["Subject"] = render_subject(position, company)
    outer["From"] = f"{from_name} <{from_addr}>"
    outer["Reply-To"] = from_addr
    outer["To"] = to_addr
    outer.set_content(plain_body)
    outer.add_alternative(html_body, subtype="html")

    outer.add_attachment(
        cv_file.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=cv_file.name,
    )

    if additional_attachments:
        for path_str in additional_attachments:
            fp = Path(path_str)
            if fp.exists():
                outer.add_attachment(
                    fp.read_bytes(),
                    maintype="application",
                    subtype="pdf",
                    filename=fp.name,
                )

    return outer, plain_body, html_body


def send_email(
    msg: EmailMessage,
    host: str,
    port: int,
    user: str,
    password: str,
    use_ssl: bool = True,
    timeout: int = 30,
) -> None:
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=timeout) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
