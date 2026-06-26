#!/usr/bin/env python3
"""
DB migration — asset-class tagging for the Gold/Silver split (Phase 1).

Adds an `asset_class` column to both holding tables and an admin-editable
`asset_class_override` table, then backfills `asset_class` on every existing
row using the curated classifier (equity/asset_class.py).

After this runs:
  * gold ETFs / sovereign gold bonds  -> asset_class = 'gold'
  * silver ETFs                        -> asset_class = 'silver'
  * tracked commodities (URNU, …)      -> asset_class = 'commodity'
  * everything else                    -> asset_class = 'equity'

The Equity page filters these non-equity classes OUT; the new Gold/Silver page
filters them IN. The daily sync keeps the column fresh for new buys.

Idempotent — safe to run repeatedly.

  /var/www/.venv/bin/python -m equity.db_migrate_asset_class
"""
import os
import sys
import logging
import psycopg2

from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

# Reuse the same curated classifier the daily sync uses, so the backfill and the
# live stamping never disagree.
from equity.asset_class import classify_asset_class

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

HOLDING_TABLES = ("equity_holding", "foreign_equity_holding")

DDL = [
    (
        "equity_holding: add asset_class",
        "ALTER TABLE equity_holding "
        "ADD COLUMN IF NOT EXISTS asset_class VARCHAR(16);",
    ),
    (
        "foreign_equity_holding: add asset_class",
        "ALTER TABLE foreign_equity_holding "
        "ADD COLUMN IF NOT EXISTS asset_class VARCHAR(16);",
    ),
    (
        "equity_holding: index on asset_class",
        "CREATE INDEX IF NOT EXISTS idx_eh_asset_class "
        "ON equity_holding(asset_class);",
    ),
    (
        "foreign_equity_holding: index on asset_class",
        "CREATE INDEX IF NOT EXISTS idx_feh_asset_class "
        "ON foreign_equity_holding(asset_class);",
    ),
    (
        "create asset_class_override",
        """
        CREATE TABLE IF NOT EXISTS asset_class_override (
            id          SERIAL PRIMARY KEY,
            symbol      VARCHAR(50),
            isin        VARCHAR(20),
            asset_class VARCHAR(16) NOT NULL,
            set_by      VARCHAR(120),
            set_at      TIMESTAMP DEFAULT NOW(),
            CHECK (symbol IS NOT NULL OR isin IS NOT NULL)
        );
        """,
    ),
    (
        "asset_class_override: unique symbol",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_aco_symbol "
        "ON asset_class_override (UPPER(symbol)) WHERE symbol IS NOT NULL;",
    ),
    (
        "asset_class_override: unique isin",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_aco_isin "
        "ON asset_class_override (UPPER(isin)) WHERE isin IS NOT NULL;",
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

    # 2. Load overrides (just-created table; empty on first run) so the backfill
    #    matches what the live sync would stamp.
    from equity.asset_class import load_overrides
    overrides = load_overrides(cur)

    # 3. Backfill asset_class on every existing holding row.
    for tbl in HOLDING_TABLES:
        try:
            cur.execute(f"SELECT id, symbol, isin FROM {tbl}")
            rows = cur.fetchall()
            updated = 0
            buckets: dict = {}
            for hid, symbol, isin in rows:
                cls = classify_asset_class(symbol or "", isin or "", overrides)
                cur.execute(
                    f"UPDATE {tbl} SET asset_class = %s WHERE id = %s", (cls, hid)
                )
                updated += 1
                buckets[cls] = buckets.get(cls, 0) + 1
            conn.commit()
            logger.info(f"✅  {tbl}: backfilled {updated} rows {buckets}")
        except Exception as e:
            conn.rollback()
            logger.error(f"❌  {tbl} backfill: {e}")
            sys.exit(1)

    cur.close()
    conn.close()
    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
