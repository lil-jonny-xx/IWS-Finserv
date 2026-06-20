#!/usr/bin/env python3
"""
DB migration — equity trade ledger (money-weighted return / XIRR support).

A per-(entity, broker, symbol) dated cash-flow ledger in the security's NATIVE
currency. Buys are stored as a negative cash flow, sells as positive, each net of
commission. The nightly equity sync reads a holding's ledger, appends the current
market value as the final inflow, and runs finmath.xirr() — so XIRR stays correct
as the market value moves, without re-pulling broker history every day.

Populated one-time from inception by equity/ibkr_backfill_inception.py (and, as
they gain transaction history, the Vested / DBS scrapers).

Idempotent — safe to run repeatedly.
Run once:  /var/www/.venv/bin/python -m equity.db_migrate_trade_ledger
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS equity_trade_ledger (
    id               BIGSERIAL     PRIMARY KEY,
    entity_id        INTEGER       NOT NULL,
    broker           VARCHAR(32)   NOT NULL,
    symbol           VARCHAR(64)   NOT NULL,
    isin             VARCHAR(20),
    trade_date       DATE          NOT NULL,
    side             VARCHAR(4)    NOT NULL,            -- BUY / SELL
    quantity         NUMERIC(24, 8) NOT NULL,          -- absolute quantity
    price_native     NUMERIC(24, 8) NOT NULL,
    fees_native      NUMERIC(24, 8) NOT NULL DEFAULT 0,
    currency         VARCHAR(3)    NOT NULL DEFAULT 'USD',
    -- Signed cash flow in NATIVE currency, net of commission: buy < 0, sell > 0.
    cash_flow_native NUMERIC(28, 8) NOT NULL,
    source           VARCHAR(32)   NOT NULL DEFAULT 'ibkr_flex',
    external_id      VARCHAR(64),                       -- broker trade id (dedup key)
    created_at       TIMESTAMP     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trade_ledger_holding
    ON equity_trade_ledger (entity_id, broker, symbol);

-- A broker trade id is unique per broker, so re-importing the same statement is a
-- no-op. Partial unique index so manual rows (external_id NULL) are still allowed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_ledger_extid
    ON equity_trade_ledger (broker, external_id) WHERE external_id IS NOT NULL;
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
        print("equity_trade_ledger ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
