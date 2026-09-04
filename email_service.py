import html
import os
import random
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from email.mime.base import MIMEBase
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

{experience}.{extra}

Bersama email ini saya lampirkan CV dan dokumen pendukung lainnya untuk pertimbangan lebih lanjut. Saya sangat terbuka untuk dihubungi guna proses seleksi selanjutnya.

{closing}

Hormat saya,
{sender_name}
{sender_phone}
{sender_email}
{sender_linkedin}
{sender_github}
"""

# Paragraf "Pengalaman & Keahlian" default — dipakai bila pengguna tidak
# mengisinya di form. Bisa di-custom per kirim via variabel {experience}.
EXPERIENCE_DEFAULT = (
    "Saya berpengalaman sebagai IT Support / System Administrator dengan keahlian "
    "mengelola infrastruktur server Linux (Docker, migrasi ke Kubernetes/k3s), "
    "administrasi jaringan (MikroTik, VLAN, FreeRADIUS), reverse proxy multi-domain, "
    "serta pengembangan sistem/aplikasi internal (FastAPI, Node.js, Python)"
)

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="id">
<body style="margin:0;padding:0;background-color:#eef1f7;">
<style>
  @media (max-width: 480px) {{
    .email-card td {{ padding-left:20px !important; padding-right:20px !important; }}
    .btn-contact {{ display:block !important; margin:0 auto 10px auto !important; }}
  }}
</style>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#eef1f7;padding:32px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="email-card" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:16px;border:1px solid #e4e8f1;box-shadow:0 10px 30px rgba(30,40,90,0.08);overflow:hidden;">
          <tr>
            <td style="height:6px;font-size:0;line-height:0;background-color:#4f46e5;background:linear-gradient(90deg,#4f46e5,#8b5cf6);">&nbsp;</td>
          </tr>
          <tr>
            <td style="padding:32px 40px 4px 40px;">
              <div style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:11px;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;color:#9aa1b5;margin-bottom:10px;">Surat Lamaran Kerja</div>
              <div style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:26px;font-weight:800;color:#1b2134;line-height:1.25;">{position}</div>
              <div style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;font-weight:600;color:#4f46e5;margin-top:5px;">di {company}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:20px 40px 2px 40px;">
              <div style="border-top:1px solid #eceff6;font-size:0;line-height:0;">&nbsp;</div>
            </td>
          </tr>
          <tr>
            <td style="padding:6px 40px 2px 40px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.8;color:#3d4354;">
              <p style="margin:0 0 14px 0;">{greeting},</p>
              <p style="margin:0 0 14px 0;">{opening}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 40px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f6fd;border-left:4px solid #4f46e5;border-radius:10px;">
                <tr>
                  <td style="padding:16px 20px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:13.5px;line-height:1.75;color:#414a5e;">
                    <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#4f46e5;margin-bottom:6px;">Pengalaman &amp; Keahlian</div>
                    {experience}.{extra}
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 40px 2px 40px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.8;color:#3d4354;">
              <p style="margin:0 0 14px 0;">Bersama email ini saya lampirkan CV dan dokumen pendukung untuk pertimbangan lebih lanjut. Saya sangat terbuka untuk dihubungi guna proses seleksi selanjutnya.</p>
              <p style="margin:0 0 14px 0;">{closing}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 40px 2px 40px;">
              <div style="border-top:1px solid #eceff6;font-size:0;line-height:0;margin-bottom:18px;">&nbsp;</div>
              <div style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;color:#3d4354;line-height:1.6;">
                <span style="color:#1b2134;font-weight:700;">Hormat saya,</span><br>
                <span style="font-size:17px;font-weight:800;color:#1b2134;">{sender_name}</span>
              </div>
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:16px 0 0 0;background-color:#eef1f7;border-radius:5em;padding:8px 10px;box-shadow:0 1px 4px rgba(0,0,0,0.08);"><tr><td style="font-size:0;text-align:center;"><a href="{wa_link}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#25D366;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">wa</span></a><a href="mailto:{sender_email}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#4f46e5;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">mail</span></a><a href="{linkedin_url}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#8b5cf6;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">RR</span></a><a href="{github_url}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#1b2134;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">gh</span></a></td></tr></table>
            </td>
          </tr>
          <tr>
            <td style="padding:4px 40px 26px 40px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:11.5px;color:#a0a7ba;line-height:1.6;">
              {sender_email} &middot; {sender_phone} &middot; {sender_linkedin}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')


def render_subject(position: str, company: str) -> str:
    return random.choice(SUBJECT_TEMPLATES).format(position=position, company=company)


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))


HTML_TEMPLATE_MINIMAL = """\
<!DOCTYPE html>
<html lang="id">
<body style="margin:0;padding:0;background-color:#fafafa;">
<style>
  @media (max-width: 480px) {{
    .email-card td {{ padding-left:20px !important; padding-right:20px !important; }}
    .btn-contact {{ display:block !important; margin:0 auto 10px auto !important; }}
  }}
