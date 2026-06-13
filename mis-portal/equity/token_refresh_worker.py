"""
Equity Token Refresh Worker

Refreshes daily-expiring broker access tokens for Zerodha, Angel One, and Dhan.

Schedule: Daily at 6:30 AM IST (01:00 UTC) — before equity_sync_worker
Cron:     0 1 * * * /var/www/.venv/bin/python /var/www/mis-portal/equity/token_refresh_worker.py >> /var/log/mis-portal-equity-token.log 2>&1
"""
import logging
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

from equity.brokers import zerodha, angel_one, dhan
from equity import tokens

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # File persistence handled by cron_wrapper stdout -> crontab log redirect
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def _sync_tokens_to_db(conn, entity_map: dict):
    """Push fresh tokens from equity_tokens.json → broker_api_credentials."""
    all_tokens = tokens.load()
    cur = conn.cursor()
    synced = 0
    for key, token in all_tokens.items():
        if not token:
            continue
        # key format: "{broker}_{entity_code}"  e.g. "zerodha_DHR", "zerodha_Rajani Corp"
        parts = key.split("_", 1)
        if len(parts) != 2:
            continue
        broker_raw, entity_code = parts[0], parts[1]
        # Normalise broker name
        broker = {"angelone": "angel_one", "angel": "angel_one"}.get(broker_raw, broker_raw)
        if broker.endswith("_refresh"):
            continue
        entity_id = entity_map.get(entity_code)
        if entity_id is None:
            continue
        cur.execute(
            """
            UPDATE broker_api_credentials
            SET    access_token = %s, updated_at = NOW()
            WHERE  broker = %s AND entity_id = %s AND is_active = TRUE
            """,
            (token, broker, entity_id),
        )
        if cur.rowcount:
            synced += 1
    conn.commit()
    cur.close()
    logger.info(f"Synced {synced} token(s) to broker_api_credentials")


def main():
    logger.info("=== Equity token refresh starting ===")
    errors = []

    for code in zerodha.SUPPORTED_ENTITIES:
        try:
            zerodha.refresh_access_token(code)
        except Exception as e:
            logger.error(f"[{code}] Zerodha token refresh failed: {e}")
            errors.append(f"zerodha:{code}")

    for code in angel_one.SUPPORTED_ENTITIES:
        try:
            angel_one.refresh_access_token(code)
        except Exception as e:
            logger.error(f"[{code}] Angel One token refresh failed: {e}")
            errors.append(f"angel_one:{code}")

    for code in dhan.SUPPORTED_ENTITIES:
        try:
            dhan.refresh_access_token(code)
        except Exception as e:
            logger.error(f"[{code}] Dhan token refresh failed: {e}")
            errors.append(f"dhan:{code}")

    # Sync all fresh tokens to DB so equity_price_worker can use them
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            database=os.getenv("DB_NAME", "mis_portal"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        cur = conn.cursor()
        cur.execute("SELECT id, entity_name FROM entity")
        entity_map = {r["entity_name"]: r["id"] for r in cur.fetchall()}
        cur.close()
        _sync_tokens_to_db(conn, entity_map)
        conn.close()
    except Exception as e:
        logger.error(f"Token DB sync failed: {e}")

    if errors:
        logger.error(f"=== Done with errors: {errors} ===")
        sys.exit(1)

    logger.info("=== Done. All tokens refreshed ===")


if __name__ == "__main__":
    main()
