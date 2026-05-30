#!/usr/bin/env python3
"""
CAS Automation Worker — IWS MIS Portal
Orchestrates daily CAS fetch for all 6 entities via one central Gmail inbox.
All entity emails (or their aliases) forward CAMS CAS emails to ***REMOVED***.

Flow:
  1. Start a Gmail collector thread that polls for new CAS PDFs continuously
  2. Fire CAMS requests for all entities sequentially (staggered to avoid rate-limits),
     spawning a parser thread for each entity immediately after its trigger
  3. Each parser thread claims the first PDF in the shared queue that opens with
     its entity's password (fitz.authenticate), then parses + upserts to DB
  4. All parser threads run concurrently — wall time ≈ slowest email delivery
  5. After all threads complete, refresh NAVs once

Schedule: Daily at 3 AM IST (21:30 UTC previous day)
Cron:     30 21 * * * /var/www/.venv/bin/python /var/www/mis-portal/workers/cas_automation_worker.py
"""
import os
import sys
import time
import random
import secrets
import logging
import tempfile
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import fitz  # PyMuPDF — used for password matching only

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env")

import cams_trigger_worker
import gmail_worker
import cas_parser_worker
import amfi_nav_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/var/log/mis-portal-cas-auto.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

WORKERS_DIR          = Path(__file__).parent
EMAIL_TIMEOUT_MINUTES = 30


@dataclass
class EntityConfig:
    code: str
    email: str
    pan: str


def _entity_configs() -> list[EntityConfig]:
    codes = ["DHR", "ADR", "IWS", "HHR", "IWSFC", "SDR"]
    return [
        EntityConfig(
            code=code,
            email=os.environ[f"ENTITY_{code}_EMAIL"],
            pan=os.environ[f"ENTITY_{code}_PAN"],
        )
        for code in codes
    ]


def _random_pdf_password() -> str:
    return secrets.token_urlsafe(12)


def _pdf_matches_password(pdf_path: str, password: str) -> bool:
    """
    Returns True if `password` correctly unlocks the PDF.
    fitz.Document.authenticate() returns 0 on wrong password, non-zero on correct.
    """
    try:
        doc = fitz.open(pdf_path)
        result = doc.authenticate(password)
        doc.close()
        return result != 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Gmail collector thread
# ---------------------------------------------------------------------------

def _gmail_collector(
    token_file: str,
    tmp_dir: str,
    run_start_ts: int,
    pending: list,
    pending_lock: threading.Lock,
    seen_ids: set,
    stop_event: threading.Event,
    deadline: float,
):
    """
    Polls Gmail every 30s for new CAS PDFs (subject-based filter, no per-entity
    to: filter). Appends (msg_id, pdf_path) to `pending` as emails arrive.
    """
    logger.info("Gmail collector started")
    while not stop_event.is_set() and time.time() < deadline:
        try:
            with pending_lock:
                current_seen = set(seen_ids)

            new_pdfs = gmail_worker.collect_new_cas_pdfs(
                token_file=token_file,
                save_dir=tmp_dir,
                after_ts=run_start_ts,
                exclude_ids=current_seen,
            )

            for msg_id, pdf_path in new_pdfs:
                with pending_lock:
                    if msg_id not in seen_ids:
                        pending.append((msg_id, pdf_path))
                        seen_ids.add(msg_id)
                        logger.info(f"Queued CAS PDF: {Path(pdf_path).name} (msg={msg_id})")

        except Exception as e:
            logger.warning(f"Gmail collector error: {e}")

        stop_event.wait(timeout=30)

    logger.info("Gmail collector stopped")


# ---------------------------------------------------------------------------
# Per-entity parser thread
# ---------------------------------------------------------------------------