</style>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#fafafa;padding:48px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="email-card" style="max-width:560px;width:100%;background-color:#ffffff;border:1px solid #e8e8e8;border-radius:8px;">
          <tr>
            <td style="padding:44px 44px 6px 44px;">
              <div style="font-family:'Helvetica Neue',Arial,sans-serif;font-size:10px;font-weight:600;letter-spacing:3px;text-transform:uppercase;color:#9ca3af;margin-bottom:18px;">Lamaran Kerja</div>
              <div style="font-family:'Helvetica Neue',Arial,sans-serif;font-size:24px;font-weight:300;color:#111111;line-height:1.3;letter-spacing:-0.2px;">{position}</div>
              <div style="font-family:'Helvetica Neue',Arial,sans-serif;font-size:14px;font-weight:400;color:#111111;margin-top:6px;">{company}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 44px 0 44px;">
              <div style="border-top:1px solid #eeeeee;font-size:0;line-height:0;">&nbsp;</div>
            </td>
          </tr>
          <tr>
            <td style="padding:28px 44px 4px 44px;font-family:'Helvetica Neue',Arial,sans-serif;font-size:14px;line-height:1.9;color:#333333;">
              <p style="margin:0 0 16px 0;">{greeting},</p>
              <p style="margin:0 0 16px 0;">{opening}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 44px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="border-left:2px solid #111111;padding:2px 0 2px 18px;font-family:'Helvetica Neue',Arial,sans-serif;font-size:13.5px;line-height:1.85;color:#444444;">
                    {experience}.{extra}
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:12px 44px 4px 44px;font-family:'Helvetica Neue',Arial,sans-serif;font-size:14px;line-height:1.9;color:#333333;">
              <p style="margin:0 0 16px 0;">Bersama email ini saya lampirkan CV untuk pertimbangan lebih lanjut. Saya sangat terbuka untuk dihubungi guna proses seleksi selanjutnya.</p>
              <p style="margin:0 0 16px 0;">{closing}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:12px 44px 4px 44px;">
              <div style="border-top:1px solid #eeeeee;font-size:0;line-height:0;margin-bottom:24px;">&nbsp;</div>
              <div style="font-family:'Helvetica Neue',Arial,sans-serif;font-size:15px;color:#111111;">
                <span style="color:#9ca3af;font-size:12px;">Hormat saya,</span><br>
                <span style="font-size:17px;font-weight:600;color:#111111;">{sender_name}</span>
              </div>
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:16px 0 0 0;background-color:#f2f2f2;border-radius:5em;padding:8px 10px;box-shadow:0 1px 4px rgba(0,0,0,0.08);"><tr><td style="font-size:0;text-align:center;"><a href="{wa_link}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#25D366;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">wa</span></a><a href="mailto:{sender_email}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#333333;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">mail</span></a><a href="{linkedin_url}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#0a66c2;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">RR</span></a><a href="{github_url}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#24292e;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">gh</span></a></td></tr></table>
            </td>
          </tr>
          <tr>
            <td align="center" style="padding:20px 44px 36px 44px;">
              <div style="font-family:'Helvetica Neue',Arial,sans-serif;font-size:10.5px;color:#b0b3b8;line-height:1.6;text-align:center;border-top:1px solid #f2f2f2;padding-top:16px;">
                {sender_email} &middot; {sender_phone} &middot; {sender_linkedin}
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

