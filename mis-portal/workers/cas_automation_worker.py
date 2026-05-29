#!/usr/bin/env python3
"""
CAS Automation Worker — IWS MIS Portal
Orchestrates daily CAS fetch for all 6 entities via one central Gmail inbox.
All 6 entity alias emails auto-forward their CAMS CAS emails to this inbox.

Flow per entity:
  1. Playwright → submit CAMS CAS request (entity alias email + PAN)
  2. Gmail API  → wait for forwarded PDF, filtered by to:alias
  3. cas_parser → parse PDF, upsert holdings + transactions
  4. amfi_nav   → refresh NAVs (once, after all entities)

Note: Atharv (ADR) is a minor — his folios may appear in both his CAS and
Dhruv's CAS. Duplicate transactions are silently ignored (ON CONFLICT DO NOTHING).

Schedule: Daily at 3 AM IST (21:30 UTC previous day)
Cron:     30 21 * * * /var/www/.venv/bin/python /var/www/mis-portal/workers/cas_automation_worker.py
"""
import os
import sys
import time
import secrets
import logging
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

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

WORKERS_DIR = Path(__file__).parent


@dataclass
class EntityConfig:
    code: str   # e.g. "DHR"
    email: str  # alias registered with CAMS (used as to: filter)
    pan: str    # PAN number registered with CAMS


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
    """Generate a fresh random alphanumeric password for each CAS request."""
    return secrets.token_urlsafe(12)


def process_entity(cfg: EntityConfig, central_token: str, tmp_dir: str) -> bool:
    entity_dir = os.path.join(tmp_dir, cfg.code)
    os.makedirs(entity_dir, exist_ok=True)
    tmp_dir = entity_dir

    logger.info(f"━━━ [{cfg.code}] {cfg.email} ━━━")

    # Fresh random password for this request — same string used to open the PDF
    pdf_password = _random_pdf_password()
    logger.info(f"[{cfg.code}] Generated PDF password for this run")

    # Step 1: trigger CAMS, passing the random password to protect the PDF
    ok = cams_trigger_worker.trigger_cas_request(cfg.pan, cfg.email, pdf_password)
    if not ok:
        logger.error(f"[{cfg.code}] CAMS trigger failed — skipping")
        return False

    # Step 2: wait in central inbox, filtered to this alias
    pdf_path = gmail_worker.wait_for_cas_email(
        token_file=central_token,
        save_dir=tmp_dir,
        to_address=cfg.email,
        poll_interval=30,
        timeout_minutes=15,
    )
    if not pdf_path:
        logger.error(f"[{cfg.code}] CAS email not received — skipping")
        return False

    # Step 3: parse + upsert using the same random password
    try:
        cas_parser_worker.run(pdf_path, pdf_password)
        logger.info(f"[{cfg.code}] CAS parsed successfully")
    except SystemExit:
        logger.error(f"[{cfg.code}] CAS parser exited with error")
        return False
    finally:
        try:
            os.remove(pdf_path)
        except OSError:
            pass

    return True


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

    results = {}
    with tempfile.TemporaryDirectory(prefix="cas_auto_") as tmp_dir:
        # Sequential processing: CAMS rate-limits / blocks IPs when multiple
        # browser sessions hit it simultaneously. Process one entity at a time
        # with a cooldown between requests to avoid triggering bot detection.
        INTER_ENTITY_DELAY_SECS = 90
        for idx, cfg in enumerate(configs):
            if idx > 0:
                logger.info(f"Cooling down {INTER_ENTITY_DELAY_SECS}s before next entity...")
                time.sleep(INTER_ENTITY_DELAY_SECS)
            try:
                results[cfg.code] = process_entity(cfg, central_token, tmp_dir)
            except Exception as exc:
                logger.error(f"[{cfg.code}] Unhandled exception: {exc}")
                results[cfg.code] = False

    # Step 4: refresh NAVs once after all entities processed
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
