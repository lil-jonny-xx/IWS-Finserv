"""
Equity DB Migration
Adds equity_holding, equity_holding_history tables and extends
security_master and holding (MF) with metric columns.

Run once:
    python mis-portal/equity/db_migrate_equity.py
"""
import sys
import logging
import psycopg2
import psycopg2.extras
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv("/var/www/mis-portal/.env")

import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def get_conn():
    return psycopg2.connect(
        host     = os.environ["DB_HOST"],
        dbname   = os.environ["DB_NAME"],
        user     = os.environ["DB_USER"],
        password = os.environ["DB_PASSWORD"],
    )


MIGRATIONS = [

    # -------------------------------------------------------------------------
    # 1. Add exchange column to security_master (equity stocks need NSE/BSE)
    # -------------------------------------------------------------------------
    (
        "security_master: add exchange column",
        """
        ALTER TABLE security_master
            ADD COLUMN IF NOT EXISTS exchange VARCHAR(10);
        """,
    ),

    # -------------------------------------------------------------------------
    # 2. Extend holding (MF) with metric columns — mirrors equity columns
    #    so both MF and equity share the same reporting structure
    # -------------------------------------------------------------------------
    (
        "holding (MF): add metric columns",
        """
        ALTER TABLE holding
            ADD COLUMN IF NOT EXISTS prev_week_value       NUMERIC(18, 2),
            ADD COLUMN IF NOT EXISTS market_value_as_on    NUMERIC(18, 2),
            ADD COLUMN IF NOT EXISTS as_of_date            DATE,
            ADD COLUMN IF NOT EXISTS exposure_pct          NUMERIC(8, 4),
            ADD COLUMN IF NOT EXISTS weekly_change         NUMERIC(18, 2),
            ADD COLUMN IF NOT EXISTS pnl_ytd               NUMERIC(18, 2),
            ADD COLUMN IF NOT EXISTS pnl_inception         NUMERIC(18, 2),
            ADD COLUMN IF NOT EXISTS pnl_weekly_change     NUMERIC(18, 2),
            ADD COLUMN IF NOT EXISTS returns_ytd_pct       NUMERIC(8, 4),
            ADD COLUMN IF NOT EXISTS returns_inception_pct NUMERIC(8, 4),
            ADD COLUMN IF NOT EXISTS cagr_inception_pct    NUMERIC(8, 4),
            ADD COLUMN IF NOT EXISTS remarks               TEXT;
        """,
    ),

    # -------------------------------------------------------------------------
    # 3. equity_holding — one row per (entity, broker, symbol), upserted daily
    # -------------------------------------------------------------------------
    (
        "create equity_holding",
        """
        CREATE TABLE IF NOT EXISTS equity_holding (
            id                      SERIAL PRIMARY KEY,
            entity_id               INTEGER NOT NULL REFERENCES entity(id),
            broker                  VARCHAR(20) NOT NULL,
            symbol                  VARCHAR(50) NOT NULL,
            isin                    VARCHAR(20),
            exchange                VARCHAR(10),

            quantity                NUMERIC(18, 4)  NOT NULL,
            avg_cost                NUMERIC(18, 4)  NOT NULL,
            cost                    NUMERIC(18, 2)  NOT NULL,
            current_price           NUMERIC(18, 4),
            current_market_value    NUMERIC(18, 2),

            prev_week_value         NUMERIC(18, 2),
            market_value_as_on      NUMERIC(18, 2),
            as_of_date              DATE,

            exposure_pct            NUMERIC(8, 4),
            weekly_change           NUMERIC(18, 2),

            pnl_ytd                 NUMERIC(18, 2),
            pnl_inception           NUMERIC(18, 2),
            pnl_weekly_change       NUMERIC(18, 2),

            returns_ytd_pct         NUMERIC(8, 4),
            returns_inception_pct   NUMERIC(8, 4),
            cagr_inception_pct      NUMERIC(8, 4),

            first_invested_date     DATE,
            remarks                 TEXT,
            updated_at              TIMESTAMP DEFAULT NOW(),

            UNIQUE (entity_id, broker, symbol)
        );
        """,
    ),
    (
        "equity_holding: index on entity_id",
        "CREATE INDEX IF NOT EXISTS idx_equity_holding_entity ON equity_holding(entity_id);",
    ),
    (
        "equity_holding: index on isin",
        "CREATE INDEX IF NOT EXISTS idx_equity_holding_isin ON equity_holding(isin);",
    ),

    # -------------------------------------------------------------------------
    # 4. equity_holding_history — daily snapshot for prev_week / YTD lookups
    # -------------------------------------------------------------------------
    (
        "create equity_holding_history",
        """
        CREATE TABLE IF NOT EXISTS equity_holding_history (
            id              SERIAL PRIMARY KEY,
            entity_id       INTEGER NOT NULL REFERENCES entity(id),
            broker          VARCHAR(20) NOT NULL,
            symbol          VARCHAR(50) NOT NULL,
            snapshot_date   DATE        NOT NULL,

            quantity        NUMERIC(18, 4),
            close_price     NUMERIC(18, 4),
            market_value    NUMERIC(18, 2),
            cost            NUMERIC(18, 2),
            pnl             NUMERIC(18, 2),

            UNIQUE (entity_id, broker, symbol, snapshot_date)
        );
        """,
    ),
    (
        "equity_holding_history: index on snapshot_date",
        "CREATE INDEX IF NOT EXISTS idx_eq_hist_date ON equity_holding_history(entity_id, broker, symbol, snapshot_date DESC);",
    ),
]


def main():
    conn = get_conn()
    conn.autocommit = False
    cur  = conn.cursor()

    for name, sql in MIGRATIONS:
        try:
            cur.execute(sql)
            conn.commit()
            logger.info(f"✅  {name}")
        except Exception as e:
            conn.rollback()
            logger.error(f"❌  {name}: {e}")
            sys.exit(1)

    cur.close()
    conn.close()
    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
