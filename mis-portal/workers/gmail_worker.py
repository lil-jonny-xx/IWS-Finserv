#!/usr/bin/env python3
"""
Gmail Worker — IWS MIS Portal
OAuth2 Gmail access: poll for CAMS CAS emails, download PDF attachments.
"""
import os
import re
import base64
import logging
import threading
import time
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path(__file__).parent / "gmail_credentials.json"

# CAMS sends CAS from this address
CAMS_SENDER = "donotreply@camsonline.com"
CAMS_SUBJECT_KEYWORD = "Consolidated Account Statement"

_token_refresh_lock = threading.Lock()


def _get_service(token_file: str):
    """Build authenticated Gmail API service from token file."""
    creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if creds.expired and creds.refresh_token:
        with _token_refresh_lock:
            # Re-read: another thread may have already refreshed
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                Path(token_file).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _search_messages(service, query: str) -> list:
    result = service.users().messages().list(userId="me", q=query).execute()
    return result.get("messages", [])


def _get_message(service, msg_id: str) -> dict:
    return service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()


def _download_attachment(service, msg_id: str, save_dir: str) -> str | None:
    """Download first PDF attachment from message. Returns saved path or None."""
    msg = _get_message(service, msg_id)
    parts = msg.get("payload", {}).get("parts", [])

    for part in parts:
        filename = part.get("filename", "")
        mime = part.get("mimeType", "")
        if not filename.lower().endswith(".pdf") and "pdf" not in mime:
            continue

        attachment_id = part.get("body", {}).get("attachmentId")
        if not attachment_id:
            data = part.get("body", {}).get("data", "")
        else:
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=attachment_id
            ).execute()
            data = att.get("data", "")

        if not data:
            continue

        pdf_bytes = base64.urlsafe_b64decode(data)
        # Strip path components and non-safe characters from sender-controlled filename.
        # os.path.join does NOT prevent traversal — an absolute or ../.. filename
        # would write outside save_dir.
        safe_name = re.sub(r"[^\w\-.]", "_", os.path.basename(filename)) if filename else ""
        if not safe_name.lower().endswith(".pdf"):
            safe_name = f"cas_{msg_id}.pdf"
        save_path = os.path.join(save_dir, safe_name)
        # Confirm confinement after join (defense-in-depth).
        if not os.path.realpath(save_path).startswith(os.path.realpath(save_dir) + os.sep):
            logger.error(f"Attachment filename rejected (traversal): {filename!r}")
            continue
        Path(save_path).write_bytes(pdf_bytes)
        logger.info(f"Downloaded attachment: {save_path} ({len(pdf_bytes)} bytes)")
        return save_path

    return None


def collect_new_cas_pdfs(
    token_file: str,
    save_dir: str,
    after_ts: int,
    exclude_ids: set,
) -> list[tuple[str, str]]:
    """
    One-shot poll: find all CAS emails received after `after_ts` (Unix timestamp)
    that are not in `exclude_ids`. Returns [(msg_id, pdf_path), ...].
    Uses subject + sender filter — no per-entity to: filter needed.
    """
    service = _get_service(token_file)
    query = (
        f"from:{CAMS_SENDER} "
        f"subject:\"{CAMS_SUBJECT_KEYWORD}\" "
        f"has:attachment "
        f"after:{after_ts}"
    )
    messages = _search_messages(service, query)
    results = []
    for msg in messages:
        msg_id = msg["id"]
        if msg_id in exclude_ids:
            continue
        path = _download_attachment(service, msg_id, save_dir)
        if path:
            results.append((msg_id, path))
    return results


def wait_for_cas_email(
    token_file: str,
    save_dir: str,
    to_address: str,
    poll_interval: int = 30,
    timeout_minutes: int = 15,
) -> str | None:
    """
    Poll Gmail inbox for a new CAS email addressed to `to_address` (the alias).
    Multiple aliases may share one inbox — the to: filter isolates the right PDF.
    Returns local PDF path when found, None on timeout.
    """
    service = _get_service(token_file)
    deadline = time.time() + timeout_minutes * 60
    query = (
        f"from:{CAMS_SENDER} "
        f"to:{to_address} "
        f"subject:\"{CAMS_SUBJECT_KEYWORD}\" "
        "has:attachment "
        "newer_than:2h"
    )
    # Note: is:unread intentionally omitted — emails opened in Gmail UI become read
    # and would be missed. newer_than:2h is sufficient to avoid stale emails.

    logger.info(f"Polling Gmail for CAS email to={to_address} (timeout: {timeout_minutes}m)...")

    while time.time() < deadline:
        messages = _search_messages(service, query)
        if messages:
            msg_id = messages[0]["id"]
            logger.info(f"CAS email found for {to_address}: msg_id={msg_id}")
            path = _download_attachment(service, msg_id, save_dir)
            if path:
                return path
            logger.warning("Email found but no PDF attachment — retrying")

        remaining = int((deadline - time.time()) / 60)
        logger.info(f"[{to_address}] No CAS email yet. Retry in {poll_interval}s (~{remaining}m left)")
        time.sleep(poll_interval)

    logger.error(f"Timed out waiting for CAS email to {to_address}")
    return None
