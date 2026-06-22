#!/usr/bin/env python3
"""
DB migration — split foreign (multi-currency) equity into its own tables.

Foreign-currency holdings (IBKR / Vested → USD, DBS Wealth → SGD) used to share
the `equity_holding` / `equity_holding_history` tables with the Indian brokers.
They now live in dedicated tables so the Equity page is India-only and the new
"Foreign Equity" page can render native-currency figures with a currency switcher.

  * `foreign_equity_holding`          — mirror of `equity_holding`
  * `foreign_equity_holding_history`  — mirror of `equity_holding_history`

The INR-converted columns (cost, current_market_value, pnl_*, …) are retained so
the Overview / Reports / Assistant aggregators can UNION the new table and keep
group net-worth totals unchanged. The `*_native` + currency/fx_rate columns carry
the original USD/SGD/… figures for display.

Idempotent — safe to run repeatedly. Existing ibkr/vested/dbs rows are copied to
the new tables (ON CONFLICT DO NOTHING) and then removed from the equity_holding
tables.

Run once:  /var/www/.venv/bin/python -m equity.db_migrate_foreign_equity
"""
import os
import sys
import logging
import psycopg2

from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

FOREIGN_BROKERS = ("ibkr", "vested", "dbs")

# Columns copied verbatim from equity_holding (everything except the SERIAL id).
HOLDING_COLS = """
    entity_id, broker, symbol, isin, exchange,
    quantity, avg_cost, cost, current_price, current_market_value,
    prev_week_value, market_value_as_on, as_of_date,
    exposure_pct, weekly_change,
    pnl_ytd, pnl_inception, pnl_weekly_change,
    returns_ytd_pct, returns_inception_pct, cagr_inception_pct,
    first_invested_date, remarks, updated_at, sector,
    symbol_override, angel_one_token,
    currency, fx_rate,
    avg_cost_native, cost_native, current_price_native, current_market_value_native,
    xirr_inception_pct
"""

HISTORY_COLS = """
    entity_id, broker, symbol, snapshot_date,
    quantity, close_price, market_value, cost, pnl, isin,
    currency, fx_rate, close_price_native, market_value_native
"""