HTML_TEMPLATE_DARK = """\
<!DOCTYPE html>
<html lang="id">
<head>
<meta name="color-scheme" content="dark">
<meta name="supported-color-schemes" content="dark">
</head>
<body style="margin:0;padding:0;background-color:#0f1222;color-scheme:dark;">
<style>
  @media (max-width: 480px) {{
    .email-card td {{ padding-left:20px !important; padding-right:20px !important; }}
    .btn-contact {{ display:block !important; margin:0 auto 10px auto !important; }}
  }}
</style>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#0f1222;padding:40px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="email-card" style="max-width:600px;width:100%;background-color:#161a2e;border:1px solid #2a3050;border-radius:14px;overflow:hidden;">
          <tr>
            <td style="height:5px;font-size:0;line-height:0;background-color:#e8b64c;">&nbsp;</td>
          </tr>
          <tr>
            <td style="padding:34px 40px 2px 40px;">
              <div style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:11px;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;color:#e8b64c;margin-bottom:10px;">Surat Lamaran Kerja</div>
              <div style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:26px;font-weight:700;color:#ffffff;line-height:1.25;">{position}</div>
              <div style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;font-weight:600;color:#e8b64c;margin-top:5px;">di {company}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:20px 40px 2px 40px;">
              <div style="border-top:1px solid #2a3050;font-size:0;line-height:0;">&nbsp;</div>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 40px 2px 40px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.85;color:#c7cbe0;">
              <p style="margin:0 0 14px 0;">{greeting},</p>
              <p style="margin:0 0 14px 0;">{opening}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:12px 40px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#1c2138;border-left:4px solid #e8b64c;border-radius:10px;">
                <tr>
                  <td style="padding:16px 20px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:13.5px;line-height:1.75;color:#b9bfd9;">
                    <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#e8b64c;margin-bottom:6px;">Pengalaman &amp; Keahlian</div>
                    {experience}.{extra}
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 40px 2px 40px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.85;color:#c7cbe0;">
              <p style="margin:0 0 14px 0;">Bersama email ini saya lampirkan CV dan dokumen pendukung untuk pertimbangan lebih lanjut. Saya sangat terbuka untuk dihubungi guna proses seleksi selanjutnya.</p>
              <p style="margin:0 0 14px 0;">{closing}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:12px 40px 2px 40px;">
              <div style="border-top:1px solid #2a3050;font-size:0;line-height:0;margin-bottom:18px;">&nbsp;</div>
              <div style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;color:#c7cbe0;line-height:1.6;">
                <span style="color:#e8b64c;font-weight:700;">Hormat saya,</span><br>
                <span style="font-size:17px;font-weight:700;color:#ffffff;">{sender_name}</span>
              </div>
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:16px auto 0 auto;background-color:#161a2e;"><table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:16px 0 0 0;background-color:#1c2138;border-radius:5em;padding:8px 10px;box-shadow:0 1px 4px rgba(0,0,0,0.3);"><tr><td style="font-size:0;text-align:center;"><a href="{wa_link}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#25D366;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">wa</span></a><a href="mailto:{sender_email}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#4a7ccc;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">mail</span></a><a href="{linkedin_url}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#0a66c2;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">RR</span></a><a href="{github_url}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#e8b64c;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#0f1222;display:inline-block;width:26px;line-height:26px;text-align:center;">gh</span></a></td></tr></table>
            </td>
          </tr>
          <tr>
            <td style="padding:4px 40px 26px 40px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:11.5px;color:#6b7299;line-height:1.6;">
              {sender_email} &middot; {sender_phone} &middot; {sender_linkedin}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

HTML_TEMPLATE_EDITORIAL = """\
<!DOCTYPE html>
<html lang="id">
<body style="margin:0;padding:0;background-color:#f6f1e7;">
<style>
  @media (max-width: 480px) {{
    .email-card td {{ padding-left:20px !important; padding-right:20px !important; }}
    .btn-contact {{ display:block !important; margin:0 auto 10px auto !important; }}
  }}
