#!/usr/bin/env python3
"""
NAV History Backfill — IWS MIS Portal

mfapi.in returns the full NAV history in a single call. This script reuses
that payload to bulk-insert all available historical NAVs into nav_history,
skipping dates already present (ON CONFLICT DO NOTHING).

Run once manually, or re-run at any time safely — it is idempotent.

Usage:
    /var/www/.venv/bin/python /var/www/mis-portal/workers/nav_history_backfill.py
"""
import os, sys, logging, requests, psycopg2, psycopg2.extras
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}
MFAPI_BASE = "https://api.mfapi.in/mf"


def get_db():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=psycopg2.extras.RealDictCursor)


def fetch_all_navs(amfi_code: str) -> list[dict]:
    """Return list of {nav_date, nav} for every date in mfapi history."""
    r = requests.get(f"{MFAPI_BASE}/{amfi_code}", timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "SUCCESS":
        return []
    rows = []
    for entry in data.get("data", []):
        try:
            rows.append({
                "nav_date": datetime.strptime(entry["date"], "%d-%m-%Y").date(),
                "nav":      float(entry["nav"]),
            })
        except (KeyError, ValueError):
            continue
    return rows


def backfill_security(conn, security_id: int, amfi_code: str, name: str) -> int:
    rows = fetch_all_navs(amfi_code)
    if not rows:
        logger.warning(f"  {name}: no data from mfapi")
        return 0

    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO nav_history (security_id, nav_date, nav, created_at)
        VALUES (%(sid)s, %(nav_date)s, %(nav)s, NOW())
        ON CONFLICT (security_id, nav_date) DO NOTHING
        """,
        [{"sid": security_id, **r} for r in rows],
    )
    inserted = cur.rowcount
    conn.commit()
    cur.close()
    return inserted


def run():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        "SELECT id, amfi_code, security_name FROM security_master WHERE amfi_code IS NOT NULL"
    )
    securities = cur.fetchall()
    cur.close()

    if not securities:
        logger.info("No securities with amfi_code found.")
        return

    logger.info(f"Backfilling {len(securities)} securities…")
    total_inserted = 0
    for s in securities:
        try:
            n = backfill_security(conn, s["id"], s["amfi_code"], s["security_name"])
            logger.info(f"  {s['security_name'][:60]}: +{n} rows")
            total_inserted += n
        except Exception as e:
            logger.error(f"  {s['security_name'][:60]}: ERROR — {e}")

    conn.close()
    logger.info(f"Done. Total rows inserted: {total_inserted}")


if __name__ == "__main__":
    run()
