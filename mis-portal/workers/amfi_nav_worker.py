#!/usr/bin/env python3
"""
AMFI NAV Worker — IWS MIS Portal
Fetches daily NAV for tracked MFs using amfi_code.
Auto-resolves missing amfi_codes via isin_resolver.
Schedule: Daily at 10 PM IST (4:30 PM UTC) via cron
"""
import os
import sys
import logging
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timezone
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from isin_resolver import resolve_all_missing

load_dotenv('/var/www/mis-portal/.env', override=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        # File persistence handled by cron_wrapper stdout -> crontab log redirect
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

MFAPI_BASE = "https://api.mfapi.in/mf"


def now_utc():
    return datetime.now(timezone.utc)


def get_db():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


def fetch_nav(amfi_code: str) -> dict:
    """Fetch latest NAV from mfapi.in with 3 retries and exponential backoff."""
    import time
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.get(f"{MFAPI_BASE}/{amfi_code}", timeout=20)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "SUCCESS":
                return None
            nav_data = data.get("data", [])
            if not nav_data:
                return None
            latest = nav_data[0]
            return {
                "nav":  float(latest["nav"]),
                "date": datetime.strptime(latest["date"], "%d-%m-%Y").date(),
                "name": data.get("meta", {}).get("scheme_name", ""),
            }
        except Exception as e:
            last_exc = e
            if attempt < 2:
                logger.warning(f"mfapi.in attempt {attempt + 1} failed for {amfi_code}: {e} — retrying")
                time.sleep(5 * (attempt + 1))
    raise last_exc


def get_tracked(conn) -> list:
    """Get securities with amfi_code."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, isin, amfi_code, security_name
        FROM security_master
        WHERE amfi_code IS NOT NULL
        AND isin IS NOT NULL
    """)
    rows = cursor.fetchall()
    cursor.close()
    return rows


def save_nav(conn, security_id, nav_date, nav):
    """Upsert NAV into nav_history."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO nav_history (security_id, nav_date, nav, created_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (security_id, nav_date)
        DO UPDATE SET nav = EXCLUDED.nav, created_at = EXCLUDED.created_at
    """, (security_id, nav_date, nav, now_utc()))
    cursor.close()


def update_holding(conn, security_id, nav):
    """Update current NAV and value on holding table."""
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE holding
        SET last_updated_nav = %s,
            current_value    = quantity * %s,
            last_updated     = %s
        WHERE security_id = %s
    """, (nav, nav, now_utc(), security_id))
    cursor.close()


def log_run(conn, status, processed, failed, started, error=None):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO ingestion_run
            (run_type, run_date, status, records_processed,
             records_failed, error_message, started_at, completed_at)
        VALUES ('amfi_nav', %s, %s, %s, %s, %s, %s, %s)
    """, (date.today(), status, processed, failed,
          error, started, now_utc()))
    cursor.close()


def run():
    started   = now_utc()
    today     = date.today()
    logger.info(f"=== AMFI NAV Worker starting for {today} ===")

    conn      = None
    processed = 0
    failed    = 0
    skipped   = 0

    try:
        conn = get_db()

        # Skip if already ran today
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) AS c FROM nav_history WHERE nav_date = %s",
            (today,)
        )
        existing = cursor.fetchone()["c"]
        cursor.close()

        if existing > 0:
            logger.info(f"Already ran today ({existing} records). Skipping.")
            return

        # Auto-resolve any missing amfi_codes first
        logger.info("Checking for missing amfi_codes...")
        resolve_all_missing(conn)

        # Get tracked securities
        tracked = get_tracked(conn)
        logger.info(f"Tracking {len(tracked)} securities")

        if not tracked:
            logger.info("No securities with amfi_code yet. Run CAS parser first.")
            log_run(conn, "success", 0, 0, started)
            conn.commit()
            return

        # Fetch NAV for each
        for sec in tracked:
            name      = sec["security_name"]
            amfi_code = sec["amfi_code"]
            sec_id    = sec["id"]

            try:
                entry = fetch_nav(amfi_code)
                if not entry:
                    logger.warning(f"No NAV for amfi_code {amfi_code}")
                    skipped += 1
                    continue

                save_nav(conn, sec_id, entry["date"], entry["nav"])
                update_holding(conn, sec_id, entry["nav"])
                logger.info(f"✅ {name[:45]} | {entry['nav']} | {entry['date']}")
                processed += 1

            except Exception as e:
                logger.error(f"Failed {name[:40]}: {e}")
                failed += 1

        log_run(conn, "success", processed, failed, started)
        conn.commit()

        logger.info(f"=== Done: {processed} saved | {skipped} skipped | {failed} failed ===")

    except Exception as e:
        logger.error(f"AMFI Worker FAILED: {e}")
        if conn:
            try:
                log_run(conn, "failed", processed, failed, started, str(e))
                conn.commit()
            except:
                pass
        sys.exit(1)

    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    run()