</style>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f6f1e7;padding:48px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="email-card" style="max-width:580px;width:100%;background-color:#fffdf7;border:1px solid #e5dcc8;border-top:4px solid #8c2f39;border-radius:4px;">
          <tr>
            <td style="padding:36px 44px 2px 44px;border-bottom:1px solid #ece4d2;">
              <div style="font-family:Georgia,'Times New Roman',serif;font-size:10.5px;font-weight:700;letter-spacing:3.5px;text-transform:uppercase;color:#8c2f39;margin-bottom:12px;">Surat Lamaran Kerja</div>
              <div style="font-family:Georgia,'Times New Roman',serif;font-size:26px;font-weight:700;color:#222222;line-height:1.3;">{position}</div>
              <div style="font-family:Georgia,'Times New Roman',serif;font-size:15px;font-style:italic;color:#8c2f39;margin-top:4px;margin-bottom:18px;">di {company}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:26px 44px 2px 44px;font-family:Georgia,'Times New Roman',serif;font-size:15px;line-height:1.95;color:#3a3a3a;">
              <p style="margin:0 0 16px 0;">{greeting},</p>
              <p style="margin:0 0 16px 0;">{opening}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:10px 44px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#faf3e6;border-left:3px solid #8c2f39;">
                <tr>
                  <td style="padding:14px 20px;font-family:Georgia,'Times New Roman',serif;font-size:13.5px;font-style:italic;line-height:1.8;color:#5a4632;">
                    {experience}.{extra}
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:14px 44px 2px 44px;font-family:Georgia,'Times New Roman',serif;font-size:15px;line-height:1.95;color:#3a3a3a;">
              <p style="margin:0 0 16px 0;">Bersama email ini saya lampirkan CV dan dokumen pendukung untuk pertimbangan lebih lanjut. Saya sangat terbuka untuk dihubungi guna proses seleksi selanjutnya.</p>
              <p style="margin:0 0 16px 0;">{closing}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:14px 44px 2px 44px;">
              <div style="border-top:1px solid #ece4d2;font-size:0;line-height:0;margin-bottom:22px;">&nbsp;</div>
              <div style="font-family:Georgia,'Times New Roman',serif;font-size:15px;color:#3a3a3a;line-height:1.6;">
                <span style="color:#8c2f39;font-style:italic;font-size:13px;">Hormat saya,</span><br>
                <span style="font-size:18px;font-weight:700;color:#222222;">{sender_name}</span>
              </div>
              <table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center" style="margin:16px auto 0 auto;background-color:#faf3e6;"><table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:16px 0 0 0;background-color:#ede5d4;border-radius:5em;padding:8px 10px;box-shadow:0 1px 4px rgba(0,0,0,0.06);"><tr><td style="font-size:0;text-align:center;"><a href="{wa_link}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#25D366;"><span style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">wa</span></a><a href="mailto:{sender_email}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#8c2f39;"><span style="font-family:Georgia,'Times New Roman',serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">mail</span></a><a href="{linkedin_url}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#5a4632;"><span style="font-family:Georgia,'Times New Roman',serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">RR</span></a><a href="{github_url}" class="btn-contact" style="display:inline-block;width:26px;height:26px;border-radius:50%;margin:0 4px;text-decoration:none;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,0.12);line-height:26px;vertical-align:middle;background-color:#3a3a3a;"><span style="font-family:Georgia,'Times New Roman',serif;font-size:9px;font-weight:700;color:#ffffff;display:inline-block;width:26px;line-height:26px;text-align:center;">gh</span></a></td></tr></table>            </td>
          </tr>
          <tr>
            <td style="padding:6px 44px 30px 44px;font-family:Georgia,'Times New Roman',serif;font-size:11px;color:#b0a48c;line-height:1.6;">
              {sender_email} &middot; {sender_phone} &middot; {sender_linkedin}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

