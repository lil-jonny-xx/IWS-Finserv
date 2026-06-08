#!/usr/bin/env python3
"""
One-shot manual re-trigger for specific entities.
Usage: python workers/manual_cas_retrigger.py
"""
import os, sys, secrets, logging, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv('/var/www/mis-portal/.env', override=True)

import fitz
import cams_trigger_worker
import gmail_worker
import cas_parser_worker

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('/var/log/mis-portal-cas-worker.log'),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

TOKEN_FILE    = str(Path(__file__).parent / "gmail_token_central.json")
POLL_INTERVAL = 30   # seconds between Gmail polls
TIMEOUT_MIN   = 30   # minutes to wait per entity

TARGETS = [
    ("IWS Fincorp", "BAGPR8013K", "***REMOVED***"),
    ("SDR",         "AOTPD7980H", "***REMOVED***"),
]


def run_entity(name, pan, email, tmp_dir, seen_ids):
    logger.info(f"━━━ [{name}] manual re-trigger ━━━")
    pdf_password = secrets.token_urlsafe(12)
    start_ts     = int(time.time()) - 60

    ok = cams_trigger_worker.trigger_cas_request(pan, email, pdf_password)
    if not ok:
        logger.error(f"[{name}] CAMS trigger reported failure — watching for PDF anyway")

    logger.info(f"[{name}] Waiting up to {TIMEOUT_MIN}m for PDF...")
    deadline = time.time() + TIMEOUT_MIN * 60

    while time.time() < deadline:
        pdfs = gmail_worker.collect_new_cas_pdfs(
            token_file=TOKEN_FILE,
            save_dir=tmp_dir,
            after_ts=start_ts,
            exclude_ids=seen_ids,
        )
        for msg_id, pdf_path in pdfs:
            seen_ids.add(msg_id)
            try:
                doc    = fitz.open(pdf_path)
                result = doc.authenticate(pdf_password)
                doc.close()
            except Exception:
                result = 0
            if result != 0:
                logger.info(f"[{name}] Claimed PDF: {Path(pdf_path).name}")
                cas_parser_worker.run(pdf_path, pdf_password)
                logger.info(f"[{name}] Parsed and upserted successfully")
                return True
        time.sleep(POLL_INTERVAL)

    logger.error(f"[{name}] Timed out — no matching PDF arrived within {TIMEOUT_MIN}m")
    return False


def main():
    tmp_dir  = tempfile.mkdtemp(prefix="cas_manual_")
    seen_ids = set()
    results  = {}

    for i, (name, pan, email) in enumerate(TARGETS):
        results[name] = run_entity(name, pan, email, tmp_dir, seen_ids)
        if i < len(TARGETS) - 1:
            logger.info("Waiting 90s before next trigger...")
            time.sleep(90)

    logger.info("━━━ Manual re-trigger complete ━━━")
    for name, ok in results.items():
        logger.info(f"  {name}: {'✅ OK' if ok else '❌ FAILED'}")


if __name__ == "__main__":
    main()
