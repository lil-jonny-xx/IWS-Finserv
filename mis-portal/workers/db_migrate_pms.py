#!/usr/bin/env python3
"""
DB migration — pms_holding table.

Stores the latest holdings inside each Nuvama-managed PMS account, parsed from
the WealthSpectrum report by nuvama_pms_worker.py. One row per instrument per
entity; `holding_type` separates equity positions from cash so the PMS view can
show an equity total, a cash total, and a combined total. Upserted as a full
replace per entity on each run (sold positions disappear).

Run once:  python -m workers.db_migrate_pms
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS pms_holding (
    id            SERIAL PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    as_on_date    DATE,
    holding_type  VARCHAR(10)  NOT NULL,          -- 'equity' | 'cash'
    security_name VARCHAR(200) NOT NULL,
    isin          VARCHAR(20),
    quantity      NUMERIC(20, 4),
    avg_cost      NUMERIC(20, 4),
    cost          NUMERIC(20, 2),
    current_price NUMERIC(20, 4),
    market_value  NUMERIC(20, 2) NOT NULL,
    weight_pct    NUMERIC(8, 4),
    source        VARCHAR(40) DEFAULT 'nuvama_pms',
    updated_at    TIMESTAMP DEFAULT NOW(),
    UNIQUE (entity_id, holding_type, security_name)
);
CREATE INDEX IF NOT EXISTS idx_pms_holding_entity ON pms_holding (entity_id);
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
        print("pms_holding table ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
