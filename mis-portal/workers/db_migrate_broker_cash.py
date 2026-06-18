#!/usr/bin/env python3
"""
DB migration — broker_cash table.

Stores the available cash balance for each (entity, broker) trading account,
fetched from the broker funds API (Zerodha margins / Angel One RMS / Dhan
fund-limit). One row per entity+broker; upserted on each refresh. Shown on the
Equity page alongside holdings.

Note: Rajani Corp's Zerodha account is a PMS, so its cash lives in pms_holding
(source=zerodha_pms), NOT here — this table only covers the regular equity
broker accounts in equity_sync_worker.BROKER_ENTITY_MAP.

Run once:  python -m workers.db_migrate_broker_cash
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS broker_cash (
    id          SERIAL PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    broker      VARCHAR(20) NOT NULL,          -- zerodha | angel_one | dhan
    balance     NUMERIC(20, 2) NOT NULL DEFAULT 0,
    as_of_date  DATE,
    updated_at  TIMESTAMP DEFAULT NOW(),
    UNIQUE (entity_id, broker)
);
CREATE INDEX IF NOT EXISTS idx_broker_cash_entity ON broker_cash (entity_id);
"""


def main():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        print("broker_cash table ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
