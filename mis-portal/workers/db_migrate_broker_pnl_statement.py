#!/usr/bin/env python3
"""
DB migration — broker_pnl_statement + broker_pnl_line tables.

Brokers (Zerodha Console, Angel One, Dhan) each publish a realised-P&L statement
that gives, per scrip, the FY-aggregated Buy Value / Sell Value / Realised P&L.
Those figures are the broker's own authority for capital gains — computed with the
depository's true corporate-action history and grandfathering — so we treat them as
a per-scrip ORACLE to reconcile our FIFO engine against (report_generator.
_fifo_realised_grouped) rather than a source of trades. See:
  * equity/broker_pnl_statement.py  — the three parsers
  * equity/broker_pnl_ingest.py     — upsert into these tables
  * workers/reconcile_pnl_statements.py — diff vs our FIFO, classify each scrip
  * workers/backfill_from_statements.py — yfinance-gated corporate_action fixes

`broker_pnl_statement` holds one row per uploaded file; `broker_pnl_line` holds its
per-scrip rows (segment EQ / FnO). Nothing here mutates stock_transaction — the
statements never carry dated trades, only FY totals.

All amounts are INR (Indian brokers — no FX).

Idempotent — safe to run repeatedly.
Run once:  python -m workers.db_migrate_broker_pnl_statement
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS broker_pnl_statement (
    id             SERIAL PRIMARY KEY,
    entity_id      INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    broker         VARCHAR(20)  NOT NULL,          -- zerodha | angel_one | dhan
    client_id      VARCHAR(40),                    -- broker account id as printed on the statement
    period_from    DATE         NOT NULL,
    period_to      DATE         NOT NULL,
    fy_label       VARCHAR(12),                    -- e.g. 'FY24-25' (Apr->Mar of period_from); NULL if it spans FYs
    segment_totals JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- {"EQ": {"realised": .., "charges": ..}, "FnO": {..}}
    stored_path    TEXT,                           -- where the uploaded file was spooled
    downloaded_at  TIMESTAMP,                      -- the statement's own "downloaded at" stamp, if any
    created_at     TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Re-uploading the same statement (same account + same window) REPLACES it rather
-- than piling up duplicates. The ingest deletes lines on conflict and re-inserts.
CREATE UNIQUE INDEX IF NOT EXISTS uq_broker_pnl_statement
    ON broker_pnl_statement (entity_id, broker, client_id, period_from, period_to);
CREATE INDEX IF NOT EXISTS idx_broker_pnl_statement_scope
    ON broker_pnl_statement (entity_id, broker, fy_label);

CREATE TABLE IF NOT EXISTS broker_pnl_line (
    id             SERIAL PRIMARY KEY,
    statement_id   INTEGER NOT NULL REFERENCES broker_pnl_statement(id) ON DELETE CASCADE,
    segment        VARCHAR(4) NOT NULL DEFAULT 'EQ',   -- EQ | FnO
    security_name  VARCHAR(200) NOT NULL,
    isin           VARCHAR(20),                        -- Zerodha carries it; Angel/Dhan do not
    quantity       NUMERIC(20, 4),
    buy_value      NUMERIC(20, 2),
    sell_value     NUMERIC(20, 2),
    realised_pnl   NUMERIC(20, 2) NOT NULL,
    st_pnl         NUMERIC(20, 2),                     -- Angel splits ST/LT; others leave NULL
    lt_pnl         NUMERIC(20, 2),
    return_pct     NUMERIC(12, 4)
);
CREATE INDEX IF NOT EXISTS idx_broker_pnl_line_stmt ON broker_pnl_line (statement_id);
CREATE INDEX IF NOT EXISTS idx_broker_pnl_line_isin ON broker_pnl_line (isin);
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
        print("broker_pnl_statement + broker_pnl_line tables ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
