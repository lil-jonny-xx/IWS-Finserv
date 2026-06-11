"""
Equity Token Refresh Worker

Refreshes daily-expiring broker access tokens for Zerodha, Angel One, and Dhan.

Schedule: Daily at 6:30 AM IST (01:00 UTC) — before equity_sync_worker
Cron:     0 1 * * * /var/www/.venv/bin/python /var/www/mis-portal/equity/token_refresh_worker.py >> /var/log/mis-portal-equity-token.log 2>&1
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env", override=True)

from equity.brokers import zerodha, angel_one, dhan

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # File persistence handled by cron_wrapper stdout -> crontab log redirect
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


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

    if errors:
        logger.error(f"=== Done with errors: {errors} ===")
        sys.exit(1)

    logger.info("=== Done. All tokens refreshed ===")


if __name__ == "__main__":
    main()
