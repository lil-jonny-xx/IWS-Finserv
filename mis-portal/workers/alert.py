"""
Alert module — sends email notifications on cron worker failures via Gmail API.

Requires the central Gmail token to have gmail.send scope.
Re-run oauth_setup.py if the token only has gmail.readonly.
"""
import base64
import email.mime.text
import logging
import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
TOKEN_FILE = Path(__file__).parent / "gmail_token_central.json"
ALERT_TO   = os.environ.get("ALERT_EMAIL", "***REMOVED***")


def _get_service():
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_failure_alert(worker_name: str, exit_code: int, output_tail: str = "") -> bool:
    """
    Send an email alert for a failed cron worker.
    Returns True on success, False on send error (never raises).
    """
    subject = f"[IWS MIS] Cron Failure: {worker_name}"
    lines = [
        f"Worker  : {worker_name}",
        f"Exit    : {exit_code}",
        "",
    ]
    if output_tail.strip():
        lines += ["── Last output ──────────────────────────", output_tail.strip()]

    body = "\n".join(lines)
    msg = email.mime.text.MIMEText(body)
    msg["to"]      = ALERT_TO
    msg["subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        service = _get_service()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info(f"Alert sent: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send alert for {worker_name}: {e}")
        return False
