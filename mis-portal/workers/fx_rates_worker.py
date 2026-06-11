#!/usr/bin/env python3
"""
FX Rates Worker — IWS MIS Portal
Fetches daily exchange rates and stores in PostgreSQL.
Primary: exchangerate-api.com
Fallback: frankfurter.app (no API key needed)
Currencies tracked: USD, GBP, SGD, AED, HKD → INR
Schedule: Daily at 6:00 AM IST via cron
"""
import os
import sys
import logging
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timezone
from dotenv import load_dotenv

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

FX_API_KEY = os.getenv("FX_API_KEY")
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}

TARGET_CURRENCIES = ["USD", "GBP", "SGD", "AED", "HKD"]


def get_db_connection():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


def now_utc():
    return datetime.now(timezone.utc)


def fetch_rates_primary():
    url = f"https://v6.exchangerate-api.com/v6/{FX_API_KEY}/latest/USD"
    logger.info("Fetching rates from exchangerate-api.com")
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    data = response.json()
    if data.get("result") != "success":
        raise ValueError(f"API error: {data.get('error-type', 'unknown')}")
    rates = data["conversion_rates"]
    inr_per_usd = rates.get("INR")
    if not inr_per_usd:
        raise ValueError("INR rate not found")
    result = {}
    for currency in TARGET_CURRENCIES:
        if currency in rates:
            result[currency] = round(inr_per_usd / rates[currency], 6)
    logger.info(f"Primary rates: {result}")
    return result


def fetch_rates_fallback():
    currencies = ",".join(TARGET_CURRENCIES + ["INR"])
    url = f"https://api.frankfurter.app/latest?from=USD&to={currencies}"
    logger.info("Fetching rates from frankfurter.app (fallback)")
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    data = response.json()
    rates = data.get("rates", {})
    inr_per_usd = rates.get("INR")
    if not inr_per_usd:
        raise ValueError("INR rate not found in fallback")
    result = {}
    for currency in TARGET_CURRENCIES:
        if currency in rates:
            result[currency] = round(inr_per_usd / rates[currency], 6)
    logger.info(f"Fallback rates: {result}")
    return result


def save_rates_to_db(conn, rates, rate_date):
    cursor = conn.cursor()
    saved = 0
    for currency, rate in rates.items():
        cursor.execute(
            """
            INSERT INTO fx_rate (from_currency, to_currency, rate_date, rate, created_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (from_currency, to_currency, rate_date)
            DO UPDATE SET rate = EXCLUDED.rate, created_at = EXCLUDED.created_at
            """,
            (currency, "INR", rate_date, rate, now_utc())
        )
        saved += 1
        logger.info(f"Saved: 1 {currency} = {rate:.4f} INR")
    cursor.close()
    return saved


def log_ingestion_run(conn, status, records_processed, records_failed,
                       started_at, error_message=None):
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO ingestion_run
            (run_type, run_date, status, records_processed, records_failed,
             error_message, started_at, completed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        ("fx_rates", date.today(), status, records_processed,
         records_failed, error_message, started_at, now_utc())
    )
    cursor.close()


def run():
    started_at = now_utc()
    today = date.today()
    logger.info(f"=== FX Rates Worker starting for {today} ===")

    conn = None
    rates = {}
    records_processed = 0
    records_failed = 0

    try:
        conn = get_db_connection()

        # Check if rates already exist for today
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) as count FROM fx_rate WHERE rate_date = %s",
            (today,)
        )
        existing = cursor.fetchone()["count"]
        cursor.close()

        if existing > 0:
            logger.info(f"Rates for {today} already exist. Skipping.")
            return

        # Try primary source
        try:
            rates = fetch_rates_primary()
        except Exception as e:
            logger.warning(f"Primary failed: {e}. Trying fallback...")
            try:
                rates = fetch_rates_fallback()
            except Exception as e2:
                raise Exception(f"Both sources failed. Primary: {e}. Fallback: {e2}")

        if not rates:
            raise Exception("No rates fetched")

        records_processed = save_rates_to_db(conn, rates, today)

        log_ingestion_run(conn, "success", records_processed, 0, started_at)
        conn.commit()

        logger.info(f"=== FX Worker completed. Saved {records_processed} rates ===")

    except Exception as e:
        records_failed = len(TARGET_CURRENCIES)
        logger.error(f"FX Worker FAILED: {e}")
        if conn:
            try:
                log_ingestion_run(conn, "failed", records_processed,
                                   records_failed, started_at, str(e))
                conn.commit()
            except Exception as log_err:
                logger.error(f"Failed to log error: {log_err}")
        sys.exit(1)

    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    run()