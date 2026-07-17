#!/usr/bin/env python3
"""
Add market_benchmark.prev_close — the previous session's official close.

The ticker shows a day% move, which needs a prior-close reference. Deriving it from
the stored series instead (value of the row before this one) would use whatever the
worker last wrote that day, i.e. the last intra-day print rather than the official
close, and would silently compare across a gap when a day is missing. Yahoo hands us
`previousClose` on the same basis as the live price, so we store it alongside.

Nullable on purpose: rows already written (and the manual GS-bond rows, and the
monthly IMF/FRED series) have no prior close and must stay readable. The API treats
NULL as "no day% for this code" rather than guessing.

Idempotent. Run before deploying the benchmark_worker that writes this column.

    /var/www/.venv/bin/python /var/www/mis-portal/workers/db_migrate_market_prev_close.py
"""
import logging
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "database": os.getenv("DB_NAME", "mis_portal"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
}


def main() -> int:
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    try:
        cur.execute("""
            ALTER TABLE market_benchmark
            ADD COLUMN IF NOT EXISTS prev_close numeric
        """)
        conn.commit()
        logger.info("market_benchmark.prev_close ready.")
        return 0
    except Exception as e:
        conn.rollback()
        logger.error("Migration failed: %s", e)
        return 1
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
