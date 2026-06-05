#!/usr/bin/env python3
"""
CAS Automation Worker — IWS MIS Portal
Orchestrates daily CAS fetch for all 6 entities via one central Gmail inbox.
All entity emails (or their aliases) forward CAMS CAS emails to ***REMOVED***.

Flow:
  1. Start a Gmail collector thread that polls for new CAS PDFs until 8 AM IST
  2. Shuffle entities so no two consecutive entries share the same PAN
  3. Fire CAMS requests one by one; between each trigger wait a random delay
     where the ceiling is itself randomised between 45–75 min (floor always 30 min)
  4. Each parser thread claims the first PDF in the shared queue that opens with
     its entity's password (fitz.authenticate), then parses + upserts to DB
  5. All parser threads run concurrently — wall time ≈ slowest email delivery
  6. After all threads complete (or 8 AM IST deadline), refresh NAVs once

Schedule: Daily at 11 PM IST (17:30 UTC)
Cron:     30 17 * * * /var/www/.venv/bin/python /var/www/mis-portal/workers/cas_automation_worker.py
"""
import os
import sys
import time
import random
import secrets
import logging
import tempfile
import threading
import datetime
import zoneinfo
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import fitz  # PyMuPDF — used for password matching only
import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

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
IST         = zoneinfo.ZoneInfo("Asia/Kolkata")


def _deadline_8am_ist() -> float:
    """Unix timestamp for 8:00 AM IST — same day if before 8 AM, next day otherwise."""
    now    = datetime.datetime.now(IST)
    target = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now >= target:
        target += datetime.timedelta(days=1)
    return target.timestamp()


@dataclass
class EntityConfig:
    code: str
    email: str
    pan: str


def _entity_configs() -> list[EntityConfig]:
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"], dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
    )
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT e.entity_name AS code, e.cas_email AS email, pg.pan_number AS pan
        FROM   entity e
        JOIN   pan_group pg ON pg.id = e.pan_group_id
        WHERE  e.cas_email IS NOT NULL AND pg.pan_number IS NOT NULL
        ORDER  BY e.id
    """)
    rows = cur.fetchall()
    conn.close()
    return [EntityConfig(code=r["code"], email=r["email"], pan=r["pan"]) for r in rows]


def _shuffled_no_consecutive_pan(configs: list[EntityConfig]) -> list[EntityConfig]:
    """
    Returns configs in a random order where no two consecutive entries share a PAN.
    Tries up to 200 random shuffles first; falls back to a greedy interleave if needed.
    """
    result = list(configs)
    for _ in range(200):
        random.shuffle(result)
        if all(result[i].pan != result[i + 1].pan for i in range(len(result) - 1)):
            return result

    # Greedy fallback: always pick from the largest PAN group that differs from the last,
    # breaking ties randomly so the output is still non-deterministic.
    groups: dict[str, list[EntityConfig]] = defaultdict(list)
    for cfg in configs:
        groups[cfg.pan].append(cfg)
    for grp in groups.values():
        random.shuffle(grp)

    ordered: list[EntityConfig] = []
    last_pan: str | None = None
    while groups:
        eligible = [(pan, lst) for pan, lst in groups.items() if pan != last_pan]
        if not eligible:
            eligible = list(groups.items())  # unavoidable consecutive — pick anything
        max_len  = max(len(lst) for _, lst in eligible)
        top      = [(pan, lst) for pan, lst in eligible if len(lst) == max_len]
        pan, lst = random.choice(top)
        ordered.append(lst.pop())
        last_pan = pan
        if not groups[pan]:
            del groups[pan]

    return ordered


def _random_pdf_password() -> str:
    return secrets.token_urlsafe(12)


def _pdf_matches_password(pdf_path: str, password: str) -> bool:
    """
    Returns True if `password` correctly unlocks the PDF.
    fitz.Document.authenticate() returns 0 on wrong password, non-zero on correct.
    """
    try:
        doc    = fitz.open(pdf_path)
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
        with pending_lock:
            snapshot = list(pending)

        for item in snapshot:
            msg_id, pdf_path = item
            if _pdf_matches_password(pdf_path, pdf_password):
                with pending_lock:
                    if item in pending:
                        pending.remove(item)
                        claimed_path = pdf_path
                        logger.info(f"[{cfg.code}] Claimed PDF: {Path(pdf_path).name}")
                        break
        else:
            time.sleep(15)

    if not claimed_path:
        deadline_str = datetime.datetime.fromtimestamp(deadline, IST).strftime("%H:%M IST")
        logger.error(f"[{cfg.code}] Timed out — no matching PDF arrived before {deadline_str}")
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

LOCK_FILE = Path("/tmp/cas_automation.lock")

def _acquire_lock() -> bool:
    """Return True if we got the lock, False if another instance is running."""
    try:
        if LOCK_FILE.exists():
            pid = int(LOCK_FILE.read_text().strip())
            try:
                os.kill(pid, 0)  # 0 = check existence only
                logger.error(f"Another CAS Automation is already running (PID {pid}). Exiting.")
                return False
            except (ProcessLookupError, PermissionError):
                logger.warning(f"Stale lock file (PID {pid} gone) — removing and continuing.")
        LOCK_FILE.write_text(str(os.getpid()))
        return True
    except Exception as e:
        logger.warning(f"Could not acquire lock: {e} — proceeding anyway")
        return True


def main():
    if not _acquire_lock():
        sys.exit(1)

    try:
        _main()
    finally:
        try:
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass


def _main():
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

    run_start_ts   = int(time.time())
    deadline       = _deadline_8am_ist()
    deadline_str   = datetime.datetime.fromtimestamp(deadline, IST).strftime("%H:%M IST")
    results        = {}
    pending        = []
    pending_lock   = threading.Lock()
    seen_ids       = set()
    stop_collector = threading.Event()

    # Shuffle so no two consecutive triggers share a PAN
    ordered = _shuffled_no_consecutive_pan(configs)
    order_str = " → ".join(f"{c.code}({c.pan[:4]}****)" for c in ordered)
    logger.info(f"Gmail collector active until {deadline_str}")
    logger.info(f"Trigger order: {' → '.join(c.code for c in ordered)}")

    with tempfile.TemporaryDirectory(prefix="cas_auto_") as tmp_dir:

        collector_thread = threading.Thread(
            target=_gmail_collector,
            args=(central_token, tmp_dir, run_start_ts,
                  pending, pending_lock, seen_ids, stop_collector, deadline),
            name="gmail-collector",
            daemon=True,
        )
        collector_thread.start()

        entity_threads = []
        for idx, cfg in enumerate(ordered):
            if idx > 0:
                # Randomise the ceiling between 45–75 min, then pick a delay
                # uniformly between 30 min and that ceiling.
                ceil_s  = random.uniform(45 * 60, 75 * 60)
                delay_s = random.uniform(30 * 60, ceil_s)
                fire_at = datetime.datetime.fromtimestamp(
                    time.time() + delay_s, IST
                ).strftime("%H:%M IST")
                logger.info(
                    f"Next trigger [{cfg.code}] in {delay_s/60:.0f}m "
                    f"(ceil {ceil_s/60:.0f}m, fires ~{fire_at})"
                )
                time.sleep(delay_s)

            logger.info(f"━━━ [{cfg.code}] ━━━")
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

        logger.info(f"All triggers fired. Waiting until {deadline_str} for PDFs...")
        for t in entity_threads:
            remaining = max(0.0, deadline - time.time())
            t.join(timeout=remaining)

        stop_collector.set()
        collector_thread.join(timeout=5)

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
