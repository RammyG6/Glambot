"""Send the link-only delivery email (Drive link + inline QR code) via SMTP.

The recipient, subject and body are whatever the operator confirmed on the
review screen at approval time — this module just resolves placeholders and
sends; it does not decide what the message says.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from .qr import make_qr_png_bytes

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "email_default.txt"
QR_CID = "qr-code"


class EmailError(RuntimeError):
    pass


def load_default_template(path: Path = DEFAULT_TEMPLATE_PATH) -> tuple[str, str]:
    """Read the default subject/body template. First line is `Subject: ...`,
    the rest (after the following blank line) is the body."""
    text = path.read_text()
    lines = text.splitlines()
    if not lines or not lines[0].lower().startswith("subject:"):
        raise EmailError(f"{path}: first line must start with 'Subject:'")
    subject = lines[0].split(":", 1)[1].strip()
    body = "\n".join(lines[1:]).lstrip("\n")
    return subject, body


def resolve_placeholders(text: str, *, link: str, project: str, filename: str) -> str:
    return (
        text.replace("{link}", link)
        .replace("{project}", project)
        .replace("{filename}", filename)
    )


def send_delivery_email(*, recipient: str, subject: str, body: str, link: str) -> None:
    """Send a link-only HTML email with the Drive link and an inline QR code.
    `subject`/`body` are expected to already have placeholders resolved."""
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    from_addr = os.environ.get("FROM_ADDR", user)

    if not all([host, user, password, from_addr]):
        raise EmailError(
            "SMTP is not configured — set SMTP_HOST, SMTP_USER, SMTP_PASS, "
            "FROM_ADDR in your .env file."
        )

    qr_bytes = make_qr_png_bytes(link)

    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = recipient

    html_body = body.replace("\n", "<br>") + (
        f'<br><br><img src="cid:{QR_CID}" alt="QR code" width="200" height="200">'
    )
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body, "plain"))
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    qr_image = MIMEImage(qr_bytes, _subtype="png")
    qr_image.add_header("Content-ID", f"<{QR_CID}>")
    qr_image.add_header("Content-Disposition", "inline", filename="qr.png")
    msg.attach(qr_image)

    logger.info("Sending delivery email to %s", recipient)
    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(from_addr, [recipient], msg.as_string())
