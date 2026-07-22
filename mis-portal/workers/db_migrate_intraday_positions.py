#!/usr/bin/env python3
"""
Add the intraday (unsettled) position columns to the holding tables.

Today's buys are not holdings yet. Zerodha and Dhan do not report them in their
holdings feed at all (they sit in the positions API until settlement), and Angel
One folds them straight into `quantity` — so the same trade was presented three
different ways depending on the broker.

These columns carry today's activity ALONGSIDE the settled quantity rather than
inside it, so `quantity` keeps meaning "settled, in demat" for every consumer that
already depends on it — daily snapshots, FIFO/XIRR metrics, the XLSX reports, the
ghost prune and the holdings-vs-ledger reconcile all keep their existing numbers.
Only the Equity page adds them, as its own labelled line.

foreign_equity_holding gets the same columns even though nothing writes them:
_EQUITY_HOLDING_COLS in main.py is shared by the equity, foreign-equity and
gold/silver endpoints (the last as a UNION), so a column present on one table and
missing on the other makes all of those tabs render EMPTY rather than error.

Idempotent — safe to re-run.
"""
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

COLUMNS = [
    ("intraday_qty",      "NUMERIC(18,4)"),   # today's net traded qty (signed: sells negative)
    ("intraday_avg_cost", "NUMERIC(18,4)"),   # today's average traded price
    ("intraday_value",    "NUMERIC(18,2)"),   # intraday_qty * current price, in INR
    ("intraday_as_of",    "DATE"),            # the session these figures belong to
]
TABLES = ["equity_holding", "foreign_equity_holding"]


def main():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
    )
    cur = conn.cursor()
    added = 0
    for table in TABLES:
        for col, coltype in COLUMNS:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = %s", (table, col))
            if cur.fetchone():
                print(f"  {table}.{col}: already present")
                continue
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            print(f"  {table}.{col}: ADDED ({coltype})")
            added += 1

    # Only ever read for the current session; a stale row from a previous day must not
    # linger on the page, and the sync clears them by date rather than by presence.
    cur.execute("CREATE INDEX IF NOT EXISTS idx_equity_holding_intraday "
                "ON equity_holding (intraday_as_of) WHERE intraday_qty IS NOT NULL")

    conn.commit()
    print(f"\n{added} column(s) added.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
