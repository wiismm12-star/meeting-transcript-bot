from __future__ import annotations

import mimetypes
import smtplib
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from transcript_bot.config import settings


class EmailDeliveryError(RuntimeError):
    """An email delivery error that is safe to show to Telegram users."""


def is_valid_email(value: str) -> bool:
    _, address = parseaddr(value.strip())
    return address == value.strip() and "@" in address and "." in address.rsplit("@", 1)[-1]


def send_transcript_email(recipient: str, meeting_id: str, attachments: list[Path]) -> None:
    if not is_valid_email(recipient):
        raise EmailDeliveryError("Email 格式不正確，請重新輸入。")
    if not all((settings.smtp_host, settings.smtp_username, settings.smtp_password, settings.smtp_from)):
        raise EmailDeliveryError(
            "Email 寄送尚未設定。請在伺服器的 .env 設定 SMTP_HOST、SMTP_USERNAME、"
            "SMTP_PASSWORD 與 SMTP_FROM。"
        )

    message = EmailMessage()
    message["Subject"] = f"會議逐字稿 {meeting_id}"
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message.set_content("附件為會議逐字稿檔案。")

    for attachment in attachments:
        content_type, _ = mimetypes.guess_type(attachment.name)
        maintype, subtype = (content_type or "application/octet-stream").split("/", 1)
        message.add_attachment(attachment.read_bytes(), maintype=maintype, subtype=subtype, filename=attachment.name)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as client:
            if settings.smtp_use_tls:
                client.starttls()
            client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError("Email 寄送失敗，請確認 SMTP 設定後再試。") from exc
