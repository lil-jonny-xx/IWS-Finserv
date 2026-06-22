#!/usr/bin/env python3
"""
DB migration — pms_realised table.

PMS holdings (pms_holding) are snapshot-only, so realised gains cannot be derived
by average-costing them. Instead the broker (Nuvama WealthSpectrum / Zerodha PMS)
publishes a realised capital-gains statement with the P&L already computed per lot.
Those pre-computed rows land here and are surfaced by report_generator._fetch_realised_gains
under group='PMS'. All amounts are INR (Indian PMS — no FX).

Populated by workers/import_pms_realised.py (manual statement import) and, once a
real statement format is wired, by nuvama_pms_worker.py.

Idempotent — safe to run repeatedly.
Run once:  python -m workers.db_migrate_pms_realised
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS pms_realised (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    source          VARCHAR(40)  NOT NULL DEFAULT 'nuvama_pms',  -- nuvama_pms | zerodha_pms
    security_name   VARCHAR(200) NOT NULL,
    isin            VARCHAR(20),
    quantity        NUMERIC(20, 4),
    buy_date        DATE,
    sale_date       DATE          NOT NULL,
    purchase_amount NUMERIC(20, 2),                 -- cost of the sold lot (INR)
    sale_amount     NUMERIC(20, 2),                 -- proceeds (INR)
    pnl             NUMERIC(20, 2) NOT NULL,         -- realised P&L from the statement (INR)
    source_ref      VARCHAR(128),                    -- dedup key (statement id, else synthetic)
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pms_realised_entity ON pms_realised (entity_id, sale_date);

-- Re-importing the same statement row is a no-op. Partial unique index so rows
-- without a source_ref are still allowed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pms_realised_ref
    ON pms_realised (source, source_ref) WHERE source_ref IS NOT NULL;
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
        print("pms_realised table ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
