# app/services/gmail_sender.py
"""
Gmail Email Notification Service

Sends automated backup notifications via SMTP + App Password.
Sender: configure with a private operator account in your local `.env`.
"""

import os
import smtplib
import logging
from email.mime.text import MIMEText
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465


async def send_backup_notification(
    recipients: List[str],
    success: bool,
    backup_time: str,
    file_size_mb: float,
    duration_seconds: int,
    filename: str,
    drive_url: Optional[str] = None,
    error_message: Optional[str] = None
) -> bool:
    """
    Send backup completion notification via SMTP

    Args:
        recipients: List of email addresses
        success: Whether backup succeeded
        backup_time: ISO 8601 timestamp
        file_size_mb: Archive size in MB
        duration_seconds: Time taken
        filename: Backup archive filename
        drive_url: Google Drive link (if uploaded)
        error_message: Error details (if failed)

    Returns:
        True if email sent successfully, False otherwise
    """
    sender = os.getenv("BACKUP_EMAIL")
    password = os.getenv("BACKUP_EMAIL_APP_PASSWORD")

    if not sender or not password:
        logger.error("[GmailSender] BACKUP_EMAIL or BACKUP_EMAIL_APP_PASSWORD not set in .env")
        return False

    try:
        # Build email body
        status_emoji = "✅" if success else "❌"
        status_text = "성공" if success else "실패"

        # Format duration
        if duration_seconds < 60:
            duration_str = f"{duration_seconds}초"
        else:
            minutes = duration_seconds // 60
            seconds = duration_seconds % 60
            duration_str = f"{minutes}분 {seconds}초"

        # Parse timestamp for readable date
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(backup_time.replace('Z', '+00:00'))
            readable_time = dt.strftime("%Y년 %m월 %d일 %H:%M:%S")
            date_str = dt.strftime("%Y-%m-%d")
        except Exception:
            readable_time = backup_time
            date_str = backup_time[:10]

        # Build message body
        body_lines = [
            "Minecraft 서버 자동 백업이 완료되었습니다.",
            "",
            f"상태: {status_emoji} {status_text}",
            f"백업 시간: {readable_time}",
            f"파일명: {filename}",
            f"파일 크기: {file_size_mb:,.1f} MB",
            f"소요 시간: {duration_str}",
        ]

        if success and drive_url:
            body_lines.append(f"Drive URL: {drive_url}")

        if not success and error_message:
            body_lines.append("")
            body_lines.append(f"오류 메시지: {error_message}")

        body = "\n".join(body_lines)

        # Create MIME message
        message = MIMEText(body, "plain", "utf-8")
        message["To"] = ", ".join(recipients)
        message["From"] = sender
        message["Subject"] = f"[CORA] Minecraft Backup {status_text} - {date_str}"

        # Send via SMTP
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(sender, password)
            server.sendmail(sender, recipients, message.as_string())

        logger.info("[GmailSender] Backup notification sent to %d recipients", len(recipients))
        return True

    except Exception as e:
        logger.error("[GmailSender] Failed to send backup notification: %s", e)
        return False
