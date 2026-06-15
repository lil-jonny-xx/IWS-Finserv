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


def send_alert(subject: str, body: str) -> bool:
    """
    Send a generic notification email via the central Gmail token.
    Prefixes the subject with "[IWS MIS]" if not already present.
    Returns True on success, False on send error (never raises).
    """
    if not subject.startswith("[IWS MIS]"):
        subject = f"[IWS MIS] {subject}"

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
        logger.error(f"Failed to send alert '{subject}': {e}")
        return False


def send_failure_alert(worker_name: str, exit_code: int, output_tail: str = "") -> bool:
    """
    Send an email alert for a failed cron worker.
    Returns True on success, False on send error (never raises).
    """
    lines = [
        f"Worker  : {worker_name}",
        f"Exit    : {exit_code}",
        "",
    ]
    if output_tail.strip():
        lines += ["── Last output ──────────────────────────", output_tail.strip()]

    return send_alert(f"Cron Failure: {worker_name}", "\n".join(lines))