# Registri desain email yang bisa dipilih di UI (index & batch).
# id harus cocok dengan nama template bawaan di get_templates().
EMAIL_DESIGNS = [
    {
        "id": "html",
        "name": "Premium Klasik",
        "desc": "Kartu profesional, gradien indigo & tombol kontak",
    },
    {
        "id": "minimal",
        "name": "Minimal Modern",
        "desc": "Bersih, tipografi tegas, aksen halus",
    },
    {
        "id": "dark",
        "name": "Elegant Dark",
        "desc": "Tema gelap navy, aksen emas — menonjol di inbox",
    },
    {
        "id": "serif",
        "name": "Editorial Serif",
        "desc": "Gaya majalah: kertas krem, tipografi serif, burgundy",
    },
]


def get_templates() -> dict[str, str]:
    return {
        "plain": BODY_TEMPLATE,
        "html": HTML_TEMPLATE,
        "minimal": HTML_TEMPLATE_MINIMAL,
        "dark": HTML_TEMPLATE_DARK,
        "serif": HTML_TEMPLATE_EDITORIAL,
    }


def get_template(name: str = "plain") -> str:
    templates = get_templates()
    return templates.get(name, BODY_TEMPLATE)


def set_template(name: str, content: str) -> None:
    from database import set_setting
    set_setting(f"template_{name}", content)


def _build_default_variants(
    company: str, position: str, sender_name: Optional[str] = None
) -> dict[str, str]:
    """Satu set greeting/opening/closing acak + data pengirim.

    Dipakai sekali per email lalu dibagi ke bagian plain-text & HTML
    supaya kedua versi isinya konsisten (dan preview = email).
    """
    if not sender_name:
        sender_name = os.environ.get("SMTP_FROM_NAME", "Nama Anda")
    sender_phone = os.environ.get("SENDER_PHONE", "08XX-XXXX-XXXX")
    sender_email = os.environ.get("SMTP_FROM", "email.anda@gmail.com")
    sender_linkedin = os.environ.get("SENDER_LINKEDIN", "linkedin.com/in/username")
    sender_github = os.environ.get("SENDER_GITHUB", "github.com/username")
    phone_digits = re.sub(r"\D", "", sender_phone)
    # Nomor lokal Indonesia (0xxx) → format internasional (62xxx) agar link
    # wa.me benar, mis. 0822… → https://wa.me/6282…
    if phone_digits.startswith("0"):
        phone_digits = "62" + phone_digits[1:]
    wa_link = f"https://wa.me/{phone_digits}" if phone_digits else "#"
    linkedin_url = "https://sub-genome-antivirus-survivors.trycloudflare.com"
    github_url = ("https://" + sender_github.removeprefix("https://")) if sender_github else "#"
    return {
        "greeting": random.choice(GREETING_VARIANTS),
        "opening": random.choice(OPENING_VARIANTS).format(
            sender_name=sender_name, position=position, company=company
        ),
        "closing": random.choice(CLOSING_VARIANTS).format(company=company),
        "sender_name": sender_name,
        "sender_phone": sender_phone,
        "sender_email": sender_email,
        "sender_linkedin": sender_linkedin,
        "sender_github": sender_github,
        "wa_link": wa_link,
        "linkedin_url": linkedin_url,
        "github_url": github_url,
    }


def build_variants(
    company: str, position: str, sender_name: Optional[str] = None
) -> dict[str, str]:
    """Public helper: satu set variabel acak untuk preview/email."""
    return _build_default_variants(company, position, sender_name=sender_name)


