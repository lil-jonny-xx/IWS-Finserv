#!/usr/bin/env python3
"""
DB migration — broker_cash_currency detail table.

`broker_cash` stays the source of truth for each (entity, broker) TOTAL cash,
stored as ONE consolidated INR row (for IBKR that's the Flex BASE_SUMMARY, i.e.
every currency rolled into the account base currency). Every existing read site
(Equity/Foreign-Equity API, reports, portfolio XIRR, overview) keeps summing
that single row unchanged — no double-count risk.

This table is ADDITIVE: it holds the per-currency breakdown behind that total,
so the portal can itemise, e.g., SDR's IBKR cash as AED / GBP (margin) / USD
instead of one AED number. One row per (entity, broker, currency); the daily
IBKR worker upserts the currencies present in today's statement and DELETES any
currency no longer present (true snapshot — sold-off / swept currencies drop).

Run once:  python -m workers.db_migrate_broker_cash_currency
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS broker_cash_currency (
    id             SERIAL PRIMARY KEY,
    entity_id      INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    broker         VARCHAR(20) NOT NULL,          -- ibkr | vested | dbs | ...
    currency       VARCHAR(10) NOT NULL,          -- AED | USD | GBP | ...
    balance_native NUMERIC(20, 2) NOT NULL DEFAULT 0,   -- cash in that currency
    balance_inr    NUMERIC(20, 2) NOT NULL DEFAULT 0,   -- converted at fx_rate
    fx_rate        NUMERIC(20, 6),                       -- INR per 1 unit
    as_of_date     DATE,
    updated_at     TIMESTAMP DEFAULT NOW(),
    UNIQUE (entity_id, broker, currency)
);
CREATE INDEX IF NOT EXISTS idx_broker_cash_ccy_entity
    ON broker_cash_currency (entity_id, broker);
"""


def main():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        print("broker_cash_currency table ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
