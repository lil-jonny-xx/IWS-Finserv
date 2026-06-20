#!/usr/bin/env python3
"""
DB migration — international (multi-currency) broker support.

Adds the columns needed to hold positions denominated in a foreign currency
(Interactive Brokers / Vested → USD, DBS Wealth Singapore → SGD) alongside the
existing INR-denominated Indian broker holdings.

Design:
  * The existing money columns (avg_cost, cost, current_price,
    current_market_value, balance, …) continue to store **INR-converted**
    values so every downstream INR consumer (Equity totals, /overview
    aggregator, XLSX reports) keeps working unchanged.
  * New `*_native` columns store the original USD/SGD figures for display.
  * `currency` + `fx_rate` (INR per 1 unit of `currency`, as of `as_of_date`)
    record what conversion was applied. INR rows get currency='INR', fx_rate=1.

FX rates come from the existing `fx_rate` table (populated daily by
workers/fx_rates_worker.py for USD/GBP/SGD/AED/HKD → INR).

Idempotent — safe to run repeatedly.

Run once:  /var/www/.venv/bin/python -m equity.db_migrate_intl_brokers
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
-- equity_holding -----------------------------------------------------------
ALTER TABLE equity_holding
    ADD COLUMN IF NOT EXISTS currency                    VARCHAR(3)     NOT NULL DEFAULT 'INR',
    ADD COLUMN IF NOT EXISTS fx_rate                     NUMERIC(18, 6) NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS avg_cost_native             NUMERIC(18, 4),
    ADD COLUMN IF NOT EXISTS cost_native                 NUMERIC(18, 2),
    ADD COLUMN IF NOT EXISTS current_price_native        NUMERIC(18, 4),
    ADD COLUMN IF NOT EXISTS current_market_value_native NUMERIC(18, 2),
    -- Money-weighted return (XIRR), annualised %, computed from the holding's
    -- dated cash-flow ledger by the foreign-broker scrapers (Vested / DBS) in
    -- the security's NATIVE currency. NULL for brokers with no transaction
    -- history (the Indian brokers only expose a current snapshot).
    ADD COLUMN IF NOT EXISTS xirr_inception_pct          NUMERIC(18, 4);

-- equity_holding_history ---------------------------------------------------
ALTER TABLE equity_holding_history
    ADD COLUMN IF NOT EXISTS currency             VARCHAR(3)     NOT NULL DEFAULT 'INR',
    ADD COLUMN IF NOT EXISTS fx_rate              NUMERIC(18, 6) NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS close_price_native   NUMERIC(18, 4),
    ADD COLUMN IF NOT EXISTS market_value_native  NUMERIC(18, 2);

-- broker_cash --------------------------------------------------------------
ALTER TABLE broker_cash
    ADD COLUMN IF NOT EXISTS currency        VARCHAR(3)     NOT NULL DEFAULT 'INR',
    ADD COLUMN IF NOT EXISTS fx_rate         NUMERIC(18, 6) NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS balance_native  NUMERIC(20, 2);
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
        print("international broker columns ready "
              "(equity_holding, equity_holding_history, broker_cash).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
