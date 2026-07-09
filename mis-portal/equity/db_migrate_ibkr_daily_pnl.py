#!/usr/bin/env python3
"""
DB migration — daily (today's) P&L for foreign equity holdings.

Adds a `pnl_daily` column to foreign_equity_holding. This column is owned
EXCLUSIVELY by the IBKR real-time stream (workers/ibkr_stream_daemon.py via
equity/ibkr_stream_sink.update_daily_pnl); it holds each position's
today-vs-prior-close P&L in INR, sourced from IBKR's reqPnLSingle.dailyPnL.

Why a new column instead of reusing an existing one: the daily Flex sync owns
quantity/cost, and foreign_price_worker owns current_price*/current_market_value*/
pnl_inception. `pnl_daily` is the one number nothing else writes, so the stream
never fights another writer. Overall daily P&L is SUM(pnl_daily) per entity.

Idempotent — safe to run repeatedly.

  /var/www/.venv/bin/python -m equity.db_migrate_ibkr_daily_pnl
"""
import os
import sys
import logging
import psycopg2

from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DDL = [
    (
        "foreign_equity_holding: add pnl_daily",
        "ALTER TABLE foreign_equity_holding "
        "ADD COLUMN IF NOT EXISTS pnl_daily NUMERIC;",
    ),
]


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "") or os.getenv("DB_PASS", ""),
    )


def main():
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    for name, sql in DDL:
        try:
            cur.execute(sql)
            conn.commit()
            logger.info(f"✅  {name}")
        except Exception as e:
            conn.rollback()
            logger.error(f"❌  {name}: {e}")
            sys.exit(1)

    cur.close()
    conn.close()
    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
