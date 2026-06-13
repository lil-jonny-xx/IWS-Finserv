#!/usr/bin/env python3
"""
DB migration — market_benchmark table.

Stores one row per (benchmark code, date).  Equity indices (Nifty/Sensex) are
auto-filled by benchmark_worker.py; GS-bond YTM/price have no free live feed and
are entered manually via the portal.  Previous-week and 31-Mar baselines are
derived at read time from the history in this table.

Run once:  python -m workers.db_migrate_benchmarks
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS market_benchmark (
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(40)  NOT NULL,         -- NIFTY, SENSEX, GS2032_YTM, ...
    label       VARCHAR(120) NOT NULL,
    as_of_date  DATE         NOT NULL,
    value       NUMERIC,                       -- index level / price / yield %
    unit        VARCHAR(20)  DEFAULT 'index',  -- 'index' | 'price' | 'pct'
    source      VARCHAR(40)  DEFAULT 'manual',
    updated_by  VARCHAR(120),
    updated_at  TIMESTAMP    DEFAULT NOW(),
    UNIQUE (code, as_of_date)
);
CREATE INDEX IF NOT EXISTS idx_market_benchmark_code_date
    ON market_benchmark (code, as_of_date DESC);
"""

# Canonical benchmark definitions (code -> (label, unit, source)).
BENCHMARKS = [
    ("NIFTY",        "NSE - Nifty 50",                 "index", "yahoo"),
    ("SENSEX",       "BSE - Sensex",                   "index", "yahoo"),
    ("GS2032_YTM",   "7.26% GS 2032 (Benchmark) (YTM)",   "pct",   "manual"),
    ("GS2032_PRICE", "7.26% GS 2032 (Benchmark) (Price)", "price", "manual"),
    ("GS2030_YTM",   "5.77% NI GS 2030 (Benchmark) (YTM)",   "pct",   "manual"),
    ("GS2030_PRICE", "5.77% NI GS 2030 (Benchmark) (Price)", "price", "manual"),
]


def main():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        database=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    cur = conn.cursor()
    cur.execute(DDL)
    # Seed a definitions registry row (date 1900-01-01) so the portal can list all
    # known benchmark codes/labels even before any value lands.
    for code, label, unit, source in BENCHMARKS:
        cur.execute("""
            INSERT INTO market_benchmark (code, label, as_of_date, value, unit, source)
            VALUES (%s, %s, DATE '1900-01-01', NULL, %s, %s)
            ON CONFLICT (code, as_of_date) DO UPDATE SET label = EXCLUDED.label
        """, (code, label, unit, source))
    conn.commit()
    cur.close()
    conn.close()
    print("✅ market_benchmark table ready; seeded", len(BENCHMARKS), "benchmark definitions.")


if __name__ == "__main__":
    main()
