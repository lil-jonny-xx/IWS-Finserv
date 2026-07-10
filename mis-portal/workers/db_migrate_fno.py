#!/usr/bin/env python3
"""
DB migration — FnO (futures & options) tables.

Open derivative positions per (entity, source, contract), scraped from the
broker portals that carry the FnO books — Share India uTrade (HHR) and later
Orbis (DHR). Neither has an investor API, so a portal scraper
(workers/shareindia_fno_worker.py) upserts here, mirroring how
foreign_equity_holding is fed.

Columns are deliberately loose/nullable: the exact fields each portal exposes
are only confirmed against the first real (screenshotted) login, and the two
portals won't show identical data.

fno_position — one row per open contract (net quantity; negative = short).
fno_account  — one row per (entity, source): margin / ledger / day P&L summary.

Run once:  python -m workers.db_migrate_fno
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS fno_position (
    id            SERIAL PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    source        VARCHAR(20) NOT NULL,               -- shareindia | orbis
    symbol        VARCHAR(120) NOT NULL,              -- full contract name as shown by the portal
    underlying    VARCHAR(60),                        -- NIFTY / BANKNIFTY / stock
    instrument    VARCHAR(10),                        -- FUT | CE | PE
    expiry        DATE,
    strike        NUMERIC(14, 2),
    product       VARCHAR(15) NOT NULL DEFAULT '',    -- NRML / MIS / … ('' when portal doesn't say)
    quantity      NUMERIC(16, 2) NOT NULL DEFAULT 0,  -- net qty; negative = short
    lot_size      INTEGER,
    avg_price     NUMERIC(16, 4),
    ltp           NUMERIC(16, 4),
    mtm_pnl       NUMERIC(18, 2),                     -- unrealised / MTM on the open position
    realized_pnl  NUMERIC(18, 2),                     -- booked intraday, if the portal shows it
    as_of_date    DATE,
    updated_at    TIMESTAMP DEFAULT NOW(),
    UNIQUE (entity_id, source, symbol, product)
);
CREATE INDEX IF NOT EXISTS idx_fno_position_entity ON fno_position (entity_id);

CREATE TABLE IF NOT EXISTS fno_account (
    id                SERIAL PRIMARY KEY,
    entity_id         INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    source            VARCHAR(20) NOT NULL,
    margin_available  NUMERIC(18, 2),
    margin_used       NUMERIC(18, 2),
    ledger_balance    NUMERIC(18, 2),
    day_realized_pnl  NUMERIC(18, 2),
    total_mtm_pnl     NUMERIC(18, 2),
    as_of_date        DATE,
    updated_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE (entity_id, source)
);
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
        print("fno_position and fno_account tables ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
