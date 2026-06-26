#!/usr/bin/env python3
"""
DB migration — unlisted rounds v2 (Phase 3 follow-up).

Adds:
  * unlisted_round.round_valuation  — the company's valuation at that round
    (informational context; not used in the share/value computation).
  * unlisted_event.bonus_shares     — for bonus issues entered as an absolute
    number of NEW shares (admin enters the count, new total is auto-derived).
    A split still uses `factor` (the share multiplier); a bonus now uses
    `bonus_shares` and its factor is derived from the share pool at that date.
  * unlisted_event.factor is made NULLABLE (bonus rows store bonus_shares, not a
    static factor).

Idempotent — safe to run repeatedly.

  /var/www/mis-portal/venv/bin/python -m workers.db_migrate_unlisted_rounds_v2
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
    ("unlisted_round.round_valuation",
     "ALTER TABLE unlisted_round ADD COLUMN IF NOT EXISTS round_valuation NUMERIC(20,2);"),
    ("unlisted_event.bonus_shares",
     "ALTER TABLE unlisted_event ADD COLUMN IF NOT EXISTS bonus_shares NUMERIC(20,4);"),
    ("unlisted_event.factor -> nullable",
     "ALTER TABLE unlisted_event ALTER COLUMN factor DROP NOT NULL;"),
]


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
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