def render_body(
    company: str,
    position: str,
    extra: str = "",
    template_name: str = "plain",
    variants: Optional[dict[str, str]] = None,
    plain: bool = False,
    experience: Optional[str] = None,
    sender_name: Optional[str] = None,
) -> str:
    from database import get_template_by_name

    # Try templates table by name
    template_data = get_template_by_name(template_name)
    if template_data:
        if plain:
            tpl = template_data["body"]
        else:
            tpl = template_data["html_body"] if template_name != "plain" else template_data["body"]
    elif plain:
        # Plain text tanpa template kustom → pakai default plain
        tpl = get_template("plain")
    else:
        from database import get_setting
        custom = get_setting(f"template_{template_name}")
        tpl = custom if custom else get_template(template_name)

    # 'extra' adalah teks biasa — escape di HTML agar tidak bisa menyelipkan
    # HTML/script ke email maupun merusak halaman preview.
    if extra:
        extra_text = f" {html.escape(extra)}" if (template_name != "plain" and not plain) else f" {extra}"
    else:
        extra_text = ""

    # Paragraf "Pengalaman & Keahlian": kalau diisi → custom, kalau kosong
    # → pakai default. Untuk versi HTML, escape agar angka/karakter aman.
    experience_text = experience.strip() if experience and experience.strip() else EXPERIENCE_DEFAULT
    if plain:
        experience_text = experience_text
    else:
        experience_text = html.escape(experience_text)

    # Variabel variant (greeting/opening/closing/sender_*) selalu disuntik untuk
    # SEMUA template — kwarg yang tidak dipakai oleh .format() otomatis diabaikan,
    # jadi aman untuk template kustom yang hanya memakai {company}/{position}/{extra}.
    if variants is None:
        variants = _build_default_variants(company, position, sender_name=sender_name)
    else:
        # Variants parsial (mis. dari pemanggil eksternal): lengkapi dengan default
        # supaya placeholder variant apa pun tetap tersubstitusi. Kunci yang
        # bentrok dengan argumen .format() (company/position/extra) dibuang agar
        # tidak memicu TypeError 'got multiple values'.
        merged = _build_default_variants(company, position, sender_name=sender_name)
        merged.update(variants)
        for k in ("company", "position", "extra"):
            merged.pop(k, None)
        variants = merged
    # Suntikkan variabel {experience} sebagai nilai akhir (default / custom).
    variants["experience"] = experience_text
    if sender_name:
        variants["sender_name"] = sender_name
    try:
        return tpl.format(company=company, position=position, extra=extra_text, **variants)
    except KeyError as e:
        raise ValueError(
            f"Template menggunakan variabel {e} yang tidak dikenal. "
            "Variabel yang tersedia: company, position, experience, extra, greeting, opening, "
            "closing, sender_name, sender_phone, sender_email, sender_linkedin, sender_github, wa_link, linkedin_url, github_url"
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
    experience: Optional[str] = None,
    sender_name: Optional[str] = None,
) -> tuple[MIMEMultipart, str, str]:
    variants = _build_default_variants(company, position, sender_name=sender_name)
    plain_body = render_body(company, position, extra, template_name, variants=variants, plain=True, experience=experience, sender_name=sender_name)
    html_body = render_body(company, position, extra, template_name, variants=variants, experience=experience, sender_name=sender_name)

    cv_file = Path(cv_path)
    if not cv_file.exists():
        raise FileNotFoundError(
            f"CV tidak ditemukan di {cv_file}. Taruh file PDF CV kamu di folder ./cv "
            "(di-mount ke /app/cv di container) atau set --cv / env CV_PATH ke path yang benar."
        )

    outer = MIMEMultipart("mixed")
    outer["Subject"] = render_subject(position, company)
    outer["From"] = f"{from_name} <{from_addr}>"
    outer["Reply-To"] = from_addr
    outer["To"] = to_addr

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(plain_body, "plain", "utf-8"))
    alternative.attach(MIMEText(html_body, "html", "utf-8"))
    outer.attach(alternative)

    pdf_part = MIMEBase("application", "pdf")
    pdf_part.set_payload(cv_file.read_bytes())
    encoders.encode_base64(pdf_part)
    pdf_part["Content-Disposition"] = f'attachment; filename="{cv_file.name}"'
    outer.attach(pdf_part)

    if additional_attachments:
        for path_str in additional_attachments:
            fp = Path(path_str)
            if fp.exists():
                att = MIMEBase("application", "pdf")
                att.set_payload(fp.read_bytes())
                encoders.encode_base64(att)
                att["Content-Disposition"] = f'attachment; filename="{fp.name}"'
                outer.attach(att)

    return outer, plain_body, html_body


def send_email(
    msg: MIMEMultipart,
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