DDL = [
    (
        "create foreign_equity_holding",
        """
        CREATE TABLE IF NOT EXISTS foreign_equity_holding (
            id                          SERIAL PRIMARY KEY,
            entity_id                   INTEGER NOT NULL REFERENCES entity(id),
            broker                      VARCHAR(20) NOT NULL,
            symbol                      VARCHAR(50) NOT NULL,
            isin                        VARCHAR(20),
            exchange                    VARCHAR(10),

            quantity                    NUMERIC(18, 4)  NOT NULL,
            avg_cost                    NUMERIC(18, 4)  NOT NULL,
            cost                        NUMERIC(18, 2)  NOT NULL,
            current_price               NUMERIC(18, 4),
            current_market_value        NUMERIC(18, 2),

            prev_week_value             NUMERIC(18, 2),
            market_value_as_on          NUMERIC(18, 2),
            as_of_date                  DATE,

            exposure_pct                NUMERIC(8, 4),
            weekly_change               NUMERIC(18, 2),

            pnl_ytd                     NUMERIC(18, 2),
            pnl_inception               NUMERIC(18, 2),
            pnl_weekly_change           NUMERIC(18, 2),

            returns_ytd_pct             NUMERIC(8, 4),
            returns_inception_pct       NUMERIC(8, 4),
            cagr_inception_pct          NUMERIC(8, 4),

            first_invested_date         DATE,
            remarks                     TEXT,
            updated_at                  TIMESTAMP DEFAULT NOW(),
            sector                      TEXT,
            symbol_override             VARCHAR(50),
            angel_one_token             VARCHAR(20),

            currency                    VARCHAR(3)     NOT NULL DEFAULT 'INR',
            fx_rate                     NUMERIC(18, 6) NOT NULL DEFAULT 1,
            avg_cost_native             NUMERIC(18, 4),
            cost_native                 NUMERIC(18, 2),
            current_price_native        NUMERIC(18, 4),
            current_market_value_native NUMERIC(18, 2),
            xirr_inception_pct          NUMERIC(18, 4),

            UNIQUE (entity_id, broker, symbol)
        );
        """,
    ),
    (
        "foreign_equity_holding: index on entity_id",
        "CREATE INDEX IF NOT EXISTS idx_foreign_eh_entity ON foreign_equity_holding(entity_id);",
    ),
    (
        "foreign_equity_holding: index on isin",
        "CREATE INDEX IF NOT EXISTS idx_foreign_eh_isin ON foreign_equity_holding(isin);",
    ),
    (
        "create foreign_equity_holding_history",
        """
        CREATE TABLE IF NOT EXISTS foreign_equity_holding_history (
            id                  SERIAL PRIMARY KEY,
            entity_id           INTEGER NOT NULL REFERENCES entity(id),
            broker              VARCHAR(20) NOT NULL,
            symbol              VARCHAR(50) NOT NULL,
            snapshot_date       DATE        NOT NULL,

            quantity            NUMERIC(18, 4),
            close_price         NUMERIC(18, 4),
            market_value        NUMERIC(18, 2),
            cost                NUMERIC(18, 2),
            pnl                 NUMERIC(18, 2),
            isin                VARCHAR(20),

            currency            VARCHAR(3)     NOT NULL DEFAULT 'INR',
            fx_rate             NUMERIC(18, 6) NOT NULL DEFAULT 1,
            close_price_native  NUMERIC(18, 4),
            market_value_native NUMERIC(18, 2),

            UNIQUE (entity_id, broker, symbol, snapshot_date)
        );
        """,
    ),
    (
        "foreign_equity_holding_history: index on snapshot_date",
        "CREATE INDEX IF NOT EXISTS idx_foreign_eh_hist_date "
        "ON foreign_equity_holding_history(entity_id, broker, symbol, snapshot_date DESC);",
    ),
]


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def main():
    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    # 1. Schema
    for name, sql in DDL:
        try:
            cur.execute(sql)
            conn.commit()
            logger.info(f"✅  {name}")
        except Exception as e:
            conn.rollback()
            logger.error(f"❌  {name}: {e}")
            sys.exit(1)

    # 2. Move existing foreign rows out of the equity_holding tables.
    try:
        cur.execute(
            f"""
            INSERT INTO foreign_equity_holding ({HOLDING_COLS})
            SELECT {HOLDING_COLS} FROM equity_holding
            WHERE broker IN %s
            ON CONFLICT (entity_id, broker, symbol) DO NOTHING
            """,
            (FOREIGN_BROKERS,),
        )
        moved_h = cur.rowcount
        cur.execute("DELETE FROM equity_holding WHERE broker IN %s", (FOREIGN_BROKERS,))
        deleted_h = cur.rowcount

        cur.execute(
            f"""
            INSERT INTO foreign_equity_holding_history ({HISTORY_COLS})
            SELECT {HISTORY_COLS} FROM equity_holding_history
            WHERE broker IN %s
            ON CONFLICT (entity_id, broker, symbol, snapshot_date) DO NOTHING
            """,
            (FOREIGN_BROKERS,),
        )
        moved_hist = cur.rowcount
        cur.execute("DELETE FROM equity_holding_history WHERE broker IN %s", (FOREIGN_BROKERS,))
        deleted_hist = cur.rowcount

        conn.commit()
        logger.info(f"✅  moved holdings: {moved_h} inserted, {deleted_h} removed from equity_holding")
        logger.info(f"✅  moved history:  {moved_hist} inserted, {deleted_hist} removed from equity_holding_history")
    except Exception as e:
        conn.rollback()
        logger.error(f"❌  data move: {e}")
        sys.exit(1)

    cur.close()
    conn.close()
    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