def _entity_worker(
    cfg: EntityConfig,
    pdf_password: str,
    pending: list,
    pending_lock: threading.Lock,
    deadline: float,
    results: dict,
):
    """
    Waits for a PDF in `pending` that matches this entity's password, then parses it.
    Password matching via fitz.authenticate() — each entity's PDF has a unique random
    password, so only the right PDF will authenticate successfully.
    """
    logger.info(f"[{cfg.code}] Waiting for matching PDF...")
    claimed_path = None

    while time.time() < deadline and claimed_path is None:
        # Take a snapshot of pending outside the lock to avoid holding it during I/O
        with pending_lock:
            snapshot = list(pending)

        for item in snapshot:
            msg_id, pdf_path = item
            if _pdf_matches_password(pdf_path, pdf_password):
                # Atomically claim it
                with pending_lock:
                    if item in pending:
                        pending.remove(item)
                        claimed_path = pdf_path
                        logger.info(f"[{cfg.code}] Claimed PDF: {Path(pdf_path).name}")
                        break
        else:
            time.sleep(15)

    if not claimed_path:
        logger.error(f"[{cfg.code}] Timed out — no matching PDF arrived within {EMAIL_TIMEOUT_MINUTES}m")
        results[cfg.code] = False
        return

    try:
        cas_parser_worker.run(claimed_path, pdf_password)
        logger.info(f"[{cfg.code}] Parsed and upserted successfully")
        results[cfg.code] = True
    except SystemExit:
        logger.error(f"[{cfg.code}] Parser exited with error")
        results[cfg.code] = False
    finally:
        try:
            os.remove(claimed_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logger.info(f"╔══ CAS Automation starting — {date.today()} ══╗")

    try:
        configs = _entity_configs()
    except KeyError as e:
        logger.error(f"Missing env var: {e}. Check .env file.")
        sys.exit(1)

    central_token = str(WORKERS_DIR / os.environ.get(
        "GMAIL_TOKEN_CENTRAL", "gmail_token_central.json"
    ))
    if not Path(central_token).exists():
        logger.error(
            f"Central Gmail token not found: {central_token}\n"
            f"  Run: python workers/oauth_setup.py --token {central_token}"
        )
        sys.exit(1)

    run_start_ts  = int(time.time())
    deadline      = run_start_ts + EMAIL_TIMEOUT_MINUTES * 60
    results       = {}
    pending       = []   # [(msg_id, pdf_path), ...]
    pending_lock  = threading.Lock()
    seen_ids      = set()
    stop_collector = threading.Event()

    with tempfile.TemporaryDirectory(prefix="cas_auto_") as tmp_dir:

        # Start the single shared Gmail collector thread
        collector_thread = threading.Thread(
            target=_gmail_collector,
            args=(central_token, tmp_dir, run_start_ts,
                  pending, pending_lock, seen_ids, stop_collector, deadline),
            name="gmail-collector",
            daemon=True,
        )
        collector_thread.start()

        # Fire CAMS triggers one by one (staggered), spawn parser thread after each
        entity_threads = []
        for idx, cfg in enumerate(configs):
            if idx > 0:
                delay = random.randint(75, 150)
                logger.info(f"Cooling {delay}s before triggering [{cfg.code}]...")
                time.sleep(delay)

            logger.info(f"━━━ [{cfg.code}] {cfg.email} ━━━")
            pdf_password = _random_pdf_password()

            ok = cams_trigger_worker.trigger_cas_request(cfg.pan, cfg.email, pdf_password)
            if not ok:
                logger.error(f"[{cfg.code}] CAMS trigger failed — skipping")
                results[cfg.code] = False
                continue

            t = threading.Thread(
                target=_entity_worker,
                args=(cfg, pdf_password, pending, pending_lock, deadline, results),
                name=f"parser-{cfg.code}",
                daemon=True,
            )
            t.start()
            entity_threads.append(t)

        # Wait for all parser threads
        logger.info(f"All triggers fired. Waiting up to {EMAIL_TIMEOUT_MINUTES}m for PDFs...")
        for t in entity_threads:
            remaining = max(0.0, deadline - time.time())
            t.join(timeout=remaining)

        stop_collector.set()
        collector_thread.join(timeout=5)

    # NAV refresh once after all entities
    logger.info("━━━ Refreshing NAVs ━━━")
    try:
        amfi_nav_worker.run()
    except SystemExit:
        logger.error("NAV worker failed")

    passed = [k for k, v in results.items() if v]
    failed = [k for k, v in results.items() if not v]
    logger.info(f"╚══ Done. Passed: {passed} | Failed: {failed} ══╝")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
