"""Sends report cards by email over SMTP (Brevo's free SMTP, or Gmail for local use)."""
import os
import smtplib
from email.message import EmailMessage

import markdown

TEMPLATE = """<div style="font-family:Georgia,serif;max-width:640px;margin:auto;background:#1B1A17;color:#ECE8E1;border-radius:14px;overflow:hidden">
<div style="padding:22px 26px;border-bottom:1px solid #35332E">
<div style="font-size:20px">Meeting Agent <span style="color:#E08A5A">&#9679;</span></div>
<div style="color:#9A958C;font:13px Arial,sans-serif;margin-top:4px">{subject}</div></div>
<div style="padding:22px 26px;line-height:1.65;font-size:16px">{body}</div>
<div style="padding:14px 26px;color:#9A958C;font:12px Arial,sans-serif;border-top:1px solid #35332E">{footer}</div></div>"""


def configured():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"))


def send_report(to, subject, report_md, transcript_json=None, link=None):
    if not configured() or not to:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("MAIL_FROM") or os.environ["SMTP_USER"]
    msg["To"] = ", ".join(to)
    msg.set_content(report_md)
    body = markdown.markdown(report_md, extensions=["nl2br", "sane_lists"])
    body = body.replace("<h2>", '<h2 style="color:#E08A5A;font-weight:normal;font-size:20px;margin:22px 0 6px">')
    footer = f'<a style="color:#ECE8E1" href="{link}">Open in Meeting Agent</a>' if link else "Sent by Meeting Agent"
    msg.add_alternative(TEMPLATE.format(subject=subject, body=body, footer=footer), subtype="html")
    if transcript_json:
        msg.add_attachment(transcript_json.encode(), maintype="application", subtype="json",
                           filename="transcript.json")
    port = int(os.environ.get("SMTP_PORT", "587"))
    host = os.environ["SMTP_HOST"]
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
    return True
