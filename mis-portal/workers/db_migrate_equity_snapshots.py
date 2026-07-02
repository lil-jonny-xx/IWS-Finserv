#!/usr/bin/env python3
"""
DB migration — equity_position_snapshot (intraday position snapshots).

The existing `equity_holding_history` table stores ONE row per holding per DAY
(the EOD close, keyed UNIQUE on entity/broker/symbol/snapshot_date). That is
enough for week/YTD anchors but cannot show *what was bought or sold during the
day*.

This table is the intraday counterpart: one row per holding per snapshot TICK
(open ~09:15, hourly, close ~15:30). `equity_snapshot_worker.py` writes a full
set of positions each tick and diffs consecutive ticks to detect the day's buys
and sells (a quantity drop = a sell, a rise / new symbol = a buy), which it then
records into `stock_transaction` so they flow into Realised Gains and the Equity
page's "Traded today" panel.

Only regular Indian equity brokers (BROKER_ENTITY_MAP) are snapshotted here —
foreign brokers have their own trade ledger and PMS accounts their own realised
feed.

Run once:  python -m workers.db_migrate_equity_snapshots
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

DDL = """
CREATE TABLE IF NOT EXISTS equity_position_snapshot (
    id             SERIAL PRIMARY KEY,
    entity_id      INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    broker         VARCHAR(20) NOT NULL,          -- zerodha | angel_one | dhan | ...
    symbol         VARCHAR(50) NOT NULL,
    isin           VARCHAR(20),
    captured_at    TIMESTAMPTZ NOT NULL,          -- tick timestamp (UTC)
    snapshot_kind  VARCHAR(10) NOT NULL,          -- open | hourly | close
    quantity       NUMERIC(20, 4) NOT NULL DEFAULT 0,
    price          NUMERIC(20, 4),                -- live price at the tick
    market_value   NUMERIC(20, 2),               -- quantity * price
    avg_cost       NUMERIC(20, 4),                -- holding avg cost (for reference/seed)
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (entity_id, broker, symbol, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_eq_pos_snap_lookup
    ON equity_position_snapshot (entity_id, broker, symbol, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_eq_pos_snap_captured
    ON equity_position_snapshot (captured_at DESC);
"""


def main():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        dbname=os.getenv("DB_NAME", "mis_portal"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
        conn.commit()
        print("equity_position_snapshot table ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
